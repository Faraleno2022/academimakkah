from datetime import date, datetime, timedelta
from decimal import Decimal
from io import BytesIO

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from openpyxl import load_workbook
from pypdf import PdfReader

from eleves.models import Classe, Ecole, Eleve
from ecole_moderne.recherche import filtrer_termes
from paiements.models import ModePaiement, Paiement, TypePaiement
from rapports.utils import collecter_donnees_periode, generer_pdf_periode
from rapports.views import _build_excel_from_donnees, collecter_donnees_journalieres, generer_pdf_journalier
from salaires.models import AvanceSalaire, Enseignant, EtatSalaire, PeriodeSalaire, TypeEnseignant

from .forms import AbonnementCantineForm
from .models import AbonnementBus, AbonnementCantine, GrilleTarifaireBus
from .periodes import fin_abonnement


class DatesAbonnementTests(SimpleTestCase):
    def test_un_deux_trois_trimestres_depuis_la_date_de_depart(self):
        debut = date(2026, 10, 9)
        self.assertEqual(fin_abonnement(debut, 'T1', 'bus'), date(2027, 1, 8))
        self.assertEqual(fin_abonnement(debut, 'T1_T2', 'bus'), date(2027, 4, 8))
        self.assertEqual(fin_abonnement(debut, 'ANNUEL', 'bus'), date(2027, 7, 8))
        self.assertEqual(fin_abonnement(debut, 'TRIMESTRIEL', 'cantine'), date(2027, 1, 8))
        self.assertEqual(fin_abonnement(debut, 'DEUX_TRIMESTRES', 'cantine'), date(2027, 4, 8))
        self.assertEqual(fin_abonnement(debut, 'TROIS_TRIMESTRES', 'cantine'), date(2027, 7, 8))


