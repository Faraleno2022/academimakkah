"""Transport HTTP rapide pour la synchronisation offline <-> serveur.

Reutilise une session `requests` (connexions persistantes, keep-alive) au lieu
d'ouvrir une connexion TCP/TLS par appel, compresse les gros payloads en gzip,
et boucle jusqu'a vidage complet de la file (au lieu de s'arreter a une seule
page de 200 changements).
"""
import gzip
import json
import logging
import threading
from datetime import timedelta
from uuid import NAMESPACE_URL, uuid5

import requests
from requests.adapters import HTTPAdapter

from .models import SyncChange
from .registry import REGISTRE_VERSION, modeles_inconnus


# Lots courts : un lot doit tenir largement dans PUSH_TIMEOUT, sinon le poste
# croit a un echec et renvoie le meme lot a chaque cycle.
PUSH_BATCH_SIZE = 100
PUSH_TIMEOUT = 120
PULL_PAGE_SIZE = 500
MAX_CYCLES_PAR_APPEL = 25  # garde-fou anti-boucle-infinie
GZIP_SEUIL_OCTETS = 512
CONNECT_TIMEOUT = 5

logger = logging.getLogger(__name__)

_sessions = threading.local()


class SyncTransportError(Exception):
    pass


class SnapshotRestartRequired(SyncTransportError):
    pass


def _get_session():
    # requests.Session porte des cookies et un état mutable : chaque tâche
    # garde sa connexion persistante sans partager cet état avec l'autre.
    session = getattr(_sessions, 'http', None)
    if session is None:
        session = requests.Session()
        adapter = HTTPAdapter(pool_connections=4, pool_maxsize=8, max_retries=0)
        session.mount('http://', adapter)
        session.mount('https://', adapter)
        _sessions.http = session
    return session


def _headers(device_id, token, gzip_body=False):
    headers = {
        'X-Sync-Device': device_id,
        'X-Sync-Token': token,
        'Accept-Encoding': 'gzip',
    }
    if gzip_body:
        headers['Content-Encoding'] = 'gzip'
        headers['Content-Type'] = 'application/json'
    elif gzip_body is not None:
        headers['Content-Type'] = 'application/json'
    return headers


def _post_json(url, device_id, token, payload, timeout=25):
    session = _get_session()
    body = json.dumps(payload).encode('utf-8')
    compress = len(body) > GZIP_SEUIL_OCTETS
    if compress:
        body = gzip.compress(body, compresslevel=6)
    try:
        response = session.post(url, data=body, headers=_headers(device_id, token, compress), timeout=(CONNECT_TIMEOUT, timeout))
    except requests.RequestException as exc:
        raise SyncTransportError(f"Serveur de synchronisation inaccessible: {exc}") from exc
    return _parse_response(response)


def _get_json(url, device_id, token, params=None, timeout=25):
    session = _get_session()
    try:
        response = session.get(url, params=params, headers=_headers(device_id, token, gzip_body=None), timeout=(CONNECT_TIMEOUT, timeout))
    except requests.RequestException as exc:
        raise SyncTransportError(f"Serveur de synchronisation inaccessible: {exc}") from exc
    return _parse_response(response)


def _parse_response(response):
    try:
        data = response.json()
    except ValueError as exc:
        raise SyncTransportError(f"Reponse serveur invalide (HTTP {response.status_code}).") from exc
    if not isinstance(data, dict):
        raise SyncTransportError(f"Reponse serveur invalide (HTTP {response.status_code}).")
    if response.status_code >= 400:
        if response.status_code == 409 and data.get('snapshot_restart_required') is True:
            raise SnapshotRestartRequired(data.get('error') or 'Instantané expiré.')
        raise SyncTransportError(data.get('error') or f"Erreur serveur {response.status_code}")
    return data


