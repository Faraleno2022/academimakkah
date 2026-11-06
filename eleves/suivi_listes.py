"""Sélections communes aux listes d'élèves et au suivi des admissions."""
from django.db.models import BooleanField, Case, CharField, Exists, OuterRef, Subquery, Value, When
from django.db.models.functions import Coalesce

from paiements.models import EcheancierPaiement, Paiement
from utilisateurs.utils import filter_by_user_school, user_is_superadmin, user_school
from .models import Classe, Eleve
from .utils_annee import get_annee_active


def classes_visibles(request):
    classes = filter_by_user_school(
        Classe.objects.select_related('ecole'), request.user, 'ecole'
    )
    if not user_is_superadmin(request.user):
        annee = get_annee_active(request, user_school(request.user))
        if annee:
            classes = classes.filter(annee_scolaire=annee)
    return classes.order_by('ecole__nom', 'nom', 'annee_scolaire', 'pk')


def eleves_visibles(request):
    return Eleve.objects.select_related(
        'classe', 'classe__ecole', 'responsable_principal'
    ).filter(classe__in=classes_visibles(request))


def passage_en_attente(request):
    """Élèves restés dans l'année précédant l'année active (passage bloqué ou
    non lancé) : sans cet avertissement, la nouvelle année paraît vide."""
    ecole = user_school(request.user)
    annee = get_annee_active(request, ecole) if ecole else None
    try:
        debut, fin = (int(x) for x in str(annee).split('-'))
    except (TypeError, ValueError):
        return None
    precedente = f"{debut - 1}-{fin - 1}"
    nb = Eleve.objects.filter(
        classe__ecole=ecole, classe__annee_scolaire=precedente,
        statut__in=('ACTIF', 'ATTENTE_PAIEMENT'),
    ).count()
    return {'annee': precedente, 'annee_active': annee, 'nb': nb} if nb else None


def annoter_accueil(eleves):
    admission = EcheancierPaiement.objects.filter(
        eleve_id=OuterRef('pk'), annee_scolaire=OuterRef('classe__annee_scolaire')
    ).values('nature_frais')[:1]
    # Sans échéancier, une fiche créée correspond à une nouvelle inscription.
    return eleves.annotate(
        nature_admission=Coalesce(Subquery(admission), Value('INSCRIPTION'), output_field=CharField())
    ).annotate(
        accueil_concerne=Case(
            When(nature_admission='INSCRIPTION', import_verrouille=False, then=Value(True)),
            default=Value(False), output_field=BooleanField(),
        )
    )


def annoter_presence_paiement(eleves):
    paiements = Paiement.objects.filter(
        eleve_id=OuterRef('pk'), annee_scolaire=OuterRef('classe__annee_scolaire'),
        statut='VALIDE', montant__gt=0,
    )
    return eleves.annotate(avec_paiement=Exists(paiements))
