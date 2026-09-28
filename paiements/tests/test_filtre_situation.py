from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from eleves.models import Classe, Ecole, Eleve, Responsable
from paiements.export_paiements_filtres import eleve_ids_par_situation
from paiements.models import (
    EcheancierPaiement,
    ModePaiement,
    Paiement,
    PaiementRemise,
    RemiseReduction,
    TypePaiement,
)
from paiements.tests.support import MIDDLEWARE_SANS_LICENCE

ANNEE = '2025-2026'


@override_settings(MIDDLEWARE=MIDDLEWARE_SANS_LICENCE)
class FiltreSituationPaiementTests(TestCase):
    """Filtre « Niveau de paiement » (retard / reste / soldé) de la liste des paiements.

    La première version agrégeait les remises par jointure, ce qui générait
    un HAVING rejeté par MySQL (erreur 500 en production).
    """

    def setUp(self):
        self.today = date.today()
        passe = self.today - timedelta(days=30)
        futur = self.today + timedelta(days=60)
        ecole = Ecole.objects.create(
            nom='École Situation', adresse='Conakry',
            telephone='+224620000101', directeur='Direction',
        )
        self.classe = Classe.objects.create(
            nom='7e A', ecole=ecole, niveau='COLLEGE_7', annee_scolaire=ANNEE,
        )
        responsable = Responsable.objects.create(
            prenom='Parent', nom='Situation', relation='PERE',
            telephone='+224620000102', adresse='Conakry',
        )
        type_paiement = TypePaiement.objects.create(nom='Scolarité')
        mode = ModePaiement.objects.create(nom='Espèces')
        remise_a = RemiseReduction.objects.create(
            nom='Fratrie', type_remise='MONTANT_FIXE', valeur=Decimal('50000'),
            motif='FRATRIE', date_debut=passe, date_fin=futur,
        )
        remise_b = RemiseReduction.objects.create(
            nom='Mérite', type_remise='MONTANT_FIXE', valeur=Decimal('50000'),
            motif='MERITE', date_debut=passe, date_fin=futur,
        )

        def eleve(matricule, prenom):
            return Eleve.objects.create(
                nom='Situation', prenom=prenom, matricule=matricule,
                classe=self.classe, sexe='F', date_naissance=date(2012, 1, 1),
                lieu_naissance='Conakry', date_inscription=date(2025, 9, 1),
                responsable_principal=responsable,
            )

        def paiement(el, montant, remises=()):
            p = Paiement.objects.create(
                eleve=el, type_paiement=type_paiement, mode_paiement=mode,
                montant=Decimal(montant), annee_scolaire=ANNEE,
                date_paiement=passe, statut='VALIDE',
            )
            for r in remises:
                PaiementRemise.objects.create(paiement=p, remise=r, montant_remise=r.valeur)
            return p

        def echeancier(el, insc_du, t1_du, t1_date, paye_insc, paye_t1):
            eche, _ = EcheancierPaiement.objects.get_or_create(
                eleve=el, annee_scolaire=ANNEE,
                defaults={
                    'date_echeance_inscription': passe,
                    'date_echeance_tranche_1': t1_date,
                    'date_echeance_tranche_2': futur,
                    'date_echeance_tranche_3': futur,
                },
            )
            # update() contourne les recalculs automatiques : on fixe l'état voulu.
            EcheancierPaiement.objects.filter(pk=eche.pk).update(
                frais_inscription_du=Decimal(insc_du), tranche_1_due=Decimal(t1_du),
                tranche_2_due=0, tranche_3_due=0,
                date_echeance_inscription=passe, date_echeance_tranche_1=t1_date,
                date_echeance_tranche_2=futur, date_echeance_tranche_3=futur,
                frais_inscription_paye=Decimal(paye_insc), tranche_1_payee=Decimal(paye_t1),
                tranche_2_payee=0, tranche_3_payee=0,
            )

        # En retard malgré deux remises (300 000 exigibles, 100 000 de remises).
        self.en_retard = eleve('SIT-001', 'Retard')
        paiement(self.en_retard, 10000, remises=[remise_a])
        paiement(self.en_retard, 10000, remises=[remise_b])
        echeancier(self.en_retard, 100000, 200000, passe, 0, 0)

        # Retard couvert par une remise, mais tranche future encore due.
        self.reste = eleve('SIT-002', 'Reste')
        paiement(self.reste, 50000, remises=[remise_a])
        echeancier(self.reste, 100000, 200000, futur, 50000, 0)

        # Entièrement soldé.
        self.solde = eleve('SIT-003', 'Solde')
        paiement(self.solde, 300000)
        echeancier(self.solde, 100000, 200000, passe, 100000, 200000)

        self.user = get_user_model().objects.create_superuser(
            'situation_admin', 'situation@example.com', 'x',
        )
        self.client.force_login(self.user)

    def _ids(self, situation):
        return set(eleve_ids_par_situation(situation, ANNEE, today=self.today)
                   .values_list('eleve_id', flat=True))

    def test_classement_des_eleves(self):
        self.assertEqual(self._ids('retard'), {self.en_retard.pk})
        self.assertEqual(self._ids('reste'), {self.en_retard.pk, self.reste.pk})
        self.assertEqual(self._ids('solde'), {self.solde.pk})

    def test_requete_sans_having(self):
        sql = str(eleve_ids_par_situation('retard', ANNEE).query).upper()
        self.assertNotIn('HAVING', sql)

    def test_liste_paiements_filtre_retard(self):
        response = self.client.get(reverse('paiements:liste_paiements'), {
            'q': '', 'classe_id': self.classe.pk, 'situation': 'retard',
            'mode_id': '', 'type_id': '', 'annee': ANNEE,
        })
        self.assertEqual(response.status_code, 200)
        eleves = {p.eleve_id for p in response.context['page_obj'].object_list}
        self.assertEqual(eleves, {self.en_retard.pk})

    def test_exports_filtres_situation(self):
        params = {'classe_id': self.classe.pk, 'situation': 'retard', 'annee': ANNEE}
        for name in ('paiements:export_paiements_filtres_excel',
                     'paiements:export_paiements_filtres_pdf'):
            with self.subTest(name=name):
                response = self.client.get(reverse(name), params)
                self.assertEqual(response.status_code, 200)
