"""API binaire privée des postes : aucun accès par simple chemin fourni."""
import hashlib
from pathlib import PurePosixPath
import re
import tempfile
from uuid import UUID

from django.db import transaction
from django.http import FileResponse, JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .media import (CHUNK_SIZE, MAX_FILE_BYTES, atomic_copy, canonical_name,
                    digest_file, local_path, media_target, notify_file_ready, pending_name, safe_name, validate_file)
from .scope import scoped_queryset
from .views import _device_from_headers


def error(message, status):
    return JsonResponse({'ok': False, 'error': message}, status=status)


def matches(ecole, obj, field, expected, digest=''):
    names = {expected, pending_name(ecole, obj.sync_uuid, field.name, expected)}
    if digest:
        names.add(canonical_name(ecole, digest, expected))
    return getattr(obj, field.name).name in names


@transaction.non_atomic_requests
@csrf_exempt
@never_cache
@require_http_methods(['GET', 'POST'])
def media_file(request):
    device, denied = _device_from_headers(request)
    if denied:
        return denied
    try:
        label = request.GET.get('model')
        object_uuid = UUID(request.GET.get('object_uuid', ''))
        field_name = request.GET.get('field', '')
        expected = safe_name(request.GET.get('name'))
        digest = request.GET.get('sha256', '')
        if digest and not re.fullmatch('[a-f0-9]{64}', digest):
            raise ValueError('Empreinte du fichier invalide.')
        obj, field = media_target(device.ecole, label, object_uuid, field_name)
    except (ValueError, TypeError, AttributeError):
        return error('Référence de fichier invalide.', 400)
    if obj is None:
        return error('Objet introuvable dans cette école.', 404)
    current = getattr(obj, field.name).name
    checking = request.method == 'GET' and request.GET.get('check') == '1'
    if request.method == 'POST' or checking:
        if not matches(device.ecole, obj, field, expected, digest):
            return error('Le fichier a été remplacé ; changement devenu ancien.', 409)
    elif current != expected:
        return error('Le fichier a été remplacé ; changement devenu ancien.', 409)
    try:
        path = local_path(field, current)
    except ValueError:
        return error('Chemin de fichier interdit.', 400)
    if checking:
        present = path.is_file()
        if present and path.stat().st_size > MAX_FILE_BYTES:
            return error('Fichier trop volumineux pour la synchronisation.', 413)
        return JsonResponse({'ok': True, 'present': present, 'name': current,
            'sha256': digest_file(path) if present else '',
            'size': path.stat().st_size if present else 0})
    if request.method == 'GET':
        if not path.is_file():
            return error('Fichier absent du serveur ; nouvel essai nécessaire.', 404)
        if path.stat().st_size > MAX_FILE_BYTES:
            return error('Fichier trop volumineux pour la synchronisation.', 413)
        source = path.open('rb')
        checksum = hashlib.file_digest(source, 'sha256').hexdigest()
        source.seek(0)
        response = FileResponse(source, as_attachment=True, filename=PurePosixPath(current).name)
        response['X-Sync-Media-SHA256'] = checksum
        response['ETag'] = f'"{checksum}"'
        return response
    try:
        length = int(request.META.get('CONTENT_LENGTH') or 0)
    except (TypeError, ValueError):
        length = 0
    if not digest or length <= 0:
        return error('Taille et empreinte du fichier obligatoires.', 400)
    if length > MAX_FILE_BYTES:
        return error('Fichier trop volumineux pour la synchronisation.', 413)
    with tempfile.SpooledTemporaryFile(max_size=1024 * 1024) as source:
        checksum, received = hashlib.sha256(), 0
        while chunk := request.read(CHUNK_SIZE):
            received += len(chunk)
            if received > MAX_FILE_BYTES:
                return error('Fichier trop volumineux pour la synchronisation.', 413)
            checksum.update(chunk)
            source.write(chunk)
        if received != length or checksum.hexdigest() != digest:
            return error('Transfert incomplet ou empreinte incorrecte.', 400)
        source.seek(0)
        try:
            validate_file(source, field)
        except (OSError, ValueError, Warning):
            return error('Image invalide.', 400)
        target = canonical_name(device.ecole, digest, expected)
        # L'entrée peut avoir changé pendant le transfert : ne jamais la remplacer.
        with transaction.atomic():
            obj = scoped_queryset(type(obj), device.ecole).select_for_update().filter(pk=obj.pk).first()
            if obj is None or not matches(device.ecole, obj, field, expected, digest):
                return error('Le fichier a été remplacé ; changement devenu ancien.', 409)
            path = local_path(field, getattr(obj, field.name).name)
            if path.is_file() and digest_file(path) != digest:
                return error('Le serveur possède déjà une autre version de ce fichier.', 409)
            restored = not local_path(field, target).is_file() or digest_file(local_path(field, target)) != digest
            if restored:
                atomic_copy(source, field, target)
            if getattr(obj, field.name).name != target:
                setattr(obj, field.name, target)
                obj.save(update_fields=[field.name, 'sync_updated_at'])
            elif restored:
                notify_file_ready(device.ecole, label, obj.sync_uuid)
        return JsonResponse({'ok': True, 'present': True, 'name': target,
                             'sha256': digest, 'size': received})
