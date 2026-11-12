"""
Serveur : rejoue les changements de postes que le serveur avait REFUSÉS, à partir de ce
qu'il a gardé (leur contenu complet), sans attendre que le poste les renvoie.

Utile après la correction des identifiants répétés d'une école à l'autre (matricule,
numéro de reçu) ou de l'auteur de la paie : les élèves, paiements et salaires refusés
depuis septembre sont enregistrés en une fois.

- Par défaut : SIMULATION. Tout est appliqué puis annulé : on voit combien passeraient et
  pourquoi les autres échouent encore, sans rien écrire.
- ``--confirmer`` : enregistre réellement, change par change.
- Seul le DERNIER changement de chaque fiche est rejoué (il remplace les précédents), et
  seulement si aucun changement plus récent de cette fiche n'a déjà été appliqué.
- Les fiches sont rejouées dans l'ordre des dépendances (école, classe, élève, paiement…),
  en plusieurs passes pour les cas croisés.
- ``--purger-anciens`` (avec ``--confirmer``) supprime ensuite les anciennes lignes de refus
  devenues inutiles (journal de synchronisation uniquement, jamais une donnée métier).
"""

import re
from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db import transaction

from eleves.models import Ecole
from synchronisation.engine import apply_sync_change, get_model, objet_par_uuid
from synchronisation.models import SyncChange
from synchronisation.registry import SYNC_MODEL_LABELS
from synchronisation.sequence import reemettre