class FinancesJournalieresTests(TestCase):
    def setUp(self):
        self.jour = date(2026, 10, 9)
        self.ecole = Ecole.objects.create(
            nom='École finances', adresse='Conakry', telephone='+224620000501', directeur='Direction',
        )
        self.classe = Classe.objects.create(
            ecole=self.ecole, nom='CM2', niveau='PRIMAIRE_5', annee_scolaire='2026-2027',
        )
        self.eleve = Eleve.objects.create(
            matricule='FIN-001', prenom='Aminata', nom='Diallo', sexe='F', classe=self.classe,
        )

    def test_cantine_remise_solde_et_date(self):
        form = AbonnementCantineForm(data={
            'classe': self.classe.pk, 'eleve': self.eleve.pk,
            'montant_tarif': '120000', 'type_remise': 'POURCENTAGE',
            'valeur_remise': '10', 'montant': '60000',
            'periodicite': 'DEUX_TRIMESTRES', 'type_repas': 'DEJEUNER',
            'date_debut': '2026-10-09', 'statut': 'ACTIF', 'alerte_avant_jours': 7,
        }, ecole=self.ecole)
        self.assertTrue(form.is_valid(), form.errors)
        abo = form.save()
        self.assertEqual(abo.date_expiration, date(2027, 4, 8))
        self.assertEqual((abo.montant_remise, abo.montant_du, abo.reste_a_payer),
                         (Decimal('12000'), Decimal('108000'), Decimal('48000')))

    def test_recherche_nom_complet_dans_les_deux_ordres(self):
        AbonnementCantine.objects.create(
            eleve=self.eleve, montant=1000, date_debut=self.jour, date_expiration=self.jour,
        )
        champs = ('eleve__nom', 'eleve__prenom', 'eleve__matricule')
        noms = ('eleve__nom', 'eleve__prenom')
        self.assertEqual(filtrer_termes(AbonnementCantine.objects.all(), 'Diallo Aminata', champs, noms).count(), 1)
        self.assertEqual(filtrer_termes(AbonnementCantine.objects.all(), 'Aminata Diallo', champs, noms).count(), 1)

    def test_rapport_journalier_contient_chaque_paiement(self):
        mode = ModePaiement.objects.create(nom='Espèces finances')
        type_paiement = TypePaiement.objects.create(nom='Scolarité finances')
        Paiement.objects.create(
            eleve=self.eleve, type_paiement=type_paiement, mode_paiement=mode,
            montant=50000, annee_scolaire='2026-2027', date_paiement=self.jour, statut='VALIDE',
        )
        grille = GrilleTarifaireBus.objects.create(
            ecole=self.ecole, zone='Zone finances', annee_scolaire='2026-2027',
            tranche_1=100000, tranche_2=100000, tranche_3=100000,
        )
        AbonnementBus.objects.create(
            eleve=self.eleve, grille=grille, periodicite='T1', montant=50000,
            date_debut=self.jour, date_expiration=date(2027, 1, 8), mode_paiement=mode,
        )
        AbonnementBus.objects.create(
            eleve=self.eleve, grille=grille, periodicite='T1', montant=50000,
            date_debut=date(2026, 10, 10), date_expiration=date(2027, 1, 9), mode_paiement=mode,
        )
        cantine = AbonnementCantine.objects.create(
            eleve=self.eleve, montant_tarif=100000, type_remise='POURCENTAGE',
            valeur_remise=10, montant=40000, date_debut=self.jour,
            date_expiration=date(2027, 1, 8), periodicite='TRIMESTRIEL',
        )
        AbonnementCantine.objects.filter(pk=cantine.pk).update(
            created_at=timezone.make_aware(datetime.combine(self.jour, datetime.min.time()) + timedelta(hours=12))
        )
        donnees = collecter_donnees_journalieres(self.jour)
        ecole = donnees['ecoles'][self.ecole.pk]
        self.assertEqual(len(ecole['lignes_paiements']), 3)
        self.assertEqual(ecole['totaux_services']['Cantine']['encaisse'], Decimal('40000'))
        bus = next(ligne for ligne in ecole['lignes_paiements'] if ligne['service'] == 'Bus')
        self.assertEqual((bus['du'], bus['encaisse'], bus['reste']),
                         (Decimal('300000'), Decimal('50000'), Decimal('250000')))
        workbook = _build_excel_from_donnees(donnees, 'Rapport journalier')
        sortie = BytesIO()
        workbook.save(sortie)
        feuille = load_workbook(BytesIO(sortie.getvalue()))['Tous les paiements']
        self.assertEqual(feuille.max_row, 4)
        self.assertEqual({feuille.cell(ligne, 2).value for ligne in range(2, 5)},
                         {'Scolarité', 'Bus', 'Cantine'})
        pdf = generer_pdf_journalier(donnees, self.jour)
        texte = ' '.join(page.extract_text() for page in PdfReader(BytesIO(pdf.getvalue())).pages)
        self.assertIn('Cantine', texte)
        self.assertIn('Bus', texte)
        self.assertIn('90 000', texte)
        self.assertIn('140 000', texte)  # scolarité + bus + cantine
        debut = timezone.make_aware(datetime.combine(self.jour, datetime.min.time()))
        fin = timezone.make_aware(datetime.combine(self.jour, datetime.max.time()))
        periode = collecter_donnees_periode(debut, fin, 'HEBDOMADAIRE')
        totaux = periode['ecoles'][self.ecole.pk]['totaux_services']
        self.assertEqual(totaux['Bus']['encaisse'], Decimal('50000'))
        self.assertEqual(totaux['Cantine']['encaisse'], Decimal('40000'))
        synthese = _build_excel_from_donnees(periode, 'Rapport période').active
        self.assertEqual(synthese.cell(3, 18).value, 140000)
        texte_periode = ' '.join(page.extract_text() for page in PdfReader(
            BytesIO(generer_pdf_periode(periode, debut, fin, 'HEBDOMADAIRE').getvalue())
        ).pages)
        self.assertIn('140 000', texte_periode)

    def test_cantine_figure_le_jour_de_l_encaissement_meme_si_le_service_commence_plus_tard(self):
        debut_service = self.jour + timedelta(days=7)
        cantine = AbonnementCantine.objects.create(
            eleve=self.eleve, montant_tarif=120000, type_remise='POURCENTAGE',
            valeur_remise=10, montant=50000, date_debut=debut_service,
            date_expiration=debut_service + timedelta(days=89), periodicite='TRIMESTRIEL',
        )
        AbonnementCantine.objects.filter(pk=cantine.pk).update(
            created_at=timezone.make_aware(datetime.combine(self.jour, datetime.min.time()) + timedelta(hours=12))
        )
        jour_paiement = collecter_donnees_journalieres(self.jour)['ecoles'][self.ecole.pk]
        jour_service = collecter_donnees_journalieres(debut_service)['ecoles'][self.ecole.pk]
        self.assertEqual(jour_paiement['totaux_services']['Cantine']['encaisse'], Decimal('50000'))
        self.assertEqual(jour_paiement['totaux_services']['Cantine']['reste'], Decimal('58000'))
        self.assertEqual(jour_service['totaux_services']['Cantine']['encaisse'], Decimal('0'))

    def test_deux_versements_bus_ne_doublent_pas_le_tarif_annuel(self):
        grille = GrilleTarifaireBus.objects.create(
            ecole=self.ecole, zone='Zone deux versements', annee_scolaire='2026-2027',
            tranche_1=100000, tranche_2=100000, tranche_3=100000,
        )
        for montant in (Decimal('50000'), Decimal('30000')):
            AbonnementBus.objects.create(
                eleve=self.eleve, grille=grille, periodicite='T1', montant=montant,
                date_debut=self.jour, date_expiration=self.jour + timedelta(days=89),
            )
        bus = collecter_donnees_journalieres(self.jour)['ecoles'][self.ecole.pk]['totaux_services']['Bus']
        self.assertEqual((bus['nombre'], bus['du'], bus['encaisse'], bus['reste']),
                         (2, Decimal('300000'), Decimal('80000'), Decimal('220000')))

    def test_salaire_compte_a_la_date_de_paiement_et_avance_a_sa_date(self):
        utilisateur = get_user_model().objects.create_user(username='audit-paie')
        periode = PeriodeSalaire.objects.create(mois=10, annee=2026, ecole=self.ecole, cree_par=utilisateur)
        enseignant = Enseignant.objects.create(
            nom='Paie', prenoms='Essai', ecole=self.ecole,
            type_enseignant=TypeEnseignant.PRIMAIRE, salaire_fixe=Decimal('100000'),
            date_embauche=date(2026, 9, 1), cree_par=utilisateur,
        )
        jour_avance = self.jour - timedelta(days=1)
        AvanceSalaire.objects.create(
            enseignant=enseignant, periode=periode, montant=Decimal('20000'),
            date_avance=jour_avance,
        )
        etat = EtatSalaire.objects.create(
            enseignant=enseignant, periode=periode, salaire_base=Decimal('100000'),
            avances=Decimal('20000'), salaire_net=Decimal('0'), valide=True, paye=True,
            calcule_par=utilisateur,
            date_validation=timezone.make_aware(datetime.combine(jour_avance, datetime.min.time()) + timedelta(hours=15)),
            date_paiement=timezone.make_aware(datetime.combine(self.jour, datetime.min.time()) + timedelta(hours=12)),
        )
        self.assertEqual(etat.salaire_net, Decimal('80000.00'))
        avance = collecter_donnees_journalieres(jour_avance)['ecoles'][self.ecole.pk]['salaires']
        paiement = collecter_donnees_journalieres(self.jour)['ecoles'][self.ecole.pk]['salaires']
        self.assertEqual((avance['etats_payes'], avance['montant_total']), (0, Decimal('20000')))
        self.assertEqual((paiement['etats_payes'], paiement['montant_total']), (1, Decimal('80000')))
        debut = timezone.make_aware(datetime.combine(jour_avance, datetime.min.time()))
        fin = timezone.make_aware(datetime.combine(self.jour, datetime.max.time()))
        periode_salaire = collecter_donnees_periode(debut, fin, 'HEBDOMADAIRE')['ecoles'][self.ecole.pk]['salaires']
        self.assertEqual((periode_salaire['etats_payes'], periode_salaire['montant_total']),
                         (1, Decimal('100000')))
