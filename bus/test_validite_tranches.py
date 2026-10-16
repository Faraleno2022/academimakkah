"""Validité de 3/6/9 mois, dates inclusives, versements et remises conservés."""
from datetime import date
from decimal import Decimal
from importlib import import_module
from io import BytesIO
from types import SimpleNamespace

from django.apps import apps
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from openpyxl import load_workbook
from pypdf import PdfReader

from eleves.models import Classe, Ecole, Eleve
from paiements.tests.support import MIDDLEWARE_SANS_LICENCE
from synchronisation.engine import serialize_instance
from .historique_eleve import date_expiration_cantine, lignes_abonnements
from .models import AbonnementBus, AbonnementCantine, GrilleTarifaireBus, RemiseBus
from .periodes import fin_abonnement


class DatesTranchesTests(SimpleTestCase):
    def test_dates_inclusives_depuis_le_debut(self):
        debut = date(2026, 10, 15)
        for service, codes in (
            ('bus', ('T1', 'T1_T2', 'ANNUEL')),
            ('cantine', ('TRIMESTRIEL', 'DEUX_TRIMESTRES', 'TROIS_TRIMESTRES')),
        ):
            for code, fin in zip(codes, (date(2027, 1, 14), date(2027, 4, 14), date(2027, 7, 14))):
                self.assertEqual(fin_abonnement(debut, code, service), fin)
        self.assertEqual(date_expiration_cantine(debut, 'ANNUEL'), date(2027, 7, 14))

    def test_fin_de_mois_et_annee_bissextile(self):
        self.assertEqual(fin_abonnement(date(2023, 11, 30), 'TRIMESTRIEL', 'cantine'), date(2024, 2, 28))
        self.assertEqual(fin_abonnement(date(2024, 2, 29), 'TROIS_TRIMESTRES', 'cantine'), date(2024, 11, 28))


