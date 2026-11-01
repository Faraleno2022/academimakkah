from django.contrib import admin

from .models import CandidatAccueil, ClasseAccueil, MatiereAccueil, NoteAccueil


@admin.register(ClasseAccueil)
class ClasseAccueilAdmin(admin.ModelAdmin):
    list_display = ['nom', 'niveau_vise', 'annee_scolaire', 'date_test', 'note_sur', 'ecole']
    list_filter = ['annee_scolaire', 'ecole']
    search_fields = ['nom', 'niveau_vise']


@admin.register(MatiereAccueil)
class MatiereAccueilAdmin(admin.ModelAdmin):
    list_display = ['nom', 'coefficient', 'classe']
    search_fields = ['nom', 'classe__nom']


@admin.register(CandidatAccueil)
class CandidatAccueilAdmin(admin.ModelAdmin):
    list_display = ['nom', 'prenom', 'sexe', 'classe']
    search_fields = ['nom', 'prenom', 'classe__nom']


@admin.register(NoteAccueil)
class NoteAccueilAdmin(admin.ModelAdmin):
    list_display = ['candidat', 'matiere', 'note']
    search_fields = ['candidat__nom', 'candidat__prenom', 'matiere__nom']
