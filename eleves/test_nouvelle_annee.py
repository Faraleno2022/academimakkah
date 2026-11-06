"""Création d'une nouvelle année scolaire (/eleves/nouvelle-annee/creer/)."""
from datetime import date, timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from eleves.models import Classe, Ecole, Eleve, GrilleTarifaire, Responsable
from notes.models import ClasseNote, Classement, MatiereNote, NoteMensuelle
from paiements.models import EcheancierPaiement

MIDDLEWARE_SANS_LICENCE = [m for m in settings.MIDDLEWARE if 'licence_middleware' not in m]


@override_settings(MIDDLEWARE=MIDDLEWARE_SANS_LICENCE)
class CreationNouvelleAnneeTest(TestCase):
    def setUp(self):
        self.ecole = Ecole.objects.create(
            nom='École Passage', adresse='Conakry', telephone='+224620555001', directeur='Direction',
        )
        self.admin = get_user_model().objects.create_superuser('admin-passage', 'p@p.gn', 'x')
        profil = self.admin.profil
        profil.ecole = self.ecole
        profil.save()

        self.classe_1 = Classe.objects.create(
            ecole=self.ecole, nom='1ÈRE ANNÉE A', niveau='PRIMAIRE_1', annee_scolaire='2025-2026',
        )
        Classe.objects.create(
            ecole=self.ecole, nom='2ÈME ANNÉE A', niveau='PRIMAIRE_2', annee_scolaire='2025-2026',
        )
        for annee in ('2025-2026', '2026-2027'):
            for niveau in ('PRIMAIRE_1', 'PRIMAIRE_2'):
                GrilleTarifaire.objects.create(
                    ecole=self.ecole, niveau=niveau, annee_scolaire=annee,
                    frais_inscription=200000, frais_reinscription=80000,
                    tranche_1=300000, tranche_2=250000, tranche_3=150000,
                )
        responsable = Responsable.objects.create(
            prenom='Parent', nom='Passage', relation='PERE', telephone='+224620555002', adresse='Conakry',
        )
        self.eleve = Eleve.objects.create(
            matricule='PAS-001', prenom='Aminata', nom='Camara', sexe='F',
            date_naissance=date(2018, 1, 1), classe=self.classe_1,
            date_inscription=date(2025, 9, 1), responsable_principal=responsable, statut='ACTIF',
        )

        self.classe_note = ClasseNote.objects.create(
            ecole=self.ecole, nom='1ÈRE ANNÉE A', niveau='PRIMAIRE_1',
            niveau_enseignement='PRIMAIRE', annee_scolaire='2025-2026',
        )
        self.matiere = MatiereNote.objects.create(
            classe=self.classe_note, nom='Mathématiques', code='MAT', coefficient=1,
        )
        # Classe supérieure configurée dans Notes : dupliquée vers la nouvelle année,
        # elle offre une matière « équivalente » vers laquelle l'ancien code déplaçait les notes.
        classe_note_2 = ClasseNote.objects.create(
            ecole=self.ecole, nom='2ÈME ANNÉE A', niveau='PRIMAIRE_2',
            niveau_enseignement='PRIMAIRE', annee_scolaire='2025-2026',
        )
        MatiereNote.objects.create(classe=classe_note_2, nom='Mathématiques', code='MAT', coefficient=1)
        self.note = NoteMensuelle.objects.create(
            eleve=self.eleve, matiere=self.matiere, mois='OCTOBRE',
            annee_scolaire='2025-2026', note=Decimal('8'),
        )
        Classement.objects.create(
            eleve=self.eleve, classe=self.classe_note, periode='ANNUEL_TRIM',
            annee_scolaire='2025-2026', moyenne_generale=Decimal('8'),
            total_points=Decimal('8'), total_coefficients=Decimal('1'),
            rang=1, rang_formate='1er/1', effectif=1,
        )
        self.ancien_echeancier = EcheancierPaiement.objects.create(
            eleve=self.eleve, annee_scolaire='2025-2026',
            frais_inscription_du=Decimal('0'), tranche_1_due=Decimal('100000'),
            tranche_2_due=Decimal('0'), tranche_3_due=Decimal('0'),
            date_echeance_inscription=date(2025, 9, 30), date_echeance_tranche_1=date(2026, 1, 10),
            date_echeance_tranche_2=date(2026, 3, 5), date_echeance_tranche_3=date(2026, 5, 5),
        )
        self.client.force_login(self.admin)

    def test_passage_conserve_les_notes_et_classements_de_l_annee_ecoulee(self):
        response = self.client.post(reverse('eleves:nouvelle_annee_creer'), {
            'annee_courante': '2025-2026', 'annee_nouvelle': '2026-2027',
            'dupliquer_notes_classes': '1', 'faire_passer_eleves': '1',
        })
        self.assertEqual(response.status_code, 302)

        self.eleve.refresh_from_db()
        self.assertEqual(self.eleve.classe.annee_scolaire, '2026-2027')
        self.assertEqual(self.eleve.classe.nom, '2ÈME ANNÉE A')

        # Les notes de l'année écoulée restent rattachées aux matières de cette année-là :
        # auparavant elles étaient déplacées vers la nouvelle année (bulletins vidés).
        self.note.refresh_from_db()
        self.assertEqual(self.note.matiere_id, self.matiere.pk)
        self.assertTrue(Classement.objects.filter(eleve=self.eleve, annee_scolaire='2025-2026').exists())

        self.assertTrue(EcheancierPaiement.objects.filter(pk=self.ancien_echeancier.pk).exists())
        self.assertTrue(EcheancierPaiement.objects.filter(eleve=self.eleve, annee_scolaire='2026-2027').exists())

    def test_grande_ecole_traitee_par_lots_enchaines(self):
        """Chaque requête traite un lot : aucune ne doit dépasser le délai du serveur."""
        from unittest.mock import patch

        deuxieme = Eleve.objects.create(
            matricule='PAS-002', prenom='Mamadou', nom='Diallo', sexe='M',
            date_naissance=date(2018, 2, 2), classe=self.classe_1,
            date_inscription=date(2025, 9, 1), responsable_principal=self.eleve.responsable_principal,
            statut='ACTIF',
        )
        Classement.objects.create(
            eleve=deuxieme, classe=self.classe_note, periode='ANNUEL_TRIM',
            annee_scolaire='2025-2026', moyenne_generale=Decimal('7'),
            total_points=Decimal('7'), total_coefficients=Decimal('1'),
            rang=2, rang_formate='2e/2', effectif=2,
        )
        url = reverse('eleves:nouvelle_annee_creer')
        with patch('eleves.views_nouvelle_annee.TAILLE_LOT_PASSAGE', 1):
            premiere = self.client.post(url, {
                'annee_courante': '2025-2026', 'annee_nouvelle': '2026-2027',
                'dupliquer_notes_classes': '1', 'faire_passer_eleves': '1',
            })
            self.assertEqual(premiere.status_code, 200)
            self.assertContains(premiere, '1 / 2')
            self.assertEqual(Eleve.objects.filter(classe__annee_scolaire='2026-2027').count(), 1)

            suite = self.client.post(url, {
                'annee_courante': '2025-2026', 'annee_nouvelle': '2026-2027', 'continuer': '1',
            })
        self.assertEqual(suite.status_code, 302)
        self.assertEqual(Eleve.objects.filter(classe__annee_scolaire='2026-2027').count(), 2)
        self.assertEqual(Classe.objects.filter(annee_scolaire='2026-2027').count(), 2)
        self.assertEqual(EcheancierPaiement.objects.filter(annee_scolaire='2026-2027').count(), 2)
        messages = [str(m) for m in suite.wsgi_request._messages]
        self.assertTrue(any('2 élève(s) passé(s)' in m for m in messages), messages)

    def test_reprise_sans_etat_ne_plante_pas(self):
        response = self.client.post(reverse('eleves:nouvelle_annee_creer'), {
            'annee_courante': '2025-2026', 'annee_nouvelle': '2026-2027', 'continuer': '1',
        })
        self.assertRedirects(response, reverse('eleves:nouvelle_annee_apercu'),
                             fetch_redirect_response=False)

    def test_creation_active_et_affiche_la_nouvelle_annee(self):
        from eleves.utils_annee import SESSION_ANNEE_ACTIVE
        session = self.client.session
        session[SESSION_ANNEE_ACTIVE] = '2025-2026'
        session.save()
        response = self.client.post(reverse('eleves:nouvelle_annee_creer'), {
            'annee_courante': '2025-2026', 'annee_nouvelle': '2026-2027',
        }, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.session[SESSION_ANNEE_ACTIVE], '2026-2027')
        self.assertEqual(response.context['annee_active'], '2026-2027')
        self.assertEqual({c.annee_scolaire for c in response.context['classes']}, {'2026-2027'})

    def test_annees_identiques_mal_formees_ou_non_consecutives_refusees(self):
        for source, target in [('2025-2026', '2025-2026'), ('2025-2026', '2027-2028'),
                               ('2025/2026', '2025/2026'), ('2025-2028', '2026-2029')]:
            response = self.client.post(reverse('eleves:nouvelle_annee_creer'), {
                'annee_courante': source, 'annee_nouvelle': target, 'faire_passer_eleves': '1',
            })
            self.assertRedirects(response, reverse('eleves:nouvelle_annee_apercu'), fetch_redirect_response=False)
        self.assertEqual(Classe.objects.count(), 2)
        self.eleve.refresh_from_db()
        self.assertEqual(self.eleve.classe_id, self.classe_1.pk)

    def test_annee_source_absente_de_l_ecole_ne_cree_pas_un_faux_succes(self):
        response = self.client.post(reverse('eleves:nouvelle_annee_creer'), {
            'annee_courante': '2023-2024', 'annee_nouvelle': '2024-2025',
        })
        self.assertRedirects(response, reverse('eleves:nouvelle_annee_apercu'), fetch_redirect_response=False)
        self.assertFalse(Classe.objects.filter(annee_scolaire='2024-2025').exists())

    def test_un_eleve_non_deplace_ne_boucle_pas_indefiniment(self):
        from unittest.mock import patch
        with patch('eleves.views_nouvelle_annee._passer_eleve'):
            response = self.client.post(reverse('eleves:nouvelle_annee_creer'), {
                'annee_courante': '2025-2026', 'annee_nouvelle': '2026-2027', 'faire_passer_eleves': '1',
            })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Classe.objects.filter(annee_scolaire='2026-2027').exists())
        messages = [str(m) for m in response.wsgi_request._messages]
        self.assertTrue(any("n'ont pas pu être passés" in message for message in messages))

    def test_echec_echeancier_annule_le_passage_et_ses_compteurs(self):
        from unittest.mock import patch
        with patch('eleves.views_nouvelle_annee._creer_echeancier', side_effect=RuntimeError('échéancier indisponible')):
            response = self.client.post(reverse('eleves:nouvelle_annee_creer'), {
                'annee_courante': '2025-2026', 'annee_nouvelle': '2026-2027', 'faire_passer_eleves': '1',
            })
        self.eleve.refresh_from_db()
        self.assertEqual(self.eleve.classe_id, self.classe_1.pk)
        messages = [str(m) for m in response.wsgi_request._messages]
        self.assertFalse(any('élève(s) passé(s)' in message for message in messages), messages)
        self.assertTrue(any("n'ont pas pu être passés" in message for message in messages))

    def test_formulaires_protegent_la_creation_contre_le_rechargement_de_sync(self):
        response = self.client.get(reverse('eleves:nouvelle_annee_apercu'))
        self.assertContains(response, 'onsubmit="return lancerAnnee(this)" data-no-fast-submit data-sync-preserve')
        self.assertNotContains(response, 'onclick="return lancerAnnee(this)"')

    def _creer_annee(self, **options):
        return self.client.post(reverse('eleves:nouvelle_annee_creer'), {
            'annee_courante': '2025-2026', 'annee_nouvelle': '2026-2027',
            'faire_passer_eleves': '1', **options,
        })

    def test_nouvel_echeancier_est_reinscription_au_tarif_du_nouveau_niveau(self):
        from paiements.services import _nature_frais
        self._creer_annee()
        nouveau = EcheancierPaiement.objects.get(eleve=self.eleve, annee_scolaire='2026-2027')
        self.assertTrue(nouveau.est_reinscription)
        self.assertEqual(nouveau.frais_inscription_du, 80000)
        self.assertEqual(nouveau.total_du, 780000)
        self.assertEqual(nouveau.total_paye, 0)
        self.assertEqual(_nature_frais(self.eleve, '2026-2027', nouvelle_annee=True,
                                      echeancier=nouveau), EcheancierPaiement.NATURE_REINSCRIPTION)

    def test_tarif_absent_bloque_l_eleve_et_permet_de_reprendre_apres_configuration(self):
        GrilleTarifaire.objects.filter(annee_scolaire='2026-2027').delete()
        response = self._creer_annee()
        self.eleve.refresh_from_db()
        self.assertEqual(self.eleve.classe_id, self.classe_1.pk)
        self.assertFalse(EcheancierPaiement.objects.filter(annee_scolaire='2026-2027').exists())
        self.assertTrue(any('Tarif manquant' in str(m) for m in response.wsgi_request._messages))
        self.assertIn('annee_courante=2025-2026', response.url)
        apercu = self.client.get(response.url)
        self.assertEqual(apercu.context['annee_courante'], '2025-2026')
        gestion = self.client.get(reverse('eleves:gestion_annees'))
        self.assertContains(gestion, 'Compléter le passage')
        self.assertEqual(self.client.session['annee_scolaire_active'], '2025-2026')
        self._creer_annee(dupliquer_grilles='1')
        self.eleve.refresh_from_db()
        self.assertEqual(self.eleve.classe.annee_scolaire, '2026-2027')
        self.assertEqual(EcheancierPaiement.objects.get(eleve=self.eleve,
            annee_scolaire='2026-2027').total_du, 780000)

    def test_copie_grilles_scolarite_et_bus_decale_les_echeances(self):
        from bus.models import GrilleTarifaireBus
        GrilleTarifaire.objects.filter(annee_scolaire='2026-2027').delete()
        source = GrilleTarifaire.objects.get(ecole=self.ecole, niveau='PRIMAIRE_2', annee_scolaire='2025-2026')
        source.date_echeance_inscription_defaut = date(2025, 9, 15)
        source.date_echeance_tranche_1_defaut = date(2025, 11, 30)
        source.date_echeance_tranche_2_defaut = date(2026, 2, 20)
        source.date_echeance_tranche_3_defaut = date(2026, 5, 10)
        source.save()
        GrilleTarifaireBus.objects.create(ecole=self.ecole, zone='Kipé', annee_scolaire='2025-2026',
            tranche_1=100000, tranche_2=90000, tranche_3=80000,
            date_echeance_tranche_1=date(2025, 12, 1), date_echeance_tranche_2=date(2026, 3, 1),
            date_echeance_tranche_3=date(2026, 6, 1))
        self._creer_annee(dupliquer_grilles='1')
        nouveau = EcheancierPaiement.objects.get(eleve=self.eleve, annee_scolaire='2026-2027')
        self.assertEqual(nouveau.date_echeance_inscription, date(2026, 9, 15))
        self.assertEqual(nouveau.date_echeance_tranche_1, date(2026, 11, 30))
        self.assertEqual(nouveau.date_echeance_tranche_2, date(2027, 2, 20))
        self.assertEqual(nouveau.date_echeance_tranche_3, date(2027, 5, 10))
        grille_bus = GrilleTarifaireBus.objects.get(ecole=self.ecole, zone='Kipé', annee_scolaire='2026-2027')
        self.assertEqual(grille_bus.montant_annuel, 270000)
        self.assertEqual(grille_bus.date_echeance_tranche_1, date(2026, 12, 1))

    def test_grilles_deja_personnalisees_ne_sont_pas_ecrasees(self):
        from bus.models import GrilleTarifaireBus
        grille = GrilleTarifaire.objects.get(ecole=self.ecole, niveau='PRIMAIRE_2', annee_scolaire='2026-2027')
        grille.tranche_1 = 450000
        grille.save()
        for annee, prix in [('2025-2026', 100000), ('2026-2027', 200000)]:
            GrilleTarifaireBus.objects.create(ecole=self.ecole, zone='Kipé', annee_scolaire=annee,
                tranche_1=prix, tranche_2=prix, tranche_3=prix)
        self._creer_annee(dupliquer_grilles='1')
        self.assertEqual(EcheancierPaiement.objects.get(eleve=self.eleve,
            annee_scolaire='2026-2027').tranche_1_due, 450000)
        self.assertEqual(GrilleTarifaireBus.objects.get(ecole=self.ecole, zone='Kipé',
            annee_scolaire='2026-2027').montant_annuel, 600000)

    def test_passage_ne_duplique_pas_les_encaissements_et_conserve_la_dette_ancienne(self):
        from bus.models import AbonnementBus, AbonnementCantine
        from paiements.models import ModePaiement, Paiement, TypePaiement
        paiement = Paiement.objects.create(eleve=self.eleve, numero_recu='ANCIEN-1', montant=40000,
            annee_scolaire='2025-2026', date_paiement=date(2025, 10, 1), statut='VALIDE',
            mode_paiement=ModePaiement.objects.create(nom='ESPECES'),
            type_paiement=TypePaiement.objects.create(nom='Scolarité'))
        self.ancien_echeancier.refresh_from_db()
        avant = (self.ancien_echeancier.total_du, self.ancien_echeancier.total_paye,
                 self.ancien_echeancier.solde_restant)
        bus = AbonnementBus.objects.create(eleve=self.eleve, montant=20000,
            date_debut=date(2025, 10, 1), date_expiration=date(2026, 6, 30))
        cantine = AbonnementCantine.objects.create(eleve=self.eleve, montant=40000,
            montant_tarif=60000, type_remise='POURCENTAGE', valeur_remise=10,
            date_debut=date(2025, 10, 1), date_expiration=date(2026, 6, 30))
        self._creer_annee()
        self.ancien_echeancier.refresh_from_db()
        self.assertEqual((self.ancien_echeancier.total_du, self.ancien_echeancier.total_paye,
                          self.ancien_echeancier.solde_restant), avant)
        paiement.refresh_from_db()
        bus.refresh_from_db()
        cantine.refresh_from_db()
        self.assertEqual(Paiement.objects.count(), 1)
        self.assertEqual(paiement.annee_scolaire, '2025-2026')
        self.assertEqual(AbonnementBus.objects.count(), 1)
        self.assertEqual(AbonnementCantine.objects.count(), 1)
        self.assertEqual(bus.annee_scolaire, '2025-2026')
        self.assertEqual(cantine.annee_scolaire, '2025-2026')
        self.assertEqual(cantine.reste_a_payer, 14000)
        self.assertEqual(EcheancierPaiement.objects.get(eleve=self.eleve,
            annee_scolaire='2026-2027').total_paye, 0)

    def test_inscription_directe_dans_nouvelle_annee_garde_son_tarif_d_inscription(self):
        from eleves.views_nouvelle_annee import _creer_echeancier
        from types import SimpleNamespace
        classe = Classe.objects.create(ecole=self.ecole, nom='1ÈRE ANNÉE A', niveau='PRIMAIRE_1',
            annee_scolaire='2026-2027')
        nouvel_eleve = Eleve.objects.create(matricule='NOUVEAU-1', prenom='Nouveau', nom='Camara', classe=classe)
        _creer_echeancier(SimpleNamespace(user=self.admin), nouvel_eleve, self.ecole,
                          '2026-2027', reinscription=False)
        echeancier = EcheancierPaiement.objects.get(eleve=nouvel_eleve, annee_scolaire='2026-2027')
        self.assertEqual(echeancier.nature_frais, EcheancierPaiement.NATURE_INSCRIPTION)
        self.assertEqual(echeancier.frais_inscription_du, 200000)

    def test_versement_anticipe_de_la_nouvelle_annee_est_pris_en_compte(self):
        from paiements.models import ModePaiement, Paiement, TypePaiement
        mode = ModePaiement.objects.create(nom='ESPECES')
        type_paiement = TypePaiement.objects.create(nom='Scolarité')
        # Simulation d'un import/sync arrivé avant la création de l'échéancier.
        Paiement.objects.bulk_create([Paiement(eleve=self.eleve, numero_recu='ANTICIPE-1',
            montant=100000, annee_scolaire='2026-2027', date_paiement=date(2026, 8, 10),
            statut='VALIDE', mode_paiement=mode, type_paiement=type_paiement)])
        self._creer_annee()
        nouveau = EcheancierPaiement.objects.get(eleve=self.eleve, annee_scolaire='2026-2027')
        self.assertEqual(nouveau.total_paye, 100000)
        self.assertEqual(nouveau.solde_restant, 680000)
        self.assertTrue(nouveau.est_reinscription)
        self.assertEqual(Paiement.objects.count(), 1)

    def _eleve_sans_moyenne(self):
        eleve = Eleve.objects.create(
            matricule='PAS-003', prenom='Fodé', nom='Bangoura', sexe='M',
            date_naissance=date(2018, 3, 3), classe=self.classe_1,
            date_inscription=date(2025, 9, 1), responsable_principal=self.eleve.responsable_principal,
            statut='ACTIF',
        )
        Classement.objects.create(
            eleve=eleve, classe=self.classe_note, periode='ANNUEL_TRIM',
            annee_scolaire='2025-2026', moyenne_generale=Decimal('3'),
            total_points=Decimal('3'), total_coefficients=Decimal('1'),
            rang=2, rang_formate='2e/2', effectif=2,
        )
        return eleve

    def test_apercu_affiche_les_eleves_dans_leurs_classes_de_la_nouvelle_annee(self):
        faible = self._eleve_sans_moyenne()
        response = self.client.get(reverse('eleves:nouvelle_annee_apercu'))
        par_classe = {c['nom']: [i['eleve'].pk for i in c['eleves']]
                      for c in response.context['classes_nouvelles']}
        self.assertEqual(par_classe['2ÈME ANNÉE A'], [self.eleve.pk])
        self.assertEqual(par_classe['1ÈRE ANNÉE A'], [faible.pk])
        self.assertContains(response, 'name="eleves_confirmes" value="%d"' % self.eleve.pk)
        self.assertContains(response, 'name="eleves_convention" value="%d"' % faible.pk)
        self.assertContains(response, 'name="confirmation_passage" value="1"')
        self.assertContains(response, 'name="eleves_admis_affiches" value="%d"' % self.eleve.pk)

    def test_seuls_les_eleves_coches_passent_en_classe_superieure(self):
        faible = self._eleve_sans_moyenne()
        admis_decoche = Eleve.objects.create(
            matricule='PAS-004', prenom='Mariama', nom='Sylla', sexe='F',
            date_naissance=date(2018, 4, 4), classe=self.classe_1,
            date_inscription=date(2025, 9, 1), responsable_principal=self.eleve.responsable_principal,
            statut='ACTIF',
        )
        Classement.objects.create(
            eleve=admis_decoche, classe=self.classe_note, periode='ANNUEL_TRIM',
            annee_scolaire='2025-2026', moyenne_generale=Decimal('9'),
            total_points=Decimal('9'), total_coefficients=Decimal('1'),
            rang=1, rang_formate='1er/3', effectif=3,
        )
        apercu = self.client.get(reverse('eleves:nouvelle_annee_apercu'))
        response = self._creer_annee(confirmation_passage='1',
                                     eleves_admis_affiches=apercu.context['eleves_admis_affiches'],
                                     eleves_confirmes=[str(self.eleve.pk)],
                                     eleves_convention=[str(faible.pk)])
        self.assertEqual(response.status_code, 302)
        for eleve, classe in [(self.eleve, '2ÈME ANNÉE A'), (faible, '2ÈME ANNÉE A'),
                              (admis_decoche, '1ÈRE ANNÉE A')]:
            eleve.refresh_from_db()
            self.assertEqual((eleve.classe.nom, eleve.classe.annee_scolaire), (classe, '2026-2027'))
        messages = [str(m) for m in response.wsgi_request._messages]
        self.assertTrue(any('1 élève(s) redoublant(s)' in m for m in messages), messages)
        self.assertTrue(any('1 élève(s) promu(s) par convention' in m for m in messages), messages)

    def test_inscrit_apres_l_apercu_n_est_pas_retenu(self):
        affiches = self.client.get(reverse('eleves:nouvelle_annee_apercu')).context['eleves_admis_affiches']
        tardif = Eleve.objects.create(
            matricule='PAS-005', prenom='Ibrahima', nom='Touré', sexe='M',
            date_naissance=date(2018, 5, 5), classe=Classe.objects.get(nom='2ÈME ANNÉE A'),
            date_inscription=date(2026, 6, 1), responsable_principal=self.eleve.responsable_principal,
            statut='ACTIF',
        )
        Classement.objects.create(
            eleve=tardif, classe=self.classe_note, periode='ANNUEL_TRIM',
            annee_scolaire='2025-2026', moyenne_generale=Decimal('7'),
            total_points=Decimal('7'), total_coefficients=Decimal('1'),
            rang=1, rang_formate='1er/1', effectif=1,
        )
        Classe.objects.create(ecole=self.ecole, nom='3ÈME ANNÉE A', niveau='PRIMAIRE_3',
                              annee_scolaire='2025-2026')
        GrilleTarifaire.objects.create(
            ecole=self.ecole, niveau='PRIMAIRE_3', annee_scolaire='2026-2027',
            frais_inscription=200000, frais_reinscription=80000,
            tranche_1=300000, tranche_2=250000, tranche_3=150000,
        )
        self._creer_annee(confirmation_passage='1', eleves_admis_affiches=affiches,
                          eleves_confirmes=[str(self.eleve.pk)])
        tardif.refresh_from_db()
        self.assertEqual((tardif.classe.nom, tardif.classe.annee_scolaire), ('3ÈME ANNÉE A', '2026-2027'))

    def test_confirmation_ne_depend_pas_de_la_session(self):
        """Plusieurs processus web ne partagent pas la session : la liste de
        l'aperçu est envoyée avec le formulaire (avant : « aperçu expiré »)."""
        response = self._creer_annee(confirmation_passage='1',
                                     eleves_admis_affiches=str(self.eleve.pk))
        self.assertEqual(response.status_code, 302)
        self.eleve.refresh_from_db()
        # Affiché admis mais décoché : il redouble dans la nouvelle année.
        self.assertEqual((self.eleve.classe.nom, self.eleve.classe.annee_scolaire),
                         ('1ÈRE ANNÉE A', '2026-2027'))

    def test_eleve_non_evalue_passe_par_defaut(self):
        Classement.objects.filter(eleve=self.eleve).delete()
        apercu = self.client.get(reverse('eleves:nouvelle_annee_apercu'))
        self.assertEqual(apercu.context['eleves_admis_affiches'], str(self.eleve.pk))
        self.assertEqual(apercu.context['eleves_non_admis'], [])
        self.assertContains(apercu, 'cb-confirme cb-non-evalue" checked')
        self._creer_annee(confirmation_passage='1',
                          eleves_admis_affiches=apercu.context['eleves_admis_affiches'],
                          eleves_confirmes=[str(self.eleve.pk)])
        self.eleve.refresh_from_db()
        self.assertEqual((self.eleve.classe.nom, self.eleve.classe.annee_scolaire),
                         ('2ÈME ANNÉE A', '2026-2027'))

    # ── Les autres modules après la création de l'année ──────────────────────

    def _choisir_annee(self, annee):
        from eleves.utils_annee import SESSION_ANNEE_ACTIVE
        session = self.client.session
        session[SESSION_ANNEE_ACTIVE] = annee
        session.save()

    def test_enseignant_reconduit_dans_la_classe_de_la_nouvelle_annee(self):
        from salaires.models import AffectationClasse, Enseignant, PeriodeSalaire, TypeEnseignant
        from salaires.services import classe_principale, effectif_classe_principale
        self._eleve_sans_moyenne()  # redouble : reste en 1ère année A
        enseignant = Enseignant.objects.create(
            nom='Maître', prenoms='Test', ecole=self.ecole, type_enseignant=TypeEnseignant.PRIMAIRE,
            statut='ACTIF', salaire_fixe=Decimal('1000000'), heures_mensuelles=Decimal('160'),
            date_embauche=date(2025, 1, 1), cree_par=self.admin,
        )
        ancienne = AffectationClasse.objects.create(
            enseignant=enseignant, classe=self.classe_1, date_debut=date(2025, 9, 1),
        )
        self._creer_annee(reconduire_affectations='1')

        ancienne.refresh_from_db()
        self.assertEqual((ancienne.actif, ancienne.date_fin), (False, date(2026, 8, 31)))
        nouvelle = AffectationClasse.objects.get(enseignant=enseignant, actif=True)
        self.assertEqual((nouvelle.classe.nom, nouvelle.classe.annee_scolaire), ('1ÈRE ANNÉE A', '2026-2027'))
        self.assertEqual(nouvelle.date_debut, date(2026, 9, 1))

        octobre = PeriodeSalaire.objects.create(mois=10, annee=2026, ecole=self.ecole,
                                                nombre_semaines=Decimal('4'), cree_par=self.admin)
        juin = PeriodeSalaire.objects.create(mois=6, annee=2026, ecole=self.ecole,
                                             nombre_semaines=Decimal('4'), cree_par=self.admin)
        self.assertEqual(classe_principale(enseignant, octobre), nouvelle.classe)
        self.assertEqual(effectif_classe_principale(enseignant, octobre), 1)
        self.assertEqual(classe_principale(enseignant, juin), self.classe_1)

    def test_recouvrement_et_bibliotheque_restent_dans_leur_annee(self):
        from depenses.models_bibliotheque import CategorieLivre, Emprunt, Livre
        from depenses.models_recouvrement import DepenseCuisine
        DepenseCuisine.objects.create(date=date(2026, 3, 10), designation='Riz 2025-2026',
                                      montant=100000, cree_par=self.admin)
        DepenseCuisine.objects.create(date=date(2026, 10, 5), designation='Riz 2026-2027',
                                      montant=30000, cree_par=self.admin)
        livre = Livre.objects.create(
            code_livre='LIV-AN-1', titre='Livre', auteur='Auteur',
            categorie=CategorieLivre.objects.create(nom='Romans', code='ROM-AN'),
            emplacement='A', nombre_exemplaires=3, exemplaires_disponibles=1,
            statut='DISPONIBLE', cree_par=self.admin,
        )
        emprunts = {}
        for numero, jour, rendu in [('RENDU', date(2026, 3, 1), date(2026, 3, 10)),
                                    ('A-RENDRE', date(2026, 5, 2), None),
                                    ('NOUVEAU', date(2026, 10, 1), None)]:
            emprunts[numero] = Emprunt.objects.create(
                numero_emprunt=numero, livre=livre, eleve=self.eleve, date_emprunt=jour,
                date_retour_prevue=jour + timedelta(days=14), date_retour_effectif=rendu,
                statut='RETOURNE' if rendu else 'EN_COURS', cree_par=self.admin,
            )
        self._creer_annee()

        cuisine = reverse('depenses:liste_module_simple', args=['cuisine'])
        emprunts_url = reverse('depenses:liste_emprunts')
        self.assertEqual(self.client.get(cuisine).context['total_montant'], 30000)
        self.assertEqual({e.numero_emprunt for e in self.client.get(emprunts_url).context['emprunts']},
                         {'A-RENDRE', 'NOUVEAU'})
        self.assertEqual(self.client.get(reverse('depenses:tableau_bord')).status_code, 200)

        self._choisir_annee('2025-2026')
        self.assertEqual(self.client.get(cuisine).context['total_montant'], 100000)
        self.assertEqual({e.numero_emprunt for e in self.client.get(emprunts_url).context['emprunts']},
                         {'RENDU', 'A-RENDRE'})

    def test_salaires_et_notes_listent_l_annee_active(self):
        from salaires.models import PeriodeSalaire
        for mois, annee in [(6, 2026), (10, 2026)]:
            PeriodeSalaire.objects.create(mois=mois, annee=annee, ecole=self.ecole,
                                          nombre_semaines=Decimal('4'), cree_par=self.admin)
        self._creer_annee(dupliquer_notes_classes='1')

        periodes = self.client.get(reverse('salaires:gestion_periodes')).context['periodes']
        self.assertEqual([(p.mois, p.annee) for p in periodes], [(10, 2026)])
        classes = self.client.get(reverse('notes:saisir_notes')).context['classes']
        self.assertEqual({c.annee_scolaire for c in classes}, {'2026-2027'})

        self._choisir_annee('2025-2026')
        periodes = self.client.get(reverse('salaires:gestion_periodes')).context['periodes']
        self.assertEqual([(p.mois, p.annee) for p in periodes], [(6, 2026)])
        classes = self.client.get(reverse('notes:saisir_notes')).context['classes']
        self.assertEqual({c.annee_scolaire for c in classes}, {'2025-2026'})

    def test_eleve_importe_en_attente_de_paiement_passe_aussi(self):
        Eleve.objects.filter(pk=self.eleve.pk).update(statut='ATTENTE_PAIEMENT', import_verrouille=True)
        apercu = self.client.get(reverse('eleves:nouvelle_annee_apercu'))
        self.assertEqual(apercu.context['nb_total_eleves'], 1)
        self._creer_annee()
        self.eleve.refresh_from_db()
        self.assertEqual((self.eleve.classe.nom, self.eleve.classe.annee_scolaire), ('2ÈME ANNÉE A', '2026-2027'))
        self.assertEqual(self.eleve.statut, 'ATTENTE_PAIEMENT')
        self._choisir_annee('2026-2027')
        self.assertIsNone(self.client.get(reverse('eleves:liste_eleves')).context['passage_en_attente'])

    def test_liste_signale_les_eleves_restes_dans_l_annee_precedente(self):
        Classe.objects.create(ecole=self.ecole, nom='1ÈRE ANNÉE A', niveau='PRIMAIRE_1',
                              annee_scolaire='2026-2027')
        self._choisir_annee('2026-2027')
        liste = self.client.get(reverse('eleves:liste_eleves'))
        self.assertEqual(liste.context['passage_en_attente']['nb'], 1)
        self.assertContains(liste, 'Compléter le passage')

    # ── Année créée par erreur ───────────────────────────────────────────────

    def _creer_annee_suivante_par_erreur(self):
        """Ancien comportement : 2027-2028 créée juste après 2026-2027."""
        from unittest.mock import patch
        with patch('eleves.views_nouvelle_annee.creation_annee_trop_tot', return_value=None):
            return self.client.post(reverse('eleves:nouvelle_annee_creer'), {
                'annee_courante': '2026-2027', 'annee_nouvelle': '2027-2028',
                'faire_passer_eleves': '1', 'dupliquer_grilles': '1',
                'reconduire_affectations': '1', 'dupliquer_notes_classes': '1',
            })

    def test_annee_suivante_ne_peut_pas_etre_creee_des_maintenant(self):
        from unittest.mock import patch
        from eleves.utils_annee import creation_annee_trop_tot

        class _Date(date):
            @classmethod
            def today(cls):
                return date(2026, 11, 6)

        self.assertEqual(creation_annee_trop_tot('2027-2028', today=date(2026, 11, 6)), date(2027, 3, 1))
        self.assertIsNone(creation_annee_trop_tot('2027-2028', today=date(2027, 3, 1)))
        self._creer_annee(dupliquer_grilles='1')
        with patch('eleves.utils_annee.date', _Date):
            apercu = self.client.get(reverse('eleves:nouvelle_annee_apercu'))
            self.assertEqual(apercu.context['annee_nouvelle'], '2027-2028')
            self.assertContains(apercu, 'Trop tôt')
            response = self.client.post(reverse('eleves:nouvelle_annee_creer'), {
                'annee_courante': '2026-2027', 'annee_nouvelle': '2027-2028', 'faire_passer_eleves': '1',
            })
        self.assertRedirects(response, reverse('eleves:gestion_annees'), fetch_redirect_response=False)
        self.assertFalse(Classe.objects.filter(annee_scolaire='2027-2028').exists())
        self.eleve.refresh_from_db()
        self.assertEqual(self.eleve.classe.annee_scolaire, '2026-2027')

    def test_annulation_ramene_chacun_dans_sa_classe_de_l_annee_precedente(self):
        from salaires.models import AffectationClasse, Enseignant, TypeEnseignant
        from eleves.models import HistoriqueEleve
        fode = self._eleve_sans_moyenne()  # redouble en 2026-2027, passe (non évalué) en 2027-2028
        enseignant = Enseignant.objects.create(
            nom='Maître', prenoms='Test', ecole=self.ecole, type_enseignant=TypeEnseignant.PRIMAIRE,
            statut='ACTIF', salaire_fixe=Decimal('1000000'), heures_mensuelles=Decimal('160'),
            date_embauche=date(2025, 1, 1), cree_par=self.admin,
        )
        AffectationClasse.objects.create(enseignant=enseignant, classe=self.classe_1,
                                         date_debut=date(2025, 9, 1))
        self._creer_annee(dupliquer_grilles='1', reconduire_affectations='1', dupliquer_notes_classes='1')
        echeancier_2026 = EcheancierPaiement.objects.get(eleve=self.eleve, annee_scolaire='2026-2027')
        # Élève de 2026-2027 sorti en fin de cycle par le passage erroné.
        sorti = Eleve.objects.create(
            matricule='PAS-009', prenom='Mamadou', nom='Diallo', sexe='M', date_naissance=date(2008, 1, 1),
            classe=Classe.objects.get(nom='2ÈME ANNÉE A', annee_scolaire='2026-2027'),
            responsable_principal=self.eleve.responsable_principal, statut='TRANSFERE',
        )
        self._creer_annee_suivante_par_erreur()
        HistoriqueEleve.objects.create(
            eleve=sorti, action='FIN_CYCLE',
            description="Fin de cycle — sorti(e) du système lors du passage à l'année 2027-2028. Classe: X",
        )
        self.eleve.refresh_from_db()
        fode.refresh_from_db()
        self.assertEqual(self.eleve.classe.annee_scolaire, '2027-2028')
        self.assertEqual((fode.classe.nom, fode.classe.annee_scolaire), ('2ÈME ANNÉE A', '2027-2028'))

        annees = {a['annee']: a for a in self.client.get(reverse('eleves:gestion_annees')).context['annees']}
        self.assertTrue(annees['2027-2028']['peut_annuler'])
        self.assertFalse(annees['2026-2027']['peut_annuler'])
        url = reverse('eleves:annuler_annee', args=['2027-2028'])
        page = self.client.get(url)
        self.assertEqual((page.context['nb_eleves'], page.context['nb_sortis']), (2, 1))
        self.client.post(url, {'confirmation': 'oui'})
        self.assertTrue(Classe.objects.filter(annee_scolaire='2027-2028').exists())

        response = self.client.post(url, {'confirmation': '2027-2028'})
        self.assertRedirects(response, reverse('eleves:gestion_annees'), fetch_redirect_response=False)
        for modele in (Classe, GrilleTarifaire, EcheancierPaiement, ClasseNote):
            self.assertFalse(modele.objects.filter(annee_scolaire='2027-2028').exists(), modele)
        for eleve, classe in ((self.eleve, '2ÈME ANNÉE A'), (fode, '1ÈRE ANNÉE A'), (sorti, '2ÈME ANNÉE A')):
            eleve.refresh_from_db()
            self.assertEqual((eleve.classe.nom, eleve.classe.annee_scolaire, eleve.statut),
                             (classe, '2026-2027', 'ACTIF'))
        self.assertTrue(EcheancierPaiement.objects.filter(pk=echeancier_2026.pk).exists())
        active = AffectationClasse.objects.get(enseignant=enseignant, actif=True)
        self.assertEqual((active.classe.annee_scolaire, active.date_fin), ('2026-2027', None))
        from eleves.utils_annee import SESSION_ANNEE_ACTIVE
        self.assertEqual(self.client.session[SESSION_ANNEE_ACTIVE], '2026-2027')

    def _connecter_role(self, role):
        user = get_user_model().objects.create_user(f'role-{role.lower()}', f'{role}@p.gn', 'x')
        profil = user.profil
        profil.ecole, profil.role, profil.is_validated, profil.actif = self.ecole, role, True, True
        profil.save()
        self.client.force_login(user)

    def test_annulation_ouverte_au_directeur_et_signalee_aux_autres_roles(self):
        self._creer_annee(dupliquer_grilles='1')
        self._creer_annee_suivante_par_erreur()
        url = reverse('eleves:annuler_annee', args=['2027-2028'])

        self._connecter_role('SECRETAIRE')
        page = self.client.get(reverse('eleves:gestion_annees'))
        self.assertFalse(page.context['annulation_autorisee'])
        self.assertContains(page, 'Administrateur ou directeur uniquement')
        self.assertNotContains(page, url)
        self.assertRedirects(self.client.get(url), reverse('eleves:gestion_annees'),
                             fetch_redirect_response=False)

        self._connecter_role('DIRECTEUR')
        page = self.client.get(reverse('eleves:gestion_annees'))
        self.assertTrue(page.context['annulation_autorisee'])
        self.assertContains(page, url)
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_annulation_par_lots(self):
        from unittest.mock import patch
        self._eleve_sans_moyenne()
        self._creer_annee(dupliquer_grilles='1')
        self._creer_annee_suivante_par_erreur()
        url = reverse('eleves:annuler_annee', args=['2027-2028'])
        with patch('eleves.annulation_annee.TAILLE_LOT_ANNULATION', 1):
            premier = self.client.post(url, {'confirmation': '2027-2028', 'total': '2'})
            self.assertTemplateUsed(premier, 'eleves/annuler_annee_progression.html')
            self.assertEqual(Eleve.objects.filter(classe__annee_scolaire='2027-2028').count(), 1)
            self.client.post(url, {'confirmation': '2027-2028', 'total': '2'})
        self.assertFalse(Classe.objects.filter(annee_scolaire='2027-2028').exists())
        self.assertEqual(Eleve.objects.filter(classe__annee_scolaire='2026-2027').count(), 2)

    def test_annulation_refusee_si_un_paiement_existe_dans_l_annee(self):
        from paiements.models import ModePaiement, Paiement, TypePaiement
        self._creer_annee(dupliquer_grilles='1')
        self._creer_annee_suivante_par_erreur()
        Paiement.objects.bulk_create([Paiement(
            eleve=self.eleve, numero_recu='P-2027', montant=100000, annee_scolaire='2027-2028',
            date_paiement=date(2026, 11, 6), statut='VALIDE',
            mode_paiement=ModePaiement.objects.create(nom='ESPECES'),
            type_paiement=TypePaiement.objects.create(nom='Scolarité'))])
        url = reverse('eleves:annuler_annee', args=['2027-2028'])
        self.assertContains(self.client.get(url), 'Annulation impossible')
        self.client.post(url, {'confirmation': '2027-2028'})
        self.eleve.refresh_from_db()
        self.assertEqual(self.eleve.classe.annee_scolaire, '2027-2028')
        # Seule l'année la plus récente peut être annulée.
        self.client.post(reverse('eleves:annuler_annee', args=['2026-2027']), {'confirmation': '2026-2027'})
        self.assertTrue(Classe.objects.filter(annee_scolaire='2026-2027').exists())
