"""Les chemins, octets et reprises sont vérifiés avec de vrais stockages isolés."""
import copy
from contextlib import contextmanager
import hashlib
import io
from pathlib import Path
import tempfile
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

import requests
from PIL import Image
from django.core.files.base import ContentFile
from django.db import connections
from django.db.backends.sqlite3.base import DatabaseWrapper
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from eleves.models import Classe, Ecole, Eleve
from paiements.tests.support import MIDDLEWARE_SANS_LICENCE
from . import media_client
from .client import pull_changes, push_pending
from .context import mute_sync
from .engine import build_change_instance
from .media import (MAX_FILE_BYTES, canonical_name, enqueue, pending_name,
                    queue_received, repair_page)
from .models import SyncChange, SyncCheckpoint, SyncDevice, SyncMediaTransfer
from .realtime import revision_ecole
from .registry import REGISTRE_VERSION


def png(color='red'):
    output = io.BytesIO()
    Image.new('RGB', (24, 24), color).save(output, format='PNG')
    return output.getvalue()


def school(name='École fichiers'):
    return Ecole.objects.create(nom=name, adresse='Conakry', telephone='600000000',
                                directeur='Direction', etat='VALIDE')


@override_settings(MIDDLEWARE=MIDDLEWARE_SANS_LICENCE)
class MediaApiTests(TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.storage = override_settings(MEDIA_ROOT=self.scratch.name)
        self.storage.enable()
        self.addCleanup(self.storage.disable)
        self.ecole = school()
        self.device = SyncDevice(ecole=self.ecole, nom='Poste images')
        self.device.definir_token('test-images')
        self.device.save()
        self.headers = {'HTTP_X_SYNC_DEVICE': str(self.device.device_id), 'HTTP_X_SYNC_TOKEN': 'test-images'}
        self.ecole.logo.save('logo.png', ContentFile(png()))
        self.url = reverse('synchronisation:media')
        self.params = {'model': 'eleves.Ecole', 'object_uuid': str(self.ecole.sync_uuid),
                       'field': 'logo', 'name': self.ecole.logo.name}

    def upload(self, content, params=None, **extra):
        from urllib.parse import urlencode
        params = {**self.params, 'sha256': hashlib.sha256(content).hexdigest(), **(params or {})}
        return self.client.post(self.url + '?' + urlencode(params), data=content,
            content_type='application/octet-stream', **{**self.headers, **extra})

    def test_le_fichier_binaire_est_transmis_avec_son_empreinte(self):
        response = self.client.get(self.url, self.params, **self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b''.join(response.streaming_content), png())
        self.assertEqual(response['X-Sync-Media-SHA256'], hashlib.sha256(png()).hexdigest())
        self.assertIn('no-store', response['Cache-Control'])
        response.close()

    def test_jeton_obligatoire_appareil_actif_et_ecole_limitee(self):
        self.assertEqual(self.client.get(self.url, self.params).status_code, 401)
        self.assertEqual(self.client.get(self.url, self.params,
            **{**self.headers, 'HTTP_X_SYNC_TOKEN': 'incorrect'}).status_code, 403)
        other = school('Autre école')
        other.logo.save('confidentiel.png', ContentFile(png('blue')))
        foreign = {**self.params, 'object_uuid': str(other.sync_uuid), 'name': other.logo.name}
        self.assertEqual(self.client.get(self.url, foreign, **self.headers).status_code, 404)
        self.assertEqual(self.upload(png(), foreign).status_code, 404)
        self.device.actif = False
        self.device.save()
        self.assertEqual(self.client.get(self.url, self.params, **self.headers).status_code, 403)

    def test_champ_texte_chemin_absolu_traversee_et_modele_externe_refuses(self):
        for change in ({'field': 'nom'}, {'name': '../db.sqlite3'}, {'name': 'C:/secrets.png'},
                       {'name': 'ecoles/../autre.png'}, {'model': 'auth.User'}):
            self.assertEqual(self.client.get(self.url, {**self.params, **change}, **self.headers).status_code, 400)

    def test_un_poste_ne_peut_pas_designer_le_fichier_d_une_autre_ecole(self):
        foreign = school('Autre école')
        foreign.logo.save('secret.png', ContentFile(png('blue')))
        obj = build_change_instance('eleves.Ecole', self.ecole.sync_uuid,
            {'logo': foreign.logo.name}, ecole=self.ecole, trusted=False)
        self.assertNotEqual(obj.logo.name, foreign.logo.name)
        response = self.client.get(self.url, {**self.params, 'name': obj.logo.name}, **self.headers)
        self.assertEqual(response.status_code, 404)

    def test_envoi_repetable_cree_un_fichier_et_diffuse_sa_reference(self):
        content, name = png('green'), 'ecoles/logos/nouveau.png'
        build_change_instance('eleves.Ecole', self.ecole.sync_uuid,
            {'logo': name}, ecole=self.ecole, trusted=False)
        SyncChange.objects.all().delete()
        response = self.upload(content, {'name': name})
        self.assertEqual(response.status_code, 200, response.content)
        target = canonical_name(self.ecole, hashlib.sha256(content).hexdigest(), name)
        self.ecole.refresh_from_db()
        self.assertEqual(self.ecole.logo.name, target)
        self.assertEqual(Path(self.ecole.logo.path).read_bytes(), content)
        self.assertEqual(SyncChange.objects.filter(payload__logo=target).count(), 1)
        self.assertEqual(self.upload(content, {'name': name}).status_code, 200)
        self.assertEqual(SyncChange.objects.filter(payload__logo=target).count(), 1)

    def test_ancien_envoi_ne_remplace_pas_un_logo_plus_recent(self):
        old = dict(self.params)
        self.ecole.logo.save('recent.png', ContentFile(png('blue')))
        self.assertEqual(self.upload(png('green'), old).status_code, 409)
        self.ecole.refresh_from_db()
        self.assertEqual(Path(self.ecole.logo.path).read_bytes(), png('blue'))

    def test_reparer_les_octets_manquants_actualise_aussi_la_page_serveur(self):
        name, content = 'ecoles/logos/repare.png', png('green')
        build_change_instance('eleves.Ecole', self.ecole.sync_uuid, {'logo': name}, ecole=self.ecole)
        self.assertEqual(self.upload(content, {'name': name}).status_code, 200)
        self.ecole.refresh_from_db()
        Path(self.ecole.logo.path).unlink()
        before = revision_ecole(self.ecole)
        self.assertEqual(self.upload(content, {'name': name}).status_code, 200)
        self.assertNotEqual(revision_ecole(self.ecole), before)
        self.assertEqual(Path(self.ecole.logo.path).read_bytes(), content)

    def test_fichier_incomplet_image_invalide_et_taille_excessive_refuses(self):
        name = 'ecoles/logos/nouveau.png'
        build_change_instance('eleves.Ecole', self.ecole.sync_uuid, {'logo': name}, ecole=self.ecole)
        self.assertEqual(self.upload(png(), {'name': name, 'sha256': '0' * 64}).status_code, 400)
        self.assertEqual(self.upload(b'pas une image', {'name': name}).status_code, 400)
        response = self.upload(png(), {'name': name}, CONTENT_LENGTH=str(MAX_FILE_BYTES + 1))
        self.assertEqual(response.status_code, 413)
        self.assertFalse(Path(self.scratch.name, pending_name(self.ecole, self.ecole.sync_uuid, 'logo', name)).exists())

    def test_etat_binaire_ne_telecharge_pas_le_fichier(self):
        response = self.client.get(self.url, {**self.params, 'check': '1'}, **self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['sha256'], hashlib.sha256(png()).hexdigest())
        self.assertFalse(response.streaming)


class MediaQueueTests(TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        storage = override_settings(MEDIA_ROOT=self.scratch.name)
        storage.enable()
        self.addCleanup(storage.disable)
        self.ecole = school()

    def test_rattrapage_des_images_absentes_sans_reinitialiser_le_curseur(self):
        with mute_sync():
            self.ecole.logo = 'ecoles/logos/ancien.png'
            self.ecole.save()
        checkpoint = SyncCheckpoint.objects.create(device_id=uuid4(), last_change_id=1500, initial_complete=True)
        repair_page(self.ecole, {})
        job = SyncMediaTransfer.objects.get()
        self.assertEqual((job.direction, job.name), ('PULL', self.ecole.logo.name))
        checkpoint.refresh_from_db()
        self.assertEqual(checkpoint.last_change_id, 1500)

    def test_file_idempotente_et_nouvelle_image_reactive_le_transfert(self):
        for _ in range(2):
            enqueue(self.ecole, 'eleves.Ecole', self.ecole.sync_uuid, 'logo', 'ecoles/logo.png', 'PULL')
        self.assertEqual(SyncMediaTransfer.objects.count(), 1)
        SyncMediaTransfer.objects.update(complete=True, error='ancienne erreur')
        enqueue(self.ecole, 'eleves.Ecole', self.ecole.sync_uuid, 'logo', 'ecoles/nouveau.png', 'PULL')
        job = SyncMediaTransfer.objects.get()
        self.assertFalse(job.complete)
        self.assertEqual(job.error, '')

    def test_image_presente_ne_se_retelecharge_pas_a_chaque_changement(self):
        self.ecole.logo.save('present.png', ContentFile(png()))
        queue_received(self.ecole, 'eleves.Ecole', self.ecole.sync_uuid, {'logo': self.ecole.logo.name})
        self.assertFalse(SyncMediaTransfer.objects.exists())

    def test_echec_reprogramme_le_fichier_sans_bloquer_les_autres(self):
        self.ecole.logo = 'ecoles/logo.png'
        self.ecole.image = 'ecoles/batiment.png'
        with mute_sync():
            self.ecole.save()
        repair_page(self.ecole, {})
        config = {'server_url': 'https://sync.invalid', 'device_id': str(uuid4()), 'token': 'test'}
        with patch('synchronisation.media_client.repair_page', return_value=True), \
                patch('synchronisation.media_client.download', side_effect=[requests.ConnectionError('coupure'), None]):
            self.assertEqual(media_client.sync_media(config, self.ecole), (0, 1))
        pending = SyncMediaTransfer.objects.get(complete=False)
        self.assertEqual(pending.attempts, 1)
        self.assertGreater(pending.retry_at, timezone.now())

    def test_donnees_receptionnees_restant_durables_si_un_fichier_echoue(self):
        from .engine import apply_sync_change
        change = SyncChange.objects.create(ecole=self.ecole, model_label='eleves.Ecole',
            object_uuid=self.ecole.sync_uuid, operation='UPDATE',
            payload={'nom': 'Nom reçu', 'logo': 'ecoles/logos/manquant.png'})
        apply_sync_change(change)
        change.refresh_from_db()
        self.ecole.refresh_from_db()
        self.assertEqual(change.statut, 'APPLIED')
        self.assertEqual(self.ecole.nom, 'NOM REÇU')
        self.assertFalse(SyncMediaTransfer.objects.get().complete)

    def test_un_telechargement_incomplet_conserve_l_image_precedente(self):
        self.ecole.logo.save('logo.png', ContentFile(png()))
        enqueue(self.ecole, 'eleves.Ecole', self.ecole.sync_uuid, 'logo', self.ecole.logo.name, 'PULL')
        response = requests.Response()
        response.status_code = 200
        response.headers['X-Sync-Media-SHA256'] = hashlib.sha256(png('blue')).hexdigest()
        response._content = png('blue')[:20]
        response._content_consumed = True
        config = {'server_url': 'https://sync.invalid', 'device_id': str(uuid4()), 'token': 'test'}
        before = revision_ecole(self.ecole)
        with patch('synchronisation.media_client._get_session') as session:
            session.return_value.get.return_value = response
            with self.assertRaisesRegex(Exception, 'incomplète'):
                media_client.download(config, SyncMediaTransfer.objects.get())
        self.assertEqual(Path(self.ecole.logo.path).read_bytes(), png())
        self.assertEqual(revision_ecole(self.ecole), before)

    def test_un_nouveau_logo_pendant_le_transfert_ne_se_fait_pas_ecraser(self):
        with mute_sync():
            self.ecole.logo = 'ecoles/logos/ancien.png'
            self.ecole.save()
        enqueue(self.ecole, 'eleves.Ecole', self.ecole.sync_uuid, 'logo', self.ecole.logo.name, 'PULL')
        response = requests.Response()
        response.status_code = 200
        response.headers['X-Sync-Media-SHA256'] = hashlib.sha256(png()).hexdigest()
        response._content_consumed = True

        def chunks(_size):
            self.ecole.logo.save('recent.png', ContentFile(png('blue')))
            yield png()

        response.iter_content = chunks
        with patch('synchronisation.media_client._get_session') as session:
            session.return_value.get.return_value = response
            with self.assertRaises(media_client.ObsoleteMedia):
                media_client.download({'server_url': 'https://sync.invalid', 'device_id': 'test', 'token': 'test'},
                                      SyncMediaTransfer.objects.get())
        self.ecole.refresh_from_db()
        self.assertEqual(Path(self.ecole.logo.path).read_bytes(), png('blue'))
        self.assertFalse(Path(self.scratch.name, 'ecoles/logos/ancien.png').exists())

    def test_acquittement_et_file_binaire_sont_atomiques(self):
        self.ecole.logo.save('envoi.png', ContentFile(png()))
        SyncChange.objects.exclude(payload__logo=self.ecole.logo.name).delete()
        before = list(SyncChange.objects.values_list('pk', flat=True))
        with patch('synchronisation.client._post_json', return_value={
                'ok': True, 'accepted': [{'index': 0}], 'rejected': []}), \
                patch('synchronisation.media.queue_sent', side_effect=RuntimeError('interruption')):
            with self.assertRaises(RuntimeError):
                push_pending('https://sync.invalid', 'test', 'token', self.ecole)
        self.assertEqual(SyncChange.objects.filter(pk__in=before, statut='PENDING').count(), len(before))

    def test_traitement_pull_only_n_envoie_aucun_fichier(self):
        enqueue(self.ecole, 'eleves.Ecole', self.ecole.sync_uuid, 'logo', 'ecoles/logo.png', 'PUSH')
        config = {'server_url': 'https://sync.invalid', 'device_id': str(uuid4()), 'token': 'test'}
        with patch('synchronisation.media_client.repair_page', return_value=True), \
                patch('synchronisation.media_client.upload') as upload:
            self.assertEqual(media_client.sync_media(config, self.ecole, direction='PULL'), (0, 0))
        upload.assert_not_called()

    def test_page_connexion_signale_separement_les_fichiers_en_attente(self):
        from .views_poste import _etat_synchronisation
        enqueue(self.ecole, 'eleves.Ecole', self.ecole.sync_uuid, 'logo', 'ecoles/logo.png', 'PULL')
        SyncMediaTransfer.objects.update(error='Image indisponible')
        state = _etat_synchronisation({'server_url': 'https://sync.invalid', 'device_id': str(uuid4()),
                                      'token': 'test', 'ecole_id': str(self.ecole.pk)})
        self.assertEqual(state['fichiers_en_attente'], 1)
        self.assertEqual(state['fichiers_a_reessayer'], 1)

    def test_le_logo_n_attend_pas_le_rattrapage_des_portraits(self):
        SyncMediaTransfer.objects.bulk_create([
            SyncMediaTransfer(ecole=self.ecole, model_label='eleves.Eleve', object_uuid=uuid4(),
                field_name='photo', direction='PUSH', name=f'eleves/photos/{i}.png') for i in range(100)
        ])
        enqueue(self.ecole, 'eleves.Ecole', self.ecole.sync_uuid, 'logo', 'ecoles/logo.png', 'PULL')
        config = {'server_url': 'https://sync.invalid', 'device_id': str(uuid4()), 'token': 'test'}
        with patch('synchronisation.media_client.repair_page', return_value=True), \
                patch('synchronisation.media_client.download') as download, \
                patch('synchronisation.media_client.upload') as upload:
            self.assertEqual(media_client.sync_media(config, self.ecole, limit=1), (0, 1))
        self.assertEqual(download.call_args.args[1].field_name, 'logo')
        upload.assert_not_called()


@override_settings(MIDDLEWARE=MIDDLEWARE_SANS_LICENCE)
class MediaPropagationTests(TransactionTestCase):
    def test_serveur_et_deux_postes_bases_et_dossiers_media_independants(self):
        server = connections['default']
        ecole = school()
        classe = Classe.objects.create(ecole=ecole, nom='CP', niveau='PRIMAIRE_1', annee_scolaire='2026-2027')
        pupil = Eleve.objects.create(classe=classe, nom='Photo', prenom='Test', matricule='PHOTO-001')
        devices = {}
        for name in ('A', 'B'):
            device = SyncDevice(ecole=ecole, nom=name)
            device.definir_token('test-' + name)
            device.save()
            devices[name] = device
        SyncChange.objects.all().delete()
        with tempfile.TemporaryDirectory() as scratch:
            bases, folders = {'server': server}, {}
            for name in ('server', 'A', 'B'):
                folders[name] = str(Path(scratch, name, 'media'))
            try:
                for name in ('A', 'B'):
                    config = copy.deepcopy(server.settings_dict)
                    config['NAME'] = str(Path(scratch, name + '.sqlite3'))
                    base = DatabaseWrapper(config, alias='default')
                    base.ensure_connection()
                    server.connection.backup(base.connection)
                    bases[name] = base

                @contextmanager
                def on(name):
                    previous = connections['default']
                    connections['default'] = bases[name]
                    try:
                        with override_settings(MEDIA_ROOT=folders[name]):
                            yield
                    finally:
                        connections['default'] = previous

                class LocalSession:
                    def get(inner, url, params=None, headers=None, **kwargs):
                        return inner.send('get', url, params, headers)

                    def post(inner, url, params=None, headers=None, data=None, **kwargs):
                        return inner.send('post', url, params, headers, data.read())

                    def send(inner, method, url, params, headers, data=None):
                        from urllib.parse import urlencode
                        with on('server'):
                            client = Client()
                            auth = {'HTTP_X_SYNC_DEVICE': headers['X-Sync-Device'], 'HTTP_X_SYNC_TOKEN': headers['X-Sync-Token']}
                            path = urlsplit(url).path
                            response = (client.get(path, params, **auth) if method == 'get' else
                                client.post(path + '?' + urlencode(params), data=data,
                                            content_type='application/octet-stream', **auth))
                            result = requests.Response()
                            result.status_code = response.status_code
                            result.headers.update(response.headers)
                            result._content = b''.join(response.streaming_content) if response.streaming else response.content
                            result._content_consumed = True
                            response.close()
                            return result

                def json_transport(url, device_id, token, params=None, payload=None, **kwargs):
                    with on('server'):
                        client = Client()
                        auth = {'HTTP_X_SYNC_DEVICE': str(device_id), 'HTTP_X_SYNC_TOKEN': token}
                        response = (client.get(urlsplit(url).path, params or {}, **auth) if payload is None else
                            client.post(urlsplit(url).path, data=payload, content_type='application/json', **auth))
                        self.assertEqual(response.status_code, 200, response.content)
                        return response.json()

                def post_transport(url, device_id, token, payload, **kwargs):
                    return json_transport(url, device_id, token, payload=payload)

                def config(name):
                    return {'server_url': 'https://sync.invalid', 'device_id': str(devices[name].device_id), 'token': 'test-' + name}

                def receive(name):
                    with on(name):
                        return pull_changes(**{k: config(name)[k] for k in ('server_url', 'device_id', 'token')}, ecole=ecole)

                def files(name):
                    with on(name):
                        for _ in range(8):
                            media_client.sync_media(config(name), ecole)

                media_client._repair.clear()
                with patch('synchronisation.client._get_json', json_transport), \
                        patch('synchronisation.client._post_json', post_transport), \
                        patch('synchronisation.media_client._get_session', return_value=LocalSession()):
                    # Un logo changé sur le serveur arrive sur les deux postes.
                    with on('server'):
                        ecole.logo.save('serveur.png', ContentFile(png()))
                        ecole.image.save('batiment.png', ContentFile(png('yellow')))
                    for name in ('A', 'B'):
                        receive(name)
                        with on(name):
                            before = revision_ecole(ecole)
                            self.assertFalse(Path(Ecole.objects.get(pk=ecole.pk).logo.path).exists())
                        files(name)
                        with on(name):
                            self.assertEqual(Path(Ecole.objects.get(pk=ecole.pk).logo.path).read_bytes(), png())
                            self.assertEqual(Path(Ecole.objects.get(pk=ecole.pk).image.path).read_bytes(), png('yellow'))
                            self.assertNotEqual(revision_ecole(ecole), before)
                    # Une ancienne synchronisation avait envoyé le chemin, mais pas les octets.
                    with on('server'):
                        Path(Ecole.objects.get(pk=ecole.pk).logo.path).unlink()
                    media_client._repair.clear()  # Premier lancement après mise à jour du poste.
                    files('A')
                    with on('server'):
                        self.assertEqual(Path(Ecole.objects.get(pk=ecole.pk).logo.path).read_bytes(), png())
                    with on('A'):
                        self.assertTrue(SyncCheckpoint.objects.get(device_id=devices['A'].device_id).initial_complete)
                    receive('B')
                    files('B')
                    with on('B'):
                        self.assertEqual(Path(Ecole.objects.get(pk=ecole.pk).logo.path).read_bytes(), png())
                    # Nouveau logo + photo saisis hors ligne A -> serveur -> B.
                    with on('A'):
                        local = Ecole.objects.get(pk=ecole.pk)
                        local.logo.save('poste.png', ContentFile(png('green')))
                        local_pupil = Eleve.objects.get(pk=pupil.pk)
                        local_pupil.photo.save('portrait.png', ContentFile(png('blue')))
                        push_pending(**config('A'), ecole=ecole)
                    files('A')
                    with on('server'):
                        self.assertEqual(Path(Ecole.objects.get(pk=ecole.pk).logo.path).read_bytes(), png('green'))
                        self.assertEqual(Path(Eleve.objects.get(pk=pupil.pk).photo.path).read_bytes(), png('blue'))
                    receive('B')
                    files('B')
                    with on('B'):
                        self.assertEqual(Path(Ecole.objects.get(pk=ecole.pk).logo.path).read_bytes(), png('green'))
                        self.assertEqual(Path(Eleve.objects.get(pk=pupil.pk).photo.path).read_bytes(), png('blue'))
                    # Aucun renvoi des changements reçus, ni image inutile à reprendre.
                    receive('A')
                    for name in ('A', 'B'):
                        with on(name):
                            self.assertEqual(push_pending(**config(name), ecole=ecole), 0)
                            self.assertFalse(SyncMediaTransfer.objects.filter(complete=False).exists())
            finally:
                for name, base in bases.items():
                    if name != 'server':
                        base.close()
                media_client._repair.clear()
