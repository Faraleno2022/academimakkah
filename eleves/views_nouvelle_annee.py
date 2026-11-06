"""
Vue pour démarrer une nouvelle année scolaire :
- Duplique les classes (eleves + notes), matières, grilles tarifaires
- Fait passer automatiquement les élèves admis en classe supérieure
  avec détection INTELLIGENTE de la classe correspondante
- Permet de revenir à une année scolaire passée (via session)
"""

import logging
import re
import unicodedata
from typing import Optional, List, Tuple

from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.db.models import Count

from decimal import Decimal
from datetime import date as _date_type, timedelta
from dateutil.relativedelta import relativedelta

from .models import Ecole, Classe, Eleve, HistoriqueEleve
from utilisateurs.utils import user_school, user_is_admin
from .utils_annee import get_annee_active, get_statut_creation_nouvelle_annee, SESSION_ANNEE_ACTIVE

logger = logging.getLogger(__name__)

# Élèves présents en classe : un élève importé verrouillé en attente de son
# premier paiement (ATTENTE_PAIEMENT) passe lui aussi dans la nouvelle année,
# sinon il resterait seul dans l'année écoulée.
STATUTS_EN_CLASSE = ('ACTIF', 'ATTENTE_PAIEMENT')


# ══════════════════════════════════════════════════════════════════════════════
#  UTILITAIRE : MOYENNE ANNUELLE POUR PASSAGE
# ══════════════════════════════════════════════════════════════════════════════

def _get_moyenne_annuelle(eleve, annee_scolaire):
    """Retourne la meilleure estimation de la moyenne annuelle d'un élève.

    Ordre de priorité :
    1. Classement avec période ANNUEL_TRIM ou ANNUEL_SEM
    2. Moyenne des classements trimestriels/semestriels
    3. Dernier classement disponible (TRIMESTRE_3, SEMESTRE_2, etc.)
    4. None si aucune donnée

    Retourne (moyenne: float|None, sur: int) — sur=10 pour primaire, 20 sinon.
    """
    from notes.models import Classement, ClasseNote
    from notes.calculs_moyennes import detecter_niveau_scolaire

    # Détecter le niveau
    niveau_scolaire = 'COLLEGE'
    try:
        classe_nom = eleve.classe.nom if eleve.classe else ''
        niveau_scolaire = detecter_niveau_scolaire(classe_nom)
    except Exception:
        pass

    if niveau_scolaire == 'MATERNELLE':
        return None, 10  # Pas de notes numériques

    sur = 10 if niveau_scolaire == 'PRIMAIRE' else 20

    # Chercher les classements de cet élève pour l'année
    classements = Classement.objects.filter(
        eleve=eleve,
        annee_scolaire=annee_scolaire,
    ).order_by('-date_calcul')

    if not classements.exists():
        return None, sur

    # 1) Classement annuel
    for c in classements:
        if 'ANNUEL' in c.periode.upper():
            return float(c.moyenne_generale), sur

    # 2) Moyenne des trimestres/semestres
    periodes_finales = ['TRIMESTRE_1', 'TRIMESTRE_2', 'TRIMESTRE_3',
                        'SEMESTRE_1', 'SEMESTRE_2']
    notes_periodes = []
    for c in classements:
        if c.periode in periodes_finales:
            notes_periodes.append(float(c.moyenne_generale))
    if notes_periodes:
        return round(sum(notes_periodes) / len(notes_periodes), 2), sur

    # 3) Dernier classement disponible
    return float(classements[0].moyenne_generale), sur


def _est_admis(moyenne, sur):
    """Vérifie si une moyenne est suffisante pour passer.
    Seuil: 5/10 pour primaire, 10/20 pour secondaire.
    """
    if moyenne is None:
        return False
    seuil = 5.0 if sur == 10 else 10.0
    return moyenne >= seuil


def _admis_au_passage(moyenne, sur):
    """Admission proposée au passage. Sans aucun classement (« non évalué »),
    l'élève passe comme en maternelle : sinon une école qui ne calcule pas
    ses classements ferait redoubler tous ses élèves. La direction peut le
    retenir en décochant sa case dans l'aperçu."""
    return moyenne is None or _est_admis(moyenne, sur)


# ══════════════════════════════════════════════════════════════════════════════
#  GESTION DES ANNÉES SCOLAIRES
# ══════════════════════════════════════════════════════════════════════════════

@login_required
def gestion_annees(request):
    """Liste toutes les années scolaires et permet de basculer l'année active."""
    ecole = user_school(request.user)
    if not ecole:
        messages.error(request, "Aucune école associée à votre compte.")
        return redirect('eleves:liste_eleves')

    from django.db.models import Q
    annees_qs = (Classe.objects
                 .filter(ecole=ecole)
                 .values('annee_scolaire')
                 .annotate(
                     nb_classes=Count('id', distinct=True),
                     nb_eleves=Count('eleves', filter=Q(eleves__statut__in=STATUTS_EN_CLASSE)),
                 )
                 .order_by('-annee_scolaire'))

    annee_active = get_annee_active(request, ecole)
    annees_connues = {a['annee_scolaire'] for a in annees_qs}

    annees = []
    for a in annees_qs:
        annees.append({
            'annee': a['annee_scolaire'],
            'nb_classes': a['nb_classes'],
            'nb_eleves': a['nb_eleves'],
            'est_active': a['annee_scolaire'] == annee_active,
            'est_recente': a == annees_qs[0],
            'passage_a_completer': (a['nb_eleves'] > 0
                and _annee_suivante(a['annee_scolaire']) in annees_connues),
        })

    context = {
        'ecole': ecole,
        'annees': annees,
        'annee_active': annee_active,
        'nouvelle_annee_status': get_statut_creation_nouvelle_annee(ecole),
        'titre_page': 'Gestion des années scolaires',
    }
    return render(request, 'eleves/gestion_annees.html', context)


@login_required
def changer_annee_active(request):
    """Bascule l'année scolaire active en session."""
    if request.method != 'POST':
        return redirect('eleves:gestion_annees')

    ecole = user_school(request.user)
    if not ecole:
        messages.error(request, "Aucune école associée à votre compte.")
        return redirect('eleves:liste_eleves')

    annee = request.POST.get('annee', '').strip()
    if not annee:
        messages.error(request, "Année scolaire manquante.")
        return redirect('eleves:gestion_annees')

    # Vérifier que cette année existe bien pour cette école
    existe = Classe.objects.filter(ecole=ecole, annee_scolaire=annee).exists()
    if not existe:
        messages.error(request, f"L'année {annee} n'existe pas pour cet établissement.")
        return redirect('eleves:gestion_annees')

    request.session[SESSION_ANNEE_ACTIVE] = annee
    messages.success(request, f"Vous consultez maintenant l'année scolaire {annee}.")
    return redirect('eleves:gestion_annees')


