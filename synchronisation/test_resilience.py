from unittest.mock import patch
from uuid import uuid4
from datetime import timedelta

from django.db import OperationalError, connection, transaction
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from eleves.models import Ecole
from synchronisation.autosync import _cycle
from synchronisation.client import (
    MARQUEUR_RECU, SyncTransportError, reessayer_recus_en_echec,
    reprogrammer_refus_temporaires, push_pending, _get_json, _post_json,
)
from synchronisation.models import SyncChange, SyncDevice


class SynchronisationResilienceTests(TestCase):
    def setUp(self):
        self.ecole = Ecole.objects.create(
            nom='École résilience', adresse='Conakry', telephone='600000000', directeur='Direction',
        )
        self.config = {
            'server_url': 'https://sync.invalid', 'device_id': str(uuid4()),
            'token': 'test-token', 'ecole_id': str(self.ecole.pk), 'interval': 4,
        }

    def test_panne_envoi_ne_bloque_pas_reception_et_rattrapage(self):
        with patch('synchronisation.autosync.charger_config', return_value=self.config), \
                patch('synchronisation.autosync._emettre_modeles_ajoutes'), \
                patch('synchronisation.client.push_pending', side_effect=SyncTransportError('timeout')) as push, \
                patch('synchronisation.client.pull_changes', return_value=7) as pull, \
                patch('synchronisation.client.rattraper_modeles_ajoutes', return_value=2) as catchup:
            config, envoyes, recus = _cycle('.')
        self.assertEqual((envoyes, recus), (0, 9))
        self.assertEqual(push.call_args.kwargs['max_cycles'], 1)
        pull.assert_called_once()
        catchup.assert_called_once()

    def test_panne_reception_ne_supprime_pas_les_envois_reussis(self):
        with patch('synchronisation.autosync.charger_config', return_value=self.config), \
                patch('synchronisation.autosync._emettre_modeles_ajoutes'), \
                patch('synchronisation.client.push_pending', return_value=5), \
                patch('synchronisation.client.pull_changes', side_effect=SyncTransportError('timeout')), \
                patch('synchronisation.client.rattraper_modeles_ajoutes', return_value=2):
            _, envoyes, recus = _cycle('.')
        self.assertEqual((envoyes, recus), (5, 2))

    def test_echec_permanent_ne_prive_pas_les_suivants_de_reessai(self):
        SyncChange.objects.filter(ecole=self.ecole).delete()
        changements = [SyncChange.objects.create(
            ecole=self.ecole, model_label='eleves.Classe', operation='UPDATE',
            object_uuid=uuid4(), payload={MARQUEUR_RECU: True}, statut=SyncChange.STATUT_FAILED,
        ) for _ in range(3)]
        visites = []

        def appliquer(change):
            visites.append(change.pk)
            raise ValueError('Parent encore absent')

        reessayer_recus_en_echec(self.ecole, appliquer, limite=2)
        reessayer_recus_en_echec(self.ecole, appliquer, limite=2)
        self.assertIn(changements[2].pk, visites)

    def refus(self, **kwargs):
        valeurs = dict(
            ecole=self.ecole, model_label='eleves.Eleve', operation='UPDATE',
            object_uuid=uuid4(), payload={'nom': 'Test'}, statut=SyncChange.STATUT_FAILED,
            erreur='Relation introuvable pour classe.',
            date_application=timezone.now() - timedelta(minutes=2),
        )
        valeurs.update(kwargs)
        return SyncChange.objects.create(**valeurs)

    def test_seuls_les_refus_temporaires_anciens_sont_repris(self):
        ancien = self.refus()
        recent = self.refus(date_application=timezone.now())
        invalide = self.refus(erreur='Le montant du paiement ne peut pas être négatif.')
        entrant = self.refus(payload={MARQUEUR_RECU: True})
        autre_ecole = Ecole.objects.create(
            nom='Autre école', adresse='Kindia', telephone='600000001', directeur='Direction',
        )
        autre = self.refus(ecole=autre_ecole)
        self.assertEqual(reprogrammer_refus_temporaires(self.ecole), 1)
        ancien.refresh_from_db()
        self.assertEqual(ancien.statut, SyncChange.STATUT_PENDING)
        for change in (recent, invalide, entrant, autre):
            change.refresh_from_db()
            self.assertEqual(change.statut, SyncChange.STATUT_FAILED)

    def test_un_refus_remplace_ne_rejoue_pas_une_ancienne_valeur(self):
        ancien = self.refus()
        self.refus(object_uuid=ancien.object_uuid, statut=SyncChange.STATUT_APPLIED)
        self.assertEqual(reprogrammer_refus_temporaires(self.ecole), 0)

    def test_refus_temporaire_est_repris_et_acquitte_apres_arrivee_du_parent(self):
        SyncChange.objects.filter(ecole=self.ecole).delete()
        refus = self.refus()
        self.assertEqual(reprogrammer_refus_temporaires(self.ecole), 1)
        with patch('synchronisation.client._post_json', return_value={
            'ok': True, 'accepted': [{'index': 0}], 'rejected': [],
        }):
            self.assertEqual(push_pending('https://sync.invalid', 'dev', 'token', self.ecole), 1)
        refus.refresh_from_db()
        self.assertEqual(refus.statut, SyncChange.STATUT_APPLIED)
        self.assertEqual(refus.erreur, '')

    def test_acquittement_incomplet_ou_invalide_ne_perd_aucun_changement(self):
        SyncChange.objects.filter(ecole=self.ecole).delete()
        changement = self.refus(statut=SyncChange.STATUT_PENDING)
        for response in (
            {'ok': True, 'accepted': [], 'rejected': []},
            {'ok': True, 'accepted': [{'index': 0}], 'rejected': [{'index': 0}]},
            {'ok': True, 'accepted': [], 'rejected': [{'index': -1}]},
        ):
            with self.subTest(response=response), patch('synchronisation.client._post_json', return_value=response):
                with self.assertRaises(SyncTransportError):
                    push_pending('https://sync.invalid', 'dev', 'token', self.ecole)
                changement.refresh_from_db()
                self.assertEqual(changement.statut, SyncChange.STATUT_PENDING)

    def test_refus_de_base_temporaire_est_reprogramme_sans_rejouer_une_contrainte(self):
        SyncChange.objects.filter(ecole=self.ecole).delete()
        incident = self.refus(statut=SyncChange.STATUT_PENDING)
        with patch('synchronisation.client._post_json', return_value={
            'ok': True, 'accepted': [],
            'rejected': [{'index': 0, 'error': 'Deadlock found', 'retryable': True}],
        }):
            self.assertEqual(push_pending('https://sync.invalid', 'dev', 'token', self.ecole), 0)
        incident.refresh_from_db()
        self.assertTrue(incident.erreur.startswith('[TEMPORAIRE] '))
        self.assertEqual(reprogrammer_refus_temporaires(self.ecole), 0)
        SyncChange.objects.filter(pk=incident.pk).update(date_application=timezone.now() - timedelta(minutes=2))
        contrainte = self.refus(erreur='UNIQUE constraint failed: eleves_eleve.matricule')
        self.assertEqual(reprogrammer_refus_temporaires(self.ecole), 1)
        incident.refresh_from_db()
        contrainte.refresh_from_db()
        self.assertEqual(incident.statut, SyncChange.STATUT_PENDING)
        self.assertEqual(contrainte.statut, SyncChange.STATUT_FAILED)

    def test_delai_connexion_distinct_du_traitement_serveur(self):
        with patch('synchronisation.client._get_session') as session:
            session.return_value.get.return_value.status_code = 200
            session.return_value.get.return_value.json.return_value = {'ok': True}
            session.return_value.post.return_value.status_code = 200
            session.return_value.post.return_value.json.return_value = {'ok': True}
            _get_json('https://sync.invalid', 'dev', 'token')
            _post_json('https://sync.invalid', 'dev', 'token', {}, timeout=120)
            self.assertEqual(session.return_value.get.call_args.kwargs['timeout'], (5, 25))
            self.assertEqual(session.return_value.post.call_args.kwargs['timeout'], (5, 120))


    def test_identifiants_stables_apres_coupure_et_distincts_par_changement(self):
        SyncChange.objects.filter(ecole=self.ecole).delete()
        objet = uuid4()
        changements = [self.refus(object_uuid=objet, statut=SyncChange.STATUT_PENDING) for _ in range(2)]
        envois = []

        def transporter(url, device_id, token, payload, timeout):
            envois.append(payload['changes'])
            if len(envois) == 1:
                raise SyncTransportError('Réponse perdue')
            return {'ok': True, 'accepted': [{'index': 0}, {'index': 1}], 'rejected': []}

        with patch('synchronisation.client._post_json', side_effect=transporter):
            with self.assertRaises(SyncTransportError):
                push_pending('https://sync.invalid', 'dev', 'token', self.ecole)
            self.assertEqual(SyncChange.objects.filter(pk__in=[c.pk for c in changements], statut=SyncChange.STATUT_PENDING).count(), 2)
            self.assertEqual(push_pending('https://sync.invalid', 'dev', 'token', self.ecole), 2)
        self.assertEqual([c['event_id'] for c in envois[0]], [c['event_id'] for c in envois[1]])
        self.assertNotEqual(envois[0][0]['event_id'], envois[0][1]['event_id'])


