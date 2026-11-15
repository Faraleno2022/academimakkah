"""Reçus compacts : 4 reçus par feuille A4 (grille 2 × 2) pour économiser le papier.

Chaque reçu occupe un quart de feuille (A6 portrait environ) et reprend les
informations utiles du reçu complet : élève, paiement, affectation, remise,
solde global restant et zone de signatures. Des traits pointillés au centre de
la feuille indiquent où découper.
"""
from io import BytesIO
import logging
import os

from django.utils import timezone

logger = logging.getLogger(__name__)

PAGE_MARGIN = 14      # marge extérieure de la feuille (pt)
CELL_PADDING = 10     # marge intérieure de chaque reçu (pt)
RECUS_PAR_PAGE = 4


def _fmt(valeur):
    """Montant entier avec espaces comme séparateur de milliers."""
    try:
        return f"{int(valeur or 0):,}".replace(',', ' ')
    except (TypeError, ValueError):
        return str(valeur or 0)


def _tronquer(texte, police, taille, largeur_max):
    """Coupe le texte (avec …) pour qu'il tienne dans la largeur donnée."""
    from reportlab.pdfbase.pdfmetrics import stringWidth

    texte = str(texte or '')
    if stringWidth(texte, police, taille) <= largeur_max:
        return texte
    while texte and stringWidth(texte + '…', police, taille) > largeur_max:
        texte = texte[:-1]
    return texte.rstrip() + '…'


def _chemin_logo(ecole, cache):
    """Chemin du logo de l'école (sinon logo statique global), mis en cache."""
    cle = getattr(ecole, 'pk', None)
    if cle in cache:
        return cache[cle]
    chemin = None
    try:
        chemin_ecole = getattr(getattr(ecole, 'logo', None), 'path', None)
        if chemin_ecole and os.path.exists(chemin_ecole):
            chemin = chemin_ecole
    except Exception:
        chemin = None
    if not chemin:
        try:
            from django.contrib.staticfiles import finders
            chemin = finders.find('logos/logo.png')
        except Exception:
            chemin = None
    cache[cle] = chemin
    return chemin


def _donnees_recu(paiement):
    """Rassemble les chiffres d'un reçu (mêmes règles que le reçu complet)."""
    from decimal import Decimal
    from django.db.models import Sum
    from .allocation import get_payment_allocation
    from .recalcul_remises import montant_affiche_sur_recu
    from .views import _echeancier_for_payment, _sum_validated_payments_and_remises

    donnees = {
        'montant_paye': montant_affiche_sur_recu(paiement),
        'remises': [],
        'remises_total': 0,
        'affectation': [],
        'total_du': 0,
        'solde_global': None,
    }

    remises = list(paiement.remises.select_related('remise').all())
    donnees['remises'] = [
        (getattr(ligne.remise, 'nom', 'Remise'), int(ligne.montant_remise or 0))
        for ligne in remises
    ]
    donnees['remises_total'] = sum(montant for _, montant in donnees['remises'])

    try:
        echeancier = _echeancier_for_payment(paiement)
    except Exception:
        echeancier = None
    if not echeancier:
        return donnees

    label_insc = (
        'Réinscription'
        if getattr(echeancier, 'nature_frais', '') == 'REINSCRIPTION'
        else 'Inscription'
    )
    try:
        allocation = get_payment_allocation(paiement)
    except Exception:
        allocation = None
    if allocation:
        for libelle, cle in (
            (label_insc, 'inscription'),
            ('1ère tranche', 'tranche_1'),
            ('2ème tranche', 'tranche_2'),
            ('3ème tranche', 'tranche_3'),
        ):
            montant = int(allocation.get(cle, 0) or 0)
            if montant:
                donnees['affectation'].append((libelle, montant))

    try:
        total_du = int(
            (echeancier.frais_inscription_du or 0)
            + (echeancier.tranche_1_due or 0)
            + (echeancier.tranche_2_due or 0)
            + (echeancier.tranche_3_due or 0)
        )
    except Exception:
        total_du = 0
    donnees['total_du'] = total_du
    if total_du > 0:
        try:
            somme_montants, somme_remises = _sum_validated_payments_and_remises(
                paiement.eleve, paiement.annee_scolaire,
            )
        except Exception:
            somme_montants = somme_remises = 0
        couverture = max(0, int(somme_montants) + int(somme_remises))
        donnees['solde_global'] = max(0, total_du - couverture)
    return donnees


