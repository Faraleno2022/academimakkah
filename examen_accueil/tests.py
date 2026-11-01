"""Tests du module Test d'accueil et du retrait du chatbot."""
import io
from datetime import date
from decimal import Decimal

from django.apps import apps
from django.conf import settings
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from eleves.models import Classe, Ecole, Eleve
from paiements.models import EcheancierPaiement
from utilisateurs.models import Profil

from .calculs import calculer_resultats, statistiques
from .models import MatiereAccueil, NoteAccueil, ParametreAccueil

MIDDLEWARE_SANS_LICENCE = [m for m in settings.MIDDLEWARE if 'licence_middleware' not in m]
ANNEE = '2026-2027'


def creer_ecole(suffixe):
    return Ecole.objects.create(nom=f"École Accueil {suffixe}", adresse="Conakry",
                                telephone=f"+22462200000{suffixe}", directeur="Direction")


def creer_utilisateur(nom, ecole, role='ADMIN'):
    user = User.objects.create_user(nom, f'{nom}@test.gn', 'x')
    Profil.objects.filter(user=user).update(ecole=ecole, role=role, actif=True, is_validated=True)
    return User.objects.get(pk=user.pk)


def creer_classe(ecole, nom="7ème A", niveau='COLLEGE_7'):
    return Classe.objects.create(ecole=ecole, nom=nom, niveau=niveau, annee_scolaire=ANNEE)


def creer_eleve(classe, nom, prenom, sexe='M'):
    matricule = f"TA-{classe.ecole_id}-{Eleve.objects.count() + 1}"
    return Eleve.objects.create(matricule=matricule, prenom=prenom, nom=nom, sexe=sexe,
                                date_naissance=date(2013, 3, 1), classe=classe, statut='ACTIF')


def creer_test(ecole, notes):
    """Classe de l'école avec un test de Français (coef 2) et Maths (coef 1).

    ``notes`` : {(nom, prenom, sexe): (note_francais, note_maths)} ; None = non noté.
    """
    classe = creer_classe(ecole)
    francais = MatiereAccueil.objects.create(classe=classe, nom="Français", coefficient=Decimal('2'), ordre=1)
    maths = MatiereAccueil.objects.create(classe=classe, nom="Mathématiques", coefficient=Decimal('1'), ordre=2)
    for (nom, prenom, sexe), valeurs in notes.items():
        eleve = creer_eleve(classe, nom, prenom, sexe)
        for matiere, valeur in zip((francais, maths), valeurs):
            if valeur is not None:
                NoteAccueil.objects.create(eleve=eleve, matiere=matiere, note=Decimal(str(valeur)))
    return classe, francais, maths


class CalculsTests(TestCase):
    def setUp(self):
        self.ecole = creer_ecole('1')

    def test_moyenne_ponderee_rang_et_admission(self):
        classe, _, _ = creer_test(self.ecole, {
            ('BAH', 'Aminata', 'F'): (14, 8),       # (28 + 8) / 3 = 12
            ('DIALLO', 'Ibrahima', 'M'): (9, 15),   # (18 + 15) / 3 = 11
            ('CAMARA', 'Mariama', 'F'): (8, 8),     # 8
        })
        resultats = calculer_resultats(classe)
        self.assertEqual([r['eleve'].nom for r in resultats], ['BAH', 'DIALLO', 'CAMARA'])
        self.assertEqual([r['moyenne'] for r in resultats], [Decimal('12.00'), Decimal('11.00'), Decimal('8.00')])
        self.assertEqual([r['rang_texte'] for r in resultats], ['1ère', '2ème', '3ème'])
        self.assertEqual([r['admis'] for r in resultats], [True, True, False])
        stats = statistiques(resultats)
        self.assertEqual((stats['admis'], stats['non_admis'], stats['moyenne_classe']), (2, 1, Decimal('10.33')))

    def test_matiere_non_notee_compte_zero_et_ex_aequo(self):
        classe, _, _ = creer_test(self.ecole, {
            ('SOW', 'Alpha', 'M'): (15, None),      # 30 / 3 = 10 : l'absence aux maths compte 0
            ('BARRY', 'Oumar', 'M'): (10, 10),      # 10
            ('KANTE', 'Fanta', 'F'): (None, 3),     # 1
        })
        resultats = {r['eleve'].nom: r for r in calculer_resultats(classe)}
        self.assertEqual(resultats['SOW']['moyenne'], Decimal('10.00'))
        self.assertEqual(resultats['SOW']['rang_texte'], '1er ex')
        self.assertEqual(resultats['BARRY']['rang_texte'], '1er ex')
        self.assertEqual(resultats['KANTE']['moyenne'], Decimal('1.00'))
        self.assertEqual(resultats['KANTE']['rang'], 3)

    def test_seuls_les_eleves_notes_sont_classes(self):
        classe, _, _ = creer_test(self.ecole, {('BAH', 'Awa', 'F'): (12, 9)})
        creer_eleve(classe, 'SYLLA', 'Ali')     # inscrit mais pas encore évalué
        self.assertEqual([r['eleve'].nom for r in calculer_resultats(classe)], ['BAH'])

    def test_parametres_de_la_classe(self):
        classe, _, _ = creer_test(self.ecole, {('BAH', 'Awa', 'F'): (6, 6)})
        ParametreAccueil.objects.create(classe=classe, note_sur=10, moyenne_admission=Decimal('5'))
        ligne = calculer_resultats(classe)[0]
        self.assertTrue(ligne['admis'])
        self.assertEqual(ligne['mention'], 'Assez bien')   # 6/10 = 12/20


