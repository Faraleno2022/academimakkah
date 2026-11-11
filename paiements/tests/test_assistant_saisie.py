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
        'champs_douteux': [], 'remarque': '',
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
        with mock.patch('paiements.assistant_saisie.extraire_json', return_value={'paiements': lu if isinstance(lu, list) else [lu]}):
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

    # ----- Plusieurs paiements : enregistrement en lot et Excel -----

    def test_plusieurs_paiements_lus(self):
        data = self._poster([_lu(matricule='7A-002'), _lu(eleve_nom='Fatoumata Camara', montant=300000)])
        self.assertEqual(len(data['paiements']), 2)
        self.assertEqual(data['paiements'][1]['eleve']['id'], self.fatou.pk)
        self.assertEqual(data['eleve']['id'], self.mamadou.pk)  # le premier reste à la racine
        self.assertIn([self.orange.pk, 'Orange Money'], data['modes'])

    def _echeancier(self, eleve):
        from datetime import date
        from decimal import Decimal
        from paiements.models import EcheancierPaiement

        EcheancierPaiement.objects.create(
            eleve=eleve, annee_scolaire='2026-2027', frais_inscription_du=Decimal('100000'),
            tranche_1_due=Decimal('200000'), tranche_2_due=Decimal('200000'), tranche_3_due=Decimal('200000'),
            date_echeance_inscription=date(2026, 9, 30), date_echeance_tranche_1=date(2026, 12, 31),
            date_echeance_tranche_2=date(2027, 3, 31), date_echeance_tranche_3=date(2027, 6, 30),
        )

    def _json(self, nom_url, lignes):
        import json
        return self.client.post(reverse(nom_url), json.dumps({'lignes': lignes}), content_type='application/json')

    def test_enregistrement_en_lot_par_la_vue_habituelle(self):
        self._echeancier(self.mamadou)
        self._echeancier(self.fatou)
        tranche = TypePaiement.objects.create(nom='Tranche 1')
        base = {'date_paiement': '2026-11-10', 'type_paiement_id': str(tranche.pk), 'mode_paiement_id': str(self.orange.pk), 'reference': 'R1'}
        with mock.patch('paiements.views.send_payment_receipt'), mock.patch('paiements.views.send_enrollment_confirmation'):
            rep = self._json('paiements:assistant_paiements_importer', [
                {**base, '_index': 0, 'matricule': '7a-002', 'montant': '150000'},
                {**base, '_index': 1, 'matricule': '8B-001', 'montant': '99999999'},  # dépasse le reste dû
                {**base, '_index': 2, 'matricule': 'X-777', 'montant': '50000'},      # autre école
                {**base, '_index': 3, 'matricule': '8B-001', 'montant': ''},          # montant manquant
            ])
        self.assertEqual(rep.status_code, 200, rep.content)
        data = rep.json()
        self.assertEqual([c['index'] for c in data['crees']], [0], data)
        self.assertEqual([e['index'] for e in data['echecs']], [1, 2, 3])
        self.assertIn('trop élevé', data['echecs'][0]['erreur'])
        self.assertIn('introuvable', data['echecs'][1]['erreur'])

        paiement = Paiement.objects.get()
        self.assertEqual((paiement.eleve, int(paiement.montant), paiement.statut), (self.mamadou, 150000, 'EN_ATTENTE'))
        self.assertEqual(paiement.cree_par, self.user)
        self.assertTrue(paiement.numero_recu)
        self.assertEqual(data['crees'][0]['recu'], paiement.numero_recu)

        # Les messages de la vue ne débordent pas sur la page suivante
        page = self.client.get(reverse('paiements:ajouter_paiement'))
        self.assertNotContains(page, 'Paiement enregistré avec succès')

    def test_excel_recapitulatif(self):
        from io import BytesIO
        from openpyxl import load_workbook

        rep = self._json('paiements:assistant_paiements_excel', [
            {'matricule': '7A-002', 'montant': '150000', 'date_paiement': '2026-11-10',
             'type_paiement_id': str(self.type_t2.pk), 'mode_paiement_id': str(self.orange.pk), 'reference': 'R1'},
        ])
        self.assertEqual(rep.status_code, 200)
        lignes = list(load_workbook(BytesIO(rep.content)).active.values)
        self.assertEqual(lignes[0][0], 'Matricule')
        self.assertEqual(lignes[1][3], 150000)
        self.assertEqual(lignes[1][6], 'Orange Money')
        self.assertFalse(Paiement.objects.exists())
