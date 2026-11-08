"""Remet dans la classe de leur année les notes déplacées par un ancien transfert.

Un changement de classe en cours d'année déplaçait aussi les notes des années
passées vers la nouvelle classe : une note de 2024-2025 pouvait se retrouver sur
une matière de 2025-2026. Le moteur de calcul ne la voit plus, et le livret de
cette année-là reste vide.

Sans option, la commande ne modifie rien et liste ce qu'elle ferait. Une note
n'est déplacée que si tout est certain : classe de l'élève connue pour cette
année, matière équivalente, et aucune note déjà présente à cet endroit.
"""
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
    matieres = list(classe_note.matieres.all())
    for candidate in matieres:
        if matiere.code and candidate.code.upper() == matiere.code.upper():
            return candidate
    nom = matiere.nom.upper().strip()
    for candidate in matieres:
        if candidate.nom.upper().strip() == nom:
            return candidate
    return None


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
    if modele.objects.filter(
        eleve=note.eleve, matiere=matiere, annee_scolaire=annee,
        **{champ: getattr(note, champ)},
    ).exists():
        return None, f"une note existe déjà dans {nom_classe} en {annee}"
    return matiere, None


class Command(BaseCommand):
    help = ("Remet dans la classe de leur année les notes rattachées à une classe d'une "
            "autre année. Sans --appliquer, ne modifie rien.")

    def add_arguments(self, parser):
        parser.add_argument('--appliquer', action='store_true',
                            help="Effectue réellement les déplacements.")

    def handle(self, *args, **options):
        appliquer = options['appliquer']
        reparables = a_verifier = 0
        for modele, champ in CHAMP_PERIODE:
            notes = (modele.objects
                     .exclude(matiere__classe__annee_scolaire=F('annee_scolaire'))
                     .select_related('eleve__classe', 'matiere__classe__ecole'))
            for note in notes:
                matiere, raison = _destination(note, modele, champ)
                libelle = (f"{modele.__name__} {note.pk} — {note.eleve.matricule} "
                           f"{note.annee_scolaire} {getattr(note, champ)}")
                if raison:
                    a_verifier += 1
                    self.stdout.write(f"À VÉRIFIER  {libelle} : {raison}")
                    continue
                reparables += 1
                self.stdout.write(f"{'RÉPARÉE' if appliquer else 'RÉPARABLE'}  {libelle} : "
                                  f"{note.matiere.classe.nom} ({note.matiere.classe.annee_scolaire}) → "
                                  f"{matiere.classe.nom} ({matiere.classe.annee_scolaire})")
                if appliquer:
                    with transaction.atomic():
                        note.matiere = matiere
                        note.save(update_fields=['matiere'])

        self.stdout.write(f"{reparables} note(s) {'réparée(s)' if appliquer else 'réparable(s)'}, "
                          f"{a_verifier} note(s) à vérifier.")
        if reparables and not appliquer:
            self.stdout.write("Rien n'a été modifié. Relancer avec --appliquer pour réparer.")