@override_settings(MIDDLEWARE=MIDDLEWARE_SANS_LICENCE)
class ParcoursCompletTests(TestCase):
    """Classes → paramètres → matières → élèves → saisie → consultation → bulletins."""

    def setUp(self):
        self.ecole = creer_ecole('2')
        self.user = creer_utilisateur('directeur_accueil', self.ecole)
        self.client.force_login(self.user)
        self.classe = creer_classe(self.ecole, "11ème Sciences", 'LYCEE_11')
        self.saran = creer_eleve(self.classe, 'CONDE', 'Saran', 'F')
        self.moussa = creer_eleve(self.classe, 'TOURE', 'Moussa')

    def url(self, nom, **params):
        params = {'classe': self.classe.pk, **params}
        return reverse(f'examen_accueil:{nom}') + '?' + '&'.join(f'{c}={v}' for c, v in params.items())

    def test_parcours_de_bout_en_bout(self):
        for nom in ('tableau_bord', 'classes'):
            self.assertEqual(self.client.get(reverse(f'examen_accueil:{nom}')).status_code, 200, nom)
        self.assertEqual(self.client.get(reverse('examen_accueil:classes')).context['lignes'][0]['classe'], self.classe)

        reponse = self.client.post(reverse('examen_accueil:parametres_classe', args=[self.classe.pk]), {
            'date_test': '2026-09-20', 'note_sur': '20', 'moyenne_admission': '10', 'observations': '',
        })
        self.assertRedirects(reponse, reverse('examen_accueil:classes'))
        parametre = ParametreAccueil.objects.get(classe=self.classe)
        self.assertEqual((parametre.date_test, parametre.cree_par), (date(2026, 9, 20), self.user))

        for nom, coefficient in [('Français', '3'), ('Physique', '2')]:
            self.client.post(self.url('matieres'), {'nom': nom, 'coefficient': coefficient, 'ordre': '1'})
        francais = MatiereAccueil.objects.get(classe=self.classe, nom='Français')
        physique = MatiereAccueil.objects.get(classe=self.classe, nom='Physique')
        self.assertEqual(self.client.get(reverse('examen_accueil:modifier_matiere', args=[francais.pk])).status_code, 200)

        reponse = self.client.get(self.url('eleves'))
        self.assertEqual([l['eleve'] for l in reponse.context['lignes']], [self.saran, self.moussa])
        self.assertContains(reponse, 'Nouvel inscrit')

        reponse = self.client.get(self.url('saisie_notes'))
        self.assertContains(reponse, f'note_{self.saran.pk}_{francais.pk}')
        reponse = self.client.post(self.url('saisie_notes'), {
            f'note_{self.saran.pk}_{francais.pk}': '15,5', f'note_{self.saran.pk}_{physique.pk}': '12',
            f'note_{self.moussa.pk}_{francais.pk}': '7', f'note_{self.moussa.pk}_{physique.pk}': '',
        })
        self.assertRedirects(reponse, self.url('saisie_notes'))
        self.assertEqual(NoteAccueil.objects.get(eleve=self.saran, matiere=francais).note, Decimal('15.50'))
        self.assertEqual(NoteAccueil.objects.count(), 3)
        # Même indicateur que la liste des élèves du module Élèves.
        self.saran.refresh_from_db()
        self.assertTrue(self.saran.evaluation_accueil_effectuee)

        reponse = self.client.get(self.url('consultation'))
        lignes = reponse.context['lignes']
        self.assertEqual(lignes[0]['eleve'], self.saran)
        self.assertEqual(lignes[0]['moyenne'], Decimal('14.10'))   # (46,5 + 24) / 5
        self.assertEqual(lignes[1]['moyenne'], Decimal('4.20'))    # 21 / 5, physique non notée = 0
        self.assertContains(reponse, 'Admis')

        pdf = self.client.get(reverse('examen_accueil:resultats_pdf', args=[self.classe.pk]))
        self.assertEqual(pdf['Content-Type'], 'application/pdf')
        self.assertTrue(pdf.content.startswith(b'%PDF'))

        excel = self.client.get(reverse('examen_accueil:resultats_excel', args=[self.classe.pk]))
        self.assertIn('spreadsheetml', excel['Content-Type'])
        from openpyxl import load_workbook
        feuille = load_workbook(io.BytesIO(excel.content)).active
        self.assertEqual([feuille.cell(row=6, column=c).value for c in (1, 2, 3, 4, 5, 6, 8)],
                         ['1ère', self.saran.matricule, self.saran.nom, self.saran.prenom, 15.5, 12, 14.1])
        self.assertEqual(feuille.cell(row=6, column=10).value, 'ADMISE')
        self.assertEqual(feuille.cell(row=7, column=10).value, 'NON ADMIS')

        self.assertEqual(len(self.client.get(self.url('bulletins')).context['lignes']), 2)
        bulletins = self.client.get(reverse('examen_accueil:bulletins_pdf', args=[self.classe.pk]))
        self.assertTrue(bulletins.content.startswith(b'%PDF'))
        bulletin = self.client.get(reverse('examen_accueil:bulletin_eleve', args=[self.classe.pk, self.moussa.pk]))
        self.assertTrue(bulletin.content.startswith(b'%PDF'))
        self.assertIn('bulletin_test_accueil_moussa-toure.pdf', bulletin['Content-Disposition'])

    def test_reinscrits_masques_par_defaut(self):
        EcheancierPaiement.objects.create(
            eleve=self.moussa, annee_scolaire=ANNEE, nature_frais='REINSCRIPTION',
            date_echeance_inscription=date(2026, 9, 1), date_echeance_tranche_1=date(2026, 10, 1),
            date_echeance_tranche_2=date(2027, 1, 1), date_echeance_tranche_3=date(2027, 4, 1),
        )
        MatiereAccueil.objects.create(classe=self.classe, nom='Français')
        self.assertEqual([l['eleve'] for l in self.client.get(self.url('saisie_notes')).context['lignes']], [self.saran])
        self.assertEqual([l['eleve'] for l in self.client.get(self.url('saisie_notes', tous=1)).context['lignes']],
                         [self.saran, self.moussa])

    def test_reprendre_les_matieres_du_module_notes(self):
        from notes.models import ClasseNote, MatiereNote

        classe_note = ClasseNote.objects.create(ecole=self.ecole, nom="11ème sciences", niveau='LYCEE_11',
                                                annee_scolaire=ANNEE)
        MatiereNote.objects.create(classe=classe_note, nom="Mathématiques", code="MATH", coefficient=Decimal('4'))
        MatiereNote.objects.create(classe=classe_note, nom="Anglais", code="ANG", coefficient=None)
        MatiereAccueil.objects.create(classe=self.classe, nom="anglais", coefficient=Decimal('2'))
        self.client.post(self.url('matieres'), {'reprendre_notes': '1'})
        self.assertEqual(sorted(MatiereAccueil.objects.filter(classe=self.classe).values_list('nom', 'coefficient')),
                         [('Mathématiques', Decimal('4.00')), ('anglais', Decimal('2.00'))])

    def test_note_hors_bareme_n_enregistre_rien(self):
        francais = MatiereAccueil.objects.create(classe=self.classe, nom='Français')
        maths = MatiereAccueil.objects.create(classe=self.classe, nom='Maths')
        NoteAccueil.objects.create(eleve=self.saran, matiere=francais, note=Decimal('12'))
        reponse = self.client.post(self.url('saisie_notes'), {
            f'note_{self.saran.pk}_{francais.pk}': '18', f'note_{self.saran.pk}_{maths.pk}': '25',
        })
        self.assertEqual(reponse.status_code, 200)
        self.assertContains(reponse, 'hors du barème')
        self.assertEqual(NoteAccueil.objects.get(eleve=self.saran, matiere=francais).note, Decimal('12'))
        self.assertFalse(NoteAccueil.objects.filter(matiere=maths).exists())

    def test_case_videe_efface_la_note(self):
        francais = MatiereAccueil.objects.create(classe=self.classe, nom='Français')
        maths = MatiereAccueil.objects.create(classe=self.classe, nom='Maths')
        NoteAccueil.objects.create(eleve=self.saran, matiere=maths, note=Decimal('9'))
        self.client.post(self.url('saisie_notes'), {
            f'note_{self.saran.pk}_{francais.pk}': '12', f'note_{self.saran.pk}_{maths.pk}': ' ',
        })
        self.assertEqual(list(NoteAccueil.objects.values_list('matiere__nom', flat=True)), ['Français'])

    def test_parametres_invalides_refuses(self):
        url = reverse('examen_accueil:parametres_classe', args=[self.classe.pk])
        reponse = self.client.post(url, {'note_sur': '10', 'moyenne_admission': '12'})
        self.assertEqual(reponse.status_code, 200)
        self.assertFalse(ParametreAccueil.objects.exists())
        # Le barème ne descend pas sous des notes déjà saisies.
        matiere = MatiereAccueil.objects.create(classe=self.classe, nom='Français')
        NoteAccueil.objects.create(eleve=self.saran, matiere=matiere, note=Decimal('15'))
        reponse = self.client.post(url, {'note_sur': '10', 'moyenne_admission': '5'})
        self.assertContains(reponse, 'dépassent 10')
        self.assertFalse(ParametreAccueil.objects.exists())

    def test_menu_propose_le_test_d_accueil(self):
        reponse = self.client.get(reverse('examen_accueil:tableau_bord'))
        self.assertContains(reponse, reverse('examen_accueil:tableau_bord'))
        self.assertContains(reponse, "fa-clipboard-check me-1\"></i>Test d'accueil")
        self.assertNotContains(reponse, 'Chatbot')