# ══════════════════════════════════════════════════════════════════════════════
#  SYSTÈME INTELLIGENT DE PROGRESSION DES CLASSES
# ══════════════════════════════════════════════════════════════════════════════

def _normaliser(texte: str) -> str:
    """Normalise un nom de classe : MAJUSCULES, sans accents, sans parenthèses.

    Exemples:
        '10ÈME ANNÉE (A)' → '10EME ANNEE A'
        'Petite Section B'  → 'PETITE SECTION B'
        '11 Série Scientifique (B)' → '11 SERIE SCIENTIFIQUE B'
    """
    texte = texte.upper().strip()
    # Retirer les accents
    texte = unicodedata.normalize('NFD', texte)
    texte = ''.join(c for c in texte if unicodedata.category(c) != 'Mn')
    # Retirer les parenthèses
    texte = texte.replace('(', '').replace(')', '')
    # Normaliser les espaces multiples
    texte = ' '.join(texte.split())
    return texte


def _extraire_base_et_lettre(nom_classe: str) -> Tuple[str, Optional[str]]:
    """Extraire le nom de base normalisé et le suffixe lettre (A, B, C…).

    Exemples:
        '10ÈME ANNÉE (A)'             → ('10EME ANNEE', 'A')
        'PETITE SECTION B'             → ('PETITE SECTION', 'B')
        'GARDERIE'                     → ('GARDERIE', None)
        '11 SÉRIE SCIENTIFIQUE (B)'    → ('11 SERIE SCIENTIFIQUE', 'B')
        '12 SCIENCES MATHS'            → ('12 SCIENCES MATHS', None)
        'TERMINALE SCIENCES SOCIALES'  → ('TERMINALE SCIENCES SOCIALES', None)
    """
    nom = _normaliser(nom_classe)
    # Un suffixe lettre = une seule lettre A-Z isolée en fin de chaîne
    match = re.match(r'^(.+?)\s+([A-Z])$', nom)
    if match:
        return match.group(1).strip(), match.group(2)
    return nom, None


# ── Mapping de progression (toutes les clés sont normalisées) ─────────────────
# None = classe terminale → l'élève reste dans la même classe (nouvelle année)
PROGRESSION_CLASSES = {
    # ── Maternelle ──────────────────────────────────────────────
    'TOUT PETITE SECTION':   'PETITE SECTION',
    'TOUTE PETITE SECTION':  'PETITE SECTION',
    'TPS':                   'PS',
    'PETITE SECTION':        'MOYENNE SECTION',
    'PS':                    'MS',
    'MOYENNE SECTION':       'GRANDE SECTION',
    'MS':                    'GS',
    'GRANDE SECTION':        '1ERE ANNEE',      # passage automatique vers primaire
    'GS':                    '1ERE ANNEE',

    # ── Primaire (1ère → 6ème) ──────────────────────────────────
    '1ERE ANNEE':  '2EME ANNEE',
    '2EME ANNEE':  '3EME ANNEE',
    '3EME ANNEE':  '4EME ANNEE',
    '4EME ANNEE':  '5EME ANNEE',
    '5EME ANNEE':  '6EME ANNEE',
    '6EME ANNEE':  '7EME ANNEE',

    # ── Collège (7ème → 10ème) ──────────────────────────────────
    '7EME ANNEE':   '8EME ANNEE',
    '8EME ANNEE':   '9EME ANNEE',
    '9EME ANNEE':  '10EME ANNEE',
    '10EME ANNEE': '11EME ANNEE',

    # ── Lycée 11ème → 12ème ─────────────────────────────────────
    '11EME ANNEE':           '12EME ANNEE',
    '11 SERIE LITTERAIRE':   '12 SCIENCES SOCIALES',
    '11 SERIE SCIENTIFIQUE': '12 SCIENCES',  # → 12 Sc. MATHS ou EXPERIMENTALES

    # ── Lycée 12ème → Terminale ─────────────────────────────────
    '12EME ANNEE':                    'TERMINALE',
    '12 SCIENCES EXPERIMENTALES':     'TERMINALE SCIENCES EXPERIMENTALES',
    '12 SCIENCES MATHS':              'TERMINALE SCIENCES MATHS',
    '12 SCIENCES SOCIALES':           'TERMINALE SCIENCES SOCIALES',
    '12 SERIE LITTERAIRE':            'TERMINALE SCIENCES SOCIALES',

    # ── Garderie → Petite Section ─────────────────────────────────
    'GARDERIE':                           'PETITE SECTION',

    # ── Terminale : fin de cycle (archivage BAC) ──────────────────
    'TERMINALE':                          None,
    'TERMINALE SCIENCES EXPERIMENTALES':  None,
    'TERMINALE SCIENCES MATHS':           None,
    'TERMINALE SCIENCES SOCIALES':        None,
}

# Classes terminales dont les élèves sortent du système
CLASSES_TERMINALES = {
    'TERMINALE', 'TERMINALE SCIENCES EXPERIMENTALES',
    'TERMINALE SCIENCES MATHS', 'TERMINALE SCIENCES SOCIALES',
}

# Classes de fin de cycle avec examen national
# Les élèves passent en classe supérieure, mais on enregistre le diplôme
CLASSES_EXAMEN = {
    '6EME ANNEE': {'diplome': 'CEP', 'label': "Certificat d'Études Primaires (CEP)"},
    '10EME ANNEE': {'diplome': 'BEPC', 'label': 'Brevet d\'Études du Premier Cycle (BEPC)'},
}

# Labels lisibles pour l'affichage dans le tableau de prévisualisation
PROGRESSION_LABELS = {
    'PETITE SECTION':         'Petite Section',
    'PS':                     'PS',
    'MOYENNE SECTION':        'Moyenne Section',
    'MS':                     'MS',
    'GRANDE SECTION':         'Grande Section',
    'GS':                     'GS',
    '1ERE ANNEE':             '1ère Année',
    '2EME ANNEE':             '2ème Année',
    '3EME ANNEE':             '3ème Année',
    '4EME ANNEE':             '4ème Année',
    '5EME ANNEE':             '5ème Année',
    '6EME ANNEE':             '6ème Année',
    '7EME ANNEE':             '7ème Année',
    '8EME ANNEE':             '8ème Année',
    '9EME ANNEE':             '9ème Année',
    '10EME ANNEE':            '10ème Année',
    '11EME ANNEE':            '11ème Année',
    '12EME ANNEE':            '12ème Année',
    '12 SCIENCES':            '12 Sciences (Maths ou Exp.)',
    '12 SCIENCES SOCIALES':   '12 Sciences Sociales',
    'TERMINALE':                          'Terminale',
    'TERMINALE SCIENCES EXPERIMENTALES':  'Terminale Sciences Expérimentales',
    'TERMINALE SCIENCES MATHS':           'Terminale Sciences Maths',
    'TERMINALE SCIENCES SOCIALES':        'Terminale Sciences Sociales',
}


