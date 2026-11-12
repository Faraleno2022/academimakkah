"""Commande de diagnostic des refus « Relation introuvable » : lecture seule, fiches manquantes regroupées."""

from io import StringIO
from uuid import uuid4

from django.core.management import call_command
from django.test import TestCase

from eleves.models import Classe, Ecole, Eleve

from .models import SyncChange


class DiagnosticRelationsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.ecole = Ecole.objects.create(nom='École diag', adresse='Conakry', telephone='+224620000801', directeur='D')
        cls.classe = Classe.objects.create(ecole=cls.ecole, nom='7A', niveau='COLLEGE_7', annee_scolaire='2026-2027')
        cls.present = Eleve.objects.create(matricule='D-1', prenom='Présent', nom='Là', sexe='M', classe=cls.classe)

    def _refus(self, uuid_eleve, modele='paiements.Paiement', **payload):
        return SyncChange.objects.create(
            ecole=self.ecole, model_label=modele, operation='UPDATE', object_uuid=uuid4(),
            payload={'eleve': {'sync_uuid': str(uuid_eleve), 'pk': 99}, **payload},
            statut=SyncChange.STATUT_FAILED, erreur='Relation introuvable pour eleve.',
        )

    def _lancer(self, **options):
        sortie = StringIO()
        call_command('diagnostic_relations_sync', stdout=sortie, **options)
        return sortie.getvalue()

    def test_aucun_refus(self):
        self.assertIn('rien à signaler', self._lancer())

    def test_regroupe_par_fiche_manquante_et_donne_des_indices(self):
        absent = uuid4()
        for _ in range(3):
            self._refus(absent, numero_recu='REC-2026-0042', montant='150000')
        self._refus(absent, modele='paiements.EcheancierPaiement', annee_scolaire='2026-2027')
        self._refus(self.present.sync_uuid, numero_recu='REC-OK')      # fiche existante : refus déjà résolu
        autres = SyncChange.objects.create(                               # autre erreur : ignorée
            ecole=self.ecole, model_label='paiements.Paiement', operation='UPDATE', object_uuid=uuid4(),
            payload={}, statut=SyncChange.STATUT_FAILED, erreur='Montant négatif.',
        )

        sortie = self._lancer()
        self.assertIn('5 refus', sortie)
        self.assertIn('2 fiche(s) manquante(s)', sortie)
        self.assertIn(str(absent), sortie)
        self.assertIn('inconnue du serveur', sortie)
        self.assertIn('3 × UPDATE paiements.Paiement', sortie)
        self.assertIn('1 × UPDATE paiements.EcheancierPaiement', sortie)
        self.assertIn('numero_recu=REC-2026-0042', sortie)               # de quoi retrouver la personne
        self.assertIn('EXISTE maintenant', sortie)                       # le refus de la fiche présente est résolu
        self.assertNotIn('Montant négatif', sortie)
        self.assertEqual(autres.erreur, SyncChange.objects.get(pk=autres.pk).erreur)

    def test_lecture_seule_et_filtre_par_ecole(self):
        self._refus(uuid4(), numero_recu='REC-1')
        avant = list(SyncChange.objects.values_list('pk', 'statut', 'erreur'))
        self._lancer()
        self.assertEqual(avant, list(SyncChange.objects.values_list('pk', 'statut', 'erreur')))  # rien modifié

        autre = Ecole.objects.create(nom='Autre diag', adresse='Conakry', telephone='+224620000802', directeur='D')
        self.assertIn('rien à signaler', self._lancer(ecole=autre.pk))
