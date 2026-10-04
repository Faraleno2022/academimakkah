"""Liste de contrôle du chauffeur : un élève par ligne, itinéraire en tête.

Les reçus bus sont des versements : un élève peut en avoir plusieurs. La liste
garde un élève par ligne, avec les informations logistiques les plus récentes
(itinéraire, arrêt, contact) et la situation de paiement de sa grille.
"""

from collections import Counter, defaultdict
from decimal import Decimal
from io import BytesIO
from xml.sax.saxutils import escape

from django.http import HttpResponse
from django.utils import timezone
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import cm
from reportlab.platypus import Paragraph, Spacer, Table

from .historique_eleve import _document, _entete, _gnf, _style_tableau, _styles

FORMATS_CONTROLE = ('pdf', 'excel')
SANS_ITINERAIRE = "Itinéraire non renseigné"
CHAMPS_LOGISTIQUES = ('itineraire', 'point_arret', 'contact_parent', 'zone')


def _statut(dernier, today):
    if dernier.statut == 'SUSPENDU':
        return 'Suspendu'
    if dernier.statut == 'EXPIRE' or (dernier.date_expiration and dernier.date_expiration < today):
        return 'Expiré'
    return 'Actif'


def _situation(grille, totaux, today):
    """(libellé, reste annuel, retard échu) ; le reste est None sans grille."""
    if grille is None:
        return 'Tarif inconnu', None, Decimal('0')
    situation = grille.situation_depuis_totaux(totaux)
    reste = situation['ANNUEL']['reste']
    echeances = {
        'T1': grille.date_echeance_tranche_1,
        'T2': grille.date_echeance_tranche_2,
        'T3': grille.date_echeance_tranche_3,
    }
    retard = sum(
        (situation[code]['reste'] for code, echeance in echeances.items() if echeance and echeance <= today),
        Decimal('0'),
    )
    if not reste:
        return 'Soldé', reste, retard
    if retard:
        return f"En retard : {_gnf(retard)}", reste, retard
    return 'À jour', reste, retard


def lignes_controle(visibles, filtres):
    """Un élève par ligne pour les élèves présents dans ``filtres``, triés par itinéraire."""
    today = timezone.localdate()
    eleve_ids = set(filtres.values_list('eleve_id', flat=True))
    versements = (
        visibles.filter(eleve_id__in=eleve_ids)
        .select_related('eleve__responsable_principal')
        .order_by('eleve_id', '-date_debut', '-created_at', '-pk')
    )
    par_eleve = defaultdict(list)
    for versement in versements:
        par_eleve[versement.eleve_id].append(versement)

    lignes = []
    for paiements in par_eleve.values():
        dernier = paiements[0]
        eleve = dernier.eleve
        # Valeur la plus récente non vide : un ancien reçu peut seul porter l'itinéraire.
        infos = {
            champ: next((getattr(p, champ) for p in paiements if getattr(p, champ)), '')
            for champ in CHAMPS_LOGISTIQUES
        }
        if not infos['contact_parent']:
            infos['contact_parent'] = getattr(getattr(eleve, 'responsable_principal', None), 'telephone', '') or ''
        grille = dernier.grille if dernier.grille_id else None
        totaux = defaultdict(Decimal)
        for paiement in paiements:
            if grille and paiement.grille_id == grille.pk:
                totaux[paiement.periodicite] += Decimal(paiement.montant or 0)
        statut = _statut(dernier, today)
        situation, reste, retard = _situation(grille, totaux, today)
        if statut != 'Actif':
            situation = statut if not reste else f"{statut} — reste {_gnf(reste)}"
        zone = getattr(grille, 'zone', '') or infos['zone']
        lignes.append({
            'eleve': eleve,
            # Sans itinéraire saisi, la zone / circuit de la grille sert de repère au chauffeur.
            'itineraire': infos['itineraire'] or zone or SANS_ITINERAIRE,
            'point_arret': infos['point_arret'],
            'contact': infos['contact_parent'],
            'classe': getattr(getattr(eleve, 'classe', None), 'nom', '') or '',
            'zone': zone,
            'statut': statut,
            'situation': situation,
            'reste': reste,
            'alerte': statut != 'Actif' or bool(retard),
        })
    lignes.sort(key=lambda l: (
        l['itineraire'] == SANS_ITINERAIRE, l['itineraire'].casefold(),
        l['classe'].casefold(), (l['eleve'].nom or '').casefold(), (l['eleve'].prenom or '').casefold(),
    ))
    return lignes


def _nom(eleve):
    return f"{eleve.prenom} {eleve.nom}".strip()


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------

ENTETES_PDF = ['N°', 'Itinéraire', 'Élève', 'Contact parent', 'Classe', "Point d'arrêt", 'Situation', 'Contrôle']


