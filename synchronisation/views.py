import gzip
import json
import logging
import secrets
from uuid import UUID

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import DatabaseError, InterfaceError, OperationalError, transaction
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import render
from django.utils.dateparse import parse_datetime
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from django.views.decorators.csrf import csrf_exempt, csrf_protect
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from eleves.models import Ecole
from utilisateurs.utils import user_is_admin, user_school

import threading

from .engine import (
    apply_sync_change, dependances_pour_poste, emettre_existants, snapshot_changes_for_ecole,
    snapshot_page_for_ecole, get_model, model_label_for, queryset_for_ecole, serialize_instance,
)
from .autosync import INTERVALLE_DEFAUT
from .liaison import NOM_FICHIER_SCRIPT, message_poste, script_connexion_poste, utiliser_code
from .models import SyncChange, SyncDevice
from .registry import (
    MODELES_PAR_VERSION, SYNC_MODEL_LABELS, SYNC_MODEL_SET, modeles_inconnus, version_registre,
)
from .sequence import attribuer_sequences


PULL_PAGE_SIZE = 500

logger = logging.getLogger(__name__)

_rattrapage_verrou = threading.Lock()
_rattrapage_fait = False


def _rattraper_modeles_ajoutes():
    """Une fois par processus : diffuse les lignes des modèles ajoutés au
    registre après leur création (saisies sur le site avant la mise à jour)."""
    global _rattrapage_fait
    if _rattrapage_fait:
        return
    with _rattrapage_verrou:
        if _rattrapage_fait:
            return
        try:
            emettre_existants([label for labels in MODELES_PAR_VERSION.values() for label in labels])
        except Exception:
            # Ne bloque jamais la réception : nouvel essai à la prochaine requête.
            logger.exception('[Sync] Rattrapage des modèles ajoutés impossible.')
            return
        _rattrapage_fait = True


def _json_body(request):
    body = request.body
    if not body:
        return {}
    if request.META.get('HTTP_CONTENT_ENCODING', '').lower() == 'gzip':
        try:
            body = gzip.decompress(body)
        except (OSError, EOFError):
            return None
    try:
        data = json.loads(body.decode('utf-8'))
        return data if isinstance(data, dict) else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _current_school(user, data=None):
    if user and user.is_authenticated and user.is_superuser and data and data.get('ecole_id'):
        return Ecole.objects.filter(pk=data['ecole_id']).first()
    if user and user.is_authenticated:
        return user_school(user)
    if data and data.get('ecole_id'):
        return Ecole.objects.filter(pk=data['ecole_id']).first()
    return None


def _has_sync_admin_token(request):
    token = request.headers.get('X-Sync-Admin-Token', '')
    expected = getattr(settings, 'MYSCHOOL_SYNC_ADMIN_TOKEN', '')
    return bool(expected and token and constant_time_compare(token, expected))


def _has_sync_admin_access(request):
    if _has_sync_admin_token(request):
        return True
    user = getattr(request, 'user', None)
    return bool(user and user.is_authenticated and user_is_admin(user))


def _device_from_headers(request):
    device_id = request.headers.get('X-Sync-Device')
    token = request.headers.get('X-Sync-Token')
    if not device_id or not token:
        return None, JsonResponse({'ok': False, 'error': 'Identifiants de synchronisation manquants.'}, status=401)
    try:
        UUID(device_id)
    except ValueError:
        return None, JsonResponse({'ok': False, 'error': 'Identifiant appareil invalide.'}, status=400)

    device = SyncDevice.objects.select_related('ecole').filter(device_id=device_id, actif=True).first()
    if not device or not device.verifier_token(token):
        return None, JsonResponse({'ok': False, 'error': 'Appareil non autorise.'}, status=403)
    device.marquer_connexion()
    return device, None


def _schools_for_user(user):
    if user.is_superuser:
        return Ecole.objects.all().order_by('nom')
    ecole = user_school(user)
    if ecole:
        return Ecole.objects.filter(pk=ecole.pk)
    return Ecole.objects.none()


