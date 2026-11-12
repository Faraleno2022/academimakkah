"""
Assistant IA pour le formulaire « Ajouter un élève ».

L'utilisateur photographie une fiche d'inscription, un extrait de naissance, une liste de
classe… (ou colle un texte). Claude relève l'identité de chaque élève et de son responsable ;
le serveur vérifie la classe, signale les élèves déjà inscrits sous un nom proche et un
responsable déjà connu par son téléphone.

- Un seul élève lu : la page pré-remplit le formulaire, l'utilisateur clique sur « Enregistrer ».
- Plusieurs élèves : la page affiche une table de corrections ; « Valider et importer »
  enregistre chaque ligne cochée avec les mêmes formulaires que la page (mêmes contrôles,
  même génération de matricule), ou « Télécharger l'Excel » produit un fichier au format de
  l'import d'élèves.
"""

import json
import logging
import re
from datetime import date, datetime
from io import BytesIO

from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.views.decorators.http import require_POST

from assistant_ia.claude import AssistantIAError, extraire_json, preparer_image, ressemblance
from utilisateurs.utils import user_is_admin, user_school

from .models import Classe, Eleve, HistoriqueEleve, Responsable
from .utils_annee import get_annee_active

logger = logging.getLogger(__name__)

RELATIONS = [code for code, _ in Responsable.RELATION_CHOICES]
MAX_LIGNES = 200

