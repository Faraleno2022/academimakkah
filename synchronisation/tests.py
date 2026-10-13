import json

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse

from eleves.models import Ecole
from utilisateurs.models import Profil


class SynchronisationApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='adminsync', password='secret123')
        self.ecole = Ecole.objects.create(
            nom='Ecole Test',
            adresse='Conakry',
            telephone='+224600000000',
            directeur='Direction',
            etat='VALIDE',
        )
        profil, _ = Profil.objects.get_or_create(user=self.user)
        profil.role = 'ADMIN'
        profil.ecole = self.ecole
        profil.is_validated = True
        profil.save(update_fields=['role', 'ecole', 'is_validated'])
        self.client = Client()

    def test_health_endpoint(self):
        response = self.client.get(reverse('synchronisation:health'))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['ok'])

    def test_register_device_and_push_change(self):
        self.client.force_login(self.user)
        response = self.client.post(
            reverse('synchronisation:register_device'),
            data=json.dumps({'nom': 'Poste direction'}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 201)
        data = response.json()

        response = self.client.post(
            reverse('synchronisation:push'),
            data=json.dumps({
                'changes': [
                    {
                        'model': 'eleves.Ecole',
                        'object_uuid': str(self.ecole.sync_uuid),
                        'operation': 'UPDATE',
                        'payload': {
                            'sync_uuid': str(self.ecole.sync_uuid),
                            'nom': 'Ecole Test Sync',
                            'adresse': 'Conakry',
                            'telephone': '+224600000000',
                            'directeur': 'Direction',
                            'etat': 'VALIDE',
                        },
                    }
                ]
            }),
            content_type='application/json',
            HTTP_X_SYNC_DEVICE=data['device_id'],
            HTTP_X_SYNC_TOKEN=data['sync_token'],
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['accepted_count'], 1)

    def test_register_device_with_admin_token_and_pull_other_device_changes(self):
        from django.test import override_settings

        with override_settings(MYSCHOOL_SYNC_ADMIN_TOKEN='bootstrap-secret'):
            response = self.client.post(
                reverse('synchronisation:register_device'),
                data=json.dumps({'nom': 'Poste 1', 'ecole_id': self.ecole.id}),
                content_type='application/json',
                HTTP_X_SYNC_ADMIN_TOKEN='bootstrap-secret',
            )
        self.assertEqual(response.status_code, 201)
        device_one = response.json()

        with override_settings(MYSCHOOL_SYNC_ADMIN_TOKEN='bootstrap-secret'):
            response = self.client.post(
                reverse('synchronisation:register_device'),
                data=json.dumps({'nom': 'Poste 2', 'ecole_id': self.ecole.id}),
                content_type='application/json',
                HTTP_X_SYNC_ADMIN_TOKEN='bootstrap-secret',
            )
        self.assertEqual(response.status_code, 201)
        device_two = response.json()

        response = self.client.post(
            reverse('synchronisation:push'),
            data=json.dumps({
                'changes': [
                    {
                        'model': 'eleves.Ecole',
                        'object_uuid': str(self.ecole.sync_uuid),
                        'operation': 'UPDATE',
                        'payload': {
                            'sync_uuid': str(self.ecole.sync_uuid),
                            'nom': 'Ecole Test Poste 1',
                            'adresse': 'Conakry',
                            'telephone': '+224600000000',
                            'directeur': 'Direction',
                            'etat': 'VALIDE',
                        },
                    }
                ]
            }),
            content_type='application/json',
            HTTP_X_SYNC_DEVICE=device_one['device_id'],
            HTTP_X_SYNC_TOKEN=device_one['sync_token'],
        )
        self.assertEqual(response.status_code, 200)

        response = self.client.get(
            reverse('synchronisation:pull'),
            HTTP_X_SYNC_DEVICE=device_two['device_id'],
            HTTP_X_SYNC_TOKEN=device_two['sync_token'],
        )
        self.assertEqual(response.status_code, 200)
        changes = response.json()['changes']
        self.assertEqual(len(changes), 2)  # création web initiale et modification du poste 1
        self.assertTrue(all(row['object_uuid'] == str(self.ecole.sync_uuid) for row in changes))
        self.assertTrue(any(row['device_id'] == device_one['device_id'] for row in changes))


class PushPendingFileTests(TestCase):
    """La file locale ne doit jamais rester bloquee derriere un refus serveur."""

    def setUp(self):
        self.ecole = Ecole.objects.create(
            nom='Ecole File', adresse='Conakry', telephone='+224600000001',
            directeur='Direction', etat='VALIDE',
        )

    def _changements(self, nombre):
        from synchronisation.models import SyncChange
        SyncChange.objects.filter(ecole=self.ecole).delete()
        return [
            SyncChange.objects.create(
                ecole=self.ecole, model_label='eleves.Eleve',
                operation=SyncChange.OPERATION_UPDATE, payload={'nom': f'E{i}'},
            )
            for i in range(nombre)
        ]

    def test_un_refus_ne_bloque_pas_les_changements_suivants(self):
        """Avant correction : le lot refuse etait renvoye a l'identique en boucle."""
        from unittest.mock import patch
        from synchronisation.client import push_pending
        from synchronisation.models import SyncChange

        changements = self._changements(3)
        envois = []

        def faux_post(url, device_id, token, payload, timeout=25):
            envois.append([c['payload']['nom'] for c in payload['changes']])
            # Le serveur refuse systematiquement le premier changement recu.
            return {
                'ok': True,
                'accepted': [{'index': i} for i in range(1, len(payload['changes']))],
                'rejected': [{'index': 0, 'error': 'Relation introuvable pour classe.'}],
            }

        with patch('synchronisation.client._post_json', faux_post):
            total = push_pending('https://exemple', 'dev', 'tok', self.ecole, batch_size=2)

        self.assertEqual(envois, [['E0', 'E1'], ['E2']])
        self.assertEqual(total, 1)
        refuse = SyncChange.objects.get(pk=changements[0].pk)
        self.assertEqual(refuse.statut, SyncChange.STATUT_FAILED)
        self.assertIn('Relation introuvable', refuse.erreur)
        self.assertEqual(SyncChange.objects.get(pk=changements[2].pk).statut,
                         SyncChange.STATUT_FAILED)
        self.assertEqual(SyncChange.objects.get(pk=changements[1].pk).statut,
                         SyncChange.STATUT_APPLIED)

    def test_rejouer_refuses_remet_en_file(self):
        from synchronisation.client import rejouer_refuses
        from synchronisation.models import SyncChange

        changements = self._changements(2)
        SyncChange.objects.filter(pk=changements[0].pk).update(
            statut=SyncChange.STATUT_FAILED, erreur='Relation introuvable.')

        self.assertEqual(rejouer_refuses(self.ecole), 1)
        rejoue = SyncChange.objects.get(pk=changements[0].pk)
        self.assertEqual(rejoue.statut, SyncChange.STATUT_PENDING)
        self.assertEqual(rejoue.erreur, '')


class DiffusionOrdonneeTests(TestCase):
    """Un poste ne doit jamais sauter un lot validé en retard par le serveur."""

    def setUp(self):
        from synchronisation.models import SyncDevice

        self.ecole = Ecole.objects.create(
            nom='Ecole Diffusion', adresse='Conakry', telephone='+224600000002',
            directeur='Direction', etat='VALIDE',
        )
        self.postes = {}
        for nom in ('A', 'B'):
            poste = SyncDevice(ecole=self.ecole, nom=f'Poste {nom}')
            poste.definir_token(f'jeton-{nom}')
            poste.save()
            self.postes[nom] = poste

    def pull(self, nom, since_id=0):
        poste = self.postes[nom]
        response = self.client.get(
            reverse('synchronisation:pull'), {'since_id': since_id},
            HTTP_X_SYNC_DEVICE=str(poste.device_id), HTTP_X_SYNC_TOKEN=f'jeton-{nom}',
        )
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def classe(self, nom):
        from eleves.models import Classe
        return Classe.objects.create(
            ecole=self.ecole, nom=nom, niveau='PRIMAIRE_1', annee_scolaire='2026-2027',
        )

    def test_lot_valide_en_retard_est_livre(self):
        from synchronisation.models import SyncChange

        # Le lot du poste A, revenu en ligne, réserve un id puis reste en cours
        # de validation (simulé : id réservé, ligne absente).
        reserve = SyncChange.objects.create(ecole=self.ecole, model_label='x', operation='UPDATE')
        id_reserve = reserve.id
        reserve.delete()
        # Pendant ce temps une saisie web est validée, et B la reçoit.
        self.classe('CP1')
        curseur = self.pull('B')['latest_change_id']
        # Le lot de A est enfin validé, avec un id inférieur au curseur de B.
        classe_a = self.classe('CP2')
        SyncChange.objects.filter(object_uuid=classe_a.sync_uuid).update(
            id=id_reserve, device=self.postes['A'], statut=SyncChange.STATUT_APPLIED,
        )

        recus = self.pull('B', curseur)['changes']
        self.assertIn(str(classe_a.sync_uuid), [ligne['object_uuid'] for ligne in recus])

    def test_numeros_croissants_et_jamais_inferieurs_a_l_id(self):
        from synchronisation.models import SyncChange
        from synchronisation.sequence import attribuer_sequences

        for nom in ('CE1', 'CE2', 'CM1'):
            self.classe(nom)
        attribuer_sequences()
        lignes = list(SyncChange.objects.order_by('sequence').values_list('id', 'sequence'))
        self.assertTrue(all(sequence >= id_ for id_, sequence in lignes))
        self.assertEqual([id_ for id_, _ in lignes], sorted(id_ for id_, _ in lignes))
        self.assertEqual(attribuer_sequences(), 0)

    def test_parents_livres_avant_le_changement(self):
        # Un poste qui a manqué la classe peut quand même appliquer l'élève.
        from eleves.models import Eleve

        classe = self.classe('CE1')
        curseur = self.pull('B')['latest_change_id']
        eleve = Eleve.objects.create(classe=classe, nom='Diallo', prenom='Awa', matricule='DIF-001')

        recus = self.pull('B', curseur)['changes']
        uuids = [ligne['object_uuid'] for ligne in recus]
        self.assertLess(uuids.index(str(classe.sync_uuid)), uuids.index(str(eleve.sync_uuid)))
        dependance = recus[uuids.index(str(classe.sync_uuid))]
        self.assertIsNone(dependance['id'])
        self.assertEqual(dependance['operation'], 'UPDATE')

    def test_reemission_rattrape_les_postes_deja_passes(self):
        from io import StringIO
        from django.core.management import call_command

        classe = self.classe('CE1')
        donnees = self.pull('B')
        curseur = donnees['latest_change_id']
        id_serveur = next(l['id'] for l in donnees['changes'] if l['object_uuid'] == str(classe.sync_uuid))
        self.assertEqual(self.pull('B', curseur)['changes'], [])

        sortie = StringIO()
        call_command('sync_reemettre', depuis='2000-01-01', stdout=sortie)
        self.assertIn('--confirmer', sortie.getvalue())
        self.assertEqual(self.pull('B', curseur)['changes'], [])

        call_command('sync_reemettre', depuis='2000-01-01', confirmer=True, stdout=StringIO())
        recus = self.pull('B', curseur)['changes']
        # Même id serveur : un poste qui l'avait déjà appliqué l'ignore.
        self.assertIn(id_serveur, [ligne['id'] for ligne in recus])

    def test_changement_recu_en_echec_jamais_renvoye_au_serveur(self):
        from synchronisation.client import MARQUEUR_RECU, rejouer_refuses
        from synchronisation.models import SyncChange

        SyncChange.objects.create(
            ecole=self.ecole, model_label='eleves.Classe', operation='UPDATE',
            payload={MARQUEUR_RECU: True}, statut=SyncChange.STATUT_FAILED,
        )
        self.assertEqual(rejouer_refuses(self.ecole), 0)

    def test_client_ignore_en_une_requete_les_changements_deja_appliques(self):
        from unittest.mock import patch
        from uuid import uuid4
        from synchronisation.client import pull_changes
        from synchronisation.models import SyncChange, SyncCheckpoint

        classe = self.classe('CE1')
        SyncChange.objects.create(
            ecole=self.ecole, model_label='eleves.Classe', object_uuid=classe.sync_uuid,
            operation='UPDATE', payload={'server_change_id': 41}, server_change_id=41, statut=SyncChange.STATUT_APPLIED,
        )
        device_id = str(uuid4())
        SyncCheckpoint.objects.create(device_id=device_id, initial_complete=True, last_change_id=40)
        page = {'ok': True, 'has_more': False, 'latest_change_id': 42, 'changes': [
            {'id': 41, 'model_label': 'eleves.Classe', 'object_uuid': str(classe.sync_uuid),
             'operation': 'UPDATE', 'payload': {'nom': 'Deja recu'}},
            {'id': 42, 'model_label': 'eleves.Classe', 'object_uuid': str(classe.sync_uuid),
             'operation': 'UPDATE', 'payload': {'nom': 'Nouveau nom'}},
        ]}
        appliques = []
        with patch('synchronisation.client._get_json', return_value=page):
            total = pull_changes('https://sync.invalid', device_id, 'jeton', self.ecole,
                                 apply_change=lambda change: appliques.append(change.payload['nom']))
        self.assertEqual(appliques, ['Nouveau nom'])
        self.assertEqual(total, 1)
