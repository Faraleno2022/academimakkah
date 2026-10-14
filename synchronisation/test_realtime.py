"""Propagation entre bases indépendantes et actualisation des pages ouvertes."""
import copy
import json
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import User
from django.contrib.sessions.backends.signed_cookies import SessionStore
from django.core.cache import cache
from django.db import connections
from django.db.backends.sqlite3.base import DatabaseWrapper
from django.http import HttpResponse
from django.test import Client, RequestFactory, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from bus.models import AbonnementBus, AbonnementCantine
from eleves.models import Classe, Ecole, Eleve
from paiements.tests.support import MIDDLEWARE_SANS_LICENCE
from utilisateurs.models import Profil

from . import autosync
from .client import _get_session, pull_changes, push_pending
from .engine import apply_sync_change, serialize_instance
from .models import SyncChange, SyncCheckpoint, SyncDevice
from .realtime import SyncPageRevisionMiddleware, revision_ecole, revision_pour_requete
from .registry import REGISTRE_VERSION


def creer_ecole(nom='École propagation'):
    return Ecole.objects.create(nom=nom, adresse='Conakry', telephone='600000100',
                                directeur='Direction', etat='VALIDE')


@override_settings(MIDDLEWARE=MIDDLEWARE_SANS_LICENCE)
class RevisionAffichageTests(TestCase):
    def setUp(self):
        cache.clear()
        self.ecole = creer_ecole()
        self.user = User.objects.create_user(username='revision', password='test')
        Profil.objects.update_or_create(user=self.user, defaults={
            'role': 'ADMIN', 'ecole': self.ecole, 'is_validated': True,
        })
        self.user = User.objects.get(pk=self.user.pk)
        self.client.force_login(self.user)

    def test_revision_authentifiee_et_limitee_a_son_ecole(self):
        url = reverse('synchronisation:revision')
        avant = self.client.get(url)
        self.assertEqual(avant.status_code, 200)
        self.assertIn('no-store', avant['Cache-Control'])
        creer_ecole('Autre école')
        self.assertEqual(self.client.get(url).json()['revision'], avant.json()['revision'])
        self.ecole.nom = 'Nom modifié'
        self.ecole.save()
        self.assertNotEqual(self.client.get(url).json()['revision'], avant.json()['revision'])
        self.client.logout()
        self.assertEqual(self.client.get(url).status_code, 302)

    def test_utilisateur_sans_ecole_ne_voit_pas_la_revision_globale(self):
        Profil.objects.filter(user=self.user).update(ecole=None)
        self.assertEqual(self.client.get(reverse('synchronisation:revision')).status_code, 403)

    def test_reessai_d_un_ancien_changement_actualise_la_revision(self):
        ancien = SyncChange.objects.filter(ecole=self.ecole).first()
        self.ecole.nom = 'Deuxième version'
        self.ecole.save()
        avant = revision_ecole(self.ecole)
        SyncChange.objects.filter(pk=ancien.pk).update(
            statut=SyncChange.STATUT_APPLIED, date_application=timezone.now(),
        )
        self.assertNotEqual(revision_ecole(self.ecole), avant)

    def test_revision_capturee_avant_le_rendu_meme_si_une_donnee_arrive_pendant(self):
        request = RequestFactory().get('/bus/liste/')
        request.user = self.user
        avant = revision_ecole(self.ecole)

        def vue(req):
            self.ecole.nom = 'Arrivée pendant le rendu'
            self.ecole.save()
            return HttpResponse(revision_pour_requete(req))

        response = SyncPageRevisionMiddleware(vue)(request)
        self.assertEqual(response.content.decode(), avant)
        self.assertNotEqual(revision_ecole(self.ecole), avant)

    def test_statistiques_et_classes_en_cache_s_actualisent_apres_reception(self):
        Profil.objects.filter(user=self.user).update(role='SECRETAIRE', allowed_menus=['eleves'])
        url = reverse('eleves:ajouter_eleve')
        avant = self.client.get(url)
        self.assertEqual(avant.status_code, 200)
        self.assertEqual(avant.context['stats']['total_eleves'], 0)
        classe = Classe.objects.create(ecole=self.ecole, nom='CP temps réel',
                                       niveau='PRIMAIRE_1', annee_scolaire='2026-2027')
        eleve = Eleve(classe=classe, nom='Données reçues', prenom='Test', matricule='SYNC-TEST')
        change = SyncChange.objects.create(ecole=self.ecole, model_label='eleves.Eleve',
            object_uuid=eleve.sync_uuid, operation='CREATE', payload=serialize_instance(eleve))
        apply_sync_change(change)
        apres = self.client.get(url)
        self.assertEqual(apres.context['stats']['total_eleves'], 1)
        self.assertIn(classe, apres.context['form'].fields['classe'].queryset)
        self.assertContains(apres, 'js/sync-refresh.js')
        self.assertContains(apres, 'data-sync-revision=')

    def test_moyenne_classement_et_rang_en_cache_changent_apres_note_recue(self):
        from notes.models import ClasseNote, MatiereNote, NoteMensuelle
        from notes.calculs_moyennes import calculer_moyennes_classe_optimise, calculer_classement_classe
        from notes.utils_rangs import calculer_rangs_classe_periode

        classe = Classe.objects.create(ecole=self.ecole, nom='8ème Année',
                                       niveau='COLLEGE_8', annee_scolaire='2026-2027')
        eleve = Eleve.objects.create(classe=classe, nom='Calcul reçu', prenom='Test', matricule='NOTE-SYNC')
        cn = ClasseNote.objects.create(ecole=self.ecole, nom=classe.nom, niveau=classe.niveau,
                                      annee_scolaire=classe.annee_scolaire)
        matiere = MatiereNote.objects.create(classe=cn, nom='Français', code='FR', coefficient=1)
        note = NoteMensuelle.objects.create(eleve=eleve, matiere=matiere, note=12,
                                           mois='OCTOBRE', annee_scolaire=classe.annee_scolaire)

        def moyennes():
            return (
                calculer_moyennes_classe_optimise([eleve], [matiere], 'OCTOBRE')[eleve.pk]['moyenne_generale'],
                calculer_classement_classe([eleve], [matiere], 'OCTOBRE')['moyennes_par_eleve'][eleve.pk],
                calculer_rangs_classe_periode(cn, 'OCTOBRE')[eleve.pk]['moyenne'],
            )

        self.assertEqual(moyennes(), (12, 12, 12))
        payload = serialize_instance(note)
        payload['note'] = '18'
        change = SyncChange.objects.create(ecole=self.ecole, model_label='notes.NoteMensuelle',
            object_uuid=note.sync_uuid, operation='UPDATE', payload=payload)
        apply_sync_change(change)
        self.assertEqual(moyennes(), (18, 18, 18))

    def test_reveil_declenche_apres_validation_de_la_transaction(self):
        with patch('synchronisation.autosync.notifier_changement') as reveil:
            with self.captureOnCommitCallbacks(execute=True):
                self.ecole.nom = 'Validée'
                self.ecole.save()
                reveil.assert_not_called()
            reveil.assert_called_once()

    def test_sondage_ne_prolonge_pas_la_session_et_conserve_le_controle_d_expiration(self):
        from ecole_moderne.security_middleware import SessionSecurityMiddleware

        request = RequestFactory().get(reverse('synchronisation:revision'))
        request.user = self.user
        ancien = time.time() - 120
        request.session = SessionStore()
        request.session['last_activity'] = ancien
        request.session.modified = False
        middleware = SessionSecurityMiddleware(lambda req: HttpResponse())
        self.assertIsNone(middleware.process_request(request))
        self.assertEqual(request.session['last_activity'], ancien)
        self.assertFalse(request.session.modified)
        request.session['last_activity'] = time.time() - 1801
        self.assertEqual(middleware.process_request(request).status_code, 302)