@require_GET
def health(request):
    return JsonResponse({
        'ok': True,
        'service': 'myschoolgn-sync',
        'version': 1,
        'server_time': timezone.now().isoformat(),
    })


def _script_ou_rien(server_url, code):
    """Fichier de connexion ; aucun si l'adresse du site ne s'y prête pas (http hors local)."""
    from .liaison import LiaisonImpossible
    try:
        return script_connexion_poste(server_url, code)
    except LiaisonImpossible:
        return ''


@login_required
def device_setup(request):
    if not user_is_admin(request.user):
        return render(request, 'utilisateurs/permission_denied.html', status=403)

    ecoles = _schools_for_user(request.user)
    generated = None

    if request.method == 'POST':
        ecole_id = request.POST.get('ecole_id')
        nom = (request.POST.get('nom') or 'Poste local').strip()[:120]
        ecole = ecoles.filter(pk=ecole_id).first()

        if not ecole:
            messages.error(request, "Ecole introuvable ou non autorisee.")
        else:
            token = secrets.token_urlsafe(32)
            device = SyncDevice(ecole=ecole, nom=nom or 'Poste local')
            device.definir_token(token)
            code_liaison = device.nouveau_code_liaison()
            device.save()

            server_url = request.build_absolute_uri('/').rstrip('/')
            generated = {
                'device': device,
                'token': token,
                'server_url': server_url,
                # Liaison à distance : le fichier .cmd ne contient que le code
                # (usage unique, expirant), jamais le jeton.
                'code_liaison': code_liaison,
                'code_expire': device.code_liaison_expire,
                'script_cmd': _script_ou_rien(server_url, code_liaison),
                'nom_script': NOM_FICHIER_SCRIPT,
                'message_poste': message_poste(server_url, code_liaison, device.code_liaison_expire, ecole.nom),
                # Contenu de sync_config.json, à poser à côté de MySchoolGN.exe.
                # Le jeton n'est conservé que haché : ce fichier ne peut être
                # produit qu'ici, et il est téléchargé depuis le navigateur.
                'config_json': json.dumps({
                    'MYSCHOOL_SYNC_SERVER_URL': server_url,
                    'MYSCHOOL_SYNC_ECOLE_ID': str(ecole.id),
                    'MYSCHOOL_SYNC_DEVICE_ID': str(device.device_id),
                    'MYSCHOOL_SYNC_TOKEN': token,
                    'MYSCHOOL_SYNC_INTERVAL': str(INTERVALLE_DEFAUT),
                }, indent=2),
                'env_block': "\n".join([
                    f"MYSCHOOL_SYNC_SERVER_URL={server_url}",
                    f"MYSCHOOL_SYNC_DEVICE_ID={device.device_id}",
                    f"MYSCHOOL_SYNC_TOKEN={token}",
                    f"MYSCHOOL_SYNC_ECOLE_ID={ecole.id}",
                ]),
            }
            messages.success(request, "Poste créé : envoyez le fichier de connexion (ou le code) à l'utilisateur du poste.")

    return render(request, 'synchronisation/device_setup.html', {
        'titre_page': 'Connexion offline',
        'ecoles': ecoles,
        'generated': generated,
    })


ESSAIS_LIAISON_MAX = 10
FENETRE_ESSAIS_LIAISON = 15 * 60


def _cle_essais_liaison(request):
    adresse = (request.META.get('HTTP_X_FORWARDED_FOR') or request.META.get('REMOTE_ADDR') or '').split(',')[0].strip()
    return f'sync-liaison-echecs:{adresse}'


