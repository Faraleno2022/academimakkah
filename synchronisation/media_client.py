"""Transport binaire reprenable, exécuté hors des tâches de données."""
from datetime import timedelta
import hashlib
import logging
import re
import tempfile
import threading
import time

import requests
from django.db import transaction
from django.db.models import Case, IntegerField, Value, When
from django.utils import timezone

from .client import CONNECT_TIMEOUT, SyncTransportError, _get_session, _headers, _parse_response
from .media import (CHUNK_SIZE, MAX_FILE_BYTES, atomic_copy, canonical_name, digest_file,
                    enqueue, local_path, media_target, notify_file_ready, pending_name,
                    repair_page, safe_name, validate_file)
from .models import SyncMediaTransfer

logger = logging.getLogger(__name__)
_lock = threading.Lock()
_repair = {}


class ObsoleteMedia(Exception):
    pass


def request_json(config, params, source=None):
    session = _get_session()
    headers = _headers(config['device_id'], config['token'], gzip_body=None)
    url = f"{config['server_url']}/api/v1/sync/media/"
    if source is None:
        response = session.get(url, params=params, headers=headers, timeout=(CONNECT_TIMEOUT, 30), allow_redirects=False)
    else:
        headers['Content-Type'] = 'application/octet-stream'
        response = session.post(url, params=params, data=source, headers=headers,
                                timeout=(CONNECT_TIMEOUT, 60), allow_redirects=False)
    with response:
        if response.status_code == 409:
            raise ObsoleteMedia()
        data = _parse_response(response)
        if not data.get('ok'):
            raise SyncTransportError('Transfert du fichier refusé.')
        return data


def params_for(job):
    return {'model': job.model_label, 'object_uuid': str(job.object_uuid),
            'field': job.field_name, 'name': job.name}


def finish(job):
    # Une nouvelle image peut avoir remplacé la file pendant le transfert.
    SyncMediaTransfer.objects.filter(pk=job.pk, name=job.name, sha256=job.sha256).update(complete=True, error='')


def local_target(job):
    obj, field = media_target(job.ecole, job.model_label, job.object_uuid, job.field_name)
    if obj is None:
        raise ObsoleteMedia()
    return obj, field


def install_server_reference(job, field, data, source=None):
    """Réutilise les octets envoyés au lieu de les retélécharger sur ce poste."""
    name = safe_name(data.get('name'))
    digest = data.get('sha256', '')
    if not re.fullmatch('[a-f0-9]{64}', digest):
        raise SyncTransportError('Empreinte serveur invalide.')
    with transaction.atomic():
        obj, _ = local_target(job)
        current = getattr(obj, field.name).name
        allowed = {job.name, pending_name(job.ecole, obj.sync_uuid, field.name, job.name), name}
        if current not in allowed:
            raise ObsoleteMedia()
        changed = False
        if source is not None:
            target = local_path(field, name)
            if not target.is_file() or digest_file(target) != digest:
                atomic_copy(source, field, name)
                changed = True
            SyncMediaTransfer.objects.filter(ecole=job.ecole, model_label=job.model_label,
                object_uuid=job.object_uuid, field_name=job.field_name, direction='PULL', name=name).update(complete=True, error='')
        else:
            enqueue(job.ecole, job.model_label, job.object_uuid, job.field_name,
                    name, 'PULL', sha256=digest, force=True)
        if current != name:
            type(obj).objects.filter(pk=obj.pk, **{field.name: current}).update(**{field.name: name})
            changed = True
        if changed:
            notify_file_ready(job.ecole, job.model_label, job.object_uuid)


def upload(config, job):
    obj, field = local_target(job)
    path = local_path(field, job.name)
    if not path.is_file():
        raise SyncTransportError('Fichier local absent ; copie conservée en attente.')
    if path.stat().st_size > MAX_FILE_BYTES:
        raise SyncTransportError('Fichier supérieur à la limite de 25 Mo.')
    # L'empreinte porte sur le même descripteur que le corps transmis.
    with path.open('rb') as source:
        digest = hashlib.file_digest(source, 'sha256').hexdigest()
        source.seek(0)
        current = getattr(obj, field.name).name
        if current not in {job.name, pending_name(job.ecole, obj.sync_uuid, field.name, job.name),
                           canonical_name(job.ecole, digest, job.name)}:
            raise ObsoleteMedia()
        params = {**params_for(job), 'sha256': digest}
        data = request_json(config, {**params, 'check': '1'})
        if data.get('present'):
            if data.get('sha256') != digest:
                # Réparation d'un ancien poste : la version existante du serveur prévaut.
                install_server_reference(job, field, data)
                return
        else:
            validate_file(source, field)
            data = request_json(config, params, source)
            if data.get('sha256') != digest:
                raise SyncTransportError('Empreinte de réception incorrecte.')
        install_server_reference(job, field, data, source)


