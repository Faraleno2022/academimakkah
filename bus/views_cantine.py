"""
Vues pour la gestion des abonnements à la cantine scolaire
"""

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import HttpResponse, JsonResponse
from django.core.paginator import Paginator
from django.db.models import Q, Sum, Count, F, DecimalField, ExpressionWrapper
from django.db.models.functions import Coalesce
from django.utils import timezone
from datetime import timedelta
from decimal import Decimal
import io

from eleves.models import Eleve
from .models import AbonnementCantine
from .forms import AbonnementCantineForm
from .historique_eleve import reprise_cantine, resume_reprise_json
from .recettes import CHAMP_DATE_CANTINE, LIBELLES_PERIODES, filtrer_periode, recettes_par_periode
from utilisateurs.utils import (
    filter_by_user_school,
    user_is_admin,
    user_is_superadmin,
    user_school,
)
from utilisateurs.permissions import can_delete_subscriptions
from ecole_moderne.security_decorators import require_school_object
from ecole_moderne.recherche import filtrer_termes
from .export_listes import exporter_liste
from .periodicites import resumes_periodicites_cantine, totaux_cantine


def _abonnements_cantine_tries(qs, avec_remise=False):
    """Conserve seulement les abonnements bénéficiant d'une remise réelle."""
    if avec_remise:
        brut = Coalesce('montant_tarif', 'montant', output_field=DecimalField(max_digits=10, decimal_places=0))
        qs = qs.alias(
            _remise_produit=ExpressionWrapper(
                brut * F('valeur_remise'),
                output_field=DecimalField(max_digits=20, decimal_places=2),
            ),
        ).filter(
            Q(montant_tarif__gt=0) | Q(montant_tarif__isnull=True, montant__gt=0),
        ).filter(
            Q(type_remise='MONTANT', valeur_remise__gte=Decimal('0.5'))
            | Q(type_remise='POURCENTAGE', _remise_produit__gte=Decimal('50')),
        )
    return qs.order_by('-date_expiration', 'eleve__nom', '-pk')


@login_required
def tableau_bord_cantine(request):
    """Tableau de bord des abonnements cantine avec alertes"""
    qs = AbonnementCantine.objects.select_related('eleve', 'eleve__classe', 'eleve__classe__ecole')
    # IMPORTANT: Seul le superuser peut voir toutes les écoles
    if not user_is_superadmin(request.user):
        qs = filter_by_user_school(qs, request.user, 'eleve__classe__ecole')
    
    # Statistiques générales
    total = qs.count()
    actifs = qs.filter(statut='ACTIF').count()
    expires = qs.filter(statut='EXPIRE').count()
    suspendus = qs.filter(statut='SUSPENDU').count()
    
    # Alertes
    today = timezone.localdate()
    from datetime import timedelta

    # Abonnements expirés (ACTIF mais date dépassée) — requête DB au lieu de boucle Python
    abonnements_expires = qs.filter(statut='ACTIF', date_expiration__lt=today)

    # Abonnements proches de l'expiration (dans les 7 jours, pas encore expirés)
    abonnements_proche_expiration = qs.filter(
        statut='ACTIF',
        date_expiration__gte=today,
        date_expiration__lte=today + timedelta(days=7)
    )

    # Abonnements critiques (expire dans 3 jours ou moins)
    abonnements_critiques = qs.filter(
        statut='ACTIF',
        date_expiration__gte=today,
        date_expiration__lte=today + timedelta(days=3)
    )
    
    # Statistiques par type de repas
    stats_type_repas = {
        'dejeuner': qs.filter(type_repas='DEJEUNER', statut='ACTIF').count(),
        'gouter': qs.filter(type_repas='GOUTER', statut='ACTIF').count(),
        'complet': qs.filter(type_repas='COMPLET', statut='ACTIF').count(),
    }
    stats_type_repas_rows = [
        {
            'code': code,
            'label': label,
            'nombre': qs.filter(type_repas=code, statut='ACTIF').count(),
        }
        for code, label in AbonnementCantine.TypeRepas.choices
    ]
    
    montants = totaux_cantine(qs)
    
    context = {
        'titre_page': 'Gestion Cantine Scolaire',
        'total': total,
        'actifs': actifs,
        'expires': expires,
        'suspendus': suspendus,
        'abonnements_expires': abonnements_expires[:10],
        'abonnements_proche_expiration': abonnements_proche_expiration[:10],
        'abonnements_critiques': abonnements_critiques[:10],
        'nb_expires': abonnements_expires.count(),
        'nb_proche_expiration': abonnements_proche_expiration.count(),
        'nb_critiques': abonnements_critiques.count(),
        'stats_type_repas': stats_type_repas,
        'stats_type_repas_rows': stats_type_repas_rows,
        'montant_abonnements': montants['du'],
        'deja_paye': montants['paye'],
        'reste_a_payer': montants['reste'],
        'recettes': recettes_par_periode(qs, CHAMP_DATE_CANTINE),
    }
    return render(request, 'bus/cantine/tableau_bord.html', context)


