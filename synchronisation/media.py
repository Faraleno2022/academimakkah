"""Transfert de fichiers locaux : périmètre école, intégrité et écriture atomique."""
import hashlib
import logging
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
import warnings

from django.core.exceptions import FieldDoesNotExist
from django.db import models, transaction
from django.utils import timezone
from PIL import Image

from .models import SyncMediaTransfer
from .registry import SYNC_MODEL_LABELS, SYNC_MODEL_SET

MAX_FILE_BYTES = 25 * 1024 * 1024
CHUNK_SIZE = 64 * 1024
logger = logging.getLogger(__name__)


def file_fields(model):
    return [field for field in model._meta.concrete_fields if isinstance(field, models.FileField)]


def safe_name(name):
    if (not isinstance(name, str) or not name or len(name) > 500
            or '\\' in name or ':' in name or any(ord(c) < 32 for c in name)):
        raise ValueError('Nom de fichier invalide.')
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in ('', '.', '..') for part in name.split('/')):
        raise ValueError('Chemin de fichier interdit.')
    return name


def local_path(field, name):
    safe_name(name)
    root = Path(field.storage.location).resolve()
    path = Path(field.storage.path(name)).resolve()
    if not path.is_relative_to(root) or path == root:
        raise ValueError('Fichier hors du stockage autorisé.')
    return path


def digest_file(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def canonical_name(ecole, digest, original):
    safe_name(original)
    suffix = PurePosixPath(original).suffix.lower()
    if not re.fullmatch(r'\.[a-z0-9]{1,9}', suffix):
        suffix = '.bin'
    return f'sync/{ecole.sync_uuid.hex}/{digest[:48]}{suffix}'


def pending_name(ecole, object_uuid, field_name, name):
    # Un poste ne choisit jamais un chemin existant appartenant à une autre école.
    key = f'{object_uuid}:{field_name}:{safe_name(name)}'.encode()
    return canonical_name(ecole, hashlib.sha256(key).hexdigest(), name)


def incoming_name(ecole, obj, field, name):
    if not name:
        return name
    safe_name(name)
    current = getattr(obj, field.name).name
    return name if name == current else pending_name(ecole, obj.sync_uuid, field.name, name)


def validate_file(source, field):
    if isinstance(field, models.ImageField):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('error', Image.DecompressionBombWarning)
                image = Image.open(source)
                if image.format not in {'PNG', 'JPEG', 'GIF', 'WEBP', 'BMP', 'TIFF', 'AVIF', 'ICO'}:
                    raise ValueError('Format d’image non pris en charge.')
                image.verify()
        except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
            raise ValueError('Dimensions d’image excessives.') from exc
        source.seek(0)


def atomic_copy(source, field, name):
    """Le fichier n'apparaît qu'après une copie complète, dans MEDIA_ROOT."""
    target = local_path(field, name)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix='.sync-', delete=False) as output:
            temporary = Path(output.name)
            source.seek(0)
            while chunk := source.read(CHUNK_SIZE):
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def media_target(ecole, label, object_uuid, field_name):
    from .engine import get_model
    from .scope import scoped_queryset
    if label not in SYNC_MODEL_SET:
        raise ValueError('Modèle non synchronisable.')
    model = get_model(label)
    try:
        field = model._meta.get_field(field_name)
    except (FieldDoesNotExist, TypeError):
        raise ValueError('Champ fichier invalide.')
    if not isinstance(field, models.FileField):
        raise ValueError('Champ fichier invalide.')
    obj = scoped_queryset(model, ecole).filter(sync_uuid=object_uuid).first()
    return obj, field


def enqueue(ecole, label, object_uuid, field_name, name, direction, *, sha256='', force=False):
    safe_name(name)
    lookup = dict(ecole=ecole, model_label=label, object_uuid=object_uuid,
                  field_name=field_name, direction=direction)
    job, created = SyncMediaTransfer.objects.get_or_create(**lookup, defaults={'name': name, 'sha256': sha256})
    if not created and (job.name != name or (sha256 and job.sha256 != sha256) or (force and job.complete)):
        SyncMediaTransfer.objects.filter(pk=job.pk).update(
            name=name, sha256=sha256, complete=False, attempts=0, error='', retry_at=timezone.now())
    from .autosync import notifier_medias
    transaction.on_commit(notifier_medias)


def queue_received(ecole, label, object_uuid, payload):
    from .engine import get_model
    if label not in SYNC_MODEL_SET or not object_uuid:
        return
    for field in file_fields(get_model(label)):
        name = payload.get(field.name)
        if name:
            path = local_path(field, name)
            try:
                exists = path.is_file()
            except OSError:
                exists = False
            # Un fichier présent ne doit pas être retéléchargé à chaque donnée.
            if not exists:
                enqueue(ecole, label, object_uuid, field.name, name, 'PULL', force=True)


def queue_sent(ecole, changes):
    from .engine import get_model
    for change in changes:
        if change.operation == 'DELETE' or not change.object_uuid or change.model_label not in SYNC_MODEL_SET:
            continue
        for field in file_fields(get_model(change.model_label)):
            name = change.payload.get(field.name)
            if not name:
                continue
            try:
                exists = local_path(field, name).is_file()
            except OSError:
                exists = True  # Le transfert signale l'erreur sans bloquer l'acquittement métier.
            except ValueError:
                logger.warning('[Sync médias] Chemin local invalide pour %s.%s', change.model_label, field.name)
                continue
            if exists:
                enqueue(ecole, change.model_label, change.object_uuid, field.name, name, 'PUSH')


def notify_file_ready(ecole, label, object_uuid):
    from .models import SyncChange
    # L'image peut arriver après le rendu HTML qui affichait encore un fichier absent.
    SyncChange.objects.create(ecole=ecole, model_label=label, object_uuid=object_uuid,
        operation='UPDATE', payload={'_sync_media_received': True},
        statut=SyncChange.STATUT_APPLIED, date_application=timezone.now())


def repair_page(ecole, progress, limit=50):
    """Rattrape aussi les chemins synchronisés avant l'ajout du transfert binaire."""
    from .engine import get_model
    from .scope import scoped_queryset
    labels = [label for label in SYNC_MODEL_LABELS if file_fields(get_model(label))]
    index, last = progress.get('index', 0), progress.get('last', 0)
    if index >= len(labels):
        return True
    label = labels[index]
    model = get_model(label)
    fields = file_fields(model)
    objects = list(scoped_queryset(model, ecole).filter(pk__gt=last).order_by('pk')
                   .only('pk', 'sync_uuid', *(f.name for f in fields))[:limit])
    for obj in objects:
        for field in fields:
            name = getattr(obj, field.name).name
            if name:
                try:
                    path = local_path(field, name)
                    try:
                        exists = path.is_file()
                    except OSError:
                        exists = False
                    enqueue(ecole, label, obj.sync_uuid, field.name, name,
                            'PUSH' if exists else 'PULL', force=not exists)
                except ValueError:
                    logger.warning('[Sync médias] Chemin ancien invalide pour %s.%s', label, field.name)
                    continue
    if len(objects) < limit:
        progress.update(index=index + 1, last=0)
    else:
        progress['last'] = objects[-1].pk
    return progress.get('index', 0) >= len(labels)
