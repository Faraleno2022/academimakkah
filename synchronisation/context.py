import threading
from contextlib import contextmanager


_state = threading.local()


def sync_is_muted():
    return bool(getattr(_state, 'muted', False))


@contextmanager
def mute_sync():
    previous = sync_is_muted()
    _state.muted = True
    try:
        yield
    finally:
        _state.muted = previous


def est_recu_du_serveur():
    """Vrai pendant l'application sur un poste d'une ligne venue du serveur."""
    return bool(getattr(_state, 'recu_du_serveur', False))


@contextmanager
def recu_du_serveur(actif=True):
    """Le serveur arbitre : ce qu'il diffuse a déjà passé ses règles métier.

    Les rejouer sur le poste refuserait des lignes valides, car l'ordre n'est pas
    celui de la saisie : l'instantané livre l'état validé avant ses avances.
    """
    previous = est_recu_du_serveur()
    _state.recu_du_serveur = actif
    try:
        yield
    finally:
        _state.recu_du_serveur = previous
