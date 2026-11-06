"""
Utilitaire pour la gestion de l'année scolaire active.
Centralise la logique de récupération de l'année active depuis la session.
"""
from datetime import date

from django.conf import settings

from .models import Classe

SESSION_ANNEE_ACTIVE = 'annee_scolaire_active'


def get_annee_active(request, ecole):
    """Retourne l'année scolaire active (session ou la plus récente).

    L'année est déterminée par :
    1. La valeur stockée en session (si elle correspond à une année existante)
    2. L'année la plus récente pour l'école donnée
    """
    if not ecole:
        return None
    annees = list(
        Classe.objects
        .filter(ecole=ecole)
        .values_list('annee_scolaire', flat=True)
        .distinct()
        .order_by('-annee_scolaire')
    )
    if not annees:
        return None
    annee_session = request.session.get(SESSION_ANNEE_ACTIVE)
    if annee_session and annee_session in annees:
        return annee_session
    return annees[0]


def bornes_annee_scolaire(annee):
    """Du 1er septembre au 31 août, comme les périodes de paie
    (``PeriodeSalaire.annee_scolaire``). None si l'année est mal formée."""
    try:
        debut = int(str(annee).split('-')[0])
    except (TypeError, ValueError):
        return None
    return date(debut, 9, 1), date(debut + 1, 8, 31)


def bornes_annee_active(request):
    """Bornes de l'année active de l'utilisateur ; None sans école
    (super-administrateur), qui garde alors toutes les années."""
    from utilisateurs.utils import user_school
    annee = get_annee_active(request, user_school(request.user))
    return bornes_annee_scolaire(annee) if annee else None


def filtrer_par_annee_active(qs, request, champ_date):
    """Restreint ``qs`` aux enregistrements datés dans l'année scolaire active :
    les recouvrements, emprunts et salaires d'une année y restent rattachés
    après la création de la suivante. Pour un DateTimeField, passer
    ``'champ__date'``."""
    bornes = bornes_annee_active(request)
    return qs.filter(**{f'{champ_date}__range': bornes}) if bornes else qs


def q_mois_annee_scolaire(annee, prefixe=''):
    """Filtre Q des mois (champs ``annee``/``mois``) d'une année scolaire :
    septembre à décembre de la 1re année civile, janvier à août de la 2e."""
    from django.db.models import Q
    bornes = bornes_annee_scolaire(annee)
    if not bornes:
        return Q()
    debut = bornes[0].year
    return (Q(**{f'{prefixe}annee': debut, f'{prefixe}mois__gte': 9})
            | Q(**{f'{prefixe}annee': debut + 1, f'{prefixe}mois__lte': 8}))


def annee_suivante(annee: str) -> str:
    """Calcule l'annee scolaire suivante. Ex: 2025-2026 -> 2026-2027."""
    try:
        debut, fin = str(annee).split('-')
        return f"{int(debut) + 1}-{int(fin) + 1}"
    except Exception:
        return annee


# Une année se prépare au plus six mois avant sa rentrée (à partir du 1er mars
# pour une rentrée le 1er septembre). Sans cette limite, un second clic sur
# « Nouvelle année scolaire » juste après une création faisait passer de
# nouveau tous les élèves vers l'année d'après (non évalués = admis).
DELAI_PREPARATION_MOIS = 6


def date_ouverture_annee(annee):
    """Premier jour où l'année ``annee`` peut être créée ; None si mal formée."""
    from dateutil.relativedelta import relativedelta
    bornes = bornes_annee_scolaire(annee)
    return bornes[0] - relativedelta(months=DELAI_PREPARATION_MOIS) if bornes else None


def creation_annee_trop_tot(annee, today=None):
    """Date d'ouverture si l'année ``annee`` ne peut pas encore être créée, sinon None."""
    ouverture = date_ouverture_annee(annee)
    if ouverture and (today or date.today()) < ouverture:
        return ouverture
    return None


def date_fin_annee_scolaire(annee: str):
    """Retourne la date de fin configuree pour une annee scolaire."""
    try:
        _, fin = str(annee).split('-')
        fin_annee = int(fin)
        fin_mois = int(getattr(settings, 'ANNEE_SCOLAIRE_FIN_MOIS', 6))
        fin_jour = int(getattr(settings, 'ANNEE_SCOLAIRE_FIN_JOUR', 30))
        return date(fin_annee, fin_mois, fin_jour)
    except Exception:
        return None


def get_statut_creation_nouvelle_annee(ecole, today=None):
    """Indique si la nouvelle annee doit etre preparee pour l'ecole."""
    if not ecole:
        return {'due': False}

    annee_courante = (
        Classe.objects
        .filter(ecole=ecole)
        .values_list('annee_scolaire', flat=True)
        .distinct()
        .order_by('-annee_scolaire')
        .first()
    )
    if not annee_courante:
        return {'due': False}

    fin = date_fin_annee_scolaire(annee_courante)
    if not fin:
        return {'due': False}

    today = today or date.today()
    prochaine = annee_suivante(annee_courante)
    prochaine_existe = Classe.objects.filter(
        ecole=ecole,
        annee_scolaire=prochaine,
    ).exists()

    return {
        'due': today > fin and not prochaine_existe,
        'annee_courante': annee_courante,
        'annee_nouvelle': prochaine,
        'date_fin': fin,
        'nouvelle_existe': prochaine_existe,
    }
