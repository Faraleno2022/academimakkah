from datetime import date
from decimal import Decimal
from io import BytesIO

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from openpyxl import load_workbook

from eleves.models import Classe, Ecole, Eleve
from paiements.models import ModePaiement
from paiements.tests.support import MIDDLEWARE_SANS_LICENCE
from utilisateurs.models import Profil

from .models import AbonnementBus, GrilleTarifaireBus


@override_settings(MIDDLEWARE=MIDDLEWARE_SANS_LICENCE)
class PaiementsCombinesEtControleChauffeurTests(TestCase):
    def setUp(self):
        self.ecole = Ecole.objects.create(
            nom='École Contrôle', adresse='Conakry', telephone='+224620000201', directeur='Direction',
        )
        self.autre_ecole = Ecole.objects.create(
            nom='École Voisine', adresse='Conakry', telephone='+224620000202', directeur='Direction',
        )
        self.classe = Classe.objects.create(
            ecole=self.ecole, nom='CM1', niveau='PRIMAIRE_4', annee_scolaire='2026-2027',
        )
        autre_classe = Classe.objects.create(
            ecole=self.autre_ecole, nom='CM2', niveau='PRIMAIRE_5', annee_scolaire='2026-2027',
        )
        self.eleve = Eleve.objects.create(
            matricule='CTL-001', prenom='Aïssatou', nom='Barry', sexe='F', classe=self.classe,
        )
        self.eleve_b = Eleve.objects.create(
            matricule='CTL-002', prenom='Ibrahima', nom='Sow', sexe='M', classe=self.classe,
        )
        self.eleve_ailleurs = Eleve.objects.create(
            matricule='CTL-999', prenom='Mamadou', nom='Diallo', sexe='M', classe=autre_classe,
        )
        self.mode = ModePaiement.objects.create(nom='Espèces Contrôle')
        self.grille = GrilleTarifaireBus.objects.create(
            ecole=self.ecole, zone='Ratoma', annee_scolaire='2026-2027',
            tranche_1=150000, tranche_2=125000, tranche_3=125000,
            date_echeance_tranche_1=date(2026, 10, 15),
            date_echeance_tranche_2=date(2027, 1, 15),
            date_echeance_tranche_3=date(2027, 4, 15),
        )
        autre_grille = GrilleTarifaireBus.objects.create(
            ecole=self.autre_ecole, zone='Matoto', annee_scolaire='2026-2027',
            tranche_1=100000, tranche_2=100000, tranche_3=100000,
        )
        AbonnementBus.objects.create(
            eleve=self.eleve_ailleurs, grille=autre_grille, periodicite='T1', montant=100000,
            date_debut=date(2026, 10, 1), date_expiration=date(2027, 6, 30),
            itineraire='Circuit secret', contact_parent='+224699999999',
        )
        self.user = get_user_model().objects.create_user(username='admin_controle', password='secret123')
        Profil.objects.update_or_create(user=self.user, defaults={
            'role': 'ADMIN', 'telephone': '+224620000211', 'ecole': self.ecole,
            'is_validated': True, 'allowed_menus': ['bus'],
        })
        self.client.force_login(self.user)

    def payer(self, eleve, periodicite, montant, **extra):
        return AbonnementBus.objects.create(
            eleve=eleve, grille=self.grille, periodicite=periodicite, montant=montant,
            date_debut=date(2026, 10, 1), date_expiration=date(2027, 4, 15), **extra,
        )

    # --- Paiements combinés ---------------------------------------------

    def test_montants_dus_des_paiements_combines(self):
        self.assertEqual(self.grille.montant_pour('T1_T2M'), Decimal('212500'))
        self.assertEqual(self.grille.montant_pour('T1_T2'), Decimal('275000'))
        self.assertEqual(self.grille.montant_pour('T1_T2_T3M'), Decimal('337500'))
        self.assertEqual(self.grille.montant_pour('ANNUEL'), Decimal('400000'))
        self.assertEqual(self.grille.echeance_pour('T1_T2M'), date(2027, 1, 15))

    def test_paiement_t1_plus_moitie_t2_reparti_dans_l_ordre(self):
        self.payer(self.eleve, 'T1_T2M', 212500)
        situation = self.grille.situation_paiements(self.eleve)
        self.assertEqual(situation['T1']['reste'], 0)
        self.assertEqual(situation['T2']['paye'], Decimal('62500'))
        self.assertEqual(situation['T2']['reste'], Decimal('62500'))
        self.assertEqual(situation['T3']['reste'], Decimal('125000'))
        self.assertEqual(situation['T1_T2M']['reste'], 0)
        self.assertEqual(situation['T1_T2']['reste'], Decimal('62500'))
        self.assertEqual(situation['ANNUEL']['reste'], Decimal('187500'))

    def test_combine_apres_paiement_direct_ne_compte_pas_deux_fois(self):
        self.payer(self.eleve, 'T1', 150000)
        situation = self.grille.situation_paiements(self.eleve)
        self.assertEqual(situation['T1_T2_T3M']['reste'], Decimal('187500'))

    def test_formulaire_accepte_un_paiement_combine_et_refuse_le_depassement(self):
        donnees = {
            'eleve': self.eleve.pk, 'grille': self.grille.pk, 'periodicite': 'T1_T2M',
            'montant': 212501, 'date_debut': '2026-10-02', 'mode_paiement': self.mode.pk,
        }
        response = self.client.post(reverse('bus:nouveau'), donnees)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Le montant dépasse le reste à payer')

        donnees['montant'] = 212500
        response = self.client.post(reverse('bus:nouveau'), donnees)
        self.assertRedirects(response, reverse('bus:liste'))
        paiement = AbonnementBus.objects.get(eleve=self.eleve)
        self.assertEqual(paiement.periodicite, 'T1_T2M')
        self.assertEqual(paiement.get_periodicite_display(), 'Tranche 1 + ½ Tranche 2')

    def test_api_tarif_pour_un_paiement_combine(self):
        self.payer(self.eleve, 'T1', 100000)
        response = self.client.get(reverse('bus:tarif_bus_json'), {
            'eleve_id': self.eleve.pk, 'grille_id': self.grille.pk, 'periodicite': 'T1_T2',
        })
        data = response.json()
        self.assertEqual((data['du'], data['paye'], data['reste']), (275000, 100000, 175000))
        self.assertEqual(data['echeance'], '2027-01-15')

    def test_recu_pdf_d_un_paiement_combine(self):
        paiement = self.payer(self.eleve, 'T1_T2_T3M', 337500)
        response = self.client.get(reverse('bus:recu_pdf', args=[paiement.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/pdf')

    # --- Liste de contrôle du chauffeur ---------------------------------

    def preparer_controle(self):
        self.payer(self.eleve, 'T1', 150000, itineraire='Circuit B', point_arret='Kipé',
                   contact_parent='+224621000001')
        # Le reçu le plus récent ne reprend pas l'itinéraire : celui du précédent est conservé.
        recent = self.payer(self.eleve, 'T2', 125000)
        AbonnementBus.objects.filter(pk=recent.pk).update(date_debut=date(2026, 12, 1))
        self.payer(self.eleve_b, 'T1', 150000, itineraire='Circuit A', contact_parent='+224621000002')

    def test_export_excel_un_eleve_par_ligne_itineraire_en_tete(self):
        self.preparer_controle()
        response = self.client.get(reverse('bus:export_controle', args=['excel']))
        self.assertEqual(response.status_code, 200)
        ws = load_workbook(BytesIO(response.content)).active
        self.assertEqual([c.value for c in ws[4]][:5], ['N°', 'Itinéraire', 'Élève', 'Matricule', 'Contact parent'])
        lignes = [[c.value for c in row] for row in ws.iter_rows(min_row=5) if row[0].value]
        self.assertEqual(len(lignes), 2)
        self.assertEqual([ligne[1] for ligne in lignes], ['Circuit A', 'Circuit B'])
        self.assertEqual(lignes[1][3], 'CTL-001')
        self.assertEqual(lignes[1][4], '+224621000001')
        self.assertEqual(lignes[1][6], 'Kipé')
        self.assertNotIn('CTL-999', [ligne[3] for ligne in lignes])

    def test_export_pdf_et_filtre_par_itineraire(self):
        self.preparer_controle()
        response = self.client.get(reverse('bus:export_controle', args=['pdf']))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/pdf')

        response = self.client.get(reverse('bus:export_controle', args=['excel']), {'q': 'Circuit A'})
        ws = load_workbook(BytesIO(response.content)).active
        matricules = [row[3].value for row in ws.iter_rows(min_row=5) if row[0].value]
        self.assertEqual(matricules, ['CTL-002'])

    def test_export_format_invalide(self):
        response = self.client.get(reverse('bus:export_controle', args=['docx']))
        self.assertEqual(response.status_code, 400)

    def test_formulaire_enregistre_l_itineraire(self):
        response = self.client.post(reverse('bus:nouveau'), {
            'eleve': self.eleve.pk, 'grille': self.grille.pk, 'periodicite': 'T1',
            'montant': 150000, 'date_debut': '2026-10-02', 'mode_paiement': self.mode.pk,
            'itineraire': 'Circuit C', 'point_arret': 'Cosa', 'contact_parent': '+224621000003',
        })
        self.assertRedirects(response, reverse('bus:liste'))
        paiement = AbonnementBus.objects.get(eleve=self.eleve)
        self.assertEqual(
            (paiement.itineraire, paiement.point_arret, paiement.contact_parent),
            ('Circuit C', 'Cosa', '+224621000003'),
        )
