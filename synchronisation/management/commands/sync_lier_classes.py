"""
Serveur : relie à une classe EN LIGNE les classes qu'un poste a créées mais que le serveur
n'a jamais reçues.

Cas : un élève du poste désigne sa classe par un identifiant (sync_uuid) que le serveur ne
connaît pas ; l'élève, ses paiements, son échéancier et son historique restent donc refusés
(« Relation introuvable pour classe »). Si la classe existe déjà en ligne sous un autre
identifiant, on enregistre un alias (le même mécanisme que la fusion de doublons) : l'identifiant
du poste désigne alors la classe en ligne.

La classe en ligne est retrouvée par le préfixe des matricules des élèves concernés
(« TPS-001 » -> classe de code matricule « TPS » dans la même école). Une correspondance
douteuse (aucune ou plusieurs classes) n'est jamais liée toute seule : elle est listée avec les
classes possibles ; ``--uuid`` et ``--classe`` permettent de décider à la main.

Simulation par défaut ; ``--confirmer`` enregistre. Ensuite, relancer ``sync_rejouer_serveur``.
"""

import re
from collections import defaultdict

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from eleves.models import Classe, Ecole
from synchronisation.engine import objet_par_uuid, retenir_alias
from synchronisation.models import SyncChange

PREFIXE_ERREUR = 'Relation introuvable pour classe'


def prefixe_matricule(matricule: str) -> str:
    """« GSE/TPS-001 » -> « TPS » ; « PN2-003 » -> « PN2 »."""
    texte = (matricule or '').strip().split('/')[-1]
    return re.sub(r'-\d+$', '', texte).strip()