@login_required
def liste_abonnements_cantine(request):
    """Liste des abonnements cantine avec filtres et recherche"""
    qs = AbonnementCantine.objects.select_related('eleve', 'eleve__classe', 'eleve__classe__ecole')
    # IMPORTANT: Seul le superuser peut voir toutes les écoles
    if not user_is_superadmin(request.user):
        qs = filter_by_user_school(qs, request.user, 'eleve__classe__ecole')

    periodicite_rows = resumes_periodicites_cantine(qs)
    periodicite = (request.GET.get('periodicite') or '').strip()
    if periodicite not in dict(AbonnementCantine.Periodicite.choices):
        periodicite = ''
    # Recettes de l'école sur les périodes, indépendamment des filtres
    recettes = recettes_par_periode(qs, CHAMP_DATE_CANTINE)
    if periodicite:
        qs = qs.filter(periodicite=periodicite)
    periode = request.GET.get('periode') or ''
    if periode not in LIBELLES_PERIODES:
        periode = ''
    if periode:
        qs = filtrer_periode(qs, periode, CHAMP_DATE_CANTINE)

    # Recherche
    q = (request.GET.get('q') or '').strip()
    if q:
        qs = filtrer_termes(qs, q, (
            'eleve__nom', 'eleve__prenom', 'eleve__matricule',
            'eleve__classe__nom', 'reference_externe', 'contact_parent',
        ), ('eleve__nom', 'eleve__prenom'))
    
    # Filtres
    filtre = (request.GET.get('filtre') or '').strip().lower()
    if filtre == 'actif':
        qs = qs.filter(statut='ACTIF')
    elif filtre == 'expire':
        qs = qs.filter(statut='EXPIRE')
    elif filtre == 'suspendu':
        qs = qs.filter(statut='SUSPENDU')
    elif filtre == 'proche_expiration':
        today = timezone.localdate()
        date_limite = today + timedelta(days=7)
        qs = qs.filter(statut='ACTIF', date_expiration__lte=date_limite, date_expiration__gte=today)
    elif filtre == 'critique':
        today = timezone.localdate()
        date_limite = today + timedelta(days=3)
        qs = qs.filter(statut='ACTIF', date_expiration__lte=date_limite, date_expiration__gte=today)
    
    # Filtre par type de repas
    type_repas = request.GET.get('type_repas')
    if type_repas:
        qs = qs.filter(type_repas=type_repas)
    
    # Filtre par classe
    classe_id = request.GET.get('classe')
    if classe_id:
        qs = qs.filter(eleve__classe_id=classe_id)
    
    avec_remise = request.GET.get('remise') == 'oui'
    filtres = _abonnements_cantine_tries(qs, avec_remise)
    montants = totaux_cantine(filtres)
    page_obj = Paginator(filtres, 100).get_page(request.GET.get('page'))
    abonnements = page_obj.object_list
    page_params = request.GET.copy()
    page_params.pop('page', None)
    context = {
        'titre_page': 'Élèves abonnés à la cantine avec remise' if avec_remise else 'Liste des Abonnements Cantine',
        'abonnements': abonnements,
        'page_obj': page_obj,
        'page_query': page_params.urlencode(),
        'total_count': page_obj.paginator.count,
        'avec_remise': avec_remise,
        'periodicite': periodicite,
        'periodicite_libelle': dict(AbonnementCantine.Periodicite.choices).get(periodicite, ''),
        'periodicite_rows': periodicite_rows,
        'q': q,
        'filtre': filtre,
        'type_repas': type_repas,
        'type_repas_choices': AbonnementCantine.TypeRepas.choices,
        'periode': periode,
        'periode_libelle': LIBELLES_PERIODES.get(periode, ''),
        'recettes': recettes,
        'total_filtre': montants['paye'],
        'total_tarif': montants['brut'],
        'total_remise': montants['remise'],
        'total_du': montants['du'],
        'total_reste': montants['reste'],
    }
    return render(request, 'bus/cantine/liste.html', context)


