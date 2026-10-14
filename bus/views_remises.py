"""Élèves bénéficiant d'une remise sur l'abonnement bus : liste, saisie et export Excel."""
from io import BytesIO

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from utilisateurs.permissions import can_delete_subscriptions
from utilisateurs.utils import filter_by_user_school, user_is_superadmin, user_school

from .forms import RemiseBusForm
from .models import GrilleTarifaireBus, RemiseBus
from .remises import lignes_remises, totaux_remises
from .annees import filtrer_annee_abonnements

ENTETES_EXCEL = [
    'N°', 'Élève', 'Matricule', 'Classe', 'Grille / zone', 'Année', 'Remise', 'Motif',
    'Tranches concernées', 'Tarif (GNF)', 'Remise (GNF)', 'Net dû (GNF)', 'Payé (GNF)', 'Reste (GNF)',
]


def _ecole(user):
    return None if user_is_superadmin(user) else user_school(user)


def _remises_visibles(user):
    remises = RemiseBus.objects.all()
    if not user_is_superadmin(user):
        remises = filter_by_user_school(remises, user, 'grille__ecole')
    return remises


def _grilles_visibles(user):
    grilles = GrilleTarifaireBus.objects.all()
    if not user_is_superadmin(user):
        grilles = filter_by_user_school(grilles, user, 'ecole')
    return grilles.order_by('-annee_scolaire', 'zone')


def _filtrer(request):
    remises, annees = filtrer_annee_abonnements(request, _remises_visibles(request.user), 'grille__annee_scolaire')
    q = (request.GET.get('q') or '').strip()
    grille_id = request.GET.get('grille') or ''
    if q:
        remises = remises.filter(
            Q(eleve__nom__icontains=q) | Q(eleve__prenom__icontains=q)
            | Q(eleve__matricule__icontains=q) | Q(eleve__classe__nom__icontains=q)
        )
    if grille_id.isdigit():
        remises = remises.filter(grille_id=int(grille_id))
    return remises, q, grille_id, annees


@login_required
def liste_remises(request):
    remises, q, grille_id, annees = _filtrer(request)
    lignes = lignes_remises(remises)
    if request.GET.get('export') == 'excel':
        return _export_excel(lignes)
    return render(request, 'bus/remises/liste.html', {
        **annees,
        'titre_page': "Élèves avec remise — bus",
        'lignes': lignes,
        'totaux': totaux_remises(lignes),
        'q': q,
        'grille_id': grille_id,
        'grilles': _grilles_visibles(request.user),
        'filtres_url': request.GET.urlencode(),
    })


@login_required
def remise_form(request, remise_id=None):
    ecole = _ecole(request.user)
    if not user_is_superadmin(request.user) and ecole is None:
        raise PermissionDenied("Aucune école n'est associée à votre compte.")
    remise = get_object_or_404(_remises_visibles(request.user), pk=remise_id) if remise_id else None
    initial = {}
    for champ in ('eleve', 'grille'):
        valeur = request.GET.get(champ)
        if valeur and valeur.isdigit():
            initial[champ] = int(valeur)
    form = RemiseBusForm(request.POST or None, instance=remise, initial=initial, ecole=ecole)
    if request.method == 'POST' and form.is_valid():
        objet = form.save(commit=False)
        if not objet.pk:
            objet.cree_par = request.user
        objet.save()
        messages.success(
            request,
            f"Remise {objet.libelle_valeur} enregistrée pour {objet.eleve.prenom} {objet.eleve.nom} "
            f"({objet.montant_total():,.0f} GNF sur l'année).".replace(',', ' '),
        )
        return redirect('bus:remises')
    return render(request, 'bus/remises/form.html', {
        'titre_page': "Modifier la remise bus" if remise else "Nouvelle remise bus",
        'form': form,
        'remise': remise,
    })


@login_required
@can_delete_subscriptions
@require_POST
def supprimer_remise(request, remise_id):
    remise = get_object_or_404(_remises_visibles(request.user), pk=remise_id)
    nom = f"{remise.eleve.prenom} {remise.eleve.nom}"
    remise.delete()
    messages.success(request, f"Remise retirée pour {nom} : le tarif de la grille s'applique de nouveau.")
    return redirect('bus:remises')


def _export_excel(lignes):
    classeur = Workbook()
    feuille = classeur.active
    feuille.title = 'Remises bus'
    feuille.append(["ÉLÈVES BÉNÉFICIANT D'UNE REMISE — BUS SCOLAIRE"])
    feuille['A1'].font = Font(bold=True, size=13)
    feuille.append([f"Édité le {timezone.localdate():%d/%m/%Y} — {len(lignes)} élève(s)"])
    feuille.append([])
    feuille.append(ENTETES_EXCEL)
    entete = feuille.max_row
    for cellule in feuille[entete]:
        cellule.font = Font(bold=True, color='FFFFFF')
        cellule.fill = PatternFill('solid', fgColor='1F4E78')
        cellule.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
    for numero, ligne in enumerate(lignes, 1):
        remise = ligne['remise']
        feuille.append([
            numero, f"{ligne['eleve'].prenom} {ligne['eleve'].nom}".strip(), ligne['eleve'].matricule or '',
            ligne['classe'], ligne['grille'].zone, ligne['grille'].annee_scolaire, remise.libelle_valeur,
            remise.get_motif_display(), remise.libelle_tranches,
            int(ligne['tarif']), int(ligne['montant_remise']), int(ligne['net']), int(ligne['paye']), int(ligne['reste']),
        ])
    totaux = totaux_remises(lignes)
    feuille.append(['', 'TOTAL', '', '', '', '', '', '', '', *(int(totaux[c]) for c in (
        'tarif', 'montant_remise', 'net', 'paye', 'reste'))])
    for cellule in feuille[feuille.max_row]:
        cellule.font = Font(bold=True)
    for colonne in range(10, 15):
        for (cellule,) in feuille.iter_rows(min_row=entete + 1, min_col=colonne, max_col=colonne):
            cellule.number_format = '#,##0'
    largeurs = [5, 28, 14, 14, 18, 11, 12, 20, 22, 13, 13, 13, 13, 13]
    for index, largeur in enumerate(largeurs, 1):
        feuille.column_dimensions[get_column_letter(index)].width = largeur
    feuille.freeze_panes = feuille.cell(row=entete + 1, column=1)
    sortie = BytesIO()
    classeur.save(sortie)
    reponse = HttpResponse(
        sortie.getvalue(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )
    reponse['Content-Disposition'] = f'attachment; filename="remises_bus_{timezone.localdate():%Y%m%d}.xlsx"'
    return reponse
