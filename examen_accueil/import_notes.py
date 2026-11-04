"""Import des notes du test d'accueil depuis un fichier Excel.

Le fichier attendu est le modèle téléchargé depuis la saisie (une ligne par
élève, une colonne par matière), mais toute feuille dont l'en-tête contient
« Matricule » ou « Nom » et les noms des matières convient. Les élèves sont
retrouvés par matricule, puis par nom et prénom ; les matières par leur nom,
sans tenir compte des accents, de la casse ni du « (coef. 2) » ajouté.
"""
import re
from decimal import Decimal

from notes.classes_utils import normaliser_nom_classe as normaliser

COLONNES_ELEVE = {
    'matricule': 'matricule',
    'nom': 'nom',
    'prenom': 'prenom', 'prenoms': 'prenom', 'prenom s': 'prenom',
}
LIGNES_ENTETE_MAX = 15


def _entete_matiere(texte):
    """« Français (coef. 2) » → « francais »."""
    return normaliser(re.sub(r'\(\s*coef[^)]*\)', '', str(texte or ''), flags=re.IGNORECASE))


def _texte_cellule(valeur):
    """Valeur Excel → texte lisible par la saisie (12.5 → « 12.5 », 14.0 → « 14 »)."""
    if valeur is None:
        return ''
    if isinstance(valeur, bool):
        return str(valeur)
    if isinstance(valeur, (int, float, Decimal)):
        return format(Decimal(str(valeur)).normalize(), 'f')
    return str(valeur).strip()


def lire_fichier(fichier, eleves, matieres, lire_note):
    """Lit le classeur et renvoie ``(notes, erreurs, avertissements)``.

    ``notes`` : {(eleve, matiere): Decimal} pour les cases remplies ; une case
    vide ne modifie rien. ``lire_note(texte)`` renvoie ``(valeur, erreur)``
    comme pour la saisie manuelle.
    """
    from openpyxl import load_workbook

    try:
        classeur = load_workbook(fichier, read_only=True, data_only=True)
    except Exception:
        return {}, ["Le fichier n'est pas un classeur Excel (.xlsx) lisible."], []
    try:
        lignes = [tuple(ligne) for ligne in classeur.worksheets[0].iter_rows(values_only=True)]
    finally:
        classeur.close()

    # Ligne d'en-tête : la première qui nomme l'élève (matricule ou nom).
    index_entete = next((i for i, ligne in enumerate(lignes[:LIGNES_ENTETE_MAX])
                         if {'matricule', 'nom'} & {normaliser(str(c or '')) for c in ligne}), None)
    if index_entete is None:
        return {}, ["En-tête introuvable : la feuille doit contenir une colonne « Matricule » ou « Nom »."], []

    par_matiere = {_entete_matiere(m.nom): m for m in matieres}
    colonnes_eleve, colonnes_matiere, ignorees = {}, {}, []
    for colonne, titre in enumerate(lignes[index_entete]):
        if titre in (None, ''):
            continue
        cle = normaliser(str(titre))
        if cle in COLONNES_ELEVE:
            colonnes_eleve.setdefault(COLONNES_ELEVE[cle], colonne)
        elif _entete_matiere(titre) in par_matiere:
            colonnes_matiere[colonne] = par_matiere[_entete_matiere(titre)]
        else:
            ignorees.append(str(titre).strip())
    if not colonnes_matiere:
        return {}, ["Aucune colonne ne correspond aux matières du test de cette classe."], []
    if 'matricule' not in colonnes_eleve and not {'nom', 'prenom'} <= colonnes_eleve.keys():
        return {}, ["Il faut une colonne « Matricule » ou les colonnes « Nom » et « Prénom(s) »."], []

    par_matricule = {normaliser(e.matricule): e for e in eleves if e.matricule}
    par_nom = {}
    for eleve in eleves:
        par_nom.setdefault((normaliser(eleve.nom), normaliser(eleve.prenom)), []).append(eleve)

    def valeur(ligne, champ):
        colonne = colonnes_eleve.get(champ)
        return _texte_cellule(ligne[colonne]) if colonne is not None and colonne < len(ligne) else ''

    notes, erreurs, deja_vus = {}, [], set()
    for numero, ligne in enumerate(lignes[index_entete + 1:], start=index_entete + 2):
        matricule, nom, prenom = valeur(ligne, 'matricule'), valeur(ligne, 'nom'), valeur(ligne, 'prenom')
        cases = {matiere: _texte_cellule(ligne[colonne]) if colonne < len(ligne) else ''
                 for colonne, matiere in colonnes_matiere.items()}
        if not (matricule or nom or prenom):
            continue
        libelle = f"Ligne {numero} ({' '.join(t for t in (nom, prenom) if t) or matricule})"
        eleve = par_matricule.get(normaliser(matricule)) if matricule else None
        if eleve is None:
            homonymes = par_nom.get((normaliser(nom), normaliser(prenom)), [])
            if len(homonymes) > 1:
                erreurs.append(f"{libelle} : plusieurs élèves portent ce nom, indiquez le matricule.")
                continue
            eleve = homonymes[0] if homonymes else None
        if eleve is None:
            erreurs.append(f"{libelle} : élève introuvable dans cette classe.")
            continue
        if eleve.pk in deja_vus:
            erreurs.append(f"{libelle} : {eleve.nom_complet} apparaît plusieurs fois.")
            continue
        deja_vus.add(eleve.pk)
        for matiere, texte in cases.items():
            note, erreur = lire_note(texte)
            if erreur:
                erreurs.append(f"{libelle} - {matiere.nom} : {erreur}")
            elif note is not None:
                notes[(eleve, matiere)] = note

    avertissements = []
    if ignorees:
        avertissements.append("Colonnes ignorées (matière inconnue) : " + ", ".join(ignorees) + ".")
    return notes, erreurs, avertissements