class TachesIndependantesTests(TestCase):
    def test_demarrage_des_taches_donnees_et_medias_une_seule_fois(self):
        with patch.object(autosync, '_worker_demarre', False), patch.object(autosync.threading, 'Thread') as thread:
            self.assertTrue(autosync.demarrer_worker('base', delai_demarrage=0))
            self.assertFalse(autosync.demarrer_worker('base'))
            self.assertEqual([c.kwargs['args'][2] for c in thread.call_args_list], ['push', 'pull', 'media'])
            self.assertEqual(thread.return_value.start.call_count, 3)

    def test_reception_continue_pendant_un_envoi_bloque(self):
        started, release, received = threading.Event(), threading.Event(), threading.Event()
        config = dict(server_url='https://sync.invalid', device_id='test', token='test', ecole_id='1', interval=2)
        erreurs = []

        def envoi(*args, **kwargs):
            started.set()
            if not release.wait(3):
                erreurs.append('Envoi non libéré')
            return 1

        def reception(*args, **kwargs):
            received.set()
            return 1

        with patch.object(autosync, 'charger_config', return_value=config), \
                patch('eleves.models.Ecole.objects.first', return_value=object()), \
                patch('synchronisation.models.SyncCheckpoint.objects.filter') as checkpoint, \
                patch.object(autosync, '_emettre_modeles_ajoutes'), \
                patch('synchronisation.client.reprogrammer_refus_temporaires'), \
                patch('synchronisation.client.push_pending', envoi), \
                patch('synchronisation.client.pull_changes', reception), \
                patch('synchronisation.client.rattraper_modeles_ajoutes', return_value=0):
            checkpoint.return_value.first.return_value = None
            push = threading.Thread(target=autosync._cycle, args=('.', 'push'))
            pull = threading.Thread(target=autosync._cycle, args=('.', 'pull'))
            push.start()
            try:
                self.assertTrue(started.wait(1))
                pull.start()
                self.assertTrue(received.wait(1), 'La réception attend encore la fin de l’envoi')
                self.assertTrue(push.is_alive())
            finally:
                release.set()
                push.join(3)
                if pull.ident:
                    pull.join(3)
        self.assertEqual(erreurs, [])

    def test_connexions_http_persistantes_distinctes_par_tache(self):
        sessions = []

        def connexion():
            session = _get_session()
            sessions.append((session, _get_session()))

        for _ in range(2):
            thread = threading.Thread(target=connexion)
            thread.start()
            thread.join(2)
        try:
            self.assertEqual(len(sessions), 2)
            self.assertTrue(all(a is b for a, b in sessions))
            self.assertIsNot(sessions[0][0], sessions[1][0])
        finally:
            for session, _ in sessions:
                session.close()

    def test_lots_suivants_sans_attente_et_reveil_pendant_cycle_conserve(self):
        event = threading.Event()

        def cycle(*args, **kwargs):
            event.set()  # Nouvelle saisie juste après la sélection du lot.
            return {'interval': 2}, 100, 0

        def wait(timeout):
            self.assertEqual(timeout, 0.1)
            self.assertTrue(event.is_set())
            raise StopIteration

        with patch.object(autosync, '_reveil', event), patch.object(event, 'wait', wait), \
                patch.object(autosync, '_cycle', cycle), patch.object(autosync, 'close_old_connections'), \
                patch.object(autosync.time, 'sleep'):
            with self.assertRaises(StopIteration):
                autosync._boucle('.', 0, 'push')


