"""Teste le clic et la création complète sur une base/dossier temporaires.

Nécessite Playwright pour Node et un navigateur Chromium (Edge par défaut).
NODE_PATH et MYSCHOOL_TEST_BROWSER peuvent indiquer leurs emplacements locaux.
Aucune donnée de l'installation ni connexion externe n'est utilisée.
"""
from contextlib import ExitStack
import json
import logging
import os
from pathlib import Path
import socket
from socketserver import ThreadingMixIn
import subprocess
import sys
import tempfile
import threading
from unittest.mock import patch
from wsgiref.simple_server import make_server, WSGIServer, WSGIRequestHandler

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['DJANGO_SETTINGS_MODULE'] = 'ecole_moderne.settings'
os.environ['TWILIO_ENABLED'] = 'false'


class ThreadedServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


class QuietHandler(WSGIRequestHandler):
    def log_message(self, *_args):
        pass


with tempfile.TemporaryDirectory(prefix='myschool-annee-') as scratch, ExitStack() as cleanup:
    cleanup.callback(logging.shutdown)
    from django.conf import settings
    settings.DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3',
        'NAME': str(Path(scratch, 'application.sqlite3')),
        'TEST': {'NAME': str(Path(scratch, 'test.sqlite3')), 'MIGRATE': False}}}
    settings.DEBUG = True
    settings.ALLOWED_HOSTS = ['127.0.0.1', 'localhost', 'testserver']
    settings.SECURE_SSL_REDIRECT = False
    settings.SESSION_COOKIE_SECURE = False
    settings.CSRF_COOKIE_SECURE = False
    settings.STATIC_ROOT = ROOT / 'static'
    settings.MEDIA_ROOT = str(Path(scratch, 'media'))
    settings.EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'
    settings.PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']
    settings.CACHES = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}
    settings.MIDDLEWARE = [m for m in settings.MIDDLEWARE if 'licence_middleware' not in m]
    for handler in settings.LOGGING.get('handlers', {}).values():
        if 'filename' in handler:
            handler['filename'] = str(Path(scratch, Path(handler['filename']).name))
    original_connect = socket.socket.connect

    def local_only(sock, address):
        if not isinstance(address, tuple) or address[0] not in {'127.0.0.1', '::1', 'localhost'}:
            raise OSError('Réseau externe désactivé pour cette vérification')
        return original_connect(sock, address)

    cleanup.enter_context(patch.object(socket.socket, 'connect', local_only))
    import django
    django.setup()
    from django.contrib.auth import get_user_model
    from django.core.wsgi import get_wsgi_application
    from django.test import Client
    from django.test.runner import DiscoverRunner
    from eleves.models import Classe, Ecole, Eleve, GrilleTarifaire, Responsable
    from eleves.utils_annee import SESSION_ANNEE_ACTIVE
    from paiements.models import EcheancierPaiement
    from bus.models import AbonnementBus, AbonnementCantine, GrilleTarifaireBus
    from datetime import date
    runner = DiscoverRunner(verbosity=0, interactive=False)
    databases = runner.setup_databases()
    try:
        ecole = Ecole.objects.create(nom='École parcours navigateur', adresse='Conakry',
            telephone='600000000', directeur='Direction', etat='VALIDE')
        first = Classe.objects.create(ecole=ecole, nom='1ÈRE ANNÉE', niveau='PRIMAIRE_1', annee_scolaire='2025-2026')
        Classe.objects.create(ecole=ecole, nom='2ÈME ANNÉE', niveau='PRIMAIRE_2', annee_scolaire='2025-2026')
        for niveau in ('PRIMAIRE_1', 'PRIMAIRE_2'):
            GrilleTarifaire.objects.create(ecole=ecole, niveau=niveau, annee_scolaire='2025-2026',
                frais_inscription=100000, frais_reinscription=50000,
                tranche_1=300000, tranche_2=200000, tranche_3=100000)
        for index in range(3):
            Eleve.objects.create(classe=first, matricule=f'NAV-{index}', nom=f'Élève {index}', prenom='Test')
        eleve_test = Eleve.objects.filter(classe=first).order_by('pk').first()
        parent_test = Responsable.objects.create(prenom='Parent', nom='Navigateur', relation='PERE',
            telephone='+224620111222', adresse='Conakry')
        eleve_test.responsable_principal = parent_test
        eleve_test.save(update_fields=['responsable_principal'])
        for annee, debut, fin, suffixe in (
            ('2025-2026', date(2025, 10, 1), date(2026, 6, 30), 'ANCIEN'),
            ('2026-2027', date(2026, 10, 1), date(2027, 6, 30), 'NOUVEAU'),
        ):
            grille_bus = GrilleTarifaireBus.objects.create(ecole=ecole, zone='Kipé', annee_scolaire=annee,
                tranche_1=100000, tranche_2=90000, tranche_3=80000)
            AbonnementBus.objects.create(eleve=eleve_test, grille=grille_bus, montant=50000,
                periodicite='T1', date_debut=debut, date_expiration=fin, numero_recu=f'BUS-{suffixe}')
            AbonnementCantine.objects.create(eleve=eleve_test, montant=90000, montant_tarif=120000,
                type_remise='POURCENTAGE', valeur_remise=10, date_debut=debut,
                date_expiration=fin, reference_externe=f'CANTINE-{suffixe}')
        admin = get_user_model().objects.create_superuser('annee-navigateur', '', 'test')
        profil = admin.profil
        profil.ecole, profil.role, profil.telephone = ecole, 'ADMIN', ''
        profil.save()
        client = Client()
        client.force_login(admin)
        session = client.session
        session[SESSION_ANNEE_ACTIVE] = '2025-2026'
        session.save()
        cleanup.enter_context(patch('eleves.views_nouvelle_annee.TAILLE_LOT_PASSAGE', 1))
        application = get_wsgi_application()
        with make_server('127.0.0.1', 0, application, server_class=ThreadedServer, handler_class=QuietHandler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            setup = Path(scratch, 'navigateur.json')
            setup.write_text(json.dumps({'url': f'http://127.0.0.1:{server.server_port}',
                'eleve_id': eleve_test.pk,
                'grille_bus_id': grille_bus.pk,
                'eleve_sans_abonnement': Eleve.objects.filter(classe=first).exclude(pk=eleve_test.pk).first().pk,
                'cookie_name': settings.SESSION_COOKIE_NAME, 'cookie_value': client.cookies[settings.SESSION_COOKIE_NAME].value}), encoding='utf-8')
            try:
                subprocess.run(['node', str(ROOT / 'scripts/test_nouvelle_annee_navigateur.cjs'), str(setup)], check=True, timeout=90, cwd=ROOT)
            finally:
                server.shutdown()
                thread.join(5)
        assert Classe.objects.filter(ecole=ecole, annee_scolaire='2026-2027').count() == 2
        assert Eleve.objects.filter(classe__ecole=ecole, classe__annee_scolaire='2026-2027', classe__nom='2ÈME ANNÉE').count() == 3
        assert EcheancierPaiement.objects.filter(eleve__classe__ecole=ecole, annee_scolaire='2026-2027').count() == 3
        parent_test.refresh_from_db()
        assert parent_test.telephone_secondaire == ''
        print('ORM : nouvelle année créée, 2 classes, 3 passages, 3 échéanciers ; base client non utilisée.')
    finally:
        runner.teardown_databases(databases)
