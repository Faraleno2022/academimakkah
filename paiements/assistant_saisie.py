"""
Assistant IA pour la saisie d'un paiement à partir d'une photo (reçu, bordereau
Orange Money / MTN, fiche de versement) ou d'un texte.

Claude lit le document ; le serveur retrouve l'élève de l'école (matricule, puis
nom) ; la page pré-remplit le formulaire « Ajouter un paiement ». L'enregistrement
passe ensuite par le circuit habituel (numéro de reçu, tranches, statut EN_ATTENTE
jusqu'à validation).
"""

import logging

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_POST

from assistant_ia.claude import AssistantIAError, extraire_json, normaliser, preparer_image, ressemblance
from eleves.models import Eleve

from .models import ModePaiement, TypePaiement

logger = logging.getLogger(__name__)

SCORE_MIN = 0.72  # en dessous, on ne propose pas l'élève automatiquement

SCHEMA = {
    "type": "object",
    "properties": {
        "eleve_nom": {"type": "string"},
        "matricule": {"type": "string"},
        "classe": {"type": "string"},
        "montant": {"anyOf": [{"type": "number"}, {"type": "null"}]},
        "date_paiement": {"type": "string"},
        "type_paiement_id": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        "mode_paiement_id": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        "reference": {"type": "string"},
        "payeur": {"type": "string"},
        "plusieurs_paiements": {"type": "boolean"},
        "champs_douteux": {"type": "array", "items": {"type": "string"}},
        "remarque": {"type": "string"},
    },
    "required": [
        "eleve_nom", "matricule", "classe", "montant", "date_paiement", "type_paiement_id",
        "mode_paiement_id", "reference", "payeur", "plusieurs_paiements", "champs_douteux", "remarque",
    ],
    "additionalProperties": False,
}


def _consigne(types, modes) -> str:
    liste_types = "\n".join(f"{t.id} | {t.nom}" for t in types) or "(aucun)"
    liste_modes = "\n".join(f"{m.id} | {m.nom}" for m in modes) or "(aucun)"
    return f"""Tu aides la comptabilité d'une école guinéenne à saisir un paiement de frais
scolaires dans MySchoolGN. On te donne la photo d'un reçu, d'un bordereau de versement,
d'une capture Orange Money / MTN Mobile Money, ou un texte.

Types de paiement possibles (id | nom) :
{liste_types}

Modes de paiement possibles (id | nom) :
{liste_modes}

Relève :
- eleve_nom : nom et prénom de l'élève concerné (pas celui du parent qui paie).
- matricule et classe de l'élève s'ils apparaissent, sinon "".
- montant : en francs guinéens (GNF), nombre entier sans espaces ; « 1 500 000 GNF » = 1500000.
  null si illisible.
- date_paiement : au format AAAA-MM-JJ, "" si absente. Aujourd'hui : {timezone.localdate():%Y-%m-%d}.
- type_paiement_id : le type de la liste qui correspond (inscription, réinscription,
  scolarité / tranche…), null si on ne peut pas le savoir.
- mode_paiement_id : le mode de la liste (espèces, Orange Money, MTN, virement, chèque…),
  null si inconnu.
- reference : numéro de transaction ou de bordereau, "" si absent.
- payeur : nom de la personne qui a payé si différent de l'élève, sinon "".
- plusieurs_paiements : true si le document contient plusieurs paiements différents
  (relève alors seulement le premier et explique dans remarque).
- champs_douteux : noms des champs dont tu n'es pas sûr (ex. ["montant", "eleve_nom"]).
- remarque : courte explication en français si quelque chose est incertain, sinon "".
N'invente rien : laisse vide ou null ce que tu ne lis pas clairement."""


def _eleves_ecole(user):
    """Mêmes élèves que la liste du formulaire de paiement."""
    from utilisateurs.utils import filter_by_user_school

    eleves = Eleve.objects.select_related("classe", "classe__ecole").filter(
        statut__in=["ACTIF", "ATTENTE_PAIEMENT"]
    )
    return filter_by_user_school(eleves, user, "classe__ecole")


def _trouver_eleve(user, nom: str, matricule: str, classe: str):
    """Renvoie (meilleur_eleve | None, liste de suggestions [(eleve, score)])."""
    eleves = _eleves_ecole(user)

    if matricule.strip():
        trouve = eleves.filter(matricule__iexact=matricule.strip()).first()
        if trouve:
            return trouve, [(trouve, 1.0)]

    if not nom.strip():
        return None, []

    classe_norm = normaliser(classe)
    scores = []
    for e in eleves:
        score = ressemblance(nom, f"{e.prenom} {e.nom}")
        # Petit bonus si la classe lue correspond : départage les homonymes
        if classe_norm and e.classe and normaliser(e.classe.nom) == classe_norm:
            score = min(1.0, score + 0.05)
        if score >= 0.55:
            scores.append((e, score))
    scores.sort(key=lambda x: x[1], reverse=True)
    suggestions = scores[:3]

    meilleur = None
    if suggestions and suggestions[0][1] >= SCORE_MIN:
        # Deux élèves presque à égalité : on ne choisit pas à la place de l'utilisateur
        if len(suggestions) == 1 or suggestions[0][1] - suggestions[1][1] >= 0.05:
            meilleur = suggestions[0][0]
    return meilleur, suggestions


def _fiche(e, score):
    return {
        "id": e.id,
        "matricule": e.matricule,
        "nom": f"{e.prenom} {e.nom}",
        "classe": e.classe.nom if e.classe else "",
        "score": round(score, 2),
    }


@login_required
@require_POST
def assistant_paiement(request):
    types = list(TypePaiement.objects.filter(actif=True).order_by("nom"))
    modes = list(ModePaiement.objects.filter(actif=True).order_by("nom"))

    try:
        images = [preparer_image(f) for f in request.FILES.getlist("photos")[:2]]
        lu = extraire_json(_consigne(types, modes), SCHEMA, images=images, texte=request.POST.get("texte", ""))
    except AssistantIAError as exc:
        return JsonResponse({"ok": False, "erreur": str(exc)}, status=400)

    # Seuls des types / modes existants et actifs sont renvoyés à la page
    if lu["type_paiement_id"] not in {t.id for t in types}:
        lu["type_paiement_id"] = None
    if lu["mode_paiement_id"] not in {m.id for m in modes}:
        lu["mode_paiement_id"] = None
    if lu["montant"] is not None and lu["montant"] <= 0:
        lu["montant"] = None
        lu["champs_douteux"].append("montant")

    eleve, suggestions = _trouver_eleve(request.user, lu["eleve_nom"], lu["matricule"], lu["classe"])

    logger.info(
        "Assistant paiement : élève %s (lu « %s »), montant %s, par %s",
        eleve.pk if eleve else "non trouvé", lu["eleve_nom"], lu["montant"], request.user.username,
    )
    return JsonResponse({
        "ok": True,
        "lu": lu,
        "eleve": _fiche(eleve, dict((e.id, s) for e, s in suggestions).get(eleve.id, 1.0)) if eleve else None,
        "suggestions": [_fiche(e, s) for e, s in suggestions],
    })