def download(config, job):
    obj, field = local_target(job)
    if getattr(obj, field.name).name != job.name:
        raise ObsoleteMedia()
    path = local_path(field, job.name)
    if job.sha256 and path.is_file() and digest_file(path) == job.sha256:
        return
    response = _get_session().get(f"{config['server_url']}/api/v1/sync/media/",
        params=params_for(job), headers=_headers(config['device_id'], config['token'], gzip_body=None),
        timeout=(CONNECT_TIMEOUT, 30), stream=True, allow_redirects=False)
    with response, tempfile.SpooledTemporaryFile(max_size=1024 * 1024) as source:
        if response.status_code == 409:
            raise ObsoleteMedia()
        if response.status_code != 200:
            raise SyncTransportError(f'Image indisponible (HTTP {response.status_code}) ; nouvel essai prévu.')
        expected = response.headers.get('X-Sync-Media-SHA256', '')
        if not re.fullmatch('[a-f0-9]{64}', expected):
            raise SyncTransportError('Empreinte du téléchargement absente ou invalide.')
        checksum, size = hashlib.sha256(), 0
        for chunk in response.iter_content(CHUNK_SIZE):
            size += len(chunk)
            if size > MAX_FILE_BYTES:
                raise SyncTransportError('Fichier supérieur à la limite de 25 Mo.')
            checksum.update(chunk)
            source.write(chunk)
        if not size or checksum.hexdigest() != expected:
            raise SyncTransportError('Image incomplète : la copie précédente est conservée.')
        source.seek(0)
        validate_file(source, field)
        with transaction.atomic():
            obj, _ = local_target(job)
            if getattr(obj, field.name).name != job.name:
                raise ObsoleteMedia()
            atomic_copy(source, field, job.name)
            notify_file_ready(job.ecole, job.model_label, job.object_uuid)


def sync_media(config, ecole, limit=4, direction=None):
    if not _lock.acquire(blocking=False):
        return 0, 0
    try:
        key = (config['server_url'], config['device_id'], str(ecole.sync_uuid))
        progress = _repair.setdefault(key, {})
        if time.monotonic() >= progress.get('next_scan', 0):
            if repair_page(ecole, progress):
                progress.clear()
                progress['next_scan'] = time.monotonic() + 300
        jobs = SyncMediaTransfer.objects.filter(ecole=ecole, complete=False, retry_at__lte=timezone.now())
        if direction is not None:
            jobs = jobs.filter(direction=direction)
        # Les logos ne doivent pas attendre la reprise de milliers de portraits.
        school_first = Case(When(model_label='eleves.Ecole', then=Value(0)),
                            default=Value(1), output_field=IntegerField())
        jobs = list(jobs.order_by(school_first, 'retry_at', 'pk')[:limit])
        sent = received = 0
        for job in jobs:
            try:
                (upload if job.direction == 'PUSH' else download)(config, job)
                finish(job)
                if job.direction == 'PUSH':
                    sent += 1
                else:
                    received += 1
            except ObsoleteMedia:
                finish(job)
            except (requests.RequestException, OSError, ValueError, Warning, SyncTransportError) as exc:
                attempts = job.attempts + 1
                SyncMediaTransfer.objects.filter(pk=job.pk, name=job.name, sha256=job.sha256).update(
                    attempts=attempts, error=str(exc)[:2000],
                    retry_at=timezone.now() + timedelta(seconds=min(300, 2 ** min(attempts, 8))))
                logger.warning('[Sync médias] %s %s.%s : %s', job.direction, job.model_label, job.field_name, exc)
        return sent, received
    finally:
        _lock.release()
