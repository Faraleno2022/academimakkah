"""Exports complets des listes d'abonnements bus et cantine."""

from decimal import Decimal
from html import escape
from io import BytesIO

from django.http import HttpResponse
from django.utils import timezone
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A3, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.platypus import LongTable, Paragraph, SimpleDocTemplate, Spacer, TableStyle


def _cellule_pdf(valeur, style):
    if isinstance(valeur, (int, float, Decimal)):
        contenu = f'{valeur:,.0f}'.replace(',', ' ')
    elif hasattr(valeur, 'strftime'):
        contenu = valeur.strftime('%d/%m/%Y')
    else:
        contenu = str(valeur) if valeur not in (None, '') else '—'
    return Paragraph(escape(contenu), style)


def exporter_liste(fmt, nom_fichier, titre, entetes_excel, lignes_excel,
                  entetes_pdf, lignes_pdf, total_encaisse):
    """Une ligne par abonnement/versement, sans limite de pagination."""
    lignes_excel = list(lignes_excel)
    lignes_pdf = list(lignes_pdf)
    horodatage = timezone.localdate().strftime('%Y%m%d')
    if fmt == 'excel':
        wb = Workbook()
        ws = wb.active
        ws.title = 'Abonnements'
        ws.append([titre])
        ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(entetes_excel))
        ws['A1'].font = Font(size=16, bold=True, color='17365D')
        ws.append([f'{len(lignes_excel)} abonnements / versements — Encaissé : {total_encaisse:,.0f} GNF'.replace(',', ' ')])
        ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=len(entetes_excel))
        ws.append([])
        ws.append(entetes_excel)
        for cell in ws[4]:
            cell.fill = PatternFill('solid', fgColor='17365D')
            cell.font = Font(color='FFFFFF', bold=True)
            cell.alignment = Alignment(wrap_text=True)
        for ligne in lignes_excel:
            ws.append([
                (int(v) if v == v.to_integral_value() else float(v))
                if isinstance(v, Decimal) else v for v in ligne
            ])
        ws.freeze_panes = 'A5'
        ws.auto_filter.ref = f'A4:{get_column_letter(len(entetes_excel))}{ws.max_row}'
        for index, entete in enumerate(entetes_excel, 1):
            longueur = max([len(str(entete))] + [len(str(ligne[index - 1] or '')) for ligne in lignes_excel])
            ws.column_dimensions[get_column_letter(index)].width = min(max(longueur + 2, 14), 38)
        for row in ws.iter_rows(min_row=5):
            for cell in row:
                if hasattr(cell.value, 'strftime'):
                    cell.number_format = 'DD/MM/YYYY'
                elif isinstance(cell.value, (int, float)):
                    cell.number_format = '#,##0.00' if isinstance(cell.value, float) else '#,##0'
        response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        response['Content-Disposition'] = f'attachment; filename="{nom_fichier}_{horodatage}.xlsx"'
        wb.save(response)
        return response

    if fmt != 'pdf':
        return HttpResponse('Format invalide', status=400)
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=landscape(A3),
                            leftMargin=30, rightMargin=30, topMargin=35, bottomMargin=35)
    styles = getSampleStyleSheet()
    cellule = ParagraphStyle('CelluleAbonnement', parent=styles['Normal'], fontSize=7, leading=9)
    entete = ParagraphStyle('EnteteAbonnement', parent=cellule, textColor=colors.white,
                            alignment=TA_CENTER, fontName='Helvetica-Bold')
    story = [Paragraph(escape(titre), styles['Title']), Spacer(1, 7),
             Paragraph(
                 f'{len(lignes_pdf)} abonnements / versements — Encaissé : {total_encaisse:,.0f} GNF'.replace(',', ' '),
                 styles['Normal'],
             ), Spacer(1, 12)]
    donnees = [[_cellule_pdf(valeur, entete) for valeur in entetes_pdf]]
    donnees += [[_cellule_pdf(valeur, cellule) for valeur in ligne] for ligne in lignes_pdf]
    if not lignes_pdf:
        donnees.append([_cellule_pdf('Aucun abonnement', cellule)] + [''] * (len(entetes_pdf) - 1))
    largeur = doc.width / len(entetes_pdf)
    tableau = LongTable(donnees, colWidths=[largeur] * len(entetes_pdf), repeatRows=1)
    tableau.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#17365D')),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#EDF3F8')]),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('GRID', (0, 0), (-1, -1), 0.25, colors.HexColor('#C7D3DF')),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
    ]))
    story.append(tableau)
    doc.build(story)
    response = HttpResponse(buffer.getvalue(), content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{nom_fichier}_{horodatage}.pdf"'
    return response
