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
from utilisateurs.models import Profil

from .calculs import calculer_resultats, statistiques
from .models import CandidatAccueil, ClasseAccueil, MatiereAccueil, NoteAccueil

MIDDLEWARE_SANS_LICENCE = [m for m in settings.MIDDLEWARE if 'licence_middleware' not in m]


def creer_ecole(suffixe):
    return Ecole.objects.create(nom=f"École Accueil {suffixe}", adresse="Conakry",
                                telephone=f"+22462200000{suffixe}", directeur="Direction")


def creer_utilisateur(nom, ecole, role='ADMIN'):
    user = User.objects.create_user(nom, f'{nom}@test.gn', 'x')
    Profil.objects.filter(user=user).update(ecole=ecole, role=role, actif=True, is_validated=True)
    return User.objects.get(pk=user.pk)


def creer_classe_complete(ecole, notes):
    """Classe de test avec Français (coef 2) et Maths (coef 1).

    ``notes`` : {(nom, prenom, sexe): (note_francais, note_maths)} ; None = non noté.
    """
    classe = ClasseAccueil.objects.create(ecole=ecole, nom="Test 7ème", annee_scolaire="2026-2027",
                                          note_sur=20, moyenne_admission=Decimal('10'))
    francais = MatiereAccueil.objects.create(classe=classe, nom="Français", coefficient=Decimal('2'), ordre=1)
    maths = MatiereAccueil.objects.create(classe=classe, nom="Mathématiques", coefficient=Decimal('1'), ordre=2)
    for (nom, prenom, sexe), valeurs in notes.items():
        candidat = CandidatAccueil.objects.create(classe=classe, nom=nom, prenom=prenom, sexe=sexe)
        for matiere, valeur in zip((francais, maths), valeurs):
            if valeur is not None:
                NoteAccueil.objects.create(candidat=candidat, matiere=matiere, note=Decimal(str(valeur)))
    return classe, francais, maths


class CalculsTests(TestCase):
    def setUp(self):
        self.ecole = creer_ecole('1')

    def test_moyenne_ponderee_rang_et_admission(self):
        classe, _, _ = creer_classe_complete(self.ecole, {
            ('BAH', 'Aminata', 'F'): (14, 8),       # (28 + 8) / 3 = 12
            ('DIALLO', 'Ibrahima', 'M'): (9, 15),   # (18 + 15) / 3 = 11
            ('CAMARA', 'Mariama', 'F'): (8, 8),     # 8
        })
        resultats = calculer_resultats(classe)
        self.assertEqual([r['candidat'].nom for r in resultats], ['BAH', 'DIALLO', 'CAMARA'])
        self.assertEqual([r['moyenne'] for r in resultats], [Decimal('12.00'), Decimal('11.00'), Decimal('8.00')])
        self.assertEqual([r['rang_texte'] for r in resultats], ['1ère', '2ème', '3ème'])
        self.assertEqual([r['admis'] for r in resultats], [True, True, False])
        stats = statistiques(classe, resultats)
        self.assertEqual((stats['admis'], stats['non_admis'], stats['moyenne_classe']), (2, 1, Decimal('10.33')))

    def test_matiere_non_notee_compte_zero_et_ex_aequo(self):
        classe, _, _ = creer_classe_complete(self.ecole, {
            ('SOW', 'Alpha', 'M'): (15, None),      # 30 / 3 = 10 : l'absence aux maths compte 0
            ('BARRY', 'Oumar', 'M'): (10, 10),      # 10
            ('KANTE', 'Fanta', 'F'): (None, None),  # 0
        })
        resultats = {r['candidat'].nom: r for r in calculer_resultats(classe)}
        self.assertEqual(resultats['SOW']['moyenne'], Decimal('10.00'))
        self.assertEqual(resultats['SOW']['rang_texte'], '1er ex')
        self.assertEqual(resultats['BARRY']['rang_texte'], '1er ex')
        self.assertEqual(resultats['KANTE']['moyenne'], Decimal('0.00'))
        self.assertEqual(resultats['KANTE']['rang'], 3)
        self.assertFalse(resultats['KANTE']['admis'])


