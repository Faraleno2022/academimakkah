"""Reçus compacts : 4 reçus par feuille A4."""
from io import BytesIO

from django.test import TestCase, override_settings
from django.urls import reverse

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
