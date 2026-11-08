"""
Annulation d'une année scolaire créée par erreur (la plus récente).

Chaque élève de l'année annulée retourne dans la classe qu'il occupait l'année
précédente, retrouvée grâce à l'historique écrit lors du passage ; les élèves
sortis en fin de cycle par ce passage redeviennent actifs ; les enseignants
retrouvent leur affectation ; puis les classes, grilles et classes Notes vides
de l'année annulée sont supprimées.

Rien n'est annulé si de l'argent ou des notes ont déjà été saisis dans l'année :
ces données ne doivent jamais disparaître avec elle.
"""
import logging
import re
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import redirect, render

from utilisateurs.utils import user_is_admin, user_school

from .models import Classe, Eleve, HistoriqueEleve
from .utils_annee import SESSION_ANNEE_ACTIVE, bornes_annee_scolaire

logger = logging.getLogger(__name__)

# Même raison que pour la création : ~80 requêtes par élève déplacé.
TAILLE_LOT_ANNULATION = 100


ROLES_ANNULATION = ('ADMIN', 'DIRECTEUR')


def peut_annuler_annee(user):
    """Administrateur ou directeur de l'école (et superutilisateur)."""
    if user_is_admin(user):
        return True
    profil = getattr(user, 'profil', None)
    return getattr(profil, 'role', None) in ROLES_ANNULATION


def _annee_precedente(annee):
    try:
        debut, fin = annee.split('-')
        return f"{int(debut) - 1}-{int(fin) - 1}"
    except (AttributeError, ValueError):
        return None


def _motifs_passage(annee):
    """Descriptions écrites par ``_passer_eleve`` : elles donnent la classe quittée.

    « Passage automatique » est la formulation de février-mars 2026 : elle reste
    dans l'historique des élèves passés à cette époque.
    """
    a = re.escape(annee)
    return (
        re.compile(rf"^Passage(?: automatique)? nouvelle année {a}: (?P<classe>.+?) → "),
        re.compile(rf"^Conservation nouvelle année {a}: (?P<classe>.+?) → "),
        re.compile(rf"^Redoublant — année {a}: maintenu\(e\) en (?P<classe>.+?) \(moyenne"),
    )


def _classe_quittee(eleve, annee, motifs):
    """Nom de la classe occupée avant le passage vers ``annee`` (le plus récent)."""
    for h in (HistoriqueEleve.objects
              .filter(eleve=eleve, action='CHANGEMENT_CLASSE', description__contains=annee)
              .order_by('-date_action', '-pk')):
        for motif in motifs:
            m = motif.match(h.description)
            if m:
                return m.group('classe')
    return None


def _sortis_par_le_passage(ecole, annee, annee_prec):
    """Élèves archivés (BAC) ou sortis en fin de cycle par le passage vers ``annee``."""
    marque = f"lors du passage à l'année {annee}"
    return (Eleve.objects
            .filter(classe__ecole=ecole, classe__annee_scolaire=annee_prec,
                    statut__in=('TRANSFERE', 'DIPLOME'),
                    historique__action__in=('FIN_CYCLE', 'DIPLOME'),
                    historique__description__contains=marque)
            .distinct())


def _blocages(ecole, annee):
    """Raisons d'interdire l'annulation (données qu'on ne doit pas perdre)."""
    from bus.models import AbonnementBus, AbonnementCantine
    from notes.models import (AppreciationMaternelle, Classement, CompositionNote, Evaluation,
                              NoteMaternelle, NoteMensuelle)
    from paiements.models import Paiement

    raisons = []
    nb = Paiement.objects.filter(eleve__classe__ecole=ecole, annee_scolaire=annee).count()
    if nb:
        raisons.append(f"{nb} paiement(s) déjà enregistré(s) pour {annee}")
    nb = (AbonnementBus.objects.filter(eleve__classe__ecole=ecole, annee_scolaire=annee).count()
          + AbonnementCantine.objects.filter(eleve__classe__ecole=ecole, annee_scolaire=annee).count())
    if nb:
        raisons.append(f"{nb} abonnement(s) bus ou cantine en {annee}")
    filtre = {'matiere__classe__ecole': ecole, 'matiere__classe__annee_scolaire': annee}
    nb = sum(m.objects.filter(**filtre).count()
             for m in (NoteMensuelle, CompositionNote, Evaluation, NoteMaternelle,
                       AppreciationMaternelle))
    nb += Classement.objects.filter(classe__ecole=ecole, classe__annee_scolaire=annee).count()
    if nb:
        raisons.append(f"{nb} note(s) ou classement(s) déjà saisi(s) en {annee}")
    return raisons


