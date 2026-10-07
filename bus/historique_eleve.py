"""Abonnements d'un élève : reprise des informations, historique, exports et carnet.

Un élève peut cumuler plusieurs abonnements (bus et cantine). Les informations
propres à l'enfant (régime, allergies, contact, zone...) sont reprises du
dernier abonnement pour ne pas les ressaisir à chaque renouvellement.
"""

from datetime import timedelta
from decimal import Decimal
from io import BytesIO
import os
from xml.sax.saxutils import escape

from dateutil.relativedelta import relativedelta
from django.urls import reverse
from django.utils import timezone
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (
    Image,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from ecole_moderne.pdf_utils import draw_logo_watermark

from .models import AbonnementBus, AbonnementCantine, GrilleTarifaireBus, RemiseBus


MOIS_FR = (
    "", "Janvier", "Février", "Mars", "Avril", "Mai", "Juin", "Juillet",
    "Août", "Septembre", "Octobre", "Novembre", "Décembre",
)
JOURS_FR = ("Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche")

TYPES_HISTORIQUE = {
    'tous': "Bus et cantine",
    'bus': "Bus uniquement",
    'cantine': "Cantine uniquement",
}

# Nombre minimal de lignes du carnet : les lignes vides servent aux visas manuscrits.
LIGNES_MIN_CARNET = 12


def _gnf(value):
    return f"{int(Decimal(str(value or 0))):,}".replace(",", " ") + " GNF"


def _date(value):
    return value.strftime('%d/%m/%Y') if value else ''


def libelle_mois(debut, fin=None):
    """« Septembre 2026 » ou « Septembre 2026 → Novembre 2026 » si la période déborde."""
    if not debut:
        return ''
    libelle = f"{MOIS_FR[debut.month]} {debut.year}"
    if fin and (fin.year, fin.month) != (debut.year, debut.month):
        libelle += f" → {MOIS_FR[fin.month]} {fin.year}"
    return libelle


def jour_semaine(value):
    return JOURS_FR[value.weekday()] if value else ''


# ---------------------------------------------------------------------------
# Reprise des informations du dernier abonnement
# ---------------------------------------------------------------------------

def date_expiration_cantine(date_debut, periodicite):
    """Date de fin d'une période cantine commençant le ``date_debut`` (incluse)."""
    if not date_debut:
        return None
    duree = {
        AbonnementCantine.Periodicite.JOURNALIER: relativedelta(days=1),
        AbonnementCantine.Periodicite.HEBDOMADAIRE: relativedelta(weeks=1),
        AbonnementCantine.Periodicite.MENSUEL: relativedelta(months=1),
        AbonnementCantine.Periodicite.TRIMESTRIEL: relativedelta(months=3),
        AbonnementCantine.Periodicite.ANNUEL: relativedelta(years=1),
    }.get(periodicite, relativedelta(months=1))
    return date_debut + duree - timedelta(days=1)


def date_debut_suivante(dernier_abonnement):
    """Le nouvel abonnement démarre le lendemain du précédent, jamais dans le passé."""
    today = timezone.localdate()
    if not dernier_abonnement or not dernier_abonnement.date_expiration:
        return today
    return max(dernier_abonnement.date_expiration + timedelta(days=1), today)


def dernier_abonnement_cantine(eleve):
    return (
        AbonnementCantine.objects.filter(eleve=eleve)
        .order_by('-date_expiration', '-created_at', '-pk')
        .first()
    )


def dernier_abonnement_bus(eleve):
    return (
        AbonnementBus.objects.filter(eleve=eleve)
        .select_related('grille', 'mode_paiement')
        .order_by('-date_debut', '-created_at', '-pk')
        .first()
    )


def remises_bus_eleve(eleve):
    """Remises bus de l'élève, avec le tarif, la remise et le net dû de l'année."""
    resultat = []
    for remise in RemiseBus.objects.filter(eleve=eleve).select_related('grille').order_by('-grille__annee_scolaire'):
        annuel = remise.grille.situation_paiements(eleve, remise=remise)['ANNUEL']
        resultat.append({'objet': remise, 'grille': remise.grille, **annuel})
    return resultat


def texte_remise_bus(item):
    """« Ratoma 2026-2027 : 10 % — Réduction fratrie, 40 000 GNF déduits (net dû 360 000 GNF) »."""
    return (
        f"{item['grille'].zone} {item['grille'].annee_scolaire} : {item['objet'].libelle}, "
        f"{_gnf(item['remise'])} déduits (net dû {_gnf(item['du'])})"
    )


def _contact_responsable(eleve):
    responsable = getattr(eleve, 'responsable_principal', None)
    return getattr(responsable, 'telephone', '') or ''


def reprise_cantine(eleve):
    """Valeurs initiales d'un nouvel abonnement cantine pour cet élève."""
    dernier = dernier_abonnement_cantine(eleve)
    if not dernier:
        contact = _contact_responsable(eleve)
        return {'contact_parent': contact} if contact else {}
    date_debut = date_debut_suivante(dernier)
    return {
        'type_repas': dernier.type_repas,
        'periodicite': dernier.periodicite,
        'montant': int(dernier.montant or 0),
        'date_debut': date_debut,
        'date_expiration': date_expiration_cantine(date_debut, dernier.periodicite),
        'alerte_avant_jours': dernier.alerte_avant_jours,
        'regime_alimentaire': dernier.regime_alimentaire,
        'allergies': dernier.allergies,
        'contact_parent': dernier.contact_parent or _contact_responsable(eleve),
        'dernier_expire_le': dernier.date_expiration,
    }


def _grille_reprise(dernier):
    """Grille du dernier paiement si elle est encore active, sinon la plus récente de la même zone."""
    grille = dernier.grille
    if grille and grille.actif:
        return grille
    zone = getattr(grille, 'zone', '') or dernier.zone
    ecole_id = getattr(grille, 'ecole_id', None) or getattr(
        getattr(dernier.eleve, 'classe', None), 'ecole_id', None
    )
    if not zone or not ecole_id:
        return None
    return (
        GrilleTarifaireBus.objects.filter(ecole_id=ecole_id, zone__iexact=zone, actif=True)
        .order_by('-annee_scolaire')
        .first()
    )


def reprise_bus(eleve):
    """Valeurs initiales d'un nouveau paiement bus : zone, tranche suivante, mode de paiement."""
    dernier = dernier_abonnement_bus(eleve)
    if not dernier:
        return {}
    valeurs = {'dernier_expire_le': dernier.date_expiration}
    if dernier.mode_paiement_id:
        valeurs['mode_paiement'] = dernier.mode_paiement_id
    grille = _grille_reprise(dernier)
    if grille:
        valeurs['grille'] = grille.pk
        situation = grille.situation_paiements(eleve)
        tranche = next((code for code in ('T1', 'T2', 'T3') if situation[code]['reste'] > 0), None)
        if tranche:
            valeurs['periodicite'] = tranche
            valeurs['montant'] = int(situation[tranche]['reste'])
    return valeurs


def infos_logistiques_bus(eleve):
    """Itinéraire, arrêt et contact du dernier abonnement bus (non ressaisis dans le formulaire)."""
    dernier = dernier_abonnement_bus(eleve)
    return {
        'itineraire': getattr(dernier, 'itineraire', '') or '',
        'point_arret': getattr(dernier, 'point_arret', '') or '',
        'contact_parent': getattr(dernier, 'contact_parent', '') or _contact_responsable(eleve),
    }


def resume_reprise_json(eleve):
    """Données de reprise renvoyées aux formulaires via l'API élève."""
    def _serialiser(valeurs):
        return {
            cle: (valeur.isoformat() if hasattr(valeur, 'isoformat') else valeur)
            for cle, valeur in valeurs.items()
        }

    nb_bus = AbonnementBus.objects.filter(eleve=eleve).count()
    nb_cantine = AbonnementCantine.objects.filter(eleve=eleve).count()
    return {
        'nb_abonnements_bus': nb_bus,
        'nb_abonnements_cantine': nb_cantine,
        'reprise_bus': _serialiser(reprise_bus(eleve)) if nb_bus else None,
        'reprise_cantine': _serialiser(reprise_cantine(eleve)) if nb_cantine else None,
        'logistique_bus': infos_logistiques_bus(eleve),
        'historique_url': f"{reverse('bus:historique_abonnements')}?eleve={eleve.pk}",
    }


# ---------------------------------------------------------------------------
# Historique unifié
# ---------------------------------------------------------------------------

def _statut(abonnement):
    if abonnement.statut == 'ACTIF' and abonnement.est_expire:
        return "Expiré"
    return abonnement.get_statut_display()


def lignes_abonnements(eleve, type_abonnement='tous'):
    """Tous les abonnements de l'élève, du plus ancien au plus récent."""
    today = timezone.localdate()
    lignes = []
    if type_abonnement in ('tous', 'bus'):
        for abo in AbonnementBus.objects.filter(eleve=eleve).select_related('grille', 'mode_paiement'):
            lignes.append({
                'objet': abo,
                'service': 'Bus',
                'detail': f"{abo.grille.zone if abo.grille_id else abo.zone or '—'} · {abo.get_periodicite_display()}",
                'montant': Decimal(abo.montant or 0),
                'date_debut': abo.date_debut,
                'date_expiration': abo.date_expiration,
                'reference': abo.numero_recu or abo.reference_externe or '',
                'statut': _statut(abo),
            })
    if type_abonnement in ('tous', 'cantine'):
        for abo in AbonnementCantine.objects.filter(eleve=eleve):
            lignes.append({
                'objet': abo,
                'service': 'Cantine',
                'detail': f"{abo.get_type_repas_display()} · {abo.get_periodicite_display()}",
                'montant': Decimal(abo.montant or 0),
                'date_debut': abo.date_debut,
                'date_expiration': abo.date_expiration,
                'reference': abo.reference_externe or '',
                'statut': _statut(abo),
            })
    lignes.sort(key=lambda ligne: (ligne['date_debut'] or today, ligne['service']))
    for ligne in lignes:
        ligne['mois'] = libelle_mois(ligne['date_debut'], ligne['date_expiration'])
        ligne['jour_expiration'] = jour_semaine(ligne['date_expiration'])
        ligne['jours_restants'] = (
            max((ligne['date_expiration'] - today).days, 0) if ligne['date_expiration'] else 0
        )
    return lignes


def resume_abonnements(lignes):
    today = timezone.localdate()
    actifs = [
        ligne for ligne in lignes
        if ligne['statut'] == 'Actif' and ligne['date_expiration'] and ligne['date_expiration'] >= today
    ]
    expirations = [ligne['date_expiration'] for ligne in lignes if ligne['date_expiration']]
    return {
        'nombre': len(lignes),
        'total': sum((ligne['montant'] for ligne in lignes), Decimal('0')),
        'total_bus': sum((ligne['montant'] for ligne in lignes if ligne['service'] == 'Bus'), Decimal('0')),
        'total_cantine': sum(
            (ligne['montant'] for ligne in lignes if ligne['service'] == 'Cantine'), Decimal('0')
        ),
        'nb_actifs': len(actifs),
        'derniere_expiration': max(expirations) if expirations else None,
    }


# ---------------------------------------------------------------------------
# Export Excel
# ---------------------------------------------------------------------------

ENTETES = [
    'N°', 'Service', 'Détail', 'Mois', 'Montant payé (GNF)', 'Date début',
    "Date d'expiration", "Jour d'expiration", 'Jours restants', 'Statut', 'Reçu / Référence',
]


def export_excel(blocs, titre):
    """``blocs`` : liste de (eleve, lignes). Une feuille, un bloc par élève."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Abonnements"
    gras = Font(bold=True)
    entete_fill = PatternFill('solid', fgColor='123B5D')
    bordure = Border(*(Side(style='thin', color='9FB3C8'),) * 4)

    ws.append([titre])
    ws['A1'].font = Font(bold=True, size=14)
    ws.append([f"Généré le {timezone.localtime().strftime('%d/%m/%Y %H:%M')}"])
    ligne_courante = 3

    for eleve, lignes in blocs:
        ligne_courante += 1
        classe = getattr(getattr(eleve, 'classe', None), 'nom', '') or ''
        ws.cell(row=ligne_courante, column=1,
                value=f"{eleve.matricule or ''} — {eleve.prenom} {eleve.nom} ({classe})").font = Font(bold=True, size=12)
        ligne_courante += 1
        for col, entete in enumerate(ENTETES, 1):
            cell = ws.cell(row=ligne_courante, column=col, value=entete)
            cell.font = Font(bold=True, color='FFFFFF')
            cell.fill = entete_fill
            cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
            cell.border = bordure
        for numero, ligne in enumerate(lignes, 1):
            ligne_courante += 1
            valeurs = [
                numero, ligne['service'], ligne['detail'], ligne['mois'], int(ligne['montant']),
                ligne['date_debut'], ligne['date_expiration'], ligne['jour_expiration'],
                ligne['jours_restants'], ligne['statut'], ligne['reference'],
            ]
            for col, valeur in enumerate(valeurs, 1):
                cell = ws.cell(row=ligne_courante, column=col, value=valeur)
                cell.border = bordure
                if col in (6, 7) and valeur:
                    cell.number_format = 'DD/MM/YYYY'
                if col == 5:
                    cell.number_format = '#,##0'
        ligne_courante += 1
        resume = resume_abonnements(lignes)
        ws.cell(row=ligne_courante, column=4, value='Total payé').font = gras
        total = ws.cell(row=ligne_courante, column=5, value=int(resume['total']))
        total.font = gras
        total.number_format = '#,##0'
        ligne_courante += 1

    for col, largeur in enumerate([6, 10, 30, 30, 18, 13, 15, 15, 13, 12, 18], 1):
        ws.column_dimensions[get_column_letter(col)].width = largeur

    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# PDF : liste et carnet
# ---------------------------------------------------------------------------

def _styles():
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(
        name='AboTitre', parent=styles['Title'], fontName='Helvetica-Bold', fontSize=16,
        leading=19, textColor=colors.HexColor('#123B5D'), alignment=TA_CENTER, spaceAfter=2,
    ))
    styles.add(ParagraphStyle(
        name='AboInfo', parent=styles['Normal'], fontSize=9, leading=12,
        textColor=colors.HexColor('#243B53'), alignment=TA_LEFT,
    ))
    styles.add(ParagraphStyle(
        name='AboCell', parent=styles['Normal'], fontSize=8.3, leading=10, alignment=TA_CENTER,
    ))
    styles.add(ParagraphStyle(
        name='AboNote', parent=styles['Normal'], fontSize=8, leading=10,
        textColor=colors.HexColor('#52606D'), alignment=TA_CENTER,
    ))
    return styles


def _fichier_existant(field):
    try:
        path = getattr(field, 'path', None)
        return path if path and os.path.exists(path) else None
    except Exception:
        return None


def _entete(ecole, titre, largeur, styles):
    school_name = getattr(ecole, 'nom', '') or 'Établissement scolaire'
    details = [
        f"{label} : {value}"
        for label, value in (
            ('Adresse', getattr(ecole, 'adresse', '')),
            ('Téléphone', getattr(ecole, 'telephone', '')),
        ) if value
    ]
    bloc_ecole = Paragraph(f"<b>{escape(school_name)}</b><br/>" + "<br/>".join(map(escape, details)), styles['AboInfo'])
    bloc_titre = Paragraph(titre, styles['AboTitre'])
    logo_path = _fichier_existant(getattr(ecole, 'logo', None))
    if logo_path:
        cellules = [Image(logo_path, width=1.7 * cm, height=1.7 * cm), bloc_ecole, bloc_titre]
        largeurs = [2.1 * cm, largeur * 0.55 - 2.1 * cm, largeur * 0.45]
    else:
        cellules = [bloc_ecole, bloc_titre]
        largeurs = [largeur * 0.55, largeur * 0.45]
    table = Table([cellules], colWidths=largeurs)
    table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('BOX', (0, 0), (-1, -1), 1, colors.HexColor('#123B5D')),
        ('LINEBELOW', (0, 0), (-1, -1), 3, colors.HexColor('#D4A72C')),
        ('BACKGROUND', (-1, 0), (-1, 0), colors.HexColor('#F0F4F8')),
        ('TOPPADDING', (0, 0), (-1, -1), 7),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 7),
    ]))
    return table


def _bloc_eleve(eleve, resume, largeur, styles, avec_photo=True):
    classe = getattr(eleve, 'classe', None)
    infos = Table([
        [
            Paragraph(f"<b>Élève :</b> {escape(f'{eleve.prenom} {eleve.nom}')}", styles['AboInfo']),
            Paragraph(f"<b>Matricule :</b> {escape(eleve.matricule or '—')}", styles['AboInfo']),
        ],
        [
            Paragraph(f"<b>Classe :</b> {escape(getattr(classe, 'nom', '') or '—')}", styles['AboInfo']),
            Paragraph(f"<b>Année scolaire :</b> {escape(getattr(classe, 'annee_scolaire', '') or '—')}", styles['AboInfo']),
        ],
        [
            Paragraph(f"<b>Abonnements :</b> {resume['nombre']} (dont {resume['nb_actifs']} en cours)", styles['AboInfo']),
            Paragraph(f"<b>Total payé :</b> {_gnf(resume['total'])}", styles['AboInfo']),
        ],
    ])
    infos.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]))
    photo_path = _fichier_existant(getattr(eleve, 'photo', None)) if avec_photo else None
    if photo_path:
        cellules = [infos, Image(photo_path, width=2.3 * cm, height=2.6 * cm)]
        largeurs = [largeur - 2.9 * cm, 2.9 * cm]
    else:
        cellules, largeurs = [infos], [largeur]
    table = Table([cellules], colWidths=largeurs)
    table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#F8FAFC')),
        ('BOX', (0, 0), (-1, -1), 0.8, colors.HexColor('#BCCCDC')),
    ]))
    return table


def _style_tableau():
    return TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#123B5D')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('BOX', (0, 0), (-1, -1), 1, colors.HexColor('#123B5D')),
        ('INNERGRID', (0, 0), (-1, -1), 0.45, colors.HexColor('#9FB3C8')),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#F8FAFC')]),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('LEFTPADDING', (0, 0), (-1, -1), 3),
        ('RIGHTPADDING', (0, 0), (-1, -1), 3),
    ])


def _document(buffer, pagesize, titre, ecole):
    doc = SimpleDocTemplate(
        buffer, pagesize=pagesize,
        leftMargin=1.3 * cm, rightMargin=1.3 * cm, topMargin=1.2 * cm, bottomMargin=1.4 * cm,
        title=titre, author=getattr(ecole, 'nom', '') or 'MySchoolGN',
    )

    def _page(canvas, document):
        canvas.saveState()
        try:
            draw_logo_watermark(canvas, pagesize[0], pagesize[1], ecole=ecole)
        except Exception:
            pass
        canvas.setStrokeColor(colors.HexColor('#BCCCDC'))
        canvas.setLineWidth(0.5)
        canvas.line(doc.leftMargin, 0.85 * cm, pagesize[0] - doc.rightMargin, 0.85 * cm)
        canvas.setFont('Helvetica', 7.5)
        canvas.setFillColor(colors.HexColor('#52606D'))
        canvas.drawString(doc.leftMargin, 0.55 * cm, titre)
        canvas.drawRightString(
            pagesize[0] - doc.rightMargin, 0.55 * cm,
            f"Page {document.page} - Généré le {timezone.localtime().strftime('%d/%m/%Y %H:%M')}",
        )
        canvas.restoreState()

    return doc, _page


def pdf_liste(blocs, ecole, titre="LISTE DES ABONNEMENTS"):
    """Liste détaillée (paysage) des abonnements d'un ou plusieurs élèves."""
    buffer = BytesIO()
    pagesize = landscape(A4)
    doc, page = _document(buffer, pagesize, "Liste des abonnements", ecole)
    styles = _styles()
    largeur = pagesize[0] - doc.leftMargin - doc.rightMargin
    elements = []
    for index, (eleve, lignes) in enumerate(blocs):
        if index:
            elements.append(PageBreak())
        resume = resume_abonnements(lignes)
        elements += [
            _entete(ecole, titre, largeur, styles), Spacer(1, 0.3 * cm),
            _bloc_eleve(eleve, resume, largeur, styles, avec_photo=False), Spacer(1, 0.3 * cm),
        ]
        cell = styles['AboCell']
        data = [[Paragraph(f"<b>{entete}</b>", cell) for entete in ENTETES]]
        for numero, ligne in enumerate(lignes, 1):
            data.append([Paragraph(escape(str(valeur)), cell) for valeur in (
                numero, ligne['service'], ligne['detail'], ligne['mois'], _gnf(ligne['montant']),
                _date(ligne['date_debut']), _date(ligne['date_expiration']), ligne['jour_expiration'],
                ligne['jours_restants'], ligne['statut'], ligne['reference'] or '—',
            )])
        if not lignes:
            data.append([Paragraph("Aucun abonnement enregistré.", cell)] + [''] * (len(ENTETES) - 1))
        data.append([''] * 3 + [Paragraph('<b>Total payé</b>', cell), Paragraph(f"<b>{_gnf(resume['total'])}</b>", cell)] + [''] * 6)
        proportions = [0.8, 1.5, 4.2, 4.0, 2.6, 2.1, 2.2, 2.1, 1.6, 1.7, 2.5]
        facteur = largeur / sum(proportions)
        tableau = Table(data, colWidths=[p * facteur for p in proportions], repeatRows=1)
        style = _style_tableau()
        if not lignes:
            style.add('SPAN', (0, 1), (-1, 1))
        tableau.setStyle(style)
        elements.append(tableau)
    doc.build(elements, onFirstPage=page, onLaterPages=page)
    return buffer.getvalue()


def pdf_carnet(blocs, ecole):
    """Carnet d'abonnement (portrait) : une page par élève, avec colonne de visa."""
    buffer = BytesIO()
    doc, page = _document(buffer, A4, "Carnet d'abonnement", ecole)
    styles = _styles()
    largeur = A4[0] - doc.leftMargin - doc.rightMargin
    cell = styles['AboCell']
    entetes = ['N°', 'Mois', 'Service', 'Montant payé', 'Date début',
               "Date d'expiration", "Jour d'expiration", 'Visa']
    elements = []
    for index, (eleve, lignes) in enumerate(blocs):
        if index:
            elements.append(PageBreak())
        resume = resume_abonnements(lignes)
        elements += [
            _entete(ecole, "CARNET<br/>D'ABONNEMENT", largeur, styles), Spacer(1, 0.3 * cm),
            _bloc_eleve(eleve, resume, largeur, styles), Spacer(1, 0.35 * cm),
        ]
        for item in remises_bus_eleve(eleve):
            elements += [
                Paragraph(f"<b>Remise bus :</b> {escape(texte_remise_bus(item))}", styles['AboInfo']),
                Spacer(1, 0.15 * cm),
            ]
        data = [[Paragraph(f"<b>{entete}</b>", cell) for entete in entetes]]
        for numero, ligne in enumerate(lignes, 1):
            rangee = [Paragraph(escape(str(valeur)), cell) for valeur in (
                numero, ligne['mois'], '',
                _gnf(ligne['montant']), _date(ligne['date_debut']), _date(ligne['date_expiration']),
                ligne['jour_expiration'], '',
            )]
            rangee[2] = Paragraph(
                f"{ligne['service']}<br/><font size=7>{escape(ligne['detail'])}</font>", cell
            )
            data.append(rangee)
        for numero in range(len(lignes) + 1, max(LIGNES_MIN_CARNET, len(lignes)) + 1):
            data.append([Paragraph(str(numero), cell)] + [''] * (len(entetes) - 1))
        proportions = [0.8, 3.4, 3.0, 2.5, 2.1, 2.2, 2.0, 2.3]
        facteur = largeur / sum(proportions)
        tableau = Table(
            data, colWidths=[p * facteur for p in proportions], repeatRows=1,
            rowHeights=[0.75 * cm] + [0.95 * cm] * (len(data) - 1),
        )
        tableau.setStyle(_style_tableau())
        derniere = resume['derniere_expiration']
        elements += [
            tableau, Spacer(1, 0.3 * cm),
            Paragraph(
                f"<b>Total payé :</b> {_gnf(resume['total'])} "
                f"(Bus : {_gnf(resume['total_bus'])} — Cantine : {_gnf(resume['total_cantine'])})"
                + (f" &nbsp;|&nbsp; <b>Dernière expiration :</b> {jour_semaine(derniere)} {_date(derniere)}" if derniere else ''),
                styles['AboInfo'],
            ),
            Spacer(1, 0.2 * cm),
            Paragraph(
                "Présentez ce carnet à chaque renouvellement. Chaque ligne doit être visée par le service "
                "comptable. L'accès au service prend fin le jour d'expiration indiqué.",
                styles['AboNote'],
            ),
        ]
    doc.build(elements, onFirstPage=page, onLaterPages=page)
    return buffer.getvalue()
