"""Tests d'accueil : classes, matières, candidats et notes propres au module.

Ces modèles n'ont aucune relation avec ceux du module ``notes`` : une note de
test d'accueil ne peut pas entrer dans une moyenne mensuelle, une composition
ou un classement scolaire.
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models

from eleves.models import Ecole
from synchronisation.mixins import SyncTrackedModel


class ClasseAccueil(SyncTrackedModel):
    """Classe (ou session) de test d'accueil d'une école."""
    BAREME_CHOICES = [(10, 'Sur 10'), (20, 'Sur 20')]

    ecole = models.ForeignKey(Ecole, on_delete=models.CASCADE, related_name='classes_test_accueil')
    nom = models.CharField(max_length=100, verbose_name="Nom de la classe de test")
    niveau_vise = models.CharField(max_length=100, blank=True, verbose_name="Niveau visé",
                                   help_text="Ex : 7ème année, 11ème Sciences")
    annee_scolaire = models.CharField(max_length=9, verbose_name="Année scolaire", help_text="Format : 2026-2027")
    date_test = models.DateField(null=True, blank=True, verbose_name="Date du test")
    note_sur = models.PositiveSmallIntegerField(choices=BAREME_CHOICES, default=20, verbose_name="Notes sur")
    moyenne_admission = models.DecimalField(
        max_digits=4, decimal_places=2, default=Decimal('10'),
        validators=[MinValueValidator(Decimal('0'))], verbose_name="Moyenne d'admission",
    )
    observations = models.TextField(blank=True, verbose_name="Observations")

    cree_par = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name='classes_test_accueil_creees')
    date_creation = models.DateTimeField(auto_now_add=True)
    date_modification = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Classe de test d'accueil"
        verbose_name_plural = "Classes de test d'accueil"
        ordering = ['-annee_scolaire', 'nom']
        unique_together = ['ecole', 'nom', 'annee_scolaire']

    def __str__(self):
        return f"{self.nom} ({self.annee_scolaire})"

    def clean(self):
        if self.moyenne_admission is not None and self.note_sur and self.moyenne_admission > self.note_sur:
            raise ValidationError({'moyenne_admission': f"La moyenne d'admission ne peut pas dépasser {self.note_sur}."})


class MatiereAccueil(SyncTrackedModel):
    """Matière évaluée au test d'accueil, avec son coefficient."""
    classe = models.ForeignKey(ClasseAccueil, on_delete=models.CASCADE, related_name='matieres')
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


class CandidatAccueil(SyncTrackedModel):
    """Candidat inscrit au test d'accueil (pas forcément encore élève)."""
    SEXE_CHOICES = [('M', 'Masculin'), ('F', 'Féminin')]

    classe = models.ForeignKey(ClasseAccueil, on_delete=models.CASCADE, related_name='candidats')
    nom = models.CharField(max_length=100, verbose_name="Nom")
    prenom = models.CharField(max_length=150, verbose_name="Prénom(s)")
    sexe = models.CharField(max_length=1, choices=SEXE_CHOICES, blank=True, verbose_name="Sexe")
    date_naissance = models.DateField(null=True, blank=True, verbose_name="Date de naissance")
    lieu_naissance = models.CharField(max_length=100, blank=True, verbose_name="Lieu de naissance")
    ecole_origine = models.CharField(max_length=150, blank=True, verbose_name="École d'origine")
    telephone = models.CharField(max_length=30, blank=True, verbose_name="Téléphone du parent")

    date_creation = models.DateTimeField(auto_now_add=True)
    date_modification = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Candidat au test d'accueil"
        verbose_name_plural = "Candidats au test d'accueil"
        ordering = ['nom', 'prenom']

    def __str__(self):
        return self.nom_complet

    @property
    def nom_complet(self):
        return f"{self.prenom} {self.nom}".strip()


class NoteAccueil(SyncTrackedModel):
    """Note d'un candidat dans une matière du test d'accueil."""
    candidat = models.ForeignKey(CandidatAccueil, on_delete=models.CASCADE, related_name='notes')
    matiere = models.ForeignKey(MatiereAccueil, on_delete=models.CASCADE, related_name='notes')
    note = models.DecimalField(max_digits=5, decimal_places=2, validators=[MinValueValidator(Decimal('0'))],
                               verbose_name="Note")

    saisi_par = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                  related_name='notes_test_accueil_saisies')
    date_modification = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Note de test d'accueil"
        verbose_name_plural = "Notes de test d'accueil"
        unique_together = ['candidat', 'matiere']

    def __str__(self):
        return f"{self.candidat} - {self.matiere.nom} : {self.note}"

    def clean(self):
        if self.candidat_id and self.matiere_id and self.candidat.classe_id != self.matiere.classe_id:
            raise ValidationError("Le candidat et la matière doivent appartenir à la même classe de test.")
        if self.note is not None and self.matiere_id and self.note > self.matiere.classe.note_sur:
            raise ValidationError({'note': f"La note ne peut pas dépasser {self.matiere.classe.note_sur}."})
