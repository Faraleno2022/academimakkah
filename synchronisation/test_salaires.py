"""Module salaires : une paie saisie sur une machine doit arriver à l'identique
sur les autres, par le journal comme par l'instantané d'un poste neuf.

Le banc crée une paie par le code réel, relit ce qui serait transmis (passage
par JSON compris), efface les lignes de paie comme sur une machine qui ne les a
pas encore reçues, puis applique les changements et compare.
"""
import inspect
import json
from datetime import date, time, timedelta
from decimal import Decimal
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.db import models, transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from eleves.models import Classe, Ecole
from paiements.models import ModePaiement
from paiements.tests.support import MIDDLEWARE_SANS_LICENCE
from salaires.forms import EtatSalaireAjustementForm
from salaires.models import (
    AffectationClasse, AvanceSalaire, DetailHeuresClasse, Enseignant, EtatSalaire,
    PeriodeSalaire, PresenceEnseignant, TypeEnseignant,
)
from salaires.services import (
    actualiser_details_heures, calculer_etat_salaire, lignes_cours_administrateur,
)

from .client import reprogrammer_refus_temporaires
from .context import mute_sync
from .engine import apply_sync_change, get_model, serialize_instance, snapshot_page_for_ecole
from .models import SyncChange, SyncDevice
from .registry import SYNC_MODEL_LABELS

MODELES_PAIE = (DetailHeuresClasse, AvanceSalaire, PresenceEnseignant, EtatSalaire,
                PeriodeSalaire, AffectationClasse, Enseignant)
# Horodatages locaux : chaque machine a les siens.
CHAMPS_LOCAUX = {'date_creation', 'date_modification', 'date_calcul'}


def empreinte(instance):
    """Contenu comparable d'une ligne, sans ce qui dépend de la machine."""
    contenu = {}
    for nom, valeur in serialize_instance(instance).items():
        if nom in CHAMPS_LOCAUX:
            continue
        # Une relation se compare par son sync_uuid : son pk change d'une machine à l'autre.
        relation = isinstance(valeur, dict) and 'sync_uuid' in valeur
        contenu[nom] = valeur['sync_uuid'] if relation else valeur
    return contenu


def paie_complete():
    return {
        f'{model.__name__}:{instance.sync_uuid}': empreinte(instance)
        for model in MODELES_PAIE for instance in model.objects.all()
    }


