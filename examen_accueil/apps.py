from django.apps import AppConfig


class ExamenAccueilConfig(AppConfig):
    """Tests d'accueil (tests d'entrée / de niveau des candidats).

    Module volontairement isolé du module ``notes`` : ses notes n'entrent
    jamais dans les moyennes mensuelles ni dans les compositions.
    """
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'examen_accueil'
    verbose_name = "Tests d'accueil"
