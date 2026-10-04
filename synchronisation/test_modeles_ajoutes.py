"""Modèles ajoutés au registre après coup (version 2) : diffusion, compatibilité
avec les postes non mis à jour, rattrapage et doublons de pointage."""
import json
from datetime import date
from unittest.mock import patch
from uuid import uuid4

from django.apps import apps
from django.contrib.auth.hashers import check_password
from django.test import TestCase
from django.urls import reverse

from eleves.models import Classe, Ecole, Eleve
from presence.models import PresenceJournaliere

from .context import mute_sync
from .engine import emettre_existants
from .mixins import SyncTrackedModel
from .models import SyncChange, SyncCheckpoint, SyncDevice
from .registry import MODELES_REGISTRE_2, REGISTRE_VERSION, SYNC_MODEL_LABELS, SYNC_MODEL_SET

PRESENCE = 'presence.PresenceJournaliere'


class RegistreCompletTests(TestCase):
    def test_tout_modele_suivi_est_synchronise(self):
        """Un nouveau modèle SyncTrackedModel oublié dans le registre resterait sur son poste."""
        oublies = sorted(
            f'{m._meta.app_label}.{m.__name__}' for m in apps.get_models()
            if issubclass(m, SyncTrackedModel)
            and f'{m._meta.app_label}.{m.__name__}' not in SYNC_MODEL_SET
        )
        self.assertEqual(oublies, [])

    def test_nouveaux_modeles_en_fin_de_registre(self):
        # Le curseur d'instantané désigne un modèle par sa position.
        self.assertEqual(SYNC_MODEL_LABELS[-len(MODELES_REGISTRE_2):], MODELES_REGISTRE_2)
        self.assertLess(MODELES_REGISTRE_2.index('notes.Devoir'), MODELES_REGISTRE_2.index('notes.RemiseDevoir'))


