"""Moyennes et classement du test d'accueil d'une classe.

Calcul autonome : il ne lit que les modèles de ce module et n'appelle aucune
fonction du module ``notes`` (moyennes mensuelles, compositions, rangs).
Seuls les élèves ayant au moins une note au test sont classés ; pour eux, comme
ailleurs dans MySchoolGN, une matière non notée compte 0 afin qu'un élève
absent à une épreuve ne soit pas favorisé.
"""
from decimal import ROUND_HALF_UP, Decimal

from eleves.models import Eleve

from .models import MatiereAccueil, NoteAccueil, ParametreAccueil

DEUX_DECIMALES = Decimal('0.01')


def arrondir(valeur):
    return valeur.quantize(DEUX_DECIMALES, rounding=ROUND_HALF_UP)


def format_rang(rang, ex_aequo=False, sexe=''):
    if rang is None:
        return '-'
    if rang == 1:
        texte = '1ère' if sexe == 'F' else '1er'
    else:
        texte = f'{rang}ème'
    return f'{texte} ex' if ex_aequo else texte


# Échelles du module Notes (calculs_moyennes.obtenir_mention_intelligente et
# EvaluationMaternelle), recopiées pour garder le calcul autonome ; un test
# vérifie qu'elles restent identiques.
MENTIONS_SECONDAIRE = [(18, 'Excellent'), (16, 'Très Bien'), (14, 'Bien'), (12, 'Assez Bien'),
                       (10, 'Passable'), (8, 'Insuffisant'), (6, 'Faible')]
MENTIONS_PRIMAIRE = [(9, 'Excellent'), (8, 'Très Bien'), (7, 'Bien'), (6, 'Assez Bien'),
                     (5, 'Passable'), (4, 'Insuffisant'), (3, 'Faible')]
LETTRES_MATERNELLE = [(Decimal('10'), 'A+'), (Decimal('9.5'), 'A'), (Decimal('8'), 'B+'), (Decimal('7'), 'B'),
                      (Decimal('6'), 'B-'), (Decimal('5'), 'C')]
MENTIONS_LETTRES = {'A+': 'Excellent', 'A': 'Très bien', 'B+': 'Bien', 'B': 'Assez bien', 'B-': 'Moyen',
                    'C': 'Passable', 'D': 'Éprouve des difficultés'}


def _sur(valeur, note_sur, reference):
    """Ramène une note du barème de la classe au barème de référence du niveau."""
    return valeur * Decimal(reference) / Decimal(note_sur)


def lettre_maternelle(valeur, note_sur=10):
    if valeur is None:
        return ''
    sur_10 = _sur(valeur, note_sur, 10)
    return next((lettre for seuil, lettre in LETTRES_MATERNELLE if sur_10 >= seuil), 'D')


def mention(moyenne, niveau, note_sur):
    """Mention du niveau (maternelle, primaire, secondaire), au barème de la classe."""
    if moyenne is None:
        return ''
    if niveau == 'MATERNELLE':
        return MENTIONS_LETTRES[lettre_maternelle(moyenne, note_sur)]
    reference, echelle = (10, MENTIONS_PRIMAIRE) if niveau == 'PRIMAIRE' else (20, MENTIONS_SECONDAIRE)
    valeur = _sur(moyenne, note_sur, reference)
    return next((texte for seuil, texte in echelle if valeur >= seuil), 'Très faible')


def eleves_participants(classe):
    """Élèves (de la classe ou partis depuis) ayant au moins une note au test de la classe."""
    return (Eleve.objects.filter(notes_test_accueil__matiere__classe=classe)
            .distinct().order_by('nom', 'prenom'))


def calculer_resultats(classe, parametre=None):
    """Résultats des élèves évalués au test d'accueil de ``classe``, triés par rang.

    Chaque ligne : eleve, détails par matière (note, coefficient, points,
    lettre en maternelle), total des points, moyenne, rang, ex aequo, admis,
    mention et appréciation (lettre et mention en maternelle). En maternelle
    et au primaire, chaque matière compte pour 1 quel que soit son coefficient.
    """
    parametre = parametre or ParametreAccueil.pour(classe)
    niveau, note_sur = parametre.niveau, parametre.note_sur
    matieres = list(MatiereAccueil.objects.filter(classe=classe))
    if not parametre.avec_coefficients:
        for matiere in matieres:
            matiere.coefficient = Decimal('1')
    eleves = list(eleves_participants(classe))
    notes = {
        (eleve_id, matiere_id): note
        for eleve_id, matiere_id, note in NoteAccueil.objects
        .filter(matiere__classe=classe).values_list('eleve_id', 'matiere_id', 'note')
    }
    total_coefficients = sum((m.coefficient for m in matieres), Decimal('0'))

    resultats = []
    for eleve in eleves:
        details = []
        total_points = Decimal('0')
        nb_notes = 0
        for matiere in matieres:
            note = notes.get((eleve.id, matiere.id))
            if note is not None:
                nb_notes += 1
            points = (note or Decimal('0')) * matiere.coefficient
            total_points += points
            details.append({'matiere': matiere, 'note': note, 'coefficient': matiere.coefficient,
                            'points': arrondir(points),
                            'lettre': lettre_maternelle(note, note_sur) if niveau == 'MATERNELLE' else ''})
        moyenne = arrondir(total_points / total_coefficients) if total_coefficients else None
        texte = mention(moyenne, niveau, note_sur)
        lettre = lettre_maternelle(moyenne, note_sur) if niveau == 'MATERNELLE' else ''
        resultats.append({
            'eleve': eleve,
            'details': details,
            'nb_notes': nb_notes,
            'total_points': arrondir(total_points),
            'total_coefficients': total_coefficients,
            'moyenne': moyenne,
            'admis': moyenne is not None and moyenne >= parametre.moyenne_admission,
            'mention': texte,
            'lettre': lettre,
            'appreciation': f"{lettre} - {texte}" if lettre else texte,
        })

    _attribuer_rangs(resultats)
    resultats.sort(key=lambda r: (r['rang'] is None, r['rang'] or 0,
                                  r['eleve'].nom.upper(), r['eleve'].prenom.upper()))
    return resultats


def _attribuer_rangs(resultats):
    classes = sorted((r for r in resultats if r['moyenne'] is not None),
                     key=lambda r: r['moyenne'], reverse=True)
    precedent, rang = None, 0
    for position, ligne in enumerate(classes, start=1):
        if ligne['moyenne'] != precedent:
            rang, precedent = position, ligne['moyenne']
        ligne['rang'] = rang
    effectifs = {}
    for ligne in classes:
        effectifs[ligne['rang']] = effectifs.get(ligne['rang'], 0) + 1
    for ligne in resultats:
        ligne.setdefault('rang', None)
        ligne['ex_aequo'] = ligne['rang'] is not None and effectifs.get(ligne['rang'], 0) > 1
        ligne['rang_texte'] = format_rang(ligne['rang'], ligne['ex_aequo'], ligne['eleve'].sexe)


def statistiques(resultats):
    moyennes = [r['moyenne'] for r in resultats if r['moyenne'] is not None]
    admis = sum(1 for r in resultats if r['admis'])
    return {
        'effectif': len(resultats),
        'admis': admis,
        'non_admis': len(resultats) - admis,
        'taux_admission': arrondir(Decimal(admis * 100) / Decimal(len(resultats))) if resultats else Decimal('0'),
        'moyenne_classe': arrondir(sum(moyennes) / len(moyennes)) if moyennes else None,
        'plus_forte': max(moyennes) if moyennes else None,
        'plus_faible': min(moyennes) if moyennes else None,
    }
