"""Synchronisation automatique en tache de fond (poste offline <-> serveur).

Meme principe que le worker de sauvegarde (ecole_moderne/sauvegarde.py) :
trois tâches démon, démarrées une fois au lancement de l'application.

Vitesse :
- Push declenche immediatement des qu'un changement local est enregistre
  (evenement, pas d'attente de l'intervalle) grace a `notifier_changement()`,
  appele par les signaux post_save/pre_delete de `synchronisation.signals`.
- Envoi et réception utilisent deux tâches indépendantes.
- Les fichiers utilisent une troisième tâche et une file durable séparée.
- Pull effectue a intervalle court (configurable, quelques secondes) car le
  serveur ne peut pas pousser vers le poste (pas de canal push serveur->client).
- Transport mutualise (connexions persistantes + gzip) via `synchronisation.client`.
"""
import json
import logging
import os
import threading
import time

from django.conf import settings
from django.db import close_old_connections

logger = logging.getLogger(__name__)
LOTS_PAR_DIRECTION = 1


INTERVALLE_DEFAUT = 2  # secondes entre deux réceptions sans nouvelle donnée
INTERVALLE_MINIMUM = 1
DELAI_DEMARRAGE_DEFAUT = 15

_verrou = threading.Lock()
_worker_demarre = False
_reveil = threading.Event()
_reveil_reception = threading.Event()
_reveil_medias = threading.Event()


def notifier_medias():
    _reveil_medias.set()


def notifier_changement():
    """Reveille immediatement le worker (appele par les signaux de sync)."""
    _reveil.set()


def _chemin_config(base_dir):
    return os.path.join(base_dir, 'sync_config.json')


def charger_config(base_dir):
    """Configuration effective : variables d'environnement (.env) completees
    par sync_config.json a cote de l'executable, si present."""
    config = {
        'server_url': (getattr(settings, 'MYSCHOOL_SYNC_SERVER_URL', '') or '').rstrip('/'),
        'device_id': getattr(settings, 'MYSCHOOL_SYNC_DEVICE_ID', '') or '',
        'token': getattr(settings, 'MYSCHOOL_SYNC_TOKEN', '') or '',
        'ecole_id': getattr(settings, 'MYSCHOOL_SYNC_ECOLE_ID', '') or '',
        'interval': INTERVALLE_DEFAUT,
    }

    chemin = _chemin_config(base_dir)
    if os.path.exists(chemin):
        try:
            with open(chemin, 'r', encoding='utf-8') as fichier:
                donnees = json.load(fichier)
        except Exception:
            donnees = {}
        if isinstance(donnees, dict):
            config['server_url'] = (donnees.get('MYSCHOOL_SYNC_SERVER_URL') or config['server_url']).rstrip('/')
            config['device_id'] = donnees.get('MYSCHOOL_SYNC_DEVICE_ID') or config['device_id']
            config['token'] = donnees.get('MYSCHOOL_SYNC_TOKEN') or config['token']
            config['ecole_id'] = donnees.get('MYSCHOOL_SYNC_ECOLE_ID') or config['ecole_id']
            try:
                config['interval'] = max(INTERVALLE_MINIMUM, int(donnees.get('MYSCHOOL_SYNC_INTERVAL') or INTERVALLE_DEFAUT))
            except (TypeError, ValueError):
                pass

    return config


def _pret(config):
    return bool(config['server_url'] and config['device_id'] and config['token'] and config['ecole_id'])


