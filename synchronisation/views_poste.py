"""Page « Connexion au serveur » de l'application installée sur un poste."""
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden
from django.shortcuts import redirect, render

from ecole_moderne.sauvegarde_views import _est_desktop

from .liaison import LiaisonImpossible, apercu_liaison, config_actuelle, lier_poste

SERVEUR_PAR_DEFAUT = 'https://www.myschoolgn.space'


def _peut_configurer(user):
    if user.is_superuser or user.is_staff:
        return True
    profil = getattr(user, 'profil', None)
    return getattr(profil, 'role', '') == 'ADMIN'


def _etat_synchronisation(config):
    from .autosync import _pret
    from .models import SyncChange, SyncCheckpoint

    etat = {'active': _pret(config), 'serveur': config.get('server_url') or ''}
    if etat['active']:
        point = SyncCheckpoint.objects.filter(device_id=config['device_id']).first()
        etat.update({
            'poste': str(config['device_id'])[:8],
            'initial_termine': bool(point and point.initial_complete),
            'en_attente': SyncChange.objects.filter(statut=SyncChange.STATUT_PENDING).count(),
            'en_echec': SyncChange.objects.filter(statut=SyncChange.STATUT_FAILED).count(),
        })
    return etat


@login_required
def connexion_serveur(request):
    if not _est_desktop():
        return HttpResponseForbidden("Cette page n'existe que dans l'application installée sur un poste.")
    if not _peut_configurer(request.user):
        return HttpResponseForbidden("Seul un administrateur peut connecter ce poste au serveur.")

    config = config_actuelle()
    source = request.POST if request.method == 'POST' else request.GET
    serveur = (source.get('serveur') or config.get('server_url')
               or getattr(settings, 'MYSCHOOL_SYNC_SERVER_URL', '') or SERVEUR_PAR_DEFAUT)
    code = (source.get('code') or '').strip().upper()
    apercu = None
    erreur = None

    if request.method == 'POST':
        try:
            resultat = lier_poste(serveur, code)
        except LiaisonImpossible as exc:
            erreur = str(exc)
        else:
            messages.success(
                request,
                f"Poste connecté à « {resultat['ecole_nom']} ». La synchronisation démarre "
                "dans quelques secondes ; la première peut prendre plusieurs minutes.",
            )
            return redirect('synchronisation_connexion_serveur')
    elif code:
        try:
            apercu = apercu_liaison(serveur, code)
        except LiaisonImpossible as exc:
            erreur = str(exc)

    return render(request, 'synchronisation/connexion_serveur.html', {
        'titre_page': 'Connexion au serveur',
        'serveur': serveur,
        'code': code,
        'apercu': apercu,
        'erreur': erreur,
        'etat': _etat_synchronisation(config_actuelle()),
    })