@override_settings(MIDDLEWARE=MIDDLEWARE_SANS_LICENCE)
class IsolementEcoleTests(TestCase):
    def setUp(self):
        self.ecole_a, self.ecole_b = creer_ecole('3'), creer_ecole('4')
        self.classe_a, self.francais_a, _ = creer_test(self.ecole_a, {('BAH', 'Awa', 'F'): (12, 9)})
        self.eleve_a = Eleve.objects.get(classe=self.classe_a)
        creer_classe(self.ecole_b, "6ème B", 'PRIMAIRE_6')
        self.client.force_login(creer_utilisateur('directeur_b', self.ecole_b))

    def test_autre_ecole_invisible_et_inaccessible(self):
        lignes = self.client.get(reverse('examen_accueil:classes')).context['lignes']
        self.assertEqual([l['classe'].nom.upper() for l in lignes], ['6ÈME B'])
        for nom in ('matieres', 'eleves', 'saisie_notes', 'consultation', 'bulletins'):
            reponse = self.client.get(f"{reverse(f'examen_accueil:{nom}')}?classe={self.classe_a.pk}")
            self.assertIsNone(reponse.context['classe'], nom)
            self.assertNotContains(reponse, 'BAH')
        for nom, args in [('parametres_classe', [self.classe_a.pk]), ('resultats_pdf', [self.classe_a.pk]),
                          ('resultats_excel', [self.classe_a.pk]), ('bulletins_pdf', [self.classe_a.pk]),
                          ('modifier_matiere', [self.francais_a.pk]),
                          ('bulletin_eleve', [self.classe_a.pk, self.eleve_a.pk])]:
            self.assertEqual(self.client.get(reverse(f'examen_accueil:{nom}', args=args)).status_code, 404, nom)
        self.assertEqual(self.client.post(reverse('examen_accueil:supprimer_matiere', args=[self.francais_a.pk])).status_code, 404)
        self.client.post(f"{reverse('examen_accueil:saisie_notes')}?classe={self.classe_a.pk}",
                         {f'note_{self.eleve_a.pk}_{self.francais_a.pk}': '1'})
        self.assertEqual(NoteAccueil.objects.get(matiere=self.francais_a).note, Decimal('12'))

    def test_utilisateur_sans_droit_notes_refuse(self):
        surveillant = creer_utilisateur('surveillant_a', self.ecole_a, role='SURVEILLANT')
        Profil.objects.filter(user=surveillant).update(peut_gerer_notes=False)
        self.client.force_login(surveillant)
        self.assertEqual(self.client.get(reverse('examen_accueil:tableau_bord')).status_code, 403)


