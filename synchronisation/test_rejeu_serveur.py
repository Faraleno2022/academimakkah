"""Rejeu sur le serveur des changements de postes refusés (simulation, puis enregistrement réel)."""

from decimal import Decimal
from io import StringIO
from uuid import uuid4

from django.core.management import call_command
from django.test import TestCase

from eleves.models import Classe, Ecole, Eleve
from paiements.models import ModePaiement, Paiement, TypePaiement

from .models import SyncChange, SyncDevice


def _ref(uuid):
    return {'sync_uuid': str(uuid), 'pk': 99999}


class RejeuServeurTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.a = Ecole.objects.create(nom='École A', adresse='Conakry', telephone='+224620001001', directeur='D', etat='VALIDE')
        cls.b = Ecole.objects.create(nom='École B', adresse='Conakry', telephone='+224620001002', directeur='D', etat='VALIDE')
        cls.classe_a = Classe.objects.create(ecole=cls.a, nom='8A', niveau='COLLEGE_8', annee_scolaire='2026-2027')
        cls.classe_b = Classe.objects.create(ecole=cls.b, nom='8A', niveau='COLLEGE_8', annee_scolaire='2026-2027')
        cls.type = TypePaiement.objects.create(nom='Tranche 1')
        cls.mode = ModePaiement.objects.create(nom='Espèces')
        cls.eleve_a = Eleve.objects.create(matricule='CL8-001', prenom='Alpha', nom='Un', sexe='M', classe=cls.classe_a)
        Paiement.objects.create(
            eleve=cls.eleve_a, type_paiement=cls.type, mode_paiement=cls.mode, montant=Decimal('100000'),
            date_paiement='2026-10-01', annee_scolaire='2026-2027', numero_recu='REC20260001',
        )
        cls.poste = SyncDevice(ecole=cls.b, nom='Poste B')
        cls.poste.definir_token('jeton-b')
        cls.poste.save()

    def _refus(self, modele, uuid, payload, erreur, operation='CREATE'):
        return SyncChange.objects.create(
            ecole=self.b, device=self.poste, model_label=modele, operation=operation, object_uuid=uuid,
            payload={'sync_uuid': str(uuid), **payload}, statut=SyncChange.STATUT_FAILED, erreur=erreur,
        )

    def _preparer(self):
        """Ce que le serveur a refusé : un élève (matricule pris), puis son paiement, répétés."""
        self.eleve_uuid, self.paiement_uuid = uuid4(), uuid4()
        eleve = {'matricule': 'CL8-001', 'prenom': 'Beta', 'nom': 'Deux', 'sexe': 'F', 'classe': _ref(self.classe_b.sync_uuid)}
        paiement = {
            'eleve': _ref(self.eleve_uuid), 'type_paiement': _ref(self.type.sync_uuid), 'mode_paiement': _ref(self.mode.sync_uuid),
            'montant': '40000', 'date_paiement': '2026-10-05', 'annee_scolaire': '2026-2027',
            'numero_recu': 'REC20260001', 'statut': 'EN_ATTENTE',
        }
        self.refus('eleves.Eleve', self.eleve_uuid, eleve, "(1062, \"Duplicate entry 'CL8-001'\")")
        for _ in range(3):  # le poste a renvoyé le même paiement trois fois
            self.refus('paiements.Paiement', self.paiement_uuid, paiement, 'Relation introuvable pour eleve.', 'UPDATE')

    def refus(self, *args, **kwargs):
        return self._refus(*args, **kwargs)

    def _lancer(self, **options):
        sortie = StringIO()
        call_command('sync_rejouer_serveur', stdout=sortie, **options)
        return sortie.getvalue()

    def test_simulation_n_ecrit_rien(self):
        self._preparer()
        avant = list(SyncChange.objects.values_list('pk', 'statut', 'sequence'))
        sortie = self._lancer()
        self.assertIn('4 refus enregistrés, 2 fiche(s) concernée(s)', sortie)  # dernier changement de chaque fiche
        self.assertIn('2 à rejouer', sortie)
        self.assertIn('SIMULATION', sortie)
        self.assertIn('2 passeraient', sortie)
        self.assertFalse(Eleve.objects.filter(classe=self.classe_b).exists())
        self.assertEqual(Paiement.objects.count(), 1)
        self.assertEqual(avant, list(SyncChange.objects.values_list('pk', 'statut', 'sequence')))

    def test_rejeu_reel_renomme_enregistre_et_rediffuse(self):
        self._preparer()
        sortie = self._lancer(confirmer=True, purger_anciens=True)
        eleve = Eleve.objects.get(classe=self.classe_b)
        self.assertEqual(eleve.matricule, f'CL8-001-E{self.b.pk}')
        paiement = Paiement.objects.get(eleve=eleve)
        self.assertEqual(paiement.numero_recu, f'REC20260001-E{self.b.pk}')
        self.assertEqual(Paiement.objects.filter(numero_recu='REC20260001').count(), 1)   # l'école A est intacte
        appliques = SyncChange.objects.filter(device=self.poste, statut=SyncChange.STATUT_APPLIED)
        self.assertEqual(appliques.count(), 2)
        self.assertFalse(appliques.filter(sequence__isnull=True).exists())                  # rediffusés aux autres postes
        self.assertFalse(SyncChange.objects.filter(statut=SyncChange.STATUT_FAILED).exists())  # anciens refus purgés
        self.assertIn('rediffusée(s)', sortie)
        # Rejouer une seconde fois ne refait rien
        self.assertIn('0 à rejouer', self._lancer(confirmer=True))
        self.assertEqual(Eleve.objects.filter(classe=self.classe_b).count(), 1)

    def test_fiche_deja_remplacee_et_echec_restant(self):
        self._preparer()
        # Un changement plus récent de l'élève a déjà été appliqué : on ne rejoue pas l'ancien
        SyncChange.objects.create(
            ecole=self.b, device=self.poste, model_label='eleves.Eleve', operation='UPDATE', object_uuid=self.eleve_uuid,
            payload={}, statut=SyncChange.STATUT_APPLIED,
        )
        # Une fiche dont le parent n'existe nulle part reste en échec, avec sa raison
        orpheline = uuid4()
        self._refus('paiements.Paiement', orpheline, {
            'eleve': _ref(uuid4()), 'type_paiement': _ref(self.type.sync_uuid), 'mode_paiement': _ref(self.mode.sync_uuid),
            'montant': '1000', 'date_paiement': '2026-10-05', 'annee_scolaire': '2026-2027', 'numero_recu': 'REC-ORPH',
        }, 'Relation introuvable pour eleve.', 'UPDATE')
        sortie = self._lancer(confirmer=True)
        self.assertIn('1 déjà remplacée(s)', sortie)
        self.assertIn('Relation introuvable pour eleve', sortie)
        self.assertFalse(Paiement.objects.filter(numero_recu__startswith='REC-ORPH').exists())
        self.assertEqual(SyncChange.objects.get(object_uuid=orpheline).statut, SyncChange.STATUT_FAILED)
