"""Dates de fin calculées à partir du premier jour de l'abonnement."""

from calendar import monthrange
from datetime import timedelta


def ajouter_mois(jour, mois):
    index = jour.year * 12 + jour.month - 1 + mois
    annee, mois_index = divmod(index, 12)
    return jour.replace(year=annee, month=mois_index + 1,
                        day=min(jour.day, monthrange(annee, mois_index + 1)[1]))


def fin_abonnement(debut, periodicite, service):
    if service == 'bus':
        if periodicite == 'MENSUEL':
            mois = 1
        else:
            from .models import PORTEES_PAIEMENT_BUS
            mois = 3 * len(PORTEES_PAIEMENT_BUS.get(periodicite, {'T1': 1}))
        return ajouter_mois(debut, mois) - timedelta(days=1)

    if periodicite == 'JOURNALIER':
        return debut
    if periodicite == 'HEBDOMADAIRE':
        return debut + timedelta(days=6)
    mois = {
        'MENSUEL': 1, 'TRIMESTRIEL': 3,
        'DEUX_TRIMESTRES': 6, 'TROIS_TRIMESTRES': 9,
        'ANNUEL': 12,
    }.get(periodicite, 1)
    return ajouter_mois(debut, mois) - timedelta(days=1)
