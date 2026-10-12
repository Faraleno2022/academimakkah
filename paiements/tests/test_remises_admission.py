"""Remises d'inscription/réinscription optionnelles, isolées par école."""
from decimal import Decimal
from datetime import timedelta
from io import BytesIO

from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from eleves.models import Ecole
from paiements import tests_modification_paiement as fixtures
from paiements.allocation import get_payment_allocation
from paiements.carnet_paiement import construire_donnees_carnet
from paiements.dashboard_metrics import build_payment_dashboard_metrics
from paiements.models import EcheancierPaiement, Paiement, RemiseReduction
from paiements.recalcul_remises import montant_affiche_sur_recu
from paiements.rapports_professionnels import collect_accounting_data
from paiements.services import synchroniser_echeancier_apres_changement_paiement
from paiements.tests.support import MIDDLEWARE_SANS_LICENCE
from paiements.views import _suggestion_paiement, _valider_paiement_impl
from paiements.views_tranches import _tranche_export_rows
from utilisateurs.models import Profil


@override_settings(MIDDLEWARE=MIDDLEWARE_SANS_LICENCE)
class RemisesAdmissionTests(TestCase):
    def setUp(self):
        fixtures.ApplicationRemisePaiementTest.setUp(self)
        self.echeancier.frais_inscription_du = 210000
        self.echeancier.tranche_1_due = 600000
        self.echeancier.tranche_2_due = 600000
        self.echeancier.tranche_3_due = 600000
        self.echeancier.save()
        self.paiement.montant = 2010000
        self.paiement.save()
        self.url = reverse('paiements:appliquer_remise', args=[self.paiement.pk])

    def autoriser(self):
        self.ecole.autoriser_remise_inscription = True
        self.ecole.save(update_fields=['autoriser_remise_inscription'])

    def appliquer(self, pct='5', **extra):
        data = {
            'montant_original': str(self.paiement.montant), 'pourcentage_scolarite': pct,
            'tranches': ['1', '2', '3'], 'inclure_inscription': 'on',
            'base_calcul': 'paiement_echeance', 'motif': 'AUTRE', 'deduire_du_montant': 'on',
        }
        data.update(extra)
        response = self.client.post(self.url, data)
        self.paiement.refresh_from_db()
        return response

    def valider(self):
        _valider_paiement_impl(self.paiement, self.user)
        self.echeancier.refresh_from_db()

    def test_desactive_par_defaut_et_post_forge_refuse(self):
        self.assertFalse(self.ecole.autoriser_remise_inscription)
        page = self.client.get(self.url)
        self.assertContains(page, 'Configurer les règles')
        self.assertNotContains(page, 'name="inclure_inscription"')
        response = self.appliquer()
        self.assertEqual(response.status_code, 200)
        self.assertIn('inclure_inscription', response.context['form'].errors)
        self.assertEqual(self.paiement.montant, 2010000)
        self.assertFalse(self.paiement.remises.exists())

    def test_cinq_pourcent_inscription_et_scolarite_sur_les_deux_bases(self):
        self.autoriser()
        for base in ('paiement_echeance', 'tranches_dues'):
            with self.subTest(base=base):
                self.assertEqual(self.appliquer(base_calcul=base).status_code, 302)
                ligne = self.paiement.remises.get()
                self.assertEqual((self.paiement.montant, ligne.montant_remise), (1909500, 100500))
                self.assertTrue(ligne.regle_calcul['inclure_inscription'])
                self.assertEqual({k: Decimal(v) for k, v in ligne.regle_calcul['repartition'].items()}, {
                    'inscription': 10500, 'tranche_1': 30000, 'tranche_2': 30000, 'tranche_3': 30000,
                })

    def test_ecole_autorisee_peut_garder_inscription_hors_remise(self):
        self.autoriser()
        self.appliquer(inclure_inscription='')
        self.assertEqual(self.paiement.montant, 1920000)
        self.assertEqual(self.paiement.remises.get().montant_remise, 90000)
        self.assertFalse(self.paiement.remises.get().regle_calcul.get('inclure_inscription'))

    def test_inscription_seule_sans_tranche_et_validation_sans_fausse_dette(self):
        self.autoriser()
        self.assertEqual(self.appliquer('50', tranches=[]).status_code, 302)
        self.assertEqual(self.paiement.montant, 1905000)
        self.valider()
        self.assertEqual(self.echeancier.frais_inscription_paye, 105000)
        self.assertEqual(self.echeancier.tranche_3_payee, 600000)
        self.assertEqual(self.echeancier.total_paye, 1905000)
        self.assertEqual(self.echeancier.total_remises_valides, 105000)
        self.assertEqual(self.echeancier.solde_restant, 0)
        self.assertEqual(self.echeancier.statut, 'PAYE_COMPLET')
        self.assertEqual(_suggestion_paiement(self.echeancier, 'Inscription')['suggested'], 0)

    def test_reinscription_seule_et_libelle_affiche(self):
        self.autoriser()
        self.echeancier.nature_frais = EcheancierPaiement.NATURE_REINSCRIPTION
        self.echeancier.frais_inscription_du = 100000
        self.echeancier.save()
        self.paiement.montant = 1900000
        self.paiement.save()
        self.assertContains(self.client.get(self.url), 'Frais de réinscription')
        self.appliquer('50', tranches=[])
        self.assertEqual(self.paiement.montant, 1850000)
        self.valider()
        self.assertEqual(self.echeancier.frais_inscription_paye, 50000)
        self.assertEqual(self.echeancier.solde_restant, 0)
        row = _tranche_export_rows(self.classe, self.paiement.annee_scolaire)[0]
        self.assertEqual((row['inscription'], row['reinscription'], row['discount']), (0, 50000, 50000))

    def test_montant_fixe_reparti_sur_inscription_et_tranche_choisie(self):
        self.autoriser()
        remise = RemiseReduction.objects.create(nom='Geste admission', type_remise='MONTANT_FIXE',
            valeur=150000, motif='AUTRE', date_debut=self.paiement.date_paiement, date_fin=self.paiement.date_paiement)
        self.appliquer('', remises=[remise.pk], tranches=['1'])
        ligne = self.paiement.remises.get()
        parts = {k: Decimal(v) for k, v in ligne.regle_calcul['repartition'].items()}
        self.assertEqual(parts, {'inscription': 38889, 'tranche_1': 111111})
        self.assertEqual(sum(parts.values()), ligne.montant_remise)
        self.valider()
        self.assertEqual(self.echeancier.solde_restant, 0)

    def test_arrondi_gnf_et_reapplication_stables(self):
        self.autoriser()
        self.echeancier.frais_inscription_du = 210010
        self.echeancier.save()
        self.paiement.montant = 2010010
        self.paiement.save()
        for _ in range(3):
            self.appliquer()
            ligne = self.paiement.remises.get()
            self.assertEqual(ligne.montant_remise, 100501)
            self.assertEqual(sum(Decimal(v) for v in ligne.regle_calcul['repartition'].values()), 100501)
            self.assertEqual(self.paiement.montant, 1909509)

    def test_reouverture_conserve_admission_seule_et_base(self):
        self.autoriser()
        self.appliquer('50', tranches=[], base_calcul='tranches_dues')
        form = self.client.get(self.url).context['form']
        self.assertTrue(form['inclure_inscription'].value())
        self.assertEqual(form['tranches'].value(), [])
        self.assertEqual(form['base_calcul'].value(), 'tranches_dues')

    def test_desactiver_ecole_ne_retire_pas_une_remise_deja_accordee(self):
        self.autoriser()
        self.appliquer()
        self.valider()
        self.ecole.autoriser_remise_inscription = False
        self.ecole.save()
        synchroniser_echeancier_apres_changement_paiement(self.eleve.pk, self.paiement.annee_scolaire)
        self.echeancier.refresh_from_db()
        self.assertEqual(self.echeancier.solde_restant, 0)
        self.assertEqual(self.echeancier.frais_inscription_paye, 199500)
        self.assertEqual(self.paiement.remises.get().montant_remise, 100500)

    def test_changement_tarif_recalcule_remise_sans_changer_cash(self):
        self.autoriser()
        self.appliquer(base_calcul='tranches_dues')
        self.valider()
        self.echeancier.frais_inscription_du = 310000
        self.echeancier.save()
        synchroniser_echeancier_apres_changement_paiement(self.eleve.pk, self.paiement.annee_scolaire)
        ligne = self.paiement.remises.get()
        self.paiement.refresh_from_db()
        self.echeancier.refresh_from_db()
        self.assertEqual(ligne.montant_remise, 105500)
        self.assertEqual(Decimal(ligne.regle_calcul['repartition']['inscription']), 15500)
        self.assertEqual(self.paiement.montant, 1909500)
        self.assertEqual(self.echeancier.solde_restant, 95000)

    def test_second_versement_ne_remise_pas_une_inscription_deja_couverte(self):
        self.autoriser()
        self.paiement.montant = 210000
        self.paiement.save()
        self.appliquer('50', tranches=[])
        self.valider()
        premier = self.paiement
        self.paiement = Paiement.objects.create(eleve=self.eleve, type_paiement=self.type_p,
            mode_paiement=self.mode_p, montant=600000, date_paiement=premier.date_paiement,
            annee_scolaire=premier.annee_scolaire)
        self.url = reverse('paiements:appliquer_remise', args=[self.paiement.pk])
        self.appliquer('50', tranches=['1'])
        self.assertEqual(self.paiement.montant, 300000)
        parts = self.paiement.remises.get().regle_calcul['repartition']
        self.assertEqual(Decimal(parts['inscription']), 0)
        self.assertEqual(Decimal(parts['tranche_1']), 300000)
        self.valider()
        self.assertEqual(self.echeancier.frais_inscription_paye, 105000)
        self.assertEqual(self.echeancier.tranche_1_payee, 300000)
        self.assertEqual(self.echeancier.solde_restant, 1200000)

    def test_recu_carnet_et_export_ne_comptent_pas_remise_comme_cash(self):
        from pypdf import PdfReader
        self.autoriser()
        self.appliquer('50', tranches=[])
        self.valider()
        affectation = get_payment_allocation(self.paiement, self.echeancier)
        self.assertEqual(affectation['inscription'], 105000)
        self.assertEqual(montant_affiche_sur_recu(self.paiement), 1905000)
        carnet = construire_donnees_carnet(self.paiement)
        self.assertEqual((carnet['total_encaisse'], carnet['total_remises'], carnet['reste']), (1905000, 105000, 0))
        row = _tranche_export_rows(self.classe, self.paiement.annee_scolaire)[0]
        self.assertEqual((row['inscription'], row['total_paid'], row['discount'], row['balance']), (105000, 1905000, 105000, 0))
        response = self.client.get(reverse('paiements:generer_recu_pdf', args=[self.paiement.pk]))
        self.assertEqual(response.status_code, 200)
        texte = '\n'.join(p.extract_text() or '' for p in PdfReader(BytesIO(response.content)).pages)
        self.assertIn('Montant payé : 1 905 000 GNF', texte)
        self.assertIn('Remise de : 105 000 GNF', texte)

    def test_annulation_reouvre_la_dette_et_conserve_cash(self):
        self.autoriser()
        self.appliquer('50', tranches=[])
        self.valider()
        self.client.post(reverse('paiements:annuler_remise_paiement', args=[self.paiement.pk]))
        self.echeancier.refresh_from_db()
        self.paiement.refresh_from_db()
        self.assertEqual(self.paiement.montant, 1905000)
        self.assertEqual(self.echeancier.solde_restant, 105000)

    def test_cent_pourcent_sur_inscription_seule(self):
        self.autoriser()
        self.appliquer('100', tranches=[])
        self.assertEqual(self.paiement.montant, 1800000)
        self.valider()
        self.assertEqual(self.echeancier.frais_inscription_paye, 0)
        self.assertEqual(self.echeancier.total_paye, 1800000)
        self.assertEqual(self.echeancier.solde_restant, 0)

    def test_tableau_de_bord_et_rapport_ventilent_le_cash_net(self):
        self.autoriser()
        self.appliquer('50', tranches=[])
        self.valider()
        metrics = build_payment_dashboard_metrics(self.user, today=self.paiement.date_paiement)
        admission = next(row for row in metrics['admissions'] if row['key'] == 'inscription')
        scolarite = next(row for row in metrics['categories'] if row['key'] == 'scolarite')
        self.assertEqual(admission['values']['today']['amount'], 105000)
        self.assertEqual(scolarite['values']['today']['amount'], 1800000)
        request = RequestFactory().get('/paiements/liste/', {
            'classe_id': self.classe.pk, 'au': self.paiement.date_paiement.isoformat(),
        })
        request.user = self.user
        data = collect_accounting_data(request)
        self.assertEqual(data['by_component']['inscription']['amount'], 105000)
        self.assertEqual(data['by_component']['tranche_3']['amount'], 600000)
        self.assertEqual(data['total_validated'], 1905000)
        self.assertEqual(data['total_discounts'], 105000)

    def test_cumul_sur_inscription_ne_depasse_pas_son_tarif(self):
        self.autoriser()
        self.paiement.montant = 210000
        self.paiement.save()
        self.appliquer('75', tranches=[])
        self.valider()
        premier = self.paiement
        self.paiement = Paiement.objects.create(eleve=self.eleve, type_paiement=self.type_p,
            mode_paiement=self.mode_p, montant=600000, date_paiement=premier.date_paiement,
            annee_scolaire=premier.annee_scolaire)
        self.url = reverse('paiements:appliquer_remise', args=[self.paiement.pk])
        self.appliquer('50', tranches=[], base_calcul='tranches_dues')
        self.assertEqual(self.paiement.montant, 600000)
        self.assertFalse(self.paiement.remises.exists())
        self.assertEqual(premier.remises.get().montant_remise, 157500)

    def test_remise_sur_tarif_sans_modifier_le_montant_deja_encaisse(self):
        self.autoriser()
        self.paiement.montant = 105000
        self.paiement.save()
        self.appliquer('50', tranches=[], base_calcul='tranches_dues', deduire_du_montant='')
        self.assertEqual(self.paiement.montant, 105000)
        self.valider()
        self.assertEqual(self.echeancier.frais_inscription_paye, 105000)
        self.assertEqual(_suggestion_paiement(self.echeancier, 'Inscription')['suggested'], 0)
        self.assertEqual(self.echeancier.solde_restant, 1800000)

    def test_admission_soldee_ne_declenche_pas_de_rappel_pour_tranches_futures(self):
        from paiements.rappels import GestionnaireRappels
        from paiements.whatsapp_recu import WhatsAppNoteRappelSender
        self.autoriser()
        today = timezone.localdate()
        self.echeancier.date_echeance_inscription = today - timedelta(days=30)
        for n in (1, 2, 3):
            setattr(self.echeancier, f'date_echeance_tranche_{n}', today + timedelta(days=30 * n))
        self.echeancier.save()
        self.paiement.montant = 210000
        self.paiement.save()
        self.appliquer('50', tranches=[])
        self.valider()
        rappels = GestionnaireRappels()
        self.assertEqual(rappels.calculer_jours_retard(self.echeancier), 0)
        self.assertFalse(rappels.detecter_eleves_en_retard(utilisateur=self.user).filter(pk=self.echeancier.pk).exists())
        page = self.client.get(reverse('paiements:echeancier_eleve', args=[self.eleve.pk]))
        self.assertEqual(page.status_code, 200)
        admission = page.context['postes_echeances'][0]
        self.assertEqual((admission['paye'], admission['remise'], admission['reste']), (105000, 105000, 0))
        self.assertEqual(page.context['finance_eleve']['prochain_paiement']['libelle'], '1ère tranche')
        self.assertEqual(page.context['finance_eleve']['retard_reel'], 0)
        self.assertContains(page, 'Soldé')
        situation = WhatsAppNoteRappelSender()._calculer_solde_eleve(self.eleve)
        self.assertEqual(situation['retard_reel'], 0)
        self.assertEqual((situation['prochain_libelle'], situation['prochain_montant']), ('1ère tranche', 600000))

    def test_remise_sur_tranche_future_reste_sur_le_poste_selectionne(self):
        from paiements.whatsapp_recu import WhatsAppNoteRappelSender
        from pypdf import PdfReader
        self.autoriser()
        self.paiement.montant = 810000
        self.paiement.save()
        self.appliquer('50', tranches=['3'], base_calcul='tranches_dues')
        self.valider()
        page = self.client.get(reverse('paiements:echeancier_eleve', args=[self.eleve.pk]))
        postes = page.context['postes_echeances']
        self.assertEqual([p['reste'] for p in postes], [0, 300000, 600000, 300000])
        situation = WhatsAppNoteRappelSender()._calculer_solde_eleve(self.eleve)
        self.assertEqual((situation['prochain_libelle'], situation['prochain_montant']), ('1ère tranche', 300000))
        response = self.client.get(reverse('paiements:generer_recu_pdf', args=[self.paiement.pk]))
        texte = '\n'.join(p.extract_text() or '' for p in PdfReader(BytesIO(response.content)).pages)
        self.assertIn('1ère tranche: 300 000 GNF', texte)
        self.assertIn('3ème tranche: 300 000 GNF', texte)

    def test_synchronisation_transmet_option_et_repartition(self):
        from synchronisation.engine import serialize_instance
        self.autoriser()
        self.appliquer()
        self.assertTrue(serialize_instance(self.ecole)['autoriser_remise_inscription'])
        regle = serialize_instance(self.paiement.remises.get())['regle_calcul']
        self.assertTrue(regle['inclure_inscription'])
        self.assertEqual(sum(Decimal(v) for v in regle['repartition'].values()), 100500)


