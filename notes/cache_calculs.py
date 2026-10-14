"""Clés de calcul courtes et invalidation indépendante des libellés affichés."""
import hashlib
import json
from uuid import uuid4

from django.core.cache import cache


CACHE_SCHEMA_VERSION = 4
VERSION_TIMEOUT = 86400


def cle_cache_calcul(famille, classe_id, *, periode, systeme='', version='',
                     selection='', revision=''):
    # L'empreinte conserve toutes les dimensions du calcul, même si un libellé
    # contient des espaces, accents ou caractères de contrôle, ou est très long.
    identite = json.dumps([classe_id, periode, systeme, version, selection, revision],
                          ensure_ascii=True, separators=(',', ':'), default=str)
    empreinte = hashlib.blake2s(identite.encode('utf-8'), digest_size=16).hexdigest()
    return f'notes:{famille}:s{CACHE_SCHEMA_VERSION}:c{classe_id}:{empreinte}'


def version_cache_classe(classe_id):
    cle = f'moy_version_classe_{classe_id}'
    version = cache.get(cle)
    if version is None:
        # Une clé de version évincée ne doit pas réactiver d'anciens résultats.
        nouvelle = uuid4().hex
        if cache.add(cle, nouvelle, VERSION_TIMEOUT):
            return nouvelle
        version = cache.get(cle, nouvelle)
    return version


def invalider_calculs_classe(classe_id):
    # Un jeton neuf évite les mises à jour perdues du compteur get()+set()
    # lorsque deux postes enregistrent simultanément des notes.
    cache.set(f'moy_version_classe_{classe_id}', uuid4().hex, VERSION_TIMEOUT)
