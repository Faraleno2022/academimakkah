"""
Assistant IA pour le formulaire « Ajouter un élève ».

L'utilisateur photographie une fiche d'inscription, un extrait de naissance, un ancien
bulletin… (ou colle un texte). Claude relève l'identité de l'élève et de son responsable ;
le serveur vérifie la classe, signale les élèves déjà inscrits sous un nom proche et un
responsable déjà connu par son téléphone. La page pré-remplit le formulaire ; rien n'est
enregistré avant le clic sur « Enregistrer ».
"""

import logging
import re

from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import JsonResponse
from django.views.decorators.http import require_POST

from assistant_ia.claude import AssistantIAError, extraire_json, preparer_image, ressemblance
from utilisateurs.utils import user_is_admin, user_school

from .models import Classe, Eleve, Responsable
from .utils_annee import get_annee_active

logger = logging.getLogger(__name__)

RELATIONS = [code for code, _ in Responsable.RELATION_CHOICES]

SCHEMA = {
    "type": "object",
    "properties": {
        "prenom": {"type": "string"},
        "nom": {"type": "string"},
        "sexe": {"type": "string", "enum": ["M", "F", ""]},
        "date_naissance": {"type": "string"},
        "lieu_naissance": {"type": "string"},
        "classe_id": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        "responsable": {
            "type": "object",
            "properties": {
                "prenom": {"type": "string"},
                "nom": {"type": "string"},
                "relation": {"type": "string", "enum": RELATIONS + [""]},
                "telephone": {"type": "string"},
                "telephone_secondaire": {"type": "string"},
                "email": {"type": "string"},
                "profession": {"type": "string"},
                "adresse": {"type": "string"},
            },
            "required": [
                "prenom", "nom", "relation", "telephone", "telephone_secondaire", "email", "profession", "adresse",
            ],
            "additionalProperties": False,
        },
        "champs_douteux": {"type": "array", "items": {"type": "string"}},
        "remarque": {"type": "string"},
    },
    "required": [
        "prenom", "nom", "sexe", "date_naissance", "lieu_naissance", "classe_id",
        "responsable", "champs_douteux", "remarque",
    ],
    "additionalProperties": False,
}


def _consigne(classes) -> str:
    liste_classes = "\n".join(f"{c.id} | {c.nom}" for c in classes) or "(aucune : mets classe_id à null)"
    return f"""Tu aides le secrétariat d'une école guinéenne à inscrire un élève dans MySchoolGN.
On te donne la photo d'un document (fiche d'inscription, extrait d'acte de naissance, ancien
bulletin, carte scolaire…) ou un texte. Relève les informations de l'élève et de son responsable.

Classes de l'école (id | nom) :
{liste_classes}

Relève :
- prenom et nom de l'élève (en Guinée le nom de famille est souvent écrit en dernier ou en
  MAJUSCULES : Diallo, Bah, Camara, Barry, Sow, Condé, Touré…). Ne mélange pas avec les parents.
- sexe : "M" ou "F" d'après le document ou, à défaut, un prénom sans ambiguïté
  (Mamadou, Ibrahima → M ; Aminata, Fatoumata, Mariama → F) ; "" si incertain.
- date_naissance au format AAAA-MM-JJ, "" si absente. « né vers 2015 » : "" et explique dans remarque.
- lieu_naissance : ville ou préfecture, "" si absent.
- classe_id : la classe de la liste qui correspond à la classe demandée (« 7ème année »,
  « CM1 », « 11e SL »…), null si le document n'indique pas de classe ou si aucune ne correspond.
- responsable : le parent ou tuteur (père, mère, tuteur…), avec :
  relation parmi {", ".join(RELATIONS)} ("" si inconnue) ;
  telephone et telephone_secondaire : uniquement les chiffres, sans indicatif 224
  (ex. « +224 622 61 35 59 » → "622613559") ; "" si absent ;
  email, profession, adresse (quartier, commune) : "" si absents.
  Si le document ne cite aucun responsable, laisse tous ses champs vides.
- champs_douteux : noms des champs dont tu n'es pas sûr
  (ex. ["date_naissance", "responsable.telephone"]).
- remarque : courte explication en français si quelque chose est incertain, sinon "".
N'invente rien : laisse vide ce que tu ne lis pas clairement."""


