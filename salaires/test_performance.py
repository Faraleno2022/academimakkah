"""La consultation d'une page ne doit pas charger toute l'histoire de paie."""
from unittest.mock import patch
from datetime import date

from django.contrib.auth.models import User
from django.db import connection
from django.http import HttpResponse
from django.test import RequestFactory, TestCase
from django.test.utils import CaptureQueriesContext

from eleves.models import Ecole
from synchronisation.context import mute_sync
from .models import Enseignant, EtatSalaire, PeriodeSalaire
from .views import gestion_periodes


class PaginationPeriodesTests(TestCase):
    def test_60_periodes_sont_comptees_sans_charger_60_listes_de_salaires(self):
        user = User.objects.create_superuser('volume-paie', 'volume@example.invalid', 'test')
        with mute_sync():
            ecole = Ecole.objects.create(nom='Paie volume', adresse='Conakry',
                                         telephone='600000000', directeur='Direction')
            enseignant = Enseignant.objects.create(ecole=ecole, nom='Test', prenoms='Volume',
                type_enseignant='PRIMAIRE', salaire_fixe=100000,
                date_embauche=date(2020, 1, 1), cree_par=user)
        periodes = PeriodeSalaire.objects.bulk_create([
            PeriodeSalaire(ecole=ecole, annee=2020 + i // 12, mois=i % 12 + 1) for i in range(60)
        ])
        EtatSalaire.objects.bulk_create([
            EtatSalaire(periode=p, enseignant=enseignant, salaire_base=100000,
                        salaire_net=100000, valide=i % 2 == 0, paye=i % 3 == 0)
            for i, p in enumerate(periodes)
        ])
        request = RequestFactory().get('/salaires/periodes/', {'page': 2})
        request.user = user
        contexte = {}

        def rendu(req, template, context):
            contexte.update(context)
            for periode in context['page_obj']:
                total = getattr(periode, 'etats_total', None)
                self.assertEqual(total if total is not None else periode.etats_salaire.count(), 1)
                self.assertEqual(periode.etats_valides, int((periode.annee - 2020) * 12 + periode.mois - 1) % 2 == 0)
            return HttpResponse('OK')

        with patch('salaires.views.render', side_effect=rendu), CaptureQueriesContext(connection) as queries:
            response = gestion_periodes(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(contexte['page_obj']), 15)
        self.assertEqual(contexte['stats']['etats_calcules'], 60)
        self.assertLess(len(queries), 20, f'{len(queries)} requêtes pour une page de 15 périodes')
        print(f'\n[Mesure requêtes / 60 périodes de salaire] {len(queries)}')