def _annee_suivante(annee: str) -> str:
    """Calcule l'année scolaire suivante. Ex: '2024-2025' → '2025-2026'"""
    try:
        debut, fin = annee.split('-')
        return f"{int(debut)+1}-{int(fin)+1}"
    except Exception:
        return annee


def _classe_superieure_label(nom_classe: str) -> Optional[str]:
    """Retourne le libellé lisible de la classe supérieure, ou None si terminale.

    Exemples:
        '10ÈME ANNÉE (A)' → '11ème Année A'
        'GRANDE SECTION (A)' → '1ère Année A'
        '11 SÉRIE LITTÉRAIRE (A)' → '12 Sciences Sociales A'
        'GARDERIE' → None  (dernière année)
        'TERMINALE SCIENCES MATHS' → None  (dernière année)
    """
    base, lettre = _extraire_base_et_lettre(nom_classe)
    cible_base = PROGRESSION_CLASSES.get(base)

    if cible_base is None:
        return None  # Classe terminale ou inconnue

    label = PROGRESSION_LABELS.get(cible_base, cible_base)
    if lettre:
        label += f' {lettre}'
    return label


def _construire_index_classes(classes) -> List[Tuple]:
    """Construit un index normalisé (base, lettre) pour chaque classe.

    Retourne une liste de tuples (classe_obj, base_normalisée, lettre).
    """
    index = []
    for cls in classes:
        base, lettre = _extraire_base_et_lettre(cls.nom)
        index.append((cls, base, lettre))
    return index


def _trouver_classe_cible(nom_classe_actuelle: str, index_nouvelles: List[Tuple]) -> Optional[Classe]:
    """Trouve la meilleure classe cible par matching intelligent multi-niveaux.

    Algorithme de correspondance (du plus précis au plus souple) :

    Niveau 1 — Match exact : base normalisée + même lettre
        Ex: cible '2EME ANNEE' + lettre 'A' → trouve '2ÈME ANNÉE A'

    Niveau 2 — Match base exacte sans contrainte de lettre
        Ex: cible 'MOYENNE SECTION' → trouve 'MOYENNE SECTION' (unique)

    Niveau 3 — Match par préfixe (pour séries scientifiques)
        Ex: cible '12 SCIENCES' → trouve '12 SCIENCES MATHS' ou '12 SCIENCES EXPERIMENTALES'

    Niveau 4 — Match par numéro de niveau + lettre
        Ex: cible '11EME ANNEE' → trouve '11 SÉRIE LITTÉRAIRE (A)' si pas de 11ème générique

    Retourne la classe trouvée ou None.
    """
    base_actuelle, lettre = _extraire_base_et_lettre(nom_classe_actuelle)
    cible_base = PROGRESSION_CLASSES.get(base_actuelle)

    if cible_base is None:
        return None  # Classe terminale → pas de progression

    # ── Niveau 1 : Base exacte + même lettre ──
    for cls, c_base, c_lettre in index_nouvelles:
        if c_base == cible_base and c_lettre == lettre:
            return cls

    # ── Niveau 2 : Base exacte, n'importe quelle lettre ──
    for cls, c_base, c_lettre in index_nouvelles:
        if c_base == cible_base:
            return cls

    # ── Niveau 3 : Préfixe de la base cible ──
    # Utile pour '12 SCIENCES' → '12 SCIENCES MATHS', '12 SCIENCES EXPERIMENTALES'
    prefix = cible_base + ' '
    # D'abord avec la même lettre
    if lettre:
        for cls, c_base, c_lettre in index_nouvelles:
            if c_base.startswith(prefix) and c_lettre == lettre:
                return cls
    # Puis sans contrainte de lettre
    for cls, c_base, c_lettre in index_nouvelles:
        if c_base.startswith(prefix):
            return cls

    # ── Niveau 4 : Numéro de niveau (ex: 11 → n'importe quelle 11ème) ──
    num_match = re.match(r'^(\d+)', cible_base)
    if num_match:
        num_str = num_match.group(1)
        # Pattern : le numéro suivi de EME, ERE, un espace, ou fin de chaîne
        pattern = re.compile(rf'^{re.escape(num_str)}(?:EME|ERE|\s|$)')
        # D'abord avec la même lettre
        if lettre:
            for cls, c_base, c_lettre in index_nouvelles:
                if pattern.match(c_base) and c_lettre == lettre:
                    return cls
        # Puis sans contrainte de lettre
        for cls, c_base, c_lettre in index_nouvelles:
            if pattern.match(c_base):
                return cls

    return None


# ══════════════════════════════════════════════════════════════════════════════
#  VUES : APERÇU & CRÉATION DE LA NOUVELLE ANNÉE
# ══════════════════════════════════════════════════════════════════════════════