_ELEVE = {
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

SCHEMA = {
    "type": "object",
    "properties": {"eleves": {"type": "array", "items": _ELEVE}},
    "required": ["eleves"],
    "additionalProperties": False,
}


def _consigne(classes) -> str:
    liste_classes = "\n".join(f"{c.id} | {c.nom}" for c in classes) or "(aucune : mets classe_id à null)"
    return f"""Tu aides le secrétariat d'une école guinéenne à inscrire des élèves dans MySchoolGN.
On te donne la photo d'un document (fiche d'inscription, extrait d'acte de naissance, ancien
bulletin, liste de classe…) ou un texte. Relève CHAQUE élève du document, dans l'ordre, avec
son responsable. Une fiche individuelle donne un seul élève ; une liste en donne plusieurs.

Classes de l'école (id | nom) :
{liste_classes}

Pour chaque élève :
- prenom et nom (en Guinée le nom de famille est souvent écrit en dernier ou en
  MAJUSCULES : Diallo, Bah, Camara, Barry, Sow, Condé, Touré…). Ne mélange pas avec les parents.
- sexe : "M" ou "F" d'après le document ou, à défaut, un prénom sans ambiguïté
  (Mamadou, Ibrahima → M ; Aminata, Fatoumata, Mariama → F) ; "" si incertain.
- date_naissance au format AAAA-MM-JJ, "" si absente. « né vers 2015 » : "" et explique dans remarque.
- lieu_naissance : ville ou préfecture, "" si absent.
- classe_id : la classe de la liste qui correspond à la classe demandée (« 7ème année »,
  « CM1 », « 11e SL »…), null si le document n'indique pas de classe ou si aucune ne correspond.
  Un titre de liste (« Liste de la 8ème année ») vaut pour tous les élèves de la liste.
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


def _doublons(eleves_ecole, prenom: str, nom: str):
    """Élèves de l'école déjà inscrits sous un nom très proche (risque de double inscription)."""
    if not (prenom or nom):
        return []
    lu = f"{prenom} {nom}"
    trouves = []
    for e in eleves_ecole:
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
    return (
        Responsable.objects.filter(Q(telephone=numero) | Q(telephone_secondaire=numero))
        .filter(Q(eleves_principal__classe__ecole=ecole) | Q(eleves_secondaire__classe__ecole=ecole))
        .distinct()
        .first()
    )


def _ecole_ou_refus(request):
    ecole = None if user_is_admin(request.user) else user_school(request.user)
    if not user_is_admin(request.user) and ecole is None:
        return None, JsonResponse({"ok": False, "erreur": "Votre compte n'est rattaché à aucune école."}, status=403)
    return ecole, None


@login_required
@require_POST
def assistant_eleve(request):
    ecole, refus = _ecole_ou_refus(request)
    if refus:
        return refus

    classes = _classes_proposees(request)
    try:
        images = [preparer_image(f) for f in request.FILES.getlist("photos")[:4]]
        resultat = extraire_json(_consigne(classes), SCHEMA, images=images, texte=request.POST.get("texte", ""))
    except AssistantIAError as exc:
        return JsonResponse({"ok": False, "erreur": str(exc)}, status=400)

    ids_classes = {c.id for c in classes}
    eleves_ecole = (
        list(Eleve.objects.filter(classe__ecole=ecole).select_related("classe")
             .only("id", "matricule", "prenom", "nom", "statut", "classe__nom"))
        if ecole else []
    )

    eleves = []
    for lu in resultat.get("eleves", [])[:MAX_LIGNES]:
        if not (lu["prenom"] or lu["nom"]):
            continue
        # Seule une classe proposée par le formulaire peut être choisie
        if lu["classe_id"] not in ids_classes:
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

        connu = _responsable_connu(ecole, resp["telephone"])
        eleves.append({
            "lu": lu,
            "doublons": _doublons(eleves_ecole, lu["prenom"], lu["nom"]),
            "responsable_connu": (
                {"id": connu.id, "nom": f"{connu.prenom} {connu.nom}", "telephone": connu.telephone}
                if connu else None
            ),
        })

    if not eleves:
        return JsonResponse({"ok": False, "erreur": "Aucun élève n'a été lu sur ce document."}, status=400)

    logger.info("Assistant élève : %s élève(s) lu(s) par %s", len(eleves), request.user.username)
    return JsonResponse({
        "ok": True,
        "eleves": eleves,
        "classes": [[c.id, c.nom] for c in classes],
        "relations": [list(r) for r in Responsable.RELATION_CHOICES],
    })


# ---------------------------------------------------------------------------
# Validation d'un lot corrigé par l'utilisateur : import direct ou fichier Excel
# ---------------------------------------------------------------------------

def _lignes_du_corps(request):
    try:
        corps = json.loads(request.body.decode("utf-8"))
        lignes = corps.get("lignes")
        assert isinstance(lignes, list)
    except Exception:  # noqa: BLE001
        return None
    return [l for l in lignes if isinstance(l, dict)][:MAX_LIGNES]


def _t(ligne, cle) -> str:
    return str(ligne.get(cle) or "").strip()


def _enregistrer_ligne(request, ligne, classes_par_id, ecole):
    """Enregistre un élève avec les formulaires de la page « Ajouter un élève »."""
    from utilisateurs.models import JournalActivite

    from .forms import EleveForm, ResponsableForm

    classe = classes_par_id.get(_t(ligne, "classe_id"))
    if classe is None:
        raise ValueError("classe à choisir")

    donnees = {
        "prenom": _t(ligne, "prenom"), "nom": _t(ligne, "nom"), "sexe": _t(ligne, "sexe").upper(),
        "date_naissance": _t(ligne, "date_naissance"), "lieu_naissance": _t(ligne, "lieu_naissance"),
        "classe": str(classe.id), "date_inscription": date.today().isoformat(), "statut": "ACTIF",
    }

    form_resp = None
    resp_partiel = None
    resp = {c: _t(ligne, f"resp_{c}") for c in ("prenom", "nom", "relation", "telephone", "adresse")}
    telephone = _telephone_local(resp["telephone"])
    connu = _responsable_connu(ecole, telephone)
    if connu:
        donnees["responsable_principal"] = str(connu.id)
    elif any(resp.values()):
        if not telephone:
            raise ValueError("téléphone du responsable invalide (8 ou 9 chiffres) : corrigez-le ou videz ses champs")
        if all(resp[c] for c in ("prenom", "nom", "relation", "adresse")):
            form_resp = ResponsableForm(
                {f"r-{c}": v for c, v in {**resp, "telephone": telephone}.items()}, prefix="r", user=request.user
            )
            if not form_resp.is_valid():
                raise ValueError("responsable : " + _erreurs(form_resp))
        else:
            # Comme l'import Excel d'élèves : un parent connu seulement par son téléphone est
            # enregistré avec ce qui est lu ; le reste se complète plus tard sur sa fiche.
            resp_partiel = Responsable(
                telephone=f"+224{telephone}", prenom=resp["prenom"], nom=resp["nom"],
                relation=resp["relation"] if resp["relation"] in RELATIONS else "AUTRE",
                adresse=resp["adresse"],
            )

    form = EleveForm(donnees, user=request.user)
    form.fields["classe"].queryset = Classe.objects.filter(pk=classe.pk)
    if not form.is_valid():
        raise ValueError(_erreurs(form))

    with transaction.atomic():
        if form_resp:
            form.instance.responsable_principal = form_resp.save()
        elif resp_partiel:
            resp_partiel.save()
            form.instance.responsable_principal = resp_partiel
        eleve = form.save(commit=False)
        eleve.cree_par = request.user
        eleve.save()
        HistoriqueEleve.objects.create(
            eleve=eleve, action="CREATION", utilisateur=request.user,
            description=f"Création du profil de {eleve.prenom} {eleve.nom} (assistant photo)",
        )
        JournalActivite.objects.create(
            user=request.user, action="CREATION", type_objet="ELEVE", objet_id=eleve.id,
            description=f"Création de l'élève {eleve.prenom} {eleve.nom} (matricule: {eleve.matricule}) via l'assistant photo",
            adresse_ip=request.META.get("REMOTE_ADDR", ""),
            user_agent=request.META.get("HTTP_USER_AGENT", "")[:200],
        )
    return eleve


def _erreurs(form) -> str:
    morceaux = []
    for champ, erreurs in form.errors.items():
        libelle = form.fields[champ].label if champ in form.fields and form.fields[champ].label else champ
        morceaux.append(f"{libelle} : {erreurs[0]}")
    return " ; ".join(morceaux) or "données invalides"


@login_required
@require_POST
def assistant_eleves_importer(request):
    ecole, refus = _ecole_ou_refus(request)
    if refus:
        return refus
    lignes = _lignes_du_corps(request)
    if not lignes:
        return JsonResponse({"ok": False, "erreur": "Aucune ligne à importer."}, status=400)

    classes_par_id = {str(c.id): c for c in _classes_proposees(request)}
    crees, echecs = [], []
    for ligne in lignes:
        nom_affiche = f"{_t(ligne, 'prenom')} {_t(ligne, 'nom')}".strip() or "(sans nom)"
        try:
            eleve = _enregistrer_ligne(request, ligne, classes_par_id, ecole)
            crees.append({"index": ligne.get("_index"), "nom": f"{eleve.prenom} {eleve.nom}",
                          "matricule": eleve.matricule, "classe": eleve.classe.nom})
        except ValueError as exc:
            echecs.append({"index": ligne.get("_index"), "nom": nom_affiche, "erreur": str(exc)})
        except Exception:  # noqa: BLE001
            logger.exception("Assistant élèves : échec d'enregistrement de %s", nom_affiche)
            echecs.append({"index": ligne.get("_index"), "nom": nom_affiche, "erreur": "erreur à l'enregistrement"})

    logger.info("Assistant élèves : %s créé(s), %s refusé(s), par %s", len(crees), len(echecs), request.user.username)
    return JsonResponse({"ok": True, "crees": crees, "echecs": echecs})


@login_required
@require_POST
def assistant_eleves_excel(request):
    """Fichier Excel au format de l'import d'élèves (École / Classe / Année scolaire + modèle)."""
    import pandas as pd

    from .import_eleves import COLONNES_EXPORT_COMPLET
    from .views_import import _ecrire_feuille_template

    ecole, refus = _ecole_ou_refus(request)
    if refus:
        return refus
    lignes = _lignes_du_corps(request)
    if not lignes:
        return JsonResponse({"ok": False, "erreur": "Aucune ligne à exporter."}, status=400)

    classes_par_id = {str(c.id): c for c in _classes_proposees(request)}
    donnees = []
    for l in lignes:
        classe = classes_par_id.get(_t(l, "classe_id"))
        naissance = _t(l, "date_naissance")
        try:
            naissance = datetime.strptime(naissance, "%Y-%m-%d").strftime("%d/%m/%Y") if naissance else ""
        except ValueError:
            pass
        telephone = _telephone_local(_t(l, "resp_telephone"))
        donnees.append({
            "École": ecole.nom if ecole else "",
            "Classe": classe.nom if classe else "",
            "Année scolaire": classe.annee_scolaire if classe else "",
            "Matricule": "",
            "Prénom": _t(l, "prenom"), "Nom": _t(l, "nom"), "Sexe": _t(l, "sexe").upper(),
            "Date de Naissance": naissance, "Lieu de Naissance": _t(l, "lieu_naissance"),
            "Nom du Père/Tuteur": _t(l, "resp_nom"), "Prénom du Père/Tuteur": _t(l, "resp_prenom"),
            "Téléphone Principal": f"+224{telephone}" if telephone else "",
            "Adresse": _t(l, "resp_adresse"),
            "Nom de la Mère": "", "Prénom de la Mère": "", "Téléphone Secondaire": "", "Email": "",
        })

    tampon = BytesIO()
    with pd.ExcelWriter(tampon, engine="openpyxl") as writer:
        _ecrire_feuille_template(writer, pd.DataFrame(donnees, columns=COLONNES_EXPORT_COMPLET), "Élèves")
    reponse = HttpResponse(
        tampon.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    reponse["Content-Disposition"] = 'attachment; filename="eleves_assistant.xlsx"'
    return reponse
