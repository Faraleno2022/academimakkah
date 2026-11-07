"""Nom du ministère de tutelle sur les bulletins et documents officiels."""
from pathlib import Path

from django.conf import settings
from django.template.loader import get_template
from django.test import SimpleTestCase
from reportlab.pdfbase.pdfmetrics import stringWidth

from ecole_moderne.ministere import (
    MINISTERE_NOM, MINISTERE_SIGLE, dessiner_ministere, lignes_ministere,
)


class CanevasFactice:
    def __init__(self):
        self.lignes = []
        self.police = None

    def setFont(self, police, taille):
        self.police = (police, taille)

    def drawCentredString(self, x, y, texte):
        self.lignes.append((y, texte, self.police))


class NomMinistereTest(SimpleTestCase):
    def test_intitule_en_vigueur(self):
        self.assertEqual(
            MINISTERE_NOM,
            "Ministère de l'Éducation Nationale, de l'Alphabétisation, "
            "de l'Enseignement Technique et de la Formation Professionnelle",
        )
        self.assertEqual(MINISTERE_SIGLE, 'MENA-ETFP')

    def test_nom_trop_long_coupe_sur_deux_lignes_sans_deborder(self):
        largeur = 515  # A4 portrait moins les marges des tableaux de notes
        c = CanevasFactice()
        y = dessiner_ministere(c, 297, 700, 'Helvetica', 10, largeur, interligne=12)
        self.assertEqual(len(c.lignes), 2)
        self.assertEqual(y, 688)
        for _, texte, (police, taille) in c.lignes:
            self.assertLessEqual(stringWidth(texte, police, taille), largeur)
        self.assertEqual(' '.join(t for _, t, _ in c.lignes), MINISTERE_NOM)

    def test_bulletin_pdf_nom_ni_sous_le_logo_ni_sous_la_photo(self):
        # notes/bulletin_intelligent.py : logo de 1,2 à 3,7 cm, photo à 3,4 cm du bord droit.
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.units import cm
        largeur_page = A4[0]
        c = CanevasFactice()
        dessiner_ministere(c, largeur_page / 2, 780, 'Helvetica', 6, largeur_page - 7.4 * cm,
                           sigle=True, max_lignes=1)
        self.assertEqual(len(c.lignes), 1)
        _, texte, (police, taille) = c.lignes[0]
        self.assertTrue(texte.endswith('(MENA-ETFP)'))
        demi = stringWidth(texte, police, taille) / 2
        self.assertGreater(largeur_page / 2 - demi, 3.7 * cm)
        self.assertLess(largeur_page / 2 + demi, largeur_page - 3.4 * cm)

    def test_bulletin_html_reserve_la_place_du_logo_et_de_la_photo(self):
        # Logo (60 px) et photo + drapeau (110 px) sont en position absolue dans l'en-tête.
        source = Path(get_template('notes/bulletin_dynamique.html').origin.name).read_text(encoding='utf-8')
        regle = source[source.index('.header-section .ministere-education,'):]
        regle = regle[:regle.index('}')]
        self.assertIn('padding-left: 115px', regle)
        self.assertIn('padding-right: 115px', regle)

    def test_gabarits_html_affichent_le_nouveau_nom(self):
        for nom in ('notes/certificat_maternelle.html', 'notes/certificat_appreciation.html'):
            self.assertIn('{% ministere_nom %}', Path(get_template(nom).origin.name).read_text(encoding='utf-8'))
        rendu = get_template('notes/bulletin_dynamique_single.html').render({})
        self.assertIn('Formation Professionnelle (MENA-ETFP)', rendu)

    def test_plus_aucune_trace_de_l_ancien_intitule(self):
        racine = Path(settings.BASE_DIR)
        anciens = ('Pré-Universitaire', 'Pre-Universitaire', 'MEPU-A')
        dossiers = ('notes', 'eleves', 'examen_accueil', 'paiements', 'rapports', 'templates')
        trouves = []
        for dossier in dossiers:
            for fichier in (racine / dossier).rglob('*'):
                if fichier.suffix not in ('.py', '.html') or 'migrations' in fichier.parts:
                    continue
                if fichier.name == 'test_ministere.py':
                    continue
                texte = fichier.read_text(encoding='utf-8', errors='ignore')
                trouves += [f"{fichier.relative_to(racine)}: {a}" for a in anciens if a in texte]
        self.assertEqual(trouves, [])