@login_required
def creer_abonnement_cantine(request):
    """Créer un nouvel abonnement cantine"""
    ecole = None if user_is_superadmin(request.user) else user_school(request.user)
    initial = {}
    eleve_id = request.GET.get('eleve')
    if eleve_id:
        eleves = Eleve.objects.select_related('classe', 'responsable_principal')
        if ecole:
            eleves = eleves.filter(classe__ecole=ecole)
        try:
            eleve = eleves.get(pk=eleve_id)
            initial['eleve'] = eleve
            initial['classe'] = eleve.classe_id
            # Élève déjà abonné : reprendre ses informations et enchaîner les dates.
            initial.update(reprise_cantine(eleve))
        except (Eleve.DoesNotExist, TypeError, ValueError):
            pass
    if request.method == 'POST':
        form = AbonnementCantineForm(request.POST, ecole=ecole)
        if form.is_valid():
            abonnement = form.save()
            messages.success(request, f"Abonnement cantine créé pour {abonnement.eleve}")
            return redirect('bus:liste_abonnements_cantine')
    else:
        form = AbonnementCantineForm(initial=initial, ecole=ecole)
    
    context = {
        'titre_page': 'Nouvel Abonnement Cantine',
        'form': form,
    }
    return render(request, 'bus/cantine/form.html', context)


@login_required
@require_school_object(model=AbonnementCantine, pk_kwarg='pk', field_path='eleve__classe__ecole')
def modifier_abonnement_cantine(request, pk):
    """Modifier un abonnement cantine existant"""
    abonnement = get_object_or_404(AbonnementCantine, pk=pk)
    ecole = None if user_is_superadmin(request.user) else user_school(request.user)

    if request.method == 'POST':
        form = AbonnementCantineForm(request.POST, instance=abonnement, ecole=ecole)
        if form.is_valid():
            form.save()
            messages.success(request, f"Abonnement cantine modifié pour {abonnement.eleve}")
            return redirect('bus:liste_abonnements_cantine')
    else:
        form = AbonnementCantineForm(instance=abonnement, ecole=ecole)
    
    context = {
        'titre_page': 'Modifier Abonnement Cantine',
        'form': form,
        'abonnement': abonnement,
    }
    return render(request, 'bus/cantine/form.html', context)


@login_required
@can_delete_subscriptions
@require_school_object(model=AbonnementCantine, pk_kwarg='pk', field_path='eleve__classe__ecole')
def supprimer_abonnement_cantine(request, pk):
    """Supprimer définitivement un abonnement cantine"""
    abonnement = get_object_or_404(AbonnementCantine, pk=pk)
    
    if request.method == 'POST':
        eleve_nom = str(abonnement.eleve)
        abonnement.delete()
        messages.success(request, f"Abonnement cantine supprimé définitivement pour {eleve_nom}")
        return redirect('bus:liste_abonnements_cantine')
    
    context = {
        'titre_page': 'Supprimer Abonnement Cantine',
        'abonnement': abonnement,
    }
    return render(request, 'bus/cantine/confirmer_suppression.html', context)


