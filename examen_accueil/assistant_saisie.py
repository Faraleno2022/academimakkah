"""
Assistant IA de la saisie des notes du test d'accueil.

Claude lit la fiche de notes (photo ou texte) et renvoie les notes pour les élèves et les
matières de la classe ; la page pré-remplit la grille. L'enregistrement reste le bouton
« Enregistrer les notes » (ou « Valider et enregistrer » dans la fenêtre, qui l'actionne).
"""

import json
import logging

from django.http import JsonResponse
from django.views.decorators.http import require_POST

from assistant_ia import lot
from assistant_ia.claude import AssistantIAError, extraire_json, preparer_image
from notes.assistant_saisie import SCHEMA
from utilisateurs.permissions import can_manage_notes

from .models import ParametreAccueil
from .views import _classe_choisie, eleves_a_evaluer

logger = logging.getLogger(__name__)


def _consigne(eleves, matieres, note_sur, matiere_ciblee) -> str:
    liste_eleves = "\n".join(f"{e.id} | {e.prenom} {e.nom} | matricule {e.matricule}" for e in eleves)
    liste_matieres = "\n".join(f"{m.id} | {m.nom}" for m in matieres)
    cible = (
        f"\nL'utilisateur indique que le document concerne la matière : {matiere_ciblee.id} | {matiere_ciblee.nom}. "
        "Si aucune colonne de matière n'est visible, attribue toutes les notes à cette matière."
        if matiere_ciblee else ""
    )
    return f"""Tu aides une école guinéenne à saisir les notes du TEST D'ACCUEIL (test d'entrée des
nouveaux élèves) dans MySchoolGN. On te donne la photo d'une fiche de notes, souvent manuscrite,
ou un texte, et tu dois relever chaque note pour les élèves et matières ci-dessous.

Élèves (id | prénom nom | matricule) :
{liste_eleves}

Matières du test (id | nom) :
{liste_matieres}

Note maximale : {note_sur}.{cible}

Règles :
- Associe chaque nom lu à l'élève de la liste qui correspond (l'ordre prénom/nom, les accents ou
  une faute d'orthographe peuvent varier). Utilise uniquement les id ci-dessus.
- Associe chaque colonne à une matière de la liste d'après son en-tête (nom ou abréviation).
- Virgule ou point décimal : « 12,5 » = 12.5.
- « Abs » ou une case marquée absent : absent=true et note=null.
- N'invente rien. Une case vide ou illisible : ne la mets pas, ou mets douteux=true si tu as un
  doute sur un chiffre (et explique dans remarque).
- douteux=true aussi si la note dépasse {note_sur}, si l'élève ou la matière est incertain.
- Un nom qui ne correspond à aucun élève va dans non_reconnus (avec la note lue), pas dans notes.
- remarque : vide si tout est clair, sinon une courte explication en français."""


@can_manage_notes
@require_POST
def assistant_notes_accueil(request):
    classe = _classe_choisie(request)
    if classe is None:
        return JsonResponse({"ok": False, "erreur": "Classe manquante."}, status=400)
    tous = request.POST.get("tous") == "1"
    eleves = list(eleves_a_evaluer(classe, tous))
    matieres = list(classe.matieres_test_accueil.all())
    if not eleves or not matieres:
        return JsonResponse({"ok": False, "erreur": "Cette classe n'a pas d'élèves ou de matières pour le test."}, status=400)
    note_sur = ParametreAccueil.pour(classe).note_sur
    matiere_ciblee = next((m for m in matieres if str(m.id) == request.POST.get("matiere_id", "")), None)

    try:
        images = [preparer_image(f) for f in request.FILES.getlist("photos")[:4]]
        resultat = extraire_json(
            _consigne(eleves, matieres, note_sur, matiere_ciblee), SCHEMA,
            images=images, texte=request.POST.get("texte", ""),
        )
    except AssistantIAError as exc:
        return JsonResponse({"ok": False, "erreur": str(exc)}, status=400)

    ids_eleves, ids_matieres = {e.id for e in eleves}, {m.id for m in matieres}
    notes = []
    for n in resultat.get("notes", []):
        if n["eleve_id"] not in ids_eleves or n["matiere_id"] not in ids_matieres:
            continue
        if n["note"] is not None and not (0 <= n["note"] <= note_sur):
            n["douteux"] = True
            n["remarque"] = (n["remarque"] + f" Note hors barème (max {note_sur}).").strip()
        notes.append(n)

    logger.info("Assistant test d'accueil : %s notes lues pour la classe %s par %s",
                len(notes), classe.pk, request.user.username)
    return JsonResponse({"ok": True, "notes": notes, "non_reconnus": resultat.get("non_reconnus", [])})


@can_manage_notes
@require_POST
def assistant_notes_accueil_excel(request):
    """Excel des notes lues, importable par « Importer » de la saisie du test d'accueil."""
    try:
        corps = json.loads(request.body.decode("utf-8"))
        notes = [n for n in corps.get("notes", []) if isinstance(n, dict)][:5000]
    except Exception:  # noqa: BLE001
        return JsonResponse({"ok": False, "erreur": "Demande invalide."}, status=400)
    request.POST = request.POST.copy()
    request.POST["classe"] = str(corps.get("classe") or "")
    classe = _classe_choisie(request)
    if classe is None:
        return JsonResponse({"ok": False, "erreur": "Classe manquante."}, status=400)

    matieres = {m.id: m for m in classe.matieres_test_accueil.all()}
    eleves = {e.id: e for e in eleves_a_evaluer(classe, tous=True)}
    valeurs = {}
    for n in notes:
        try:
            eleve_id, matiere_id = int(n["eleve_id"]), int(n["matiere_id"])
            note = float(str(n["note"]).replace(",", "."))
        except (KeyError, TypeError, ValueError):
            continue
        if eleve_id in eleves and matiere_id in matieres:
            valeurs[(eleve_id, matiere_id)] = note
    if not valeurs:
        return JsonResponse({"ok": False, "erreur": "Aucune note à exporter."}, status=400)

    colonnes = [m for m in matieres.values() if any(mid == m.id for _, mid in valeurs)]
    lignes_eleves = sorted({eleves[e] for e, _ in valeurs}, key=lambda e: (e.nom, e.prenom))
    return lot.reponse_excel(
        "test_accueil_assistant.xlsx", "Notes",
        ["Matricule", "Nom", "Prénom(s)"] + [m.nom for m in colonnes],
        [[e.matricule, e.nom, e.prenom] + [valeurs.get((e.id, m.id)) for m in colonnes] for e in lignes_eleves],
        lignes_avant=[[f"TEST D'ACCUEIL - {classe.nom} ({classe.annee_scolaire}) - notes lues par l'assistant"], []],
    )
