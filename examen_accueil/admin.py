from django.contrib import admin

from .models import MatiereAccueil, NoteAccueil, ParametreAccueil


@admin.register(ParametreAccueil)
class ParametreAccueilAdmin(admin.ModelAdmin):
    list_display = ['classe', 'date_test', 'note_sur', 'moyenne_admission']
    list_filter = ['classe__annee_scolaire', 'classe__ecole']
    search_fields = ['classe__nom']


@admin.register(MatiereAccueil)
class MatiereAccueilAdmin(admin.ModelAdmin):
    list_display = ['nom', 'coefficient', 'classe']
    search_fields = ['nom', 'classe__nom']


@admin.register(NoteAccueil)
class NoteAccueilAdmin(admin.ModelAdmin):
    list_display = ['eleve', 'matiere', 'note']
    search_fields = ['eleve__nom', 'eleve__prenom', 'eleve__matricule', 'matiere__nom']
