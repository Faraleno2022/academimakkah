"""Reçus compacts : 4 reçus par feuille A4."""
from io import BytesIO

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from paiements import tests_modification_paiement as fixtures
from paiements.recus_compacts import generer_recus_4_par_feuille_pdf
from paiements.tests.support import MIDDLEWARE_SANS_LICENCE
from paiements.views import _valider_paiement_impl


@override_settings(MIDDLEWARE=MIDDLEWARE_SANS_LICENCE)
class Recus4ParFeuilleTests(TestCase):
    def setUp(self):
        fixtures.ApplicationRemisePaiementTest.setUp(self)
        self.paiement.montant = 100000
        self.paiement.save()
        _valider_paiement_impl(self.paiement, self.user)
        self.paiement.refresh_from_db()
        self.url = reverse('paiements:generer_recus_4_par_feuille_pdf')

    def _texte(self, contenu):
        from pypdf import PdfReader
        pages = PdfReader(BytesIO(contenu)).pages
        return len(pages), '\n'.join(p.extract_text() or '' for p in pages)

    def test_vue_renvoie_un_pdf_avec_le_recu(self):
        response = self.client.get(self.url, {'ids': self.paiement.pk})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/pdf')
        pages, texte = self._texte(response.content)
        self.assertEqual(pages, 1)
        self.assertIn('REÇU DE PAIEMENT', texte)
        self.assertIn(self.paiement.numero_recu, texte)
        self.assertIn('100 000 GNF', texte)

    def test_quatre_par_page_puis_nouvelle_page(self):
        for nombre, pages_attendues in ((1, 1), (4, 1), (5, 2), (8, 2), (9, 3)):
            with self.subTest(nombre=nombre):
                pdf = generer_recus_4_par_feuille_pdf([self.paiement] * nombre)
                pages, texte = self._texte(pdf)
                self.assertEqual(pages, pages_attendues)
                self.assertEqual(texte.count('REÇU DE PAIEMENT'), nombre)

    def test_sans_selection_redirige(self):
        response = self.client.get(self.url)
        self.assertRedirects(
            response, reverse('paiements:liste_paiements'),
            fetch_redirect_response=False,
        )

    def test_paiement_non_valide_ignore(self):
        self.paiement.statut = 'EN_ATTENTE'
        self.paiement.save(update_fields=['statut'])
        response = self.client.get(self.url, {'ids': self.paiement.pk})
        self.assertEqual(response.status_code, 302)

    # ── Page de préparation de la feuille ────────────────────────────
    def test_page_preparation_prechargee_avec_le_paiement(self):
        response = self.client.get(reverse('paiements:recus_4_par_feuille'), {'ids': self.paiement.pk})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.paiement.numero_recu)
        self.assertEqual(response.context['preselection'][0]['id'], self.paiement.pk)

    def test_page_preparation_vide_sans_ids(self):
        response = self.client.get(reverse('paiements:recus_4_par_feuille'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['preselection'], [])

    def test_recherche_par_nom_numero_et_matricule(self):
        url = reverse('paiements:recus_4_recherche')
        for terme in ('camara', self.paiement.numero_recu, self.paiement.eleve.matricule):
            with self.subTest(terme=terme):
                data = self.client.get(url, {'q': terme}).json()
                self.assertEqual([r['id'] for r in data['resultats']], [self.paiement.pk])
        self.assertEqual(self.client.get(url, {'q': 'c'}).json()['resultats'], [])
        self.assertEqual(self.client.get(url, {'q': 'introuvable-xyz'}).json()['resultats'], [])

    def test_recherche_derniers_et_jour(self):
        url = reverse('paiements:recus_4_recherche')
        derniers = self.client.get(url, {'mode': 'derniers'}).json()['resultats']
        self.assertEqual([r['id'] for r in derniers], [self.paiement.pk])
        self.paiement.date_paiement = timezone.localdate()
        self.paiement.save(update_fields=['date_paiement'])
        jour = self.client.get(url, {'mode': 'jour'}).json()['resultats']
        self.assertEqual([r['id'] for r in jour], [self.paiement.pk])

    def test_recherche_ignore_paiement_non_valide(self):
        self.paiement.statut = 'EN_ATTENTE'
        self.paiement.save(update_fields=['statut'])
        data = self.client.get(reverse('paiements:recus_4_recherche'), {'q': 'camara'}).json()
        self.assertEqual(data['resultats'], [])