def _contexte(ecole, annee):
    annees = list(Classe.objects.filter(ecole=ecole)
                  .values_list('annee_scolaire', flat=True).distinct().order_by('-annee_scolaire'))
    annee_prec = _annee_precedente(annee)
    if not annees or annee != annees[0] or annee_prec not in annees:
        return None
    return {
        'annee': annee,
        'annee_precedente': annee_prec,
        'nb_eleves': Eleve.objects.filter(classe__ecole=ecole, classe__annee_scolaire=annee).count(),
        'nb_sortis': _sortis_par_le_passage(ecole, annee, annee_prec).count(),
        'nb_classes': Classe.objects.filter(ecole=ecole, annee_scolaire=annee).count(),
        'blocages': _blocages(ecole, annee),
    }


def _ramener_eleve(request, eleve, annee, annee_prec, classes_prec, motifs):
    from paiements.models import EcheancierPaiement

    nom = _classe_quittee(eleve, annee, motifs)
    # Inscrit directement dans l'année annulée : même classe l'année précédente.
    cible = classes_prec.get(nom) or classes_prec.get(eleve.classe.nom)
    if cible is None:
        raise ValueError(f"aucune classe « {nom or eleve.classe.nom} » en {annee_prec}")
    EcheancierPaiement.objects.filter(eleve=eleve, annee_scolaire=annee).delete()
    classe_annulee = eleve.classe.nom
    eleve.classe = cible
    eleve._current_user = request.user
    # Ni transfert de notes, ni recalcul financier, ni historique automatique.
    eleve._passage_nouvelle_annee = True
    eleve.save()
    HistoriqueEleve.objects.create(
        eleve=eleve,
        action='CHANGEMENT_CLASSE',
        description=(f"Annulation de l'année {annee}: {classe_annulee} → retour en {cible.nom} "
                     f"({annee_prec}). Nouveau matricule: {eleve.matricule}"),
        utilisateur=request.user,
    )


def _reprendre_affectations(ecole, annee, annee_prec):
    """Rouvre les affectations closes par la reconduction et retire leurs copies."""
    from django.core.exceptions import ValidationError
    from salaires.models import AffectationClasse

    veille = bornes_annee_scolaire(annee)[0] - timedelta(days=1)
    classes_prec = {c.nom: c for c in Classe.objects.filter(ecole=ecole, annee_scolaire=annee_prec)}
    rouvertes = 0
    for aff in (AffectationClasse.objects.select_related('classe', 'enseignant')
                .filter(classe__ecole=ecole, classe__annee_scolaire=annee)):
        origine = (AffectationClasse.objects
                   .filter(enseignant=aff.enseignant, classe__ecole=ecole,
                           classe__annee_scolaire=annee_prec, classe__nom=aff.classe.nom,
                           matiere=aff.matiere, actif=False, date_fin=veille)
                   .first())
        try:
            with transaction.atomic():
                if origine:
                    aff.delete()
                    origine.actif = True
                    origine.date_fin = None
                    origine.save()
                    rouvertes += 1
                elif aff.classe.nom in classes_prec:
                    aff.classe = classes_prec[aff.classe.nom]
                    aff.save()
                    rouvertes += 1
                else:
                    aff.delete()
        except ValidationError as exc:
            # Ex. l'enseignant a déjà une autre classe principale active.
            logger.warning(f"Affectation {aff.pk} non reprise : {exc}")
            aff.delete()
    return rouvertes


