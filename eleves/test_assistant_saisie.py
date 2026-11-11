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
        with mock.patch('eleves.assistant_saisie.extraire_json', return_value={'eleves': lu if isinstance(lu, list) else [lu]}) as appel:
            rep = self.client.post(reverse('eleves:assistant_eleve'), {'texte': 'Aminata Diallo, 7A'})
        return rep, appel

    def test_lecture_controlee_doublon_et_responsable_connu(self):
        rep, appel = self._poster(_lecture(classe_id=self.classe.pk))
        self.assertEqual(rep.status_code, 200, rep.content)
        data = rep.json()
        self.assertTrue(data['ok'])
        un = data['eleves'][0]
        self.assertEqual(un['lu']['classe_id'], self.classe.pk)
        self.assertEqual(un['lu']['responsable']['telephone'], '622613559')  # sans indicatif
        self.assertEqual([d['matricule'] for d in un['doublons']], ['7A-009'])
        self.assertEqual(un['responsable_connu']['id'], self.parent.pk)
        self.assertIn([self.classe.pk, self.classe.nom], data['classes'])

        consigne = appel.call_args.args[0]
        self.assertIn('7A', consigne)
        self.assertNotIn('7B', consigne)  # seules les classes de l'école sont proposées

    def test_classe_d_une_autre_ecole_ignoree_et_date_invalide(self):
        rep, _ = self._poster(_lecture(classe_id=self.autre_classe.pk, date_naissance='12/03/2014', prenom='Ibrahima', nom='Sow'))
        un = rep.json()['eleves'][0]
        self.assertIsNone(un['lu']['classe_id'])
        self.assertEqual(un['lu']['date_naissance'], '')
        self.assertIn('date_naissance', un['lu']['champs_douteux'])
        self.assertEqual(un['doublons'], [])

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

    # ----- Plusieurs élèves : import direct et Excel -----

    def _ligne(self, **modifs):
        ligne = {
            '_index': 0, 'prenom': 'Ibrahima', 'nom': 'Sow', 'sexe': 'M', 'date_naissance': '2013-05-02',
            'lieu_naissance': 'Labé', 'classe_id': str(self.classe.pk),
            'resp_prenom': '', 'resp_nom': '', 'resp_relation': '', 'resp_telephone': '', 'resp_adresse': '',
        }
        ligne.update(modifs)
        return ligne

    def _json(self, nom_url, lignes):
        import json
        return self.client.post(reverse(nom_url), json.dumps({'lignes': lignes}), content_type='application/json')

    def test_import_en_lot_comme_le_formulaire(self):
        rep = self._json('eleves:assistant_eleves_importer', [
            self._ligne(),  # sans responsable
            self._ligne(_index=1, prenom='Fatou', nom='Camara', sexe='F', resp_prenom='Sekou', resp_nom='Camara',
                        resp_relation='PERE', resp_telephone='664 11 22 33', resp_adresse='Kipé'),
            self._ligne(_index=2, prenom='Mariama', nom='Diallo', sexe='F', resp_telephone='622613559'),  # parent connu
            self._ligne(_index=3, prenom='Oumar', nom='Bah', resp_prenom='Alpha'),  # responsable incomplet
            self._ligne(_index=4, prenom='Sans', nom='Classe', classe_id=str(self.autre_classe.pk)),  # autre école
        ])
        self.assertEqual(rep.status_code, 200, rep.content)
        data = rep.json()
        self.assertEqual([c['index'] for c in data['crees']], [0, 1, 2])
        self.assertEqual([e['index'] for e in data['echecs']], [3, 4])
        self.assertIn('incomplet', data['echecs'][0]['erreur'])

        ibrahima = Eleve.objects.get(prenom='IBRAHIMA', nom='SOW')
        self.assertTrue(ibrahima.matricule)  # généré comme par le formulaire
        self.assertEqual(ibrahima.cree_par, self.user)
        self.assertIsNone(ibrahima.responsable_principal)
        fatou = Eleve.objects.get(prenom='FATOU', nom='CAMARA')
        self.assertEqual(fatou.responsable_principal.telephone, '+224664112233')
        self.assertEqual(fatou.responsable_principal.relation, 'PERE')
        self.assertEqual(Eleve.objects.get(prenom='MARIAMA').responsable_principal, self.parent)
        self.assertFalse(Eleve.objects.filter(prenom='OUMAR').exists())
        self.assertFalse(Eleve.objects.filter(classe=self.autre_classe).exists())

    def test_excel_au_format_de_l_import(self):
        from io import BytesIO
        import pandas as pd
        from .import_eleves import COLONNES_EXPORT_COMPLET

        rep = self._json('eleves:assistant_eleves_excel', [
            self._ligne(resp_prenom='Sekou', resp_nom='Sow', resp_telephone='664112233', resp_adresse='Kipé'),
        ])
        self.assertEqual(rep.status_code, 200)
        self.assertIn('spreadsheet', rep['Content-Type'])
        df = pd.read_excel(BytesIO(rep.content), dtype=str).fillna('')
        self.assertEqual(list(df.columns), COLONNES_EXPORT_COMPLET)
        ligne = df.iloc[0]
        self.assertEqual((ligne['Classe'], ligne['Prénom'], ligne['Date de Naissance']), ('7A', 'Ibrahima', '02/05/2013'))
        self.assertEqual(ligne['Téléphone Principal'], '+224664112233')
        self.assertFalse(Eleve.objects.filter(nom='SOW').exists())  # l'Excel n'enregistre rien