class Command(BaseCommand):
    help = "Relie les classes d'un poste jamais reçues par le serveur à leur classe en ligne (simulation par défaut)."

    def add_arguments(self, parser):
        parser.add_argument('--ecole', type=int, help="Limiter à une école (id).")
        parser.add_argument('--uuid', help="sync_uuid de la classe du poste (liaison manuelle, avec --classe).")
        parser.add_argument('--classe', type=int, help="Id de la classe EN LIGNE à lui associer.")
        parser.add_argument('--confirmer', action='store_true', help="Enregistre réellement (sinon simulation).")

    def handle(self, *args, **options):
        if bool(options['uuid']) != bool(options['classe']):
            raise CommandError("--uuid et --classe vont ensemble.")
        ecoles = {e.pk: e.nom for e in Ecole.objects.all()}

        # Classes que des changements refusés désignent sans que le serveur les connaisse
        manquantes = {}
        refus = SyncChange.objects.filter(
            statut=SyncChange.STATUT_FAILED, erreur__startswith=PREFIXE_ERREUR, device__isnull=False)
        if options['ecole']:
            refus = refus.filter(ecole_id=options['ecole'])
        for change in refus.order_by('id').iterator(chunk_size=500):
            ref = (change.payload or {}).get('classe')
            uuid = ref.get('sync_uuid') if isinstance(ref, dict) else None
            if not uuid:
                continue
            fiche = manquantes.setdefault((change.ecole_id, str(uuid)), {
                'prefixes': defaultdict(int), 'eleves': {}, 'changements': defaultdict(int)})
            fiche['changements'][change.model_label] += 1
            if change.model_label == 'eleves.Eleve':
                prefixe = prefixe_matricule((change.payload or {}).get('matricule', ''))
                if prefixe:
                    fiche['prefixes'][prefixe] += 1
                fiche['eleves'][change.object_uuid] = (
                    f"{(change.payload or {}).get('matricule', '?')} "
                    f"{(change.payload or {}).get('prenom', '')} {(change.payload or {}).get('nom', '')}".strip())

        if options['uuid']:
            self._lier_a_la_main(options, ecoles, manquantes)
            return

        if not manquantes:
            self.stdout.write(self.style.SUCCESS("Aucun refus « Relation introuvable pour classe » : rien à relier."))
            return

        self.stdout.write(f"{len(manquantes)} classe(s) inconnue(s) du serveur :\n")
        a_lier = []
        for (ecole_id, uuid), fiche in sorted(manquantes.items(), key=lambda x: (x[0][0], -sum(x[1]['changements'].values()))):
            ecole = ecoles.get(ecole_id, str(ecole_id))
            self.stdout.write(self.style.WARNING(f"■ {ecole} — classe {uuid}"))
            deja = objet_par_uuid(Classe, uuid)
            if deja is not None:
                self.stdout.write(f"  déjà reliée à « {deja.nom} » ({deja.annee_scolaire}) : relancez sync_rejouer_serveur.\n")
                continue
            self.stdout.write("  élèves concernés : " + " ; ".join(sorted(fiche['eleves'].values())) if fiche['eleves'] else "  aucun élève direct")
            self.stdout.write("  bloqué : " + " ; ".join(f"{n} × {k}" for k, n in fiche['changements'].items()))
            candidats = self._candidats(ecole_id, fiche['prefixes'])
            if len(candidats) == 1:
                classe = candidats[0]
                self.stdout.write(self.style.SUCCESS(
                    f"  -> classe en ligne trouvée : « {classe.nom} » ({classe.annee_scolaire}, id {classe.pk}, code {classe.code_matricule})\n"))
                a_lier.append((ecole_id, uuid, classe))
            else:
                self.stdout.write(self.style.ERROR(
                    "  -> " + ("aucune classe en ligne ne correspond" if not candidats else "plusieurs classes possibles")
                    + f" (préfixes de matricule : {', '.join(fiche['prefixes']) or 'inconnus'})"))
                autres = Classe.objects.filter(ecole_id=ecole_id).order_by('nom', '-annee_scolaire')
                for c in (candidats or list(autres[:25])):
                    self.stdout.write(f"     id {c.pk} : {c.nom} ({c.annee_scolaire}) code={c.code_matricule or '-'}")
                self.stdout.write(f"     pour décider : --uuid {uuid} --classe <id> --confirmer\n")

        if not a_lier:
            return
        if options['confirmer']:
            with transaction.atomic():
                for _, uuid, classe in a_lier:
                    retenir_alias('eleves.Classe', uuid, classe)
            self.stdout.write(self.style.SUCCESS(
                f"{len(a_lier)} classe(s) reliée(s). Lancez maintenant : manage.py sync_rejouer_serveur --confirmer"))
        else:
            self.stdout.write(self.style.WARNING(
                f"SIMULATION : {len(a_lier)} liaison(s) seraient créées. Relancez avec --confirmer."))

    @staticmethod
    def _candidats(ecole_id, prefixes):
        trouvees = {}
        for prefixe in prefixes:
            for classe in Classe.objects.filter(ecole_id=ecole_id, code_matricule__iexact=prefixe):
                trouvees[classe.pk] = classe
        return sorted(trouvees.values(), key=lambda c: c.pk)

    def _lier_a_la_main(self, options, ecoles, manquantes):
        uuid = options['uuid'].strip().lower()
        classe = Classe.objects.filter(pk=options['classe']).first()
        if classe is None:
            raise CommandError(f"Classe en ligne introuvable : {options['classe']}.")
        ecoles_concernees = {e for e, u in manquantes if u.lower() == uuid}
        if not ecoles_concernees:
            raise CommandError("Aucun changement refusé ne désigne cette classe : vérifiez le sync_uuid.")
        if classe.ecole_id not in ecoles_concernees:
            raise CommandError("Cette classe en ligne n'appartient pas à l'école du poste concerné : liaison refusée.")
        if objet_par_uuid(Classe, uuid) is not None:
            raise CommandError("Ce sync_uuid désigne déjà une classe en ligne : rien à relier.")
        if options['confirmer']:
            retenir_alias('eleves.Classe', uuid, classe)
            self.stdout.write(self.style.SUCCESS(
                f"Liaison créée : {uuid} -> « {classe.nom} » ({classe.annee_scolaire}). Lancez : sync_rejouer_serveur --confirmer"))
        else:
            self.stdout.write(self.style.WARNING(
                f"SIMULATION : {uuid} serait relié à « {classe.nom} » ({classe.annee_scolaire}). Relancez avec --confirmer."))
