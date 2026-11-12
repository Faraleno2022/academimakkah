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

from assistant_ia import lot
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


def _paiement_identique(eleve, montant, date_iso, reference=""):
    """Paiement déjà enregistré pour cet élève : même référence, ou même montant le même jour."""
    import re

    from .models import Paiement

    if eleve is None:
        return None
    actifs = Paiement.objects.filter(eleve=eleve).exclude(statut__in=("REJETE", "REMBOURSE")).order_by("-pk")
    if (reference or "").strip():
        par_reference = actifs.filter(reference_externe__iexact=reference.strip()).first()
        if par_reference:
            return par_reference
    try:
        montant = int(float(montant))
    except (TypeError, ValueError):
        return None
    if montant <= 0 or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_iso or ""):
        return None
    return actifs.filter(montant=montant, date_paiement=date_iso).first()


def _resume_paiement(p) -> dict:
    return {"recu": p.numero_recu or "", "date": p.date_paiement.strftime("%d/%m/%Y"),
            "montant": int(p.montant), "statut": p.get_statut_display()}


LIBELLES_POSTES = {"tranche_1": "Tranche 1", "tranche_2": "Tranche 2", "tranche_3": "Tranche 3"}


def _gnf(valeur) -> str:
    return f"{int(valeur):,}".replace(",", " ") + " GNF"


def _type_pour_poste(cle, types, echeancier):
    """Type de paiement actif qui vise exactement ce poste (inscription ou tranche n)."""
    from .allocation import payment_type_plan

    attendu = "reinscription" if echeancier.est_reinscription else "inscription"
    candidats = []
    for t in types:
        plan = payment_type_plan(t)
        if cle == "inscription":
            if plan["include_registration"] and not plan["tranches"]:
                candidats.append((plan["registration_kind"] != attendu, t))
        elif plan["tranches"] == (int(cle[-1]),) and not plan["include_registration"]:
            candidats.append((False, t))
    candidats.sort(key=lambda c: (c[0], len(c[1].nom)))
    return candidats[0][1] if candidats else None


def _situation(eleve, lu, types, deja_dans_document=0):
    """
    Situation de la scolarité de l'élève pour l'année en cours : total dû, reste à payer,
    reste par poste (inscription, tranches) et prochain poste à régler. Même calcul que
    la page « Ajouter un paiement » : paiements validés ET en attente, remises déduites.
    Peut corriger ``lu["type_paiement_id"]`` quand la tranche lue est déjà soldée.
    """
    from decimal import Decimal

    from django.db.models import Sum

    from .allocation import ALLOCATION_COMPONENTS, payment_type_plan, reste_par_tranche_avec_couverture
    from .models import Paiement, PaiementRemise
    from .views import _echeancier_for_eleve

    echeancier = _echeancier_for_eleve(eleve)
    if echeancier is None:
        return {"echeancier": False, "bloquant": False, "alerte": "",
                "resume": "Pas encore d'échéancier pour cette année : il sera créé à l'enregistrement."}

    actifs = Paiement.objects.filter(
        eleve=eleve, annee_scolaire=echeancier.annee_scolaire, statut__in=("EN_ATTENTE", "VALIDE")
    )
    encaisse = actifs.aggregate(s=Sum("montant"))["s"] or 0
    en_attente = actifs.filter(statut="EN_ATTENTE").aggregate(s=Sum("montant"))["s"] or 0
    remises = PaiementRemise.objects.filter(
        paiement__eleve=eleve, paiement__annee_scolaire=echeancier.annee_scolaire,
        paiement__statut__in=("EN_ATTENTE", "VALIDE"),
    ).aggregate(s=Sum("montant_remise"))["s"] or 0
    couverture = Decimal(encaisse) + Decimal(remises) + Decimal(deja_dans_document)

    restes = reste_par_tranche_avec_couverture(echeancier, couverture)
    total_du = int(echeancier.total_du)
    reste = max(0, total_du - int(couverture))
    postes = []
    for cle, champ_du, _ in ALLOCATION_COMPONENTS:
        du = int(getattr(echeancier, champ_du) or 0)
        if du:
            postes.append({
                "cle": cle, "du": du, "reste": int(restes[cle]),
                "libelle": echeancier.libelle_frais_admission if cle == "inscription" else LIBELLES_POSTES[cle],
            })
    prochain = next((p for p in postes if p["reste"] > 0), None)
    situation = {
        "echeancier": True, "total_du": total_du, "reste": reste, "solde": reste == 0,
        "en_attente": int(en_attente), "postes": postes,
        "prochain": prochain["libelle"] if prochain else "", "alerte": "", "bloquant": False,
    }

    if reste == 0:
        situation["resume"] = f"Scolarité entièrement payée ({_gnf(total_du)})."
        situation["alerte"] = "Scolarité déjà entièrement payée : aucun paiement n'est attendu pour cet élève cette année."
        situation["bloquant"] = True
        return situation

    detail = " ; ".join(
        f"{p['libelle']} soldé" if p["reste"] == 0 else
        (f"{p['libelle']} : reste {_gnf(p['reste'])}" if p["reste"] < p["du"] else f"{p['libelle']} : {_gnf(p['du'])} dû")
        for p in postes
    )
    resume = f"Reste à payer : {_gnf(reste)} sur {_gnf(total_du)} ({detail}). À régler maintenant : {prochain['libelle']}."
    if en_attente:
        resume += f" Dont {_gnf(en_attente)} déjà saisi(s) en attente de validation."
    if deja_dans_document:
        resume += f" Tient compte de {_gnf(deja_dans_document)} lu(s) plus haut dans ce document."

    montant = lu["montant"]
    if montant is not None and montant > reste:
        situation["alerte"] = (f"Le montant lu ({_gnf(montant)}) dépasse le reste à payer ({_gnf(reste)}) : "
                               "il serait refusé. Corrigez le montant.")
        situation["bloquant"] = True
    elif montant is not None and montant > prochain["reste"]:
        resume += f" Le montant dépasse le reste de {prochain['libelle']} : l'excédent passera sur le poste suivant."
    elif montant is not None and montant < prochain["reste"]:
        resume += f" Paiement partiel : il restera {_gnf(prochain['reste'] - int(montant))} sur {prochain['libelle']}."

    # Type de paiement : absent, ou visant un poste déjà soldé -> on propose le prochain poste
    par_id = {t.id: t for t in types}
    type_lu = par_id.get(lu["type_paiement_id"])
    vises = []
    if type_lu:
        plan = payment_type_plan(type_lu)
        vises = (["inscription"] if plan["include_registration"] else []) + [f"tranche_{n}" for n in plan["tranches"]]
    deja_solde = bool(vises) and all(int(restes.get(c, 0)) == 0 for c in vises)
    if type_lu is None or deja_solde:
        propose = _type_pour_poste(prochain["cle"], types, echeancier)
        if propose and propose.id != lu["type_paiement_id"]:
            if deja_solde:
                resume += f" « {type_lu.nom} » est déjà soldé : type remplacé par « {propose.nom} »."
            else:
                resume += f" Type proposé : « {propose.nom} »."
            lu["type_paiement_id"] = propose.id
            lu["champs_douteux"].append("type_paiement_id")
    situation["resume"] = resume
    return situation


