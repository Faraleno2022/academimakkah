"""Ordre de diffusion des changements du serveur vers les postes.

Un poste demande « les changements après le n° X » puis retient le plus grand
numéro reçu. Ce numéro ne peut pas être l'id : sur MySQL, l'id est réservé à
l'insertion, mais la ligne n'est visible qu'à la validation de la
transaction. Le lot d'un poste revenu en ligne (une longue transaction)
devenait donc visible après des saisies d'id supérieur déjà livrées, et les
autres postes, déjà positionnés au-delà, ne le recevaient jamais.

Ici le numéro est attribué après coup, uniquement aux lignes déjà validées,
sous le verrou de la ligne unique de ``SyncSequence``. Chaque attribution est
validée d'un bloc et dans l'ordre : à tout instant, les numéros visibles
forment un préfixe sans trou, et une ligne validée en retard reçoit un numéro
plus grand que tout ce qui a déjà été livré.
"""
from django.db import transaction
from django.db.models import Max

from .models import SyncChange, SyncSequence


TAILLE_LOT = 2000


def _numeroter(changements, compteur):
    """Numérote dans l'ordre des id ; le numéro reste >= id.

    Garder ``sequence >= id`` prolonge sans trou l'historique numéroté par
    son id (migration 0003) : un curseur de poste exprimé en id reste valide.
    """
    valeur = compteur.valeur
    for change in changements:
        valeur = max(valeur + 1, change.id)
        change.sequence = valeur
    SyncChange.objects.bulk_update(changements, ['sequence'], batch_size=500)
    compteur.valeur = valeur
    compteur.save(update_fields=['valeur'])


def _compteur_verrouille():
    compteur = SyncSequence.objects.select_for_update().filter(pk=1).first()
    if compteur is None:
        maximum = SyncChange.objects.aggregate(maximum=Max('sequence'))['maximum'] or 0
        SyncSequence.objects.get_or_create(pk=1, defaults={'valeur': maximum})
        compteur = SyncSequence.objects.select_for_update().get(pk=1)
    return compteur


def attribuer_sequences(max_lots=None):
    """Numérote les changements validés qui n'ont pas encore de numéro.

    À appeler avant de lire les changements à diffuser, hors de toute
    transaction englobante : la lecture qui suit doit voir l'état validé.
    Retourne le nombre de changements numérotés.
    """
    total = 0
    lots = 0
    while (max_lots is None or lots < max_lots) and SyncChange.objects.filter(sequence__isnull=True).exists():
        with transaction.atomic():
            compteur = _compteur_verrouille()
            # Lue après le verrou : aucune autre attribution n'est en cours.
            changements = list(
                SyncChange.objects.filter(sequence__isnull=True)
                .only('id').order_by('id')[:TAILLE_LOT]
            )
            if not changements:
                break
            _numeroter(changements, compteur)
        total += len(changements)
        lots += 1
    return total


def reemettre(changements_qs):
    """Redonne un numéro neuf (en fin de file) aux changements indiqués.

    Les postes positionnés au-delà de leur ancien numéro les reçoivent de
    nouveau ; ceux qui les avaient déjà appliqués les ignorent (le client
    écarte un id de changement serveur déjà appliqué). Retourne le nombre de
    changements réémis.
    """
    total = 0
    dernier_id = 0
    while True:
        with transaction.atomic():
            compteur = _compteur_verrouille()
            changements = list(
                changements_qs.filter(id__gt=dernier_id).only('id').order_by('id')[:TAILLE_LOT]
            )
            if not changements:
                break
            _numeroter(changements, compteur)
        dernier_id = changements[-1].id
        total += len(changements)
    return total
