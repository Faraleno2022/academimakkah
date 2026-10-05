"""Liaison d'un poste éloigné au serveur en ligne, sans configuration manuelle.

Sur le site, l'administrateur génère un code de liaison à usage unique (valable
quelques jours) et le fichier « Connecter_MySchoolGN.cmd ». Sur le poste, ce
fichier démarre MySchoolGN si besoin et ouvre la page « Connexion au serveur »
avec le code. Le poste échange le code contre ses identifiants et écrit lui-même
sync_config.json : ni jeton dans le fichier envoyé, ni recopie à la main.
"""
import json
import os
import re
from urllib.parse import urlsplit

from django.db import transaction
from django.utils import timezone

PAGE_POSTE = '/synchronisation/connecter/'
API_LIAISON = '/api/v1/sync/devices/liaison/'
NOM_FICHIER_SCRIPT = 'Connecter_MySchoolGN.cmd'
NOM_FICHIER_CONFIG = 'sync_config.json'
NOM_FICHIER_PORT = '.port'

# Le script est un fichier exécutable : seules ces valeurs y sont insérées.
_SERVEUR_SUR = re.compile(r'^https?://[A-Za-z0-9.-]+(:\d{1,5})?$')
_CODE_SUR = re.compile(r'^[A-Z0-9]{4}-[A-Z0-9]{4}$')


class LiaisonImpossible(Exception):
    """Message destiné à l'utilisateur."""


def normaliser_serveur(url):
    """« https://www.myschoolgn.space/ » -> « https://www.myschoolgn.space »."""
    url = (url or '').strip()
    parties = urlsplit(url)
    if parties.scheme not in ('http', 'https') or not parties.hostname:
        raise LiaisonImpossible("Adresse du serveur invalide.")
    hote = parties.hostname
    if parties.scheme == 'http' and hote not in ('localhost', '127.0.0.1'):
        raise LiaisonImpossible("Le serveur doit être en https.")
    serveur = f'{parties.scheme}://{hote}' + (f':{parties.port}' if parties.port else '')
    if not _SERVEUR_SUR.match(serveur):
        raise LiaisonImpossible("Adresse du serveur invalide.")
    return serveur


# ---------------------------------------------------------------------------
# Côté serveur en ligne
# ---------------------------------------------------------------------------

