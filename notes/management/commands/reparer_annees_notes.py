"""Remet dans la classe de leur année les notes déplacées par un ancien transfert.

Un changement de classe en cours d'année déplaçait aussi les notes des années
passées vers la nouvelle classe : une note de 2024-2025 pouvait se retrouver sur
une matière de 2025-2026. Le moteur de calcul ne la voit plus, et le livret de
cette année-là reste vide.

Sans --appliquer, la commande ne modifie rien et liste ce qu'elle ferait. Une note
n'est déplacée que si tout est certain : classe de l'élève connue pour cette
année, matière équivalente, et aucune note déjà présente à cet endroit.

--journal reprend en plus les notes qu'une réparation précédente a rangées sur une
matière inactive alors qu'une matière active du même nom existe.
"""
import re

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import F

from notes.livret_scolaire import classe_note_par_nom, classes_quittees_selon_historique
from notes.models import AppreciationMaternelle, CompositionNote, NoteMensuelle

CHAMP_PERIODE = (
    (NoteMensuelle, 'mois'),
    (CompositionNote, 'periode'),
    (AppreciationMaternelle, 'trimestre'),
)


def _classe_de_l_annee(eleve, annee):
    if eleve.classe and eleve.classe.annee_scolaire == annee:
        return eleve.classe.nom
    return classes_quittees_selon_historique(eleve).get(annee)


def _matiere_equivalente(classe_note, matiere):
    """Même code, sinon même nom, en préférant une matière active.

    Bulletins et livrets ne lisent que les matières actives. Supprimer une matière
    qui contient des notes la désactive seulement, et une autre du même nom la
    remplace souvent sous un autre code.
    """
    code = (matiere.code or '').upper()
    nom = matiere.nom.upper().strip()
    matieres = list(classe_note.matieres.all())
    for actif in (True, False):
        for candidate in matieres:
            if candidate.actif == actif and code and candidate.code.upper() == code:
                return candidate
        for candidate in matieres:
            if candidate.actif == actif and candidate.nom.upper().strip() == nom:
                return candidate
    return None


def _deja_occupee(modele, champ, note, matiere):
    return modele.objects.filter(
        eleve=note.eleve, matiere=matiere, annee_scolaire=note.annee_scolaire,
        **{champ: getattr(note, champ)},
    ).exists()


def _destination(note, modele, champ):
    """(matière de destination, None) ou (None, raison de ne pas y toucher)."""
    annee = note.annee_scolaire
    nom_classe = _classe_de_l_annee(note.eleve, annee)
    if not nom_classe:
        return None, f"classe de l'élève inconnue pour {annee}"
    classe_note = classe_note_par_nom(note.matiere.classe.ecole, annee, nom_classe)
    if not classe_note:
        return None, f"pas de classe de notes « {nom_classe} » en {annee}"
    matiere = _matiere_equivalente(classe_note, note.matiere)
    if not matiere:
        return None, f"pas de matière « {note.matiere.nom} » dans {nom_classe} en {annee}"
    if _deja_occupee(modele, champ, note, matiere):
        return None, f"une note existe déjà dans {nom_classe} en {annee}"
    return matiere, None


def _notes_du_journal(chemin):
    """Notes marquées RÉPARÉE dans la sortie d'un précédent --appliquer."""
    with open(chemin, encoding='utf-8') as journal:
        reparees = set(re.findall(r'^RÉPARÉE\s+(\w+) (\d+) ', journal.read(), re.M))
    for modele, champ in CHAMP_PERIODE:
        pks = [pk for nom, pk in reparees if nom == modele.__name__]
        notes = (modele.objects.filter(pk__in=pks, matiere__actif=False)
                 .select_related('eleve', 'matiere__classe'))
        yield from ((note, modele, champ) for note in notes)


class Command(BaseCommand):
    help = ("Remet dans la classe de leur année les notes rattachées à une classe d'une "
            "autre année. Sans --appliquer, ne modifie rien.")

    def add_arguments(self, parser):
        parser.add_argument('--appliquer', action='store_true',
                            help="Effectue réellement les déplacements.")
        parser.add_argument('--journal', metavar='FICHIER',
                            help="Sortie d'un précédent --appliquer : reprend ses notes restées "
                                 "sur une matière inactive quand une matière active du même nom existe.")

    def handle(self, *args, **options):
        self.appliquer = options['appliquer']
        self.reparables = self.a_verifier = 0

        if options['journal']:
            for note, modele, champ in _notes_du_journal(options['journal']):
                matiere = _matiere_equivalente(note.matiere.classe, note.matiere)
                if not matiere.actif:
                    continue
                raison = None
                if _deja_occupee(modele, champ, note, matiere):
                    raison = f"une note existe déjà sur la matière active {matiere.nom} ({matiere.code})"
                self._traiter(note, modele, champ, matiere, raison)

        for modele, champ in CHAMP_PERIODE:
            notes = (modele.objects
                     .exclude(matiere__classe__annee_scolaire=F('annee_scolaire'))
                     .select_related('eleve__classe', 'matiere__classe__ecole'))
            for note in notes:
                matiere, raison = _destination(note, modele, champ)
                self._traiter(note, modele, champ, matiere, raison)

        self.stdout.write(f"{self.reparables} note(s) {'réparée(s)' if self.appliquer else 'réparable(s)'}, "
                          f"{self.a_verifier} note(s) à vérifier.")
        if self.reparables and not self.appliquer:
            self.stdout.write("Rien n'a été modifié. Relancer avec --appliquer pour réparer.")

    def _traiter(self, note, modele, champ, matiere, raison):
        libelle = (f"{modele.__name__} {note.pk} — {note.eleve.matricule} "
                   f"{note.annee_scolaire} {getattr(note, champ)}")
        if raison:
            self.a_verifier += 1
            self.stdout.write(f"À VÉRIFIER  {libelle} : {raison}")
            return
        self.reparables += 1
        origine = note.matiere
        self.stdout.write(f"{'RÉPARÉE' if self.appliquer else 'RÉPARABLE'}  {libelle} : "
                          f"{origine.classe.nom} {origine.code} ({origine.classe.annee_scolaire}) → "
                          f"{matiere.classe.nom} {matiere.code} ({matiere.classe.annee_scolaire})")
        if self.appliquer:
            with transaction.atomic():
                note.matiere = matiere
                note.save(update_fields=['matiere'])
