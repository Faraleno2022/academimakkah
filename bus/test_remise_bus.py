"""Remise sur l'abonnement bus et report de l'excédent sur la tranche suivante."""
from datetime import date
from decimal import Decimal
from io import BytesIO

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from openpyxl import load_workbook
from pypdf import PdfReader

from eleves.models import Classe, Ecole, Eleve
from paiements.models import ModePaiement
from paiements.tests.support import MIDDLEWARE_SANS_LICENCE
from utilisateurs.models import Profil

from .controle_chauffeur import lignes_controle
from .historique_eleve import reprise_bus
from .models import AbonnementBus, GrilleTarifaireBus, RemiseBus


def texte_pdf(contenu):
    return ' '.join(page.extract_text() for page in PdfReader(BytesIO(contenu)).pages)


@override_settings(MIDDLEWARE=MIDDLEWARE_SANS_LICENCE)
class RemiseBusTests(TestCase):
    def setUp(self):
        self.ecole = Ecole.objects.create(
            nom='École Remise', adresse='Conakry', telephone='+224620000401', directeur='Direction',
        )
        self.classe = Classe.objects.create(
            ecole=self.ecole, nom='CM2', niveau='PRIMAIRE_5', annee_scolaire='2026-2027',
        )
        self.eleve = Eleve.objects.create(matricule='REM-001', prenom='Kadiatou', nom='Sylla', sexe='F', classe=self.classe)
        self.mode = ModePaiement.objects.create(nom='Espèces Remise')
        self.grille = GrilleTarifaireBus.objects.create(
            ecole=self.ecole, zone='Kipé', annee_scolaire='2026-2027',
            tranche_1=150000, tranche_2=125000, tranche_3=125000,
            date_echeance_tranche_1=date(2026, 10, 15),
            date_echeance_tranche_2=date(2027, 1, 15),
            date_echeance_tranche_3=date(2027, 4, 15),
        )
        self.user = get_user_model().objects.create_user(username='caisse_remise', password='secret123')
        Profil.objects.update_or_create(user=self.user, defaults={
            'role': 'ADMIN', 'telephone': '+224620000402', 'ecole': self.ecole,
            'is_validated': True, 'allowed_menus': ['bus'], 'peut_supprimer_abonnements': True,
        })
        self.client.force_login(self.user)

    def remise(self, **champs):
        valeurs = {'type_remise': 'POURCENTAGE', 'valeur': Decimal('10'), 'motif': 'FRATRIE'}
        valeurs.update(champs)
        return RemiseBus.objects.create(eleve=self.eleve, grille=self.grille, **valeurs)

    def payer(self, periodicite, montant):
        return AbonnementBus.objects.create(
            eleve=self.eleve, grille=self.grille, periodicite=periodicite, montant=montant,
            date_debut=date(2026, 10, 1), date_expiration=date(2027, 4, 15), mode_paiement=self.mode,
        )

    def donnees(self, **champs):
        valeurs = {
            'eleve': self.eleve.pk, 'grille': self.grille.pk, 'periodicite': 'T1',
            'montant': 135000, 'date_debut': '2026-10-02', 'mode_paiement': self.mode.pk,
        }
        valeurs.update(champs)
        return valeurs

    # --- Calcul de la remise -------------------------------------------

    def test_pourcentage_sur_une_seule_tranche(self):
        remise = self.remise(sur_tranche_2=False, sur_tranche_3=False)
        situation = self.grille.situation_paiements(self.eleve)
        self.assertEqual(situation['T1']['du'], Decimal('135000'))
        self.assertEqual(situation['T1']['remise'], Decimal('15000'))
        self.assertEqual(situation['T2']['du'], Decimal('125000'))
        self.assertEqual(situation['ANNUEL']['du'], Decimal('385000'))
        self.assertEqual(remise.libelle, '10 % — Réduction fratrie (1ère tranche)')

    def test_montant_fixe_reparti_au_prorata_sans_perte_d_un_franc(self):
        remise = self.remise(type_remise='MONTANT_FIXE', valeur=Decimal('100001'))
        parts = remise.remises_par_tranche(self.grille.tranches_brutes())
        self.assertEqual(sum(parts.values()), Decimal('100001'))
        self.assertEqual(parts['T1'], Decimal('37501'))
        self.assertEqual(self.grille.situation_paiements(self.eleve)['ANNUEL']['du'], Decimal('299999'))

    def test_remise_superieure_au_tarif_plafonnee(self):
        remise = self.remise(type_remise='MONTANT_FIXE', valeur=Decimal('999999'), sur_tranche_1=False)
        parts = remise.remises_par_tranche(self.grille.tranches_brutes())
        self.assertEqual(parts, {'T1': 0, 'T2': Decimal('125000'), 'T3': Decimal('125000')})

    def test_exemple_de_la_caisse_99_995(self):
        self.grille.tranche_1 = 100000
        self.grille.save()
        self.remise(type_remise='MONTANT_FIXE', valeur=Decimal('5'), sur_tranche_2=False, sur_tranche_3=False)
        data = self.client.get(reverse('bus:tarif_bus_json'), {
            'eleve_id': self.eleve.pk, 'grille_id': self.grille.pk, 'periodicite': 'T1',
        }).json()
        self.assertEqual((data['brut'], data['remise'], data['du'], data['reste']), (100000, 5, 99995, 99995))
        self.assertEqual(data['remise_eleve']['libelle'], '5 GNF — Réduction fratrie (1ère tranche)')

    # --- Report de l'excédent -------------------------------------------

    def test_excedent_de_la_tranche_1_passe_a_la_2_puis_a_la_3(self):
        self.remise()
        self.payer('T1', 300000)
        situation = self.grille.situation_paiements(self.eleve)
        self.assertEqual(situation['T1']['reste'], 0)
        self.assertEqual(situation['T2']['reste'], 0)
        self.assertEqual(situation['T3']['paye'], Decimal('52500'))
        self.assertEqual(situation['ANNUEL']['reste'], Decimal('60000'))

    def test_excedent_de_la_tranche_2_ne_remonte_pas_sur_la_1(self):
        self.payer('T2', 150000)
        situation = self.grille.situation_paiements(self.eleve)
        self.assertEqual(situation['T1']['paye'], 0)
        self.assertEqual(situation['T3']['paye'], Decimal('25000'))
        self.assertEqual(situation['T2']['plafond'], Decimal('100000'))
        self.assertEqual(situation['T1']['plafond'], Decimal('250000'))

    def test_formulaire_accepte_le_report_et_refuse_au_dela_de_l_annee(self):
        self.remise()
        response = self.client.post(reverse('bus:nouveau'), self.donnees(montant=360001))
        self.assertContains(response, "reste à payer de l&#x27;année (360 000 GNF, remise déduite)")
        response = self.client.post(reverse('bus:nouveau'), self.donnees(montant=200000))
        self.assertRedirects(response, reverse('bus:liste'))
        situation = self.grille.situation_paiements(self.eleve)
        self.assertEqual((situation['T1']['reste'], situation['T2']['paye']), (0, Decimal('65000')))

    # --- Saisie dans le formulaire de paiement ----------------------------

    def test_remise_saisie_au_paiement_est_enregistree(self):
        response = self.client.post(reverse('bus:nouveau'), self.donnees(
            montant=120000, remise_modifiee='1', remise_type='POURCENTAGE', remise_valeur='20',
            remise_motif='EMPLOYE', remise_sur_tranche_1='on', remise_sur_tranche_2='on', remise_sur_tranche_3='on',
        ))
        self.assertRedirects(response, reverse('bus:liste'))
        remise = RemiseBus.objects.get(eleve=self.eleve, grille=self.grille)
        self.assertEqual((remise.valeur, remise.motif, remise.cree_par), (Decimal('20'), 'EMPLOYE', self.user))
        self.assertEqual(self.grille.situation_paiements(self.eleve)['T1']['reste'], 0)

    def test_montant_controle_avec_la_remise_saisie(self):
        response = self.client.post(reverse('bus:nouveau'), self.donnees(
            montant=1, remise_modifiee='1', remise_type='POURCENTAGE', remise_valeur='100',
            remise_motif='NE_PAIE_RIEN', remise_sur_tranche_1='on', remise_sur_tranche_2='on', remise_sur_tranche_3='on',
        ))
        self.assertContains(response, 'Le montant dépasse le reste à payer')
        self.assertFalse(RemiseBus.objects.exists())

    def test_remise_incomplete_refusee(self):
        response = self.client.post(reverse('bus:nouveau'), self.donnees(
            remise_modifiee='1', remise_type='POURCENTAGE', remise_valeur='120',
        ))
        self.assertContains(response, 'Un pourcentage ne peut pas dépasser 100 %')
        self.assertContains(response, 'Indiquez le motif de la remise')
        self.assertContains(response, 'Cochez au moins une tranche')

    def test_paiement_sans_toucher_a_la_remise_la_conserve(self):
        self.remise()
        response = self.client.post(reverse('bus:nouveau'), self.donnees())
        self.assertRedirects(response, reverse('bus:liste'))
        self.assertTrue(RemiseBus.objects.filter(eleve=self.eleve).exists())

    def test_aucune_remise_choisie_retire_la_remise(self):
        self.remise()
        response = self.client.post(reverse('bus:nouveau'), self.donnees(
            montant=150000, remise_modifiee='1', remise_type='',
        ))
        self.assertRedirects(response, reverse('bus:liste'))
        self.assertFalse(RemiseBus.objects.exists())

    def test_apercu_api_d_une_remise_non_enregistree(self):
        data = self.client.get(reverse('bus:tarif_bus_json'), {
            'eleve_id': self.eleve.pk, 'grille_id': self.grille.pk, 'periodicite': 'T1',
            'remise_modifiee': '1', 'remise_type': 'MONTANT_FIXE', 'remise_valeur': '50000',
            'remise_sur_tranche_1': '1', 'remise_sur_tranche_2': '0', 'remise_sur_tranche_3': '0',
        }).json()
        self.assertEqual((data['du'], data['plafond']), (100000, 350000))
        self.assertEqual([t['tranche'] for t in data['report']], ['T1', 'T2', 'T3'])
        self.assertFalse(RemiseBus.objects.exists())

    def test_reprise_propose_le_reste_net(self):
        self.remise()
        self.payer('T1', 135000)
        self.assertEqual(reprise_bus(self.eleve)['periodicite'], 'T2')
        self.assertEqual(reprise_bus(self.eleve)['montant'], 112500)

    # --- Liste des élèves avec remise -------------------------------------

    def test_liste_et_export_excel(self):
        self.remise()
        self.payer('T1', 100000)
        response = self.client.get(reverse('bus:remises'))
        self.assertContains(response, 'KADIATOU')
        self.assertContains(response, '360\xa0000')
        self.assertContains(response, '260\xa0000')
        response = self.client.get(reverse('bus:remises'), {'export': 'excel'})
        feuille = load_workbook(BytesIO(response.content)).active
        ligne = [cellule.value for cellule in feuille[5]]
        self.assertEqual(ligne[6:], ['10 %', 'Réduction fratrie', 'toutes les tranches', 400000, 40000, 360000, 100000, 260000])
        self.assertContains(self.client.get(reverse('bus:liste')), reverse('bus:remises'))

    def test_creation_modification_et_retrait_depuis_la_liste(self):
        response = self.client.post(reverse('bus:remise_nouvelle'), {
            'eleve': self.eleve.pk, 'grille': self.grille.pk, 'type_remise': 'MONTANT_FIXE',
            'valeur': '30000', 'motif': 'SOCIALE', 'sur_tranche_1': 'on', 'sur_tranche_3': 'on',
        })
        self.assertRedirects(response, reverse('bus:remises'))
        remise = RemiseBus.objects.get()
        self.assertEqual(remise.tranches, ['T1', 'T3'])
        self.client.post(reverse('bus:remise_modifier', args=[remise.pk]), {
            'type_remise': 'POURCENTAGE', 'valeur': '50', 'motif': 'SOCIALE', 'sur_tranche_1': 'on',
        })
        remise.refresh_from_db()
        self.assertEqual((remise.valeur, remise.tranches, remise.eleve_id), (Decimal('50'), ['T1'], self.eleve.pk))
        self.client.post(reverse('bus:remise_supprimer', args=[remise.pk]))
        self.assertFalse(RemiseBus.objects.exists())

    def test_doublon_refuse(self):
        self.remise()
        response = self.client.post(reverse('bus:remise_nouvelle'), {
            'eleve': self.eleve.pk, 'grille': self.grille.pk, 'type_remise': 'POURCENTAGE',
            'valeur': '5', 'motif': 'AUTRE', 'sur_tranche_1': 'on',
        })
        self.assertContains(response, 'déjà une remise sur cette grille')

    def test_autre_ecole_invisible(self):
        autre = Ecole.objects.create(nom='Autre École', adresse='Kindia', telephone='+224620000499', directeur='D')
        grille = GrilleTarifaireBus.objects.create(
            ecole=autre, zone='Kindia', annee_scolaire='2026-2027', tranche_1=1, tranche_2=1, tranche_3=1,
        )
        classe = Classe.objects.create(ecole=autre, nom='CE1', niveau='PRIMAIRE_2', annee_scolaire='2026-2027')
        eleve = Eleve.objects.create(matricule='EXT-1', prenom='Ibrahima', nom='Kaba', sexe='M', classe=classe)
        externe = RemiseBus.objects.create(eleve=eleve, grille=grille, valeur=10, motif='AUTRE')
        self.assertNotContains(self.client.get(reverse('bus:remises')), 'KABA')
        self.assertEqual(self.client.get(reverse('bus:remise_modifier', args=[externe.pk])).status_code, 404)

    # --- Reçus, carnet, liste du chauffeur --------------------------------

    def test_recu_affiche_tarif_remise_et_net(self):
        self.remise()
        paiement = self.payer('T1', 135000)
        texte = texte_pdf(self.client.get(reverse('bus:recu_pdf', args=[paiement.pk])).content)
        self.assertIn('Remise accordée', texte)
        self.assertIn('10 % — Réduction fratrie', texte)
        self.assertIn('REMISE', texte)
        self.assertIn('- 40 000', texte)
        self.assertIn('360 000', texte)

    def test_carnet_affiche_la_remise(self):
        self.remise()
        self.payer('T1', 135000)
        response = self.client.get(
            reverse('bus:historique_abonnements_export', args=['carnet']), {'eleve': self.eleve.pk},
        )
        texte = texte_pdf(response.content)
        self.assertIn('Remise bus', texte)
        self.assertIn('40 000 GNF déduits', texte)
        page = self.client.get(reverse('bus:historique_abonnements'), {'eleve': self.eleve.pk})
        self.assertContains(page, 'Remise bus — Kipé 2026-2027')

    def test_liste_du_chauffeur_affiche_la_remise_et_le_reste_net(self):
        self.remise()
        self.payer('T1', 135000)
        visibles = AbonnementBus.objects.all()
        ligne = lignes_controle(visibles, visibles)[0]
        self.assertEqual(ligne['reste'], Decimal('225000'))
        self.assertEqual(ligne['remise'], 'Remise 10 % (Réduction fratrie)')
        response = self.client.get(reverse('bus:export_controle', args=['excel']))
        feuille = load_workbook(BytesIO(response.content)).active
        self.assertEqual(feuille.cell(row=4, column=11).value, 'Remise')
        self.assertEqual(feuille.cell(row=5, column=11).value, 'Remise 10 % (Réduction fratrie)')
        self.assertEqual(feuille.cell(row=5, column=12).value, 225000)
        self.assertIn('Remise 10 %', texte_pdf(self.client.get(reverse('bus:export_controle', args=['pdf'])).content))
