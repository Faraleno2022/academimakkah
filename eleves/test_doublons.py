"""Doublons d'élèves dans une même classe : détection, proposition et correction."""

from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from administration.models import CorbeilleEleve
from paiements.models import ModePaiement, Paiement, TypePaiement

from .doublons import detecter
from .models import Classe, Ecole, Eleve

SANS_LICENCE = [m for m in settings.MIDDLEWARE if 'licence_middleware' not in m]


@override_settings(MIDDLEWARE=SANS_LICENCE, PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class DoublonsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.ecole = Ecole.objects.create(nom='École doublons', adresse='Conakry', telephone='+224620000701', directeur='D')
        autre = Ecole.objects.create(nom='Autre doublons', adresse='Conakry', telephone='+224620000702', directeur='D')
        cls.classe = Classe.objects.create(ecole=cls.ecole, nom='9B', niveau='COLLEGE_9', annee_scolaire='2026-2027')
        cls.autre_classe = Classe.objects.create(ecole=cls.ecole, nom='9C', niveau='COLLEGE_9', annee_scolaire='2026-2027')
        classe_x = Classe.objects.create(ecole=autre, nom='9B', niveau='COLLEGE_9', annee_scolaire='2026-2027')

        cree = lambda m, p, n, c=None: Eleve.objects.create(matricule=m, prenom=p, nom=n, sexe='F', classe=c or cls.classe)
        cls.germaine_1 = cree('CL9-182', 'Germaine', 'Dopaldogu')
        cls.germaine_2 = cree('CL9-185', 'GERMAINE', 'DOPALDOGU ')       # même nom, casse et espace différents
        cls.micheline_1 = cree('CL9-167', 'Micheline', 'Loua')
        cls.micheline_2 = cree('CL9-186', 'Loua', 'Micheline')           # prénom et nom inversés
        cls.ousmane = cree('CL9-200', 'Ousmane', 'Camara')
        cree('CL9C-1', 'Ousmane', 'Camara', cls.autre_classe)            # même nom, autre classe : pas un doublon
        cree('X-1', 'Germaine', 'Dopaldogu', classe_x)                   # autre école : jamais vu
        cls.mariam = cree('CL9-210', 'Mariame', 'Keita')
        cls.mariama = cree('CL9-211', 'Mariama', 'Keita')                # une lettre d'écart : à vérifier

        # La seconde fiche de Micheline porte un paiement : elle doit être gardée
        Paiement.objects.create(
            eleve=cls.micheline_2, type_paiement=TypePaiement.objects.create(nom='Tranche 1'),
            mode_paiement=ModePaiement.objects.create(nom='Espèces'), montant=Decimal('100000'),
            date_paiement='2026-10-01', annee_scolaire='2026-2027',
        )
        cls.admin = get_user_model().objects.create_user(username='admin-doublons', password='x')
        profil = cls.admin.profil
        profil.ecole, profil.est_compte_principal, profil.is_validated = cls.ecole, True, True
        profil.save()

    def setUp(self):
        self.client.force_login(self.admin)

    def test_detection_et_proposition(self):
        groupes = detecter(Classe.objects.filter(ecole=self.ecole))
        certains = {frozenset(f['eleve'].pk for f in g['fiches']): g for g in groupes if g['certain']}
        self.assertEqual(set(certains), {
            frozenset({self.germaine_1.pk, self.germaine_2.pk}),
            frozenset({self.micheline_1.pk, self.micheline_2.pk}),
        })
        germaine = certains[frozenset({self.germaine_1.pk, self.germaine_2.pk})]
        self.assertEqual(germaine['garde']['eleve'], self.germaine_1)          # sans données : la plus ancienne
        self.assertTrue(next(f for f in germaine['fiches'] if f['eleve'] == self.germaine_2)['proposee'])

        micheline = certains[frozenset({self.micheline_1.pk, self.micheline_2.pk})]
        self.assertEqual(micheline['garde']['eleve'], self.micheline_2)        # celle qui a un paiement
        self.assertTrue(next(f for f in micheline['fiches'] if f['eleve'] == self.micheline_1)['proposee'])

        a_verifier = [g for g in groupes if not g['certain']]
        self.assertEqual([{f['eleve'].pk for f in g['fiches']} for g in a_verifier], [{self.mariam.pk, self.mariama.pk}])
        self.assertFalse(any(f['proposee'] for f in a_verifier[0]['fiches']))  # jamais coché d'office

    def test_fiche_avec_donnees_jamais_proposee(self):
        Paiement.objects.create(
            eleve=self.germaine_2, type_paiement=TypePaiement.objects.first(), mode_paiement=ModePaiement.objects.first(),
            montant=Decimal('50000'), date_paiement='2026-10-02', annee_scolaire='2026-2027',
        )
        Paiement.objects.create(
            eleve=self.germaine_1, type_paiement=TypePaiement.objects.first(), mode_paiement=ModePaiement.objects.first(),
            montant=Decimal('50000'), date_paiement='2026-10-02', annee_scolaire='2026-2027',
        )
        groupe = next(g for g in detecter(Classe.objects.filter(pk=self.classe.pk))
                      if g['certain'] and self.germaine_1.pk in {f['eleve'].pk for f in g['fiches']})
        self.assertTrue(groupe['a_verifier'])
        self.assertFalse(any(f['proposee'] for f in groupe['fiches']))

    def test_page_et_correction_vers_la_corbeille(self):
        url = reverse('eleves:doublons_eleves')
        page = self.client.get(url, {'classe': self.classe.pk})
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'CL9-185')
        self.assertNotContains(page, 'X-1')

        rep = self.client.post(url, {'classe': self.classe.pk, 'corbeille': [self.germaine_2.pk, self.micheline_1.pk]})
        self.assertEqual(rep.status_code, 302)
        self.assertFalse(Eleve.objects.filter(pk__in=[self.germaine_2.pk, self.micheline_1.pk]).exists())
        self.assertTrue(Eleve.objects.filter(pk__in=[self.germaine_1.pk, self.micheline_2.pk]).count() == 2)
        self.assertEqual(set(CorbeilleEleve.objects.values_list('matricule', flat=True)), {'CL9-185', 'CL9-167'})
        self.assertEqual(Paiement.objects.filter(eleve=self.micheline_2).count(), 1)  # données conservées

    def test_jamais_la_derniere_fiche_ni_une_autre_ecole(self):
        url = reverse('eleves:doublons_eleves')
        # Les deux fiches de Germaine cochées : la dernière restante est protégée
        self.client.post(url, {'corbeille': [self.germaine_1.pk, self.germaine_2.pk]})
        self.assertEqual(Eleve.objects.filter(classe=self.classe, matricule__in=['CL9-182', 'CL9-185']).count(), 1)
        # Un élève sans doublon ni d'une autre école n'est jamais supprimé
        self.client.post(url, {'corbeille': [self.ousmane.pk, Eleve.objects.get(matricule='X-1').pk]})
        self.assertTrue(Eleve.objects.filter(matricule__in=['CL9-200', 'X-1']).count() == 2)

    def test_correction_reservee_aux_administrateurs(self):
        simple = get_user_model().objects.create_user(username='secretaire-doublons', password='x')
        profil = simple.profil
        profil.ecole, profil.est_compte_principal, profil.is_validated, profil.role = self.ecole, False, True, 'SECRETAIRE'
        profil.save()
        self.client.force_login(simple)
        url = reverse('eleves:doublons_eleves')
        self.assertContains(self.client.get(url), 'Seul un administrateur')
        self.client.post(url, {'corbeille': [self.germaine_2.pk]})
        self.assertTrue(Eleve.objects.filter(pk=self.germaine_2.pk).exists())
