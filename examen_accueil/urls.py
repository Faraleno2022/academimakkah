from django.urls import path

from . import views

app_name = 'examen_accueil'

urlpatterns = [
    path('', views.tableau_bord, name='tableau_bord'),
    path('classes/', views.classes, name='classes'),
    path('classes/<int:classe_id>/parametres/', views.parametres_classe, name='parametres_classe'),
    path('matieres/', views.matieres, name='matieres'),
    path('matieres/<int:pk>/modifier/', views.modifier_matiere, name='modifier_matiere'),
    path('matieres/<int:pk>/supprimer/', views.supprimer_matiere, name='supprimer_matiere'),
    path('eleves/', views.eleves, name='eleves'),
    path('saisie/', views.saisie_notes, name='saisie_notes'),
    path('saisie/<int:classe_id>/modele/', views.modele_notes, name='modele_notes'),
    path('saisie/<int:classe_id>/fiche/', views.fiche_notes, name='fiche_notes'),
    path('saisie/<int:classe_id>/importer/', views.importer_notes, name='importer_notes'),
    path('consultation/', views.consultation, name='consultation'),
    path('consultation/<int:classe_id>/pdf/', views.resultats_pdf, name='resultats_pdf'),
    path('consultation/<int:classe_id>/excel/', views.resultats_excel, name='resultats_excel'),
    path('bulletins/', views.bulletins, name='bulletins'),
    path('bulletins/<int:classe_id>/pdf/', views.bulletins_pdf, name='bulletins_pdf'),
    path('bulletins/<int:classe_id>/eleve/<int:eleve_id>/pdf/', views.bulletin_eleve_pdf, name='bulletin_eleve'),
]