@override_settings(MIDDLEWARE=MIDDLEWARE_SANS_LICENCE)
class ParcoursCompletTests(TestCase):
    """Classe → matières → candidats → notes → résultats → exports → bulletins."""

    def setUp(self):
        self.ecole = creer_ecole('2')
        self.user = creer_utilisateur('directeur_accueil', self.ecole)
        self.client.force_login(self.user)

    def test_parcours_de_bout_en_bout(self):
        reponse = self.client.post(reverse('examen_accueil:liste'), {
            'nom': 'Test 11ème', 'niveau_vise': '11ème Sciences', 'annee_scolaire': '2026-2027',
            'date_test': '2026-09-20', 'note_sur': '20', 'moyenne_admission': '10', 'observations': '',
        })
        classe = ClasseAccueil.objects.get()
        self.assertRedirects(reponse, reverse('examen_accueil:detail', args=[classe.pk]))
        self.assertEqual(classe.ecole, self.ecole)
        self.assertEqual(classe.cree_par, self.user)

        url_detail = reverse('examen_accueil:detail', args=[classe.pk])
        for nom, coefficient in [('Français', '3'), ('Physique', '2')]:
            self.client.post(url_detail, {'ajouter_matiere': '1', 'matiere-nom': nom,
                                          'matiere-coefficient': coefficient, 'matiere-ordre': '1'})
        for nom, prenom, sexe in [('Conde', 'Saran', 'F'), ('Toure', 'Moussa', 'M')]:
            self.client.post(url_detail, {'ajouter_candidat': '1', 'candidat-nom': nom,
                                          'candidat-prenom': prenom, 'candidat-sexe': sexe})
        self.assertEqual(classe.matieres.count(), 2)
        self.assertEqual(list(classe.candidats.values_list('nom', flat=True)), ['CONDE', 'TOURE'])

        francais = classe.matieres.get(nom='Français')
        physique = classe.matieres.get(nom='Physique')
        saran = classe.candidats.get(nom='CONDE')
        moussa = classe.candidats.get(nom='TOURE')
        self.assertContains(self.client.get(url_detail), 'CONDE Saran')
        ligne = self.client.get(reverse('examen_accueil:liste')).context['lignes'][0]
        self.assertEqual((ligne['nb_matieres'], ligne['nb_candidats']), (2, 2))
        for nom, pk in [('modifier_classe', classe.pk), ('modifier_matiere', francais.pk),
                        ('modifier_candidat', saran.pk), ('saisie_notes', classe.pk)]:
            self.assertEqual(self.client.get(reverse(f'examen_accueil:{nom}', args=[pk])).status_code, 200, nom)
        reponse = self.client.post(reverse('examen_accueil:saisie_notes', args=[classe.pk]), {
            f'note_{saran.pk}_{francais.pk}': '15,5', f'note_{saran.pk}_{physique.pk}': '12',
            f'note_{moussa.pk}_{francais.pk}': '7', f'note_{moussa.pk}_{physique.pk}': '',
        })
        self.assertRedirects(reponse, reverse('examen_accueil:saisie_notes', args=[classe.pk]))
        self.assertEqual(NoteAccueil.objects.get(candidat=saran, matiere=francais).note, Decimal('15.50'))
        self.assertEqual(NoteAccueil.objects.count(), 3)

        reponse = self.client.get(reverse('examen_accueil:resultats', args=[classe.pk]))
        self.assertEqual(reponse.status_code, 200)
        lignes = reponse.context['lignes']
        self.assertEqual(lignes[0]['candidat'], saran)
        self.assertEqual(lignes[0]['moyenne'], Decimal('14.10'))   # (46,5 + 24) / 5
        self.assertEqual(lignes[1]['moyenne'], Decimal('4.20'))    # 21 / 5, physique non notée = 0
        self.assertContains(reponse, 'Admis')

        pdf = self.client.get(reverse('examen_accueil:resultats_pdf', args=[classe.pk]))
        self.assertEqual(pdf['Content-Type'], 'application/pdf')
        self.assertTrue(pdf.content.startswith(b'%PDF'))

        excel = self.client.get(reverse('examen_accueil:resultats_excel', args=[classe.pk]))
        self.assertIn('spreadsheetml', excel['Content-Type'])
        from openpyxl import load_workbook
        feuille = load_workbook(io.BytesIO(excel.content)).active
        self.assertEqual([feuille.cell(row=6, column=c).value for c in (1, 2, 3, 4, 5, 7)],
                         ['1ère', 'CONDE', 'Saran', 15.5, 12, 14.1])
        self.assertEqual(feuille.cell(row=6, column=9).value, 'ADMISE')
        self.assertEqual(feuille.cell(row=7, column=9).value, 'NON ADMIS')

        bulletins = self.client.get(reverse('examen_accueil:bulletins_pdf', args=[classe.pk]))
        self.assertTrue(bulletins.content.startswith(b'%PDF'))
        bulletin = self.client.get(reverse('examen_accueil:bulletin_candidat', args=[moussa.pk]))
        self.assertTrue(bulletin.content.startswith(b'%PDF'))
        self.assertIn('bulletin_test_accueil_moussa-toure.pdf', bulletin['Content-Disposition'])

    def test_note_hors_bareme_n_enregistre_rien(self):
        classe, francais, maths = creer_classe_complete(self.ecole, {('BAH', 'Awa', 'F'): (12, None)})
        candidat = classe.candidats.get()
        reponse = self.client.post(reverse('examen_accueil:saisie_notes', args=[classe.pk]), {
            f'note_{candidat.pk}_{francais.pk}': '18', f'note_{candidat.pk}_{maths.pk}': '25',
        })
        self.assertEqual(reponse.status_code, 200)
        self.assertContains(reponse, 'hors du barème')
        self.assertEqual(NoteAccueil.objects.get(candidat=candidat, matiere=francais).note, Decimal('12'))
        self.assertFalse(NoteAccueil.objects.filter(matiere=maths).exists())

    def test_case_videe_efface_la_note(self):
        classe, francais, maths = creer_classe_complete(self.ecole, {('BAH', 'Awa', 'F'): (12, 9)})
        candidat = classe.candidats.get()
        self.client.post(reverse('examen_accueil:saisie_notes', args=[classe.pk]), {
            f'note_{candidat.pk}_{francais.pk}': '12', f'note_{candidat.pk}_{maths.pk}': ' ',
        })
        self.assertEqual(list(NoteAccueil.objects.values_list('matiere__nom', flat=True)), ['Français'])

    def test_moyenne_admission_superieure_au_bareme_refusee(self):
        reponse = self.client.post(reverse('examen_accueil:liste'), {
            'nom': 'Test CP', 'annee_scolaire': '2026-2027', 'note_sur': '10', 'moyenne_admission': '12',
        })
        self.assertEqual(reponse.status_code, 200)
        self.assertFalse(ClasseAccueil.objects.exists())

    def test_menu_propose_le_test_d_accueil(self):
        reponse = self.client.get(reverse('examen_accueil:liste'))
        self.assertContains(reponse, reverse('examen_accueil:liste'))
        self.assertContains(reponse, "fa-clipboard-check me-1\"></i>Test d'accueil")
        self.assertNotContains(reponse, 'Chatbot')


