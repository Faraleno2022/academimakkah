"""Régressions sur les gros lots, les verrous et la reprise des réceptions."""
from unittest.mock import Mock, patch
from uuid import uuid4
import json
import time

from django.db import connection, transaction
from django.test import TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from eleves.models import Classe, Ecole, Eleve
from .client import (MARQUEUR_RECU, SnapshotRestartRequired, _appliquer_lot, _parse_response,
                     pull_changes, reessayer_recus_en_echec)
from .context import mute_sync
from .engine import apply_sync_change, emettre_existants, snapshot_page_for_ecole
from .models import SyncChange, SyncCheckpoint, SyncDevice, SyncSequence
from .registry import REGISTRE_VERSION
from .sequence import attribuer_sequences


class BaseVolumeSynchronisation:
    def setUp(self):
        self.ecole = Ecole.objects.create(nom='École volume', adresse='Conakry',
                                         telephone='600000000', directeur='Direction', etat='VALIDE')
        self.classe = Classe.objects.create(ecole=self.ecole, nom='CP volume',
                                           niveau='PRIMAIRE_1', annee_scolaire='2026-2027')
        self.poste = SyncDevice(ecole=self.ecole, nom='Poste volume')
        self.poste.definir_token('test-volume')
        self.poste.save()
        SyncChange.objects.all().delete()

    def eleves(self, nombre):
        objets = Eleve.objects.bulk_create([
            Eleve(classe=self.classe, nom=f'Élève {i}', prenom='Volume', matricule=f'VOL-{i}')
            for i in range(nombre)
        ])
        SyncChange.objects.bulk_create([
            SyncChange(ecole=self.ecole, model_label='eleves.Eleve', object_uuid=obj.sync_uuid,
                       operation='CREATE') for obj in objets
        ])
        attribuer_sequences()
        return objets