def script_connexion_poste(serveur, code):
    """Contenu de Connecter_MySchoolGN.cmd (fins de ligne Windows, ASCII)."""
    serveur = normaliser_serveur(serveur)
    if not _CODE_SUR.match(code or ''):
        raise ValueError('Code de liaison invalide.')
    lignes = [
        '@echo off',
        'setlocal EnableExtensions',
        'title MySchoolGN - Connexion au serveur',
        f'set "SERVEUR={serveur}"',
        f'set "CODE={code}"',
        'echo.',
        'echo  Connexion de ce poste MySchoolGN au serveur %SERVEUR%',
        'echo  Code de liaison : %CODE%',
        'echo.',
        'set "APP="',
        'rem 1. MySchoolGN deja ouvert : son dossier contient le fichier .port',
        'for /f "delims=" %%P in (\'powershell -NoProfile -Command "Get-Process MySchoolGN -ErrorAction SilentlyContinue | ForEach-Object { $_.Path }"\') do (',
        f'  if exist "%%~dpP{NOM_FICHIER_PORT}" set "APP=%%P"',
        ')',
        'set "OUVERT=%APP%"',
        'rem 2. Sinon : emplacements d\'installation habituels',
        'if not defined APP if exist "%LOCALAPPDATA%\\Programs\\MySchoolGN\\MySchoolGN.exe" set "APP=%LOCALAPPDATA%\\Programs\\MySchoolGN\\MySchoolGN.exe"',
        'if not defined APP if exist "%ProgramFiles%\\MySchoolGN\\MySchoolGN.exe" set "APP=%ProgramFiles%\\MySchoolGN\\MySchoolGN.exe"',
        'if not defined APP if exist "%ProgramFiles(x86)%\\MySchoolGN\\MySchoolGN.exe" set "APP=%ProgramFiles(x86)%\\MySchoolGN\\MySchoolGN.exe"',
        'if not defined APP goto introuvable',
        'for %%F in ("%APP%") do set "DOSSIER=%%~dpF"',
        'if not defined OUVERT (',
        '  echo  Demarrage de MySchoolGN, patientez...',
        '  start "" "%APP%"',
        ')',
        'set "PORT="',
        'for /L %%i in (1,1,90) do (',
        '  if not defined PORT call :lire_port',
        '  if not defined PORT ping -n 3 127.0.0.1 >nul',
        ')',
        'if not defined PORT goto pas_de_reponse',
        f'set "URL=http://127.0.0.1:%PORT%{PAGE_POSTE}?code=%CODE%&serveur=%SERVEUR%"',
        'set "EDGE=%ProgramFiles(x86)%\\Microsoft\\Edge\\Application\\msedge.exe"',
        'if not exist "%EDGE%" set "EDGE=%ProgramFiles%\\Microsoft\\Edge\\Application\\msedge.exe"',
        'rem La fenetre de MySchoolGN garde la session : ouverture dans son profil.',
        'if exist "%EDGE%" (',
        '  start "" "%EDGE%" --app="%URL%" --user-data-dir="%DOSSIER%.appwindow"',
        ') else (',
        '  start "" "%URL%"',
        ')',
        'echo  La page de connexion est ouverte dans MySchoolGN.',
        'echo  Verifiez le nom de l\'ecole puis cliquez sur "Connecter ce poste".',
        'echo  (Si MySchoolGN demande de vous identifier, connectez-vous d\'abord.)',
        'ping -n 21 127.0.0.1 >nul',
        'exit /b 0',
        '',
        ':lire_port',
        f'if not exist "%DOSSIER%{NOM_FICHIER_PORT}" exit /b 0',
        f'set /p P=<"%DOSSIER%{NOM_FICHIER_PORT}"',
        'if exist "%SystemRoot%\\System32\\curl.exe" (',
        '  curl -s -o nul -m 3 "http://127.0.0.1:%P%/api/v1/sync/health/" && set "PORT=%P%"',
        ') else (',
        '  set "PORT=%P%"',
        ')',
        'exit /b 0',
        '',
        ':introuvable',
        'echo  MySchoolGN est introuvable sur ce PC. Installez-le puis relancez ce fichier.',
        'pause',
        'exit /b 1',
        '',
        ':pas_de_reponse',
        'echo  MySchoolGN ne repond pas. Ouvrez MySchoolGN, puis relancez ce fichier.',
        'pause',
        'exit /b 1',
    ]
    return '\r\n'.join(lignes) + '\r\n'


def message_poste(serveur, code, expire, nom_ecole):
    """Texte à envoyer à l'utilisateur du poste (WhatsApp, SMS, e-mail)."""
    return (
        f"Connexion de MySchoolGN au serveur de {nom_ecole} :\n"
        f"1. Ouvrez le fichier {NOM_FICHIER_SCRIPT} reçu (double-clic) sur le PC où MySchoolGN est installé.\n"
        f"2. Dans MySchoolGN, vérifiez le nom de l'école puis cliquez sur « Connecter ce poste ».\n"
        f"Sans le fichier : menu Administration > Connexion au serveur, puis saisissez le code {code}.\n"
        f"Serveur : {serveur}\n"
        f"Le code est valable jusqu'au {timezone.localtime(expire).strftime('%d/%m/%Y à %H:%M')} "
        f"et ne sert qu'une fois."
    )


def utiliser_code(code, serveur_url):
    """Consomme le code : nouveau jeton du poste et configuration à écrire."""
    from .autosync import INTERVALLE_DEFAUT
    from .models import SyncDevice
    import secrets

    with transaction.atomic():
        poste = SyncDevice.par_code_liaison(code)
        if poste is None:
            return None
        poste = SyncDevice.objects.select_for_update().select_related('ecole').get(pk=poste.pk)
        if not poste.code_liaison_hash or poste.code_liaison_expire is None \
                or poste.code_liaison_expire <= timezone.now():
            return None
        jeton = secrets.token_urlsafe(32)
        poste.definir_token(jeton)
        poste.code_liaison_hash = ''
        poste.code_liaison_expire = None
        poste.lie_le = timezone.now()
        poste.save(update_fields=[
            'token_hash', 'code_liaison_hash', 'code_liaison_expire', 'lie_le', 'date_modification',
        ])
    return {
        'ecole_nom': poste.ecole.nom,
        'ecole_sync_uuid': str(poste.ecole.sync_uuid),
        'poste_nom': poste.nom,
        'config': {
            'MYSCHOOL_SYNC_SERVER_URL': serveur_url,
            'MYSCHOOL_SYNC_ECOLE_ID': str(poste.ecole_id),
            'MYSCHOOL_SYNC_DEVICE_ID': str(poste.device_id),
            'MYSCHOOL_SYNC_TOKEN': jeton,
            'MYSCHOOL_SYNC_INTERVAL': str(INTERVALLE_DEFAUT),
        },
    }