class SynchronisationTransactionsServeurTests(TransactionTestCase):
    """Transactions réelles pour reproduire un savepoint perdu côté serveur."""

    def setUp(self):
        self.ecole = Ecole.objects.create(
            nom='École transactions', adresse='Conakry', telephone='600000000', directeur='Direction',
        )
        self.poste = SyncDevice(ecole=self.ecole, nom='Poste test')
        self.poste.definir_token('test-token')
        self.poste.save()
        SyncChange.objects.all().delete()

    def envoyer(self, noms):
        return self.client.post(
            reverse('synchronisation:push'),
            data={'changes': [{
                'model': 'eleves.Ecole', 'object_uuid': str(self.ecole.sync_uuid),
                'operation': 'UPDATE', 'payload': {'nom': nom},
            } for nom in noms]},
            content_type='application/json',
            HTTP_X_SYNC_DEVICE=str(self.poste.device_id), HTTP_X_SYNC_TOKEN='test-token',
        )

    def test_savepoint_perdu_ne_casse_ni_le_refus_ni_le_reste_du_lot(self):
        from synchronisation.engine import apply_sync_change

        def appliquer(change):
            if change.payload['nom'] == 'Échec':
                with transaction.atomic():
                    Ecole.objects.filter(pk=self.ecole.pk).update(adresse='Écriture à annuler')
                    raise OperationalError(1213, 'Deadlock found when trying to get lock')
            return apply_sync_change(change)

        # MySQL peut annuler toute la transaction lors d'un deadlock : les
        # savepoints disparaissent et leur rollback échoue à son tour.
        with patch('synchronisation.views.apply_sync_change', side_effect=appliquer), \
                patch.object(connection, 'savepoint_rollback', side_effect=OperationalError(1305, 'SAVEPOINT does not exist')):
            response = self.envoyer(['Premier', 'Échec', 'Dernier'])
        self.assertEqual(response.status_code, 200, response.content)
        resultat = response.json()
        self.assertEqual([item['index'] for item in resultat['accepted']], [0, 2])
        self.assertEqual(resultat['rejected'][0]['index'], 1)
        self.assertTrue(resultat['rejected'][0]['retryable'])
        self.assertIn('Deadlock', resultat['rejected'][0]['error'])
        self.ecole.refresh_from_db()
        self.assertEqual(self.ecole.nom, 'DERNIER')
        self.assertEqual(self.ecole.adresse, 'CONAKRY')
        self.assertEqual(SyncChange.objects.filter(statut=SyncChange.STATUT_APPLIED).count(), 2)
        self.assertEqual(SyncChange.objects.filter(statut=SyncChange.STATUT_FAILED).count(), 1)

    def test_donnee_invalide_ne_bloque_pas_la_suivante(self):
        from synchronisation.engine import apply_sync_change

        def appliquer(change):
            if change.payload['nom'] == 'Invalide':
                # Véritable erreur SQL de contrainte, pas une panne temporaire.
                Ecole.objects.filter(pk=self.ecole.pk).update(nom=None)
            return apply_sync_change(change)

        with patch('synchronisation.views.apply_sync_change', side_effect=appliquer):
            response = self.envoyer(['Invalide', 'Valide'])
        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(response.json()['rejected'][0]['retryable'])
        self.assertEqual(response.json()['accepted_count'], 1)
        self.ecole.refresh_from_db()
        self.assertEqual(self.ecole.nom, 'VALIDE')

    def test_base_indisponible_retourne_503_sans_faux_acquittement(self):
        with patch.object(SyncChange.objects, 'create', side_effect=OperationalError('database is locked')):
            response = self.envoyer(['En attente'])
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.json()['ok'])
        self.assertNotIn('accepted', response.json())
        self.ecole.refresh_from_db()
        self.assertEqual(self.ecole.nom, 'ÉCOLE TRANSACTIONS')
        self.assertFalse(SyncChange.objects.exists())


    def envoyer_evenement(self, nom, event_id):
        return self.client.post(
            reverse('synchronisation:push'),
            data={'changes': [{
                'model': 'eleves.Ecole', 'object_uuid': str(self.ecole.sync_uuid),
                'operation': 'UPDATE', 'payload': {'nom': nom}, 'event_id': event_id,
            }]}, content_type='application/json',
            HTTP_X_SYNC_DEVICE=str(self.poste.device_id), HTTP_X_SYNC_TOKEN='test-token',
        )

    def test_acquittement_perdu_ne_rejoue_pas_une_ancienne_valeur(self):
        event_id = str(uuid4())
        premier = self.envoyer_evenement('Ancienne valeur', event_id)
        self.assertEqual(premier.status_code, 200)
        # Le serveur valide, mais la réponse n'arrive pas au poste. Une saisie
        # web intervient avant le prochain essai du même changement.
        Ecole.objects.filter(pk=self.ecole.pk).update(nom='SAISIE PLUS RÉCENTE')
        second = self.envoyer_evenement('Ancienne valeur', event_id)
        self.assertEqual(second.status_code, 200)
        self.ecole.refresh_from_db()
        self.assertEqual(self.ecole.nom, 'SAISIE PLUS RÉCENTE')
        self.assertEqual(premier.json()['accepted'], second.json()['accepted'])
        self.assertEqual(SyncChange.objects.filter(device=self.poste, statut=SyncChange.STATUT_APPLIED).count(), 1)


    def test_un_nouvel_evenement_peut_reappliquer_la_meme_valeur(self):
        self.envoyer_evenement('Valeur voulue', str(uuid4()))
        Ecole.objects.filter(pk=self.ecole.pk).update(nom='AUTRE VALEUR')
        second = self.envoyer_evenement('Valeur voulue', str(uuid4()))
        self.assertEqual(second.json()['accepted_count'], 1)
        self.ecole.refresh_from_db()
        self.assertEqual(self.ecole.nom, 'VALEUR VOULUE')
        self.assertEqual(SyncChange.objects.filter(device=self.poste, statut=SyncChange.STATUT_APPLIED).count(), 2)

    def test_identifiant_reutilise_avec_autres_donnees_est_refuse(self):
        event_id = str(uuid4())
        self.envoyer_evenement('Première valeur', event_id)
        second = self.envoyer_evenement('Valeur différente', event_id)
        self.assertEqual(second.json()['accepted_count'], 0)
        self.assertIn('données différentes', second.json()['rejected'][0]['error'])
        self.ecole.refresh_from_db()
        self.assertEqual(self.ecole.nom, 'PREMIÈRE VALEUR')

    def test_evenement_refuse_peut_etre_repris_apres_une_panne(self):
        event_id = str(uuid4())
        with patch('synchronisation.views.apply_sync_change', side_effect=OperationalError('database is locked')):
            premier = self.envoyer_evenement('Après reprise', event_id)
        self.assertTrue(premier.json()['rejected'][0]['retryable'])
        self.assertFalse(SyncChange.objects.filter(source_event_id=event_id).exists())
        second = self.envoyer_evenement('Après reprise', event_id)
        self.assertEqual(second.json()['accepted_count'], 1)
        self.ecole.refresh_from_db()
        self.assertEqual(self.ecole.nom, 'APRÈS REPRISE')

    def test_un_identifiant_est_propre_a_son_poste(self):
        event_id = str(uuid4())
        self.envoyer_evenement('Poste initial', event_id)
        autre = SyncDevice(ecole=self.ecole, nom='Autre poste')
        autre.definir_token('test-token')
        autre.save()
        self.poste = autre
        second = self.envoyer_evenement('Autre poste', event_id)
        self.assertEqual(second.json()['accepted_count'], 1)
        self.ecole.refresh_from_db()
        self.assertEqual(self.ecole.nom, 'AUTRE POSTE')

    def test_identifiant_invalide_est_refuse_sans_modifier_la_donnee(self):
        response = self.envoyer_evenement('Invalide', 'pas-un-uuid')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['accepted_count'], 0)
        self.assertEqual(response.json()['rejected_count'], 1)
        self.ecole.refresh_from_db()
        self.assertEqual(self.ecole.nom, 'ÉCOLE TRANSACTIONS')

    def test_reemission_suppression_ne_supprime_pas_un_objet_recree(self):
        from eleves.models import Classe
        from synchronisation.context import mute_sync
        event_id = str(uuid4())
        with mute_sync():
            classe = Classe.objects.create(ecole=self.ecole, nom='CE1', niveau='PRIMAIRE_1', annee_scolaire='2026-2027')
        object_uuid = classe.sync_uuid
        donnees = {'changes': [{
            'model': 'eleves.Classe', 'object_uuid': str(object_uuid),
            'operation': 'DELETE', 'payload': {}, 'event_id': event_id,
        }]}
        headers = {'HTTP_X_SYNC_DEVICE': str(self.poste.device_id), 'HTTP_X_SYNC_TOKEN': 'test-token'}
        premier = self.client.post(reverse('synchronisation:push'), data=donnees, content_type='application/json', **headers)
        self.assertEqual(premier.json()['accepted_count'], 1)
        self.assertFalse(Classe.objects.filter(sync_uuid=object_uuid).exists())
        with mute_sync():
            Classe.objects.create(ecole=self.ecole, sync_uuid=object_uuid, nom='CE1 recréée', niveau='PRIMAIRE_1', annee_scolaire='2026-2027')
        second = self.client.post(reverse('synchronisation:push'), data=donnees, content_type='application/json', **headers)
        self.assertEqual(second.json()['accepted'], premier.json()['accepted'])
        self.assertTrue(Classe.objects.filter(sync_uuid=object_uuid).exists())


    def test_changement_decole_du_poste_ne_reutilise_pas_un_ancien_acquittement(self):
        event_id = str(uuid4())
        self.envoyer_evenement('École initiale', event_id)
        autre_ecole = Ecole.objects.create(
            nom='Autre école', adresse='Kindia', telephone='600000001', directeur='Direction',
        )
        SyncDevice.objects.filter(pk=self.poste.pk).update(ecole=autre_ecole)
        response = self.envoyer_evenement('École initiale', event_id)
        self.assertEqual(response.json()['accepted_count'], 0)
        self.assertEqual(response.json()['rejected_count'], 1)
        autre_ecole.refresh_from_db()
        self.assertEqual(autre_ecole.nom, 'AUTRE ÉCOLE')
