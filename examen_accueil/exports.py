"""Exports du test d'accueil : résultats (PDF, Excel) et bulletins (PDF)."""
import io
import os
from xml.sax.saxutils import escape

from django.utils import timezone
from django.utils.text import slugify

from ecole_moderne.ministere import MINISTERE_NOM
from ecole_moderne.pdf_utils import draw_logo_watermark
from notes.charte_graphique import get_charte_reportlab

from .calculs import calculer_resultats, mention, statistiques
from .models import MatiereAccueil, ParametreAccueil

MENTION_INDEPENDANCE = (
    "Résultats du test d'accueil uniquement : ils n'entrent pas dans les "
    "moyennes mensuelles ni dans les compositions."
)


def nom_fichier(classe, prefixe, extension):
    return f"{prefixe}_{slugify(classe.nom) or 'classe'}_{classe.annee_scolaire}.{extension}"


def nombre(valeur):
    """Décimal affiché à la française, sans zéros inutiles (12,5 ; 14 ; 9,25)."""
    if valeur is None:
        return '-'
    texte = f"{valeur:.2f}".rstrip('0').rstrip('.')
    return texte.replace('.', ',')


def decision(ligne):
    feminin = ligne['eleve'].sexe == 'F'
    if ligne['admis']:
        return 'ADMISE' if feminin else 'ADMIS'
    return 'NON ADMISE' if feminin else 'NON ADMIS'


# --------------------------------------------------------------------------- PDF

# Colonnes fixes du tableau des résultats (cm) : rang, matricule, sexe, puis
# total, moyenne, mention, décision. Le nom et les matières se partagent le reste.
COLONNES_AVANT = (1.3, 1.9, None, 0.9)
COLONNES_APRES = (1.4, 1.5, 2.4, 1.8)
LARGEUR_MATIERE_HORIZONTALE = 1.4
HAUTEUR_ENTETE_VERTICAL = 3.4


def _disposition(largeur, nb_matieres):
    """(largeur du nom, largeur d'une matière, en-têtes verticaux ?) en points."""
    from reportlab.lib.units import cm

    reste = largeur - sum(c for c in COLONNES_AVANT + COLONNES_APRES if c) * cm
    if not nb_matieres:
        return reste, 0, False
    largeur_matiere = min(2.2 * cm, (reste - 4.5 * cm) / nb_matieres)
    if largeur_matiere >= LARGEUR_MATIERE_HORIZONTALE * cm:
        return reste - largeur_matiere * nb_matieres, largeur_matiere, False
    # Beaucoup de matières (maternelle, 10ème...) : intitulés verticaux, nom plus étroit.
    largeur_matiere = (reste - 3.2 * cm) / nb_matieres
    return 3.2 * cm, largeur_matiere, True