@login_required
def nouvelle_annee_apercu(request):
    """Affiche un aperçu de ce qui sera créé pour la nouvelle année."""
    ecole = user_school(request.user)
    if not ecole:
        messages.error(request, "Aucune école associée à votre compte.")
        return redirect('eleves:liste_eleves')

    # Détecter l'année courante (la plus récente)
    annees = (Classe.objects
              .filter(ecole=ecole)
              .values_list('annee_scolaire', flat=True)
              .distinct()
              .order_by('-annee_scolaire'))
    if not annees:
        messages.warning(request, "Aucune classe trouvée pour cet établissement.")
        return redirect('eleves:gestion_classes')

    source_demandee = request.GET.get('annee_courante', '').strip()
    annee_courante = source_demandee if source_demandee in annees else annees[0]
    annee_nouvelle = _annee_suivante(annee_courante)

    # Vérifier si la nouvelle année existe déjà
    deja_existante = Classe.objects.filter(ecole=ecole, annee_scolaire=annee_nouvelle).exists()

    classes_actuelles = (Classe.objects
                         .filter(ecole=ecole, annee_scolaire=annee_courante)
                         .prefetch_related('eleves')
                         .order_by('niveau', 'nom'))

    preview_classes = []
    for cls in classes_actuelles:
        sup_label = _classe_superieure_label(cls.nom)
        nb_eleves = cls.eleves.filter(statut__in=STATUTS_EN_CLASSE).count()
        preview_classes.append({
            'classe': cls,
            'nb_eleves': nb_eleves,
            'classe_superieure': sup_label,
        })

    # Grilles tarifaires courantes
    from .models import GrilleTarifaire
    grilles = GrilleTarifaire.objects.filter(ecole=ecole, annee_scolaire=annee_courante)

    # Classes notes (du module notes)
    from notes.models import ClasseNote, MatiereNote
    classes_notes = ClasseNote.objects.filter(
        ecole=ecole, annee_scolaire=annee_courante, actif=True
    ).prefetch_related('matieres')
    preview_notes = []
    for cn in classes_notes:
        preview_notes.append({
            'classe_note': cn,
            'nb_matieres': cn.matieres.filter(actif=True).count(),
        })

    # Élèves de Terminale (pour que l'utilisateur coche ceux qui ont le BAC)
    eleves_terminale = []
    for cls in classes_actuelles:
        base, _ = _extraire_base_et_lettre(cls.nom)
        if base in CLASSES_TERMINALES:
            for eleve in cls.eleves.filter(statut__in=STATUTS_EN_CLASSE).order_by('nom', 'prenom'):
                eleves_terminale.append({
                    'eleve': eleve,
                    'classe_nom': cls.nom,
                })

    # Élèves de 6ème (CEP) et 10ème (BEPC)
    eleves_examens = []  # liste de {'diplome': 'CEP'/'BEPC', 'label': ..., 'eleves': [...]}
    for base_classe, info_examen in CLASSES_EXAMEN.items():
        eleves_de_ce_niveau = []
        for cls in classes_actuelles:
            base, _ = _extraire_base_et_lettre(cls.nom)
            if base == base_classe:
                for eleve in cls.eleves.filter(statut__in=STATUTS_EN_CLASSE).order_by('nom', 'prenom'):
                    eleves_de_ce_niveau.append({
                        'eleve': eleve,
                        'classe_nom': cls.nom,
                    })
        if eleves_de_ce_niveau:
            eleves_examens.append({
                'diplome': info_examen['diplome'],
                'label': info_examen['label'],
                'eleves': eleves_de_ce_niveau,
            })

    # Répartition des élèves (hors Terminale) dans les classes de la nouvelle
    # année, selon la même règle que le passage : les admis vont en classe
    # supérieure, les autres restent dans une classe du même nom. Les classes
    # étant dupliquées par nom, celles de l'année courante servent d'index.
    from notes.calculs_moyennes import detecter_niveau_scolaire
    destinations = list(classes_actuelles)
    noms_destinations = {c.nom for c in destinations}
    for c in Classe.objects.filter(ecole=ecole, annee_scolaire=annee_nouvelle).order_by('niveau', 'nom'):
        if c.nom not in noms_destinations:
            destinations.append(c)
            noms_destinations.add(c.nom)
    index_destinations = _construire_index_classes(destinations)
    cles_classes = {c.nom: f'nc{i}' for i, c in enumerate(destinations)}
    repartition = {c.nom: [] for c in destinations}

    eleves_non_admis = []
    eleves_admis_affiches = []
    for cls in classes_actuelles:
        base, _ = _extraire_base_et_lettre(cls.nom)
        if base in CLASSES_TERMINALES:
            continue
        maternelle = detecter_niveau_scolaire(cls.nom) == 'MATERNELLE'
        superieure = _trouver_classe_cible(cls.nom, index_destinations)
        classe_passage = superieure.nom if superieure else cls.nom
        for eleve in cls.eleves.filter(statut__in=STATUTS_EN_CLASSE).order_by('nom', 'prenom'):
            if maternelle:
                moyenne, sur, admis = None, 10, True
            else:
                moyenne, sur = _get_moyenne_annuelle(eleve, annee_courante)
                admis = _admis_au_passage(moyenne, sur)
            item = {
                'eleve': eleve,
                'classe_nom': cls.nom,
                'moyenne': moyenne,
                'sur': sur,
                'seuil': 5.0 if sur == 10 else 10.0,
                'admis': admis,
                'maternelle': maternelle,
                'non_evalue': not maternelle and moyenne is None,
                'sans_superieure': superieure is None,
                'cle_passage': cles_classes[classe_passage],
                'cle_maintien': cles_classes[cls.nom],
            }
            repartition[classe_passage if admis else cls.nom].append(item)
            if admis:
                eleves_admis_affiches.append(str(eleve.pk))
            else:
                eleves_non_admis.append(item)

    classes_nouvelles = [
        {'nom': c.nom, 'cle': cles_classes[c.nom], 'eleves': repartition[c.nom]}
        for c in destinations
    ]

    context = {
        'ecole': ecole,
        'annee_courante': annee_courante,
        'annee_nouvelle': annee_nouvelle,
        'deja_existante': deja_existante,
        'preview_classes': preview_classes,
        'grilles': grilles,
        'preview_notes': preview_notes,
        'nb_total_eleves': sum(p['nb_eleves'] for p in preview_classes),
        'eleves_terminale': eleves_terminale,
        'eleves_examens': eleves_examens,
        'eleves_non_admis': eleves_non_admis,
        'classes_nouvelles': classes_nouvelles,
        # Les admis décochés redoublent : la création compare les cases reçues
        # à cette liste envoyée avec le formulaire (un élève inscrit depuis
        # l'aperçu n'est donc pas retenu par erreur).
        'eleves_admis_affiches': ','.join(eleves_admis_affiches),
        'nb_eleves_repartis': sum(len(c['eleves']) for c in classes_nouvelles),
        'titre_page': f'Nouvelle Année Scolaire {annee_nouvelle}',
    }
    return render(request, 'eleves/nouvelle_annee.html', context)



# Nombre d'élèves traités par requête. Chaque élève passé déclenche la file
# de synchronisation, le journal d'audit et l'historique (~80 requêtes) :
# traiter toute l'école en une seule requête dépassait le délai du serveur,
# qui l'interrompait et annulait tout. Chaque lot est enregistré séparément
# et la page enchaîne automatiquement le lot suivant.
TAILLE_LOT_PASSAGE = 100

_COMPTEURS = (
    'classes_creees', 'grilles_creees', 'grilles_bus_creees', 'configs_paiement_creees',
    'classes_notes_creees', 'matieres_creees', 'eleves_passes',
    'eleves_conserves', 'eleves_diplomes', 'eleves_sortis', 'eleves_cep',
    'eleves_bepc', 'eleves_convention', 'eleves_redoublants', 'echeanciers_crees',
    'affectations_reconduites',
)


def _cle_session_passage(ecole, annee_nouvelle):
    return f'passage_annee_{ecole.pk}_{annee_nouvelle}'


