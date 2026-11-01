from django.urls import path

from . import views

app_name = 'examen_accueil'

urlpatterns = [
    path('', views.liste_classes, name='liste'),
    path('classe/<int:pk>/', views.detail_classe, name='detail'),
    path('classe/<int:pk>/modifier/', views.modifier_classe, name='modifier_classe'),
    path('classe/<int:pk>/supprimer/', views.supprimer_classe, name='supprimer_classe'),
    path('classe/<int:pk>/notes/', views.saisie_notes, name='saisie_notes'),
    path('classe/<int:pk>/resultats/', views.resultats, name='resultats'),
    path('classe/<int:pk>/resultats/pdf/', views.resultats_pdf, name='resultats_pdf'),
    path('classe/<int:pk>/resultats/excel/', views.resultats_excel, name='resultats_excel'),
    path('classe/<int:pk>/bulletins/', views.bulletins_pdf, name='bulletins_pdf'),
    path('matiere/<int:pk>/modifier/', views.modifier_matiere, name='modifier_matiere'),
    path('matiere/<int:pk>/supprimer/', views.supprimer_matiere, name='supprimer_matiere'),
    path('candidat/<int:pk>/modifier/', views.modifier_candidat, name='modifier_candidat'),
    path('candidat/<int:pk>/supprimer/', views.supprimer_candidat, name='supprimer_candidat'),
    path('candidat/<int:pk>/bulletin/', views.bulletin_candidat_pdf, name='bulletin_candidat'),
]