@override_settings(MIDDLEWARE=MIDDLEWARE_SANS_LICENCE)
class ValiditeTranchesTests(TestCase):
    def setUp(self):
        self.ecole = Ecole.objects.create(nom='École validité', adresse='Conakry', telephone='600111333', directeur='D')
        self.classe = Classe.objects.create(ecole=self.ecole, nom='CE1', niveau='PRIMAIRE_2', annee_scolaire='2026-2027')
        self.eleve = Eleve.objects.create(classe=self.classe, matricule='VAL-001', nom='Camara', prenom='Awa')
        self.grille = GrilleTarifaireBus.objects.create(ecole=self.ecole, zone='Kipé', annee_scolaire='2026-2027',
            tranche_1=150000, tranche_2=125000, tranche_3=125000)
        self.user = get_user_model().objects.create_user('validite-admin', password='test')
        profil = self.user.profil
        profil.role, profil.ecole, profil.is_validated, profil.allowed_menus = 'ADMIN', self.ecole, True, ['bus']
        profil.save()
        self.client.force_login(self.user)

    def paiement(self, code='T1', montant=150000, debut=date(2026, 10, 15), **kwargs):
        return AbonnementBus.objects.create(eleve=self.eleve, grille=self.grille,
            periodicite=code, montant=montant, date_debut=debut, date_expiration=date(2027, 7, 14), **kwargs)

    def test_premiere_tranche_trois_mois_et_reste_annuel_conserve(self):
        abo = self.paiement()
        self.assertEqual(abo.date_debut_abonnement, date(2026, 10, 15))
        self.assertEqual(abo.date_expiration, date(2027, 1, 14))
        self.assertEqual(self.grille.situation_paiements(self.eleve)['ANNUEL']['reste'], 250000)

    def test_paiements_suivants_prolongent_depuis_le_premier(self):
        premier = self.paiement()
        deuxieme = self.paiement('T2', 125000, date(2026, 12, 20))
        troisieme = self.paiement('T3', 125000, date(2027, 3, 5))
        self.assertEqual(deuxieme.date_debut, date(2026, 12, 20))
        self.assertEqual(deuxieme.date_debut_abonnement, premier.date_debut)
        self.assertEqual(deuxieme.date_expiration, date(2027, 4, 14))
        self.assertEqual(troisieme.date_debut_abonnement, premier.date_debut)
        self.assertEqual(troisieme.date_expiration, date(2027, 7, 14))
        premier.refresh_from_db()
        self.assertEqual(premier.date_expiration, date(2027, 1, 14))
        self.assertEqual(self.grille.situation_paiements(self.eleve)['ANNUEL']['paye'], 400000)

    def test_choix_annuel_avec_seulement_premiere_tranche_payee_ne_donne_pas_neuf_mois(self):
        abo = self.paiement('ANNUEL', 150000)
        self.assertEqual(abo.date_expiration, date(2027, 1, 14))

    def test_ancien_libelle_t1_avec_annee_entierement_payee_donne_neuf_mois(self):
        abo = self.paiement('T1', 400000)
        self.assertEqual(abo.date_expiration, date(2027, 7, 14))

    def test_complement_premiere_tranche_ne_prolonge_pas_un_deuxieme_trimestre(self):
        self.paiement(montant=75000)
        complement = self.paiement(montant=75000, debut=date(2026, 11, 1))
        self.assertEqual(complement.date_expiration, date(2027, 1, 14))

    def test_remise_est_prise_en_compte_pour_solder_les_tranches(self):
        abo = self.paiement('ANNUEL', 360000)
        self.assertEqual(abo.date_expiration, date(2027, 4, 14))
        remise = RemiseBus.objects.create(eleve=self.eleve, grille=self.grille,
            type_remise='POURCENTAGE', valeur=10, motif='FRATRIE')
        abo.refresh_from_db()
        self.assertEqual(abo.date_expiration, date(2027, 7, 14))
        self.assertEqual(abo.montant, 360000)
        remise.delete()
        abo.refresh_from_db()
        self.assertEqual(abo.date_expiration, date(2027, 4, 14))

    def test_correction_date_premier_recu_recalcule_les_suivants(self):
        premier = self.paiement()
        second = self.paiement('T2', 125000, date(2026, 12, 20))
        premier.date_debut = date(2026, 10, 20)
        premier.save(update_fields=['date_debut'])
        second.refresh_from_db()
        self.assertEqual(second.date_debut_abonnement, date(2026, 10, 20))
        self.assertEqual(second.date_expiration, date(2027, 4, 19))

    def test_suppression_premier_recu_recalcule_le_cycle_restant(self):
        premier = self.paiement()
        second = self.paiement('T2', 125000, date(2026, 12, 20))
        premier.delete()
        second.refresh_from_db()
        self.assertEqual(second.date_debut_abonnement, date(2026, 12, 20))
        self.assertEqual(second.date_expiration, date(2027, 3, 19))

    def test_autre_annee_ne_reprend_pas_la_date_de_l_ancien_cycle(self):
        self.paiement()
        grille = GrilleTarifaireBus.objects.create(ecole=self.ecole, zone='Kipé', annee_scolaire='2027-2028',
            tranche_1=150000, tranche_2=125000, tranche_3=125000)
        abo = AbonnementBus.objects.create(eleve=self.eleve, grille=grille, periodicite='T1', montant=150000,
            date_debut=date(2027, 10, 1), date_expiration=date(2028, 6, 30))
        self.assertEqual(abo.date_debut_abonnement, date(2027, 10, 1))
        self.assertEqual(abo.date_expiration, date(2027, 12, 31))

    def test_reception_hors_ordre_recalcule_le_cycle(self):
        second = self.paiement('T2', 125000, date(2026, 12, 20))
        self.paiement()
        second.refresh_from_db()
        self.assertEqual(second.date_debut_abonnement, date(2026, 10, 15))
        self.assertEqual(second.date_expiration, date(2027, 4, 14))

    def test_cantine_annuelle_partiellement_payee_et_remise_donne_trois_mois(self):
        abo = AbonnementCantine.objects.create(eleve=self.eleve, periodicite='ANNUEL', montant=500000,
            montant_tarif=900000, type_remise='POURCENTAGE', valeur_remise=10,
            date_debut=date(2026, 10, 15), date_expiration=date(2027, 10, 14))
        self.assertEqual(abo.date_expiration, date(2027, 1, 14))
        self.assertEqual(abo.periode_couverte, '3 mois sur 9 mois')
        self.assertEqual(abo.montant_remise, 90000)
        self.assertEqual(abo.reste_a_payer, 310000)

    def test_cantine_annuelle_trois_tranches_nettes_egales(self):
        abo = AbonnementCantine.objects.create(eleve=self.eleve, periodicite='ANNUEL', montant=270000,
            montant_tarif=900000, type_remise='POURCENTAGE', valeur_remise=10,
            date_debut=date(2026, 10, 15))
        for montant, mois, fin in ((270000, 3, date(2027, 1, 14)),
                                  (540000, 6, date(2027, 4, 14)), (810000, 9, date(2027, 7, 14))):
            abo.montant = montant
            abo.save(update_fields=['montant'])
            abo.refresh_from_db()
            self.assertEqual(abo.periode_couverte, f'{mois} mois sur 9 mois')
            self.assertEqual(abo.date_expiration, fin)
            self.assertEqual(abo.reste_a_payer, 810000 - montant)

    def test_cantine_remise_fixe_arrondis_sans_surplus_de_validite(self):
        abo = AbonnementCantine(eleve=self.eleve, periodicite='ANNUEL', montant=333333,
            montant_tarif=1100000, type_remise='MONTANT', valeur_remise=100000, date_debut=date(2026, 10, 15))
        for montant, mois in ((333333, 3), (666665, 3), (666666, 6), (999999, 6), (1000000, 9)):
            abo.montant = montant
            abo.save()
            self.assertEqual(abo.periode_couverte, f'{mois} mois sur 9 mois')

    def test_cantine_sans_tarif_conserve_periode_choisie_et_aucune_fausse_dette(self):
        abo = AbonnementCantine.objects.create(eleve=self.eleve, periodicite='ANNUEL', montant=900000,
            date_debut=date(2026, 10, 15))
        self.assertEqual(abo.periode_couverte, '9 mois sur 9 mois')
        self.assertEqual(abo.reste_a_payer, 0)

    def test_cantine_zero_versement_et_gratuite(self):
        abo = AbonnementCantine.objects.create(eleve=self.eleve, periodicite='ANNUEL', montant=0,
            montant_tarif=900000, date_debut=date(2026, 10, 15))
        self.assertEqual(abo.periode_couverte, '0 mois sur 9 mois')
        self.assertLess(abo.date_expiration, abo.date_debut)
        abo.type_remise, abo.valeur_remise = 'POURCENTAGE', 100
        abo.save()
        self.assertEqual(abo.periode_couverte, '9 mois sur 9 mois')
        self.assertEqual(abo.reste_a_payer, 0)

    def test_migration_cantine_periode_payee_preserve_montants_et_remises(self):
        abo = AbonnementCantine.objects.create(eleve=self.eleve, periodicite='ANNUEL', montant=600000,
            montant_tarif=900000, date_debut=date(2026, 10, 15))
        AbonnementCantine.objects.filter(pk=abo.pk).update(date_expiration=date(2027, 7, 14))
        migration = import_module('bus.migrations.0014_cantine_periode_payee')
        schema = SimpleNamespace(connection=SimpleNamespace(alias='default'))
        migration.regulariser_cantine(apps, schema)
        migration.regulariser_cantine(apps, schema)
        abo.refresh_from_db()
        self.assertEqual(abo.date_expiration, date(2027, 4, 14))
        self.assertEqual((abo.montant, abo.montant_tarif), (600000, 900000))

    def test_cantine_liste_excel_pdf_affichent_periode_payee(self):
        abo = AbonnementCantine.objects.create(eleve=self.eleve, periodicite='ANNUEL', montant=300000,
            montant_tarif=900000, date_debut=date(2026, 10, 15))
        self.assertContains(self.client.get(reverse('bus:liste_abonnements_cantine')), '3 mois sur 9 mois')
        excel = self.client.get(reverse('bus:export_cantine_excel'))
        rows = list(load_workbook(BytesIO(excel.content), data_only=True).active.values)
        self.assertTrue(any(any('3 mois sur 9 mois' in str(cell) for cell in row) for row in rows))
        pdf = self.client.get(reverse('bus:export_cantine_pdf'))
        texte = '\n'.join(p.extract_text() for p in PdfReader(BytesIO(pdf.content)).pages)
        self.assertIn('3 mois sur 9 mois', texte)
        self.assertIn('14/01/2027', texte)

    def test_api_apercu_affiche_debut_et_fin_du_cycle(self):
        self.paiement()
        data = self.client.get(reverse('bus:tarif_bus_json'), {'eleve_id': self.eleve.pk,
            'grille_id': self.grille.pk, 'periodicite': 'T2', 'date_debut': '2026-12-20', 'montant': '125000'}).json()
        self.assertEqual(data['debut_abonnement'], '2026-10-15')
        self.assertEqual(data['fin_abonnement'], '2027-04-14')

    def test_api_modification_conserve_ordre_des_recus_du_meme_jour(self):
        premier = self.paiement(numero_recu='BUS-A')
        second = self.paiement('T2', 125000, numero_recu='BUS-B')
        data = self.client.get(reverse('bus:tarif_bus_json'), {'eleve_id': self.eleve.pk,
            'grille_id': self.grille.pk, 'periodicite': 'T1', 'date_debut': '2026-10-15',
            'montant': '150000', 'exclure': premier.pk}).json()
        self.assertEqual(data['fin_abonnement'], premier.date_expiration.isoformat())
        self.assertEqual(second.date_expiration, date(2027, 4, 14))

    def test_liste_recu_historique_et_exports_affichent_les_memes_dates(self):
        self.paiement()
        second = self.paiement('T2', 125000, date(2026, 12, 20))
        response = self.client.get(reverse('bus:liste'))
        self.assertContains(response, 'Début abonnement : 15/10/2026')
        self.assertContains(response, '14/04/2027')
        self.assertContains(response, '6 mois sur 9 mois')
        self.assertContains(response, '20/12/2026')
        historique = next(r for r in lignes_abonnements(self.eleve, 'bus') if r['objet'].pk == second.pk)
        self.assertEqual(historique['date_debut'], date(2026, 10, 15))
        excel = self.client.get(reverse('bus:export_abonnements_bus', args=['excel']))
        rows = list(load_workbook(BytesIO(excel.content), data_only=True).active.values)
        ligne = next(r for r in rows if second.numero_recu in r)
        self.assertEqual(ligne[-1].date(), date(2026, 10, 15))
        self.assertEqual(ligne[14].date(), date(2026, 12, 20))
        self.assertEqual(ligne[15].date(), date(2027, 4, 14))
        recu = self.client.get(reverse('bus:recu_pdf', args=[second.pk]))
        texte = '\n'.join(p.extract_text() for p in PdfReader(BytesIO(recu.content)).pages)
        for valeur in ('15/10/2026', '20/12/2026', '14/04/2027'):
            self.assertIn(valeur, texte)
        from .abonnement_public import generer_token_abonnement
        public = self.client.get(reverse('bus:recu_public_pdf', args=[second.pk]),
            {'token': generer_token_abonnement(second.pk)})
        self.assertEqual(public.status_code, 200)
        texte_public = '\n'.join(p.extract_text() for p in PdfReader(BytesIO(public.content)).pages)
        for valeur in ('15/10/2026', '20/12/2026', '14/04/2027'):
            self.assertIn(valeur, texte_public)
        from .whatsapp_bus import WhatsAppBusSender
        message = WhatsAppBusSender()._generer_message_abonnement(second)
        self.assertIn('Date du paiement: 20/12/2026', message)
        self.assertIn('Date début: 15/10/2026', message)
        self.assertEqual(serialize_instance(second)['date_debut_abonnement'], '2026-10-15')

    def test_migration_regularise_anciens_releves_sans_modifier_les_paiements(self):
        premier = self.paiement('ANNUEL', 150000)
        second = self.paiement('T2', 125000, date(2026, 12, 20))
        AbonnementBus.objects.all().update(date_debut_abonnement=None, date_expiration=date(2027, 7, 14))
        migration = import_module('bus.migrations.0013_validite_tranches')
        schema = SimpleNamespace(connection=SimpleNamespace(alias='default'))
        migration.regulariser_dates(apps, schema)
        migration.regulariser_dates(apps, schema)
        premier.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(premier.date_expiration, date(2027, 1, 14))
        self.assertEqual(second.date_expiration, date(2027, 4, 14))
        self.assertEqual(second.date_debut_abonnement, date(2026, 10, 15))
        self.assertEqual(premier.montant + second.montant, 275000)

    def test_migration_cantine_traite_plusieurs_lots_sans_modifier_les_montants(self):
        AbonnementCantine.objects.bulk_create([
            AbonnementCantine(eleve=self.eleve, annee_scolaire='2026-2027', periodicite='ANNUEL',
                montant=100, date_debut=date(2026, 10, 15), date_expiration=date(2027, 10, 14))
            for _ in range(501)
        ])
        migration = import_module('bus.migrations.0013_validite_tranches')
        migration.regulariser_dates(apps, SimpleNamespace(connection=SimpleNamespace(alias='default')))
        self.assertEqual(AbonnementCantine.objects.filter(date_expiration=date(2027, 7, 14), montant=100).count(), 501)
