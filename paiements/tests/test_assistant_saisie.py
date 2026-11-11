"""Tests de l'assistant IA de saisie des paiements (l'appel à Claude est simulé)."""

from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from eleves.models import Classe, Ecole, Eleve, Responsable
from paiements.models import ModePaiement, Paiement, TypePaiement


def _lu(**valeurs):
    base = {
        'eleve_nom': '', 'matricule': '', 'classe': '', 'montant': 500000, 'date_paiement': '2026-11-10',
        'type_paiement_id': None, 'mode_paiement_id': None, 'reference': 'PP2611.1234', 'payeur': '',
        'plusieurs_paiements': False, 'champs_douteux': [], 'remarque': '',
    }
    base.update(valeurs)
    return base


@override_settings(
    MIDDLEWARE=[m for m in settings.MIDDLEWARE if m != 'ecole_moderne.licence_middleware.LicenceMiddleware'],
    PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'],
)
class AssistantPaiementTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.ecole = Ecole.objects.create(nom='École paiement IA', adresse='Conakry', telephone='+224620000011', directeur='Direction')
        autre = Ecole.objects.create(nom='Autre école', adresse='Conakry', telephone='+224620000012', directeur='Direction')
        cls.user = get_user_model().objects.create_user(username='compta-ia', password='test')
        profil = cls.user.profil
        profil.ecole, profil.est_compte_principal, profil.is_validated = cls.ecole, True, True
        profil.save()
        resp = Responsable.objects.create(prenom='Parent', nom='Test', relation='PERE', telephone='+224620000013', adresse='Conakry')
        classe = Classe.objects.create(ecole=cls.ecole, nom='7A', niveau='COLLEGE_7', annee_scolaire='2026-2027')
        classe_b = Classe.objects.create(ecole=cls.ecole, nom='8B', niveau='COLLEGE_8', annee_scolaire='2026-2027')
        classe_x = Classe.objects.create(ecole=autre, nom='7A', niveau='COLLEGE_7', annee_scolaire='2026-2027')
        cls.mamadou = Eleve.objects.create(matricule='7A-002', prenom='Mamadou', nom='Bah', sexe='M', classe=classe, responsable_principal=resp, statut='ACTIF')
        cls.fatou = Eleve.objects.create(matricule='8B-001', prenom='Fatoumata', nom='Camara', sexe='F', classe=classe_b, responsable_principal=resp, statut='ACTIF')
        cls.intrus = Eleve.objects.create(matricule='X-777', prenom='Ibrahima', nom='Sow', sexe='M', classe=classe_x, responsable_principal=resp, statut='ACTIF')
        cls.type_t2 = TypePaiement.objects.create(nom='Scolarité - 2ème tranche')
        cls.orange = ModePaiement.objects.create(nom='Orange Money')

    def setUp(self):
        self.client.force_login(self.user)

    def _poster(self, lu):
        with mock.patch('paiements.assistant_saisie.extraire_json', return_value=lu):
            rep = self.client.post(reverse('paiements:assistant_paiement'), {'texte': 'reçu'})
        self.assertEqual(rep.status_code, 200, rep.content)
        return rep.json()

    def test_matricule_exact(self):
        data = self._poster(_lu(matricule='7a-002', type_paiement_id=self.type_t2.pk, mode_paiement_id=self.orange.pk))
        self.assertEqual(data['eleve']['id'], self.mamadou.pk)
        self.assertEqual(data['lu']['type_paiement_id'], self.type_t2.pk)
        self.assertEqual(data['lu']['mode_paiement_id'], self.orange.pk)

    def test_nom_dans_le_desordre(self):
        data = self._poster(_lu(eleve_nom='BAH Mamadou', classe='7A'))
        self.assertEqual(data['eleve']['id'], self.mamadou.pk)

    def test_eleve_d_une_autre_ecole_jamais_propose(self):
        data = self._poster(_lu(eleve_nom='Ibrahima Sow', matricule='X-777'))
        self.assertIsNone(data['eleve'])
        self.assertNotIn(self.intrus.pk, [s['id'] for s in data['suggestions']])

    def test_type_et_mode_inconnus_ignores(self):
        data = self._poster(_lu(eleve_nom='Fatoumata Camara', type_paiement_id=999999, mode_paiement_id=888888))
        self.assertIsNone(data['lu']['type_paiement_id'])
        self.assertIsNone(data['lu']['mode_paiement_id'])

    def test_aucun_paiement_cree(self):
        self._poster(_lu(matricule='7A-002', type_paiement_id=self.type_t2.pk))
        self.assertFalse(Paiement.objects.exists())

    def test_la_page_d_ajout_affiche_le_bouton(self):
        rep = self.client.get(reverse('paiements:ajouter_paiement'))
        self.assertEqual(rep.status_code, 200)
        self.assertContains(rep, 'Remplir depuis une photo')
        self.assertContains(rep, reverse('paiements:assistant_paiement'))