@override_settings(MIDDLEWARE=MIDDLEWARE_SANS_LICENCE)
class IsolementEcoleTests(TestCase):
    def setUp(self):
        self.ecole_a, self.ecole_b = creer_ecole('3'), creer_ecole('4')
        self.classe_a, self.francais_a, _ = creer_classe_complete(self.ecole_a, {('BAH', 'Awa', 'F'): (12, 9)})
        self.client.force_login(creer_utilisateur('directeur_b', self.ecole_b))

    def test_autre_ecole_invisible_et_inaccessible(self):
        reponse = self.client.get(reverse('examen_accueil:liste'))
        self.assertEqual(reponse.context['lignes'], [])
        candidat = self.classe_a.candidats.get()
        for nom, args in [('detail', [self.classe_a.pk]), ('saisie_notes', [self.classe_a.pk]),
                          ('resultats', [self.classe_a.pk]), ('resultats_pdf', [self.classe_a.pk]),
                          ('resultats_excel', [self.classe_a.pk]), ('bulletins_pdf', [self.classe_a.pk]),
                          ('modifier_matiere', [self.francais_a.pk]), ('bulletin_candidat', [candidat.pk])]:
            self.assertEqual(self.client.get(reverse(f'examen_accueil:{nom}', args=args)).status_code, 404, nom)
        self.assertEqual(self.client.post(reverse('examen_accueil:supprimer_classe', args=[self.classe_a.pk])).status_code, 404)
        self.assertTrue(ClasseAccueil.objects.filter(pk=self.classe_a.pk).exists())

    def test_utilisateur_sans_droit_notes_refuse(self):
        surveillant = creer_utilisateur('surveillant_a', self.ecole_a, role='SURVEILLANT')
        Profil.objects.filter(user=surveillant).update(peut_gerer_notes=False)
        self.client.force_login(surveillant)
        self.assertEqual(self.client.get(reverse('examen_accueil:liste')).status_code, 403)


