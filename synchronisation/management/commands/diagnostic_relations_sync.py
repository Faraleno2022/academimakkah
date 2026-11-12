"""
Diagnostic (lecture seule) des changements de synchronisation refusés.

1. Refus « Relation introuvable » : un poste envoie un changement (paiement, échéancier,
   salaire…) qui désigne une fiche parente (élève, enseignant…) inconnue du serveur. La
   commande dit, par école, combien de fiches manquent, si les refus continuent
   aujourd'hui, et pour les paiements si le reçu est déjà en ligne.
2. Causes racines : les AUTRES refus (doublon de matricule ou de reçu, validation…),
   regroupés par erreur. Une fiche parente manque souvent parce que SA création a été
   refusée pour une autre raison : c'est cette erreur qu'il faut corriger.

Ne modifie aucune donnée. Sortie courte par défaut ; ``--detail`` liste chaque fiche.
"""

from collections import defaultdict
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from eleves.models import Ecole
from synchronisation.engine import get_model, objet_par_uuid
from synchronisation.models import SyncChange

PREFIXE = 'Relation introuvable pour '
CHAMPS_UTILES = (
    'numero_recu', 'montant', 'date_paiement', 'statut', 'annee_scolaire',
    'description', 'action', 'matricule',
)
JOURS_ACTIF = 3


def _fiche_manquante(change):
    """(champ, modèle parent, uuid de la fiche manquante) d'après le refus et le payload."""
    champ = (change.erreur or '').split(PREFIXE, 1)[-1].strip().rstrip('.')
    brut = (change.payload or {}).get(champ)
    uuid = brut.get('sync_uuid') if isinstance(brut, dict) else None
    modele = get_model(change.model_label)
    parent = None
    if modele is not None and champ:
        try:
            parent = modele._meta.get_field(champ).remote_field.model
        except Exception:  # noqa: BLE001
            parent = None
    return champ, parent, uuid


def _gnf(valeur) -> str:
    try:
        return f"{int(float(valeur)):,}".replace(',', ' ')
    except (TypeError, ValueError):
        return str(valeur)


class Groupe:
    """Résumé d'une fiche manquante (aucune ligne de refus n'est gardée en mémoire)."""

    def __init__(self, ecole_id, label_parent, champ, uuid):
        self.ecole_id, self.label_parent, self.champ, self.uuid = ecole_id, label_parent, champ, uuid
        self.nombre, self.premier, self.dernier = 0, None, None
        self.bloques = defaultdict(int)
        self.nom = ''
        self.recus = {}          # numero_recu -> montant
        self.indices = []

    def ajouter(self, change):
        self.nombre += 1
        date = change.date_creation
        self.premier = date if self.premier is None or date < self.premier else self.premier
        self.dernier = date if self.dernier is None or date > self.dernier else self.dernier
        self.bloques[f"{change.operation} {change.model_label}"] += 1
        payload = change.payload or {}
        description = str(payload.get('description') or '')
        if not self.nom and description.startswith('Création du profil de '):
            self.nom = description.split('Création du profil de ', 1)[1].strip()
        if change.model_label == 'paiements.Paiement' and payload.get('numero_recu'):
            self.recus[str(payload['numero_recu'])] = payload.get('montant')
        if len(self.indices) < 4:
            indice = {k: v for k, v in payload.items() if k in CHAMPS_UTILES and v not in ('', None)}
            if indice and indice not in self.indices:
                self.indices.append(indice)