def _classes_proposees(request):
    """Mêmes classes que la liste du formulaire (école de l'utilisateur, année active)."""
    if user_is_admin(request.user):
        return []
    ecole = user_school(request.user)
    if not ecole:
        return []
    classes = Classe.objects.filter(ecole=ecole)
    annee = get_annee_active(request, ecole)
    if annee:
        classes = classes.filter(annee_scolaire=annee)
    return list(classes.order_by("niveau", "nom"))


def _telephone_local(valeur: str) -> str:
    chiffres = re.sub(r"\D+", "", valeur or "")
    if chiffres.startswith("224") and len(chiffres) in (11, 12):
        chiffres = chiffres[3:]
    return chiffres if len(chiffres) in (8, 9) else ""


def _doublons(ecole, prenom: str, nom: str):
    """Élèves de l'école déjà inscrits sous un nom très proche (risque de double inscription)."""
    if not ecole or not (prenom or nom):
        return []
    lu = f"{prenom} {nom}"
    trouves = []
    for e in Eleve.objects.filter(classe__ecole=ecole).select_related("classe").only(
        "id", "matricule", "prenom", "nom", "statut", "classe__nom"
    ):
        score = ressemblance(lu, f"{e.prenom} {e.nom}")
        if score >= 0.88:
            trouves.append((score, e))
    trouves.sort(key=lambda x: x[0], reverse=True)
    return [
        {"id": e.id, "matricule": e.matricule, "nom": f"{e.prenom} {e.nom}",
         "classe": e.classe.nom if e.classe else "", "statut": e.get_statut_display()}
        for _, e in trouves[:3]
    ]


def _responsable_connu(ecole, telephone_local: str):
    """Responsable déjà enregistré dans l'école avec ce numéro."""
    if not ecole or not telephone_local:
        return None
    numero = f"+224{telephone_local}"
    r = (
        Responsable.objects.filter(Q(telephone=numero) | Q(telephone_secondaire=numero))
        .filter(Q(eleves_principal__classe__ecole=ecole) | Q(eleves_secondaire__classe__ecole=ecole))
        .distinct()
        .first()
    )
    if not r:
        return None
    return {"id": r.id, "nom": f"{r.prenom} {r.nom}", "telephone": r.telephone}


@login_required
@require_POST
def assistant_eleve(request):
    ecole = None if user_is_admin(request.user) else user_school(request.user)
    if not user_is_admin(request.user) and ecole is None:
        return JsonResponse({"ok": False, "erreur": "Votre compte n'est rattaché à aucune école."}, status=403)

    classes = _classes_proposees(request)
    try:
        images = [preparer_image(f) for f in request.FILES.getlist("photos")[:3]]
        lu = extraire_json(_consigne(classes), SCHEMA, images=images, texte=request.POST.get("texte", ""))
    except AssistantIAError as exc:
        return JsonResponse({"ok": False, "erreur": str(exc)}, status=400)

    # Seule une classe proposée par le formulaire peut être choisie
    if lu["classe_id"] not in {c.id for c in classes}:
        lu["classe_id"] = None
    resp = lu["responsable"]
    for champ in ("telephone", "telephone_secondaire"):
        brut = resp[champ]
        resp[champ] = _telephone_local(brut)
        if brut and not resp[champ]:
            lu["champs_douteux"].append(f"responsable.{champ}")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", lu["date_naissance"] or ""):
        if lu["date_naissance"]:
            lu["champs_douteux"].append("date_naissance")
        lu["date_naissance"] = ""

    logger.info(
        "Assistant élève : « %s %s » lu par %s", lu["prenom"], lu["nom"], request.user.username,
    )
    return JsonResponse({
        "ok": True,
        "lu": lu,
        "doublons": _doublons(ecole, lu["prenom"], lu["nom"]),
        "responsable_connu": _responsable_connu(ecole, resp["telephone"]),
    })