@csrf_exempt
@require_http_methods(['GET', 'POST'])
def liaison(request):
    """Liaison d'un poste éloigné par code à usage unique.

    GET ?code= : aperçu (école, poste) sans consommer le code.
    POST {"code"} : consomme le code et renvoie la configuration du poste.
    Les échecs répétés depuis une même adresse sont bloqués un moment.
    """
    from django.core.cache import cache
    from .models import SyncDevice

    cle = _cle_essais_liaison(request)
    if (cache.get(cle) or 0) >= ESSAIS_LIAISON_MAX:
        return JsonResponse({'ok': False, 'error': "Trop d'essais. Réessayez dans quelques minutes."}, status=429)

    if request.method == 'POST':
        data = _json_body(request) or {}
        code = data.get('code')
    else:
        code = request.GET.get('code')

    def echec():
        cache.set(cle, (cache.get(cle) or 0) + 1, FENETRE_ESSAIS_LIAISON)
        return JsonResponse(
            {'ok': False, 'error': 'Code de liaison invalide, expiré ou déjà utilisé.'}, status=404,
        )

    if request.method == 'GET':
        poste = SyncDevice.par_code_liaison(code)
        if poste is None:
            return echec()
        return JsonResponse({
            'ok': True,
            'ecole_nom': poste.ecole.nom,
            'ecole_sync_uuid': str(poste.ecole.sync_uuid),
            'poste_nom': poste.nom,
            'expire_le': poste.code_liaison_expire.isoformat(),
        })

    resultat = utiliser_code(code, request.build_absolute_uri('/').rstrip('/'))
    if resultat is None:
        return echec()
    return JsonResponse({'ok': True, **resultat})


@csrf_exempt
@require_POST
def register_device(request):
    # API tokens do not rely on cookies; session authentication requires CSRF.
    if _has_sync_admin_token(request):
        return _register_device(request)
    return _register_device_with_session(request)


@csrf_protect
def _register_device_with_session(request):
    return _register_device(request)


def _register_device(request):
    if not _has_sync_admin_access(request):
        return JsonResponse({'ok': False, 'error': 'Permission refusee.'}, status=403)

    data = _json_body(request)
    if data is None:
        return JsonResponse({'ok': False, 'error': 'JSON invalide.'}, status=400)

    try:
        ecole = _current_school(request.user, data)
    except (ValueError, TypeError):
        return JsonResponse({'ok': False, 'error': 'École invalide.'}, status=400)
    if not ecole:
        return JsonResponse({'ok': False, 'error': 'Aucune ecole associee a cet utilisateur.'}, status=400)

    nom = data.get('nom') or data.get('name') or 'Poste local'
    if not isinstance(nom, str):
        return JsonResponse({'ok': False, 'error': "Nom de l'appareil invalide."}, status=400)
    nom = nom.strip()[:120]
    token = secrets.token_urlsafe(32)
    device = SyncDevice(ecole=ecole, nom=nom)
    device.definir_token(token)
    device.save()

    return JsonResponse({
        'ok': True,
        'device_id': str(device.device_id),
        'sync_token': token,
        'ecole_id': ecole.id,
        'message': 'Conservez ce token sur le poste local. Il ne sera plus affiche.',
    }, status=201)