class Command(BaseCommand):
    help = "Lecture seule : fiches manquantes derrière les refus « Relation introuvable » et causes racines des autres refus."

    def add_arguments(self, parser):
        parser.add_argument('--ecole', type=int, help="Limiter à une école (id).")
        parser.add_argument('--detail', action='store_true', help="Lister chaque fiche manquante.")

    def handle(self, *args, **options):
        from paiements.models import Paiement

        maintenant = timezone.now()
        ecoles = {e.pk: e.nom for e in Ecole.objects.all()}
        filtre = {'ecole_id': options['ecole']} if options['ecole'] else {}
        echecs = SyncChange.objects.filter(statut=SyncChange.STATUT_FAILED, **filtre)

        # ---- 1. Refus « Relation introuvable », regroupés par fiche manquante
        groupes = {}
        refus = echecs.filter(erreur__startswith=PREFIXE).order_by('id')
        for change in refus.iterator(chunk_size=500):
            champ, parent, uuid = _fiche_manquante(change)
            label = parent._meta.label if parent else '?'
            cle = (change.ecole_id, label, champ, str(uuid or ''))
            groupes.setdefault(cle, Groupe(*cle)).ajouter(change)

        total_echecs = echecs.count()
        self.stdout.write(f"{total_echecs} changement(s) refusé(s) au total, dont {sum(g.nombre for g in groupes.values())} "
                          f"« Relation introuvable » ({len(groupes)} fiche(s) manquante(s)).\n")

        if groupes:
            # Présence en ligne : un reçu ou un élève peut avoir été rattrapé depuis.
            recus = {r for g in groupes.values() for r in g.recus}
            en_ligne = {}
            if recus:
                for p in Paiement.objects.filter(numero_recu__in=recus).select_related('eleve__classe'):
                    en_ligne[(p.eleve.classe.ecole_id if p.eleve and p.eleve.classe else None, p.numero_recu)] = p
            existe = {}
            for g in groupes.values():
                parent = get_model(g.label_parent) if g.label_parent != '?' else None
                existe[g] = bool(parent is not None and g.uuid and objet_par_uuid(parent, g.uuid) is not None)

            self.stdout.write(self.style.MIGRATE_HEADING("1. Fiches manquantes par école"))
            par_ecole = defaultdict(list)
            for g in groupes.values():
                par_ecole[g.ecole_id].append(g)
            for ecole_id, liste in sorted(par_ecole.items(), key=lambda x: -sum(g.nombre for g in x[1])):
                dernier = max(g.dernier for g in liste)
                actifs = [g for g in liste if g.dernier >= maintenant - timedelta(days=JOURS_ACTIF)]
                par_type = defaultdict(int)
                for g in liste:
                    par_type[g.label_parent] += 1
                self.stdout.write(self.style.WARNING(f"■ {ecoles.get(ecole_id, f'école {ecole_id}')}"))
                self.stdout.write(
                    f"  {sum(g.nombre for g in liste)} refus, {len(liste)} fiche(s) manquante(s) : "
                    + ", ".join(f"{n} {k}" for k, n in sorted(par_type.items(), key=lambda x: -x[1])))
                self.stdout.write(
                    f"  dernier refus : {dernier:%d/%m/%Y %H:%M} — "
                    + (f"{len(actifs)} fiche(s) encore en boucle ces {JOURS_ACTIF} derniers jours" if actifs
                       else "plus aucun refus récent (boucle arrêtée)")
                    + f" — {sum(1 for g in liste if existe[g])} existent maintenant en ligne")

            self.stdout.write(self.style.MIGRATE_HEADING("\n2. Paiements bloqués : le reçu est-il en ligne ?"))
            lignes = []
            for g in groupes.values():
                for recu, montant in g.recus.items():
                    p = en_ligne.get((g.ecole_id, recu))
                    autre = (not p) and Paiement.objects.filter(numero_recu=recu).select_related('eleve__classe').first()
                    if p:
                        etat = 'EN LIGNE'
                    elif autre:
                        ecole_autre = ecoles.get(autre.eleve.classe.ecole_id, '?') if autre.eleve and autre.eleve.classe else '?'
                        etat = f"ABSENT — ce numéro est pris en ligne par l'école « {ecole_autre} »"
                    else:
                        etat = 'ABSENT'
                    lignes.append((ecoles.get(g.ecole_id, '?'), recu, montant, g.nom or '(nom inconnu)', etat, g.dernier))
            if not lignes:
                self.stdout.write("  Aucun paiement concerné.")
            else:
                absents = [l for l in lignes if l[4].startswith('ABSENT')]
                pris = [l for l in absents if 'est pris' in l[4]]
                self.stdout.write(
                    f"  {len(lignes)} paiement(s) concerné(s) : {len(lignes) - len(absents)} en ligne, "
                    f"{len(absents)} ABSENT(S) dont {len(pris)} dont le numéro est déjà pris par une autre école.")
                limite = None if options['detail'] else 25
                for ecole, recu, montant, nom, etat, dernier in sorted(absents, key=lambda l: (l[0], l[1]))[:limite]:
                    self.stdout.write(f"  - {ecole} | {recu} | {_gnf(montant)} GNF | {nom} | {etat}")
                if limite and len(absents) > limite:
                    self.stdout.write(f"  … {len(absents) - limite} autre(s) (option --detail pour tout voir)")

            if options['detail']:
                self.stdout.write(self.style.MIGRATE_HEADING("\n3. Détail par fiche manquante"))
                for g in sorted(groupes.values(), key=lambda g: -g.nombre):
                    self.stdout.write(
                        f"■ {g.label_parent} {g.uuid} — {ecoles.get(g.ecole_id, '?')} — {g.nombre} refus "
                        f"({g.premier:%d/%m/%Y} → {g.dernier:%d/%m/%Y}) {g.nom}")
                    self.stdout.write("  " + " ; ".join(f"{n} × {k}" for k, n in g.bloques.items()))

        # ---- 2. Causes racines : les autres refus
        self.stdout.write(self.style.MIGRATE_HEADING("\n4. Causes racines : autres refus, regroupés par erreur"))
        autres = defaultdict(lambda: [0, None, None, set()])
        for modele, erreur, date, ecole_id in (
            echecs.exclude(erreur__startswith=PREFIXE).values_list('model_label', 'erreur', 'date_creation', 'ecole_id').iterator(chunk_size=1000)
        ):
            resume = ' '.join((erreur or '(sans message)').split())[:140]
            ligne = autres[(modele, resume)]
            ligne[0] += 1
            ligne[1] = date if ligne[1] is None or date < ligne[1] else ligne[1]
            ligne[2] = date if ligne[2] is None or date > ligne[2] else ligne[2]
            ligne[3].add(ecoles.get(ecole_id, str(ecole_id)))
        if not autres:
            self.stdout.write("  Aucun autre refus.")
        for (modele, resume), (nombre, premier, dernier, noms) in sorted(autres.items(), key=lambda x: -x[1][0])[:20]:
            self.stdout.write(f"  {nombre:>6} × {modele} — {resume}")
            self.stdout.write(f"          du {premier:%d/%m/%Y} au {dernier:%d/%m/%Y} — {', '.join(sorted(noms))}")

        self.stdout.write("\nLecture seule : aucune donnée modifiée.")