@override_settings(MIDDLEWARE=MIDDLEWARE_SANS_LICENCE)
class IndependanceNotesScolairesTests(TestCase):
    """Les notes du test d'accueil ne doivent JAMAIS entrer dans les moyennes scolaires."""

    def setUp(self):
        from notes.models import ClasseNote, MatiereNote, NoteMensuelle

        self.ecole = creer_ecole('5')
        self.client.force_login(creer_utilisateur('directeur_notes', self.ecole))
        self.classe = creer_classe(self.ecole, "10ème A", 'COLLEGE_10')
        self.eleve = creer_eleve(self.classe, 'Bah', 'Aissatou', 'F')
        self.classe_note = ClasseNote.objects.create(ecole=self.ecole, nom="10ème A", niveau='COLLEGE_10',
                                                     annee_scolaire=ANNEE)
        self.matiere_note = MatiereNote.objects.create(classe=self.classe_note, nom="Français", code="FR",
                                                       coefficient=Decimal('2'))
        NoteMensuelle.objects.create(eleve=self.eleve, matiere=self.matiere_note, mois='OCTOBRE',
                                     annee_scolaire=ANNEE, note=Decimal('8'))

    def _moyennes_scolaires(self):
        from notes.calculs_moyennes import calculer_moyenne_generale_eleve
        from notes.models import MatiereNote

        matieres = MatiereNote.objects.filter(classe=self.classe_note)
        return [calculer_moyenne_generale_eleve(self.eleve, matieres, periode, system_type)['moyenne_generale']
                for periode, system_type in [('OCTOBRE', 'mensuel'), ('TRIMESTRE_1', 'trimestre'),
                                             ('SEMESTRE_1', 'semestre')]]

    def _compteurs_notes(self):
        from notes.models import Classement, CompositionNote, MatiereNote, NoteEleve, NoteMensuelle, NoteSuivi
        return [m.objects.count() for m in (MatiereNote, NoteEleve, NoteMensuelle, CompositionNote, NoteSuivi,
                                            Classement)]

    def test_saisie_test_accueil_sans_effet_sur_moyennes_et_compositions(self):
        avant, compteurs_avant = self._moyennes_scolaires(), self._compteurs_notes()

        # Même élève, même classe, même matière, même année : le test d'accueil reste à part.
        url = f"{reverse('examen_accueil:matieres')}?classe={self.classe.pk}"
        self.client.post(url, {'reprendre_notes': '1'})
        francais = MatiereAccueil.objects.get(classe=self.classe, nom="Français")
        reponse = self.client.post(f"{reverse('examen_accueil:saisie_notes')}?classe={self.classe.pk}",
                                   {f'note_{self.eleve.pk}_{francais.pk}': '20'})
        self.assertEqual(reponse.status_code, 302)
        self.assertEqual(NoteAccueil.objects.get().note, Decimal('20'))
        self.client.get(f"{reverse('examen_accueil:consultation')}?classe={self.classe.pk}")
        self.client.get(reverse('examen_accueil:bulletins_pdf', args=[self.classe.pk]))

        self.assertEqual(self._moyennes_scolaires(), avant)
        self.assertEqual(self._compteurs_notes(), compteurs_avant)
        self.assertEqual(avant[0], 8.0)
        # Et réciproquement : la note mensuelle de 8 n'apparaît pas au test d'accueil.
        self.assertEqual(calculer_resultats(self.classe)[0]['moyenne'], Decimal('20.00'))

    def test_aucun_lien_entre_modeles_accueil_et_notes(self):
        for modele in apps.get_app_config('examen_accueil').get_models():
            for champ in modele._meta.get_fields():
                cible = getattr(champ, 'related_model', None)
                if cible is not None:
                    self.assertNotEqual(cible._meta.app_label, 'notes', f'{modele.__name__}.{champ.name}')
        for modele in apps.get_app_config('notes').get_models():
            for champ in modele._meta.get_fields():
                cible = getattr(champ, 'related_model', None)
                if cible is not None:
                    self.assertNotEqual(cible._meta.app_label, 'examen_accueil', f'{modele.__name__}.{champ.name}')


class SynchronisationTests(TestCase):
    def test_modeles_synchronises_et_rattaches_a_l_ecole(self):
        from synchronisation.registry import MODELES_REGISTRE_4, SYNC_MODEL_SET
        from synchronisation.scope import school_path

        for modele in apps.get_app_config('examen_accueil').get_models():
            label = f'examen_accueil.{modele.__name__}'
            self.assertIn(label, SYNC_MODEL_SET)
            self.assertIn(label, MODELES_REGISTRE_4)
            self.assertTrue(school_path(modele).endswith('ecole'), label)


@override_settings(MIDDLEWARE=MIDDLEWARE_SANS_LICENCE)
class ChatbotRetireTests(TestCase):
    def test_application_et_url_supprimees(self):
        self.assertFalse(apps.is_installed('chatbot'))
        user = creer_utilisateur('directeur_chatbot', creer_ecole('6'))
        self.client.force_login(user)
        self.assertEqual(self.client.get('/chatbot/').status_code, 404)