def _export_cantine(request, fmt):
    """Exporte chaque abonnement visible avec les filtres de la liste."""
    qs = AbonnementCantine.objects.select_related('eleve', 'eleve__classe', 'eleve__classe__ecole')
    if not user_is_superadmin(request.user):
        qs = filter_by_user_school(qs, request.user, 'eleve__classe__ecole')
    periodicite = (request.GET.get('periodicite') or '').strip()
    if periodicite in dict(AbonnementCantine.Periodicite.choices):
        qs = qs.filter(periodicite=periodicite)
    periode = request.GET.get('periode') or ''
    if periode in LIBELLES_PERIODES:
        qs = filtrer_periode(qs, periode, CHAMP_DATE_CANTINE)
    q = (request.GET.get('q') or '').strip()
    if q:
        qs = filtrer_termes(qs, q, (
            'eleve__nom', 'eleve__prenom', 'eleve__matricule',
            'eleve__classe__nom', 'reference_externe', 'contact_parent',
        ), ('eleve__nom', 'eleve__prenom'))
    if request.GET.get('type_repas'):
        qs = qs.filter(type_repas=request.GET['type_repas'])
    if (request.GET.get('classe') or '').isdigit():
        qs = qs.filter(eleve__classe_id=int(request.GET['classe']))
    filtre = request.GET.get('filtre', '').strip().lower()
    if filtre == 'actif':
        qs = qs.filter(statut='ACTIF')
    elif filtre == 'expire':
        qs = qs.filter(statut='EXPIRE')
    elif filtre == 'suspendu':
        qs = qs.filter(statut='SUSPENDU')
    elif filtre == 'proche_expiration':
        today = timezone.localdate()
        date_limite = today + timedelta(days=7)
        qs = qs.filter(statut='ACTIF', date_expiration__lte=date_limite, date_expiration__gte=today)
    elif filtre == 'critique':
        today = timezone.localdate()
        date_limite = today + timedelta(days=3)
        qs = qs.filter(statut='ACTIF', date_expiration__lte=date_limite, date_expiration__gte=today)
    abonnements = _abonnements_cantine_tries(qs, request.GET.get('remise') == 'oui')
    entetes_excel = [
        'École', 'Élève', 'Matricule', 'Classe', 'Type repas', 'Périodicité',
        'Montant abonnement (GNF)', 'Type remise', 'Valeur remise', 'Remise (GNF)',
        'Dû net (GNF)', 'Déjà payé (GNF)', 'Reste à payer (GNF)',
        'Date début', 'Date expiration', 'Jours restants', 'Statut',
        'Référence externe', 'Régime alimentaire', 'Allergies', 'Contact parent',
        'Observations',
    ]
    entetes_pdf = [
        'Élève', 'Matricule', 'Classe', 'Repas', 'Périodicité', 'Montant',
        'Remise', 'Dû net', 'Déjà payé', 'Reste', 'Début', 'Fin', 'Référence',
    ]
    lignes_excel, lignes_pdf = [], []
    for abo in abonnements:
        eleve = abo.eleve
        nom = f'{eleve.prenom} {eleve.nom}'
        classe = eleve.classe.nom if eleve.classe_id else ''
        ecole = eleve.classe.ecole.nom if eleve.classe_id else ''
        lignes_excel.append([
            ecole, nom, eleve.matricule or '', classe,
            abo.get_type_repas_display(), abo.get_periodicite_display(),
            abo.montant_brut, abo.get_type_remise_display(), abo.valeur_remise,
            abo.montant_remise, abo.montant_du, abo.deja_paye, abo.reste_a_payer,
            abo.date_debut, abo.date_expiration, abo.jours_restants,
            abo.get_statut_display(), abo.reference_externe or '',
            abo.regime_alimentaire or '', abo.allergies or '',
            abo.contact_parent or '', abo.observations or '',
        ])
        lignes_pdf.append([
            nom, eleve.matricule or '', classe, abo.get_type_repas_display(),
            abo.get_periodicite_display(), abo.montant_brut,
            abo.montant_remise, abo.montant_du, abo.deja_paye,
            abo.reste_a_payer, abo.date_debut, abo.date_expiration,
            abo.reference_externe or '',
        ])
    return exporter_liste(
        fmt, 'abonnements_cantine', 'Abonnements cantine',
        entetes_excel, lignes_excel, entetes_pdf, lignes_pdf,
        sum((abo.montant or 0 for abo in abonnements), 0),
    )