def _texte_vertical(texte, hauteur):
    """Intitulé de colonne écrit de bas en haut, réduit pour tenir dans ``hauteur``."""
    from reportlab.lib import colors
    from reportlab.pdfbase.pdfmetrics import stringWidth
    from reportlab.platypus import Flowable

    class TexteVertical(Flowable):
        def wrap(self, largeur_disponible, hauteur_disponible):
            self.largeur_colonne = largeur_disponible
            return largeur_disponible, hauteur

        def draw(self):
            taille = 7.5
            while taille > 5 and stringWidth(texte, 'Helvetica-Bold', taille) > hauteur - 4:
                taille -= 0.5
            lignes = [texte]
            if stringWidth(texte, 'Helvetica-Bold', taille) > hauteur - 4 and ' ' in texte:
                coupe = texte.rfind(' ', 0, len(texte) // 2 + 1)
                coupe = coupe if coupe > 0 else texte.find(' ')
                lignes = [texte[:coupe], texte[coupe + 1:]]
            canevas = self.canv
            canevas.saveState()
            canevas.setFillColor(colors.white)
            canevas.setFont('Helvetica-Bold', taille)
            canevas.rotate(90)
            centre = self.largeur_colonne / 2
            decalage = (len(lignes) - 1) * (taille + 1) / 2
            for index, ligne in enumerate(lignes):
                canevas.drawString(2, -(centre - decalage + index * (taille + 1)) - taille * 0.35, ligne)
            canevas.restoreState()

    return TexteVertical()


def _styles():
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet

    base = getSampleStyleSheet()
    return {
        'centre': ParagraphStyle('centre', parent=base['Normal'], alignment=TA_CENTER, fontSize=9, leading=11),
        'ecole': ParagraphStyle('ecole', parent=base['Normal'], alignment=TA_CENTER, fontName='Helvetica-Bold',
                                fontSize=14, leading=17),
        'titre': ParagraphStyle('titre', parent=base['Normal'], alignment=TA_CENTER, fontName='Helvetica-Bold',
                                fontSize=13, leading=16, spaceBefore=6, spaceAfter=4),
        'normal': ParagraphStyle('normal', parent=base['Normal'], fontSize=9, leading=11),
        'cellule': ParagraphStyle('cellule', parent=base['Normal'], fontSize=8, leading=10),
        'entete': ParagraphStyle('entete', parent=base['Normal'], alignment=TA_CENTER, fontSize=7.5, leading=9.5,
                                 textColor='#FFFFFF'),
        'note': ParagraphStyle('note', parent=base['Normal'], alignment=TA_CENTER, fontName='Helvetica-Oblique',
                               fontSize=7.5, leading=9, textColor='#555555'),
    }


def _entete(ecole, charte, styles, largeur):
    """En-tête officiel : République, ministère, école (logo à gauche si présent)."""
    from reportlab.platypus import Image, Paragraph, Table, TableStyle

    lignes = [
        Paragraph("<b>RÉPUBLIQUE DE GUINÉE</b>", styles['centre']),
        Paragraph("<font color='red'>Travail</font> - <font color='#C9A100'>Justice</font> - "
                  "<font color='green'>Solidarité</font>", styles['centre']),
        Paragraph(escape(MINISTERE_NOM), styles['centre']),
    ]
    ire, dpe = getattr(ecole, 'ire', '') or '', getattr(ecole, 'dpe', '') or ''
    if ire or dpe:
        lignes.append(Paragraph(escape(" - ".join(filter(None, [f"IRE : {ire}" if ire else '',
                                                               f"DPE : {dpe}" if dpe else '']))),
                                styles['centre']))
    couleur_ecole = '#' + charte['couleur_primaire'].hexval()[2:]
    lignes.append(Paragraph(f"<font color='{couleur_ecole}'>{escape((ecole.nom or 'ÉCOLE').upper())}</font>", styles['ecole']))
    contacts = " | ".join(filter(None, [getattr(ecole, 'adresse', ''), getattr(ecole, 'telephone', '')]))
    if contacts:
        lignes.append(Paragraph(escape(contacts), styles['centre']))

    logo = None
    try:
        chemin = getattr(getattr(ecole, 'logo', None), 'path', None)
        if chemin and os.path.exists(chemin):
            logo = Image(chemin, width=50, height=50, kind='proportional')
    except Exception:
        logo = None

    tableau = Table([[logo or '', lignes, '']], colWidths=[60, largeur - 120, 60])
    tableau.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('LINEBELOW', (0, 0), (-1, 0), 1.2, charte['couleur_secondaire']),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
    ]))
    return tableau


def _filigrane(ecole):
    def dessiner(canvas, doc):
        try:
            draw_logo_watermark(canvas, *doc.pagesize, opacity=0.04, rotate=30, scale=1.5, ecole=ecole)
        except Exception:
            pass
        canvas.saveState()
        canvas.setFont('Helvetica', 7)
        canvas.setFillGray(0.4)
        canvas.drawRightString(doc.pagesize[0] - doc.rightMargin, 18,
                               f"MySchoolGN - imprimé le {timezone.localdate():%d/%m/%Y} - page {canvas.getPageNumber()}")
        canvas.restoreState()
    return dessiner


