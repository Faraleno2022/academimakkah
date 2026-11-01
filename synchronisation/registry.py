SYNC_MODEL_LABELS = [
    'eleves.Ecole',
    'eleves.Classe',
    'eleves.Responsable',
    'eleves.GrilleTarifaire',
    'eleves.Eleve',
    'eleves.HistoriqueEleve',
    'paiements.TypePaiement',
    'paiements.ModePaiement',
    'paiements.Paiement',
    'paiements.EcheancierPaiement',
    'paiements.RemiseReduction',
    'paiements.PaiementRemise',
    'paiements.Relance',
    'paiements.TwilioInboundMessage',
    'paiements.ConfigurationPaiement',
    'depenses.CategorieDepense',
    'depenses.Fournisseur',
    'depenses.Depense',
    'depenses.PieceJustificative',
    'depenses.BudgetAnnuel',
    'depenses.HistoriqueDepense',
    'depenses.CategorieArticle',
    'depenses.Article',
    'depenses.BienEtablissement',
    'depenses.MouvementStock',
    'depenses.Inventaire',
    'depenses.LigneInventaire',
    'depenses.CategorieLivre',
    'depenses.Livre',
    'depenses.Emprunt',
    'depenses.Reservation',
    'depenses.HistoriqueLivre',
    'depenses.ParametreBibliotheque',
    'notes.ClasseNote',
    'notes.MatiereNote',
    'notes.Evaluation',
    'notes.NoteEleve',
    'notes.NoteMensuelle',
    'notes.CompositionNote',
    'notes.AppreciationMaternelle',
    'notes.BulletinMaternelle',
    'notes.ThemeBulletin',
    'notes.EvaluationMaternelle',
    'notes.NoteMaternelle',
    'notes.AnalyseTravailMaternelle',
    'notes.RecommandationMaternelle',
    'notes.Classement',
    'notes.ActiviteJournaliere',
    'notes.PieceJointeActivite',
    'bus.GrilleTarifaireBus',
    'bus.AbonnementBus',
    'bus.AbonnementCantine',
    'salaires.Enseignant',
    'salaires.AffectationClasse',
    'salaires.PeriodeSalaire',
    'salaires.EtatSalaire',
    'salaires.AvanceSalaire',
    'salaires.PresenceEnseignant',
    'salaires.DetailHeuresClasse',
    'abonnements.TypeAbonnement',
    'abonnements.Itineraire',
    'abonnements.MenuCantine',
    'abonnements.Abonnement',
    'abonnements.AbonnementBus',
    'abonnements.AbonnementCantine',
    'abonnements.PresenceCantine',
    'rapports.TypeRapport',
    'rapports.Rapport',
    'rapports.TableauBord',
    'rapports.Widget',
    'rapports.ExportProgramme',
]

# Version 2 du registre : modèles ajoutés après coup. Ils restent en fin de
# liste (parents avant enfants) : le curseur de l'instantané initial désigne
# un modèle par sa position, il ne doit pas se décaler pour les postes en cours.
MODELES_REGISTRE_2 = [
    'depenses.AbonnementInformatique',
    'depenses.ContributionPapierRame',
    'depenses.DepenseCuisine',
    'depenses.DepenseDocument',
    'depenses.Versement',
    'notes.CreneauEmploiDuTemps',
    'notes.Devoir',
    'notes.RemiseDevoir',
    'notes.NoteSuivi',
    'presence.PresenceJournaliere',
]
SYNC_MODEL_LABELS += MODELES_REGISTRE_2

# Version 3 : remises accordées sur l'abonnement bus (après bus.GrilleTarifaireBus).
MODELES_REGISTRE_3 = [
    'bus.RemiseBus',
]
SYNC_MODEL_LABELS += MODELES_REGISTRE_3

# Version 4 : module Test d'accueil sur les classes et élèves de l'école
# (parents avant enfants). Remplace la première forme du module (1.3.28, non
# diffusée) qui avait ses propres classes et candidats.
MODELES_REGISTRE_4 = [
    'examen_accueil.ParametreAccueil',
    'examen_accueil.MatiereAccueil',
    'examen_accueil.NoteAccueil',
]
SYNC_MODEL_LABELS += MODELES_REGISTRE_4

# Version du registre connue de ce code. Un poste l'annonce au serveur, qui ne
# lui envoie pas les modèles qu'il ne sait pas appliquer.
REGISTRE_VERSION = 4
MODELES_PAR_VERSION = {2: MODELES_REGISTRE_2, 3: MODELES_REGISTRE_3, 4: MODELES_REGISTRE_4}

SYNC_MODEL_SET = set(SYNC_MODEL_LABELS)


def modeles_inconnus(version):
    """Modèles introduits après la version ``version`` du registre."""
    return [
        label
        for numero, labels in sorted(MODELES_PAR_VERSION.items()) if numero > version
        for label in labels
    ]


def version_registre(valeur):
    """Version annoncée par un poste ; les anciens postes n'annoncent rien (1)."""
    try:
        return max(1, int(valeur))
    except (TypeError, ValueError):
        return 1


# Clé naturelle (contrainte d'unicité) : deux postes hors ligne peuvent créer
# la même ligne sous deux sync_uuid différents. La dernière synchronisée
# remplace l'autre au lieu d'être refusée.
CLES_NATURELLES = {
    'presence.PresenceJournaliere': ('eleve', 'date'),
    'notes.RemiseDevoir': ('devoir', 'eleve'),
    'examen_accueil.ParametreAccueil': ('classe',),
    'examen_accueil.NoteAccueil': ('eleve', 'matiere'),
    # Modes et types créés par défaut sur un poste avant sa liaison au serveur.
    'paiements.ModePaiement': ('nom',),
    'paiements.TypePaiement': ('nom',),
}