def _dupliquer_structure(request, ecole, annee_courante, annee_nouvelle, options, resultats):
    """Classes, configurations de paiement, grilles et classes Notes (idempotent)."""
    classes_actuelles = Classe.objects.filter(
        ecole=ecole, annee_scolaire=annee_courante
    ).order_by('niveau', 'nom')

    for cls in classes_actuelles:
        nouvelle_cls, created = Classe.objects.get_or_create(
            ecole=ecole,
            nom=cls.nom,
            annee_scolaire=annee_nouvelle,
            defaults={
                'niveau': cls.niveau,
                'capacite_max': cls.capacite_max,
                'code_matricule': cls.code_matricule,
            }
        )
        if created:
            resultats['classes_creees'] += 1

        from paiements.models import ConfigurationPaiement
        try:
            config_ancienne = getattr(cls, 'configuration_paiement', None)
            if config_ancienne and not ConfigurationPaiement.objects.filter(classe=nouvelle_cls).exists():
                ConfigurationPaiement.objects.create(
                    classe=nouvelle_cls,
                    montant_inscription=config_ancienne.montant_inscription,
                    montant_scolarite=config_ancienne.montant_scolarite,
                    nombre_tranches=config_ancienne.nombre_tranches,
                    cree_par=request.user,
                )
                resultats['configs_paiement_creees'] += 1
        except Exception as exc:
            logger.warning(f"Config paiement non copiée pour {nouvelle_cls.nom}: {exc}")

    if options['dupliquer_grilles']:
        from .models import GrilleTarifaire
        for g in GrilleTarifaire.objects.filter(ecole=ecole, annee_scolaire=annee_courante):
            _, created = GrilleTarifaire.objects.get_or_create(
                ecole=ecole,
                niveau=g.niveau,
                annee_scolaire=annee_nouvelle,
                defaults={
                    'frais_inscription': g.frais_inscription,
                    'frais_reinscription': g.frais_reinscription,
                    'tranche_1': g.tranche_1,
                    'tranche_2': g.tranche_2,
                    'tranche_3': g.tranche_3,
                    'periode_1': g.periode_1,
                    'periode_2': g.periode_2,
                    'periode_3': g.periode_3,
                    **{
                        champ: (getattr(g, champ) + relativedelta(years=1)
                                if getattr(g, champ) else None)
                        for champ in (
                            'date_echeance_inscription_defaut',
                            'date_echeance_tranche_1_defaut',
                            'date_echeance_tranche_2_defaut',
                            'date_echeance_tranche_3_defaut',
                        )
                    },
                }
            )
            if created:
                resultats['grilles_creees'] += 1

        from bus.models import GrilleTarifaireBus
        for grille in GrilleTarifaireBus.objects.filter(ecole=ecole, annee_scolaire=annee_courante):
            _, created = GrilleTarifaireBus.objects.get_or_create(
                ecole=ecole, zone=grille.zone, annee_scolaire=annee_nouvelle,
                defaults={
                    'tranche_1': grille.tranche_1,
                    'tranche_2': grille.tranche_2,
                    'tranche_3': grille.tranche_3,
                    'actif': grille.actif,
                    'cree_par': request.user,
                    **{
                        champ: (getattr(grille, champ) + relativedelta(years=1)
                                if getattr(grille, champ) else None)
                        for champ in (
                            'date_echeance_tranche_1', 'date_echeance_tranche_2',
                            'date_echeance_tranche_3',
                        )
                    },
                },
            )
            if created:
                resultats['grilles_bus_creees'] += 1

    if options.get('reconduire_affectations'):
        _reconduire_affectations(ecole, annee_courante, annee_nouvelle, resultats)

    if options['dupliquer_notes_classes']:
        from notes.models import ClasseNote, MatiereNote
        for cn in ClasseNote.objects.filter(ecole=ecole, annee_scolaire=annee_courante, actif=True):
            nouvelle_cn, created = ClasseNote.objects.get_or_create(
                ecole=ecole,
                nom=cn.nom,
                annee_scolaire=annee_nouvelle,
                defaults={
                    'niveau': cn.niveau,
                    'niveau_enseignement': cn.niveau_enseignement,
                    'effectif': cn.effectif,
                    'description': cn.description,
                    'actif': True,
                    'cree_par': request.user,
                }
            )
            if created:
                resultats['classes_notes_creees'] += 1
                for m in cn.matieres.filter(actif=True):
                    _, m_created = MatiereNote.objects.get_or_create(
                        classe=nouvelle_cn,
                        code=m.code,
                        defaults={
                            'nom': m.nom,
                            'coefficient': m.coefficient,
                            'description': m.description,
                            'actif': True,
                            'cree_par': request.user,
                        }
                    )
                    if m_created:
                        resultats['matieres_creees'] += 1


def _reconduire_affectations(ecole, annee_courante, annee_nouvelle, resultats):
    """Les enseignants gardent leurs classes dans la nouvelle année.

    Chaque affectation en cours sur une classe de l'année écoulée est close la
    veille de la rentrée (31 août) et reprise sur la classe du même nom de la
    nouvelle année : la paie de chaque mois compte alors l'effectif et les
    cours de la bonne classe, et l'historique de l'année écoulée est conservé.
    """
    from django.core.exceptions import ValidationError
    from django.db.models import Q
    from salaires.models import AffectationClasse, StatutEnseignant
    from .utils_annee import bornes_annee_scolaire

    rentree = bornes_annee_scolaire(annee_nouvelle)[0]
    nouvelles = {c.nom: c for c in Classe.objects.filter(ecole=ecole, annee_scolaire=annee_nouvelle)}
    affectations = (AffectationClasse.objects
                    .select_related('classe', 'enseignant')
                    .filter(classe__ecole=ecole, classe__annee_scolaire=annee_courante, actif=True)
                    .filter(Q(date_fin__isnull=True) | Q(date_fin__gte=rentree))
                    .exclude(enseignant__statut=StatutEnseignant.DEMISSIONNAIRE))
    for affectation in affectations:
        cible = nouvelles.get(affectation.classe.nom)
        if cible is None:
            continue
        try:
            with transaction.atomic():
                if affectation.date_debut >= rentree:
                    # Saisie après la rentrée sur l'ancienne classe : simple correction.
                    affectation.classe = cible
                    affectation.save()
                else:
                    affectation.actif = False
                    affectation.date_fin = rentree - timedelta(days=1)
                    affectation.save()
                    AffectationClasse.objects.create(
                        enseignant=affectation.enseignant,
                        classe=cible,
                        heures_par_semaine=affectation.heures_par_semaine,
                        taux_horaire=affectation.taux_horaire,
                        matiere=affectation.matiere,
                        date_debut=rentree,
                        actif=True,
                    )
        except ValidationError as exc:
            logger.warning(f"Affectation {affectation.pk} non reconduite : {exc}")
            continue
        resultats['affectations_reconduites'] += 1