@override_settings(MIDDLEWARE=MIDDLEWARE_SANS_LICENCE)
class ConfigurationRemisesEcoleTests(TestCase):
    def setUp(self):
        self.ecole = Ecole.objects.create(nom='École remise A', adresse='Conakry',
            telephone='+224620000001', directeur='Direction', etat='VALIDE')
        self.autre = Ecole.objects.create(nom='École remise B', adresse='Kindia',
            telephone='+224620000002', directeur='Direction', etat='VALIDE')
        self.user = User.objects.create_user(username='regle-remise', password='test')
        Profil.objects.update_or_create(user=self.user, defaults={
            'ecole': self.ecole, 'role': 'ADMIN', 'is_validated': True,
        })
        self.client.force_login(self.user)
        self.url = reverse('eleves:configurer_ecole', args=[self.ecole.pk])

    def test_reglage_par_ecole_enregistre_et_synchronise(self):
        from synchronisation.models import SyncChange
        page = self.client.get(self.url)
        self.assertContains(page, 'name="autoriser_remise_inscription"', count=1)
        response = self.client.post(self.url, {'action': 'update_remises_scolarite', 'autoriser_remise_inscription': 'on'})
        self.assertEqual(response.status_code, 302)
        self.ecole.refresh_from_db()
        self.autre.refresh_from_db()
        self.assertTrue(self.ecole.autoriser_remise_inscription)
        self.assertFalse(self.autre.autoriser_remise_inscription)
        self.assertTrue(SyncChange.objects.filter(object_uuid=self.ecole.sync_uuid).latest('id').payload['autoriser_remise_inscription'])
        self.client.post(self.url, {'action': 'update_remises_scolarite'})
        self.ecole.refresh_from_db()
        self.assertFalse(self.ecole.autoriser_remise_inscription)

    def test_modification_autre_ecole_refusee(self):
        response = self.client.post(reverse('eleves:configurer_ecole', args=[self.autre.pk]), {
            'action': 'update_remises_scolarite', 'autoriser_remise_inscription': 'on',
        })
        self.assertEqual(response.status_code, 403)
        self.autre.refresh_from_db()
        self.assertFalse(self.autre.autoriser_remise_inscription)

    def test_sous_utilisateur_sans_droit_ne_modifie_pas_la_regle(self):
        Profil.objects.filter(user=self.user).update(role='SECRETAIRE', est_compte_principal=False,
                                                  peut_gerer_grilles_tarifaires=False)
        self.client.post(self.url, {'action': 'update_remises_scolarite', 'autoriser_remise_inscription': 'on'})
        self.ecole.refresh_from_db()
        self.assertFalse(self.ecole.autoriser_remise_inscription)