# Palette sobre : bleu nuit + gris clair, lisible aussi en impression noir et blanc.
BLEU = (0.10, 0.22, 0.42)
BLEU_CLAIR = (0.88, 0.92, 0.97)
GRIS_CLAIR = (0.95, 0.96, 0.97)
BORDURE = (0.62, 0.66, 0.72)
ROUGE_CLAIR = (0.99, 0.91, 0.90)
VERT_CLAIR = (0.90, 0.97, 0.91)


def _dessiner_recu(c, paiement, x, y, largeur, hauteur, cache_logos):
    """Dessine un reçu sous forme de tableaux dans le rectangle (x, y) = coin bas-gauche."""
    donnees = _donnees_recu(paiement)
    eleve = paiement.eleve
    classe = getattr(eleve, 'classe', None)
    ecole = getattr(classe, 'ecole', None)

    gauche = x + CELL_PADDING
    droite = x + largeur - CELL_PADDING
    utile = droite - gauche
    haut = y + hauteur - CELL_PADDING
    bas = y + CELL_PADDING
    haut_ligne = 12.5

    # ── Primitives de tableau ────────────────────────────────────────
    def cellule(px, py, w, h, texte='', *, fond=None, gras=False, taille=8,
                align='left', couleur=(0, 0, 0)):
        """Une cellule : fond, bordure fine, texte tronqué et centré verticalement."""
        c.saveState()
        if fond is not None:
            c.setFillColorRGB(*fond)
        c.setStrokeColorRGB(*BORDURE)
        c.setLineWidth(0.5)
        c.rect(px, py - h, w, h, stroke=1, fill=1 if fond is not None else 0)
        if texte != '':
            police = 'Helvetica-Bold' if gras else 'Helvetica'
            c.setFillColorRGB(*couleur)
            c.setFont(police, taille)
            texte = _tronquer(texte, police, taille, w - 8)
            baseline = py - h / 2 - taille * 0.35
            if align == 'right':
                c.drawRightString(px + w - 4, baseline, texte)
            elif align == 'center':
                c.drawCentredString(px + w / 2, baseline, texte)
            else:
                c.drawString(px + 4, baseline, texte)
        c.restoreState()

    def titre_section(cur, libelle):
        """Bandeau bleu pleine largeur ; retourne l'ordonnée sous le bandeau."""
        cellule(gauche, cur, utile, 11.5, libelle.upper(), fond=BLEU, gras=True,
                taille=7, couleur=(1, 1, 1))
        return cur - 11.5

    def ligne_cle_valeur(cur, libelle, valeur, *, gras=False, fond_valeur=None,
                         taille=8, fond_libelle=GRIS_CLAIR, align='left', h=haut_ligne):
        """Ligne à 2 colonnes : libellé grisé | valeur."""
        w1 = utile * 0.42
        cellule(gauche, cur, w1, h, libelle, fond=fond_libelle, taille=7.5,
                couleur=(0.25, 0.25, 0.3))
        cellule(gauche + w1, cur, utile - w1, h, valeur, fond=fond_valeur,
                gras=gras, taille=taille, align=align)
        return cur - h

    def ligne_montant(cur, libelle, montant, *, fond=None, gras=False, taille=8):
        """Ligne à 2 colonnes : désignation | montant aligné à droite."""
        w1 = utile * 0.58
        cellule(gauche, cur, w1, haut_ligne, libelle, fond=fond, gras=gras, taille=taille)
        cellule(gauche + w1, cur, utile - w1, haut_ligne, f"{_fmt(montant)} GNF",
                fond=fond, gras=gras, taille=taille, align='right')
        return cur - haut_ligne

    # ── Cadre extérieur du reçu ──────────────────────────────────────
    c.saveState()
    c.setStrokeColorRGB(*BLEU)
    c.setLineWidth(1)
    c.roundRect(x + 3, y + 3, largeur - 6, hauteur - 6, 5)
    c.restoreState()

    # ── Bandeau d'en-tête : logo + école ─────────────────────────────
    bandeau_h = 38
    c.saveState()
    c.setFillColorRGB(*BLEU)
    c.rect(gauche, haut - bandeau_h, utile, bandeau_h, stroke=0, fill=1)
    c.restoreState()

    debut_texte = gauche + 6
    logo = _chemin_logo(ecole, cache_logos) if ecole is not None else None
    if logo:
        try:
            from reportlab.lib.utils import ImageReader
            taille_logo = bandeau_h - 8
            c.saveState()
            c.setFillColorRGB(1, 1, 1)
            c.roundRect(gauche + 4, haut - bandeau_h + 4, taille_logo, taille_logo, 3,
                        stroke=0, fill=1)
            c.restoreState()
            c.drawImage(
                ImageReader(logo), gauche + 5, haut - bandeau_h + 5,
                width=taille_logo - 2, height=taille_logo - 2,
                preserveAspectRatio=True, mask='auto',
            )
            debut_texte = gauche + taille_logo + 12
        except Exception:
            debut_texte = gauche + 6
    c.setFillColorRGB(1, 1, 1)
    nom_ecole = getattr(ecole, 'nom', '') or ''
    c.setFont('Helvetica-Bold', 9.5)
    c.drawString(debut_texte, haut - 14,
                 _tronquer(nom_ecole, 'Helvetica-Bold', 9.5, droite - debut_texte - 4))
    contact = ' · '.join(
        p for p in (
            getattr(ecole, 'telephone', '') or '',
            getattr(ecole, 'email', '') or '',
        ) if p
    )
    if contact:
        c.setFont('Helvetica', 6.8)
        c.drawString(debut_texte, haut - 24,
                     _tronquer(contact, 'Helvetica', 6.8, droite - debut_texte - 4))
    c.setFont('Helvetica-Bold', 8)
    c.drawString(debut_texte, haut - 34, 'REÇU DE PAIEMENT')
    c.setFillColorRGB(0, 0, 0)
    cur = haut - bandeau_h

    # ── N° de reçu et date ───────────────────────────────────────────
    w_num = utile * 0.6
    cellule(gauche, cur, w_num, 16, f"N° {paiement.numero_recu or '—'}",
            fond=BLEU_CLAIR, gras=True, taille=9)
    cellule(gauche + w_num, cur, utile - w_num, 16,
            paiement.date_paiement.strftime('%d/%m/%Y'),
            fond=BLEU_CLAIR, gras=True, taille=8.5, align='right')
    cur -= 16 + 4

    # ── Élève ────────────────────────────────────────────────────────
    cur = titre_section(cur, 'Élève')
    cur = ligne_cle_valeur(cur, 'Nom', getattr(eleve, 'nom_complet', '') or str(eleve),
                           gras=True)
    if getattr(eleve, 'matricule', None):
        cur = ligne_cle_valeur(cur, 'Matricule', eleve.matricule)
    if classe is not None:
        cur = ligne_cle_valeur(cur, 'Classe', str(classe))
    cur -= 4

    # ── Paiement ─────────────────────────────────────────────────────
    cur = titre_section(cur, 'Paiement')
    cur = ligne_cle_valeur(cur, 'Type', getattr(paiement.type_paiement, 'nom', '') or '—')
    cur = ligne_cle_valeur(cur, 'Mode', getattr(paiement.mode_paiement, 'nom', '') or '—')
    if getattr(paiement, 'reference_externe', None):
        cur = ligne_cle_valeur(cur, 'Référence', paiement.reference_externe)
    if donnees['total_du'] > 0:
        cur = ligne_cle_valeur(cur, 'Montant annuel',
                               f"{_fmt(donnees['total_du'])} GNF", align='right')
    cur = ligne_cle_valeur(
        cur, 'MONTANT PAYÉ', f"{_fmt(donnees['montant_paye'])} GNF",
        gras=True, taille=9.5, fond_valeur=BLEU_CLAIR, fond_libelle=BLEU_CLAIR,
        align='right', h=15,
    )
    cur -= 4

    # ── Affectation ──────────────────────────────────────────────────
    if donnees['affectation']:
        cur = titre_section(cur, 'Affectation du paiement')
        for libelle, montant in donnees['affectation']:
            cur = ligne_montant(cur, libelle, montant)
        cur -= 4

    # ── Remise ───────────────────────────────────────────────────────
    if donnees['remises_total'] > 0:
        cur = titre_section(cur, 'Remise')
        for nom, montant in donnees['remises'][:2]:
            cur = ligne_montant(cur, nom, montant, taille=7.5)
        cur = ligne_montant(cur, 'Total remise', donnees['remises_total'],
                            fond=GRIS_CLAIR, gras=True)
        cur -= 4

    # ── Solde global ─────────────────────────────────────────────────
    if donnees['solde_global'] is not None:
        solde = donnees['solde_global']
        w1 = utile * 0.58
        fond = ROUGE_CLAIR if solde > 0 else VERT_CLAIR
        cellule(gauche, cur, w1, 15, 'SOLDE GLOBAL RESTANT', fond=fond, gras=True, taille=7.5)
        cellule(gauche + w1, cur, utile - w1, 15,
                f"{_fmt(solde)} GNF" if solde > 0 else 'Soldé',
                fond=fond, gras=True, taille=9, align='right')
        cur -= 15

    # ── Signatures : tableau à 2 cases, ancré en bas de la cellule ───
    sig_titre_h, sig_case_h = 11.5, 30
    sig_haut = bas + 10 + sig_titre_h + sig_case_h
    moitie = utile / 2
    for index, libelle in enumerate(('Caissier(e)', 'Responsable')):
        px = gauche + index * moitie
        cellule(px, sig_haut, moitie, sig_titre_h, libelle, fond=GRIS_CLAIR,
                gras=True, taille=7.5, align='center')
        cellule(px, sig_haut - sig_titre_h, moitie, sig_case_h)

    c.setFont('Helvetica', 6)
    c.setFillGray(0.45)
    c.drawCentredString(
        x + largeur / 2, bas - 1,
        f"Généré le {timezone.now().strftime('%d/%m/%Y %H:%M')}",
    )
    c.setFillGray(0.0)


