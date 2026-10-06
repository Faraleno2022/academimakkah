"""Modes et types de paiement proposés dès l'installation.

L'utilisateur n'a pas à les saisir lui-même : ceux qui manquent sont créés au
lancement du Desktop (et par ``manage.py assurer_referentiels_paiement`` sur
le serveur). Une valeur déjà présente sous une autre écriture (« Especes »,
« Inscription » pour « Frais d'inscription », « Scolarité » pour l'annuel) ou
désactivée par l'école n'est jamais recréée.
"""
import unicodedata
import uuid

from django.db import transaction

from synchronisation.context import mute_sync

from .allocation import payment_type_plan
from .models import ModePaiement, TypePaiement

MODES_PAIEMENT_DEFAUT = (
    ("Espèces", "Paiement en liquide"),
    ("Orange Money", "Paiement mobile Orange Money"),
    ("MTN Mobile Money", "Paiement mobile MTN MoMo"),
    ("Chèque", "Paiement par chèque"),
    ("Virement bancaire", "Transfert bancaire"),
)

TYPES_PAIEMENT_DEFAUT = (
    ("Frais d'inscription", "Frais d'inscription d'un nouvel élève"),
    ("Réinscription", "Frais de réinscription d'un ancien élève"),
    ("Tranche 1", "1ère tranche de scolarité"),
    ("Tranche 2", "2ème tranche de scolarité"),
    ("Tranche 3", "3ème tranche de scolarité"),
    ("Tranche 1 + Tranche 2", "1ère et 2ème tranches de scolarité"),
    ("Tranche 2 + Tranche 3", "2ème et 3ème tranches de scolarité"),
    ("Annuel (Tranche 1 + Tranche 2 + Tranche 3)", "Scolarité de l'année complète"),
    ("Frais d'inscription + Tranche 1", "Inscription et 1ère tranche"),
    ("Frais d'inscription + Annuel", "Inscription et scolarité de l'année complète"),
    ("Réinscription + Tranche 1", "Réinscription et 1ère tranche"),
    ("Réinscription + Annuel", "Réinscription et scolarité de l'année complète"),
)

# Même sync_uuid sur le serveur et sur chaque poste : une valeur par défaut créée
# des deux côtés désigne la même ligne pour la synchronisation.
_ESPACE_UUID = uuid.UUID('6f1c2b0e-5d6a-4c1e-9b7a-3e2f8d4c1a90')


def _normaliser(nom):
    texte = unicodedata.normalize('NFKD', (nom or '').strip().lower())
    return ' '.join(''.join(c for c in texte if not unicodedata.combining(c)).split())


def _cle_type(nom):
    """Deux libellés au même plan (postes couverts) sont un même type."""
    plan = payment_type_plan(nom)
    return (plan['registration_kind'], plan['include_registration'], plan['tranches'], plan['covers_balance'])


def uuid_defaut(model, nom):
    return uuid.uuid5(_ESPACE_UUID, f'{model._meta.label}:{_normaliser(nom)}')


def mode_paiement_par_defaut():
    """Espèces, présélectionné dans les formulaires de paiement."""
    for pk, nom in ModePaiement.objects.filter(actif=True).values_list('pk', 'nom'):
        if _normaliser(nom) == 'especes':
            return pk
    return None


def _creer_manquants(model, valeurs, cle):
    existants = {cle(nom) for nom in model.objects.values_list('nom', flat=True)}
    crees = []
    for nom, description in valeurs:
        if cle(nom) in existants:
            continue
        model.objects.create(
            nom=nom, description=description, actif=True, sync_uuid=uuid_defaut(model, nom),
        )
        existants.add(cle(nom))
        crees.append(nom)
    return crees


@transaction.atomic
def assurer_referentiels_paiement():
    """Crée les modes et types de paiement manquants. Retourne les noms créés.

    Rien n'est mis en file de synchronisation : ce sont des référentiels
    communs, que le poste reçoit du serveur et n'y envoie jamais.
    """
    with mute_sync():
        modes = _creer_manquants(ModePaiement, MODES_PAIEMENT_DEFAUT, _normaliser)
        types = _creer_manquants(TypePaiement, TYPES_PAIEMENT_DEFAUT, _cle_type)
    return modes, types


def retirer_referentiels_inutilises():
    """Avant le premier import d'un poste neuf : retire les valeurs par défaut
    jamais utilisées, pour ne garder que celles de l'école reçues du serveur."""
    retires = 0
    for model, valeurs in ((ModePaiement, MODES_PAIEMENT_DEFAUT), (TypePaiement, TYPES_PAIEMENT_DEFAUT)):
        uuids = [uuid_defaut(model, nom) for nom, _description in valeurs]
        for obj in model.objects.filter(sync_uuid__in=uuids):
            # Paiement est en CASCADE : ne jamais supprimer une valeur utilisée.
            utilise = any(
                relation.related_model._base_manager.filter(**{relation.field.name: obj}).exists()
                for relation in model._meta.related_objects
            )
            if utilise:
                continue
            with mute_sync():
                obj.delete()
            retires += 1
    return retires
