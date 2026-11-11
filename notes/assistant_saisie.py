"""
Assistant IA pour la saisie intelligente des notes.

L'utilisateur envoie la photo d'une fiche de notes (ou un texte dicté/collé) ;
Claude fait correspondre chaque ligne aux élèves et matières de la classe et
renvoie les notes. La page pré-remplit la grille ; rien n'est enregistré tant
que l'utilisateur ne clique pas sur « Enregistrer toutes les notes ».
"""

import logging

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_POST

from assistant_ia.claude import AssistantIAError, extraire_json, preparer_image
from utilisateurs.permissions import has_permission

from .models import ClasseNote, MatiereNote

logger = logging.getLogger(__name__)

SCHEMA = {
    "type": "object",
    "properties": {
        "notes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "eleve_id": {"type": "integer"},
                    "matiere_id": {"type": "integer"},
                    "note": {"anyOf": [{"type": "number"}, {"type": "null"}]},
                    "absent": {"type": "boolean"},
                    "douteux": {"type": "boolean"},
                    "remarque": {"type": "string"},
                },
                "required": ["eleve_id", "matiere_id", "note", "absent", "douteux", "remarque"],
                "additionalProperties": False,
            },
        },
        "non_reconnus": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["notes", "non_reconnus"],
    "additionalProperties": False,
}


def _eleves_de_la_classe(classe_note):
    """Mêmes règles que la page de saisie intelligente pour retrouver les élèves."""
    from eleves.models import Classe as ClasseEleve, Eleve

    classe_eleve = (
        ClasseEleve.objects.filter(
            nom=classe_note.nom,
            annee_scolaire=classe_note.annee_scolaire,
            ecole=classe_note.ecole,
        ).first()
        or ClasseEleve.objects.filter(
            nom__iexact=classe_note.nom, annee_scolaire=classe_note.annee_scolaire
        ).first()
    )
    if not classe_eleve:
        return []
    return list(Eleve.pedagogiques.filter(classe=classe_eleve, statut="ACTIF").order_by("prenom", "nom"))


def _eleves_de_la_page(request, classe_note):
    """
    Élèves affichés par la page qui appelle l'assistant (« Saisir les notes » a ses propres
    règles de correspondance de classe). Restreints aux élèves actifs de l'école de la classe.
    """
    from eleves.models import Eleve

    ids = [int(x) for x in request.POST.get("eleve_ids", "").split(",") if x.strip().isdigit()][:400]
    if not ids:
        return []
    return list(
        Eleve.pedagogiques.filter(pk__in=ids, statut="ACTIF", classe__ecole=classe_note.ecole)
        .order_by("prenom", "nom")
    )


def _consigne(eleves, matieres, note_max, matiere_ciblee) -> str:
    liste_eleves = "\n".join(f"{e.id} | {e.prenom} {e.nom} | matricule {e.matricule}" for e in eleves)
    liste_matieres = "\n".join(f"{m.id} | {m.nom} | code {m.code or '-'}" for m in matieres)
    cible = (
        f"\nL'utilisateur indique que le document concerne la matière : {matiere_ciblee.id} | {matiere_ciblee.nom}. "
        "Si aucune colonne de matière n'est visible, attribue toutes les notes à cette matière."
        if matiere_ciblee
        else ""
    )
    return f"""Tu aides une école guinéenne à saisir des notes dans MySchoolGN.
On te donne la photo d'une fiche de notes (souvent manuscrite) ou un texte, et tu dois
relever chaque note pour les élèves et matières de la classe ci-dessous.

Élèves de la classe (id | prénom nom | matricule) :
{liste_eleves}

Matières de la classe (id | nom | code) :
{liste_matieres}

Note maximale : {note_max}.{cible}

Règles :
- Associe chaque nom lu à l'élève de la liste qui correspond (l'ordre prénom/nom, les
  accents ou une faute d'orthographe peuvent varier). Utilise uniquement les id ci-dessus.
- Associe chaque colonne à une matière de la liste d'après son en-tête (nom ou abréviation).
- Virgule ou point décimal : « 12,5 » = 12.5.
- « Abs », « A » ou une case marquée absent : absent=true et note=null.
- N'invente rien. Une case vide ou illisible : ne la mets pas, ou mets douteux=true si tu as
  un doute sur un chiffre (et explique dans remarque).
- douteux=true aussi si la note dépasse {note_max}, si l'élève ou la matière est incertain.
- Un nom qui ne correspond à aucun élève va dans non_reconnus (avec la note lue), pas dans notes.
- remarque : vide si tout est clair, sinon une courte explication en français."""


