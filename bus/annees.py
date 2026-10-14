"""Année des abonnements indépendante de la classe actuelle de l'élève."""
from datetime import date

from django.conf import settings
from django.db.models import Q

from eleves.models import Classe
from eleves.utils_annee import get_annee_active
from utilisateurs.utils import user_is_superadmin, user_school


def annee_depuis_date(debut):
    if not debut:
        return ''
    if isinstance(debut, str):
        debut = date.fromisoformat(debut)
    mois = getattr(settings, 'ANNEE_SCOLAIRE_DEBUT_MOIS', 9)
    annee = debut.year - (debut.month < mois)
    return f'{annee}-{annee + 1}'


def filtrer_annee_abonnements(request, qs, champ='annee_scolaire'):
    """Même sélection pour listes, totaux, alertes et exports.

    Les anciennes lignes sans année (notamment reçues d'un ancien poste) sont
    classées par leur date de début, jamais par la nouvelle classe de l'élève.
    """
    ecole = user_school(request.user)
    classes = Classe.objects.all()
    if not user_is_superadmin(request.user):
        classes = classes.filter(ecole=ecole)
    annees = set(classes.values_list('annee_scolaire', flat=True).distinct())
    annees.update(qs.order_by().values_list(champ, flat=True).distinct())
    annees.discard('')
    annees.discard(None)
    choix = sorted(annees, reverse=True)
    annee = (request.GET.get('annee') or '').strip()
    if not annee:
        annee = (get_annee_active(request, ecole)
                 or ('toutes' if user_is_superadmin(request.user) else
                     choix[0] if choix else 'toutes'))
    if annee != 'toutes':
        if annee not in annees:
            qs = qs.none()
        else:
            condition = Q(**{champ: annee})
            if champ == 'annee_scolaire':
                try:
                    debut, fin = map(int, annee.split('-'))
                    mois = getattr(settings, 'ANNEE_SCOLAIRE_DEBUT_MOIS', 9)
                    condition |= Q(annee_scolaire='', date_debut__gte=date(debut, mois, 1),
                                   date_debut__lt=date(fin, mois, 1))
                except (ValueError, TypeError):
                    pass
            qs = qs.filter(condition)
    return qs, {'annee_abonnements': annee, 'annees_abonnements': choix}
