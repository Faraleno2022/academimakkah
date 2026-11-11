"""Assistant IA des modules Bus, Cantine, Avances de salaire et Test d'accueil (Claude simulé)."""

import json
from datetime import date
from decimal import Decimal
from io import BytesIO
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from bus.models import AbonnementBus, AbonnementCantine, GrilleTarifaireBus
from eleves.models import Classe, Ecole, Eleve
from examen_accueil.models import MatiereAccueil, NoteAccueil
from paiements.models import ModePaiement
from salaires.models import AvanceSalaire, Enseignant, EtatSalaire, PeriodeSalaire, TypeEnseignant
from utilisateurs.models import Profil

SANS_LICENCE = [m for m in settings.MIDDLEWARE if 'licence_middleware' not in m]
DOUTE = {'champs_douteux': [], 'remarque': ''}


@override_settings(MIDDLEWARE=SANS_LICENCE, PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.ecole = Ecole.objects.create(nom='École lot', adresse='Conakry', telephone='+224620000501', directeur='Direction')
        cls.autre = Ecole.objects.create(nom='Autre lot', adresse='Conakry', telephone='+224620000502', directeur='Direction')
        cls.classe = Classe.objects.create(ecole=cls.ecole, nom='7A', niveau='COLLEGE_7', annee_scolaire='2026-2027')
        classe_x = Classe.objects.create(ecole=cls.autre, nom='7A', niveau='COLLEGE_7', annee_scolaire='2026-2027')
        cls.mamadou = Eleve.objects.create(matricule='L-001', prenom='Mamadou', nom='Bah', sexe='M', classe=cls.classe)
        cls.fatou = Eleve.objects.create(matricule='L-002', prenom='Fatoumata', nom='Camara', sexe='F', classe=cls.classe)
        cls.intrus = Eleve.objects.create(matricule='X-900', prenom='Ibrahima', nom='Sow', sexe='M', classe=classe_x)
        cls.mode = ModePaiement.objects.create(nom='Espèces lot')
        cls.user = get_user_model().objects.create_user(username='caisse-lot', password='x')
        Profil.objects.update_or_create(user=cls.user, defaults={
            'role': 'ADMIN', 'telephone': '+224620000503', 'ecole': cls.ecole, 'is_validated': True,
            'est_compte_principal': True,
        })

    def setUp(self):
        self.client.force_login(self.user)

    def lire(self, module, nom_url, reponse_ia, **donnees):
        with mock.patch(f'{module}.extraire_json', return_value=reponse_ia) as appel:
            rep = self.client.post(reverse(nom_url), {'texte': 'document', **donnees})
        return rep, appel

    def envoyer(self, nom_url, **corps):
        return self.client.post(reverse(nom_url), json.dumps(corps), content_type='application/json')

    def classeur(self, rep):
        from openpyxl import load_workbook

        self.assertEqual(rep.status_code, 200, rep.content[:300])
        self.assertIn('spreadsheet', rep['Content-Type'])
        return list(load_workbook(BytesIO(rep.content)).active.values)


class BusTests(Base):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.grille = GrilleTarifaireBus.objects.create(
            ecole=cls.ecole, zone='Kipé', annee_scolaire='2026-2027',
            tranche_1=150000, tranche_2=125000, tranche_3=125000,
            date_echeance_tranche_1=date(2026, 10, 15), date_echeance_tranche_2=date(2027, 1, 15),
            date_echeance_tranche_3=date(2027, 4, 15),
        )

    def _lu(self, **v):
        return {'eleve_nom': '', 'matricule': '', 'classe': '', 'montant': 150000, 'date': '2026-11-10',
                'grille_id': self.grille.pk, 'tranche': 'T1', 'mode_paiement_id': self.mode.pk,
                'reference': 'OM1', **DOUTE, **v}

    def test_lecture_prepare_la_table(self):
        rep, appel = self.lire('bus.assistant_saisie', 'bus:assistant_bus', {'paiements': [
            self._lu(eleve_nom='BAH Mamadou'), self._lu(eleve_nom='Sow Ibrahima', matricule='X-900'),
        ]})
        self.assertEqual(rep.status_code, 200, rep.content)
        data = rep.json()
        self.assertEqual([c['cle'] for c in data['colonnes']][:3], ['matricule', 'grille_id', 'tranche'])
        self.assertEqual(data['lignes'][0]['valeurs']['matricule'], 'L-001')
        self.assertTrue(data['lignes'][0]['coche'])
        self.assertFalse(data['lignes'][1]['coche'])  # élève d'une autre école : jamais proposé
        self.assertIn('non retrouvé', data['lignes'][1]['alerte'])
        self.assertIn('Kipé', appel.call_args.args[0])
        self.assertFalse(AbonnementBus.objects.exists())

    def test_enregistrement_en_lot_par_la_vue_habituelle(self):
        base = {'grille_id': str(self.grille.pk), 'tranche': 'T1', 'date': '2026-11-10',
                'mode_paiement_id': str(self.mode.pk), 'reference': 'OM1'}
        rep = self.envoyer('bus:assistant_bus_importer', lignes=[
            {**base, '_index': 0, 'matricule': 'l-001', 'montant': '150 000'},
            {**base, '_index': 1, 'matricule': 'L-002', 'montant': '900000'},   # dépasse le reste à payer
            {**base, '_index': 2, 'matricule': 'X-900', 'montant': '150000'},   # autre école
        ])
        data = rep.json()
        self.assertEqual([c['index'] for c in data['crees']], [0], data)
        self.assertEqual([e['index'] for e in data['echecs']], [1, 2])
        self.assertIn('dépasse', data['echecs'][0]['erreur'])
        abo = AbonnementBus.objects.get()
        self.assertEqual((abo.eleve, int(abo.montant), abo.cree_par, abo.zone), (self.mamadou, 150000, self.user, 'Kipé'))
        self.assertIn(abo.numero_recu, data['crees'][0]['texte'])
        self.assertNotContains(self.client.get(reverse('bus:nouveau')), 'Paiement bus enregistré')

    def test_excel_et_bouton(self):
        lignes = self.classeur(self.envoyer('bus:assistant_bus_excel', lignes=[
            {'matricule': 'L-001', 'grille_id': str(self.grille.pk), 'tranche': 'T1', 'montant': '150000', 'date': '2026-11-10'},
        ]))
        self.assertEqual(lignes[1][:6], ('L-001', 'Mamadou Bah'.upper() if lignes[1][1].isupper() else lignes[1][1], '7A',
                                         'Kipé (2026-2027)', '1ère tranche', 150000))
        page = self.client.get(reverse('bus:nouveau'))
        self.assertContains(page, 'ouvrirAssistantBus')
        self.assertContains(page, reverse('bus:assistant_bus_importer'))


class CantineTests(Base):
    def test_lecture_import_excel_et_bouton(self):
        lu = {'eleve_nom': 'Camara Fatoumata', 'matricule': '', 'classe': '7A', 'montant': 150000,
              'date': '2026-11-05', 'periodicite': 'MENSUEL', 'type_repas': '', 'reference': '', **DOUTE}
        rep, _ = self.lire('bus.assistant_saisie', 'bus:assistant_cantine', {'abonnements': [lu]})
        ligne = rep.json()['lignes'][0]
        self.assertEqual((ligne['valeurs']['matricule'], ligne['valeurs']['type_repas']), ('L-002', 'DEJEUNER'))
        self.assertIn('type_repas', ligne['douteux'])

        valeurs = {**ligne['valeurs'], '_index': 0}
        rep = self.envoyer('bus:assistant_cantine_importer', lignes=[valeurs, {**valeurs, '_index': 1, 'matricule': 'X-900'}])
        data = rep.json()
        self.assertEqual(([c['index'] for c in data['crees']], [e['index'] for e in data['echecs']]), ([0], [1]), data)
        abo = AbonnementCantine.objects.get()
        self.assertEqual((abo.eleve, int(abo.montant), abo.periodicite, abo.statut), (self.fatou, 150000, 'MENSUEL', 'ACTIF'))
        self.assertGreater(abo.date_expiration, date(2026, 11, 5))

        self.assertEqual(self.classeur(self.envoyer('bus:assistant_cantine_excel', lignes=[valeurs]))[1][3], 'Mensuel')
        self.assertContains(self.client.get(reverse('bus:creer_abonnement_cantine')), 'ouvrirAssistantCantine')


class AvancesTests(Base):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # Comme la page « Ajouter une avance » : seul un non-administrateur est limité à son école
        Profil.objects.filter(user=cls.user).update(role='COMPTABLE')
        cls.periode = PeriodeSalaire.objects.create(mois=11, annee=2026, ecole=cls.ecole, nombre_semaines=Decimal('4'), cree_par=cls.user)
        cls.prof = Enseignant.objects.create(
            nom='Diallo', prenoms='Ibrahima', ecole=cls.ecole, type_enseignant=TypeEnseignant.PRIMAIRE, statut='ACTIF',
            salaire_fixe=Decimal('1000000'), heures_mensuelles=Decimal('160'), date_embauche=date(2025, 1, 1), cree_par=cls.user,
        )
        cls.prof_ailleurs = Enseignant.objects.create(
            nom='Keita', prenoms='Sekou', ecole=cls.autre, type_enseignant=TypeEnseignant.PRIMAIRE, statut='ACTIF',
            salaire_fixe=Decimal('1000000'), heures_mensuelles=Decimal('160'), date_embauche=date(2025, 1, 1), cree_par=cls.user,
        )

    def test_lecture_import_excel_et_bouton(self):
        lu = {'personne': 'Ibrahima DIALLO', 'enseignant_id': None, 'montant': 200000, 'date': '2026-11-10',
              'mode_paiement_id': None, 'reference': '', 'motif': 'Besoin familial', **DOUTE}
        rep, appel = self.lire('salaires.assistant_saisie', 'salaires:assistant_avances', {'avances': [
            lu, {**lu, 'personne': 'Sekou Keita', 'enseignant_id': self.prof_ailleurs.pk},
        ]})
        self.assertEqual(rep.status_code, 200, rep.content)
        lignes = rep.json()['lignes']
        self.assertEqual(lignes[0]['valeurs']['enseignant_id'], self.prof.pk)  # retrouvé par le nom
        self.assertEqual(lignes[0]['valeurs']['periode_id'], self.periode.pk)
        self.assertEqual(lignes[1]['valeurs']['enseignant_id'], '')            # personnel d'une autre école
        self.assertFalse(lignes[1]['coche'])
        self.assertNotIn('Keita', appel.call_args.args[0])

        valeurs = {**lignes[0]['valeurs'], '_index': 0}
        rep = self.envoyer('salaires:assistant_avances_importer', lignes=[
            valeurs, {**valeurs, '_index': 1, 'enseignant_id': str(self.prof_ailleurs.pk)}, {**valeurs, '_index': 2, 'montant': ''},
        ])
        data = rep.json()
        self.assertEqual(([c['index'] for c in data['crees']], [e['index'] for e in data['echecs']]), ([0], [1, 2]), data)
        avance = AvanceSalaire.objects.get()
        self.assertEqual((avance.enseignant, int(avance.montant), avance.cree_par), (self.prof, 200000, self.user))
        etat = EtatSalaire.objects.get(enseignant=self.prof, periode=self.periode)
        self.assertEqual(etat.avances, Decimal('200000.00'))  # salaire net recalculé, comme sur la page

        self.assertEqual(self.classeur(self.envoyer('salaires:assistant_avances_excel', lignes=[valeurs]))[1][2], 200000)
        self.assertContains(self.client.get(reverse('salaires:ajouter_avance')), 'ouvrirAssistantAvances')


class TestAccueilTests(Base):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.francais = MatiereAccueil.objects.create(classe=cls.classe, nom='Français')
        cls.maths = MatiereAccueil.objects.create(classe=cls.classe, nom='Mathématiques')

    def _note(self, eleve, matiere, note, **v):
        return {'eleve_id': eleve.pk, 'matiere_id': matiere.pk, 'note': note, 'absent': False, 'douteux': False, 'remarque': '', **v}

    def test_lecture_excel_reimportable_et_bouton(self):
        rep, appel = self.lire('examen_accueil.assistant_saisie', 'examen_accueil:assistant_notes', {
            'notes': [self._note(self.mamadou, self.francais, 14.5), self._note(self.fatou, self.maths, 99),
                      self._note(self.intrus, self.francais, 10)],
            'non_reconnus': ['Sow 12'],
        }, classe=self.classe.pk, tous='1')
        self.assertEqual(rep.status_code, 200, rep.content)
        data = rep.json()
        self.assertEqual(len(data['notes']), 2)  # l'élève d'une autre école est écarté
        self.assertTrue(data['notes'][1]['douteux'])  # hors barème
        self.assertNotIn('IBRAHIMA', appel.call_args.args[0].upper())
        self.assertFalse(NoteAccueil.objects.exists())

        rep = self.envoyer('examen_accueil:assistant_notes_excel', classe=self.classe.pk, notes=[
            {'eleve_id': self.mamadou.pk, 'matiere_id': self.francais.pk, 'note': '14,5'},
            {'eleve_id': self.fatou.pk, 'matiere_id': self.maths.pk, 'note': '9'},
        ])
        self.classeur(rep)
        from django.core.files.uploadedfile import SimpleUploadedFile
        fichier = SimpleUploadedFile('notes.xlsx', rep.content)
        self.client.post(reverse('examen_accueil:importer_notes', args=[self.classe.pk]), {'fichier': fichier, 'tous': '1'})
        self.assertEqual(NoteAccueil.objects.count(), 2)
        self.assertEqual(NoteAccueil.objects.get(eleve=self.mamadou).note, Decimal('14.50'))

        page = self.client.get(reverse('examen_accueil:saisie_notes'), {'classe': self.classe.pk, 'tous': '1'})
        self.assertContains(page, 'ouvrirAssistantAccueil')
        self.assertContains(page, reverse('examen_accueil:assistant_notes'))