@login_required
@require_POST
def assistant_notes(request):
    if not has_permission(request.user, "peut_gerer_notes"):
        return JsonResponse({"ok": False, "erreur": "Vous n'avez pas la permission de gérer les notes."}, status=403)

    from utilisateurs.utils import user_school
    from .calculs_moyennes import detecter_niveau_scolaire

    ecole = None if request.user.is_superuser else user_school(request.user)
    classe_id = request.POST.get("classe_id")
    if not classe_id:
        return JsonResponse({"ok": False, "erreur": "Classe manquante."}, status=400)
    if ecole:
        classe = get_object_or_404(ClasseNote, pk=classe_id, ecole=ecole)
    else:
        classe = get_object_or_404(ClasseNote, pk=classe_id)

    matieres = list(MatiereNote.objects.filter(classe=classe, actif=True).order_by("nom"))
    eleves = _eleves_de_la_page(request, classe) or _eleves_de_la_classe(classe)
    if not matieres or not eleves:
        return JsonResponse({"ok": False, "erreur": "Cette classe n'a pas d'élèves ou de matières."}, status=400)
    note_max = 10 if detecter_niveau_scolaire(classe.nom) == "PRIMAIRE" else 20

    matiere_ciblee = None
    if request.POST.get("matiere_id"):
        matiere_ciblee = next((m for m in matieres if str(m.id) == request.POST["matiere_id"]), None)
    # Page « Saisir les notes » : une seule matière à l'écran, on n'en propose pas d'autre
    if matiere_ciblee and request.POST.get("matiere_seule"):
        matieres = [matiere_ciblee]

    try:
        images = [preparer_image(f) for f in request.FILES.getlist("photos")[:4]]
        resultat = extraire_json(
            _consigne(eleves, matieres, note_max, matiere_ciblee),
            SCHEMA,
            images=images,
            texte=request.POST.get("texte", ""),
        )
    except AssistantIAError as exc:
        return JsonResponse({"ok": False, "erreur": str(exc)}, status=400)

    # Contrôle côté serveur : on ne garde que des élèves et matières de cette classe
    ids_eleves = {e.id for e in eleves}
    ids_matieres = {m.id for m in matieres}
    notes = []
    for n in resultat.get("notes", []):
        if n["eleve_id"] not in ids_eleves or n["matiere_id"] not in ids_matieres:
            continue
        note = n["note"]
        if note is not None and not (0 <= note <= note_max):
            n["douteux"] = True
            n["remarque"] = (n["remarque"] + f" Note hors barème (max {note_max}).").strip()
        notes.append(n)

    logger.info(
        "Assistant notes : %s notes lues pour la classe %s par %s",
        len(notes), classe.pk, request.user.username,
    )
    return JsonResponse({"ok": True, "notes": notes, "non_reconnus": resultat.get("non_reconnus", [])})


@login_required
@require_POST
def assistant_notes_excel(request):
    """
    Fichier Excel des notes lues, au format de « l'import intelligent »
    (N° | Matricule | Prénoms | Nom | Sexe | une colonne par matière).
    """
    import json
    from io import BytesIO

    from django.http import HttpResponse
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    from eleves.models import Eleve
    from utilisateurs.utils import user_school
    from .calculs_moyennes import detecter_niveau_scolaire
    from .import_intelligent import _get_periode_label

    if not has_permission(request.user, "peut_gerer_notes"):
        return JsonResponse({"ok": False, "erreur": "Vous n'avez pas la permission de gérer les notes."}, status=403)
    try:
        corps = json.loads(request.body.decode("utf-8"))
        notes = [n for n in corps.get("notes", []) if isinstance(n, dict)][:5000]
        classe_id = int(corps.get("classe_id"))
    except Exception:  # noqa: BLE001
        return JsonResponse({"ok": False, "erreur": "Demande invalide."}, status=400)

    ecole = None if request.user.is_superuser else user_school(request.user)
    filtre = {"pk": classe_id, **({"ecole": ecole} if ecole else {})}
    classe = get_object_or_404(ClasseNote, **filtre)
    matieres = {m.id: m for m in MatiereNote.objects.filter(classe=classe, actif=True)}

    valeurs = {}  # (eleve_id, matiere_id) -> note
    for n in notes:
        try:
            eleve_id, matiere_id, note = int(n["eleve_id"]), int(n["matiere_id"]), float(str(n["note"]).replace(",", "."))
        except (KeyError, TypeError, ValueError):
            continue
        if matiere_id in matieres:
            valeurs[(eleve_id, matiere_id)] = note
    if not valeurs:
        return JsonResponse({"ok": False, "erreur": "Aucune note à exporter."}, status=400)

    eleves = list(
        Eleve.pedagogiques.filter(pk__in={e for e, _ in valeurs}, classe__ecole=classe.ecole).order_by("prenom", "nom")
    )
    colonnes = sorted({matieres[m] for _, m in valeurs}, key=lambda m: m.nom)
    note_max = 10 if detecter_niveau_scolaire(classe.nom) == "PRIMAIRE" else 20
    periode = str(corps.get("periode") or "")

    classeur = Workbook()
    feuille = classeur.active
    feuille.title = "Notes"
    # Deux lignes d'information, comme le modèle : l'import saute les 2 premières lignes
    feuille.append([f"FICHE DE REPORT DES NOTES - {classe.nom} - {_get_periode_label(periode)} - Notes sur {note_max}"])
    feuille.append(["Légende: " + " | ".join(f"{m.code or m.nom[:10]}: {m.nom}" for m in colonnes)])
    feuille.append(["N°", "Matricule", "Prénoms", "Nom", "Sexe"] + [m.code or m.nom[:15] for m in colonnes])
    for cellule in feuille[3]:
        cellule.font = Font(bold=True, color="FFFFFF")
        cellule.fill = PatternFill(start_color="1a5276", end_color="1a5276", fill_type="solid")
    for i, e in enumerate(eleves, start=1):
        feuille.append([i, e.matricule, e.prenom, e.nom, e.sexe or ""] + [valeurs.get((e.id, m.id), "") for m in colonnes])
    feuille["A1"].font = Font(bold=True, size=12, color="1a5276")
    for lettre, largeur in (("A", 5), ("B", 18), ("C", 18), ("D", 16), ("E", 6)):
        feuille.column_dimensions[lettre].width = largeur
    feuille.freeze_panes = "A4"

    tampon = BytesIO()
    classeur.save(tampon)
    reponse = HttpResponse(
        tampon.getvalue(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    reponse["Content-Disposition"] = 'attachment; filename="notes_assistant.xlsx"'
    return reponse
