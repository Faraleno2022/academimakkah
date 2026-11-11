"""
Outils communs aux assistants « plusieurs lignes » (bus, cantine, avances, test d'accueil…).

Principe : Claude lit le document, le serveur prépare une table de corrections
(colonnes + lignes) ; l'utilisateur corrige, puis chaque ligne validée est
enregistrée PAR LA VUE HABITUELLE du module (mêmes formulaires, mêmes contrôles).
"""

import copy
import json
import re
from io import BytesIO

from django.contrib import messages
from django.http import HttpResponse, QueryDict
from django.utils.datastructures import MultiValueDict
from django.utils.html import strip_tags

from .claude import normaliser, ressemblance

MAX_LIGNES = 100
SCORE_MIN = 0.72  # en dessous, on ne propose pas automatiquement


def lignes_du_corps(request, maximum=MAX_LIGNES):
    """Lignes validées par l'utilisateur (corps JSON {"lignes": [...]}) ; None si invalide."""
    try:
        corps = json.loads(request.body.decode("utf-8"))
        lignes = corps.get("lignes")
        assert isinstance(lignes, list)
    except Exception:  # noqa: BLE001
        return None, {}
    return [l for l in lignes if isinstance(l, dict)][:maximum], corps


def texte(ligne, cle) -> str:
    return str(ligne.get(cle) or "").strip()


def chiffres(valeur) -> str:
    """« 1 500 000 GNF » ou « 150000.0 » -> « 1500000 » / « 150000 »."""
    brut = str(valeur or "").strip()
    brut = re.split(r"[.,]\d{1,2}$", brut)[0] if re.search(r"[.,]\d{1,2}$", brut) else brut
    return re.sub(r"\D", "", brut)


def colonne(cle, titre, type_="text", options=None, largeur=120):
    c = {"cle": cle, "titre": titre, "type": type_, "largeur": largeur}
    if options is not None:
        c["options"] = [[str(v), str(l)] for v, l in options]
    return c


def erreurs_formulaire(form) -> str:
    morceaux = []
    for champ, erreurs in form.errors.items():
        libelle = form.fields[champ].label if champ in form.fields and form.fields[champ].label else ""
        morceaux.append(f"{libelle} : {erreurs[0]}" if libelle else str(erreurs[0]))
    return " ; ".join(morceaux) or "données invalides"


def trouver(candidats, nom_lu: str, libelle, bonus=None):
    """
    Cherche ``nom_lu`` parmi ``candidats`` (libelle(c) -> « Prénom Nom »).
    Renvoie (meilleur | None, [(candidat, score)] triés, 3 au plus).
    """
    if not (nom_lu or "").strip():
        return None, []
    scores = []
    for c in candidats:
        score = ressemblance(nom_lu, libelle(c))
        if bonus:
            score = min(1.0, score + bonus(c))
        if score >= 0.55:
            scores.append((c, score))
    scores.sort(key=lambda x: x[1], reverse=True)
    suggestions = scores[:3]
    meilleur = None
    if suggestions and suggestions[0][1] >= SCORE_MIN:
        # Deux candidats presque à égalité : on ne choisit pas à la place de l'utilisateur
        if len(suggestions) == 1 or suggestions[0][1] - suggestions[1][1] >= 0.05:
            meilleur = suggestions[0][0]
    return meilleur, suggestions


def trouver_eleve(eleves, nom: str, matricule: str, classe: str = ""):
    """Élève par matricule exact, sinon par nom (ordre et accents indifférents)."""
    if (matricule or "").strip():
        trouve = eleves.filter(matricule__iexact=matricule.strip()).first()
        if trouve:
            return trouve, [(trouve, 1.0)]
    classe_norm = normaliser(classe)

    def bonus(e):  # départage les homonymes par la classe lue
        return 0.05 if classe_norm and e.classe and normaliser(e.classe.nom) == classe_norm else 0.0

    return trouver(list(eleves), nom, lambda e: f"{e.prenom} {e.nom}", bonus)


def cellule_eleve(eleve, suggestions, nom_lu):
    """(matricule, début de remarque, alerte) pour une ligne de table portant sur un élève."""
    if eleve:
        classe = eleve.classe.nom if eleve.classe else ""
        return eleve.matricule, f"Élève : {eleve.prenom} {eleve.nom} ({classe}).", ""
    alerte = f"Élève « {nom_lu or 'non lu'} » non retrouvé : saisissez son matricule."
    if suggestions:
        alerte += " Suggestions : " + " ; ".join(
            f"{e.prenom} {e.nom} – {e.matricule} ({e.classe.nom if e.classe else ''})" for e, _ in suggestions
        )
    return "", "", alerte


def executer_vue(request, vue, donnees, **kwargs):
    """
    Appelle une vue de création du module avec ``donnees`` comme formulaire POST.
    Renvoie (réponse, [messages d'erreur émis par la vue]).
    """
    stock = messages.get_messages(request)
    deja = len(list(stock))
    sous_requete = copy.copy(request)
    sous_requete.method = "POST"
    post = QueryDict(mutable=True)
    post.update({cle: str(valeur) for cle, valeur in donnees.items()})
    sous_requete._post, sous_requete._files = post, MultiValueDict()
    reponse = vue(sous_requete, **kwargs)
    # Itérer le stock le marque « lu » : les messages de la vue n'iront pas sur la page suivante
    nouveaux = list(stock)[deja:]
    erreurs = [
        re.sub(r"\s+", " ", strip_tags(str(m))).strip() for m in nouveaux if m.level >= messages.ERROR
    ]
    return reponse, erreurs


def reponse_excel(nom_fichier, titre_feuille, entetes, lignes, lignes_avant=()):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    classeur = Workbook()
    feuille = classeur.active
    feuille.title = titre_feuille
    for avant in lignes_avant:
        feuille.append(list(avant))
    feuille.append(list(entetes))
    for cellule in feuille[feuille.max_row]:
        cellule.font = Font(bold=True, color="FFFFFF")
        cellule.fill = PatternFill(start_color="1746A2", end_color="1746A2", fill_type="solid")
    for ligne in lignes:
        feuille.append(list(ligne))
    for i in range(1, len(entetes) + 1):
        feuille.column_dimensions[get_column_letter(i)].width = 22

    tampon = BytesIO()
    classeur.save(tampon)
    reponse = HttpResponse(
        tampon.getvalue(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    reponse["Content-Disposition"] = f'attachment; filename="{nom_fichier}"'
    return reponse


SCHEMA_DOUTE = {
    "champs_douteux": {"type": "array", "items": {"type": "string"}},
    "remarque": {"type": "string"},
}


def schema_liste(cle, proprietes):
    """Schéma JSON « { cle: [ {proprietes + champs_douteux + remarque} ] } »."""
    proprietes = {**proprietes, **SCHEMA_DOUTE}
    return {
        "type": "object",
        "properties": {cle: {"type": "array", "items": {
            "type": "object", "properties": proprietes,
            "required": list(proprietes), "additionalProperties": False,
        }}},
        "required": [cle],
        "additionalProperties": False,
    }


NOMBRE_OU_NUL = {"anyOf": [{"type": "number"}, {"type": "null"}]}
ENTIER_OU_NUL = {"anyOf": [{"type": "integer"}, {"type": "null"}]}
REGLES_COMMUNES = """- champs_douteux : noms des champs dont tu n'es pas sûr (ex. ["montant"]).
- remarque : courte explication en français si quelque chose est incertain, sinon "".
N'invente rien : laisse "" ou null ce que tu ne lis pas clairement."""
