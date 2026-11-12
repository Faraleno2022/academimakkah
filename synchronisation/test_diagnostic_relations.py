"""Commande de diagnostic des refus de synchronisation : lecture seule, fiches manquantes et causes racines."""

from datetime import timedelta
from decimal import Decimal
from io import StringIO
from uuid import uuid4

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from eleves.models import Classe, Ecole, Eleve
from paiements.models import ModePaiement, Paiement, TypePaiement

from .models import SyncChange


class DiagnosticRelationsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.ecole = Ecole.objects.create(nom='École diag', adresse='Conakry', telephone='+224620000801', directeur='D')
        cls.autre = Ecole.objects.create(nom='Autre école diag', adresse='Conakry', telephone='+224620000802', directeur='D')
        cls.classe = Classe.objects.create(ecole=cls.ecole, nom='7A', niveau='COLLEGE_7', annee_scolaire='2026-2027')
        classe_autre = Classe.objects.create(ecole=cls.autre, nom='7A', niveau='COLLEGE_7', annee_scolaire='2026-2027')
        cls.present = Eleve.objects.create(matricule='D-1', prenom='Présent', nom='Là', sexe='M', classe=cls.classe)
        cls.eleve_autre = Eleve.objects.create(matricule='X-1', prenom='Autre', nom='École', sexe='F', classe=classe_autre)
        # REC20260001 est déjà pris EN LIGNE par l'autre école : le numéro est unique sur tout le serveur
        Paiement.objects.create(
            eleve=cls.eleve_autre, type_paiement=TypePaiement.objects.create(nom='T1'),
            mode_paiement=ModePaiement.objects.create(nom='Espèces'), montant=Decimal('100000'),
            date_paiement='2026-10-01', annee_scolaire='2026-2027', numero_recu='REC20260001',
        )

    def _refus(self, uuid_eleve, modele='paiements.Paiement', ecole=None, **payload):
        return SyncChange.objects.create(
            ecole=ecole or self.ecole, model_label=modele, operation='UPDATE', object_uuid=uuid4(),
            payload={'eleve': {'sync_uuid': str(uuid_eleve), 'pk': 99}, **payload},
            statut=SyncChange.STATUT_FAILED, erreur='Relation introuvable pour eleve.',
        )

    def _lancer(self, **options):
        sortie = StringIO()
        call_command('diagnostic_relations_sync', stdout=sortie, **options)
        return sortie.getvalue()

    def test_aucun_refus(self):
        sortie = self._lancer()
        self.assertIn('0 changement(s) refusé(s)', sortie)
        self.assertIn('Aucun autre refus', sortie)

    def test_fiches_manquantes_par_ecole_et_recus(self):
        absent = uuid4()
        self._refus(absent, modele='eleves.HistoriqueEleve', description='Création du profil de AMINATA TEST')
        self._refus(absent, numero_recu='REC20260001', montant='348000')       # numéro pris par l'autre école
        self._refus(absent, numero_recu='REC20260077', montant='40000')        # absent partout
        self._refus(self.present.sync_uuid, numero_recu='REC-OK')              # la fiche existe : refus résolu
        autres_refus = SyncChange.objects.create(
            ecole=self.ecole, model_label='eleves.Eleve', operation='CREATE', object_uuid=uuid4(), payload={},
            statut=SyncChange.STATUT_FAILED, erreur='UNIQUE constraint failed: eleves_eleve.matricule',
        )
        vieux = self._refus(uuid4(), modele='eleves.HistoriqueEleve', ecole=self.autre)
        SyncChange.objects.filter(pk=vieux.pk).update(date_creation=timezone.now() - timedelta(days=30))

        sortie = self._lancer()
        self.assertIn('3 fiche(s) manquante(s)', sortie)
        self.assertIn('ÉCOLE DIAG', sortie)                                     # noms d'école mis en majuscules
        self.assertIn('encore en boucle', sortie)                               # école diag : refus récents
        self.assertIn('plus aucun refus récent', sortie)                        # autre école : refus d'il y a 30 jours
        self.assertIn('REC20260077 | 40 000 GNF | AMINATA TEST | ABSENT', sortie)
        self.assertIn("est pris en ligne par l'école « AUTRE ÉCOLE DIAG »", sortie)
        self.assertIn('1 existent maintenant en ligne', sortie)
        self.assertIn('UNIQUE constraint failed: eleves_eleve.matricule', sortie)  # cause racine
        self.assertEqual(autres_refus.erreur, SyncChange.objects.get(pk=autres_refus.pk).erreur)

    def test_detail_lecture_seule_et_filtre_par_ecole(self):
        self._refus(uuid4(), numero_recu='REC-1', montant='1000')
        avant = list(SyncChange.objects.values_list('pk', 'statut', 'erreur'))
        self.assertIn('Détail par fiche manquante', self._lancer(detail=True))
        self.assertEqual(avant, list(SyncChange.objects.values_list('pk', 'statut', 'erreur')))  # rien modifié
        self.assertIn('0 changement(s) refusé(s)', self._lancer(ecole=self.autre.pk))
