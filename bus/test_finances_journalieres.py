from datetime import date
from decimal import Decimal
from io import BytesIO

from django.test import SimpleTestCase, TestCase
from openpyxl import load_workbook
from pypdf import PdfReader

from eleves.models import Classe, Ecole, Eleve
from ecole_moderne.recherche import filtrer_termes
from paiements.models import ModePaiement, Paiement, TypePaiement
from rapports.views import _build_excel_from_donnees, collecter_donnees_journalieres, generer_pdf_journalier

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
        AbonnementCantine.objects.create(
            eleve=self.eleve, montant_tarif=100000, type_remise='POURCENTAGE',
            valeur_remise=10, montant=40000, date_debut=self.jour,
            date_expiration=date(2027, 1, 8), periodicite='TRIMESTRIEL',
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