def _infos_classe(classe, parametre):
    elements = [f"Classe : <b>{escape(classe.nom)}</b>",
                f"Année scolaire : <b>{escape(classe.annee_scolaire)}</b>"]
    if parametre.date_test:
        elements.append(f"Date du test : <b>{parametre.date_test:%d/%m/%Y}</b>")
    bareme = f"Notes sur <b>{parametre.note_sur}</b>"
    if not parametre.avec_coefficients:
        bareme += " sans coefficient"
    elements.append(f"{parametre.libelle_niveau} - {bareme} - admission à <b>{nombre(parametre.moyenne_admission)}</b>")
    return " &nbsp;|&nbsp; ".join(elements)


def resultats_pdf(classe):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.units import cm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    ecole = classe.ecole
    charte = get_charte_reportlab(ecole)
    styles = _styles()
    parametre = ParametreAccueil.pour(classe)
    resultats = calculer_resultats(classe, parametre)
    stats = statistiques(resultats)
    matieres = list(MatiereAccueil.objects.filter(classe=classe))

    # Portrait tant que les intitulés tiennent à l'horizontale, sinon paysage.
    taille = A4 if not _disposition(A4[0] - 2.4 * cm, len(matieres))[2] else landscape(A4)
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=taille, leftMargin=1.2 * cm, rightMargin=1.2 * cm,
                            topMargin=1 * cm, bottomMargin=1.2 * cm,
                            title=f"Résultats du test d'accueil - {classe.nom}")
    largeur = taille[0] - doc.leftMargin - doc.rightMargin

    histoire = [_entete(ecole, charte, styles, largeur), Spacer(1, 6),
                Paragraph("RÉSULTATS DU TEST D'ACCUEIL", styles['titre']),
                Paragraph(_infos_classe(classe, parametre), styles['centre']), Spacer(1, 8)]

    largeur_nom, largeur_matiere, vertical = _disposition(largeur, len(matieres))
    entetes = ['Rang', 'Matricule', 'Nom et prénom(s)', 'Sexe']
    for m in matieres:
        coefficient = f"coef. {nombre(m.coefficient)}" if parametre.avec_coefficients else ''
        if vertical:
            entetes.append(_texte_vertical(f"{m.nom} ({coefficient})" if coefficient else m.nom,
                                           HAUTEUR_ENTETE_VERTICAL * cm))
        else:
            entetes.append(Paragraph(f"<b>{escape(m.nom)}</b>" + (f"<br/>{coefficient}" if coefficient else ''),
                                     styles['entete']))
    entetes += ['Total', 'Moyenne', 'Mention', 'Décision']
    donnees = [entetes]
    for ligne in resultats:
        eleve = ligne['eleve']
        donnees.append(
            [ligne['rang_texte'], eleve.matricule, Paragraph(escape(f"{eleve.nom} {eleve.prenom}"), styles['cellule']),
             eleve.sexe or '-']
            + [nombre(d['note']) for d in ligne['details']]
            + [nombre(ligne['total_points']), nombre(ligne['moyenne']),
               Paragraph(escape(ligne['appreciation'] or '-'), styles['cellule']), decision(ligne)]
        )
    if not resultats:
        donnees.append(['', '', 'Aucun élève évalué', ''] + [''] * len(matieres) + ['', '', '', ''])

    largeurs = ([c * cm if c else largeur_nom for c in COLONNES_AVANT] + [largeur_matiere] * len(matieres)
                + [c * cm for c in COLONNES_APRES])

    tableau = Table(donnees, colWidths=largeurs, repeatRows=1)
    style = [
        ('BACKGROUND', (0, 0), (-1, 0), charte['couleur_primaire']),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 7.5 if vertical else 8),
        ('LEFTPADDING', (4, 1), (3 + len(matieres), -1), 1), ('RIGHTPADDING', (4, 1), (3 + len(matieres), -1), 1),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('ALIGN', (2, 1), (2, -1), 'LEFT'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('GRID', (0, 0), (-1, -1), 0.4, charte['couleur_bordure']),
        ('LEFTPADDING', (0, 0), (-1, 0), 3), ('RIGHTPADDING', (0, 0), (-1, 0), 3),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, charte['couleur_fond_tableau']]),
    ]
    for index, ligne in enumerate(resultats, start=1):
        couleur = charte['couleur_mention_tb'] if ligne['admis'] else charte['couleur_mention_insuffisant']
        style += [('TEXTCOLOR', (-1, index), (-1, index), couleur), ('FONTNAME', (-1, index), (-1, index), 'Helvetica-Bold')]
    if vertical:
        style += [('VALIGN', (4, 0), (3 + len(matieres), 0), 'BOTTOM'),
                  ('TOPPADDING', (4, 0), (3 + len(matieres), 0), 0), ('BOTTOMPADDING', (4, 0), (3 + len(matieres), 0), 2)]
    tableau.setStyle(TableStyle(style))
    histoire += [tableau, Spacer(1, 10)]

    synthese = (
        f"Effectif : <b>{stats['effectif']}</b> &nbsp;|&nbsp; Admis : <b>{stats['admis']}</b> &nbsp;|&nbsp; "
        f"Non admis : <b>{stats['non_admis']}</b> &nbsp;|&nbsp; Taux d'admission : <b>{nombre(stats['taux_admission'])} %</b>"
        f" &nbsp;|&nbsp; Moyenne de la classe : <b>{nombre(stats['moyenne_classe'])}</b>"
        f" &nbsp;|&nbsp; Plus forte : <b>{nombre(stats['plus_forte'])}</b>"
        f" &nbsp;|&nbsp; Plus faible : <b>{nombre(stats['plus_faible'])}</b>"
    )
    histoire += [Paragraph(synthese, styles['normal']), Spacer(1, 18),
                 Table([['', "Le Directeur"]], colWidths=[largeur * 0.6, largeur * 0.4],
                       style=[('FONTNAME', (1, 0), (1, 0), 'Helvetica-Bold'), ('ALIGN', (1, 0), (1, 0), 'CENTER')]),
                 Spacer(1, 30), Paragraph(MENTION_INDEPENDANCE, styles['note'])]

    doc.build(histoire, onFirstPage=_filigrane(ecole), onLaterPages=_filigrane(ecole))
    return buffer.getvalue()