def _cycle(base_dir, direction=None):
    """Une direction, ou les deux en mode manuel ; retourne les compteurs."""
    from eleves.models import Ecole
    from .client import pull_changes, push_pending, rattraper_modeles_ajoutes, reprogrammer_refus_temporaires

    config = charger_config(base_dir)
    if not _pret(config):
        return config, 0, 0

    # Un poste est toujours dedie a une seule ecole : son id local (attribue
    # a l'amorçage) ne correspond pas forcement a config['ecole_id'] (celui
    # du serveur). On prend l'ecole unique deja presente, quel que soit son id.
    ecole = Ecole.objects.first()
    if direction == 'media':
        from .media_client import sync_media
        sent, received = sync_media(config, ecole) if ecole else (0, 0)
        return config, sent, received
    if not ecole:
        if direction == 'push':
            return config, 0, 0
        # Poste tout juste installe : rien en local encore, y compris
        # l'ecole elle-meme. pull_changes(ecole=None) l'amorce depuis le
        # premier lot recu du serveur avant de traiter le reste. Sans ce
        # cas particulier, le worker ne ferait jamais rien pour un poste
        # neuf (ecole introuvable en boucle, silencieusement).
        # Les modes/types créés par défaut à l'installation cèdent la place
        # à ceux de l'école, reçus du serveur.
        from paiements.referentiels import retirer_referentiels_inutilises
        retirer_referentiels_inutilises()
        nb_recus = pull_changes(
            config['server_url'], config['device_id'], config['token'],
            ecole=None, initial=True, max_cycles=LOTS_PAR_DIRECTION,
        )
        return config, 0, nb_recus

    from .models import SyncCheckpoint
    checkpoint = SyncCheckpoint.objects.filter(device_id=config['device_id']).first()
    if checkpoint is not None and not checkpoint.initial_complete:
        if direction == 'push':
            return config, 0, 0
        nb_recus = pull_changes(config['server_url'], config['device_id'], config['token'], ecole, max_cycles=LOTS_PAR_DIRECTION)
        return config, 0, nb_recus

    nb_pousses = nb_recus = 0
    if direction != 'pull':
        try:
            _emettre_modeles_ajoutes()
        except Exception:
            logger.exception('[Sync] Mise en file du rattrapage impossible ; réception maintenue.')
        try:
            reprogrammer_refus_temporaires(ecole)
        except Exception:
            logger.exception('[Sync] Reprise des refus temporaires impossible ; poursuite du cycle.')
    for sens, fonction in (
        ('poste vers serveur', push_pending),
        ('serveur vers poste', pull_changes),
        ('rattrapage serveur vers poste', rattraper_modeles_ajoutes),
    ):
        if direction == 'push' and fonction is not push_pending:
            continue
        if direction == 'pull' and fonction is push_pending:
            continue
        debut = time.monotonic()
        try:
            nombre = fonction(
                config['server_url'], config['device_id'], config['token'], ecole,
                max_cycles=LOTS_PAR_DIRECTION,
            )
            if fonction is push_pending:
                nb_pousses += nombre
            else:
                nb_recus += nombre
            journaliser = logger.info if nombre else logger.debug
            journaliser('[Sync] %s : %s changement(s), %.2f s', sens, nombre, time.monotonic() - debut)
        except Exception:
            logger.exception('[Sync] Échec %s après %.2f s ; poursuite du cycle', sens, time.monotonic() - debut)
    return config, nb_pousses, nb_recus


_modeles_ajoutes_emis = False
_modeles_ajoutes_progression = {}


def _emettre_modeles_ajoutes():
    """Une fois par lancement : met en file les lignes saisies sur ce poste
    dans des modèles ajoutés au registre après coup (jamais envoyées)."""
    global _modeles_ajoutes_emis
    if _modeles_ajoutes_emis:
        return
    from .engine import emettre_existants
    from .registry import MODELES_PAR_VERSION
    nb = emettre_existants([label for labels in MODELES_PAR_VERSION.values() for label in labels],
                          limite=50, progression=_modeles_ajoutes_progression)
    if nb:
        print(f"[Sync] {nb} ligne(s) existante(s) mise(s) en file pour envoi.")
    _modeles_ajoutes_emis = bool(_modeles_ajoutes_progression.get('complete'))


def _boucle(base_dir, delai_demarrage, direction=None):
    time.sleep(max(0, delai_demarrage))
    reveil = {'pull': _reveil_reception, 'media': _reveil_medias}.get(direction, _reveil)
    while True:
        # Effacer avant le travail : un enregistrement pendant le cycle garde
        # son réveil, y compris s'il arrive juste après la sélection du lot.
        reveil.clear()
        intervalle = INTERVALLE_DEFAUT
        try:
            close_old_connections()
            config, nb_pousses, nb_recus = _cycle(base_dir, direction=direction)
            intervalle = config.get('interval', INTERVALLE_DEFAUT)
            if nb_pousses or nb_recus:
                print(f"[Sync] {nb_pousses} envoye(s), {nb_recus} recu(s).")
                # Vider les lots suivants sans attendre le prochain sondage.
                intervalle = 0.1
        except Exception as exc:
            print(f"[Sync] Cycle ignore ({exc})")
        finally:
            close_old_connections()

        reveil.wait(timeout=intervalle)


def demarrer_worker(base_dir, delai_demarrage=DELAI_DEMARRAGE_DEFAUT):
    """Demarre la synchronisation automatique en tache de fond (idempotent).

    Retourne True si le worker a ete demarre, False s'il tournait deja.
    """
    global _worker_demarre
    with _verrou:
        if _worker_demarre:
            return False
        _worker_demarre = True
    for direction in ('push', 'pull', 'media'):
        threading.Thread(
            target=_boucle, args=(base_dir, delai_demarrage, direction),
            name=f'sync-auto-{direction}', daemon=True,
        ).start()
    return True