def push_pending(server_url, device_id, token, ecole, batch_size=PUSH_BATCH_SIZE, max_cycles=None):
    """Envoie tous les changements PENDING de l'ecole, par lots, jusqu'a vidage."""
    total = 0
    dernier_id = 0
    for _ in range(MAX_CYCLES_PAR_APPEL if max_cycles is None else max_cycles):
        # `id__gt` fait avancer la fenetre meme si le serveur n'accepte rien :
        # sans lui, un lot entierement refuse est reselectionne a l'identique
        # au cycle suivant et les changements qui le suivent ne partent jamais.
        pending = list(
            SyncChange.objects
            .filter(ecole=ecole, statut=SyncChange.STATUT_PENDING, id__gt=dernier_id)
            .order_by('id')[:batch_size]
        )
        if not pending:
            break
        dernier_id = pending[-1].id

        response = _post_json(
            f'{server_url}/api/v1/sync/push/',
            device_id, token,
            {
                'changes': [
                    {
                        # Stable même après un redémarrage ou une réponse perdue.
                        'event_id': str(uuid5(NAMESPACE_URL,
                            f'myschool-sync:{str(device_id).lower()}:{change.pk}:{change.date_creation.isoformat()}')),
                        'model': change.model_label,
                        'object_uuid': str(change.object_uuid) if change.object_uuid else None,
                        'operation': change.operation,
                        'payload': change.payload,
                    }
                    for change in pending
                ]
            },
            timeout=PUSH_TIMEOUT,
        )
        if not response.get('ok'):
            raise SyncTransportError(response.get('error') or 'Push refuse.')

        # Une réponse partielle ne prouve pas que le serveur a traité le lot.
        # Ne marquer aucune ligne tant que chaque index n'est pas acquitté une fois.
        accepted = response.get('accepted', [])
        rejected = response.get('rejected', [])
        if not isinstance(accepted, list) or not isinstance(rejected, list):
            raise SyncTransportError('Accusé de réception du lot invalide.')
        indexes = []
        for item in accepted + rejected:
            index = item.get('index') if isinstance(item, dict) else None
            if type(index) is not int or not 0 <= index < len(pending):
                raise SyncTransportError('Index invalide dans l’accusé de réception.')
            indexes.append(index)
        if len(indexes) != len(pending) or len(set(indexes)) != len(pending):
            raise SyncTransportError('Accusé de réception incomplet ou dupliqué ; lot conservé.')
        accepted_indexes = {item['index'] for item in accepted}
        motifs = {
            item['index']: (('[TEMPORAIRE] ' if item.get('retryable') is True else '')
                            + str(item.get('error') or 'Refus sans motif.'))
            for item in rejected
        }
        from django.utils import timezone
        now = timezone.now()
        applied_ids = [c.id for i, c in enumerate(pending) if i in accepted_indexes]
        if applied_ids:
            from django.db import transaction
            from .media import queue_sent
            # La file binaire est durable avant l'acquittement des données.
            with transaction.atomic():
                queue_sent(ecole, [c for i, c in enumerate(pending) if i in accepted_indexes])
                SyncChange.objects.filter(id__in=applied_ids).update(statut=SyncChange.STATUT_APPLIED, date_application=now, erreur='')
            total += len(applied_ids)

        # Un changement refuse sort de la file d'attente avec son motif : laisse
        # en PENDING il serait renvoye a chaque cycle, indefiniment et en
        # silence. Il reste consultable et rejouable (`sync_offline
        # --rejouer-refuses`) une fois la cause corrigee.
        for index, motif in motifs.items():
            if index >= len(pending):
                continue
            change = pending[index]
            SyncChange.objects.filter(id=change.id).update(
                statut=SyncChange.STATUT_FAILED, erreur=motif[:2000], date_application=now,
            )
            logger.warning(
                '[Sync] Changement refuse par le serveur : %s %s (%s) -> %s',
                change.operation, change.model_label, change.object_uuid, motif,
            )
        if motifs:
            print(f'[Sync] {len(motifs)} changement(s) refuse(s) par le serveur, voir myschool.log.')

        if len(pending) < batch_size:
            break
    return total


# Marque, dans le payload, un changement recu du serveur qui n'a pas pu etre
# applique : il est reessaye a chaque cycle et ne doit jamais etre renvoye.
MARQUEUR_RECU = '_recu_du_serveur'
REESSAIS_PAR_CYCLE = 200