def _creer_echeancier(request, eleve, ecole, annee_nouvelle, *, reinscription=True):
    """Crée l'échéancier de la nouvelle année (sans jamais toucher aux anciens)."""
    from paiements.models import EcheancierPaiement
    from .models import GrilleTarifaire

    if EcheancierPaiement.objects.filter(eleve=eleve, annee_scolaire=annee_nouvelle).exists():
        return False
    try:
        annee_debut = int(annee_nouvelle.split('-')[0])
    except Exception:
        annee_debut = _date_type.today().year
    annee_fin = annee_debut + 1

    niveau = getattr(eleve.classe, 'niveau', None)
    grille = None
    if niveau:
        grille = GrilleTarifaire.objects.filter(
            ecole=ecole, niveau=niveau, annee_scolaire=annee_nouvelle
        ).first()
    if grille is None:
        raise ValueError(
            f"Tarif manquant pour {eleve.classe.get_niveau_display()} en {annee_nouvelle}. "
            "Copiez les grilles tarifaires ou configurez les tarifs avant le passage."
        )
    from paiements.services import _nature_frais
    nature = _nature_frais(eleve, annee_nouvelle, nouvelle_annee=reinscription)
    fi = Decimal(str((grille.frais_reinscription if nature == EcheancierPaiement.NATURE_REINSCRIPTION
                      else grille.frais_inscription) or 0))
    t1 = Decimal(str(grille.tranche_1 or 0))
    t2 = Decimal(str(grille.tranche_2 or 0))
    t3 = Decimal(str(grille.tranche_3 or 0))

    EcheancierPaiement.objects.create(
        eleve=eleve,
        annee_scolaire=annee_nouvelle,
        nature_frais=nature,
        frais_inscription_du=fi,
        tranche_1_due=t1,
        tranche_2_due=t2,
        tranche_3_due=t3,
        frais_inscription_paye=Decimal('0'),
        tranche_1_payee=Decimal('0'),
        tranche_2_payee=Decimal('0'),
        tranche_3_payee=Decimal('0'),
        date_echeance_inscription=grille.date_echeance_inscription_defaut or _date_type(annee_debut, 10, 1),
        date_echeance_tranche_1=grille.date_echeance_tranche_1_defaut or _date_type(annee_fin, 1, 15),
        date_echeance_tranche_2=grille.date_echeance_tranche_2_defaut or _date_type(annee_fin, 3, 15),
        date_echeance_tranche_3=grille.date_echeance_tranche_3_defaut or _date_type(annee_fin, 5, 15),
        statut='A_PAYER',
        cree_par=request.user,
    )
    # Un versement anticipé peut déjà avoir été saisi/importé pour cette année.
    # Il compte dans le nouvel échéancier ; ceux des autres années restent exclus.
    from paiements.models import Paiement
    if Paiement.objects.filter(eleve=eleve, annee_scolaire=annee_nouvelle).exists():
        from paiements.services import synchroniser_echeancier_apres_changement_paiement
        synchroniser_echeancier_apres_changement_paiement(eleve.pk, annee_nouvelle)
    return True


def _passer_eleve(request, eleve, ctx, resultats):
    """Fait passer un élève vers la nouvelle année (logique d'origine inchangée)."""
    from notes.calculs_moyennes import detecter_niveau_scolaire

    annee_courante, annee_nouvelle = ctx['annee_courante'], ctx['annee_nouvelle']
    ancienne_classe = eleve.classe
    base_actuelle, _ = _extraire_base_et_lettre(ancienne_classe.nom)
    eleve_id_str = str(eleve.pk)

    if base_actuelle in CLASSES_TERMINALES:
        if eleve_id_str in ctx['eleves_bac']:
            eleve.statut = 'DIPLOME'
            eleve.save()
            HistoriqueEleve.objects.create(
                eleve=eleve,
                action='DIPLOME',
                description=(
                    f"Diplômé(e) — BAC obtenu, archivé(e) lors du passage "
                    f"à l'année {annee_nouvelle}. Classe: {ancienne_classe.nom}"
                ),
                utilisateur=request.user,
            )
            resultats['eleves_diplomes'] += 1
        else:
            eleve.statut = 'TRANSFERE'
            eleve.save()
            HistoriqueEleve.objects.create(
                eleve=eleve,
                action='FIN_CYCLE',
                description=(
                    f"Fin de cycle — sorti(e) du système lors du passage "
                    f"à l'année {annee_nouvelle}. Classe: {ancienne_classe.nom}"
                ),
                utilisateur=request.user,
            )
            resultats['eleves_sortis'] += 1
        return

    if detecter_niveau_scolaire(ancienne_classe.nom) != 'MATERNELLE':
        moyenne, sur = _get_moyenne_annuelle(eleve, annee_courante)
        admis = _admis_au_passage(moyenne, sur)
        par_convention = eleve_id_str in ctx['eleves_convention']
    else:
        admis, par_convention, moyenne, sur = True, False, None, 10
    retenu = admis and eleve_id_str in ctx['eleves_retenus']
    if retenu:
        admis = False

    eleve._current_user = request.user
    eleve._passage_nouvelle_annee = True
    nouvelle_meme = ctx['map_anciennes_nouvelles'].get(ancienne_classe.pk)

    def affecter(classe):
        # Contrôle avant Eleve.save() : un tarif absent ne doit déclencher ni
        # changement de matricule, ni historique, ni synchronisation inutile.
        if classe.niveau not in ctx['niveaux_tarifes']:
            raise ValueError(
                f"Tarif manquant pour {classe.get_niveau_display()} en {annee_nouvelle}. "
                "Copiez les grilles tarifaires ou configurez les tarifs avant le passage."
            )
        eleve.classe = classe
        eleve.save()

    if not admis and not par_convention:
        if nouvelle_meme:
            affecter(nouvelle_meme)
            moy_txt = f"{moyenne}/{sur}" if moyenne is not None else "non évaluée"
            decision_txt = ", décision de la direction" if retenu else ""
            HistoriqueEleve.objects.create(
                eleve=eleve,
                action='CHANGEMENT_CLASSE',
                description=(
                    f"Redoublant — année {annee_nouvelle}: "
                    f"maintenu(e) en {ancienne_classe.nom} "
                    f"(moyenne: {moy_txt}{decision_txt})"
                ),
                utilisateur=request.user,
            )
            resultats['eleves_redoublants'] += 1
        return

    sup_cls = _trouver_classe_cible(ancienne_classe.nom, ctx['index_nouvelles'])
    if sup_cls:
        affecter(sup_cls)
        convention_txt = " (par convention direction/parents)" if par_convention else ""
        moy_txt = f" — moyenne: {moyenne}/{sur}" if moyenne is not None else ""
        HistoriqueEleve.objects.create(
            eleve=eleve,
            action='CHANGEMENT_CLASSE',
            description=(
                f"Passage nouvelle année {annee_nouvelle}: "
                f"{ancienne_classe.nom} → {sup_cls.nom}{moy_txt}{convention_txt}"
            ),
            utilisateur=request.user,
        )
        if par_convention:
            resultats['eleves_convention'] += 1
        resultats['eleves_passes'] += 1

        if base_actuelle == '6EME ANNEE' and eleve_id_str in ctx['eleves_cep']:
            HistoriqueEleve.objects.create(
                eleve=eleve,
                action='DIPLOME',
                description=(
                    f"Certificat d'Études Primaires (CEP) obtenu — "
                    f"année {annee_courante}, classe: {ancienne_classe.nom}"
                ),
                utilisateur=request.user,
            )
            resultats['eleves_cep'] += 1
        elif base_actuelle == '10EME ANNEE' and eleve_id_str in ctx['eleves_bepc']:
            HistoriqueEleve.objects.create(
                eleve=eleve,
                action='DIPLOME',
                description=(
                    f"Brevet d'Études du Premier Cycle (BEPC) obtenu — "
                    f"année {annee_courante}, classe: {ancienne_classe.nom}"
                ),
                utilisateur=request.user,
            )
            resultats['eleves_bepc'] += 1
    elif nouvelle_meme:
        affecter(nouvelle_meme)
        HistoriqueEleve.objects.create(
            eleve=eleve,
            action='CHANGEMENT_CLASSE',
            description=(
                f"Conservation nouvelle année {annee_nouvelle}: "
                f"{ancienne_classe.nom} → {nouvelle_meme.nom} "
                f"(classe supérieure non trouvée)"
            ),
            utilisateur=request.user,
        )
        resultats['eleves_conserves'] += 1