@override_settings(MIDDLEWARE=MIDDLEWARE_SANS_LICENCE)
class PropagationTroisPostesTests(TransactionTestCase):
    """Un serveur et trois SQLite distincts, transporté via les vraies vues API.

    Seul le transport réseau est remplacé. ORM, signaux, sérialisation, API,
    acquittement, curseurs et pages métier sont exécutés normalement.
    """
    def test_eleve_bus_cantine_et_corrections_arrivent_partout_sans_boucle(self):
        cache.clear()
        serveur = connections['default']
        ecole = creer_ecole()
        classe = Classe.objects.create(ecole=ecole, nom='CP', niveau='PRIMAIRE_1', annee_scolaire='2026-2027')
        user = User.objects.create_user(username='trois-postes', password='test')
        Profil.objects.update_or_create(user=user, defaults={
            'role': 'ADMIN', 'ecole': ecole, 'is_validated': True, 'allowed_menus': ['bus', 'eleves'],
        })
        postes = {}
        for nom in ('A', 'B', 'C'):
            device = SyncDevice(ecole=ecole, nom=f'Poste {nom}')
            device.definir_token(f'test-{nom}')
            device.save()
            postes[nom] = device
        SyncChange.objects.all().delete()
        mesures = {}

        with tempfile.TemporaryDirectory(prefix='sync-trois-postes-') as dossier:
            bases = {}
            try:
                for nom in postes:
                    config = copy.deepcopy(serveur.settings_dict)
                    config['NAME'] = str(Path(dossier, f'{nom}.sqlite3'))
                    base = DatabaseWrapper(config, alias='default')
                    base.ensure_connection()
                    serveur.connection.backup(base.connection)
                    bases[nom] = base

                @contextmanager
                def sur(base):
                    precedent = connections['default']
                    connections['default'] = base
                    try:
                        yield
                    finally:
                        connections['default'] = precedent

                def transport(url, device_id, token, params=None, payload=None, **kwargs):
                    with sur(serveur):
                        client = Client()
                        headers = dict(HTTP_X_SYNC_DEVICE=str(device_id), HTTP_X_SYNC_TOKEN=token)
                        chemin = url.split('https://sync.invalid', 1)[1]
                        response = (client.get(chemin, params or {}, **headers) if payload is None else
                                    client.post(chemin, data=payload, content_type='application/json', **headers))
                        self.assertEqual(response.status_code, 200, response.content)
                        return response.json()

                def post(url, device_id, token, payload, **kwargs):
                    return transport(url, device_id, token, payload=payload, **kwargs)

                def envoyer(nom):
                    with sur(bases[nom]):
                        device = postes[nom]
                        return push_pending('https://sync.invalid', str(device.device_id), f'test-{nom}', ecole)

                def recevoir(nom):
                    with sur(bases[nom]):
                        device = postes[nom]
                        return pull_changes('https://sync.invalid', str(device.device_id), f'test-{nom}', ecole)

                for nom, base in bases.items():
                    with sur(base):
                        SyncCheckpoint.objects.create(device_id=postes[nom].device_id,
                            initial_complete=True, registre=REGISTRE_VERSION)

                with patch('synchronisation.client._get_json', transport), patch('synchronisation.client._post_json', post):
                    debut = time.perf_counter()
                    with sur(bases['A']):
                        eleve = Eleve.objects.create(classe_id=classe.pk, nom='Propagation',
                                                    prenom='Élève', matricule='PROP-001', sexe='F')
                    self.assertGreater(envoyer('A'), 0)
                    for nom in bases:
                        self.assertGreater(recevoir(nom), 0)
                    mesures['eleve_A_vers_serveur_B_C_ms'] = round((time.perf_counter() - debut) * 1000, 1)

                    debut = time.perf_counter()
                    with sur(bases['B']):
                        local = Eleve.objects.get(sync_uuid=eleve.sync_uuid)
                        bus = AbonnementBus.objects.create(eleve=local, montant=100000,
                            date_debut=date(2026, 10, 12), date_expiration=date(2026, 11, 12))
                        cantine = AbonnementCantine.objects.create(eleve=local, montant=200000,
                            montant_tarif=300000, type_remise='POURCENTAGE', valeur_remise=10,
                            date_debut=date(2026, 10, 12), date_expiration=date(2027, 1, 12))
                    self.assertGreaterEqual(envoyer('B'), 2)
                    for nom in bases:
                        recevoir(nom)
                    mesures['bus_cantine_B_vers_serveur_A_C_ms'] = round((time.perf_counter() - debut) * 1000, 1)

                    debut = time.perf_counter()
                    with sur(bases['C']):
                        correction = AbonnementCantine.objects.get(sync_uuid=cantine.sync_uuid)
                        correction.montant = 250000
                        correction.save()
                    self.assertGreater(envoyer('C'), 0)
                    with sur(serveur):
                        central = Eleve.objects.get(sync_uuid=eleve.sync_uuid)
                        central.nom = 'Modification serveur'
                        central.save()
                    for nom in bases:
                        recevoir(nom)
                    mesures['correction_C_et_serveur_vers_tous_ms'] = round((time.perf_counter() - debut) * 1000, 1)

                    for nom, base in {'serveur': serveur, **bases}.items():
                        with self.subTest(base=nom), sur(base):
                            self.assertEqual(Eleve.objects.get(sync_uuid=eleve.sync_uuid).nom, 'MODIFICATION SERVEUR')
                            self.assertEqual(AbonnementBus.objects.get(sync_uuid=bus.sync_uuid).montant, 100000)
                            recu = AbonnementCantine.objects.get(sync_uuid=cantine.sync_uuid)
                            self.assertEqual((recu.montant_du, recu.deja_paye, recu.reste_a_payer), (270000, 250000, 20000))
                            self.assertFalse(SyncChange.objects.filter(statut=SyncChange.STATUT_FAILED).exists())
                            revision = revision_ecole(ecole)
                            lecteur = Client()
                            lecteur.force_login(User.objects.get(pk=user.pk))
                            page = lecteur.get(reverse('bus:liste_abonnements_cantine'))
                            self.assertEqual(page.status_code, 200)
                            self.assertContains(page, 'MODIFICATION SERVEUR')
                            self.assertContains(page, f'data-sync-revision="{revision}"')
                            self.assertContains(page, '250')
                            page_bus = lecteur.get(reverse('bus:liste'))
                            self.assertContains(page_bus, 'MODIFICATION SERVEUR')
                            self.assertContains(page_bus, 'js/sync-refresh.js')
                    for nom in bases:
                        self.assertEqual(envoyer(nom), 0, 'Un changement reçu repart en boucle')
                        self.assertEqual(recevoir(nom), 0, 'Un changement déjà reçu est appliqué deux fois')
            finally:
                connections['default'] = serveur
                for base in bases.values():
                    base.close()
        print('\n[Mesure locale 3 postes / sans réseau] ' + json.dumps(mesures, ensure_ascii=False))
