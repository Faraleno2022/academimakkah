"""Historique bus/cantine indépendant des passages de classe et des exports."""
from datetime import date
from decimal import Decimal
from importlib import import_module
from io import BytesIO
from types import SimpleNamespace

from django.apps import apps
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from openpyxl import load_workbook

from eleves.models import Classe, Ecole, Eleve
from eleves.utils_annee import SESSION_ANNEE_ACTIVE
from synchronisation.engine import serialize_instance

from .forms import AbonnementCantineForm
from .historique_eleve import reprise_bus, reprise_cantine
from .models import AbonnementBus, AbonnementCantine, GrilleTarifaireBus, RemiseBus


@override_settings(MIDDLEWARE=[m for m in settings.MIDDLEWARE if 'licence_middleware' not in m])
class AnneesAbonnementsTests(TestCase):
    def setUp(self):
        self.ecole = Ecole.objects.create(nom='École années', adresse='Conakry',
            telephone='600111222', directeur='Direction')
        self.ancienne = Classe.objects.create(ecole=self.ecole, nom='CE1', niveau='PRIMAIRE_2',
            annee_scolaire='2025-2026')
        self.nouvelle = Classe.objects.create(ecole=self.ecole, nom='CE2', niveau='PRIMAIRE_3',
            annee_scolaire='2026-2027')
        self.eleve = Eleve.objects.create(matricule='ANN-001', nom='Camara', prenom='Aminata',
            sexe='F', classe=self.nouvelle)
        self.user = get_user_model().objects.create_user('annees-admin', password='test')
        profil = self.user.profil
        profil.ecole, profil.role, profil.is_validated = self.ecole, 'ADMIN', True
        profil.allowed_menus = ['bus']
        profil.save()
        self.client.force_login(self.user)
        self.grille_ancienne = GrilleTarifaireBus.objects.create(ecole=self.ecole,
            zone='Kipé', annee_scolaire='2025-2026', tranche_1=100000, tranche_2=100000, tranche_3=100000)
        self.grille_nouvelle = GrilleTarifaireBus.objects.create(ecole=self.ecole,
            zone='Kipé', annee_scolaire='2026-2027', tranche_1=120000, tranche_2=110000, tranche_3=100000)
        self.bus_ancien = AbonnementBus.objects.create(eleve=self.eleve, grille=self.grille_ancienne,
            montant=50000, periodicite='T1', date_debut=date(2025, 10, 1), date_expiration=date(2026, 6, 30))
        self.bus_nouveau = AbonnementBus.objects.create(eleve=self.eleve, grille=self.grille_nouvelle,
            montant=100000, periodicite='T1', date_debut=date(2026, 10, 1), date_expiration=date(2027, 6, 30))
        self.cantine_ancienne = AbonnementCantine.objects.create(eleve=self.eleve, montant=60000,
            montant_tarif=100000, type_remise='POURCENTAGE', valeur_remise=10,
            date_debut=date(2025, 10, 1), date_expiration=date(2026, 6, 30))
        self.cantine_nouvelle = AbonnementCantine.objects.create(eleve=self.eleve, montant=90000,
            montant_tarif=120000, type_remise='POURCENTAGE', valeur_remise=10,
            date_debut=date(2026, 10, 1), date_expiration=date(2027, 6, 30))

    def test_liste_bus_et_totaux_sont_ceux_de_l_annee_active(self):
        response = self.client.get(reverse('bus:liste'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual([a.pk for a in response.context['abonnements']], [self.bus_nouveau.pk])
        self.assertEqual(response.context['total_montant'], 100000)
        self.assertEqual(response.context['total_reste_bus'], 230000)
        self.assertContains(response, 'name="annee"')
        self.assertEqual(response.context['annee_abonnements'], '2026-2027')

    def test_superadministrateur_associe_a_une_ecole_utilise_aussi_son_annee_active(self):
        self.user.is_superuser = True
        self.user.save(update_fields=['is_superuser'])
        response = self.client.get(reverse('bus:liste_abonnements_cantine'))
        self.assertEqual(response.context['total_count'], 1)
        self.assertEqual(response.context['annee_abonnements'], '2026-2027')

    def test_liste_cantine_et_remises_separees_par_annee(self):
        response = self.client.get(reverse('bus:liste_abonnements_cantine'), {'remise': 'oui'})
        self.assertEqual([a.pk for a in response.context['abonnements']], [self.cantine_nouvelle.pk])
        self.assertEqual(response.context['total_tarif'], 120000)
        self.assertEqual(response.context['total_remise'], 12000)
        self.assertEqual(response.context['total_filtre'], 90000)
        self.assertEqual(response.context['total_reste'], 18000)
        self.assertContains(response, 'annee=2026-2027')

    def test_consulter_ancienne_annee_meme_apres_changement_de_classe(self):
        session = self.client.session
        session[SESSION_ANNEE_ACTIVE] = '2025-2026'
        session.save()
        bus = self.client.get(reverse('bus:liste'))
        cantine = self.client.get(reverse('bus:liste_abonnements_cantine'))
        self.assertEqual([a.pk for a in bus.context['abonnements']], [self.bus_ancien.pk])
        self.assertEqual([a.pk for a in cantine.context['abonnements']], [self.cantine_ancienne.pk])
        self.assertEqual(bus.context['total_reste_bus'], 250000)
        self.assertEqual(cantine.context['total_reste'], 30000)

    def test_historique_toutes_annees_reste_accessible(self):
        for url in ('bus:liste', 'bus:liste_abonnements_cantine'):
            response = self.client.get(reverse(url), {'annee': 'toutes'})
            self.assertEqual(response.context['total_count'], 2)

    def test_exports_excel_respectent_la_meme_annee_que_la_liste(self):
        for url, args, encaisse in (
            ('bus:export_abonnements_bus', ['excel'], 50000),
            ('bus:export_cantine_excel', [], 60000),
        ):
            response = self.client.get(reverse(url, args=args), {'annee': '2025-2026'})
            self.assertEqual(response.status_code, 200)
            rows = list(load_workbook(BytesIO(response.content), data_only=True).active.values)
            # Chaque export ne contient qu'un reçu, plus ses titres et totaux.
            lignes = [row for row in rows if 'ANN-001' in row]
            self.assertEqual(len(lignes), 1, rows)
            self.assertIn('2025-2026', lignes[0])
            self.assertIn(encaisse, lignes[0])

    def test_exports_pdf_respectent_l_annee(self):
        from unittest.mock import patch
        from django.http import HttpResponse
        for url, args, module in (
            ('bus:export_abonnements_bus', ['pdf'], 'bus.views'),
            ('bus:export_cantine_pdf', [], 'bus.views_cantine'),
        ):
            with patch(f'{module}.exporter_liste', return_value=HttpResponse('PDF')) as export:
                self.client.get(reverse(url, args=args), {'annee': '2025-2026'})
            lignes_excel = export.call_args.args[4]
            self.assertEqual(len(lignes_excel), 1)
            self.assertIn('2025-2026', lignes_excel[0])

    def test_tableaux_de_bord_et_alertes_utilisent_l_annee_active(self):
        for url in ('bus:tableau_bord_cantine',):
            response = self.client.get(reverse(url))
            self.assertEqual(response.context['total'], 1)
        response = self.client.get(reverse('bus:alertes_cantine_json'), {'annee': '2026-2027'})
        self.assertEqual(response.json()['total'], 1)

    def test_remises_bus_ne_melangent_pas_les_grilles_des_deux_annees(self):
        for grille in (self.grille_ancienne, self.grille_nouvelle):
            RemiseBus.objects.create(eleve=self.eleve, grille=grille, type_remise='POURCENTAGE',
                valeur=10, motif='AUTRE')
        response = self.client.get(reverse('bus:remises'))
        self.assertEqual(len(response.context['lignes']), 1)
        self.assertEqual(response.context['lignes'][0]['grille'].annee_scolaire, '2026-2027')

    def test_annee_inconnue_ne_revele_pas_les_autres_annees(self):
        response = self.client.get(reverse('bus:liste_abonnements_cantine'), {'annee': '2040-2041'})
        self.assertEqual(response.context['total_count'], 0)

    def test_historique_toutes_annees_reste_limite_a_l_ecole(self):
        autre = Ecole.objects.create(nom='Autre école', adresse='Kindia', telephone='600000003', directeur='D')
        classe = Classe.objects.create(ecole=autre, nom='CE2', niveau='PRIMAIRE_3', annee_scolaire='2026-2027')
        eleve = Eleve.objects.create(matricule='AUTRE-001', nom='Autre', prenom='Élève', classe=classe)
        AbonnementCantine.objects.create(eleve=eleve, montant=999999,
            date_debut=date(2026, 10, 1), date_expiration=date(2027, 6, 30))
        response = self.client.get(reverse('bus:liste_abonnements_cantine'), {'annee': 'toutes'})
        self.assertEqual(response.context['total_count'], 2)

    def test_modification_cantine_ne_deplace_pas_l_encaissement_d_annee(self):
        self.cantine_ancienne.observations = 'Correction du contact'
        self.cantine_ancienne.save(update_fields=['observations'])
        self.cantine_ancienne.refresh_from_db()
        self.assertEqual(self.cantine_ancienne.annee_scolaire, '2025-2026')
        data = serialize_instance(self.cantine_ancienne)
        self.assertEqual(data['annee_scolaire'], '2025-2026')

    def test_reprise_bus_propose_la_nouvelle_grille_et_son_reste(self):
        self.bus_nouveau.delete()
        reprise = reprise_bus(self.eleve)
        self.assertEqual(reprise['grille'], self.grille_nouvelle.pk)
        self.assertEqual(reprise['periodicite'], 'T1')
        self.assertEqual(reprise['montant'], 120000)

    def test_reprise_bus_ne_reutilise_pas_une_ancienne_grille_a_defaut(self):
        self.bus_nouveau.delete()
        self.grille_nouvelle.delete()
        self.assertNotIn('grille', reprise_bus(self.eleve))

    def test_reprise_cantine_propose_tarif_et_remise_sans_creer_de_paiement(self):
        reprise = reprise_cantine(self.eleve)
        self.assertEqual(reprise['annee_scolaire'], '2026-2027')
        self.assertEqual(reprise['montant_tarif'], 120000)
        self.assertEqual(reprise['type_remise'], 'POURCENTAGE')
        self.assertEqual(Decimal(reprise['valeur_remise']), 10)
        self.assertEqual(AbonnementCantine.objects.count(), 2)

    def test_migration_classe_les_anciens_releves_sans_changer_leurs_montants(self):
        AbonnementCantine.objects.filter(pk=self.cantine_ancienne.pk).update(annee_scolaire='')
        AbonnementBus.objects.filter(pk=self.bus_ancien.pk).update(annee_scolaire='')
        migration = import_module('bus.migrations.0012_abonnements_annee_scolaire')
        migration.renseigner_annees(apps, SimpleNamespace(connection=SimpleNamespace(alias='default')))
        migration.renseigner_annees(apps, SimpleNamespace(connection=SimpleNamespace(alias='default')))
        self.cantine_ancienne.refresh_from_db()
        self.bus_ancien.refresh_from_db()
        self.assertEqual(self.cantine_ancienne.annee_scolaire, '2025-2026')
        self.assertEqual(self.cantine_ancienne.montant, 60000)
        self.assertEqual(self.cantine_ancienne.reste_a_payer, 30000)
        self.assertEqual(self.bus_ancien.annee_scolaire, '2025-2026')
        self.assertEqual(self.bus_ancien.montant, 50000)

    def test_ligne_recue_sans_annee_se_classe_par_date(self):
        AbonnementCantine.objects.filter(pk=self.cantine_ancienne.pk).update(annee_scolaire='')
        response = self.client.get(reverse('bus:liste_abonnements_cantine'))
        self.assertEqual(response.context['total_count'], 1)
        response = self.client.get(reverse('bus:liste_abonnements_cantine'), {'annee': '2025-2026'})
        self.assertEqual([a.pk for a in response.context['abonnements']], [self.cantine_ancienne.pk])

    def test_migration_traite_plusieurs_lots_sans_perdre_de_lignes(self):
        AbonnementCantine.objects.bulk_create([
            AbonnementCantine(eleve=self.eleve, montant=100, date_debut=date(2020, 2, 1),
                date_expiration=date(2020, 3, 1)) for _ in range(501)
        ])
        migration = import_module('bus.migrations.0012_abonnements_annee_scolaire')
        migration.renseigner_annees(apps, SimpleNamespace(connection=SimpleNamespace(alias='default')))
        self.assertEqual(AbonnementCantine.objects.filter(annee_scolaire='2019-2020').count(), 501)
        self.assertFalse(AbonnementCantine.objects.filter(annee_scolaire='').exists())

    def test_formulaire_cantine_conserve_l_annee_du_recu_modifie(self):
        data = {'eleve': self.eleve.pk, 'montant_tarif': 100000, 'montant': 60000,
            'type_remise': 'POURCENTAGE', 'valeur_remise': 10, 'periodicite': 'MENSUEL',
            'type_repas': 'DEJEUNER', 'date_debut': '2025-10-01', 'statut': 'ACTIF',
            'alerte_avant_jours': 7}
        form = AbonnementCantineForm(data, instance=self.cantine_ancienne, ecole=self.ecole)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().annee_scolaire, '2025-2026')