def generer_recus_4_par_feuille_pdf(paiements):
    """Construit le PDF : 4 reçus par feuille A4. Retourne les octets du PDF."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    largeur_page, hauteur_page = A4
    largeur_cell = (largeur_page - 2 * PAGE_MARGIN) / 2
    hauteur_cell = (hauteur_page - 2 * PAGE_MARGIN) / 2

    tampon = BytesIO()
    c = canvas.Canvas(tampon, pagesize=A4)
    cache_logos = {}

    paiements = list(paiements)
    for debut in range(0, len(paiements), RECUS_PAR_PAGE):
        groupe = paiements[debut:debut + RECUS_PAR_PAGE]

        # Traits de découpe pointillés, au centre de la feuille.
        c.saveState()
        c.setStrokeGray(0.65)
        c.setLineWidth(0.5)
        c.setDash(3, 3)
        milieu_x = PAGE_MARGIN + largeur_cell
        milieu_y = PAGE_MARGIN + hauteur_cell
        c.line(milieu_x, PAGE_MARGIN, milieu_x, hauteur_page - PAGE_MARGIN)
        c.line(PAGE_MARGIN, milieu_y, largeur_page - PAGE_MARGIN, milieu_y)
        c.restoreState()

        for index, paiement in enumerate(groupe):
            colonne = index % 2
            ligne = index // 2  # 0 = rangée du haut
            x = PAGE_MARGIN + colonne * largeur_cell
            y = PAGE_MARGIN + (1 - ligne) * hauteur_cell
            try:
                _dessiner_recu(c, paiement, x, y, largeur_cell, hauteur_cell, cache_logos)
            except Exception:
                logger.exception(
                    "Reçu compact : échec du dessin du paiement %s",
                    getattr(paiement, 'pk', '?'),
                )
        c.showPage()

    if not paiements:
        c.showPage()
    c.save()
    pdf = tampon.getvalue()
    tampon.close()
    return pdf
