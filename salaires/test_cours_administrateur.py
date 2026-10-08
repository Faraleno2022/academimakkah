"""Administrateur qui donne des cours au secondaire, payés en plus du fixe."""

import uuid
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse
from reportlab.platypus import Paragraph, Table

from eleves.models import Classe, Ecole
from rapports.models import TableauBord
from synchronisation.engine import build_change_instance, serialize_instance

from .documents_paie import _elements_bulletin
from .forms import AffectationClasseForm
from .models import (
    AffectationClasse,
    DetailHeuresClasse,
    Enseignant,
    EtatSalaire,
    PeriodeSalaire,
    TypeEnseignant,
)
from .services import calculer_etat_salaire, charge_ou_fonction
from .tests import TEST_MIDDLEWARE


@override_settings(MIDDLEWARE=TEST_MIDDLEWARE)
class CoursAdministrateurTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser(
            username='paie-admin-cours', email='cours@example.com', password='x',
        )
        self.ecole = Ecole.objects.create(
            nom='École cours', adresse='Conakry', telephone='+224610000099',
            directeur='Direction',
        )
        self.classe_a = Classe.objects.create(
            ecole=self.ecole, nom='10e A', niveau='COLLEGE_10', annee_scolaire='2025-2026',
        )
        self.classe_b = Classe.objects.create(
            ecole=self.ecole, nom='Terminale SM', niveau='TERMINALE', annee_scolaire='2025-2026',
        )
        self.classe_primaire = Classe.objects.create(
            ecole=self.ecole, nom='CE2', niveau='PRIMAIRE_3', annee_scolaire='2025-2026',
        )
        self.periode = PeriodeSalaire.objects.create(
            mois=7, annee=2026, ecole=self.ecole,
            nombre_semaines=Decimal('4'), cree_par=self.user,
        )
        self.admin = Enseignant.objects.create(
            nom='Diallo', prenoms='Mamadou', ecole=self.ecole,
            type_enseignant=TypeEnseignant.ADMINISTRATEUR, fonction='Censeur',
            statut='ACTIF', salaire_fixe=Decimal('1000000'),
            date_embauche=date(2025, 1, 1), cree_par=self.user,
        )
        self.client.force_login(self.user)

    def affecter(self, classe, heures, taux, matiere=''):
        return AffectationClasse.objects.create(
            enseignant=self.admin, classe=classe,
            heures_par_semaine=Decimal(heures), taux_horaire=Decimal(taux),
            matiere=matiere, date_debut=date(2025, 9, 1),
        )

    def test_cours_ajoutes_au_salaire_fixe(self):
        self.affecter(self.classe_a, '3', '10000', 'Maths')
        self.affecter(self.classe_b, '2', '15000', 'PC')

        etat, _ = calculer_etat_salaire(self.admin, self.periode, self.user)

        # 3 h × 4 semaines × 10 000 + 2 h × 4 semaines × 15 000
        self.assertEqual(etat.salaire_base, Decimal('1240000.00'))
        self.assertEqual(etat.salaire_net, Decimal('1240000.00'))
        details = {
            d.affectation_classe.classe_id: d
            for d in DetailHeuresClasse.objects.filter(etat_salaire=etat)
        }
        self.assertEqual(details[self.classe_a.pk].heures_realisees, Decimal('12.00'))
        self.assertEqual(details[self.classe_a.pk].montant, Decimal('120000.00'))
        self.assertEqual(details[self.classe_b.pk].taux_horaire_applique, Decimal('15000.00'))
        self.assertEqual(
            charge_ou_fonction(self.admin, self.periode), 'Censeur (cours : Maths/PC)'
        )

    def test_sans_cours_le_salaire_reste_fixe(self):
        etat, _ = calculer_etat_salaire(self.admin, self.periode, self.user)
        self.assertEqual(etat.salaire_base, Decimal('1000000.00'))
        self.assertFalse(etat.details_heures.exists())

    def test_heures_et_taux_obligatoires(self):
        with self.assertRaises(ValidationError) as erreur:
            AffectationClasse.objects.create(
                enseignant=self.admin, classe=self.classe_a,
                heures_par_semaine=Decimal('3'), date_debut=date(2025, 9, 1),
            )
        self.assertIn('taux_horaire', erreur.exception.message_dict)

    def test_classe_du_primaire_refusee(self):
        with self.assertRaises(ValidationError):
            self.affecter(self.classe_primaire, '3', '10000')

    def test_taux_ignore_pour_un_enseignant_du_secondaire(self):
        prof = Enseignant.objects.create(
            nom='Bah', prenoms='Aïssatou', ecole=self.ecole,
            type_enseignant=TypeEnseignant.SECONDAIRE, statut='ACTIF',
            taux_horaire=Decimal('8000'), date_embauche=date(2025, 1, 1), cree_par=self.user,
        )
        form = AffectationClasseForm(
            data={
                'classe': self.classe_a.pk, 'heures_par_semaine': '4',
                'taux_horaire': '99999', 'date_debut': '2025-09-01', 'actif': 'on',
            },
            enseignant=prof,
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertIsNone(form.save().taux_horaire)

    @patch('salaires.views.timezone.localdate', return_value=date(2026, 7, 15))
    def test_fiche_enseignant_enregistre_plusieurs_classes(self, _localdate):
        donnees = {
            'nom': self.admin.nom, 'prenoms': self.admin.prenoms,
            'telephone': '', 'email': '', 'adresse': '',
            'ecole': str(self.ecole.pk),
            'type_enseignant': TypeEnseignant.ADMINISTRATEUR,
            'statut': 'ACTIF', 'fonction': 'Censeur',
            'taux_horaire': '', 'mode_calcul_horaire': 'POINTAGE',
            'salaire_fixe': '1000000', 'heures_mensuelles': '',
            'date_embauche': '2025-01-01',
            'affectations-TOTAL_FORMS': '2',
            'affectations-INITIAL_FORMS': '0',
            'affectations-MIN_NUM_FORMS': '0',
            'affectations-MAX_NUM_FORMS': '1000',
            'affectations-0-classe': str(self.classe_a.pk),
            'affectations-0-heures_par_semaine': '3',
            'affectations-0-taux_horaire': '10000',
            'affectations-0-matiere': 'Maths',
            'affectations-0-date_debut': '2025-09-01',
            'affectations-0-actif': 'on',
            'affectations-1-classe': str(self.classe_b.pk),
            'affectations-1-heures_par_semaine': '2',
            'affectations-1-taux_horaire': '15000',
            'affectations-1-date_debut': '2025-09-01',
            'affectations-1-actif': 'on',
        }
        response = self.client.post(
            reverse('salaires:modifier_enseignant', args=[self.admin.pk]), donnees,
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.admin.affectations.count(), 2)
        etat = EtatSalaire.objects.get(enseignant=self.admin, periode=self.periode)
        self.assertEqual(etat.salaire_base, Decimal('1240000.00'))

        detail = self.client.get(reverse('salaires:detail_enseignant', args=[self.admin.pk]))
        self.assertContains(detail, 'Cours au secondaire')
        self.assertContains(detail, '15')

    def test_fiche_enseignant_refuse_un_cours_sans_taux(self):
        donnees = {
            'nom': self.admin.nom, 'prenoms': self.admin.prenoms,
            'ecole': str(self.ecole.pk),
            'type_enseignant': TypeEnseignant.ADMINISTRATEUR,
            'statut': 'ACTIF', 'fonction': 'Censeur',
            'mode_calcul_horaire': 'POINTAGE', 'salaire_fixe': '1000000',
            'date_embauche': '2025-01-01',
            'affectations-TOTAL_FORMS': '1',
            'affectations-INITIAL_FORMS': '0',
            'affectations-MIN_NUM_FORMS': '0',
            'affectations-MAX_NUM_FORMS': '1000',
            'affectations-0-classe': str(self.classe_a.pk),
            'affectations-0-heures_par_semaine': '3',
            'affectations-0-date_debut': '2025-09-01',
            'affectations-0-actif': 'on',
        }
        response = self.client.post(
            reverse('salaires:modifier_enseignant', args=[self.admin.pk]), donnees,
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.admin.affectations.exists())

    def test_ajustement_modifie_le_fixe_et_garde_les_cours(self):
        affectation = self.affecter(self.classe_a, '3', '10000')
        etat, _ = calculer_etat_salaire(self.admin, self.periode, self.user)

        page = self.client.get(reverse('salaires:ajuster_etat_salaire', args=[etat.pk]))
        self.assertContains(page, 'Salaire fixe (GNF)')
        self.assertContains(page, 'Cours au secondaire du mois : 120 000 GNF, ajoutés automatiquement.')
        self.assertContains(page, 'value="1000000.00"')

        response = self.client.post(
            reverse('salaires:ajuster_etat_salaire', args=[etat.pk]),
            {'salaire_base': '900000', 'jours_chomes': '0', 'deductions': '0', 'observations': ''},
        )
        self.assertEqual(response.status_code, 302)
        etat.refresh_from_db()
        self.assertEqual(etat.salaire_base, Decimal('1020000.00'))
        self.assertEqual(etat.details_heures.count(), 1)

        # Un recalcul garde le fixe ajusté et suit les nouvelles heures.
        affectation.heures_par_semaine = Decimal('5')
        affectation.save()
        etat, _ = calculer_etat_salaire(self.admin, self.periode, self.user)
        self.assertEqual(etat.salaire_base, Decimal('1100000.00'))

    def test_bulletin_detaille_les_cours(self):
        self.affecter(self.classe_a, '3', '10000')
        etat, _ = calculer_etat_salaire(self.admin, self.periode, self.user)

        textes = []
        for element in _elements_bulletin(etat, 18 * 28.35):
            if isinstance(element, Table):
                for ligne in element._cellvalues:
                    for cellule in ligne:
                        if isinstance(cellule, Paragraph):
                            textes.append(cellule.getPlainText())
        self.assertIn('Salaire fixe', textes)
        self.assertTrue(any(t.startswith('Cours 10E A (12 h') for t in textes), textes)


class TableauBordSansProprietaireTests(TestCase):
    def test_tableau_recu_d_un_poste_est_accepte_sans_proprietaire(self):
        user = get_user_model().objects.create_user(username='proprio', password='x')
        ecole = Ecole.objects.create(
            nom='École rapports', adresse='Conakry', telephone='+224610000098',
            directeur='Direction',
        )
        source = TableauBord.objects.create(nom='Suivi', proprietaire=user)
        payload = serialize_instance(source)
        self.assertIsNone(payload['proprietaire'])

        tableau = build_change_instance(
            'rapports.TableauBord', uuid.uuid4(), payload,
            operation='CREATE', ecole=ecole,
        )

        self.assertEqual(tableau.nom, 'Suivi')
        self.assertIsNone(tableau.proprietaire)


@override_settings(MIDDLEWARE=TEST_MIDDLEWARE)
class FormatDatesFicheTests(TestCase):
    def test_fiche_modifiee_affiche_les_dates_au_format_iso(self):
        user = get_user_model().objects.create_superuser(
            username='dates-fiche', email='dates@example.com', password='x',
        )
        ecole = Ecole.objects.create(
            nom='École dates', adresse='Conakry', telephone='+224610000097',
            directeur='Direction',
        )
        classe = Classe.objects.create(
            ecole=ecole, nom='10e B', niveau='COLLEGE_10', annee_scolaire='2025-2026',
        )
        prof = Enseignant.objects.create(
            nom='Sow', prenoms='Ibrahima', ecole=ecole,
            type_enseignant=TypeEnseignant.SECONDAIRE, statut='ACTIF',
            taux_horaire=Decimal('8000'), date_embauche=date(2025, 1, 6), cree_par=user,
        )
        AffectationClasse.objects.create(
            enseignant=prof, classe=classe, heures_par_semaine=Decimal('4'),
            date_debut=date(2025, 9, 1),
        )
        self.client.force_login(user)

        page = self.client.get(reverse('salaires:modifier_enseignant', args=[prof.pk]))

        self.assertContains(page, 'value="2025-01-06"')
        self.assertContains(page, 'value="2025-09-01"')
