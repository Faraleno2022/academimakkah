from datetime import date, datetime, timedelta
from io import BytesIO

from openpyxl import load_workbook
from pypdf import PdfReader

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from eleves.models import Classe, Ecole, Eleve
from paiements.tests.support import MIDDLEWARE_SANS_LICENCE
from utilisateurs.models import Profil

from .models import AbonnementBus, AbonnementCantine
from .recettes import CHAMP_DATE_BUS, CHAMP_DATE_CANTINE, debut_periode, recettes_par_periode


@override_settings(MIDDLEWARE=MIDDLEWARE_SANS_LICENCE)
class RecettesAbonnementsTests(TestCase):
    def test_totaux_sql_cantine_respectent_arrondis_et_tarif_absent(self):
        from decimal import Decimal
        from .periodicites import totaux_cantine
        objets = []
        for tarif in (None, 0, 1, 101, 9999999999):
            for type_remise, valeur in (
                ('', '0'), ('POURCENTAGE', '0.01'), ('POURCENTAGE', '50'),
                ('POURCENTAGE', '120'), ('MONTANT', '0.49'),
                ('MONTANT', '0.50'), ('MONTANT', '200'),
            ):
                objets.append(AbonnementCantine(
                    eleve=self.eleve, montant=40, montant_tarif=tarif,
                    type_remise=type_remise, valeur_remise=Decimal(valeur),
                    date_expiration=date(2027, 1, 1),
                ))
        AbonnementCantine.objects.bulk_create(objets)
        totaux = totaux_cantine(AbonnementCantine.objects.all())
        for cle, attribut in (
            ('brut', 'montant_brut'), ('remise', 'montant_remise'),
            ('du', 'montant_du'), ('paye', 'deja_paye'), ('reste', 'reste_a_payer'),
        ):
            self.assertEqual(totaux[cle], sum(getattr(objet, attribut) for objet in objets))

    def setUp(self):
        self.ecole = Ecole.objects.create(
            nom='École Recettes', adresse='Conakry', telephone='+224620000301', directeur='Direction',
        )
        autre_ecole = Ecole.objects.create(
            nom='Autre Recettes', adresse='Kindia', telephone='+224620000302', directeur='Direction',
        )
        classe = Classe.objects.create(
            ecole=self.ecole, nom='CE2', niveau='PRIMAIRE_3', annee_scolaire='2026-2027',
        )
        autre_classe = Classe.objects.create(
            ecole=autre_ecole, nom='CE2', niveau='PRIMAIRE_3', annee_scolaire='2026-2027',
        )
        self.eleve = Eleve.objects.create(matricule='REC-001', prenom='Fanta', nom='Camara', sexe='F', classe=classe)
        self.eleve2 = Eleve.objects.create(matricule='REC-002', prenom='Sekou', nom='Bah', sexe='M', classe=classe)
        self.autre_eleve = Eleve.objects.create(
            matricule='REC-900', prenom='Awa', nom='Sow', sexe='F', classe=autre_classe,
        )
        User = get_user_model()
        user = User.objects.create_user(username='admin_recettes', password='secret123')
        Profil.objects.update_or_create(
            user=user,
            defaults={
                'role': 'ADMIN', 'telephone': '+224620000311', 'ecole': self.ecole,
                'is_validated': True, 'allowed_menus': ['bus'],
            },
        )
        self.client.force_login(user)

    def _bus(self, eleve, paye_le, montant):
        return AbonnementBus.objects.create(
            eleve=eleve, periodicite='T1', montant=montant,
            date_debut=paye_le, date_expiration=paye_le + timedelta(days=90),
        )

    def _cantine(self, eleve, enregistre_le, montant):
        abo = AbonnementCantine.objects.create(
            eleve=eleve, montant=montant, periodicite='MENSUEL', type_repas='REPAS_10H',
            date_debut=enregistre_le + timedelta(days=15),
            date_expiration=enregistre_le + timedelta(days=45),
        )
        instant = timezone.make_aware(datetime.combine(enregistre_le, datetime.min.time()) + timedelta(hours=10))
        AbonnementCantine.objects.filter(pk=abo.pk).update(created_at=instant)
        return abo

    def test_debut_des_periodes(self):
        mercredi = date(2026, 9, 23)
        self.assertEqual(debut_periode('jour', mercredi), mercredi)
        self.assertEqual(debut_periode('semaine', mercredi), date(2026, 9, 21))
        self.assertEqual(debut_periode('mois', mercredi), date(2026, 9, 1))
        self.assertEqual(debut_periode('annee', mercredi), date(2026, 1, 1))

    def test_recettes_bus_par_periode(self):
        today = date(2026, 9, 23)
        self._bus(self.eleve, today, 100000)
        self._bus(self.eleve, date(2026, 9, 21), 50000)
        self._bus(self.eleve2, date(2026, 9, 2), 30000)
        self._bus(self.eleve2, date(2026, 2, 10), 20000)
        self._bus(self.eleve2, date(2025, 12, 31), 10000)
        self._bus(self.eleve2, date(2026, 9, 24), 5000)  # daté dans le futur : pas encore encaissé
        recettes = {
            r['code']: r for r in recettes_par_periode(AbonnementBus.objects.all(), CHAMP_DATE_BUS, today)
        }
        self.assertEqual(
            {code: int(r['montant']) for code, r in recettes.items()},
            {'jour': 100000, 'semaine': 150000, 'mois': 180000, 'annee': 200000},
        )
        self.assertEqual((recettes['semaine']['nb'], recettes['semaine']['nb_eleves']), (2, 1))
        self.assertEqual((recettes['annee']['nb'], recettes['annee']['nb_eleves']), (4, 2))

    def test_recettes_cantine_comptent_la_date_d_enregistrement(self):
        today = date(2026, 9, 23)
        # Réabonnement payé aujourd'hui mais qui ne démarre que dans 15 jours.
        self._cantine(self.eleve, today, 300000)
        self._cantine(self.eleve2, date(2026, 8, 30), 250000)
        recettes = {
            r['code']: r for r in recettes_par_periode(AbonnementCantine.objects.all(), CHAMP_DATE_CANTINE, today)
        }
        self.assertEqual(int(recettes['jour']['montant']), 300000)
        self.assertEqual(int(recettes['mois']['montant']), 300000)
        self.assertEqual(int(recettes['annee']['montant']), 550000)

    def test_liste_bus_filtre_par_periode_et_bouton_details(self):
        today = timezone.localdate()
        self._bus(self.eleve, today, 100000)
        self._bus(self.eleve2, today.replace(year=today.year - 1), 40000)
        self._bus(self.autre_eleve, today, 999000)  # autre école : exclu
        response = self.client.get(reverse('bus:liste'), {'periode': 'jour'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([a.eleve_id for a in response.context['abonnements']], [self.eleve.pk])
        self.assertEqual(int(response.context['total_montant']), 100000)
        recettes = {r['code']: r for r in response.context['recettes']}
        self.assertEqual(int(recettes['jour']['montant']), 100000)
        self.assertContains(response, 'Détails')
        self.assertContains(response, f"?eleve={self.eleve.pk}&type=bus")

    def test_liste_et_export_cantine_filtres_par_periode(self):
        today = timezone.localdate()
        self._cantine(self.eleve, today, 300000)
        self._cantine(self.eleve2, today.replace(year=today.year - 1), 200000)
        response = self.client.get(reverse('bus:liste_abonnements_cantine'), {'periode': 'annee'})
        self.assertEqual([a.eleve_id for a in response.context['abonnements']], [self.eleve.pk])
        self.assertEqual(int(response.context['total_filtre']), 300000)
        self.assertContains(response, 'Détails')
        export = self.client.get(reverse('bus:export_cantine_excel'), {'periode': 'annee'})
        self.assertTrue(export.content.startswith(b'PK'))

    def test_cantine_sans_tarif_distinct_est_deja_payee_et_exportable(self):
        today = timezone.localdate()
        abonnement = self._cantine(self.eleve, today, 300000)
        self._cantine(self.autre_eleve, today, 900000)
        self.assertEqual(int(abonnement.deja_paye), 300000)
        self.assertEqual(int(abonnement.reste_a_payer), 0)
        liste = self.client.get(reverse('bus:liste_abonnements_cantine'))
        self.assertContains(liste, 'Déjà payé')
        self.assertEqual(int(liste.context['total_reste']), 0)
        self.assertEqual(int(liste.context['total_filtre']), 300000)
        excel = self.client.get(reverse('bus:export_cantine_excel'))
        ws = load_workbook(BytesIO(excel.content), data_only=True).active
        self.assertEqual(ws.max_row, 5)
        entetes = [cell.value for cell in ws[4]]
        self.assertEqual(ws.cell(5, entetes.index('Déjà payé (GNF)') + 1).value, 300000)
        self.assertEqual(ws.cell(5, entetes.index('Reste à payer (GNF)') + 1).value, 0)
        pdf = self.client.get(reverse('bus:export_cantine_pdf'))
        self.assertEqual(pdf.status_code, 200)
        self.assertTrue(pdf.content.startswith(b'%PDF'))
        texte = ' '.join(page.extract_text() for page in PdfReader(BytesIO(pdf.content)).pages)
        self.assertIn('REC-001', texte)
        self.assertNotIn('REC-900', texte)

    def test_periodicites_cantine_totaux_remises_listes_et_export(self):
        today = timezone.localdate()
        trimestre = self._cantine(self.eleve, today, 150000)
        trimestre.periodicite = 'TRIMESTRIEL'
        trimestre.montant_tarif = 300000
        trimestre.type_remise = 'POURCENTAGE'
        trimestre.valeur_remise = 10
        trimestre.save(update_fields=['periodicite', 'montant_tarif', 'type_remise', 'valeur_remise'])
        second = self._cantine(self.eleve2, today, 200000)
        second.periodicite = 'TRIMESTRIEL'
        second.save(update_fields=['periodicite'])
        annuel = self._cantine(self.eleve2, today, 450000)
        annuel.periodicite = 'ANNUEL'
        annuel.save(update_fields=['periodicite'])
        autre = self._cantine(self.autre_eleve, today, 999000)
        autre.periodicite = 'TRIMESTRIEL'
        autre.save(update_fields=['periodicite'])

        url = reverse('bus:liste_abonnements_cantine')
        liste = self.client.get(url)
        lignes = {row['code']: row for row in liste.context['periodicite_rows']}
        self.assertEqual((lignes['TRIMESTRIEL']['nb'], lignes['ANNUEL']['nb']), (2, 1))
        self.assertEqual(
            tuple(int(lignes['ANNUEL'][key]) for key in ('brut', 'du', 'paye', 'reste')),
            (450000, 450000, 450000, 0),
        )
        self.assertEqual(
            tuple(int(lignes['TRIMESTRIEL'][key]) for key in ('brut', 'remise', 'du', 'paye', 'reste')),
            (500000, 30000, 470000, 350000, 120000),
        )
        self.assertContains(liste, f'{url}?periodicite=TRIMESTRIEL#liste-abonnements')
        filtree = self.client.get(url, {'periodicite': 'TRIMESTRIEL'})
        self.assertEqual({a.pk for a in filtree.context['abonnements']}, {trimestre.pk, second.pk})
        self.assertEqual(int(filtree.context['total_reste']), 120000)
        self.assertContains(filtree, 'name="periodicite" value="TRIMESTRIEL"')

        excel = self.client.get(reverse('bus:export_cantine_excel'), {'periodicite': 'TRIMESTRIEL'})
        ws = load_workbook(BytesIO(excel.content), data_only=True).active
        self.assertEqual(ws.max_row, 6)
        self.assertEqual({ws.cell(row, 3).value for row in (5, 6)}, {'REC-001', 'REC-002'})

    def test_bouton_cantine_affiche_seulement_les_abonnements_avec_remise(self):
        today = timezone.localdate()
        abonnement = self._cantine(self.eleve, today, 180000)
        abonnement.montant_tarif = 200000
        abonnement.type_remise = 'POURCENTAGE'
        abonnement.valeur_remise = 10
        abonnement.save(update_fields=['montant_tarif', 'type_remise', 'valeur_remise'])
        self._cantine(self.eleve2, today, 100000)
        remise_arrondie_a_zero = self._cantine(self.eleve2, today, 1)
        remise_arrondie_a_zero.montant_tarif = 1
        remise_arrondie_a_zero.type_remise = 'POURCENTAGE'
        remise_arrondie_a_zero.valeur_remise = 1
        remise_arrondie_a_zero.save(update_fields=['montant_tarif', 'type_remise', 'valeur_remise'])
        autre = self._cantine(self.autre_eleve, today, 450000)
        autre.montant_tarif = 500000
        autre.type_remise = 'POURCENTAGE'
        autre.valeur_remise = 10
        autre.save(update_fields=['montant_tarif', 'type_remise', 'valeur_remise'])

        url = reverse('bus:liste_abonnements_cantine')
        liste = self.client.get(url)
        self.assertContains(liste, 'Élèves avec remise')
        self.assertContains(liste, f'{url}?remise=oui')
        remises = self.client.get(url, {'remise': 'oui'})
        self.assertEqual([abo.eleve_id for abo in remises.context['abonnements']], [self.eleve.pk])
        self.assertEqual(int(remises.context['total_remise']), 20000)
        self.assertContains(remises, 'name="remise" value="oui"')

        excel = self.client.get(reverse('bus:export_cantine_excel'), {'remise': 'oui'})
        ws = load_workbook(BytesIO(excel.content), data_only=True).active
        self.assertEqual(ws.max_row, 5)
        self.assertEqual(ws.cell(5, 3).value, 'REC-001')
        pdf = self.client.get(reverse('bus:export_cantine_pdf'), {'remise': 'oui'})
        texte = ' '.join(page.extract_text() for page in PdfReader(BytesIO(pdf.content)).pages)
        self.assertIn('REC-001', texte)
        self.assertNotIn('REC-002', texte)
        self.assertNotIn('REC-900', texte)

    def test_liste_cantine_pagine_sans_reduire_les_soldes_ni_les_exports(self):
        today = timezone.localdate()
        AbonnementCantine.objects.bulk_create([
            AbonnementCantine(
                eleve=self.eleve, montant=40, montant_tarif=100,
                type_remise='MONTANT', valeur_remise=10,
                periodicite='MENSUEL', date_debut=today,
                date_expiration=today + timedelta(days=30),
            )
            for _ in range(101)
        ])
        url = reverse('bus:liste_abonnements_cantine')
        premiere = self.client.get(url, {'remise': 'oui'})
        seconde = self.client.get(url, {'remise': 'oui', 'page': 2})
        self.assertEqual((len(premiere.context['abonnements']), len(seconde.context['abonnements'])), (100, 1))
        self.assertEqual(premiere.context['total_count'], 101)
        self.assertEqual(int(premiere.context['total_filtre']), 4040)
        self.assertEqual(int(premiere.context['total_remise']), 1010)
        self.assertEqual(int(seconde.context['total_reste']), 5050)
        self.assertContains(premiere, 'remise=oui&amp;page=2')
        excel = self.client.get(reverse('bus:export_cantine_excel'), {'remise': 'oui'})
        self.assertEqual(load_workbook(BytesIO(excel.content), data_only=True).active.max_row, 105)

    def test_details_cantine_reste_dans_la_cantine(self):
        today = timezone.localdate()
        self._bus(self.eleve, today, 100000)
        self._cantine(self.eleve, today, 300000)
        liste = self.client.get(reverse('bus:liste_abonnements_cantine'))
        details = f"{reverse('bus:historique_abonnements')}?eleve={self.eleve.pk}&type=cantine"
        self.assertContains(liste, details)
        page = self.client.get(details)
        self.assertContains(page, "Abonnements cantine d&#x27;un élève")
        self.assertContains(page, reverse('bus:liste_abonnements_cantine'))
        self.assertNotContains(page, 'Bus scolaire</a>')
        self.assertNotContains(page, 'Nouveau paiement bus')
        self.assertEqual([ligne['service'] for ligne in page.context['lignes']], ['Cantine'])
        bus = self.client.get(f"{reverse('bus:historique_abonnements')}?eleve={self.eleve.pk}&type=bus")
        self.assertContains(bus, "Abonnements bus d&#x27;un élève")
        self.assertNotContains(bus, 'Nouvel abonnement cantine')

    def test_tableaux_de_bord_affichent_les_recettes(self):
        today = timezone.localdate()
        self._bus(self.eleve, today, 125000)
        self._cantine(self.eleve, today, 300000)
        bus = self.client.get(reverse('bus:index'))  # la liste sert de tableau de bord bus
        self.assertEqual(int({r['code']: r for r in bus.context['recettes']}['jour']['montant']), 125000)
        self.assertContains(bus, 'Recettes bus encaissées')
        cantine = self.client.get(reverse('bus:tableau_bord_cantine'))
        self.assertEqual(int({r['code']: r for r in cantine.context['recettes']}['jour']['montant']), 300000)
        self.assertContains(cantine, 'Recettes cantine encaissées')
        self.assertEqual(int(cantine.context['montant_abonnements']), 300000)
        self.assertEqual(int(cantine.context['deja_paye']), 300000)
        self.assertEqual(int(cantine.context['reste_a_payer']), 0)
        self.assertContains(cantine, reverse('bus:export_cantine_pdf'))
