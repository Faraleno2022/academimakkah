import uuid
from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from bus.models import AbonnementBus, GrilleTarifaireBus
from eleves.models import Classe, Ecole, Eleve
from paiements.tests.support import MIDDLEWARE_SANS_LICENCE
from synchronisation.engine import build_change_instance
from synchronisation.models import SyncChange
from utilisateurs.models import Profil

from .models import ModePaiement, TypePaiement
from .referentiels import (
    MODES_PAIEMENT_DEFAUT,
    TYPES_PAIEMENT_DEFAUT,
    assurer_referentiels_paiement,
    retirer_referentiels_inutilises,
    uuid_defaut,
)


class ReferentielsPaiementTests(TestCase):
    def test_installation_vierge_cree_modes_et_types(self):
        modes, types = assurer_referentiels_paiement()
        self.assertEqual(len(modes), len(MODES_PAIEMENT_DEFAUT))
        self.assertEqual(len(types), len(TYPES_PAIEMENT_DEFAUT))
        self.assertTrue(TypePaiement.objects.filter(nom='Tranche 2 + Tranche 3', actif=True).exists())
        especes = ModePaiement.objects.get(nom='Espèces')
        # Même identifiant sur le serveur et sur tous les postes.
        self.assertEqual(especes.sync_uuid, uuid_defaut(ModePaiement, 'Espèces'))
        self.assertFalse(SyncChange.objects.exists())
        self.assertEqual(assurer_referentiels_paiement(), ([], []))

    def test_ne_double_pas_une_valeur_deja_saisie_ou_desactivee(self):
        ModePaiement.objects.create(nom='Especes')
        TypePaiement.objects.create(nom='Inscription')
        TypePaiement.objects.create(nom='Scolarité')
        TypePaiement.objects.create(nom='1ère tranche', actif=False)
        modes, types = assurer_referentiels_paiement()
        self.assertNotIn('Espèces', modes)
        self.assertNotIn("Frais d'inscription", types)
        self.assertNotIn('Annuel (Tranche 1 + Tranche 2 + Tranche 3)', types)
        self.assertNotIn('Tranche 1', types)
        self.assertIn('Tranche 2', types)
        self.assertFalse(TypePaiement.objects.get(nom='1ère tranche').actif)

    def test_poste_neuf_retire_les_valeurs_jamais_utilisees(self):
        assurer_referentiels_paiement()
        ecole = Ecole.objects.create(nom='École Réf', adresse='Conakry', telephone='+224620000301', directeur='D')
        classe = Classe.objects.create(ecole=ecole, nom='CE1', niveau='PRIMAIRE_2', annee_scolaire='2026-2027')
        eleve = Eleve.objects.create(matricule='REF-1', prenom='Fanta', nom='Bah', sexe='F', classe=classe)
        grille = GrilleTarifaireBus.objects.create(
            ecole=ecole, zone='Kaloum', annee_scolaire='2026-2027', tranche_1=1, tranche_2=1, tranche_3=1,
        )
        AbonnementBus.objects.create(
            eleve=eleve, grille=grille, periodicite='T1', montant=1,
            mode_paiement=ModePaiement.objects.get(nom='Orange Money'),
            date_debut=date(2026, 10, 1), date_expiration=date(2027, 6, 30),
        )
        retirer_referentiels_inutilises()
        self.assertEqual(list(ModePaiement.objects.values_list('nom', flat=True)), ['Orange Money'])
        self.assertFalse(TypePaiement.objects.exists())

    def test_synchro_fusionne_un_mode_de_meme_nom(self):
        assurer_referentiels_paiement()
        ecole = Ecole.objects.create(nom='École Sync', adresse='Conakry', telephone='+224620000302', directeur='D')
        uuid_serveur = uuid.uuid4()
        build_change_instance(
            'paiements.ModePaiement', uuid_serveur,
            {'nom': 'Espèces', 'description': 'Serveur', 'actif': True},
            ecole=ecole, trusted=True,
        )
        especes = ModePaiement.objects.get(nom='Espèces')
        self.assertEqual(especes.sync_uuid, uuid_serveur)
        self.assertEqual(ModePaiement.objects.filter(nom='Espèces').count(), 1)


@override_settings(MIDDLEWARE=MIDDLEWARE_SANS_LICENCE)
class ModeParDefautFormulaireBusTests(TestCase):
    def test_especes_preselectionne(self):
        assurer_referentiels_paiement()
        ecole = Ecole.objects.create(nom='École Form', adresse='Conakry', telephone='+224620000303', directeur='D')
        user = get_user_model().objects.create_user(username='bus_ref', password='secret123')
        Profil.objects.update_or_create(user=user, defaults={
            'role': 'ADMIN', 'telephone': '+224620000304', 'ecole': ecole,
            'is_validated': True, 'allowed_menus': ['bus'],
        })
        self.client.force_login(user)
        response = self.client.get(reverse('bus:nouveau'))
        especes = ModePaiement.objects.get(nom='Espèces')
        self.assertEqual(response.context['form']['mode_paiement'].value(), especes.pk)
