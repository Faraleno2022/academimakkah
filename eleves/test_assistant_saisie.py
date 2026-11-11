"""Tests de l'assistant IA du formulaire « Ajouter un élève » (l'appel à Claude est simulé)."""

from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from assistant_ia.claude import AssistantIAError

from .models import Classe, Ecole, Eleve, Responsable


def _lecture(**modifs):
    lu = {
        'prenom': 'Aminata', 'nom': 'Diallo', 'sexe': 'F', 'date_naissance': '2014-03-12',
        'lieu_naissance': 'Kindia', 'classe_id': None,
        'responsable': {
            'prenom': 'Mamadou', 'nom': 'Diallo', 'relation': 'PERE', 'telephone': '+224 622 61 35 59',
            'telephone_secondaire': '', 'email': '', 'profession': '', 'adresse': 'Ratoma',
        },
        'champs_douteux': [], 'remarque': '',
    }
    lu.update(modifs)
    return lu


@override_settings(
    MIDDLEWARE=[m for m in settings.MIDDLEWARE if m != 'ecole_moderne.licence_middleware.LicenceMiddleware'],
    PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'],
)
class AssistantEleveTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.ecole = Ecole.objects.create(nom='École assistant', adresse='Conakry', telephone='+224620000001', directeur='Direction', etat='VALIDE')
        cls.autre_ecole = Ecole.objects.create(nom='Autre école', adresse='Conakry', telephone='+224620000002', directeur='Direction', etat='VALIDE')
        cls.user = get_user_model().objects.create_user(username='secretaire-ia', password='test')
        profil = cls.user.profil
        profil.ecole, profil.est_compte_principal, profil.is_validated = cls.ecole, True, True
        profil.save()

        cls.classe = Classe.objects.create(ecole=cls.ecole, nom='7A', niveau='COLLEGE_7', annee_scolaire='2026-2027')
        cls.autre_classe = Classe.objects.create(ecole=cls.autre_ecole, nom='7B', niveau='COLLEGE_7', annee_scolaire='2026-2027')
        cls.parent = Responsable.objects.create(prenom='Mamadou', nom='Diallo', relation='PERE', telephone='+224622613559', adresse='Ratoma')
        cls.inscrite = Eleve.objects.create(matricule='7A-009', prenom='Aminata', nom='Diallo', sexe='F', classe=cls.classe, responsable_principal=cls.parent, statut='ACTIF')

    def setUp(self):
        self.client.force_login(self.user)

    def _poster(self, lu):
        with mock.patch('eleves.assistant_saisie.extraire_json', return_value=lu) as appel:
            rep = self.client.post(reverse('eleves:assistant_eleve'), {'texte': 'Aminata Diallo, 7A'})
        return rep, appel

    def test_lecture_controlee_doublon_et_responsable_connu(self):
        rep, appel = self._poster(_lecture(classe_id=self.classe.pk))
        self.assertEqual(rep.status_code, 200, rep.content)
        data = rep.json()
        self.assertTrue(data['ok'])
        self.assertEqual(data['lu']['classe_id'], self.classe.pk)
        self.assertEqual(data['lu']['responsable']['telephone'], '622613559')  # sans indicatif
        self.assertEqual([d['matricule'] for d in data['doublons']], ['7A-009'])
        self.assertEqual(data['responsable_connu']['id'], self.parent.pk)

        consigne = appel.call_args.args[0]
        self.assertIn('7A', consigne)
        self.assertNotIn('7B', consigne)  # seules les classes de l'école sont proposées

    def test_classe_d_une_autre_ecole_ignoree_et_date_invalide(self):
        rep, _ = self._poster(_lecture(classe_id=self.autre_classe.pk, date_naissance='12/03/2014', prenom='Ibrahima', nom='Sow'))
        data = rep.json()
        self.assertIsNone(data['lu']['classe_id'])
        self.assertEqual(data['lu']['date_naissance'], '')
        self.assertIn('date_naissance', data['lu']['champs_douteux'])
        self.assertEqual(data['doublons'], [])

    def test_rien_n_est_enregistre(self):
        avant_eleves, avant_resp = Eleve.objects.count(), Responsable.objects.count()
        self._poster(_lecture(prenom='Fatou', nom='Camara'))
        self.assertEqual(Eleve.objects.count(), avant_eleves)
        self.assertEqual(Responsable.objects.count(), avant_resp)

    def test_erreur_assistant_lisible(self):
        with mock.patch('eleves.assistant_saisie.extraire_json', side_effect=AssistantIAError('Pas de connexion internet vers l\'assistant.')):
            rep = self.client.post(reverse('eleves:assistant_eleve'), {'texte': 'x'})
        self.assertEqual(rep.status_code, 400)
        self.assertIn('connexion', rep.json()['erreur'])

    def test_la_page_ajouter_affiche_le_bouton(self):
        rep = self.client.get(reverse('eleves:ajouter_eleve'))
        self.assertEqual(rep.status_code, 200)
        self.assertContains(rep, 'Remplir depuis une photo')
        self.assertContains(rep, reverse('eleves:assistant_eleve'))
