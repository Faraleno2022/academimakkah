"""Liaison des classes de poste jamais reçues par le serveur, puis rejeu des élèves bloqués."""

from io import StringIO
from uuid import uuid4

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from eleves.models import Classe, Ecole, Eleve

from .models import SyncAlias, SyncChange, SyncDevice


class LierClassesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.ecole = Ecole.objects.create(nom='École B', adresse='Conakry', telephone='+224620001101', directeur='D', etat='VALIDE')
        cls.autre = Ecole.objects.create(nom='École C', adresse='Conakry', telephone='+224620001102', directeur='D', etat='VALIDE')
        cls.tps = Classe.objects.create(ecole=cls.ecole, nom='Toute petite section', niveau='TOUTE_PETITE_SECTION',
                                        annee_scolaire='2026-2027', code_matricule='TPS')
        cls.pn1a = Classe.objects.create(ecole=cls.ecole, nom='1ère A', niveau='PRIMAIRE_1', annee_scolaire='2026-2027', code_matricule='PN1')
        cls.pn1b = Classe.objects.create(ecole=cls.ecole, nom='1ère B', niveau='PRIMAIRE_1', annee_scolaire='2026-2027', code_matricule='PN1')
        cls.ailleurs = Classe.objects.create(ecole=cls.autre, nom='TPS autre école', niveau='TOUTE_PETITE_SECTION',
                                             annee_scolaire='2026-2027', code_matricule='TPS')
        cls.poste = SyncDevice(ecole=cls.ecole, nom='Poste B')
        cls.poste.definir_token('jeton')
        cls.poste.save()

    def _refus_eleve(self, matricule, prenom, classe_uuid):
        uuid = uuid4()
        SyncChange.objects.create(
            ecole=self.ecole, device=self.poste, model_label='eleves.Eleve', operation='CREATE', object_uuid=uuid,
            payload={'sync_uuid': str(uuid), 'matricule': matricule, 'prenom': prenom, 'nom': 'Test', 'sexe': 'F',
                     'classe': {'sync_uuid': str(classe_uuid), 'pk': 9}},
            statut=SyncChange.STATUT_FAILED, erreur='Relation introuvable pour classe.')
        return uuid

    def _lancer(self, commande='sync_lier_classes', **options):
        sortie = StringIO()
        call_command(commande, stdout=sortie, **options)
        return sortie.getvalue()

    def test_liaison_automatique_par_prefixe_puis_rejeu(self):
        classe_tps = uuid4()
        eleve_uuid = self._refus_eleve('TPS-001', 'Fatoumata', classe_tps)

        sortie = self._lancer()                                         # simulation
        self.assertIn('TOUTE PETITE SECTION', sortie)   # les noms sont mis en majuscules
        self.assertNotIn('TPS AUTRE ÉCOLE', sortie)                              # jamais la classe d'une autre école
        self.assertIn('SIMULATION', sortie)
        self.assertFalse(SyncAlias.objects.exists())

        self._lancer(confirmer=True)
        alias = SyncAlias.objects.get()
        self.assertEqual((alias.model_label, str(alias.ancien_uuid), alias.object_pk), ('eleves.Classe', str(classe_tps), self.tps.pk))

        # Le rejeu serveur enregistre maintenant l'élève, dans la classe en ligne
        self._lancer('sync_rejouer_serveur', confirmer=True)
        eleve = Eleve.objects.get(sync_uuid=eleve_uuid)
        self.assertEqual(eleve.classe, self.tps)
        self.assertEqual(eleve.matricule, 'TPS-001')
        self.assertIn('rien à relier', self._lancer())

    def test_plusieurs_classes_ou_aucune_jamais_liees_toutes_seules(self):
        self._refus_eleve('PN1-004', 'Marie', uuid4())                 # deux classes PN1 : ambigu
        self._refus_eleve('XYZ-001', 'Inconnue', uuid4())              # aucune classe XYZ
        sortie = self._lancer(confirmer=True)
        self.assertIn('plusieurs classes possibles', sortie)
        self.assertIn('aucune classe en ligne ne correspond', sortie)
        self.assertIn('1ÈRE A', sortie)
        self.assertIn('1ÈRE B', sortie)
        self.assertFalse(SyncAlias.objects.exists())

    def test_liaison_manuelle_et_garde_fous(self):
        classe_inconnue = uuid4()
        self._refus_eleve('PN1-004', 'Marie', classe_inconnue)
        with self.assertRaises(CommandError):                           # classe d'une autre école
            self._lancer(uuid=str(classe_inconnue), classe=self.ailleurs.pk, confirmer=True)
        with self.assertRaises(CommandError):                           # uuid qu'aucun refus ne désigne
            self._lancer(uuid=str(uuid4()), classe=self.pn1a.pk, confirmer=True)
        with self.assertRaises(CommandError):                           # --uuid sans --classe
            self._lancer(uuid=str(classe_inconnue))
        self.assertIn('SIMULATION', self._lancer(uuid=str(classe_inconnue), classe=self.pn1b.pk))
        self.assertFalse(SyncAlias.objects.exists())
        self._lancer(uuid=str(classe_inconnue), classe=self.pn1b.pk, confirmer=True)
        self.assertEqual(SyncAlias.objects.get().object_pk, self.pn1b.pk)