def fiche_notes_pdf(classe, eleves, matieres):
    """Fiche vierge à remettre aux enseignants : une page par matière, une ligne par élève."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    ecole = classe.ecole
    charte = get_charte_reportlab(ecole)
    styles = _styles()
    parametre = ParametreAccueil.pour(classe)

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=1.2 * cm, rightMargin=1.2 * cm,
                            topMargin=1 * cm, bottomMargin=1.2 * cm,
                            title=f"Fiche de notes du test d'accueil - {classe.nom}")
    largeur = A4[0] - doc.leftMargin - doc.rightMargin
    largeurs = [1 * cm, 2.6 * cm, None, 1.1 * cm, 2.4 * cm, 4.2 * cm]
    largeurs[2] = largeur - sum(l for l in largeurs if l)

    histoire = []
    for index, matiere in enumerate(matieres):
        if index:
            histoire.append(PageBreak())
        infos = [f"Classe : <b>{escape(classe.nom)}</b>", f"Année scolaire : <b>{escape(classe.annee_scolaire)}</b>",
                 f"Matière : <b>{escape(matiere.nom)}</b>"]
        if parametre.avec_coefficients:
            infos.append(f"Coefficient : <b>{nombre(matiere.coefficient)}</b>")
        infos.append(f"Notes sur <b>{parametre.note_sur}</b>")
        date_test = f"{parametre.date_test:%d/%m/%Y}" if parametre.date_test else "____ / ____ / ________"
        histoire += [
            _entete(ecole, charte, styles, largeur), Spacer(1, 6),
            Paragraph("FICHE DE NOTES DU TEST D'ACCUEIL", styles['titre']),
            Paragraph(" &nbsp;|&nbsp; ".join(infos), styles['centre']), Spacer(1, 6),
            Paragraph(f"Enseignant(e) : ______________________________________ &nbsp;&nbsp; "
                      f"Date du test : {date_test} &nbsp;&nbsp; Effectif : <b>{len(eleves)}</b>", styles['normal']),
            Spacer(1, 8),
        ]
        donnees = [['N°', 'Matricule', 'Nom et prénom(s)', 'Sexe', f"Note / {parametre.note_sur}", 'Observation']]
        donnees += [[numero, eleve.matricule, Paragraph(escape(f"{eleve.nom} {eleve.prenom}"), styles['cellule']),
                     eleve.sexe or '', '', ''] for numero, eleve in enumerate(eleves, start=1)]
        if not eleves:
            donnees.append(['', '', 'Aucun élève', '', '', ''])
        tableau = Table(donnees, colWidths=largeurs, repeatRows=1,
                        rowHeights=[0.8 * cm] + [0.75 * cm] * (len(donnees) - 1))
        tableau.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), charte['couleur_primaire']),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 8.5),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('ALIGN', (2, 1), (2, -1), 'LEFT'),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('GRID', (0, 0), (-1, -1), 0.5, charte['couleur_bordure']),
            ('LINEBEFORE', (4, 0), (4, -1), 1.2, charte['couleur_primaire']),
            ('LINEAFTER', (4, 0), (4, -1), 1.2, charte['couleur_primaire']),
        ]))
        histoire += [
            tableau, Spacer(1, 18),
            Table([["Signature de l'enseignant(e)", "Visa du Directeur"]], colWidths=[largeur / 2] * 2,
                  style=[('FONTNAME', (0, 0), (-1, -1), 'Helvetica-Bold'), ('ALIGN', (0, 0), (-1, -1), 'CENTER')]),
            Spacer(1, 30), Paragraph(MENTION_INDEPENDANCE, styles['note']),
        ]

    doc.build(histoire, onFirstPage=_filigrane(ecole), onLaterPages=_filigrane(ecole))
    return buffer.getvalue()


def bulletins_pdf(classe, eleves=None):
    """Un bulletin par page ; ``eleves`` limite l'impression (sinon tous les élèves évalués)."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    ecole = classe.ecole
    charte = get_charte_reportlab(ecole)
    styles = _styles()
    parametre = ParametreAccueil.pour(classe)
    resultats = calculer_resultats(classe, parametre)
    effectif = len(resultats)
    if eleves is not None:
        ids = {e.pk for e in eleves}
        resultats = [r for r in resultats if r['eleve'].pk in ids]
    else:
        # Bulletins par ordre alphabétique, plus simple à distribuer.
        resultats = sorted(resultats, key=lambda r: (r['eleve'].nom.upper(), r['eleve'].prenom.upper()))

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=1.5 * cm, rightMargin=1.5 * cm,
                            topMargin=1 * cm, bottomMargin=1.2 * cm,
                            title=f"Bulletins du test d'accueil - {classe.nom}")
    largeur = A4[0] - doc.leftMargin - doc.rightMargin
    histoire = []

    for numero, ligne in enumerate(resultats):
        eleve = ligne['eleve']
        if numero:
            histoire.append(PageBreak())
        histoire += [_entete(ecole, charte, styles, largeur), Spacer(1, 6),
                     Paragraph("BULLETIN DU TEST D'ACCUEIL", styles['titre']),
                     Paragraph(_infos_classe(classe, parametre), styles['centre']), Spacer(1, 10)]

        identite = [
            ['Nom et prénom(s)', f"{eleve.nom} {eleve.prenom}", 'Sexe', eleve.get_sexe_display() or '-'],
            ['Date de naissance', f"{eleve.date_naissance:%d/%m/%Y}" if eleve.date_naissance else '-',
             'Lieu de naissance', eleve.lieu_naissance or '-'],
            ['Matricule', eleve.matricule, 'Élèves évalués', str(effectif)],
        ]
        bloc = Table(identite, colWidths=[3.4 * cm, largeur / 2 - 3.4 * cm, 3.2 * cm, largeur / 2 - 3.2 * cm])
        bloc.setStyle(TableStyle([
            ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'), ('FONTNAME', (2, 0), (2, -1), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 9),
            ('BACKGROUND', (0, 0), (0, -1), charte['couleur_fond_tableau']),
            ('BACKGROUND', (2, 0), (2, -1), charte['couleur_fond_tableau']),
            ('GRID', (0, 0), (-1, -1), 0.4, charte['couleur_bordure']),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ]))
        histoire += [bloc, Spacer(1, 12)]

        if parametre.avec_coefficients:
            notes = [['Matière', 'Coefficient', f'Note / {parametre.note_sur}', 'Points']]
            for detail in ligne['details']:
                notes.append([detail['matiere'].nom, nombre(detail['coefficient']),
                              nombre(detail['note']) if detail['note'] is not None else 'Non noté',
                              nombre(detail['points'])])
            notes.append(['TOTAL', nombre(ligne['total_coefficients']), '', nombre(ligne['total_points'])])
            colonnes = [largeur * 0.46, largeur * 0.18, largeur * 0.18, largeur * 0.18]
        else:
            # Maternelle et primaire : pas de coefficient, une appréciation par matière.
            notes = [['Matière', f'Note / {parametre.note_sur}', 'Appréciation']]
            for detail in ligne['details']:
                texte = mention(detail['note'], parametre.niveau, parametre.note_sur)
                if detail['lettre']:
                    texte = f"{detail['lettre']} - {texte}"
                notes.append([detail['matiere'].nom,
                              nombre(detail['note']) if detail['note'] is not None else 'Non noté', texte or '-'])
            notes.append(['TOTAL', nombre(ligne['total_points']), ''])
            colonnes = [largeur * 0.46, largeur * 0.2, largeur * 0.34]
        tableau = Table(notes, colWidths=colonnes, repeatRows=1)
        tableau.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), charte['couleur_primaire']),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
            ('BACKGROUND', (0, -1), (-1, -1), charte['couleur_fond_tableau']),
            ('FONTSIZE', (0, 0), (-1, -1), 9.5),
            ('ALIGN', (1, 0), (-1, -1), 'CENTER'),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('GRID', (0, 0), (-1, -1), 0.4, charte['couleur_bordure']),
            ('TOPPADDING', (0, 0), (-1, -1), 5), ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ]))
        histoire += [tableau, Spacer(1, 12)]

        couleur = charte['couleur_mention_tb'] if ligne['admis'] else charte['couleur_mention_insuffisant']
        bilan = Table([
            ['Moyenne', f"{nombre(ligne['moyenne'])} / {parametre.note_sur}", 'Rang', f"{ligne['rang_texte']} / {effectif}"],
            ['Mention', ligne['appreciation'] or '-', 'Décision', decision(ligne)],
        ], colWidths=[largeur * 0.18, largeur * 0.32, largeur * 0.18, largeur * 0.32])
        bilan.setStyle(TableStyle([
            ('FONTNAME', (0, 0), (-1, -1), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 10.5),
            ('BACKGROUND', (0, 0), (0, -1), charte['couleur_fond_tableau']),
            ('BACKGROUND', (2, 0), (2, -1), charte['couleur_fond_tableau']),
            ('TEXTCOLOR', (3, 1), (3, 1), couleur),
            ('GRID', (0, 0), (-1, -1), 0.6, charte['couleur_bordure']),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'), ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('TOPPADDING', (0, 0), (-1, -1), 6), ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ]))
        histoire += [bilan, Spacer(1, 26),
                     Table([["Le Parent", "Le Directeur"]], colWidths=[largeur / 2, largeur / 2],
                           style=[('FONTNAME', (0, 0), (-1, -1), 'Helvetica-Bold'), ('ALIGN', (0, 0), (-1, -1), 'CENTER')]),
                     Spacer(1, 50), Paragraph(MENTION_INDEPENDANCE, styles['note'])]

    if not histoire:
        histoire = [_entete(ecole, charte, styles, largeur), Spacer(1, 20),
                    Paragraph("Aucun élève évalué au test d'accueil dans cette classe.", styles['titre'])]
    doc.build(histoire, onFirstPage=_filigrane(ecole), onLaterPages=_filigrane(ecole))
    return buffer.getvalue()


# ------------------------------------------------------------------------- Excel

def resultats_excel(classe):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    parametre = ParametreAccueil.pour(classe)
    resultats = calculer_resultats(classe, parametre)
    stats = statistiques(resultats)
    matieres = list(MatiereAccueil.objects.filter(classe=classe))

    classeur = Workbook()
    feuille = classeur.active
    feuille.title = "Résultats"
    nb_colonnes = 4 + len(matieres) + 4
    fin = get_column_letter(nb_colonnes)

    feuille.merge_cells(f"A1:{fin}1")
    feuille['A1'] = (classe.ecole.nom or '').upper()
    feuille['A1'].font = Font(bold=True, size=14)
    feuille.merge_cells(f"A2:{fin}2")
    feuille['A2'] = f"RÉSULTATS DU TEST D'ACCUEIL - {classe.nom} ({classe.annee_scolaire})"
    feuille['A2'].font = Font(bold=True, size=12)
    feuille.merge_cells(f"A3:{fin}3")
    details = [parametre.libelle_niveau,
               f"Notes sur {parametre.note_sur}" + ('' if parametre.avec_coefficients else ' sans coefficient'),
               f"Moyenne d'admission : {nombre(parametre.moyenne_admission)}"]
    if parametre.date_test:
        details.append(f"Date du test : {parametre.date_test:%d/%m/%Y}")
    feuille['A3'] = " | ".join(details)
    for cellule in ('A1', 'A2', 'A3'):
        feuille[cellule].alignment = Alignment(horizontal='center')

    entetes = ['Rang', 'Matricule', 'Nom', 'Prénom(s)'] + [
        f"{m.nom} (coef. {nombre(m.coefficient)})" if parametre.avec_coefficients else m.nom for m in matieres]
    entetes += ['Total points', 'Moyenne', 'Mention', 'Décision']
    remplissage = PatternFill('solid', fgColor='2C3E50')
    bordure = Border(*(Side(style='thin', color='BDC3C7'),) * 4)
    for colonne, titre in enumerate(entetes, start=1):
        cellule = feuille.cell(row=5, column=colonne, value=titre)
        cellule.font = Font(bold=True, color='FFFFFF')
        cellule.fill = remplissage
        cellule.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
        cellule.border = bordure

    ligne_excel = 6
    for ligne in resultats:
        eleve = ligne['eleve']
        valeurs = [ligne['rang_texte'], eleve.matricule, eleve.nom, eleve.prenom]
        valeurs += [float(d['note']) if d['note'] is not None else None for d in ligne['details']]
        valeurs += [float(ligne['total_points']),
                    float(ligne['moyenne']) if ligne['moyenne'] is not None else None,
                    ligne['appreciation'], decision(ligne)]
        for colonne, valeur in enumerate(valeurs, start=1):
            cellule = feuille.cell(row=ligne_excel, column=colonne, value=valeur)
            cellule.border = bordure
            if colonne > 4:
                cellule.alignment = Alignment(horizontal='center')
        feuille.cell(row=ligne_excel, column=nb_colonnes).font = Font(
            bold=True, color='27AE60' if ligne['admis'] else 'E74C3C')
        ligne_excel += 1

    ligne_excel += 1
    for libelle, valeur in [
        ('Effectif', stats['effectif']), ('Admis', stats['admis']), ('Non admis', stats['non_admis']),
        ("Taux d'admission (%)", float(stats['taux_admission'])),
        ('Moyenne de la classe', float(stats['moyenne_classe']) if stats['moyenne_classe'] is not None else None),
    ]:
        feuille.cell(row=ligne_excel, column=3, value=libelle).font = Font(bold=True)
        feuille.cell(row=ligne_excel, column=4, value=valeur)
        ligne_excel += 1
    feuille.cell(row=ligne_excel + 1, column=1, value=MENTION_INDEPENDANCE).font = Font(italic=True, color='7F8C8D')

    largeurs = [8, 14, 22, 26] + [14] * len(matieres) + [13, 11, 22, 13]
    for colonne, largeur in enumerate(largeurs, start=1):
        feuille.column_dimensions[get_column_letter(colonne)].width = largeur
    feuille.row_dimensions[5].height = 32
    feuille.freeze_panes = 'E6'

    sortie = io.BytesIO()
    classeur.save(sortie)
    return sortie.getvalue()


def modele_saisie_excel(classe, eleves):
    """Feuille à remplir : un élève par ligne, une matière par colonne, notes actuelles pré-remplies."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.datavalidation import DataValidation

    from .models import NoteAccueil

    parametre = ParametreAccueil.pour(classe)
    matieres = list(MatiereAccueil.objects.filter(classe=classe))
    existantes = {(n.eleve_id, n.matiere_id): n.note for n in NoteAccueil.objects.filter(matiere__classe=classe)}

    classeur = Workbook()
    feuille = classeur.active
    feuille.title = "Notes"
    nb_colonnes = 3 + len(matieres)
    fin = get_column_letter(nb_colonnes)

    feuille.merge_cells(f"A1:{fin}1")
    feuille['A1'] = f"TEST D'ACCUEIL - {classe.nom} ({classe.annee_scolaire}) - notes sur {parametre.note_sur}"
    feuille['A1'].font = Font(bold=True, size=12)
    feuille.merge_cells(f"A2:{fin}2")
    feuille['A2'] = ("Remplissez les notes puis importez ce fichier. Une case vide ne change pas la note "
                     "existante. Ne modifiez pas les colonnes Matricule, Nom et Prénom(s).")
    feuille['A2'].font = Font(italic=True, color='7F8C8D')

    entetes = ['Matricule', 'Nom', 'Prénom(s)'] + [m.nom for m in matieres]
    remplissage = PatternFill('solid', fgColor='2C3E50')
    bordure = Border(*(Side(style='thin', color='BDC3C7'),) * 4)
    for colonne, titre in enumerate(entetes, start=1):
        cellule = feuille.cell(row=4, column=colonne, value=titre)
        cellule.font = Font(bold=True, color='FFFFFF')
        cellule.fill = remplissage
        cellule.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
        cellule.border = bordure

    for numero, eleve in enumerate(eleves, start=5):
        valeurs = [eleve.matricule, eleve.nom, eleve.prenom]
        valeurs += [float(existantes[(eleve.pk, m.pk)]) if (eleve.pk, m.pk) in existantes else None
                    for m in matieres]
        for colonne, valeur in enumerate(valeurs, start=1):
            cellule = feuille.cell(row=numero, column=colonne, value=valeur)
            cellule.border = bordure
            if colonne > 3:
                cellule.alignment = Alignment(horizontal='center')

    if matieres and eleves:
        controle = DataValidation(type='decimal', operator='between', formula1='0',
                                  formula2=str(parametre.note_sur), allow_blank=True, showErrorMessage=True,
                                  error=f"Note entre 0 et {parametre.note_sur}.")
        controle.add(f"D5:{fin}{4 + len(eleves)}")
        feuille.add_data_validation(controle)

    for colonne, largeur in enumerate([16, 22, 26] + [14] * len(matieres), start=1):
        feuille.column_dimensions[get_column_letter(colonne)].width = largeur
    feuille.row_dimensions[4].height = 32
    feuille.freeze_panes = 'D5'

    sortie = io.BytesIO()
    classeur.save(sortie)
    return sortie.getvalue()
