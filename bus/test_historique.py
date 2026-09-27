from datetime import date, timedelta
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from eleves.models import Classe, Ecole, Eleve
from paiements.models import ModePaiement
from paiements.tests.support import MIDDLEWARE_SANS_LICENCE
from utilisateurs.models import Profil

from .historique_eleve import date_expiration_cantine, libelle_mois, lignes_abonnements
from .models import AbonnementBus, AbonnementCantine, GrilleTarifaireBus


@override_settings(MIDDLEWARE=MIDDLEWARE_SANS_LICENCE)
class HistoriqueAbonnementsTests(TestCase):
    def setUp(self):
        self.ecole = Ecole.objects.create(
            nom='École Historique', adresse='Conakry',
            telephone='+224620000201', directeur='Direction',
        )
        self.autre_ecole = Ecole.objects.create(
            nom='Autre École', adresse='Conakry',
            telephone='+224620000202', directeur='Direction',
        )
        self.classe = Classe.objects.create(
            ecole=self.ecole, nom='CM1', niveau='PRIMAIRE_4', annee_scolaire='2026-2027',
        )
        autre_classe = Classe.objects.create(
            ecole=self.autre_ecole, nom='CM2', niveau='PRIMAIRE_5', annee_scolaire='2026-2027',
        )
        self.eleve = Eleve.objects.create(
            matricule='HIS-001', prenom='Aïssatou', nom='Diallo', sexe='F', classe=self.classe,
        )
        self.autre_eleve = Eleve.objects.create(
            matricule='HIS-900', prenom='Mory', nom='Kaba', sexe='M', classe=autre_classe,
        )
        self.mode = ModePaiement.objects.create(nom='Espèces Historique')
        self.grille = GrilleTarifaireBus.objects.create(
            ecole=self.ecole, zone='Ratoma', annee_scolaire='2026-2027',
            tranche_1=150000, tranche_2=125000, tranche_3=125000,
            date_echeance_tranche_1=date(2026, 10, 15),
            date_echeance_tranche_2=date(2027, 1, 15),
            date_echeance_tranche_3=date(2027, 4, 15),
        )
        self.today = timezone.localdate()
        self.cantine = AbonnementCantine.objects.create(
            eleve=self.eleve, montant=300000, periodicite='MENSUEL', type_repas='REPAS_10H',
            date_debut=self.today - timedelta(days=10),
            date_expiration=self.today + timedelta(days=20),
            regime_alimentaire='Sans arachide', allergies='Arachides',
            contact_parent='+224622000000',
        )
        self.bus = AbonnementBus.objects.create(
            eleve=self.eleve, grille=self.grille, periodicite='T1', montant=150000,
            date_debut=date(2026, 9, 5), date_expiration=date(2027, 4, 15),
            mode_paiement=self.mode, point_arret='Carrefour Cosa', contact_parent='+224622111111',
        )
        User = get_user_model()
        self.user = User.objects.create_user(username='admin_historique', password='secret123')
        Profil.objects.update_or_create(
            user=self.user,
            defaults={
                'role': 'ADMIN', 'telephone': '+224620000211', 'ecole': self.ecole,
                'is_validated': True, 'allowed_menus': ['bus'],
            },
        )
        self.client.force_login(self.user)

    def test_calculs_de_dates_et_mois(self):
        self.assertEqual(date_expiration_cantine(date(2026, 1, 31), 'MENSUEL'), date(2026, 2, 27))
        self.assertEqual(date_expiration_cantine(date(2026, 9, 1), 'TRIMESTRIEL'), date(2026, 11, 30))
        self.assertEqual(date_expiration_cantine(date(2026, 9, 1), 'JOURNALIER'), date(2026, 9, 1))
        self.assertEqual(libelle_mois(date(2026, 9, 1), date(2026, 9, 30)), 'Septembre 2026')
        self.assertEqual(
            libelle_mois(date(2026, 9, 1), date(2026, 11, 30)), 'Septembre 2026 → Novembre 2026'
        )

    def test_nouvel_abonnement_cantine_reprend_les_infos_de_l_eleve(self):
        response = self.client.get(reverse('bus:creer_abonnement_cantine'), {'eleve': self.eleve.pk})
        initial = response.context['form'].initial
        debut = self.cantine.date_expiration + timedelta(days=1)
        self.assertEqual(initial['regime_alimentaire'], 'Sans arachide')
        self.assertEqual(initial['allergies'], 'Arachides')
        self.assertEqual(initial['contact_parent'], '+224622000000')
        self.assertEqual(initial['type_repas'], 'REPAS_10H')
        self.assertEqual(initial['montant'], 300000)
        self.assertEqual(initial['date_debut'], debut)
        self.assertEqual(initial['date_expiration'], date_expiration_cantine(debut, 'MENSUEL'))

    def test_api_eleve_renvoie_la_reprise(self):
        data = self.client.get(reverse('bus:get_eleve_info_json', args=[self.eleve.pk])).json()
        self.assertEqual(data['nb_abonnements_cantine'], 1)
        self.assertEqual(data['nb_abonnements_bus'], 1)
        self.assertEqual(data['reprise_cantine']['allergies'], 'Arachides')
        self.assertEqual(data['reprise_bus']['grille'], self.grille.pk)
        self.assertEqual(data['reprise_bus']['periodicite'], 'T2')
        self.assertEqual(data['reprise_bus']['montant'], 125000)
        self.assertEqual(data['reprise_bus']['mode_paiement'], self.mode.pk)

    def test_nouveau_paiement_bus_reprend_arret_et_contact(self):
        response = self.client.post(reverse('bus:nouveau'), {
            'eleve': self.eleve.pk, 'grille': self.grille.pk, 'periodicite': 'T2',
            'montant': 125000, 'date_debut': '2026-12-01', 'mode_paiement': self.mode.pk,
        })
        self.assertRedirects(response, reverse('bus:liste'))
        nouveau = AbonnementBus.objects.filter(eleve=self.eleve, periodicite='T2').get()
        self.assertEqual(nouveau.point_arret, 'Carrefour Cosa')
        self.assertEqual(nouveau.contact_parent, '+224622111111')

    def test_historique_liste_les_abonnements_bus_et_cantine(self):
        response = self.client.get(reverse('bus:historique_abonnements'), {'eleve': self.eleve.pk})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['lignes']), 2)
        self.assertEqual(response.context['resume']['total'], 450000)
        cantine = self.client.get(
            reverse('bus:historique_abonnements'), {'eleve': self.eleve.pk, 'type': 'cantine'}
        )
        self.assertEqual([l['service'] for l in cantine.context['lignes']], ['Cantine'])
        ligne = lignes_abonnements(self.eleve, 'cantine')[0]
        self.assertTrue(ligne['jour_expiration'])

    def test_exports_pdf_excel_et_carnet(self):
        url = lambda fmt: reverse('bus:historique_abonnements_export', args=[fmt])
        pdf = self.client.get(url('pdf'), {'eleve': self.eleve.pk})
        self.assertEqual(pdf['Content-Type'], 'application/pdf')
        self.assertTrue(pdf.content.startswith(b'%PDF'))
        carnet = self.client.get(url('carnet'), {'eleve': self.eleve.pk})
        self.assertTrue(carnet.content.startswith(b'%PDF'))
        classe = self.client.get(url('carnet'), {'classe': self.classe.pk})
        self.assertTrue(classe.content.startswith(b'%PDF'))
        excel = self.client.get(url('excel'), {'eleve': self.eleve.pk})
        self.assertIn('spreadsheetml', excel['Content-Type'])
        self.assertTrue(excel.content.startswith(b'PK'))

    def test_eleve_d_une_autre_ecole_est_inaccessible(self):
        response = self.client.get(reverse('bus:historique_abonnements'), {'eleve': self.autre_eleve.pk})
        self.assertIsNone(response.context['eleve'])
        export = self.client.get(
            reverse('bus:historique_abonnements_export', args=['pdf']), {'eleve': self.autre_eleve.pk}
        )
        self.assertEqual(export.status_code, 302)

    @skipUnless(shutil.which('node'), "Node.js requis pour vérifier la syntaxe JavaScript")
    def test_scripts_des_formulaires_sont_valides(self):
        """Une erreur de syntaxe JS désactive le filtre classe → élève et la reprise des infos."""
        pages = [
            reverse('bus:nouveau'), f"{reverse('bus:nouveau')}?eleve={self.eleve.pk}",
            reverse('bus:creer_abonnement_cantine'),
            f"{reverse('bus:creer_abonnement_cantine')}?eleve={self.eleve.pk}",
            f"{reverse('bus:historique_abonnements')}?eleve={self.eleve.pk}",
        ]
        motif = re.compile(r'<script(?![^>]*(?:src=|ld.json))[^>]*>(.*?)</script>', re.S)
        with tempfile.TemporaryDirectory() as dossier:
            for page in pages:
                html = self.client.get(page).content.decode()
                for numero, script in enumerate(motif.findall(html)):
                    fichier = Path(dossier) / f'script_{numero}.js'
                    fichier.write_text(script, encoding='utf-8')
                    resultat = subprocess.run(
                        ['node', '--check', str(fichier)], capture_output=True, text=True,
                    )
                    self.assertEqual(resultat.returncode, 0, f"{page} : {resultat.stderr}")