def _refus_de_paie_corriges():
    """Paie refusée à tort par un serveur plus ancien : auteur absent ou doublon.

    Ces lignes n'existent que sur leur poste et rien d'autre ne les renverrait.
    """
    from django.db.models import Q
    motifs = Q()
    for champ in ('cree_par', 'calcule_par', 'pointe_par', '__all__'):
        motifs |= Q(erreur__contains=f"'{champ}'")
    return Q(model_label__startswith='salaires.') & motifs


def reprogrammer_refus_temporaires(ecole, limite=100):
    """Reprend les dépendances absentes et verrous après au moins une minute.

    Les refus de validation restent visibles. Un ancien changement ne doit
    jamais être rejoué si une version plus récente de cet objet existe.
    """
    from django.db.models import Exists, OuterRef, Q
    from django.utils import timezone
    plus_recents = SyncChange.objects.filter(
        ecole=ecole, model_label=OuterRef('model_label'),
        object_uuid=OuterRef('object_uuid'), id__gt=OuterRef('id'),
    )
    candidats = SyncChange.objects.filter(
        ecole=ecole, statut=SyncChange.STATUT_FAILED,
    ).exclude(payload__has_key=MARQUEUR_RECU).filter(
        Q(date_application__lte=timezone.now() - timedelta(minutes=1))
        | Q(date_application__isnull=True),
    ).filter(
        Q(erreur__startswith='Relation introuvable pour ')
        | Q(erreur__startswith='[TEMPORAIRE] ')
        | Q(erreur__icontains='database is locked')
        | Q(erreur__icontains='database table is locked')
        | _refus_de_paie_corriges(),
    ).filter(object_uuid__isnull=False).annotate(
        remplace=Exists(plus_recents),
    ).filter(remplace=False).order_by('date_application', 'id')
    ids = list(candidats.values_list('pk', flat=True)[:limite])
    return SyncChange.objects.filter(pk__in=ids, statut=SyncChange.STATUT_FAILED).update(
        statut=SyncChange.STATUT_PENDING,
    )


def rejouer_refuses(ecole):
    """Remet en file les changements refuses, une fois leur cause corrigee."""
    return SyncChange.objects.filter(ecole=ecole, statut=SyncChange.STATUT_FAILED).exclude(
        payload__has_key=MARQUEUR_RECU,
    ).update(statut=SyncChange.STATUT_PENDING, erreur='')


def _garder_pour_nouvel_essai(ecole, item, payload, exc):
    from django.utils import timezone
    valeurs = dict(
        model_label=item['model_label'],
        object_uuid=item.get('object_uuid') or None,
        operation=item['operation'], payload={**payload, MARQUEUR_RECU: True},
        statut=SyncChange.STATUT_FAILED, erreur=str(exc)[:2000],
        date_application=timezone.now(),
    )
    server_id = item.get('id')
    precedent = (SyncChange.objects.filter(ecole=ecole, server_change_id=server_id,
        statut=SyncChange.STATUT_FAILED).order_by('-pk').first() if server_id else None)
    if precedent is None:
        SyncChange.objects.create(ecole=ecole, server_change_id=server_id, **valeurs)
    else:
        SyncChange.objects.filter(pk=precedent.pk).update(**valeurs)
    logger.warning(
        '[Sync] Changement recu non applique, nouvel essai au prochain cycle : %s %s (%s) -> %s',
        item.get('operation'), item.get('model_label'), item.get('object_uuid'), exc,
    )


