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

_PAIEMENT = {
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
        "champs_douteux": {"type": "array", "items": {"type": "string"}},
        "remarque": {"type": "string"},
    },
    "required": [
        "eleve_nom", "matricule", "classe", "montant", "date_paiement", "type_paiement_id",
        "mode_paiement_id", "reference", "payeur", "champs_douteux", "remarque",
    ],
    "additionalProperties": False,
}

SCHEMA = {
    "type": "object",
    "properties": {"paiements": {"type": "array", "items": _PAIEMENT}},
    "required": ["paiements"],
    "additionalProperties": False,
}

MAX_LIGNES = 100


def _consigne(types, modes) -> str:
    liste_types = "\n".join(f"{t.id} | {t.nom}" for t in types) or "(aucun)"
    liste_modes = "\n".join(f"{m.id} | {m.nom}" for m in modes) or "(aucun)"
    return f"""Tu aides la comptabilité d'une école guinéenne à saisir un paiement de frais
scolaires dans MySchoolGN. On te donne la photo d'un reçu, d'un bordereau de versement,
d'une capture Orange Money / MTN Mobile Money, d'une liste de versements, ou un texte.
Relève CHAQUE paiement du document, dans l'ordre : un reçu donne un seul paiement, un
bordereau ou une liste peut en donner plusieurs (une ligne par élève).

Types de paiement possibles (id | nom) :
{liste_types}

Modes de paiement possibles (id | nom) :
{liste_modes}

Pour chaque paiement :
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
        images = [preparer_image(f) for f in request.FILES.getlist("photos")[:4]]
        resultat = extraire_json(_consigne(types, modes), SCHEMA, images=images, texte=request.POST.get("texte", ""))
    except AssistantIAError as exc:
        return JsonResponse({"ok": False, "erreur": str(exc)}, status=400)

    ids_types, ids_modes = {t.id for t in types}, {m.id for m in modes}
    paiements = []
    for lu in resultat.get("paiements", [])[:MAX_LIGNES]:
        # Seuls des types / modes existants et actifs sont renvoyés à la page
        if lu["type_paiement_id"] not in ids_types:
            lu["type_paiement_id"] = None
        if lu["mode_paiement_id"] not in ids_modes:
            lu["mode_paiement_id"] = None
        if lu["montant"] is not None and lu["montant"] <= 0:
            lu["montant"] = None
            lu["champs_douteux"].append("montant")
        lu["plusieurs_paiements"] = False

        eleve, suggestions = _trouver_eleve(request.user, lu["eleve_nom"], lu["matricule"], lu["classe"])
        paiements.append({
            "lu": lu,
            "eleve": _fiche(eleve, dict((e.id, s) for e, s in suggestions).get(eleve.id, 1.0)) if eleve else None,
            "suggestions": [_fiche(e, s) for e, s in suggestions],
        })

    if not paiements:
        return JsonResponse({"ok": False, "erreur": "Aucun paiement n'a été lu sur ce document."}, status=400)

    logger.info(
        "Assistant paiement : %s paiement(s) lu(s), premier élève %s, par %s",
        len(paiements), (paiements[0]["eleve"] or {}).get("id", "non trouvé"), request.user.username,
    )
    # Le premier paiement reste au niveau racine : la page s'en sert quand il n'y en a qu'un
    return JsonResponse({
        "ok": True,
        **paiements[0],
        "paiements": paiements,
        "types": [[t.id, t.nom] for t in types],
        "modes": [[m.id, m.nom] for m in modes],
    })


# ---------------------------------------------------------------------------
# Plusieurs paiements corrigés par l'utilisateur : enregistrement en lot ou Excel
# ---------------------------------------------------------------------------

def _lignes_du_corps(request):
    import json

    try:
        lignes = json.loads(request.body.decode("utf-8")).get("lignes")
        assert isinstance(lignes, list)
    except Exception:  # noqa: BLE001
        return None
    return [l for l in lignes if isinstance(l, dict)][:MAX_LIGNES]


def _t(ligne, cle) -> str:
    return str(ligne.get(cle) or "").strip()


def _texte_message(message) -> str:
    import re
    from django.utils.html import strip_tags

    return re.sub(r"\s+", " ", strip_tags(str(message))).strip()


@login_required
@require_POST
def assistant_paiements_importer(request):
    """
    Enregistre chaque ligne par la vue « Ajouter un paiement » elle-même : mêmes contrôles
    (école, échéancier, sur-paiement), même numéro de reçu, statut EN_ATTENTE, mêmes notifications.
    """
    import copy
    import re

    from django.contrib import messages
    from django.http import QueryDict
    from django.utils.datastructures import MultiValueDict

    from .forms import PaiementForm
    from .models import Paiement
    from .views import ajouter_paiement

    lignes = _lignes_du_corps(request)
    if not lignes:
        return JsonResponse({"ok": False, "erreur": "Aucune ligne à enregistrer."}, status=400)

    eleves = _eleves_ecole(request.user)
    stock = messages.get_messages(request)
    deja_lus = len(list(stock))
    crees, echecs = [], []

    for ligne in lignes:
        index, matricule = ligne.get("_index"), _t(ligne, "matricule")
        eleve = eleves.filter(matricule__iexact=matricule).first() if matricule else None
        if eleve is None:
            echecs.append({"index": index, "nom": matricule or "(sans matricule)",
                           "erreur": "élève introuvable dans votre école : vérifiez le matricule"})
            continue
        nom = f"{eleve.prenom} {eleve.nom}"

        donnees = QueryDict(mutable=True)
        donnees.update({
            "eleve": str(eleve.pk),
            "type_paiement": _t(ligne, "type_paiement_id"),
            "mode_paiement": _t(ligne, "mode_paiement_id"),
            "montant": re.sub(r"[^\d]", "", _t(ligne, "montant").split(".")[0].split(",")[0]),
            "date_paiement": _t(ligne, "date_paiement") or timezone.localdate().isoformat(),
            "reference_externe": _t(ligne, "reference"),
            "observations": "Saisi avec l'assistant photo.",
        })
        form = PaiementForm(donnees, user=request.user)
        if not form.is_valid():
            erreurs = " ; ".join(
                f"{form.fields[c].label or c} : {e[0]}" if c in form.fields else str(e[0])
                for c, e in form.errors.items()
            )
            echecs.append({"index": index, "nom": nom, "erreur": erreurs})
            continue

        sous_requete = copy.copy(request)
        sous_requete.method = "POST"
        sous_requete._post, sous_requete._files = donnees, MultiValueDict()
        try:
            reponse = ajouter_paiement(sous_requete)
        except Exception:  # noqa: BLE001
            logger.exception("Assistant paiements : échec pour %s", nom)
            echecs.append({"index": index, "nom": nom, "erreur": "erreur à l'enregistrement"})
            continue

        tous = list(stock)
        nouveaux, deja_lus = tous[deja_lus:], len(tous)
        trouve = re.search(r"/(\d+)/?$", reponse.get("Location", "")) if reponse.status_code == 302 else None
        paiement = Paiement.objects.filter(pk=trouve.group(1), eleve=eleve).first() if trouve else None
        if paiement:
            crees.append({"index": index, "nom": nom, "matricule": eleve.matricule,
                          "montant": int(paiement.montant), "recu": paiement.numero_recu or ""})
        else:
            erreurs = [_texte_message(m) for m in nouveaux if m.level >= messages.ERROR]
            echecs.append({"index": index, "nom": nom, "erreur": " ".join(erreurs) or "paiement refusé"})

    list(stock)  # les messages de la vue ne doivent pas réapparaître sur la page suivante
    logger.info("Assistant paiements : %s enregistré(s), %s refusé(s), par %s", len(crees), len(echecs), request.user.username)
    return JsonResponse({"ok": True, "crees": crees, "echecs": echecs})


@login_required
@require_POST
def assistant_paiements_excel(request):
    """Récapitulatif Excel des paiements lus (il n'existe pas d'import Excel des paiements)."""
    from io import BytesIO

    from django.http import HttpResponse
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    lignes = _lignes_du_corps(request)
    if not lignes:
        return JsonResponse({"ok": False, "erreur": "Aucune ligne à exporter."}, status=400)

    eleves = {e.matricule.upper(): e for e in _eleves_ecole(request.user)}
    types = dict(TypePaiement.objects.filter(actif=True).values_list("id", "nom"))
    modes = dict(ModePaiement.objects.filter(actif=True).values_list("id", "nom"))

    def libelle(table, valeur):
        return table.get(int(valeur), "") if str(valeur).isdigit() else ""

    classeur = Workbook()
    feuille = classeur.active
    feuille.title = "Paiements"
    feuille.append(["Matricule", "Élève", "Classe", "Montant (GNF)", "Date", "Type de paiement", "Mode de paiement", "Référence"])
    for cellule in feuille[1]:
        cellule.font = Font(bold=True, color="FFFFFF")
        cellule.fill = PatternFill(start_color="1746A2", end_color="1746A2", fill_type="solid")
    for l in lignes:
        e = eleves.get(_t(l, "matricule").upper())
        montant = _t(l, "montant")
        feuille.append([
            _t(l, "matricule"), f"{e.prenom} {e.nom}" if e else "", e.classe.nom if e and e.classe else "",
            int(float(montant)) if montant.replace(".", "", 1).isdigit() else montant,
            _t(l, "date_paiement"), libelle(types, _t(l, "type_paiement_id")),
            libelle(modes, _t(l, "mode_paiement_id")), _t(l, "reference"),
        ])
    for lettre, largeur in zip("ABCDEFGH", (18, 28, 16, 16, 12, 28, 20, 20)):
        feuille.column_dimensions[lettre].width = largeur

    tampon = BytesIO()
    classeur.save(tampon)
    reponse = HttpResponse(
        tampon.getvalue(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    reponse["Content-Disposition"] = 'attachment; filename="paiements_assistant.xlsx"'
    return reponse
