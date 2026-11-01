"""Tests d'accueil sur les classes et les élèves de l'école.

Le module reprend les classes (``eleves.Classe``) et les élèves
(``eleves.Eleve``) déjà enregistrés, mais garde ses propres matières, notes et
paramètres. Aucune relation avec les modèles du module ``notes`` : une note de
test d'accueil ne peut pas entrer dans une moyenne mensuelle, une composition
ou un classement scolaire.
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models

from eleves.models import Classe, Eleve
from synchronisation.mixins import SyncTrackedModel

NOTE_SUR_DEFAUT = 20
MOYENNE_ADMISSION_DEFAUT = Decimal('10')

# Mêmes règles que le module Notes : maternelle et primaire notés sur 10 sans
# coefficient, secondaire (collège, lycée) sur 20 avec coefficients.
NIVEAUX_MATERNELLE = {'GARDERIE', 'TOUTE_PETITE_SECTION', 'PETITE_SECTION', 'MOYENNE_SECTION',
                      'GRANDE_SECTION', 'MATERNELLE'}
BAREME_PAR_NIVEAU = {'MATERNELLE': 10, 'PRIMAIRE': 10, 'SECONDAIRE': 20}
LIBELLES_NIVEAU = {'MATERNELLE': 'Maternelle', 'PRIMAIRE': 'Primaire', 'SECONDAIRE': 'Secondaire'}


def niveau_enseignement(classe):
    """'MATERNELLE', 'PRIMAIRE' ou 'SECONDAIRE' d'après le niveau de la classe."""
    niveau = classe.niveau or ''
    if niveau in NIVEAUX_MATERNELLE:
        return 'MATERNELLE'
    if niveau.startswith('PRIMAIRE'):
        return 'PRIMAIRE'
    return 'SECONDAIRE'


class ParametreAccueil(SyncTrackedModel):
    """Barème, moyenne d'admission et date du test d'accueil d'une classe."""
    BAREME_CHOICES = [(10, 'Sur 10'), (20, 'Sur 20')]

    classe = models.OneToOneField(Classe, on_delete=models.CASCADE, related_name='parametre_test_accueil')
    date_test = models.DateField(null=True, blank=True, verbose_name="Date du test")
    note_sur = models.PositiveSmallIntegerField(choices=BAREME_CHOICES, default=NOTE_SUR_DEFAUT,
                                                verbose_name="Notes sur")
    moyenne_admission = models.DecimalField(
        max_digits=4, decimal_places=2, default=MOYENNE_ADMISSION_DEFAUT,
        validators=[MinValueValidator(Decimal('0'))], verbose_name="Moyenne d'admission",
    )
    observations = models.TextField(blank=True, verbose_name="Observations")

    cree_par = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name='parametres_test_accueil_crees')
    date_creation = models.DateTimeField(auto_now_add=True)
    date_modification = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Paramètres du test d'accueil"
        verbose_name_plural = "Paramètres du test d'accueil"

    def __str__(self):
        return f"Test d'accueil - {self.classe}"

    def clean(self):
        if self.moyenne_admission is not None and self.note_sur and self.moyenne_admission > self.note_sur:
            raise ValidationError({'moyenne_admission': f"La moyenne d'admission ne peut pas dépasser {self.note_sur}."})

    @classmethod
    def pour(cls, classe):
        """Paramètres enregistrés de la classe, ou valeurs par défaut de son niveau (non enregistrées)."""
        return cls.objects.filter(classe=classe).first() or cls.defaut(classe)

    @classmethod
    def defaut(cls, classe):
        """Barème du niveau de la classe et admission à la moitié, non enregistrés."""
        note_sur = BAREME_PAR_NIVEAU[niveau_enseignement(classe)]
        return cls(classe=classe, note_sur=note_sur, moyenne_admission=Decimal(note_sur) / 2)

    @property
    def niveau(self):
        return niveau_enseignement(self.classe)

    @property
    def libelle_niveau(self):
        return LIBELLES_NIVEAU[self.niveau]

    @property
    def avec_coefficients(self):
        """Comme au module Notes, seuls les coefficients du secondaire comptent."""
        return self.niveau == 'SECONDAIRE'


class MatiereAccueil(SyncTrackedModel):
    """Matière évaluée au test d'accueil d'une classe, avec son coefficient."""
    classe = models.ForeignKey(Classe, on_delete=models.CASCADE, related_name='matieres_test_accueil')
    nom = models.CharField(max_length=100, verbose_name="Matière")
    coefficient = models.DecimalField(
        max_digits=4, decimal_places=2, default=Decimal('1'),
        validators=[MinValueValidator(Decimal('0.01'))], verbose_name="Coefficient",
    )
    ordre = models.PositiveSmallIntegerField(default=0, verbose_name="Ordre d'affichage")

    date_creation = models.DateTimeField(auto_now_add=True)
    date_modification = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Matière de test d'accueil"
        verbose_name_plural = "Matières de test d'accueil"
        ordering = ['ordre', 'nom']
        unique_together = ['classe', 'nom']

    def __str__(self):
        return f"{self.nom} (coef. {self.coefficient})"


class NoteAccueil(SyncTrackedModel):
    """Note d'un élève dans une matière du test d'accueil."""
    eleve = models.ForeignKey(Eleve, on_delete=models.CASCADE, related_name='notes_test_accueil')
    matiere = models.ForeignKey(MatiereAccueil, on_delete=models.CASCADE, related_name='notes')
    note = models.DecimalField(max_digits=5, decimal_places=2, validators=[MinValueValidator(Decimal('0'))],
                               verbose_name="Note")

    saisi_par = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                  related_name='notes_test_accueil_saisies')
    date_modification = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Note de test d'accueil"
        verbose_name_plural = "Notes de test d'accueil"
        unique_together = ['eleve', 'matiere']

    def __str__(self):
        return f"{self.eleve} - {self.matiere.nom} : {self.note}"

    def clean(self):
        if self.note is not None and self.matiere_id:
            note_sur = ParametreAccueil.pour(self.matiere.classe).note_sur
            if self.note > note_sur:
                raise ValidationError({'note': f"La note ne peut pas dépasser {note_sur}."})