def reessayer_recus_en_echec(ecole, apply_change=None, limite=REESSAIS_PAR_CYCLE):
    """Reapplique les changements recus mis de cote (parent arrive depuis, base
    momentanement verrouillee...). Un changement remplace par un plus recent
    du meme objet est abandonne. Retourne le nombre de changements rattrapes."""
    from django.db import transaction
    from django.utils import timezone
    if apply_change is None:
        from .engine import apply_sync_change
        apply_change = apply_sync_change
    reussis = 0
    en_echec = list(
        SyncChange.objects.filter(
            ecole=ecole, statut=SyncChange.STATUT_FAILED, payload__has_key=MARQUEUR_RECU,
        ).order_by('date_application', 'id')[:limite]
    )
    for change in en_echec:
        if change.object_uuid and SyncChange.objects.filter(
            ecole=ecole, statut__in=[SyncChange.STATUT_APPLIED, SyncChange.STATUT_PENDING], model_label=change.model_label,
            object_uuid=change.object_uuid, id__gt=change.id,
        ).exists():
            change.delete()
            continue
        try:
            with transaction.atomic():
                apply_change(change)
            reussis += 1
        except Exception as exc:
            SyncChange.objects.filter(pk=change.pk).update(
                statut=SyncChange.STATUT_FAILED, erreur=str(exc)[:2000],
                date_application=timezone.now(),
            )
    if reussis:
        print(f'[Sync] {reussis} changement(s) recu(s) rattrape(s).')
    return reussis


def _appliquer_lot(items, ecole, apply_change):
    """Applique une page reçue du serveur ; retourne le nombre de changements appliqués."""
    from django.db import transaction
    # Un changement serveur deja applique (lot recu deux fois, ou reemis par
    # `sync_reemettre`) est ignore : une seule requete pour toute la page.
    ids_serveur = [item.get('id') for item in items if item.get('id')]
    deja_appliques = set(
        SyncChange.objects.filter(
            ecole=ecole, statut=SyncChange.STATUT_APPLIED,
            server_change_id__in=ids_serveur,
        ).values_list('server_change_id', flat=True)
    ) if ids_serveur else set()
    applied = 0
    for item in items:
        server_change_id = item.get('id')
        if server_change_id and server_change_id in deja_appliques:
            continue
        payload = item.get('payload') or {}
        if server_change_id:
            payload = {**payload, 'server_change_id': server_change_id}
        try:
            with transaction.atomic():
                change = SyncChange.objects.create(
                    ecole=ecole, model_label=item['model_label'],
                    server_change_id=server_change_id,
                    object_uuid=item.get('object_uuid') or None,
                    operation=item['operation'], payload=payload,
                )
                apply_change(change)
        except Exception as exc:
            _garder_pour_nouvel_essai(ecole, item, payload, exc)
            continue
        applied += 1
        if server_change_id:
            deja_appliques.add(server_change_id)
    return applied


def rattraper_modeles_ajoutes(server_url, device_id, token, ecole, apply_change=None, max_cycles=None):
    """Poste mis à jour : récupère l'historique des modèles ajoutés au registre.

    Avant la mise à jour, le serveur ne lui envoyait pas ces modèles et son
    curseur est passé au-delà. Un second curseur (``rattrapage_id``) relit
    depuis le début les seuls modèles ajoutés, une fois, puis le poste note la
    version du registre qu'il connaît. Retourne le nombre de changements appliqués.
    """
    from django.db import transaction
    from .models import SyncCheckpoint
    if apply_change is None:
        from .engine import apply_sync_change
        apply_change = apply_sync_change
    checkpoint = SyncCheckpoint.objects.filter(device_id=device_id).first()
    if checkpoint is None or not checkpoint.initial_complete or checkpoint.registre >= REGISTRE_VERSION:
        return 0
    modeles = modeles_inconnus(checkpoint.registre)
    total = 0
    for _ in range(MAX_CYCLES_PAR_APPEL if max_cycles is None else max_cycles):
        response = _get_json(f'{server_url}/api/v1/sync/pull/', device_id, token, params={
            'since_id': checkpoint.rattrapage_id, 'registre': REGISTRE_VERSION,
            'modeles': ','.join(modeles),
        })
        if not response.get('ok'):
            raise SyncTransportError(response.get('error') or 'Pull refusé.')
        items = response.get('changes', [])
        if not isinstance(items, list):
            raise SyncTransportError('Lot de synchronisation invalide.')
        total += _appliquer_lot(items, ecole, apply_change)
        with transaction.atomic():
            checkpoint.rattrapage_id = int(response.get('latest_change_id') or checkpoint.rattrapage_id)
            fini = not response.get('has_more', len(items) >= PULL_PAGE_SIZE)
            if fini:
                checkpoint.registre = REGISTRE_VERSION
            checkpoint.save(update_fields=['rattrapage_id', 'registre'])
        if fini:
            break
    return total


