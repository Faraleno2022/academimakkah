from datetime import datetime, time

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from django.utils import timezone

from eleves.models import Ecole
from synchronisation.models import SyncChange
from synchronisation.sequence import attribuer_sequences, reemettre


class Command(BaseCommand):
    help = (
        "Serveur : renvoie aux postes les changements enregistres depuis une date. "
        "Rattrape les donnees que des postes ont sautees avant la correction de "
        "l'ordre de diffusion ; un poste qui les a deja appliquees les ignore."
    )

    def add_arguments(self, parser):
        parser.add_argument('--depuis', required=True, help='Date de debut (AAAA-MM-JJ).')
        parser.add_argument('--ecole', type=int, help="Limiter a une ecole (id).")
        parser.add_argument(
            '--confirmer', action='store_true',
            help='Effectue la reemission (sinon, simple decompte).',
        )

    def handle(self, *args, **options):
        try:
            jour = datetime.strptime(options['depuis'], '%Y-%m-%d').date()
        except ValueError as exc:
            raise CommandError('--depuis attend une date au format AAAA-MM-JJ.') from exc
        debut = timezone.make_aware(datetime.combine(jour, time.min))

        changements = SyncChange.objects.filter(date_creation__gte=debut).filter(
            Q(statut=SyncChange.STATUT_APPLIED)
            | Q(device__isnull=True, statut=SyncChange.STATUT_PENDING)
        )
        if options['ecole']:
            if not Ecole.objects.filter(pk=options['ecole']).exists():
                raise CommandError(f"Ecole introuvable : {options['ecole']}.")
            changements = changements.filter(ecole_id=options['ecole'])

        nombre = changements.count()
        if not options['confirmer']:
            self.stdout.write(
                f'{nombre} changement(s) seraient renvoyes aux postes depuis le {jour:%d/%m/%Y}. '
                'Relancez avec --confirmer pour les renvoyer.'
            )
            return

        attribuer_sequences()
        total = reemettre(changements)
        self.stdout.write(self.style.SUCCESS(
            f'{total} changement(s) renvoye(s) aux postes depuis le {jour:%d/%m/%Y}.'
        ))