@override_settings(MIDDLEWARE=MIDDLEWARE_SANS_LICENCE)
class IndependanceNotesScolairesTests(TestCase):
    """Les notes du test d'accueil ne doivent JAMAIS entrer dans les moyennes scolaires."""

    def setUp(self):
        from notes.models import ClasseNote, MatiereNote, NoteMensuelle

        self.ecole = creer_ecole('5')
        self.client.force_login(creer_utilisateur('directeur_notes', self.ecole))
        classe = Classe.objects.create(ecole=self.ecole, nom="10ème A", niveau='COLLEGE_10', annee_scolaire="2026-2027")
        self.eleve = Eleve.objects.create(matricule='ACC-001', prenom='Aissatou', nom='Bah', sexe='F',
                                          date_naissance=date(2012, 1, 5), classe=classe, statut='ACTIF')
        self.classe_note = ClasseNote.objects.create(ecole=self.ecole, nom="10ème A", niveau='COLLEGE_10',
                                                     annee_scolaire="2026-2027")
        self.matiere_note = MatiereNote.objects.create(classe=self.classe_note, nom="Français", code="FR",
                                                       coefficient=Decimal('2'))
        NoteMensuelle.objects.create(eleve=self.eleve, matiere=self.matiere_note, mois='OCTOBRE',
                                     annee_scolaire="2026-2027", note=Decimal('8'))

    def _moyennes_scolaires(self):
        from notes.calculs_moyennes import calculer_moyenne_generale_eleve
        from notes.models import MatiereNote

        matieres = MatiereNote.objects.filter(classe=self.classe_note)
        return [calculer_moyenne_generale_eleve(self.eleve, matieres, periode, system_type)['moyenne_generale']
                for periode, system_type in [('OCTOBRE', 'mensuel'), ('TRIMESTRE_1', 'trimestre'),
                                             ('SEMESTRE_1', 'semestre')]]

    def _compteurs_notes(self):
        from notes.models import Classement, CompositionNote, NoteEleve, NoteMensuelle, NoteSuivi
        return [m.objects.count() for m in (NoteEleve, NoteMensuelle, CompositionNote, NoteSuivi, Classement)]

    def test_saisie_test_accueil_sans_effet_sur_moyennes_et_compositions(self):
        avant, compteurs_avant = self._moyennes_scolaires(), self._compteurs_notes()

        # Même élève, même matière, même année : le test d'accueil reste à part.
        classe = ClasseAccueil.objects.create(ecole=self.ecole, nom="10ème A", annee_scolaire="2026-2027")
        francais = MatiereAccueil.objects.create(classe=classe, nom="Français", coefficient=Decimal('2'))
        candidat = CandidatAccueil.objects.create(classe=classe, nom="BAH", prenom="Aissatou", sexe='F')
        reponse = self.client.post(reverse('examen_accueil:saisie_notes', args=[classe.pk]),
                                   {f'note_{candidat.pk}_{francais.pk}': '20'})
        self.assertEqual(reponse.status_code, 302)
        self.assertEqual(NoteAccueil.objects.get().note, Decimal('20'))
        self.client.get(reverse('examen_accueil:resultats', args=[classe.pk]))
        self.client.get(reverse('examen_accueil:bulletins_pdf', args=[classe.pk]))

        self.assertEqual(self._moyennes_scolaires(), avant)
        self.assertEqual(self._compteurs_notes(), compteurs_avant)
        self.assertEqual(avant[0], 8.0)

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