@login_required
@require_POST
def assistant_paiement(request):
    empreintes = lot.empreintes_document(request)
    types = list(TypePaiement.objects.filter(actif=True).order_by("nom"))
    modes = list(ModePaiement.objects.filter(actif=True).order_by("nom"))

    try:
        images = [preparer_image(f) for f in request.FILES.getlist("photos")[:4]]
        resultat = extraire_json(_consigne(types, modes), SCHEMA, images=images, texte=request.POST.get("texte", ""))
    except AssistantIAError as exc:
        return JsonResponse({"ok": False, "erreur": str(exc)}, status=400)

    ids_types, ids_modes = {t.id for t in types}, {m.id for m in modes}
    paiements, engages = [], {}  # engages : montants déjà lus dans ce document, par élève
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
        identique = _paiement_identique(eleve, lu["montant"], lu["date_paiement"], lu["reference"])
        situation = None
        if eleve is not None:
            try:
                situation = _situation(eleve, lu, types, engages.get(eleve.id, 0))
            except Exception:  # noqa: BLE001 — la situation est une aide : elle ne doit jamais bloquer la lecture
                logger.exception("Assistant paiement : situation indisponible pour l'élève %s", eleve.pk)
            if lu["montant"] is not None and not identique and not (situation or {}).get("bloquant"):
                engages[eleve.id] = engages.get(eleve.id, 0) + int(lu["montant"])
        paiements.append({
            "lu": lu,
            "situation": situation,
            "deja_enregistre": _resume_paiement(identique) if identique else None,
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
    return lot.reponse_lecture(
        request, "paiements", empreintes,
        **paiements[0],
        paiements=paiements,
        types=[[t.id, t.nom] for t in types],
        modes=[[m.id, m.nom] for m in modes],
    )


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
        identique = _paiement_identique(eleve, donnees["montant"], donnees["date_paiement"], donnees["reference_externe"])
        if identique:
            montant_gnf = f"{int(identique.montant):,}".replace(",", " ")
            echecs.append({"index": index, "nom": nom, "erreur": (
                f"déjà enregistré le {identique.date_paiement:%d/%m/%Y} (reçu {identique.numero_recu}, "
                f"{montant_gnf} GNF). S'il s'agit vraiment d'un second paiement identique, "
                "saisissez-le par le formulaire.")})
            continue
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
    import json
    lot.noter_import(request, "paiements", lot.empreintes_du_corps(json.loads(request.body.decode("utf-8"))), len(crees))
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