def pull_changes(server_url, device_id, token, ecole, since_id=None, initial=False, apply_change=None, max_cycles=None):
    """Import paginé et reprenable.

    Un changement qui ne s'applique pas est mis de côté pour un nouvel essai au
    lieu de bloquer toute la file : avant, un seul changement en échec (parent
    manquant, conflit) arrêtait définitivement la réception sur ce poste. Seul
    un incident sur le lot entier (transport, base) laisse le curseur en place.
    """
    from django.db import transaction
    from .models import SyncCheckpoint
    from .engine import build_change_instance
    if apply_change is None:
        from .engine import apply_sync_change
        apply_change = apply_sync_change
    checkpoint, _ = SyncCheckpoint.objects.get_or_create(device_id=device_id)
    if initial:
        checkpoint.initial_complete = False
        checkpoint.snapshot_cursor = ''
        checkpoint.save(update_fields=['initial_complete', 'snapshot_cursor'])
    if since_id is not None:
        checkpoint.last_change_id = int(since_id)
    total = 0
    for _ in range(MAX_CYCLES_PAR_APPEL if max_cycles is None else max_cycles):
        bootstrapping = not checkpoint.initial_complete
        params = {'since_id': checkpoint.last_change_id, 'registre': REGISTRE_VERSION}
        if bootstrapping:
            params.update(initial='1', snapshot_version='2')
            if checkpoint.snapshot_cursor:
                params['snapshot_cursor'] = checkpoint.snapshot_cursor
        try:
            response = _get_json(f'{server_url}/api/v1/sync/pull/', device_id, token, params=params)
        except SnapshotRestartRequired:
            if not bootstrapping or not checkpoint.snapshot_cursor:
                raise
            checkpoint.snapshot_cursor = ''
            checkpoint.save(update_fields=['snapshot_cursor'])
            logger.warning('[Sync] Curseur initial expiré : reprise automatique sans effacer les données locales.')
            continue
        if not response.get('ok'):
            raise SyncTransportError(response.get('error') or 'Pull refusé.')
        items = response.get('changes', [])
        if not isinstance(items, list):
            raise SyncTransportError('Lot de synchronisation invalide.')
        applied = 0
        try:
            # Chaque donnée est durable avant la suivante. Une page de 500
            # éléments ne garde plus le verrou d'écriture SQLite pendant tout
            # le rattrapage. Le curseur ne progresse qu'après la page entière.
            if ecole is None:
                premier = items[0] if items else None
                if not premier or premier.get('model_label') != 'eleves.Ecole':
                    raise SyncTransportError("École absente du premier lot reçu.")
                ecole = build_change_instance(
                    'eleves.Ecole', premier.get('object_uuid'), premier.get('payload') or {},
                    trusted=True,
                )
            applied = _appliquer_lot(items, ecole, apply_change)
            with transaction.atomic():
                if bootstrapping:
                    next_cursor = response.get('next_snapshot_cursor') or ''
                    if next_cursor and next_cursor == checkpoint.snapshot_cursor:
                        raise SyncTransportError('Le curseur initial ne progresse pas.')
                    checkpoint.snapshot_cursor = next_cursor
                    checkpoint.initial_complete = not bool(next_cursor)
                    if checkpoint.initial_complete:
                        checkpoint.last_change_id = int(response.get('latest_change_id') or 0)
                        # L'instantané couvrait tous les modèles connus : rien à rattraper.
                        checkpoint.registre = REGISTRE_VERSION
                else:
                    checkpoint.last_change_id = int(response.get('latest_change_id') or checkpoint.last_change_id)
                checkpoint.save()
        except Exception as exc:
            raise SyncTransportError(f"Lot interrompu, données validées conservées et reprise au même curseur : {exc}") from exc
        total += applied
        if bootstrapping:
            # Once the snapshot is complete, read changes made while it was being downloaded.
            continue
        if not response.get('has_more', len(items) >= PULL_PAGE_SIZE):
            break
    if ecole is not None:
        total += reessayer_recus_en_echec(ecole, apply_change)
    return total