@login_required
def export_cantine_excel(request):
    return _export_cantine(request, 'excel')


@login_required
def export_cantine_pdf(request):
    return _export_cantine(request, 'pdf')


@login_required
def alertes_cantine_json(request):
    """API JSON pour récupérer les alertes cantine (pour dashboard)"""
    qs = AbonnementCantine.objects.select_related('eleve', 'eleve__classe')
    # IMPORTANT: Seul le superuser peut voir toutes les écoles
    if not user_is_superadmin(request.user):
        qs = filter_by_user_school(qs, request.user, 'eleve__classe__ecole')
    
    # Abonnements expirés
    expires = [a for a in qs.filter(statut='ACTIF') if a.est_expire]
    
    # Abonnements proches de l'expiration
    proche_expiration = [a for a in qs.filter(statut='ACTIF') if a.est_proche_expiration and not a.est_expire]
    
    # Abonnements critiques (3 jours ou moins)
    critiques = [a for a in proche_expiration if a.jours_restants <= 3]
    
    data = {
        'total': qs.count(),
        'actifs': qs.filter(statut='ACTIF').count(),
        'expires': {
            'count': len(expires),
            'abonnements': [
                {
                    'id': a.id,
                    'eleve': str(a.eleve),
                    'classe': a.eleve.classe.nom if a.eleve.classe else '',
                    'date_expiration': a.date_expiration.strftime('%d/%m/%Y'),
                    'jours_restants': a.jours_restants,
                }
                for a in expires[:10]  # Limiter à 10
            ]
        },
        'proche_expiration': {
            'count': len(proche_expiration),
            'abonnements': [
                {
                    'id': a.id,
                    'eleve': str(a.eleve),
                    'classe': a.eleve.classe.nom if a.eleve.classe else '',
                    'date_expiration': a.date_expiration.strftime('%d/%m/%Y'),
                    'jours_restants': a.jours_restants,
                }
                for a in proche_expiration[:10]
            ]
        },
        'critiques': {
            'count': len(critiques),
            'abonnements': [
                {
                    'id': a.id,
                    'eleve': str(a.eleve),
                    'classe': a.eleve.classe.nom if a.eleve.classe else '',
                    'date_expiration': a.date_expiration.strftime('%d/%m/%Y'),
                    'jours_restants': a.jours_restants,
                }
                for a in critiques
            ]
        }
    }
    
    return JsonResponse(data)


@login_required
def get_eleve_info_json(request, eleve_id):
    """API JSON pour récupérer les informations d'un élève"""
    import os
    
    try:
        eleve = Eleve.objects.select_related('classe', 'responsable_principal').get(pk=eleve_id)
        
        # Vérifier les permissions - seul le superuser peut voir toutes les écoles
        if not user_is_superadmin(request.user):
            eleves_qs = filter_by_user_school(Eleve.objects.all(), request.user, 'classe__ecole')
            if not eleves_qs.filter(pk=eleve_id).exists():
                return JsonResponse({'error': 'Permission refusée'}, status=403)
        
        # Gérer la photo de manière sécurisée
        photo_url = None
        if eleve.photo:
            try:
                # Vérifier que le fichier existe physiquement
                if hasattr(eleve.photo, 'path') and os.path.exists(eleve.photo.path):
                    photo_url = eleve.photo.url
                elif hasattr(eleve.photo, 'url'):
                    # Si pas de path (stockage distant), utiliser l'URL directement
                    photo_url = eleve.photo.url
            except Exception as e:
                # En cas d'erreur, on ignore simplement la photo
                print(f"Erreur lors de la récupération de la photo pour l'élève {eleve_id}: {e}")
                photo_url = None
        
        data = {
            'success': True,
            'nom_complet': f"{eleve.nom} {eleve.prenom}",
            'nom': eleve.nom,
            'prenom': eleve.prenom,
            'matricule': eleve.matricule or '',
            'classe': eleve.classe.nom if eleve.classe else '',
            'classe_id': eleve.classe.id if eleve.classe else None,
            'telephone_parent': eleve.responsable_principal.telephone if eleve.responsable_principal else '',
            'email_parent': eleve.responsable_principal.email if eleve.responsable_principal else '',
            'photo_url': photo_url,
        }
        data.update(resume_reprise_json(eleve))
        return JsonResponse(data)
    except Eleve.DoesNotExist:
        return JsonResponse({'error': 'Élève non trouvé'}, status=404)


