"""
Diagnostic (lecture seule) des changements de synchronisation refusés pour
« Relation introuvable ».

Un poste envoie un changement (paiement, échéancier, historique…) qui désigne un
élève (ou une autre fiche parente) que le serveur ne connaît pas. Le poste le
renvoie à chaque cycle : la commande regroupe ces refus par fiche manquante pour
dire QUI est concerné, depuis quand, combien de fois, et si la fiche existe
maintenant (le refus est alors résolu) ou a été mise à la corbeille.

Ne modifie aucune donnée.
"""

from collections import defaultdict

from django.core.management.base import BaseCommand

from eleves.models import Ecole
from synchronisation.engine import get_model, objet_par_uuid
from synchronisation.models import SyncChange

PREFIXE = 'Relation introuvable pour '
CHAMPS_UTILES = (
    'numero_recu', 'montant', 'date_paiement', 'statut', 'annee_scolaire',
    'description', 'action', 'matricule',
)


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


class Command(BaseCommand):
    help = "Liste, sans rien modifier, les fiches manquantes derrière les refus « Relation introuvable »."

    def add_arguments(self, parser):
        parser.add_argument('--ecole', type=int, help="Limiter à une école (id).")

    def handle(self, *args, **options):
        refus = SyncChange.objects.filter(
            statut=SyncChange.STATUT_FAILED, erreur__startswith=PREFIXE,
        ).select_related('ecole').order_by('id')
        if options['ecole']:
            refus = refus.filter(ecole_id=options['ecole'])

        groupes = defaultdict(list)
        for change in refus.iterator(chunk_size=500):
            champ, parent, uuid = _fiche_manquante(change)
            cle = (change.ecole_id, parent._meta.label if parent else '?', champ, str(uuid or ''))
            groupes[cle].append(change)

        if not groupes:
            self.stdout.write(self.style.SUCCESS("Aucun refus « Relation introuvable » : rien à signaler."))
            return

        total = sum(len(v) for v in groupes.values())
        self.stdout.write(f"{total} refus « Relation introuvable », {len(groupes)} fiche(s) manquante(s) :\n")
        for (ecole_id, label_parent, champ, uuid), changes in sorted(groupes.items(), key=lambda g: -len(g[1])):
            ecole = Ecole.objects.filter(pk=ecole_id).values_list('nom', flat=True).first() or f"école {ecole_id}"
            premier, dernier = changes[0], changes[-1]
            self.stdout.write(self.style.WARNING(f"■ {label_parent} manquant(e) — champ « {champ} » — {ecole}"))
            self.stdout.write(f"  sync_uuid de la fiche : {uuid or 'absent du changement'}")
            self.stdout.write(
                f"  {len(changes)} refus du {premier.date_creation:%d/%m/%Y %H:%M} au {dernier.date_creation:%d/%m/%Y %H:%M}"
            )

            etat = "inconnue du serveur"
            parent = get_model(label_parent) if label_parent != '?' else None
            if parent is not None and uuid:
                trouvee = objet_par_uuid(parent, uuid)
                if trouvee is not None:
                    etat = f"EXISTE maintenant (id {trouvee.pk}) : le prochain essai doit réussir"
            self.stdout.write(f"  état : {etat}")

            par_modele = defaultdict(int)
            for c in changes:
                par_modele[f"{c.operation} {c.model_label}"] += 1
            self.stdout.write("  changements bloqués : " + " ; ".join(f"{n} × {k}" for k, n in par_modele.items()))

            # Indices pour retrouver la personne : champs lisibles des changements bloqués
            vus = set()
            for c in changes:
                indices = {k: v for k, v in (c.payload or {}).items() if k in CHAMPS_UTILES and v not in ('', None)}
                signature = tuple(sorted((k, str(v)) for k, v in indices.items()))
                if indices and signature not in vus and len(vus) < 5:
                    vus.add(signature)
                    self.stdout.write(f"  indice ({c.model_label}) : " + ", ".join(f"{k}={str(v)[:80]}" for k, v in indices.items()))
            self.stdout.write("")

        self.stdout.write(
            "Lecture seule : aucune donnée modifiée. Pour retrouver l'élève, cherchez dans l'application "
            "le reçu ou le montant indiqué ci-dessus."
        )