def pdf_controle(lignes, ecole, precisions):
    buffer = BytesIO()
    pagesize = landscape(A4)
    doc, page = _document(buffer, pagesize, "Liste de contrôle du bus", ecole)
    styles = _styles()
    cell = styles['AboCell']
    largeur = pagesize[0] - doc.leftMargin - doc.rightMargin
    effectifs = Counter(ligne['itineraire'] for ligne in lignes)
    nb_itineraires = len(effectifs)
    resume = (
        f"<b>{len(lignes)}</b> élève(s) — <b>{nb_itineraires}</b> itinéraire(s)"
        + (f" &nbsp;|&nbsp; {escape(' · '.join(precisions))}" if precisions else '')
        + " &nbsp;|&nbsp; Cochez la colonne « Contrôle » à la montée de l'élève."
    )
    elements = [
        _entete(ecole, "LISTE DE CONTRÔLE<br/>DU BUS SCOLAIRE", largeur, styles), Spacer(1, 0.25 * cm),
        Paragraph(resume, styles['AboInfo']), Spacer(1, 0.25 * cm),
    ]

    data = [[Paragraph(f"<font color='white'><b>{entete}</b></font>", cell) for entete in ENTETES_PDF]]
    style = _style_tableau()
    itineraire_courant = None
    numero = 0
    for ligne in lignes:
        if ligne['itineraire'] != itineraire_courant:
            itineraire_courant = ligne['itineraire']
            data.append([Paragraph(
                f"<b>Itinéraire : {escape(itineraire_courant)}</b> — {effectifs[itineraire_courant]} élève(s)", styles['AboInfo']
            )] + [''] * (len(ENTETES_PDF) - 1))
            rang = len(data) - 1
            style.add('SPAN', (0, rang), (-1, rang))
            style.add('BACKGROUND', (0, rang), (-1, rang), colors.HexColor('#FFF4D6'))
        numero += 1
        eleve = ligne['eleve']
        data.append([
            Paragraph(str(numero), cell),
            Paragraph(f"<b>{escape(ligne['itineraire'])}</b>", cell),
            Paragraph(
                f"<b>{escape(_nom(eleve))}</b><br/><font size=7>{escape(eleve.matricule or '')}</font>", cell
            ),
            Paragraph(escape(ligne['contact'] or '—'), cell),
            Paragraph(escape(ligne['classe'] or '—'), cell),
            Paragraph(escape(ligne['point_arret'] or '—'), cell),
            Paragraph(
                f"<font color='#B42318'><b>{escape(ligne['situation'])}</b></font>"
                if ligne['alerte'] else escape(ligne['situation']),
                cell,
            ),
            '',
        ])
    if not lignes:
        data.append([Paragraph("Aucun abonné bus pour ces critères.", cell)] + [''] * (len(ENTETES_PDF) - 1))
        style.add('SPAN', (0, 1), (-1, 1))

    proportions = [0.7, 3.3, 3.9, 2.4, 1.9, 2.8, 3.0, 1.6]
    facteur = largeur / sum(proportions)
    tableau = Table(data, colWidths=[p * facteur for p in proportions], repeatRows=1)
    tableau.setStyle(style)
    elements.append(tableau)
    doc.build(elements, onFirstPage=page, onLaterPages=page)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Excel
# ---------------------------------------------------------------------------

ENTETES_EXCEL = [
    'N°', 'Itinéraire', 'Élève', 'Matricule', 'Contact parent', 'Classe', "Point d'arrêt",
    'Zone / circuit', 'Statut', 'Situation', 'Reste annuel (GNF)', 'Contrôle',
]


def excel_controle(lignes, ecole, precisions):
    wb = Workbook()
    ws = wb.active
    ws.title = "Contrôle bus"
    ws.append([f"Liste de contrôle du bus — {getattr(ecole, 'nom', '') or 'École'}"])
    ws['A1'].font = Font(bold=True, size=14)
    ws.append([
        f"Généré le {timezone.localtime().strftime('%d/%m/%Y %H:%M')} — {len(lignes)} élève(s)"
        + (f" — {' · '.join(precisions)}" if precisions else '')
    ])
    ws.append([])
    entete_ligne = 4
    bordure = Border(*(Side(style='thin', color='9FB3C8'),) * 4)
    for col, entete in enumerate(ENTETES_EXCEL, 1):
        c = ws.cell(row=entete_ligne, column=col, value=entete)
        c.font = Font(bold=True, color='FFFFFF')
        c.fill = PatternFill('solid', fgColor='123B5D')
        c.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
        c.border = bordure

    alerte = Font(color='B42318', bold=True)
    for numero, ligne in enumerate(lignes, 1):
        eleve = ligne['eleve']
        valeurs = [
            numero, ligne['itineraire'], _nom(eleve), eleve.matricule or '', ligne['contact'],
            ligne['classe'], ligne['point_arret'], ligne['zone'], ligne['statut'], ligne['situation'],
            int(ligne['reste']) if ligne['reste'] is not None else None, '',
        ]
        rang = entete_ligne + numero
        for col, valeur in enumerate(valeurs, 1):
            c = ws.cell(row=rang, column=col, value=valeur)
            c.border = bordure
            c.alignment = Alignment(vertical='center', wrap_text=col in (2, 7, 10))
        ws.cell(row=rang, column=2).font = Font(bold=True)
        ws.cell(row=rang, column=11).number_format = '#,##0'
        if ligne['alerte']:
            ws.cell(row=rang, column=10).font = alerte

    for col, largeur in enumerate([6, 26, 28, 14, 18, 14, 22, 18, 10, 28, 16, 11], 1):
        ws.column_dimensions[get_column_letter(col)].width = largeur
    derniere = entete_ligne + max(len(lignes), 1)
    ws.auto_filter.ref = f"A{entete_ligne}:{get_column_letter(len(ENTETES_EXCEL))}{derniere}"
    ws.freeze_panes = ws.cell(row=entete_ligne + 1, column=3)
    ws.print_title_rows = f"{entete_ligne}:{entete_ligne}"
    ws.page_setup.orientation = 'landscape'
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True

    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def export_controle(fmt, lignes, ecole, precisions):
    horodatage = timezone.localdate().strftime('%Y%m%d')
    if fmt == 'pdf':
        response = HttpResponse(pdf_controle(lignes, ecole, precisions), content_type='application/pdf')
        response['Content-Disposition'] = f'inline; filename="controle_bus_{horodatage}.pdf"'
        return response
    response = HttpResponse(
        excel_controle(lignes, ecole, precisions),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )
    response['Content-Disposition'] = f'attachment; filename="controle_bus_{horodatage}.xlsx"'
    return response