class ModelesAjoutesTests(TestCase):
    def setUp(self):
        self.ecole = Ecole.objects.create(
            nom='Ecole Pointage', adresse='Conakry', telephone='+224600000010',
            directeur='Direction', etat='VALIDE',
        )
        self.classe = Classe.objects.create(
            ecole=self.ecole, nom='CE1', niveau='PRIMAIRE_3', annee_scolaire='2026-2027',
        )
        self.eleve = Eleve.objects.create(classe=self.classe, nom='Bah', prenom='Oumou', matricule='PTG-001')
        self.poste = SyncDevice(ecole=self.ecole, nom='Poste A')
        self.poste.definir_token('jeton-A')
        self.poste.save()

    def entetes(self):
        return {'HTTP_X_SYNC_DEVICE': str(self.poste.device_id), 'HTTP_X_SYNC_TOKEN': 'jeton-A'}

    def pull(self, **params):
        response = self.client.get(reverse('synchronisation:pull'), params, **self.entetes())
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def pointer(self, statut='PRESENT', jour=date(2026, 10, 5)):
        return PresenceJournaliere.objects.create(
            eleve=self.eleve, classe=self.classe, date=jour, statut=statut,
        )

    def test_un_pointage_est_diffuse(self):
        presence = self.pointer()
        self.assertTrue(SyncChange.objects.filter(model_label=PRESENCE, object_uuid=presence.sync_uuid).exists())
        recus = self.pull(since_id=0, registre=REGISTRE_VERSION)['changes']
        self.assertIn(str(presence.sync_uuid), [ligne['object_uuid'] for ligne in recus])

    def test_poste_non_mis_a_jour_ne_recoit_pas_les_nouveaux_modeles(self):
        presence = self.pointer()
        recus = self.pull(since_id=0)['changes']
        self.assertNotIn(str(presence.sync_uuid), [ligne['object_uuid'] for ligne in recus])
        self.assertIn(str(self.eleve.sync_uuid), [ligne['object_uuid'] for ligne in recus])

        instantane = self.pull(initial=1, snapshot_version=2)['changes']
        self.assertNotIn(PRESENCE, {ligne['model_label'] for ligne in instantane})
        instantane = self.pull(initial=1, snapshot_version=2, registre=REGISTRE_VERSION)['changes']
        self.assertIn(PRESENCE, {ligne['model_label'] for ligne in instantane})

    def test_rattrapage_ne_renvoie_que_les_modeles_demandes(self):
        presence = self.pointer()
        recus = self.pull(since_id=0, registre=REGISTRE_VERSION, modeles=','.join(MODELES_REGISTRE_2))['changes']
        self.assertEqual({ligne['model_label'] for ligne in recus if ligne['id']}, {PRESENCE})
        self.assertIn(str(presence.sync_uuid), [ligne['object_uuid'] for ligne in recus])

    def test_lignes_anterieures_emises_une_seule_fois(self):
        with mute_sync():  # saisi avant l'ajout au registre : aucun changement
            presence = self.pointer()
        self.assertFalse(SyncChange.objects.filter(object_uuid=presence.sync_uuid).exists())

        self.assertEqual(emettre_existants(MODELES_REGISTRE_2), 1)
        change = SyncChange.objects.get(object_uuid=presence.sync_uuid)
        self.assertEqual((change.ecole, change.statut), (self.ecole, SyncChange.STATUT_PENDING))
        self.assertEqual(emettre_existants(MODELES_REGISTRE_2), 0)

    def test_doublon_de_pointage_le_dernier_synchronise_l_emporte(self):
        existant = self.pointer('PRESENT')
        autre_uuid = uuid4()
        response = self.client.post(
            reverse('synchronisation:push'),
            data=json.dumps({'changes': [{
                'model': PRESENCE, 'object_uuid': str(autre_uuid), 'operation': 'CREATE',
                'payload': {
                    'eleve': {'model': 'eleves.Eleve', 'sync_uuid': str(self.eleve.sync_uuid)},
                    'classe': {'model': 'eleves.Classe', 'sync_uuid': str(self.classe.sync_uuid)},
                    'date': '2026-10-05', 'statut': 'ABSENT', 'motif': 'Malade',
                },
            }]}),
            content_type='application/json', **self.entetes(),
        )
        self.assertEqual(response.json()['accepted_count'], 1, response.content)
        presence = PresenceJournaliere.objects.get(eleve=self.eleve, date=date(2026, 10, 5))
        self.assertEqual(presence.pk, existant.pk)
        self.assertEqual((presence.sync_uuid, presence.statut, presence.motif), (autre_uuid, 'ABSENT', 'Malade'))

    def test_poste_mis_a_jour_rattrape_puis_note_la_version(self):
        from .client import rattraper_modeles_ajoutes

        device_id = str(uuid4())
        SyncCheckpoint.objects.create(device_id=device_id, initial_complete=True, last_change_id=500)
        page = {'ok': True, 'has_more': False, 'latest_change_id': 77, 'changes': [
            {'id': 77, 'model_label': PRESENCE, 'object_uuid': str(uuid4()),
             'operation': 'UPDATE', 'payload': {}},
        ]}
        appels, appliques = [], []

        def faux_get(url, device_id, token, params=None, timeout=25):
            appels.append(params)
            return page

        with patch('synchronisation.client._get_json', faux_get):
            total = rattraper_modeles_ajoutes(
                'https://sync.invalid', device_id, 'jeton', self.ecole,
                apply_change=lambda change: appliques.append(change.model_label),
            )
            self.assertEqual(total, 1)
            # Déjà à jour : plus aucune requête.
            self.assertEqual(rattraper_modeles_ajoutes('https://sync.invalid', device_id, 'jeton', self.ecole), 0)

        self.assertEqual(len(appels), 1)
        self.assertEqual(appels[0]['since_id'], 0)
        self.assertEqual(appels[0]['modeles'].split(','), MODELES_REGISTRE_2)
        self.assertEqual(appliques, [PRESENCE])
        checkpoint = SyncCheckpoint.objects.get(device_id=device_id)
        self.assertEqual((checkpoint.registre, checkpoint.rattrapage_id, checkpoint.last_change_id),
                         (REGISTRE_VERSION, 77, 500))


class JetonPosteTests(TestCase):
    def test_verification_mise_en_cache_sans_accepter_un_autre_jeton(self):
        ecole = Ecole.objects.create(
            nom='Ecole Jeton', adresse='Conakry', telephone='+224600000011', directeur='Direction',
        )
        poste = SyncDevice(ecole=ecole, nom='Poste J')
        poste.definir_token('bon-jeton')
        poste.save()
        with patch('synchronisation.models.check_password', wraps=check_password) as verif:
            self.assertTrue(poste.verifier_token('bon-jeton'))
            self.assertTrue(poste.verifier_token('bon-jeton'))
            self.assertEqual(verif.call_count, 1)  # le hachage coûteux n'est fait qu'une fois
            self.assertFalse(poste.verifier_token('mauvais-jeton'))
            self.assertFalse(poste.verifier_token(''))

        poste.definir_token('nouveau-jeton')
        poste.save()
        self.assertFalse(poste.verifier_token('bon-jeton'))
        self.assertTrue(poste.verifier_token('nouveau-jeton'))