@transaction.non_atomic_requests
@csrf_exempt
@require_POST
def push(request):
    device, error_response = _device_from_headers(request)
    if error_response:
        return error_response

    data = _json_body(request)
    if data is None:
        return JsonResponse({'ok': False, 'error': 'JSON invalide.'}, status=400)

    changes = data.get('changes', [])
    if not isinstance(changes, list) or len(changes) > 1000:
        return JsonResponse({'ok': False, 'error': 'Le champ changes doit etre une liste.'}, status=400)

    accepted = []
    rejected = []
    valid_operations = {choice[0] for choice in SyncChange.OPERATION_CHOICES}

    # Chaque donnée a sa transaction : les verrous sont libérés rapidement
    # et un incident de base ne rend pas tout le lot inutilisable.
    for index, change in enumerate(changes):
        if not isinstance(change, dict):
            rejected.append({'index': index, 'error': 'Changement invalide.'})
            continue

        raw_operation = change.get('operation')
        raw_model = change.get('model') or change.get('model_label')
        if not isinstance(raw_operation, str) or not isinstance(raw_model, str):
            rejected.append({'index': index, 'error': 'Opération ou modèle invalide.'})
            continue
        operation = raw_operation.upper()
        model_label = raw_model.strip()
        payload = change.get('payload') or {}
        raw_uuid = change.get('object_uuid')

        if operation not in valid_operations:
            rejected.append({'index': index, 'error': 'Operation invalide.'})
            continue
        if not model_label:
            rejected.append({'index': index, 'error': 'Modele manquant.'})
            continue
        if not isinstance(payload, dict):
            rejected.append({'index': index, 'error': 'Payload invalide.'})
            continue

        object_uuid = None
        if raw_uuid:
            try:
                object_uuid = UUID(str(raw_uuid))
            except ValueError:
                rejected.append({'index': index, 'error': 'UUID objet invalide.'})
                continue

        if object_uuid and 'sync_uuid' not in payload:
            payload = {**payload, 'sync_uuid': str(object_uuid)}

        try:
            with transaction.atomic():
                sync_change = SyncChange.objects.create(
                    ecole=device.ecole,
                    device=device,
                    model_label=model_label[:120],
                    object_uuid=object_uuid,
                    operation=operation,
                    payload=payload,
                )
                apply_sync_change(sync_change)
            accepted.append({'index': index, 'change_id': sync_change.id})
        except Exception as exc:
            # Le rollback de la donnée est terminé avant d'écrire son erreur.
            # Un deadlock peut invalider tous les savepoints : une transaction
            # englobant le lot empêcherait même l'enregistrement de ce refus.
            logger.exception('[Sync] Échec du changement %s %s (index %s)', operation, model_label, index)
            try:
                with transaction.atomic():
                    sync_change = SyncChange.objects.create(
                        ecole=device.ecole,
                        device=device,
                        model_label=model_label[:120],
                        object_uuid=object_uuid,
                        operation=operation,
                        payload=payload,
                        statut=SyncChange.STATUT_FAILED,
                        erreur=str(exc),
                        date_application=timezone.now(),
                    )
            except DatabaseError:
                logger.exception('[Sync] Base indisponible lors de l’enregistrement du refus.')
                # Sans acquittement fiable, le poste conserve son lot en attente.
                return JsonResponse({
                    'ok': False, 'error': 'Base de synchronisation indisponible. Réessayez plus tard.',
                }, status=503)
            rejected.append({
                'index': index, 'change_id': sync_change.id, 'error': str(exc),
                'retryable': isinstance(exc, (OperationalError, InterfaceError, transaction.TransactionManagementError)),
            })

    return JsonResponse({
        'ok': True,
        'accepted_count': len(accepted),
        'rejected_count': len(rejected),
        'accepted': accepted,
        'rejected': rejected,
        'server_time': timezone.now().isoformat(),
    })


