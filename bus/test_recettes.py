from datetime import date, datetime, timedelta

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
