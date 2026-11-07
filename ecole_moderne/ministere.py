"""Ministère de tutelle imprimé sur les bulletins, livrets et autres documents.

Intitulé en vigueur depuis le décret D/2026/006/PRG/SGG du 26 janvier 2026,
qui a réuni l'ancien MEPU-A, l'enseignement technique et la formation
professionnelle. En cas de nouveau changement, ne modifier que ce fichier.
"""
from reportlab.lib.utils import simpleSplit
from reportlab.pdfbase.pdfmetrics import stringWidth

MINISTERE_NOM = (
    "Ministère de l'Éducation Nationale, de l'Alphabétisation, "
    "de l'Enseignement Technique et de la Formation Professionnelle"
)
MINISTERE_SIGLE = "MENA-ETFP"
MINISTERE_NOM_SIGLE = f"{MINISTERE_NOM} ({MINISTERE_SIGLE})"

# Coupure naturelle quand le nom ne tient pas sur une ligne.
MINISTERE_LIGNES = (
    "Ministère de l'Éducation Nationale, de l'Alphabétisation,",
    "de l'Enseignement Technique et de la Formation Professionnelle",
)


def lignes_ministere(police, taille, largeur_max, *, sigle=False, max_lignes=2,
                     taille_min=5, texte=None):
    """Répartit le nom sur au plus `max_lignes` lignes de `largeur_max` points,
    en réduisant la police si nécessaire. Renvoie (lignes, taille)."""
    complet = texte or (MINISTERE_NOM_SIGLE if sigle else MINISTERE_NOM)
    while True:
        if stringWidth(complet, police, taille) <= largeur_max:
            return [complet], taille
        if max_lignes >= 2 and texte is None:
            lignes = list(MINISTERE_LIGNES)
            if sigle:
                lignes[-1] += f" ({MINISTERE_SIGLE})"
            if all(stringWidth(l, police, taille) <= largeur_max for l in lignes):
                return lignes, taille
        elif max_lignes >= 2:
            lignes = simpleSplit(complet, police, taille, largeur_max)
            if len(lignes) <= max_lignes:
                return lignes, taille
        if taille <= taille_min:
            # Dernier recours : couper au mot, sans dépasser le nombre de lignes.
            return simpleSplit(complet, police, taille, largeur_max)[:max(1, max_lignes)], taille
        taille = max(taille_min, taille - 0.5)


def dessiner_ministere(c, cx, y, police, taille, largeur_max, *, interligne=None,
                       sigle=False, max_lignes=2, texte=None):
    """Dessine le nom centré sur `cx` à partir de `y` et renvoie l'ordonnée de
    la dernière ligne dessinée (égale à `y` si le nom tient sur une ligne)."""
    lignes, taille_finale = lignes_ministere(
        police, taille, largeur_max, sigle=sigle, max_lignes=max_lignes, texte=texte,
    )
    pas = interligne or taille_finale * 1.2
    c.setFont(police, taille_finale)
    for i, ligne in enumerate(lignes):
        if i:
            y -= pas
        c.drawCentredString(cx, y, ligne)
    c.setFont(police, taille)
    return y
