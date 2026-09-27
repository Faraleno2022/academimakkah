"""Recettes des abonnements par période : aujourd'hui, cette semaine, ce mois, cette année.

Un abonnement compte dans la période où il a été payé :
- bus : ``date_debut`` est la date du paiement saisie au guichet ;
- cantine : date d'enregistrement (``created_at``), car ``date_debut`` peut être
  postérieure au paiement quand un réabonnement démarre à la fin du précédent.
"""

from datetime import timedelta
from decimal import Decimal

from django.db.models import Count, Sum
from django.utils import timezone


CHAMP_DATE_BUS = 'date_debut'
CHAMP_DATE_CANTINE = 'created_at__date'

PERIODES = (
    ('jour', "Aujourd'hui", 'fa-calendar-day', 'primary'),
    ('semaine', "Cette semaine", 'fa-calendar-week', 'info'),
    ('mois', "Ce mois", 'fa-calendar-alt', 'success'),
    ('annee', "Cette année", 'fa-calendar', 'dark'),
)
LIBELLES_PERIODES = {code: libelle for code, libelle, _icone, _couleur in PERIODES}

MOIS_FR = (
    "", "janvier", "février", "mars", "avril", "mai", "juin", "juillet",
    "août", "septembre", "octobre", "novembre", "décembre",
)


def debut_periode(code, today=None):
    """Premier jour de la période (la semaine commence le lundi)."""
    today = today or timezone.localdate()
    if code == 'jour':
        return today
    if code == 'semaine':
        return today - timedelta(days=today.weekday())
    if code == 'mois':
        return today.replace(day=1)
    if code == 'annee':
        return today.replace(month=1, day=1)
    return None


def precision_periode(code, today=None):
    """Texte court affiché sous le libellé : « 27/09/2026 », « depuis le lun. 21/09 »..."""
    today = today or timezone.localdate()
    debut = debut_periode(code, today)
    if code == 'jour':
        return today.strftime('%d/%m/%Y')
    if code == 'semaine':
        return f"du {debut.strftime('%d/%m')} au {today.strftime('%d/%m/%Y')}"
    if code == 'mois':
        return f"{MOIS_FR[today.month]} {today.year}"
    if code == 'annee':
        return str(today.year)
    return ''


def filtrer_periode(qs, code, champ_date, today=None):
    """Abonnements payés depuis le début de la période ; ``qs`` inchangé si le code est inconnu."""
    debut = debut_periode(code, today)
    if not debut:
        return qs
    today = today or timezone.localdate()
    return qs.filter(**{f'{champ_date}__gte': debut, f'{champ_date}__lte': today})


def recettes_par_periode(qs, champ_date, today=None):
    """Nombre d'abonnements, d'élèves et montant encaissé pour chaque période."""
    today = today or timezone.localdate()
    lignes = []
    for code, libelle, icone, couleur in PERIODES:
        agregat = filtrer_periode(qs, code, champ_date, today).aggregate(
            nb=Count('id'), nb_eleves=Count('eleve', distinct=True), montant=Sum('montant'),
        )
        lignes.append({
            'code': code,
            'libelle': libelle,
            'precision': precision_periode(code, today),
            'icone': icone,
            'couleur': couleur,
            'nb': agregat['nb'] or 0,
            'nb_eleves': agregat['nb_eleves'] or 0,
            'montant': agregat['montant'] or Decimal('0'),
        })
    return lignes