# ---------------------------------------------------------------------------
# Côté poste (application installée)
# ---------------------------------------------------------------------------

def dossier_application():
    from ecole_moderne.sauvegarde import base_dir
    return base_dir()


def config_actuelle():
    """Configuration de synchronisation en vigueur sur ce poste (fichier ou .env)."""
    from .autosync import charger_config
    return charger_config(dossier_application())


def _appel_serveur(serveur, methode, **kwargs):
    import requests
    try:
        reponse = requests.request(methode, serveur + API_LIAISON, timeout=25, **kwargs)
    except requests.RequestException as exc:
        raise LiaisonImpossible(
            "Le serveur ne répond pas. Vérifiez la connexion Internet de ce PC puis réessayez."
        ) from exc
    try:
        donnees = reponse.json()
    except ValueError:
        donnees = {}
    if reponse.status_code == 429:
        raise LiaisonImpossible("Trop d'essais. Patientez quelques minutes puis réessayez.")
    if reponse.status_code >= 400 or not donnees.get('ok'):
        raise LiaisonImpossible(
            donnees.get('error') or "Code de liaison invalide, expiré ou déjà utilisé."
        )
    return donnees


def apercu_liaison(serveur, code):
    """École et poste correspondant au code, sans le consommer."""
    return _appel_serveur(normaliser_serveur(serveur), 'GET', params={'code': code})


def verifier_ecole_compatible(ecole_sync_uuid):
    """Refuse de lier un poste qui contient déjà une autre école."""
    from eleves.models import Ecole
    locales = list(Ecole.objects.values_list('sync_uuid', flat=True))
    if locales and ecole_sync_uuid not in {str(u) for u in locales}:
        raise LiaisonImpossible(
            "Ce poste contient déjà une autre école. La liaison mélangerait les données : "
            "utilisez un poste neuf ou contactez l'assistance."
        )


def lier_poste(serveur, code):
    """Échange le code contre les identifiants et active la synchronisation."""
    from .autosync import notifier_changement
    from .models import SyncCheckpoint

    serveur = normaliser_serveur(serveur)
    apercu = _appel_serveur(serveur, 'GET', params={'code': code})
    verifier_ecole_compatible(apercu.get('ecole_sync_uuid'))
    donnees = _appel_serveur(serveur, 'POST', json={'code': code})
    verifier_ecole_compatible(donnees.get('ecole_sync_uuid'))
    config = dict(donnees['config'])
    # Adresse effectivement jointe par ce poste (pas celle vue derrière un proxy).
    config['MYSCHOOL_SYNC_SERVER_URL'] = serveur

    ancienne = config_actuelle()
    if ancienne.get('device_id') and ancienne['device_id'] != config['MYSCHOOL_SYNC_DEVICE_ID']:
        # Poste déjà synchronisé avec la même école : reprendre là où il en était.
        precedent = SyncCheckpoint.objects.filter(device_id=ancienne['device_id']).first()
        if precedent is not None:
            SyncCheckpoint.objects.update_or_create(
                device_id=config['MYSCHOOL_SYNC_DEVICE_ID'],
                defaults={
                    'initial_complete': precedent.initial_complete,
                    'snapshot_cursor': precedent.snapshot_cursor,
                    'last_change_id': precedent.last_change_id,
                    'registre': precedent.registre,
                    'rattrapage_id': precedent.rattrapage_id,
                },
            )

    chemin = os.path.join(dossier_application(), NOM_FICHIER_CONFIG)
    if os.path.exists(chemin):
        os.replace(chemin, chemin + '.ancien')
    temporaire = chemin + '.tmp'
    with open(temporaire, 'w', encoding='utf-8') as fichier:
        json.dump(config, fichier, indent=2)
    os.replace(temporaire, chemin)
    # Le worker relit la configuration à chaque cycle : pas de redémarrage.
    notifier_changement()
    return {'ecole_nom': donnees.get('ecole_nom'), 'poste_nom': donnees.get('poste_nom'), 'serveur': serveur}