class VolumeSynchronisationTests(BaseVolumeSynchronisation, TestCase):
    def test_reception_200_eleves_ne_fait_pas_une_requete_par_ligne(self):
        self.eleves(200)
        self.poste.verifier_token('test-volume')
        with patch('synchronisation.views._rattrapage_fait', True), CaptureQueriesContext(connection) as queries:
            response = self.client.get(reverse('synchronisation:pull'), {'registre': REGISTRE_VERSION},
                HTTP_X_SYNC_DEVICE=str(self.poste.device_id), HTTP_X_SYNC_TOKEN='test-volume')
        self.assertEqual(response.status_code, 200)
        data = response.json()['changes']
        self.assertEqual(sum(row['model_label'] == 'eleves.Eleve' for row in data), 200)
        self.assertLess(len(queries), 35, f'{len(queries)} requêtes pour 200 élèves')
        print(f'\n[Mesure requêtes / 200 élèves] {len(queries)}')

    def test_instantane_200_eleves_charge_les_relations_par_lots(self):
        self.eleves(200)
        with CaptureQueriesContext(connection) as queries:
            rows, _, _ = snapshot_page_for_ecole(self.ecole, labels={'eleves.Eleve'})
        self.assertEqual(len(rows), 200)
        self.assertLess(len(queries), 25, f'{len(queries)} requêtes pour un instantané de 200 élèves')
        print(f'\n[Mesure requêtes / instantané 200 élèves] {len(queries)}')

    def test_reessai_ancien_necrase_pas_une_saisie_locale_en_attente(self):
        ancien = SyncChange.objects.create(ecole=self.ecole, model_label='eleves.Ecole',
            object_uuid=self.ecole.sync_uuid, operation='UPDATE',
            payload={'nom': 'ANCIEN SERVEUR', MARQUEUR_RECU: True}, statut=SyncChange.STATUT_FAILED)
        self.ecole.nom = 'NOUVELLE SAISIE LOCALE'
        self.ecole.save()
        reessayer_recus_en_echec(self.ecole)
        self.ecole.refresh_from_db()
        self.assertEqual(self.ecole.nom, 'NOUVELLE SAISIE LOCALE')
        self.assertFalse(SyncChange.objects.filter(pk=ancien.pk).exists())

    def test_numerotation_par_lots_bornes_ne_perd_aucun_changement(self):
        self.eleves(9)
        SyncChange.objects.update(sequence=None)
        with patch('synchronisation.sequence.TAILLE_LOT', 3):
            self.assertEqual(attribuer_sequences(max_lots=1), 3)
            self.assertEqual(SyncChange.objects.filter(sequence__isnull=True).count(), 6)
            self.assertEqual(attribuer_sequences(max_lots=1), 3)
            self.assertEqual(attribuer_sequences(max_lots=1), 3)
        sequences = list(SyncChange.objects.order_by('sequence').values_list('sequence', flat=True))
        self.assertEqual(len(set(sequences)), 9)
        self.assertNotIn(None, sequences)

    def test_curseur_expire_demande_une_reprise_explicite(self):
        with patch('synchronisation.views._rattrapage_fait', True):
            response = self.client.get(reverse('synchronisation:pull'),
                {'initial': '1', 'snapshot_version': '2', 'snapshot_cursor': 'curseur-invalide'},
                HTTP_X_SYNC_DEVICE=str(self.poste.device_id), HTTP_X_SYNC_TOKEN='test-volume')
        self.assertEqual(response.status_code, 409)
        self.assertTrue(response.json()['snapshot_restart_required'])
        reponse_http = Mock(status_code=409)
        reponse_http.json.return_value = response.json()
        with self.assertRaises(SnapshotRestartRequired):
            _parse_response(reponse_http)

    def test_curseur_expire_redemarre_sans_effacer_les_saisies(self):
        device_id = str(self.poste.device_id)
        SyncCheckpoint.objects.create(device_id=device_id, snapshot_cursor='ancien-curseur')
        page = {'ok': True, 'has_more': False, 'latest_change_id': 0, 'changes': []}
        with patch('synchronisation.client._get_json', side_effect=[SnapshotRestartRequired('Expiré'), page]) as transport:
            self.assertEqual(pull_changes('https://sync.invalid', device_id, 'test', self.ecole, max_cycles=2), 0)
        checkpoint = SyncCheckpoint.objects.get(device_id=device_id)
        self.assertTrue(checkpoint.initial_complete)
        self.assertNotIn('snapshot_cursor', transport.call_args_list[1].kwargs['params'])
        self.assertTrue(Classe.objects.filter(pk=self.classe.pk).exists())

    def test_un_evenement_duplique_dans_une_page_nest_applique_quune_fois(self):
        item = {'id': 100, 'model_label': 'eleves.Ecole', 'object_uuid': str(self.ecole.sync_uuid),
                'operation': 'UPDATE', 'payload': {'nom': 'Une seule application'}}
        self.assertEqual(_appliquer_lot([item, item], self.ecole, apply_sync_change), 1)
        self.assertEqual(SyncChange.objects.filter(server_change_id=100).count(), 1)

    def test_reception_repetee_en_echec_ne_multiplie_pas_le_journal(self):
        item = {'id': 101, 'model_label': 'eleves.Classe', 'object_uuid': str(uuid4()),
                'operation': 'UPDATE', 'payload': {'nom': 'Parent absent'}}
        appliquer = Mock(side_effect=ValueError('Relation introuvable pour ecole.'))
        for _ in range(3):
            self.assertEqual(_appliquer_lot([item], self.ecole, appliquer), 0)
        self.assertEqual(SyncChange.objects.filter(server_change_id=101, statut=SyncChange.STATUT_FAILED).count(), 1)

    def test_recherche_des_acquittements_utilise_lindex_et_lecole(self):
        query = SyncChange.objects.filter(ecole=self.ecole, server_change_id__in=[100, 101],
                                         statut=SyncChange.STATUT_APPLIED)
        self.assertNotIn('JSON_EXTRACT', str(query.query))
        if connection.vendor == 'sqlite':
            self.assertIn('sync_school_server_status', query.explain())

    def test_migration_reprend_les_identifiants_historiques(self):
        from importlib import import_module
        from types import SimpleNamespace
        from django.apps import apps
        historiques = [SyncChange.objects.create(ecole=self.ecole, model_label='eleves.Classe',
            operation='UPDATE', payload={'server_change_id': identifiant})
            for identifiant in (42, 43, True, 'incorrect', -1)]
        migration = import_module('synchronisation.migrations.0008_syncchange_reception_indexes')
        migration.reprendre_identifiants_recus(apps, SimpleNamespace(connection=connection))
        ids = list(SyncChange.objects.filter(pk__in=[c.pk for c in historiques])
                   .order_by('pk').values_list('server_change_id', flat=True))
        self.assertEqual(ids, [42, 43, None, None, None])

    def test_rattrapage_historique_se_poursuit_en_petits_lots_sans_doublon(self):
        self.eleves(7)
        SyncChange.objects.all().delete()
        progression = {}
        labels = ['eleves.Eleve']
        self.assertEqual(emettre_existants(labels, limite=3, progression=progression), 3)
        self.assertFalse(progression.get('complete'))
        self.assertEqual(emettre_existants(labels, limite=3, progression=progression), 3)
        self.assertEqual(emettre_existants(labels, limite=3, progression=progression), 1)
        self.assertTrue(progression['complete'])
        self.assertEqual(emettre_existants(labels, limite=3, progression=progression), 0)
        self.assertEqual(SyncChange.objects.filter(model_label='eleves.Eleve').count(), 7)

    def test_echec_du_rattrapage_ne_saute_pas_lobjet_en_erreur(self):
        self.eleves(2)
        SyncChange.objects.all().delete()
        progression = {}
        with patch('synchronisation.engine.serialize_instance', side_effect=RuntimeError('Interruption')):
            with self.assertRaises(RuntimeError):
                emettre_existants(['eleves.Eleve'], limite=1, progression=progression)
        self.assertEqual(emettre_existants(['eleves.Eleve'], limite=3, progression=progression), 2)

    def test_un_rattrapage_en_cours_ne_retient_pas_la_requete_dun_autre_poste(self):
        from . import views
        verrou = Mock()
        verrou.acquire.return_value = False
        with patch.object(views, '_rattrapage_fait', False), \
                patch.object(views, '_rattrapage_verrou', verrou), \
                patch.object(views, 'emettre_existants') as emettre:
            views._rattraper_modeles_ajoutes()
        verrou.acquire.assert_called_once_with(blocking=False)
        emettre.assert_not_called()

    def test_les_relations_prechargees_ne_diffusent_pas_les_eleves_dune_autre_ecole(self):
        with mute_sync():
            autre_ecole = Ecole.objects.create(nom='Autre école volume', adresse='Conakry',
                                               telephone='600000001', directeur='Direction')
            autre_classe = Classe.objects.create(ecole=autre_ecole, nom='Classe privée',
                                                niveau='PRIMAIRE_1', annee_scolaire='2026-2027')
        etranger = Eleve.objects.bulk_create([
            Eleve(classe=autre_classe, nom='CONFIDENTIEL', prenom='Autre école', matricule='CONF-1')
        ])[0]
        self.eleves(2)
        # Même un ancien journal mal rattaché ne permet pas de lire cet élève.
        SyncChange.objects.create(ecole=self.ecole, model_label='eleves.Eleve',
                                  object_uuid=etranger.sync_uuid, operation='UPDATE')
        with patch('synchronisation.views._rattrapage_fait', True):
            response = self.client.get(reverse('synchronisation:pull'), {'registre': REGISTRE_VERSION},
                HTTP_X_SYNC_DEVICE=str(self.poste.device_id), HTTP_X_SYNC_TOKEN='test-volume')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(str(etranger.sync_uuid), response.content.decode())
        self.assertNotIn('CONFIDENTIEL', response.content.decode())


