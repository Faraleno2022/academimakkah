"""Historique des abonnements d'un élève : liste, exports PDF / Excel et carnet."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify

from eleves.models import Classe, Eleve
from utilisateurs.utils import filter_by_user_school, user_is_superadmin

from . import historique_eleve as historique


def _perimetre(request):
    eleves = Eleve.objects.select_related('classe', 'classe__ecole', 'responsable_principal')
    classes = Classe.objects.select_related('ecole')
    if not user_is_superadmin(request.user):
        eleves = filter_by_user_school(eleves, request.user, 'classe__ecole')
        classes = filter_by_user_school(classes, request.user, 'ecole')
    return eleves, classes


def _entier(valeur):
    try:
        return int(valeur)
    except (TypeError, ValueError):
        return None


def _selection(request):
    """Élève et classe demandés, limités à l'école de l'utilisateur."""
    eleves, classes = _perimetre(request)
    type_abonnement = request.GET.get('type', 'tous')
    if type_abonnement not in historique.TYPES_HISTORIQUE:
        type_abonnement = 'tous'
    eleve = eleves.filter(pk=_entier(request.GET.get('eleve'))).first()
    classe = classes.filter(pk=_entier(request.GET.get('classe'))).first()
    if eleve and not classe:
        classe = eleve.classe
    return eleves, classes, eleve, classe, type_abonnement


@login_required
def historique_abonnements(request):
    eleves, classes, eleve, classe, type_abonnement = _selection(request)
    lignes = historique.lignes_abonnements(eleve, type_abonnement) if eleve else []
    return render(request, 'bus/historique_eleve.html', {
        'titre_page': "Abonnements d'un élève",
        'classes': classes.order_by('ecole__nom', 'nom'),
        'eleves': eleves.order_by('classe__nom', 'nom', 'prenom'),
        'eleve': eleve,
        'classe': classe,
        'type_abonnement': type_abonnement,
        'types': historique.TYPES_HISTORIQUE,
        'lignes': lignes,
        'resume': historique.resume_abonnements(lignes),
    })


def _blocs_classe(eleves, classe, type_abonnement):
    """Élèves de la classe ayant au moins un abonnement du type demandé."""
    blocs = []
    for eleve in eleves.filter(classe=classe).order_by('nom', 'prenom'):
        lignes = historique.lignes_abonnements(eleve, type_abonnement)
        if lignes:
            blocs.append((eleve, lignes))
    return blocs


@login_required
def export_historique_abonnements(request, fmt):
    """``fmt`` : ``pdf`` (liste), ``excel`` ou ``carnet``.

    Avec un élève : son historique. Avec seulement une classe : tous les élèves
    abonnés de la classe (un bloc ou une page par élève).
    """
    eleves, _classes, eleve, classe, type_abonnement = _selection(request)
    if fmt not in ('pdf', 'excel', 'carnet'):
        return redirect('bus:historique_abonnements')
    if eleve:
        blocs = [(eleve, historique.lignes_abonnements(eleve, type_abonnement))]
        suffixe = slugify(eleve.matricule or '') or f"eleve_{eleve.pk}"
    elif classe:
        blocs = _blocs_classe(eleves, classe, type_abonnement)
        suffixe = f"classe_{classe.pk}"
    else:
        blocs = []
    if not blocs:
        messages.warning(request, "Sélectionnez un élève ou une classe ayant des abonnements.")
        return redirect(f"{reverse('bus:historique_abonnements')}?{request.GET.urlencode()}")

    ecole = getattr(blocs[0][0].classe, 'ecole', None)
    horodatage = timezone.localdate().strftime('%Y%m%d')
    if fmt == 'excel':
        titre = f"Abonnements — {historique.TYPES_HISTORIQUE[type_abonnement]}"
        contenu = historique.export_excel(blocs, titre)
        response = HttpResponse(
            contenu,
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        response['Content-Disposition'] = f'attachment; filename="abonnements_{suffixe}_{horodatage}.xlsx"'
        return response

    if fmt == 'carnet':
        contenu = historique.pdf_carnet(blocs, ecole)
        nom = f"carnet_abonnement_{suffixe}_{horodatage}.pdf"
    else:
        contenu = historique.pdf_liste(blocs, ecole)
        nom = f"abonnements_{suffixe}_{horodatage}.pdf"
    response = HttpResponse(contenu, content_type='application/pdf')
    response['Content-Disposition'] = f'inline; filename="{nom}"'
    return response