@csrf_exempt
@require_http_methods(['GET', 'POST'])
def pull(request):
    device, error_response = _device_from_headers(request)
    if error_response:
        return error_response

    cursor = request.GET.get('snapshot_cursor')
    paginated = request.GET.get('snapshot_version') == '2'
    since = request.GET.get('since')
    since_id = request.GET.get('since_id')
    initial = request.GET.get('initial') in {'1', 'true', 'yes'}
    registre = request.GET.get('registre')
    modeles = request.GET.get('modeles')
    if request.method == 'POST':
        data = _json_body(request)
        if data is None:
            return JsonResponse({'ok': False, 'error': 'JSON invalide.'}, status=400)
        cursor = data.get('snapshot_cursor') or cursor
        paginated = str(data.get('snapshot_version', '')) == '2' or paginated
        since = data.get('since') or since
        since_id = data.get('since_id') or since_id
        initial = str(data.get('initial', '')).lower() in {'1', 'true', 'yes'}
        registre = data.get('registre') or registre
        modeles = data.get('modeles') or modeles

    _rattraper_modeles_ajoutes()
    # Un poste d'une version antérieure ne sait pas appliquer les modèles
    # ajoutés depuis : les lui envoyer les mettrait en échec à chaque cycle.
    exclus = set(modeles_inconnus(version_registre(registre)))
    autorises = [label for label in SYNC_MODEL_LABELS if label not in exclus]
    if modeles:
        # Rattrapage d'un poste mis à jour : seulement les modèles demandés.
        demandes = {label.strip() for label in str(modeles).split(',')} & SYNC_MODEL_SET
        autorises = [label for label in autorises if label in demandes]

    if initial:
        next_cursor = None
        watermark = since_id
        if paginated:
            try:
                serialized_changes, next_cursor, watermark = snapshot_page_for_ecole(
                    device.ecole, cursor, labels=set(autorises),
                )
            except (ValueError, TypeError) as exc:
                return JsonResponse({'ok': False, 'error': str(exc)}, status=400)
        else:
            # Compatibility with existing PCs: a complete snapshot without the former 5,000 cap.
            serialized_changes = snapshot_changes_for_ecole(device.ecole, labels=set(autorises))
        return JsonResponse({
            'ok': True,
            'device_id': str(device.device_id),
            'ecole_id': device.ecole_id,
            'since': since,
            'since_id': since_id,
            'initial': True,
            'changes': serialized_changes,
            'latest_change_id': watermark,
            'next_snapshot_cursor': next_cursor,
            'snapshot_complete': next_cursor is None,
            'server_time': timezone.now().isoformat(),
        })

    # Le curseur des postes porte sur le numéro de diffusion, attribué après
    # validation : un lot validé en retard ne peut plus être sauté.
    attribuer_sequences()
    changes = (
        SyncChange.objects
        .filter(ecole=device.ecole, sequence__isnull=False)
        .filter(Q(statut=SyncChange.STATUT_APPLIED) | Q(device__isnull=True, statut=SyncChange.STATUT_PENDING))
        .exclude(device=device)
        .select_related('device')
    )
    if exclus or modeles:
        changes = changes.filter(model_label__in=autorises)
    if since_id:
        try:
            changes = changes.filter(sequence__gt=int(since_id))
        except (TypeError, ValueError):
            return JsonResponse({'ok': False, 'error': 'since_id invalide.'}, status=400)
    elif since:
        parsed_since = parse_datetime(str(since))
        if not parsed_since:
            return JsonResponse({'ok': False, 'error': 'since invalide. Utilisez une date ISO ou since_id.'}, status=400)
        if timezone.is_naive(parsed_since):
            parsed_since = timezone.make_aware(parsed_since, timezone.get_current_timezone())
        changes = changes.filter(date_creation__gt=parsed_since)

    changes = changes.order_by('sequence')[:PULL_PAGE_SIZE]
    changes = list(changes)
    serialized_changes = []
    deja_vus = set()
    for change in changes:
        model = get_model(change.model_label)
        if model is None:
            continue
        payload = change.payload
        if change.operation != 'DELETE':
            obj = queryset_for_ecole(model, device.ecole).filter(sync_uuid=change.object_uuid).first()
            if obj is None:
                continue
            payload = serialize_instance(obj)
            # Les parents passent avant : un poste qui les a manqués peut
            # quand même appliquer ce changement au lieu de bloquer dessus.
            deja_vus.add((model_label_for(model), obj.pk))
            serialized_changes.extend(
                ligne for ligne in dependances_pour_poste(obj, device.ecole, deja_vus)
                if ligne['model_label'] not in exclus
            )
        else:
            # A deletion conveys no historical personal data.
            payload = {'sync_uuid': str(change.object_uuid)}
        serialized_changes.append({
            'id': change.id, 'model': change.model_label, 'model_label': change.model_label,
            'object_uuid': str(change.object_uuid) if change.object_uuid else None,
            'operation': change.operation, 'payload': payload,
            'device_id': str(change.device.device_id) if change.device else None,
            'device_name': change.device.nom if change.device else None,
            'date_creation': change.date_creation.isoformat(),
        })


    return JsonResponse({
        'ok': True,
        'device_id': str(device.device_id),
        'ecole_id': device.ecole_id,
        'since': since,
        'since_id': since_id,
        'changes': serialized_changes,
        'latest_change_id': changes[-1].sequence if changes else since_id,
        'has_more': len(changes) == PULL_PAGE_SIZE,
        'server_time': timezone.now().isoformat(),
    })