def _message_final(annee_nouvelle, resultats):
    libelles = [
        ('classes_creees', "{} classe(s) créée(s)"),
        ('configs_paiement_creees', "{} config(s) paiement copiée(s)"),
        ('grilles_creees', "{} grille(s) tarifaire(s) copiée(s)"),
        ('grilles_bus_creees', "{} grille(s) bus copiée(s)"),
        ('affectations_reconduites', "{} affectation(s) d'enseignant reconduite(s)"),
        ('eleves_passes', "{} élève(s) passé(s) en classe supérieure"),
        ('eleves_conserves', "{} élève(s) conservé(s) dans leur classe"),
        ('eleves_cep', "{} élève(s) titulaire(s) du CEP"),
        ('eleves_bepc', "{} élève(s) titulaire(s) du BEPC"),
        ('eleves_diplomes', "{} élève(s) diplômé(s) (BAC) archivé(s)"),
        ('eleves_sortis', "{} élève(s) sorti(s) du système (fin de cycle)"),
        ('eleves_convention', "{} élève(s) promu(s) par convention (direction/parents)"),
        ('eleves_redoublants', "{} élève(s) redoublant(s)"),
        ('echeanciers_crees', "{} échéancier(s) de paiement créé(s)"),
    ]
    parts = [modele.format(resultats[cle]) for cle, modele in libelles if resultats.get(cle)]
    if resultats.get('classes_notes_creees'):
        parts.insert(3, f"{resultats['classes_notes_creees']} classe(s) notes avec "
                        f"{resultats['matieres_creees']} matière(s)")
    return f"Année scolaire {annee_nouvelle} créée avec succès ! " + ' | '.join(parts)


