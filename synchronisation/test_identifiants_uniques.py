"""Matricules et numéros de reçu répétés d'une école à l'autre : la fiche est enregistrée, pas refusée."""

from decimal import Decimal
from uuid import uuid4

from django.db import IntegrityError
from django.test import TestCase

from eleves.models import Classe, Ecole, Eleve
from paiements.models import ModePaiement, Paiement, TypePaiement

from .engine import build_change_instance


def _ref(obj):
    return {'sync_uuid': str(obj.sync_uuid), 'pk': obj.pk}


class IdentifiantsUniquesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.a = Ecole.objects.create(nom='École A', adresse='Conakry', telephone='+224620000901', directeur='D', etat='VALIDE')
        cls.b = Ecole.objects.create(nom='École B', adresse='Conakry', telephone='+224620000902', directeur='D', etat='VALIDE')
        cls.classe_a = Classe.objects.create(ecole=cls.a, nom='8A', niveau='COLLEGE_8', annee_scolaire='2026-2027')
        cls.classe_b = Classe.objects.create(ecole=cls.b, nom='8A', niveau='COLLEGE_8', annee_scolaire='2026-2027')
        cls.type = TypePaiement.objects.create(nom='Tranche 1')
        cls.mode = ModePaiement.objects.create(nom='Espèces')
        # L'école A a déjà CL8-001 et REC20260001 en ligne
        cls.eleve_a = Eleve.objects.create(matricule='CL8-001', prenom='Alpha', nom='Un', sexe='M', classe=cls.classe_a)
        cls.paiement_a = Paiement.objects.create(
            eleve=cls.eleve_a, type_paiement=cls.type, mode_paiement=cls.mode, montant=Decimal('100000'),
            date_paiement='2026-10-01', annee_scolaire='2026-2027', numero_recu='REC20260001',
        )

    def _eleve(self, matricule, ecole, classe, uuid=None, operation='CREATE'):
        return build_change_instance('eleves.Eleve', uuid or uuid4(), {
            'matricule': matricule, 'prenom': 'Beta', 'nom': 'Deux', 'sexe': 'F', 'classe': _ref(classe),
        }, operation=operation, ecole=ecole)

    def _paiement(self, numero, eleve, ecole, uuid=None):
        return build_change_instance('paiements.Paiement', uuid or uuid4(), {
            'eleve': _ref(eleve), 'type_paiement': _ref(self.type), 'mode_paiement': _ref(self.mode),
            'montant': '40000', 'date_paiement': '2026-10-05', 'annee_scolaire': '2026-2027',
            'numero_recu': numero, 'statut': 'EN_ATTENTE',
        }, operation='CREATE', ecole=ecole)

    def test_matricule_deja_pris_par_une_autre_ecole(self):
        uuid = uuid4()
        eleve = self._eleve('CL8-001', self.b, self.classe_b, uuid)
        self.assertEqual(eleve.matricule, f'CL8-001-E{self.b.pk}')
        self.assertEqual(Eleve.objects.get(pk=self.eleve_a.pk).matricule, 'CL8-001')   # l'autre école n'est pas touchée
        # Le poste renvoie la même fiche (mise à jour) : même résultat, pas de nouvelle fiche
        encore = self._eleve('CL8-001', self.b, self.classe_b, uuid, operation='UPDATE')
        self.assertEqual((encore.pk, encore.matricule), (eleve.pk, f'CL8-001-E{self.b.pk}'))
        self.assertEqual(Eleve.objects.filter(classe__ecole=self.b).count(), 1)

    def test_matricule_libre_ou_meme_ecole_inchange(self):
        libre = self._eleve('CL8-002', self.b, self.classe_b)
        self.assertEqual(libre.matricule, 'CL8-002')                                   # aucun conflit : rien ne change
        # Même école, autre fiche, même matricule : vrai doublon, refusé comme avant
        with self.assertRaises(IntegrityError):
            self._eleve('CL8-002', self.b, self.classe_b)

    def test_numero_de_recu_deja_pris(self):
        eleve_b = self._eleve('CL8-050', self.b, self.classe_b)
        paiement = self._paiement('REC20260001', eleve_b, self.b)
        self.assertEqual(paiement.numero_recu, f'REC20260001-E{self.b.pk}')
        self.assertEqual(Paiement.objects.get(pk=self.paiement_a.pk).numero_recu, 'REC20260001')
        # Un second reçu libre de l'école B garde son numéro
        self.assertEqual(self._paiement('REC20260099', eleve_b, self.b).numero_recu, 'REC20260099')

    def test_numero_long_garde_la_longueur_maximale(self):
        eleve_b = self._eleve('CL8-051', self.b, self.classe_b)
        Paiement.objects.create(
            eleve=self.eleve_a, type_paiement=self.type, mode_paiement=self.mode, montant=Decimal('1000'),
            date_paiement='2026-10-02', annee_scolaire='2026-2027', numero_recu='REC2026-4838-0001',
        )
        paiement = self._paiement('REC2026-4838-0001', eleve_b, self.b)
        self.assertLessEqual(len(paiement.numero_recu), 20)
        self.assertTrue(paiement.numero_recu.startswith('REC2026-4838-0001'))
        self.assertNotEqual(paiement.numero_recu, 'REC2026-4838-0001')