def _finaliser(request, ecole, annee, annee_prec):
    from bus.models import GrilleTarifaireBus
    from notes.models import ClasseNote
    from paiements.models import EcheancierPaiement

    from .models import GrilleTarifaire

    rouverts = 0
    for eleve in _sortis_par_le_passage(ecole, annee, annee_prec):
        eleve.statut = 'ACTIF'  # redevient ATTENTE_PAIEMENT si l'import est verrouillé
        eleve.save()
        HistoriqueEleve.objects.create(
            eleve=eleve, action='CHANGEMENT_STATUT',
            description=f"Annulation de l'année {annee}: retour en {eleve.classe.nom} ({annee_prec}).",
            utilisateur=request.user,
        )
        rouverts += 1
    affectations = _reprendre_affectations(ecole, annee, annee_prec)
    EcheancierPaiement.objects.filter(eleve__classe__ecole=ecole, annee_scolaire=annee).delete()
    ClasseNote.objects.filter(ecole=ecole, annee_scolaire=annee).delete()
    GrilleTarifaire.objects.filter(ecole=ecole, annee_scolaire=annee).delete()
    GrilleTarifaireBus.objects.filter(ecole=ecole, annee_scolaire=annee).delete()
    Classe.objects.filter(ecole=ecole, annee_scolaire=annee, eleves__isnull=True).delete()
    return rouverts, affectations


@login_required
def annuler_annee(request, annee):
    """Confirmation (GET) puis annulation par lots (POST) de l'année ``annee``."""
    ecole = user_school(request.user)
    if not ecole or not peut_annuler_annee(request.user):
        messages.error(request, "Seul un administrateur ou le directeur de l'école "
                                "peut annuler une année scolaire.")
        return redirect('eleves:gestion_annees')

    ctx = _contexte(ecole, annee)
    if ctx is None:
        messages.error(request, "Seule l'année la plus récente peut être annulée, "
                                "et l'année qui la précède doit exister.")
        return redirect('eleves:gestion_annees')

    if request.method != 'POST':
        return render(request, 'eleves/annuler_annee.html', {**ctx, 'titre_page': f"Annuler {annee}"})

    if ctx['blocages']:
        messages.error(request, f"Annulation impossible : {' ; '.join(ctx['blocages'])}.")
        return redirect('eleves:gestion_annees')
    if request.POST.get('confirmation', '').strip() != annee:
        messages.error(request, f"Tapez {annee} pour confirmer l'annulation.")
        return redirect('eleves:annuler_annee', annee=annee)

    annee_prec = ctx['annee_precedente']
    classes_prec = {c.nom: c for c in Classe.objects.filter(ecole=ecole, annee_scolaire=annee_prec)}
    motifs = _motifs_passage(annee)
    echecs = [int(pk) for pk in request.POST.getlist('echecs') if pk.isdigit()]
    restants = (Eleve.objects.filter(classe__ecole=ecole, classe__annee_scolaire=annee)
                .exclude(pk__in=echecs))
    for eleve in list(restants.select_related('classe').order_by('pk')[:TAILLE_LOT_ANNULATION]):
        try:
            with transaction.atomic():
                _ramener_eleve(request, eleve, annee, annee_prec, classes_prec, motifs)
        except Exception as exc:
            logger.error(f"Annulation {annee} impossible pour l'élève {eleve.pk}: {exc}", exc_info=True)
            echecs.append(eleve.pk)

    reste = restants.exclude(pk__in=echecs).count()
    if reste:
        total = request.POST.get('total', '')
        total = int(total) if total.isdigit() else ctx['nb_eleves']
        traites = max(0, total - reste)
        return render(request, 'eleves/annuler_annee_progression.html', {
            'annee': annee, 'echecs': echecs, 'traites': traites, 'total': total,
            'pourcentage': min(100, int(traites * 100 / max(1, total))),
            'titre_page': f"Annuler {annee}",
        })

    if echecs:
        noms = ', '.join(f"{e.prenom} {e.nom}" for e in Eleve.objects.filter(pk__in=echecs[:20]))
        messages.error(request, f"{len(echecs)} élève(s) n'ont pas pu revenir en {annee_prec} "
                                f"({noms}) : l'année {annee} est conservée. Déplacez-les "
                                f"manuellement puis relancez l'annulation.")
        return redirect('eleves:gestion_annees')

    with transaction.atomic():
        rouverts, affectations = _finaliser(request, ecole, annee, annee_prec)
    request.session[SESSION_ANNEE_ACTIVE] = annee_prec
    texte = f"L'année {annee} a été annulée : les élèves sont de retour dans leurs classes de {annee_prec}."
    if rouverts:
        texte += f" {rouverts} élève(s) de fin de cycle réintégré(s)."
    if affectations:
        texte += f" {affectations} affectation(s) d'enseignant reprise(s)."
    messages.success(request, texte)
    return redirect('eleves:gestion_annees')
