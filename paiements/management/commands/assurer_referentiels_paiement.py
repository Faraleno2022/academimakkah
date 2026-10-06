from django.core.management.base import BaseCommand

from paiements.referentiels import assurer_referentiels_paiement


class Command(BaseCommand):
    help = "Crée les modes et types de paiement par défaut qui manquent (sans doublon)."

    def handle(self, *args, **options):
        modes, types = assurer_referentiels_paiement()
        for nom in modes:
            self.stdout.write(self.style.SUCCESS(f"Mode créé : {nom}"))
        for nom in types:
            self.stdout.write(self.style.SUCCESS(f"Type créé : {nom}"))
        if not (modes or types):
            self.stdout.write("Tous les modes et types de paiement par défaut existent déjà.")