class VolumeHistoriqueTests(BaseVolumeSynchronisation, TestCase):
    def test_10000_eleves_et_100000_evenements(self):
        eleves = Eleve.objects.bulk_create([
            Eleve(classe=self.classe, nom=f'Élève {i}', prenom='Volume', matricule=f'BIG-{i}')
            for i in range(10000)
        ], batch_size=500)
        for debut in range(0, 100000, 1000):
            SyncChange.objects.bulk_create([
                SyncChange(ecole=self.ecole, model_label='eleves.Eleve',
                    object_uuid=eleves[i // 10].sync_uuid, operation='UPDATE', sequence=i + 1,
                    statut=SyncChange.STATUT_APPLIED) for i in range(debut, debut + 1000)
            ], batch_size=500)
        SyncSequence.objects.update_or_create(pk=1, defaults={'valeur': 100000})
        self.poste.verifier_token('test-volume')
        debut = time.perf_counter()
        with patch('synchronisation.views._rattrapage_fait', True), CaptureQueriesContext(connection) as queries:
            response = self.client.get(reverse('synchronisation:pull'),
                {'since_id': 99500, 'registre': REGISTRE_VERSION},
                HTTP_X_SYNC_DEVICE=str(self.poste.device_id), HTTP_X_SYNC_TOKEN='test-volume')
        temps_delta = time.perf_counter() - debut
        self.assertEqual(response.status_code, 200)
        delta = response.json()
        self.assertEqual(sum(row['id'] is not None for row in delta['changes']), 500)
        self.assertEqual(delta['latest_change_id'], 100000)
        self.assertLess(len(queries), 35)
        debut = time.perf_counter()
        recus = set()
        curseur = None
        pages = 0
        while True:
            rows, curseur, watermark = snapshot_page_for_ecole(self.ecole, curseur,
                                                             labels={'eleves.Eleve'})
            recus.update(row['object_uuid'] for row in rows)
            pages += 1
            self.assertLessEqual(len(rows), 500)
            self.assertEqual(watermark, 100000)
            if curseur is None:
                break
        self.assertEqual(len(recus), 10000)
        print('\n[Mesure volume / SQLite local sans réseau] ' + json.dumps({
            'eleves': 10000, 'evenements': 100000, 'delta_500_requetes': len(queries),
            'delta_500_ms': round(temps_delta * 1000, 1), 'instantane_pages': pages,
            'instantane_10000_ms': round((time.perf_counter() - debut) * 1000, 1),
        }))


class TransactionsReceptionTests(TransactionTestCase):
    def setUp(self):
        with mute_sync():
            self.ecole = Ecole.objects.create(nom='École transactions réception', adresse='Conakry',
                                             telephone='600000000', directeur='Direction')
        self.device_id = str(uuid4())
        SyncCheckpoint.objects.create(device_id=self.device_id, initial_complete=True,
                                      registre=REGISTRE_VERSION)
        self.page = {'ok': True, 'has_more': False, 'latest_change_id': 2, 'changes': [
            {'id': i, 'model_label': 'eleves.Ecole', 'object_uuid': str(self.ecole.sync_uuid),
             'operation': 'UPDATE', 'payload': {'nom': f'École {i}'}} for i in (1, 2)
        ]}

    def test_chaque_donnee_est_validee_avant_la_suivante(self):
        validations = []
        validations_avant_suivante = []

        def appliquer(change):
            if change.payload['server_change_id'] == 2:
                validations_avant_suivante.append(list(validations))
            apply_sync_change(change)
            transaction.on_commit(lambda: validations.append(change.payload['server_change_id']))

        with patch('synchronisation.client._get_json', return_value=self.page):
            self.assertEqual(pull_changes('https://sync.invalid', self.device_id, 'jeton', self.ecole,
                                         apply_change=appliquer), 2)
        self.assertEqual(validations, [1, 2])
        self.assertEqual(validations_avant_suivante, [[1]], 'Le lot entier garde encore sa transaction ouverte')

    def test_interruption_du_curseur_reprend_sans_reappliquer_les_donnees_validees(self):
        appliques = []

        def appliquer(change):
            apply_sync_change(change)
            appliques.append(change.payload['server_change_id'])

        with patch('synchronisation.client._get_json', return_value=self.page):
            with patch.object(SyncCheckpoint, 'save', side_effect=RuntimeError('Interruption avant curseur')):
                with self.assertRaises(Exception):
                    pull_changes('https://sync.invalid', self.device_id, 'jeton', self.ecole,
                                 apply_change=appliquer)
            self.assertEqual(pull_changes('https://sync.invalid', self.device_id, 'jeton', self.ecole,
                                         apply_change=appliquer), 0)
        self.assertEqual(appliques, [1, 2])
        self.assertEqual(SyncCheckpoint.objects.get(device_id=self.device_id).last_change_id, 2)