class Command(BaseCommand):
    help = "Rejoue sur le serveur les changements de postes refusés (simulation par défaut)."

    def add_arguments(self, parser):
        parser.add_argument('--ecole', type=int, help="Limiter à une école (id).")
        parser.add_argument('--confirmer', action='store_true', help="Enregistre réellement (sinon simulation).")
        parser.add_argument('--passes', type=int, default=4, help="Nombre maximal de passes (défaut 4).")
        parser.add_argument('--purger-anciens', action='store_true',
                            help="Avec --confirmer : supprime les anciennes lignes de refus des fiches rejouées.")

    def handle(self, *args, **options):
        reel = options['confirmer']
        ecoles = {e.pk: e.nom for e in Ecole.objects.all()}
        echecs = SyncChange.objects.filter(statut=SyncChange.STATUT_FAILED, device__isnull=False, object_uuid__isnull=False)
        if options['ecole']:
            echecs = echecs.filter(ecole_id=options['ecole'])

        # Dernier changement refusé de chaque fiche (id seulement : le contenu est chargé au rejeu)
        derniers = {}
        for pk, ecole_id, label, uuid in echecs.order_by('id').values_list(
                'id', 'ecole_id', 'model_label', 'object_uuid').iterator(chunk_size=2000):
            derniers[(ecole_id, label, uuid)] = pk
        total_refus = echecs.count()

        a_rejouer, deja_remplaces = [], 0
        for (ecole_id, label, uuid), pk in derniers.items():
            if SyncChange.objects.filter(
                    ecole_id=ecole_id, model_label=label, object_uuid=uuid, id__gt=pk,
                    statut=SyncChange.STATUT_APPLIED).exists():
                deja_remplaces += 1
                continue
            a_rejouer.append((ecole_id, label, uuid, pk))

        ordre = {label: i for i, label in enumerate(SYNC_MODEL_LABELS)}
        a_rejouer.sort(key=lambda x: (ordre.get(x[1], 9999), x[3]))
        self.stdout.write(
            f"{total_refus} refus enregistrés, {len(derniers)} fiche(s) concernée(s) : "
            f"{deja_remplaces} déjà remplacée(s) par un changement plus récent, {len(a_rejouer)} à rejouer."
            + ("" if reel else "  [SIMULATION : rien ne sera enregistré]") + "\n")

        reussis, restants = [], list(a_rejouer)
        erreurs = defaultdict(lambda: [0, set()])
        raisons = {}  # id du changement -> dernière erreur
        ctx = transaction.atomic() if not reel else _SansTransaction()
        with ctx:
            for numero in range(1, max(1, options['passes']) + 1):
                encore = []
                erreurs.clear()  # seules comptent les erreurs de la dernière passe
                for ecole_id, label, uuid, pk in restants:
                    change = SyncChange.objects.filter(pk=pk).first()
                    if change is None:
                        continue
                    try:
                        with transaction.atomic():
                            apply_sync_change(change)
                        reussis.append((ecole_id, label, uuid, pk))
                    except Exception as exc:  # noqa: BLE001 — chaque fiche est indépendante
                        encore.append((ecole_id, label, uuid, pk))
                        raisons[pk] = ' '.join(str(exc).split())
                        erreurs[(label, ' '.join(str(exc).split())[:150])][0] += 1
                        erreurs[(label, ' '.join(str(exc).split())[:150])][1].add(ecoles.get(ecole_id, str(ecole_id)))
                self.stdout.write(f"passe {numero} : {len(restants) - len(encore)} réussie(s), {len(encore)} en échec")
                if len(encore) == len(restants):
                    restants = encore
                    break
                restants = encore
                if not restants:
                    break
            # Identification des fiches restantes : à faire AVANT l'annulation de la simulation
            restantes_details = [
                (ecoles.get(ecole_id, str(ecole_id)), label, self._indice(pk, raisons.get(pk, "")))
                for ecole_id, label, _, pk in restants[:60]
            ]
            if not reel:
                transaction.set_rollback(True)

        # Bilan
        self.stdout.write(self.style.MIGRATE_HEADING("\nRésultat par école"))
        par_ecole = defaultdict(lambda: defaultdict(lambda: [0, 0]))
        for ecole_id, label, _, _ in reussis:
            par_ecole[ecole_id][label][0] += 1
        for ecole_id, label, _, _ in restants:
            par_ecole[ecole_id][label][1] += 1
        for ecole_id, modeles in sorted(par_ecole.items(), key=lambda x: -sum(v[0] + v[1] for v in x[1].values())):
            ok = sum(v[0] for v in modeles.values())
            ko = sum(v[1] for v in modeles.values())
            self.stdout.write(self.style.WARNING(f"■ {ecoles.get(ecole_id, ecole_id)} : {ok} {'enregistrée(s)' if reel else 'passeraient'}, {ko} encore en échec"))
            for label, (a, b) in sorted(modeles.items(), key=lambda x: -(x[1][0] + x[1][1])):
                self.stdout.write(f"    {label} : {a} ok, {b} échec")

        if erreurs:
            self.stdout.write(self.style.MIGRATE_HEADING("\nÉchecs restants, par erreur"))
            for (label, erreur), (nombre, noms) in sorted(erreurs.items(), key=lambda x: -x[1][0])[:15]:
                self.stdout.write(f"  {nombre:>5} × {label} — {erreur}  ({', '.join(sorted(noms))})")

        if restantes_details:
            self.stdout.write(self.style.MIGRATE_HEADING("\nFiches qui restent en échec (décision humaine)"))
            for ecole, label, indice in restantes_details:
                self.stdout.write(f"  - {ecole} | {label} | {indice}")
            if len(restants) > len(restantes_details):
                self.stdout.write(f"  … {len(restants) - len(restantes_details)} autre(s)")

        if reel:
            ids = [pk for _, _, _, pk in reussis]
            if ids:
                reemettre(SyncChange.objects.filter(pk__in=ids))  # les autres postes les reçoivent
            if options['purger_anciens']:
                purges = 0
                for ecole_id, label, uuid, pk in reussis:
                    purges += SyncChange.objects.filter(
                        ecole_id=ecole_id, model_label=label, object_uuid=uuid,
                        statut=SyncChange.STATUT_FAILED, id__lt=pk,
                    ).delete()[0]
                self.stdout.write(f"\n{purges} ancienne(s) ligne(s) de refus supprimée(s) du journal.")
            self.stdout.write(self.style.SUCCESS(f"\n{len(reussis)} fiche(s) enregistrée(s), rediffusée(s) aux autres postes."))
        else:
            self.stdout.write(self.style.WARNING(
                "\nSIMULATION terminée : rien n'a été enregistré. Relancez avec --confirmer pour appliquer."))


    @staticmethod
    def _indice(pk, raison):
        """Ce qui permet de reconnaître la fiche, et pourquoi son parent manque s'il manque."""
        change = SyncChange.objects.filter(pk=pk).first()
        if change is None:
            return raison
        payload = change.payload or {}
        morceaux = []
        for cle in ('matricule', 'prenom', 'nom', 'numero_recu', 'montant', 'date_avance',
                    'date_paiement', 'description', 'motif', 'statut'):
            valeur = payload.get(cle)
            if valeur not in ('', None):
                morceaux.append(f"{cle}={str(valeur)[:60]}")
        for champ, label_parent in (('enseignant', 'salaires.Enseignant'), ('eleve', 'eleves.Eleve')):
            ref = payload.get(champ)
            modele = get_model(label_parent)
            if isinstance(ref, dict) and ref.get('sync_uuid') and modele is not None:
                parent = objet_par_uuid(modele, ref['sync_uuid'])
                if parent is not None:
                    morceaux.append(f"{champ}={getattr(parent, 'nom_complet', parent)}")
        texte = ", ".join(morceaux) or change.model_label
        texte += f" | {raison[:110]}"
        correspondance = re.match(r"Relation introuvable pour (\w+)", raison)
        modele = get_model(change.model_label)
        if correspondance and modele is not None:
            champ = correspondance.group(1)
            ref = payload.get(champ)
            try:
                parent_label = modele._meta.get_field(champ).remote_field.model._meta.label
            except Exception:  # noqa: BLE001
                parent_label = None
            if parent_label and isinstance(ref, dict) and ref.get('sync_uuid'):
                histoire = SyncChange.objects.filter(
                    model_label=parent_label, object_uuid=ref['sync_uuid']).order_by('-id').first()
                if histoire is None:
                    texte += f" | {champ} : le serveur n'a JAMAIS reçu cette fiche ({parent_label})"
                else:
                    texte += f" | {champ} : dernier changement {histoire.statut} ({(histoire.erreur or 'sans erreur')[:90]})"
        return texte


class _SansTransaction:
    """Contexte neutre : en mode réel, chaque changement est validé séparément."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False