@login_required
@require_school_object(model=AbonnementCantine, pk_kwarg='abo_id', field_path='eleve__classe__ecole')
def generer_recu_cantine_pdf(request, abo_id):
    """Génère un reçu PDF pour un abonnement cantine"""
    abo = get_object_or_404(
        AbonnementCantine.objects.select_related('eleve', 'eleve__classe', 'eleve__classe__ecole'), 
        id=abo_id
    )
    
    try:
        from reportlab.pdfgen import canvas
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.utils import ImageReader
        from django.contrib.staticfiles import finders
        import os
    except Exception:
        messages.error(request, "ReportLab requis (pip install reportlab)")
        return redirect('bus:cantine_liste')
    
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    
    # Filigrane
    try:
        from ecole_moderne.pdf_utils import draw_logo_watermark
        ecole_obj = getattr(getattr(abo.eleve, 'classe', None), 'ecole', None)
        draw_logo_watermark(c, width, height, opacity=0.04, rotate=30, scale=1.5, ecole=ecole_obj)
    except Exception:
        pass
    
    # Logo de l'école en haut à gauche
    try:
        logo_path = None
        ecole_obj = getattr(getattr(abo.eleve, 'classe', None), 'ecole', None)
        
        if ecole_obj and hasattr(ecole_obj, 'logo'):
            school_logo_path = getattr(getattr(ecole_obj, 'logo', None), 'path', None)
            if school_logo_path and os.path.exists(school_logo_path):
                logo_path = school_logo_path
        
        if not logo_path:
            logo_path = finders.find('logos/logo.png')
        
        if logo_path:
            try:
                logo_img = ImageReader(logo_path)
                logo_w, logo_h = 60, 60
                c.drawImage(logo_img, 40, height - 100, width=logo_w, height=logo_h, preserveAspectRatio=True, mask='auto')
            except Exception:
                pass
    except Exception:
        pass
    
    # Titre
    c.setFont('Helvetica-Bold', 18)
    title = 'REÇU ABONNEMENT CANTINE SCOLAIRE'
    tw = c.stringWidth(title, 'Helvetica-Bold', 18)
    c.drawString((width - tw)/2, height - 50, title)
    
    # Nom de l'école sous le titre
    try:
        ecole_nom = getattr(ecole_obj, 'nom', '')
        if ecole_nom:
            c.setFont('Helvetica-Bold', 12)
            tw_ecole = c.stringWidth(ecole_nom, 'Helvetica-Bold', 12)
            c.drawString((width - tw_ecole)/2, height - 70, ecole_nom)
    except Exception:
        pass
    
    # Photo de l'élève en haut à droite
    try:
        img_drawn = False
        img_w, img_h = 100, 100
        x_img = width - 40 - img_w
        y_img = height - 40 - img_h
        
        el = abo.eleve
        photo_path = getattr(getattr(el, 'photo', None), 'path', None)
        
        if photo_path and os.path.exists(photo_path):
            try:
                img = ImageReader(photo_path)
                c.drawImage(img, x_img, y_img, width=img_w, height=img_h, preserveAspectRatio=True, mask='auto')
                img_drawn = True
            except Exception:
                img_drawn = False
        
        if not img_drawn:
            # Dessiner un placeholder avec initiales
            nom_complet = f"{getattr(el, 'prenom', '')} {getattr(el, 'nom', '')}".strip()
            initiales = ''.join([p[0].upper() for p in nom_complet.split()[:2]]) or 'E'
            c.setLineWidth(1)
            try:
                c.roundRect(x_img, y_img, img_w, img_h, 8)
            except Exception:
                c.rect(x_img, y_img, img_w, img_h)
            c.setFont('Helvetica-Bold', 24)
            c.drawCentredString(x_img + img_w/2, y_img + img_h/2 - 8, initiales)
            c.setFont('Helvetica', 8)
            c.drawCentredString(x_img + img_w/2, y_img + 6, "Pas de photo")
        
        # Afficher le nom de l'élève sous l'image/placeholder
        try:
            nom_aff = f"{getattr(el, 'prenom', '')} {getattr(el, 'nom', '')}".strip()
            if nom_aff:
                c.setFont('Helvetica-Bold', 10)
                c.drawCentredString(x_img + img_w/2, y_img - 12, nom_aff)
        except Exception:
            pass
    except Exception:
        pass
    
    # Corps
    y = height - 110
    c.setFont('Helvetica', 12)
    
    def line(lbl, val):
        nonlocal y
        c.setFont('Helvetica-Bold', 12)
        c.drawString(40, y, f"{lbl} :")
        c.setFont('Helvetica', 12)
        c.drawString(200, y, str(val))
        y -= 20
    
    el = abo.eleve
    line('Élève', f"{el.prenom} {el.nom} ({el.matricule})")
    line('Classe', getattr(el.classe, 'nom', ''))
    line('École', getattr(getattr(el.classe, 'ecole', None), 'nom', ''))
    line('Type de repas', abo.get_type_repas_display())
    line('Périodicité', abo.get_periodicite_display())
    line('Tarif', f"{int(abo.montant_brut):,}".replace(',', ' ') + ' GNF')
    line('Remise', f"{int(abo.montant_remise):,}".replace(',', ' ') + ' GNF')
    line('Dû net', f"{int(abo.montant_du):,}".replace(',', ' ') + ' GNF')
    line('Encaissé', f"{int(abo.montant):,}".replace(',', ' ') + ' GNF')
    line('Reste', f"{int(abo.reste_a_payer):,}".replace(',', ' ') + ' GNF')
    line('Début', abo.date_debut.strftime('%d/%m/%Y') if abo.date_debut else '')
    line('Expiration', abo.date_expiration.strftime('%d/%m/%Y') if abo.date_expiration else '')
    
    # Calcul et affichage de la durée en jours
    if abo.date_debut and abo.date_expiration:
        duree_jours = (abo.date_expiration - abo.date_debut).days
        line('Durée', f"{duree_jours} jours")
    
    # Informations spécifiques à la cantine
    if abo.regime_alimentaire:
        line('Régime alimentaire', abo.regime_alimentaire)
    if abo.allergies:
        line('Allergies', abo.allergies)
    if abo.contact_parent:
        line('Contact parent', abo.contact_parent)
    
    # Pied de page avec date d'émission
    y -= 30
    c.setFont('Helvetica', 9)
    c.setFillGray(0.5)
    c.drawString(40, y, f"Reçu émis le {timezone.localdate().strftime('%d/%m/%Y')}")
    c.setFillGray(0)
    
    # Signature
    y -= 40
    c.setFont('Helvetica-Bold', 10)
    c.drawString(width - 200, y, "Signature et cachet")
    c.line(width - 200, y - 5, width - 40, y - 5)
    
    c.showPage()
    c.save()
    
    pdf = buffer.getvalue()
    buffer.close()
    
    resp = HttpResponse(content_type='application/pdf')
    resp['Content-Disposition'] = f'inline; filename=recu_cantine_{abo.id}.pdf'
    resp.write(pdf)
    return resp