@override_settings(MIDDLEWARE=MIDDLEWARE_SANS_LICENCE)
class BancSynchroPaie(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser(
            username='synchro-paie', email='synchro@example.com', password='x',
        )
        self.ecole = Ecole.objects.create(
            nom='Ecole synchro', adresse='Conakry', telephone='+224610000123',
            directeur='Direction', etat='VALIDE',
        )
        self.college = Classe.objects.create(
            ecole=self.ecole, nom='7e A', niveau='COLLEGE_7', annee_scolaire='2025-2026',
        )
        self.lycee = Classe.objects.create(
            ecole=self.ecole, nom='Terminale', niveau='TERMINALE', annee_scolaire='2025-2026',
        )
        self.primaire = Classe.objects.create(
            ecole=self.ecole, nom='CE1', niveau='PRIMAIRE_3', annee_scolaire='2025-2026',
        )
        self.mode = ModePaiement.objects.create(nom='Espèces synchro')
        self.poste = SyncDevice(ecole=self.ecole, nom='Poste paie')
        self.poste.definir_token('jeton-paie')
        self.poste.save()

    def creer_paie(self):
        """Une paie réaliste : les trois profils de personnel, pointage, avance, validation."""
        self.periode = PeriodeSalaire.objects.create(
            mois=7, annee=2026, ecole=self.ecole, nombre_semaines=Decimal('4'), cree_par=self.user,
        )
        self.secondaire = Enseignant.objects.create(
            nom='Camara', prenoms='Sekou', ecole=self.ecole, type_enseignant=TypeEnseignant.SECONDAIRE,
            taux_horaire=Decimal('10000'), date_embauche=date(2025, 1, 1), cree_par=self.user,
        )
        self.primaire_ens = Enseignant.objects.create(
            nom='Bah', prenoms='Aissatou', ecole=self.ecole, type_enseignant=TypeEnseignant.PRIMAIRE,
            salaire_fixe=Decimal('1000000'), date_embauche=date(2025, 1, 1), cree_par=self.user,
        )
        self.admin = Enseignant.objects.create(
            nom='Diallo', prenoms='Mamadou', ecole=self.ecole, type_enseignant=TypeEnseignant.ADMINISTRATEUR,
            fonction='Censeur', salaire_fixe=Decimal('2000000'), date_embauche=date(2025, 1, 1),
            cree_par=self.user,
        )
        AffectationClasse.objects.create(
            enseignant=self.secondaire, classe=self.college, heures_par_semaine=Decimal('6'),
            matiere='Maths', date_debut=date(2025, 9, 1),
        )
        AffectationClasse.objects.create(
            enseignant=self.primaire_ens, classe=self.primaire, date_debut=date(2025, 9, 1),
        )
        self.aff_admin = AffectationClasse.objects.create(
            enseignant=self.admin, classe=self.lycee, heures_par_semaine=Decimal('3'),
            taux_horaire=Decimal('15000'), matiere='PC', date_debut=date(2025, 9, 1),
        )
        PresenceEnseignant.objects.create(
            enseignant=self.secondaire, date=date(2026, 7, 6), statut='PRESENT',
            heure_arrivee=time(8, 0), heure_depart=time(12, 30), pointe_par=self.user,
        )
        PresenceEnseignant.objects.create(
            enseignant=self.primaire_ens, date=date(2026, 7, 6), statut='ABSENT', pointe_par=self.user,
        )
        self.etats = {}
        for enseignant in (self.secondaire, self.primaire_ens, self.admin):
            self.etats[enseignant.pk] = calculer_etat_salaire(enseignant, self.periode, self.user)[0]
        AvanceSalaire.objects.create(
            enseignant=self.primaire_ens, periode=self.periode, montant=Decimal('100000'),
            mode_paiement=self.mode, cree_par=self.user,
        )
        # Validation puis paiement, comme à l'écran.
        etat = EtatSalaire.objects.get(enseignant=self.primaire_ens, periode=self.periode)
        etat.valide = True
        etat.date_validation = timezone.now()
        etat.valide_par = self.user
        etat.save()
        etat.paye = True
        etat.date_paiement = timezone.now()
        etat.save()

    def effacer_paie(self):
        with mute_sync():
            for model in MODELES_PAIE:
                model.objects.all().delete()

    def journal(self):
        """Les changements de paie tels qu'ils voyagent (JSON compris)."""
        return [
            {'model': c.model_label, 'uuid': c.object_uuid, 'operation': c.operation,
             'payload': json.loads(json.dumps(c.payload))}
            for c in SyncChange.objects.filter(model_label__startswith='salaires.').order_by('pk')
        ]

    def instantane(self):
        lignes, curseur = [], None
        while True:
            page, curseur, _ = snapshot_page_for_ecole(self.ecole, cursor=curseur)
            lignes.extend(page)
            if not curseur:
                break
        return [
            {'model': l['model_label'], 'uuid': l['object_uuid'], 'operation': l['operation'],
             'payload': json.loads(json.dumps(l['payload']))}
            for l in lignes if l['model_label'].startswith('salaires.')
        ]

    def appliquer(self, lignes, device=None):
        """Applique les changements un par un ; retourne les refus."""
        refus = []
        for ligne in lignes:
            try:
                with transaction.atomic():
                    change = SyncChange.objects.create(
                        ecole=self.ecole, device=device, model_label=ligne['model'],
                        object_uuid=ligne['uuid'], operation=ligne['operation'], payload=ligne['payload'],
                    )
                    apply_sync_change(change)
            except Exception as exc:  # noqa: BLE001 - le banc recense ce que ferait la vue.
                refus.append(f"{ligne['operation']} {ligne['model']} : {exc}")
        return refus


class PaieDiffuseeTests(BancSynchroPaie):
    maxDiff = None

    def verifier(self, lignes, device):
        attendu = paie_complete()
        self.effacer_paie()
        refus = self.appliquer(lignes, device=device)
        self.assertEqual(refus, [])
        self.assertEqual(paie_complete(), attendu)

    def test_journal_poste_vers_serveur(self):
        self.creer_paie()
        self.verifier(self.journal(), self.poste)

    def test_journal_serveur_vers_poste(self):
        self.creer_paie()
        self.verifier(self.journal(), None)

    def test_instantane_poste_neuf(self):
        self.creer_paie()
        self.verifier(self.instantane(), None)

    def test_instantane_periode_cloturee(self):
        self.creer_paie()
        self.periode.cloturee = True
        self.periode.date_cloture = timezone.now()
        self.periode.save()
        self.verifier(self.instantane(), None)


class ConflitsEntrePostesTests(BancSynchroPaie):
    """Deux machines hors ligne saisissent la même paie sous deux sync_uuid."""

    def test_meme_mois_saisi_sur_deux_postes(self):
        # Le personnel est commun ; la paie du mois a été saisie des deux côtés.
        # Le second poste arrive après : le serveur a déjà sa propre paie.
        self.creer_paie()
        second_poste = [l for l in self.journal() if l['model'] != 'salaires.AvanceSalaire']
        SyncChange.objects.all().delete()
        with mute_sync():
            for model in (PeriodeSalaire, EtatSalaire, PresenceEnseignant, DetailHeuresClasse, AffectationClasse):
                for ligne in model.objects.all():
                    model.objects.filter(pk=ligne.pk).update(sync_uuid=uuid4())
        details = DetailHeuresClasse.objects.count()
        self.assertEqual(self.appliquer(second_poste, device=self.poste), [])
        self.assertEqual(PeriodeSalaire.objects.count(), 1)
        self.assertEqual(EtatSalaire.objects.count(), 3)
        self.assertEqual(PresenceEnseignant.objects.count(), 2)
        self.assertEqual(AffectationClasse.objects.count(), 3)
        self.assertEqual(DetailHeuresClasse.objects.count(), details)

    def test_enfant_reference_un_parent_fusionne(self):
        periode = PeriodeSalaire.objects.create(mois=3, annee=2026, ecole=self.ecole, cree_par=self.user)
        enseignant = Enseignant.objects.create(
            nom='Sow', prenoms='Fatou', ecole=self.ecole, type_enseignant=TypeEnseignant.PRIMAIRE,
            salaire_fixe=Decimal('900000'), date_embauche=date(2025, 1, 1), cree_par=self.user,
        )
        ancien_uuid = periode.sync_uuid
        doublon = uuid4()
        payload = {**serialize_instance(periode), 'sync_uuid': str(doublon)}
        self.assertEqual(self.appliquer([{
            'model': 'salaires.PeriodeSalaire', 'uuid': str(doublon), 'operation': 'CREATE', 'payload': json.loads(json.dumps(payload)),
        }], device=self.poste), [])
        self.assertEqual(PeriodeSalaire.objects.count(), 1)
        # Un état déjà parti d'un autre poste désigne encore l'ancien sync_uuid.
        ligne_etat = {
            'enseignant': {'model': 'salaires.Enseignant', 'sync_uuid': str(enseignant.sync_uuid)},
            'periode': {'model': 'salaires.PeriodeSalaire', 'sync_uuid': str(ancien_uuid)},
            'salaire_base': '900000.00', 'salaire_net': '900000.00',
        }
        etat_uuid = uuid4()
        self.assertEqual(self.appliquer([{
            'model': 'salaires.EtatSalaire', 'uuid': str(etat_uuid), 'operation': 'CREATE', 'payload': ligne_etat,
        }], device=self.poste), [])
        self.assertEqual(EtatSalaire.objects.get(sync_uuid=etat_uuid).periode_id, periode.pk)

    def test_changement_sous_ancien_identifiant_reste_diffuse(self):
        # Le pull ne relit que l'identifiant courant : sans réécriture, cette
        # modification reçue sous l'ancien sync_uuid n'irait jamais aux autres postes.
        periode = PeriodeSalaire.objects.create(mois=4, annee=2026, ecole=self.ecole, cree_par=self.user)
        ancien_uuid = periode.sync_uuid
        doublon = uuid4()
        contenu = json.loads(json.dumps(serialize_instance(periode)))
        self.assertEqual(self.appliquer([{
            'model': 'salaires.PeriodeSalaire', 'uuid': str(doublon), 'operation': 'CREATE',
            'payload': {**contenu, 'sync_uuid': str(doublon)},
        }], device=self.poste), [])
        self.assertEqual(self.appliquer([{
            'model': 'salaires.PeriodeSalaire', 'uuid': str(ancien_uuid), 'operation': 'UPDATE',
            'payload': {**contenu, 'lieu_edition': 'Kindia'},
        }], device=self.poste), [])
        periode.refresh_from_db()
        self.assertEqual((periode.sync_uuid, periode.lieu_edition), (doublon, 'Kindia'))
        dernier = SyncChange.objects.filter(model_label='salaires.PeriodeSalaire').latest('pk')
        self.assertEqual(dernier.object_uuid, doublon)

    def test_paie_payee_pas_ecrasee_par_un_etat_hors_ligne(self):
        self.creer_paie()
        etat = EtatSalaire.objects.get(enseignant=self.primaire_ens, periode=self.periode)
        self.assertTrue(etat.paye)
        doublon = uuid4()
        payload = {**serialize_instance(etat), 'sync_uuid': str(doublon), 'paye': False, 'valide': False}
        self.assertEqual(self.appliquer([{
            'model': 'salaires.EtatSalaire', 'uuid': str(doublon), 'operation': 'CREATE', 'payload': json.loads(json.dumps(payload)),
        }], device=self.poste), [])
        etat.refresh_from_db()
        self.assertTrue(etat.paye)
        self.assertTrue(etat.valide)
        self.assertNotEqual(etat.sync_uuid, doublon)
        # Le doublon désigne bien la ligne payée pour la suite de ses changements.
        self.assertEqual(self.appliquer([{
            'model': 'salaires.EtatSalaire', 'uuid': str(doublon), 'operation': 'UPDATE',
            'payload': {'observations': 'Revu'},
        }], device=None), [])
        etat.refresh_from_db()
        self.assertEqual(etat.observations, 'Revu')
        self.assertEqual(EtatSalaire.objects.filter(enseignant=self.primaire_ens).count(), 1)

    def test_serveur_arbitre_une_avance_sur_paie_deja_payee(self):
        # Le serveur reste juge de ce qu'un poste lui envoie : le refus est visible.
        self.creer_paie()
        avance = AvanceSalaire.objects.get()
        doublon = uuid4()
        ligne = {
            'model': 'salaires.AvanceSalaire', 'uuid': str(doublon), 'operation': 'CREATE',
            'payload': json.loads(json.dumps({**serialize_instance(avance), 'sync_uuid': str(doublon)})),
        }
        refus = self.appliquer([ligne], device=self.poste)
        self.assertEqual(len(refus), 1)
        self.assertIn('validé ou payé', refus[0])
        # Ce que le serveur a accepté s'applique sur le poste, quel que soit l'ordre.
        net = EtatSalaire.objects.get(enseignant=self.primaire_ens).salaire_net
        self.assertEqual(self.appliquer([ligne], device=None), [])
        self.assertEqual(AvanceSalaire.objects.count(), 2)
        self.assertEqual(EtatSalaire.objects.get(enseignant=self.primaire_ens).salaire_net, net)


class RattrapageDesRefusTests(BancSynchroPaie):
    def refus(self, label='salaires.Enseignant', erreur="{'cree_par': ['Ce champ ne peut pas être vide.']}"):
        return SyncChange.objects.create(
            ecole=self.ecole, model_label=label, object_uuid=uuid4(), operation='CREATE', payload={},
            statut=SyncChange.STATUT_FAILED, erreur=erreur,
            date_application=timezone.now() - timedelta(minutes=5),
        )

    def test_paie_refusee_a_tort_est_renvoyee(self):
        enseignant = self.refus()
        periode = self.refus('salaires.PeriodeSalaire', "{'__all__': ['Un objet existe déjà.']}")
        self.assertEqual(reprogrammer_refus_temporaires(self.ecole), 2)
        enseignant.refresh_from_db()
        periode.refresh_from_db()
        self.assertEqual(enseignant.statut, SyncChange.STATUT_PENDING)
        self.assertEqual(periode.statut, SyncChange.STATUT_PENDING)

    def test_refus_metier_reste_visible(self):
        self.refus('salaires.AvanceSalaire', "{'periode': ['Le salaire de cette période est déjà validé ou payé.']}")
        self.refus('notes.NoteEleve', "{'__all__': ['Un objet existe déjà.']}")
        self.assertEqual(reprogrammer_refus_temporaires(self.ecole), 0)


class AuteursNonEchangesTests(TestCase):
    def test_aucun_modele_synchronise_n_exige_son_auteur(self):
        """L'auteur ne voyage pas : l'exiger à l'enregistrement refuse toute ligne reçue."""
        bloquants = []
        for label in SYNC_MODEL_LABELS:
            model = get_model(label)
            if model is None or 'full_clean' not in inspect.getsource(model.save):
                continue
            for champ in model._meta.concrete_fields:
                if (isinstance(champ, models.ForeignKey) and champ.remote_field.model == get_user_model()
                        and not champ.blank):
                    bloquants.append(f'{label}.{champ.name}')
        self.assertEqual(bloquants, [])


class HeuresCorrigeesTests(BancSynchroPaie):
    def test_correction_conservee_sur_une_autre_machine(self):
        self.creer_paie()
        etat = EtatSalaire.objects.get(enseignant=self.admin, periode=self.periode)
        form = EtatSalaireAjustementForm(
            data={
                'salaire_base': '2000000', 'deductions': '0',
                f'cours_heures_{self.aff_admin.pk}': '5',
                **{champ: '0' for champ in EtatSalaire.CHAMPS_PRIMES},
                'jours_chomes': '0', 'heures_revision': '0',
            },
            instance=etat,
        )
        self.assertTrue(form.is_valid(), form.errors)
        etat = form.save(commit=False)
        etat.heures_cours_corrigees = form.corrections_cours
        etat.save()
        actualiser_details_heures(etat)
        lignes = self.journal()
        self.assertEqual(etat.details_heures.get().heures_realisees, Decimal('5.00'))

        # Sur l'autre machine, les clés primaires ne sont pas les mêmes.
        self.effacer_paie()
        self.assertEqual(self.appliquer(lignes, device=None), [])
        recu = EtatSalaire.objects.get(enseignant__fonction='Censeur')
        affectation = AffectationClasse.objects.get(enseignant=recu.enseignant)
        self.assertNotEqual(affectation.pk, self.aff_admin.pk)
        heures = lignes_cours_administrateur(recu.enseignant, recu.periode, recu)[0][1]
        self.assertEqual(heures, Decimal('5.00'))

    def test_ancienne_correction_par_pk_reprise_du_detail(self):
        # Ce pk désigne une tout autre affectation sur cette machine.
        self.creer_paie()
        etat = EtatSalaire.objects.get(enseignant=self.admin, periode=self.periode)
        detail = etat.details_heures.get()
        detail.heures_realisees = Decimal('7')
        detail.save()
        autre_affectation = AffectationClasse.objects.exclude(pk=self.aff_admin.pk).first()
        etat.heures_cours_corrigees = {str(autre_affectation.pk): '99'}
        etat.save()
        self.assertEqual(lignes_cours_administrateur(self.admin, self.periode, etat)[0][1], Decimal('7.00'))
        # L'actualisation des détails ne perd pas la correction en les recréant.
        actualiser_details_heures(etat)
        self.assertEqual(etat.details_heures.get().heures_realisees, Decimal('7.00'))


class SuppressionsPaieTests(BancSynchroPaie):
    def test_suppressions_diffusees(self):
        self.creer_paie()
        SyncChange.objects.all().delete()
        avance_uuid = AvanceSalaire.objects.get().sync_uuid
        presence = PresenceEnseignant.objects.get(enseignant=self.secondaire)
        presence_uuid = presence.sync_uuid
        presence.delete()
        etat = EtatSalaire.objects.get(enseignant=self.primaire_ens, periode=self.periode)
        lignes = self.journal()
        self.assertEqual([(l['model'], l['operation']) for l in lignes],
                         [('salaires.PresenceEnseignant', 'DELETE')])
        self.assertEqual(self.appliquer(lignes, device=None), [])
        self.assertFalse(PresenceEnseignant.objects.filter(sync_uuid=presence_uuid).exists())

        # Reçue alors que la paie est payée ici : la machine d'origine a décidé.
        self.assertTrue(etat.paye)
        self.assertEqual(self.appliquer([{
            'model': 'salaires.AvanceSalaire', 'uuid': str(avance_uuid), 'operation': 'DELETE',
            'payload': {'sync_uuid': str(avance_uuid)},
        }], device=None), [])
        self.assertFalse(AvanceSalaire.objects.filter(sync_uuid=avance_uuid).exists())

    def test_suppression_enseignant_en_cascade(self):
        self.creer_paie()
        SyncChange.objects.all().delete()
        with mute_sync():
            AvanceSalaire.objects.all().delete()
        uuid_enseignant = self.primaire_ens.sync_uuid
        self.primaire_ens.delete()
        lignes = self.journal()
        modeles = {l['model'] for l in lignes}
        self.assertIn('salaires.Enseignant', modeles)
        self.assertIn('salaires.EtatSalaire', modeles)
        self.assertTrue(all(l['operation'] == 'DELETE' for l in lignes))
        self.assertFalse(Enseignant.objects.filter(sync_uuid=uuid_enseignant).exists())
