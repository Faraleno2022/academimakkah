"""
WhiteNoise avec secours sur les sources statiques non collectées.
"""
from whitenoise.middleware import WhiteNoiseMiddleware


class StaticFallbackWhiteNoiseMiddleware(WhiteNoiseMiddleware):
    """
    Sert les fichiers de STATICFILES_DIRS absents de STATIC_ROOT.

    WhiteNoise standard laisse les finders remplacer les fichiers collectés,
    ce qui ferait perdre leurs variantes gzip/Brotli. Ici les fichiers collectés
    restent prioritaires : les finders ne comblent que les oublis de
    collectstatic (ex. vidéos publiques ajoutées sans collecte sur PythonAnywhere).
    """

    def add_files_from_finders(self):
        collected = self.files
        self.files = {}
        super().add_files_from_finders()
        found, self.files = self.files, collected
        for url, static_file in found.items():
            self.files.setdefault(url, static_file)