@login_required
def nouvelle_annee_creer(request):
    """Crée la nouvelle année scolaire, en plusieurs lots si nécessaire."""
    if request.method != 'POST':
        return redirect('eleves:nouvelle_annee_apercu')

    ecole = user_school(request.user)
    if not ecole:
        messages.error(request, "Aucune école associée à votre compte.")
        return redirect('eleves:liste_eleves')

    annee_courante = request.POST.get('annee_courante', '').strip()
    annee_nouvelle = request.POST.get('annee_nouvelle', '').strip()
    if not annee_courante or not annee_nouvelle:
        messages.error(request, "Paramètres manquants.")
        return redirect('eleves:nouvelle_annee_apercu')

    # Une année identique (ou mal formée) laisserait chaque élève dans la
    # sélection des "restants" et ferait tourner les lots indéfiniment.
    if (not re.fullmatch(r'[1-9]\d{3}-[1-9]\d{3}', annee_courante)
            or not re.fullmatch(r'[1-9]\d{3}-[1-9]\d{3}', annee_nouvelle)
            or int(annee_courante[-4:]) != int(annee_courante[:4]) + 1
            or annee_nouvelle != _annee_suivante(annee_courante)):
        messages.error(request, "Choisissez deux années scolaires consécutives au format 2025-2026.")
        return redirect('eleves:nouvelle_annee_apercu')
    if not Classe.objects.filter(ecole=ecole, annee_scolaire=annee_courante).exists():
        messages.error(request, "L'année de départ n'existe pas pour cet établissement.")
        return redirect('eleves:nouvelle_annee_apercu')

    cle = _cle_session_passage(ecole, annee_nouvelle)
    etat = request.session.get(cle)

    if request.POST.get('continuer') == '1':
        if not etat or etat.get('annee_courante') != annee_courante:
            messages.error(request, "La création en cours a expiré. Relancez-la depuis l'aperçu : "
                                    "les élèves déjà passés ne seront pas traités deux fois.")
            return redirect('eleves:nouvelle_annee_apercu')
    else:
        # Liste de confirmation de l'aperçu : un admis décoché redouble.
        eleves_retenus = []
        if (request.POST.get('confirmation_passage') == '1'
                and request.POST.get('faire_passer_eleves') == '1'):
            affiches = [pk.strip() for pk in request.POST.get('eleves_admis_affiches', '').split(',')
                        if pk.strip().isdigit()]
            confirmes = set(request.POST.getlist('eleves_confirmes'))
            eleves_retenus = [pk for pk in affiches if pk not in confirmes]

        # Premier envoi : mémoriser les choix pour les lots suivants.
        etat = {
            'annee_courante': annee_courante,
            'dupliquer_grilles': request.POST.get('dupliquer_grilles') == '1',
            'dupliquer_notes_classes': request.POST.get('dupliquer_notes_classes') == '1',
            'reconduire_affectations': request.POST.get('reconduire_affectations') == '1',
            'faire_passer_eleves': request.POST.get('faire_passer_eleves') == '1',
            'eleves_bac': request.POST.getlist('eleves_bac'),
            'eleves_cep': request.POST.getlist('eleves_cep'),
            'eleves_bepc': request.POST.getlist('eleves_bepc'),
            'eleves_convention': request.POST.getlist('eleves_convention'),
            'eleves_retenus': eleves_retenus,
            'resultats': {c: 0 for c in _COMPTEURS},
            'echecs': [],
            'erreurs_tarifs': [],
            'total': Eleve.objects.filter(
                classe__ecole=ecole, classe__annee_scolaire=annee_courante, statut__in=STATUTS_EN_CLASSE,
            ).count(),
        }
        try:
            with transaction.atomic():
                _dupliquer_structure(request, ecole, annee_courante, annee_nouvelle,
                                     etat, etat['resultats'])
        except Exception as e:
            logger.error(f"Erreur création nouvelle année: {e}", exc_info=True)
            messages.error(request, f"Erreur lors de la création : {e}")
            return redirect('eleves:nouvelle_annee_apercu')

    resultats = etat['resultats']

    if etat['faire_passer_eleves']:
        from .models import GrilleTarifaire
        map_anciennes_nouvelles = {}
        nouvelles_par_nom = {
            c.nom: c for c in Classe.objects.filter(ecole=ecole, annee_scolaire=annee_nouvelle)
        }
        for cls in Classe.objects.filter(ecole=ecole, annee_scolaire=annee_courante):
            if cls.nom in nouvelles_par_nom:
                map_anciennes_nouvelles[cls.pk] = nouvelles_par_nom[cls.nom]
        ctx = {
            'annee_courante': annee_courante,
            'annee_nouvelle': annee_nouvelle,
            'map_anciennes_nouvelles': map_anciennes_nouvelles,
            'index_nouvelles': _construire_index_classes(list(nouvelles_par_nom.values())),
            'niveaux_tarifes': set(GrilleTarifaire.objects.filter(
                ecole=ecole, annee_scolaire=annee_nouvelle,
            ).values_list('niveau', flat=True)),
            'eleves_bac': set(etat['eleves_bac']),
            'eleves_cep': set(etat['eleves_cep']),
            'eleves_bepc': set(etat['eleves_bepc']),
            'eleves_convention': set(etat['eleves_convention']),
            'eleves_retenus': set(etat.get('eleves_retenus', [])),
        }

        restants = (Eleve.objects
                    .filter(classe__ecole=ecole, classe__annee_scolaire=annee_courante, statut__in=STATUTS_EN_CLASSE)
                    .exclude(pk__in=etat['echecs']))
        lot = list(restants.select_related('classe').order_by('pk')[:TAILLE_LOT_PASSAGE])

        for eleve in lot:
            compteurs_avant = resultats.copy()
            try:
                with transaction.atomic():
                    _passer_eleve(request, eleve, ctx, resultats)
                    if eleve.statut in STATUTS_EN_CLASSE and eleve.classe.annee_scolaire != annee_nouvelle:
                        raise ValueError("Aucune classe de destination trouvée dans la nouvelle année.")
                    if (eleve.statut in STATUTS_EN_CLASSE
                            and eleve.classe.annee_scolaire == annee_nouvelle
                            and _creer_echeancier(request, eleve, ecole, annee_nouvelle)):
                        resultats['echeanciers_crees'] += 1
            except Exception as exc:
                # Un élève en erreur ne bloque ni son lot ni les suivants.
                logger.error(f"Passage nouvelle année impossible pour l'élève {eleve.pk}: {exc}",
                             exc_info=True)
                resultats.clear()
                resultats.update(compteurs_avant)
                etat['echecs'].append(eleve.pk)
                if isinstance(exc, ValueError) and str(exc).startswith('Tarif manquant'):
                    erreurs = etat.setdefault('erreurs_tarifs', [])
                    if str(exc) not in erreurs and len(erreurs) < 20:
                        erreurs.append(str(exc))

        request.session[cle] = etat
        # Le QuerySet initial a figé la liste d'exclusion avant les erreurs du lot.
        reste = restants.exclude(pk__in=etat['echecs']).count()
        if reste:
            total = etat['total'] or 1
            return render(request, 'eleves/nouvelle_annee_progression.html', {
                'annee_courante': annee_courante,
                'annee_nouvelle': annee_nouvelle,
                'traites': max(0, total - reste),
                'total': total,
                'pourcentage': min(100, int((total - reste) * 100 / total)),
                'titre_page': f'Nouvelle Année Scolaire {annee_nouvelle}',
            })

        # Élèves inscrits directement dans la nouvelle année sans échéancier.
        from paiements.models import EcheancierPaiement
        deja = set(EcheancierPaiement.objects.filter(
            eleve__classe__ecole=ecole, annee_scolaire=annee_nouvelle,
        ).values_list('eleve_id', flat=True))
        for eleve in (Eleve.objects
                      .filter(classe__ecole=ecole, classe__annee_scolaire=annee_nouvelle, statut__in=STATUTS_EN_CLASSE)
                      .exclude(pk__in=deja).select_related('classe')):
            try:
                with transaction.atomic():
                    if _creer_echeancier(request, eleve, ecole, annee_nouvelle, reinscription=False):
                        resultats['echeanciers_crees'] += 1
            except Exception as exc:
                logger.warning(f"Échéancier non créé pour {eleve}: {exc}")
                messages.warning(request, f"Échéancier non créé pour {eleve.nom_complet} : {exc}")

    echecs = etat['echecs']
    request.session.pop(cle, None)
    request.session[SESSION_ANNEE_ACTIVE] = (
        annee_courante if echecs and len(echecs) == etat['total'] else annee_nouvelle
    )
    bilan = _message_final(annee_nouvelle, resultats)
    if echecs:
        bilan = bilan.replace('créée avec succès', 'préparée — passage à compléter')
        messages.warning(request, bilan)
    else:
        messages.success(request, bilan)
    for erreur in etat.get('erreurs_tarifs', []):
        messages.warning(request, erreur)
    if echecs:
        noms = ', '.join(
            f"{e.prenom} {e.nom}" for e in Eleve.objects.filter(pk__in=echecs[:20])
        )
        messages.warning(request, f"{len(echecs)} élève(s) n'ont pas pu être passés et restent "
                                  f"en {annee_courante} : {noms}. Vous pouvez relancer la création "
                                  f"pour réessayer ou les déplacer manuellement.")
        from django.urls import reverse
        return redirect(reverse('eleves:nouvelle_annee_apercu') + f'?annee_courante={annee_courante}')
    return redirect('eleves:gestion_classes')
