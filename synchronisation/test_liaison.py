"""Liaison d'un poste éloigné par code à usage unique."""
import json
import os
import re
import tempfile
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from eleves.models import Ecole
from paiements.tests.support import MIDDLEWARE_SANS_LICENCE
from utilisateurs.models import Profil

from .autosync import _pret, charger_config
from .liaison import LiaisonImpossible, lier_poste, script_connexion_poste, verifier_ecole_compatible
from .models import SyncCheckpoint, SyncDevice

SERVEUR = 'https://www.myschoolgn.space'


class _ReponseTest:
    """Réponse ``requests`` construite depuis le client de test Django."""

    def __init__(self, response):
        self.status_code = response.status_code
        self._contenu = response.content

    def json(self):
        return json.loads(self._contenu)


@override_settings(MIDDLEWARE=MIDDLEWARE_SANS_LICENCE)
class LiaisonPosteTests(TestCase):
    def setUp(self):
        cache.clear()
        self.ecole = Ecole.objects.create(
            nom='Ecole Liaison', adresse='Conakry', telephone='+224600000020', directeur='Direction',
        )
        self.admin = User.objects.create_user(username='admin_liaison', password='secret123')
        Profil.objects.update_or_create(user=self.admin, defaults={
            'role': 'ADMIN', 'ecole': self.ecole, 'is_validated': True, 'telephone': '+224600000021',
        })
        self.admin = User.objects.get(pk=self.admin.pk)
        self.client.force_login(self.admin)
        self.dossier = tempfile.TemporaryDirectory()
        self.addCleanup(self.dossier.cleanup)
        env = patch.dict(os.environ, {'MYSCHOOL_BASE_DIR': self.dossier.name, 'OFFLINE_MODE': '1'})
        env.start()
        self.addCleanup(env.stop)

    def generer(self, nom='Poste Kindia'):
        response = self.client.post(reverse('synchronisation:device_setup'), {
            'ecole_id': self.ecole.pk, 'nom': nom,
        }, secure=True)
        self.assertEqual(response.status_code, 200)
        return response.context['generated']

    def faux_requests(self, methode, url, timeout=None, params=None, json=None):
        """Le poste et le serveur partagent ici la même base : le serveur est le client de test."""
        chemin = url.replace(SERVEUR, '')
        if methode == 'GET':
            return _ReponseTest(self.client.get(chemin, params or {}))
        return _ReponseTest(self.client.post(chemin, data=json or {}, content_type='application/json'))

    # --- Site en ligne ------------------------------------------------------

    def test_generation_du_code_et_du_fichier_de_connexion(self):
        genere = self.generer()
        code = genere['code_liaison']
        self.assertRegex(code, r'^[A-Z2-9]{4}-[A-Z2-9]{4}$')
        script = genere['script_cmd']
        self.assertIn(f'set "CODE={code}"', script)
        self.assertIn('set "SERVEUR=https://testserver"', script)
        self.assertIn('\r\n', script)
        self.assertNotIn(genere['token'], script)  # le fichier n'emporte jamais le jeton
        self.assertTrue(script.isascii())
        self.assertIn(code, genere['message_poste'])

    def test_fichier_refuse_une_adresse_dangereuse(self):
        with self.assertRaises(LiaisonImpossible):
            script_connexion_poste('https://exemple.com" & calc & "', 'ABCD-EFGH')
        with self.assertRaises(LiaisonImpossible):
            script_connexion_poste('http://exemple.com', 'ABCD-EFGH')  # http seulement en local

    def test_code_a_usage_unique_et_nouveau_jeton(self):
        genere = self.generer()
        code = genere['code_liaison']
        apercu = self.client.get(reverse('synchronisation:liaison'), {'code': code.lower()}).json()
        self.assertEqual((apercu['ecole_nom'], apercu['poste_nom']), (self.ecole.nom, 'Poste Kindia'))

        reponse = self.client.post(reverse('synchronisation:liaison'), data={'code': code},
                                   content_type='application/json').json()
        poste = SyncDevice.objects.get(device_id=reponse['config']['MYSCHOOL_SYNC_DEVICE_ID'])
        self.assertTrue(poste.verifier_token(reponse['config']['MYSCHOOL_SYNC_TOKEN']))
        self.assertFalse(poste.verifier_token(genere['token']))  # l'ancien jeton est remplacé
        self.assertIsNotNone(poste.lie_le)

        deuxieme = self.client.post(reverse('synchronisation:liaison'), data={'code': code},
                                    content_type='application/json')
        self.assertEqual(deuxieme.status_code, 404)

    def test_code_expire_refuse(self):
        code = self.generer()['code_liaison']
        SyncDevice.objects.update(code_liaison_expire=timezone.now() - timedelta(minutes=1))
        response = self.client.get(reverse('synchronisation:liaison'), {'code': code})
        self.assertEqual(response.status_code, 404)

    def test_essais_repetes_bloques(self):
        for _ in range(10):
            self.assertEqual(self.client.get(reverse('synchronisation:liaison'), {'code': 'AAAA-AAAA'}).status_code, 404)
        code = self.generer()['code_liaison']
        self.assertEqual(self.client.get(reverse('synchronisation:liaison'), {'code': code}).status_code, 429)

    # --- Poste ---------------------------------------------------------------

    def test_liaison_complete_ecrit_la_configuration(self):
        code = self.generer()['code_liaison']
        with patch('requests.request', self.faux_requests):
            resultat = lier_poste(SERVEUR + '/', code)
        self.assertEqual(resultat['ecole_nom'], self.ecole.nom)

        chemin = os.path.join(self.dossier.name, 'sync_config.json')
        config = json.load(open(chemin, encoding='utf-8'))
        self.assertEqual(config['MYSCHOOL_SYNC_SERVER_URL'], SERVEUR)
        self.assertEqual(config['MYSCHOOL_SYNC_INTERVAL'], '4')
        poste = SyncDevice.objects.get(device_id=config['MYSCHOOL_SYNC_DEVICE_ID'])
        self.assertTrue(poste.verifier_token(config['MYSCHOOL_SYNC_TOKEN']))
        lu = charger_config(self.dossier.name)
        self.assertTrue(_pret(lu))
        self.assertEqual(lu['server_url'], SERVEUR)

    def test_poste_deja_synchronise_reprend_son_avancement(self):
        ancien = str(uuid4())
        with open(os.path.join(self.dossier.name, 'sync_config.json'), 'w', encoding='utf-8') as f:
            json.dump({'MYSCHOOL_SYNC_SERVER_URL': SERVEUR, 'MYSCHOOL_SYNC_DEVICE_ID': ancien,
                       'MYSCHOOL_SYNC_TOKEN': 'x', 'MYSCHOOL_SYNC_ECOLE_ID': str(self.ecole.pk)}, f)
        SyncCheckpoint.objects.create(device_id=ancien, initial_complete=True, last_change_id=812, registre=2)
        code = self.generer()['code_liaison']
        with patch('requests.request', self.faux_requests):
            lier_poste(SERVEUR, code)
        nouveau = json.load(open(os.path.join(self.dossier.name, 'sync_config.json'), encoding='utf-8'))
        point = SyncCheckpoint.objects.get(device_id=nouveau['MYSCHOOL_SYNC_DEVICE_ID'])
        self.assertEqual((point.initial_complete, point.last_change_id, point.registre), (True, 812, 2))
        self.assertTrue(os.path.exists(os.path.join(self.dossier.name, 'sync_config.json.ancien')))

    def test_poste_d_une_autre_ecole_refuse(self):
        with self.assertRaises(LiaisonImpossible):
            verifier_ecole_compatible(str(uuid4()))
        code = self.generer()['code_liaison']
        with patch('synchronisation.liaison.verifier_ecole_compatible',
                   side_effect=LiaisonImpossible('autre école')), \
                patch('requests.request', self.faux_requests):
            with self.assertRaises(LiaisonImpossible):
                lier_poste(SERVEUR, code)
        # Refus avant consommation : le code reste utilisable.
        self.assertEqual(self.client.get(reverse('synchronisation:liaison'), {'code': code}).status_code, 200)
        self.assertFalse(os.path.exists(os.path.join(self.dossier.name, 'sync_config.json')))

    def test_page_du_poste(self):
        code = self.generer()['code_liaison']
        with patch('requests.request', self.faux_requests):
            page = self.client.get(reverse('synchronisation_connexion_serveur'), {'code': code, 'serveur': SERVEUR})
            self.assertContains(page, 'Connecter ce poste')
            self.assertContains(page, self.ecole.nom)
            reponse = self.client.post(reverse('synchronisation_connexion_serveur'), {'code': code, 'serveur': SERVEUR})
        self.assertRedirects(reponse, reverse('synchronisation_connexion_serveur'))
        etat = self.client.get(reverse('synchronisation_connexion_serveur'))
        self.assertContains(etat, 'Active')
        self.assertContains(etat, 'Connexion au serveur')  # lien du menu de l'application

    def test_page_du_poste_reservee_aux_administrateurs_et_au_poste(self):
        simple = User.objects.create_user(username='comptable_liaison', password='secret123')
        Profil.objects.update_or_create(user=simple, defaults={
            'role': 'COMPTABLE', 'ecole': self.ecole, 'is_validated': True, 'telephone': '+224600000022',
        })
        self.client.force_login(User.objects.get(pk=simple.pk))
        self.assertEqual(self.client.get(reverse('synchronisation_connexion_serveur')).status_code, 403)
        self.client.force_login(self.admin)
        with patch.dict(os.environ, {'OFFLINE_MODE': '0'}), override_settings(DEBUG=False):
            self.assertEqual(self.client.get(reverse('synchronisation_connexion_serveur')).status_code, 403)
