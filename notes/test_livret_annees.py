"""Livret scolaire : chaque année affiche ses propres notes, dans sa propre classe."""
from datetime import date
from decimal import Decimal
from io import BytesIO, StringIO

from pypdf import PdfReader

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from eleves.models import Classe, Ecole, Eleve, HistoriqueEleve, Responsable
from eleves.utils_annee import SESSION_ANNEE_ACTIVE
from notes.livret_scolaire import _collecter_parcours_eleve
from notes.models import ClasseNote, Classement, CompositionNote, MatiereNote, NoteMensuelle
from utilisateurs.models import Profil

MIDDLEWARE_SANS_LICENCE = [m for m in settings.MIDDLEWARE if 'licence_middleware' not in m]

PASSEE = '2024-2025'
COURANTE = '2025-2026'
SEPTIEME = '7ÈME ANNÉE A'
HUITIEME = '8ÈME ANNÉE A'
HUITIEME_B = '8ÈME ANNÉE B'


@override_settings(MIDDLEWARE=MIDDLEWARE_SANS_LICENCE)
class LivretBase(TestCase):
    def setUp(self):
        self.ecole = Ecole.objects.create(
            nom='École Livret', adresse='Conakry', telephone='+224620700001', directeur='Direction',
        )
        self.responsable = Responsable.objects.create(
            prenom='Parent', nom='Livret', relation='PERE', telephone='+224620700002', adresse='Conakry',
        )
        self.classe_7, self.note_7, self.maths_7 = self._classe(SEPTIEME, PASSEE)
        self.classe_8, self.note_8, self.maths_8 = self._classe(HUITIEME, COURANTE)
        self.classe_8b, self.note_8b, self.maths_8b = self._classe(HUITIEME_B, COURANTE)
        self.aminata = self._eleve('LIV-001', 'Aminata', self.classe_7)
        self.binta = self._eleve('LIV-002', 'Binta', self.classe_7)

    def _classe(self, nom, annee, avec_notes=True):
        classe = Classe.objects.create(ecole=self.ecole, nom=nom, niveau='COLLEGE_7', annee_scolaire=annee)
        if not avec_notes:
            return classe, None, None
        classe_note = ClasseNote.objects.create(
            ecole=self.ecole, nom=classe.nom, niveau='COLLEGE_7', annee_scolaire=annee,
        )
        matiere = MatiereNote.objects.create(classe=classe_note, nom='Mathématiques', code='MATH', coefficient=2)
        return classe, classe_note, matiere

    def _eleve(self, matricule, prenom, classe):
        return Eleve.objects.create(
            matricule=matricule, prenom=prenom, nom='Camara', sexe='F', date_naissance=date(2012, 1, 1),
            classe=classe, date_inscription=date(2024, 9, 1),
            responsable_principal=self.responsable, statut='ACTIF',
        )

    def _noter(self, eleve, matiere, annee, valeur):
        NoteMensuelle.objects.create(eleve=eleve, matiere=matiere, mois='OCTOBRE',
                                     annee_scolaire=annee, note=Decimal(valeur))
        CompositionNote.objects.create(eleve=eleve, matiere=matiere, periode='SEMESTRE_1',
                                       annee_scolaire=annee, note=Decimal(valeur))

    def _promouvoir(self, eleve, classe):
        """Même effet que le passage à la nouvelle année : les notes ne bougent pas."""
        ancienne = eleve.classe.nom
        eleve.classe = classe
        eleve._passage_nouvelle_annee = True
        eleve.save()
        HistoriqueEleve.objects.create(
            eleve=eleve, action='CHANGEMENT_CLASSE',
            description=f"Passage nouvelle année {classe.annee_scolaire}: {ancienne} → {classe.nom}",
        )
        return Eleve.objects.get(pk=eleve.pk)

    def _parcours(self, eleve):
        eleve = Eleve.objects.get(pk=eleve.pk)
        return {p['annee_scolaire']: p for p in _collecter_parcours_eleve(eleve, self.ecole)}


class ParcoursParAnneeTest(LivretBase):
    def test_chaque_annee_affiche_ses_notes_et_sa_classe(self):
        self._noter(self.aminata, self.maths_7, PASSEE, '12')
        aminata = self._promouvoir(self.aminata, self.classe_8)
        self._noter(aminata, self.maths_8, COURANTE, '16')

        parcours = self._parcours(aminata)

        self.assertEqual(list(parcours), [PASSEE, COURANTE])
        self.assertEqual(parcours[PASSEE]['classe_nom'], self.classe_7.nom)
        self.assertEqual(parcours[PASSEE]['matieres_data'][0]['sem1_compo'], 12)
        self.assertEqual(parcours[COURANTE]['classe_nom'], self.classe_8.nom)
        self.assertEqual(parcours[COURANTE]['matieres_data'][0]['sem1_compo'], 16)

    def test_un_transfert_en_cours_d_annee_laisse_les_annees_passees_en_place(self):
        self._noter(self.aminata, self.maths_7, PASSEE, '12')
        Classement.objects.create(eleve=self.aminata, classe=self.note_7, periode='ANNUEL_SEM',
                                  annee_scolaire=PASSEE, moyenne_generale=Decimal('12'),
                                  total_points=Decimal('24'), total_coefficients=Decimal('2'),
                                  rang=1, rang_formate='1er/2', effectif=2)
        aminata = self._promouvoir(self.aminata, self.classe_8)
        self._noter(aminata, self.maths_8, COURANTE, '16')

        aminata.classe = self.classe_8b
        aminata.save()

        ancienne = NoteMensuelle.objects.get(eleve=aminata, annee_scolaire=PASSEE)
        recente = NoteMensuelle.objects.get(eleve=aminata, annee_scolaire=COURANTE)
        self.assertEqual(ancienne.matiere, self.maths_7)
        self.assertEqual(recente.matiere, self.maths_8b)
        self.assertTrue(Classement.objects.filter(eleve=aminata, annee_scolaire=PASSEE).exists())

        parcours = self._parcours(aminata)
        self.assertEqual(parcours[PASSEE]['classe_nom'], self.classe_7.nom)
        self.assertEqual(parcours[PASSEE]['matieres_data'][0]['sem1_compo'], 12)
        self.assertEqual(parcours[COURANTE]['classe_nom'], self.classe_8b.nom)
        self.assertEqual(parcours[COURANTE]['matieres_data'][0]['sem1_compo'], 16)

    def test_le_transfert_ne_range_pas_les_notes_dans_une_classe_d_une_autre_annee(self):
        # La classe de destination n'a pas de classe de notes cette année-là,
        # mais une classe du même nom en a une l'année précédente.
        homonyme = ClasseNote.objects.create(ecole=self.ecole, nom='8ÈME ANNÉE C',
                                             niveau='COLLEGE_7', annee_scolaire=PASSEE)
        MatiereNote.objects.create(classe=homonyme, nom='Mathématiques', code='MATH', coefficient=2)
        classe_c = Classe.objects.create(ecole=self.ecole, nom='8ÈME ANNÉE C',
                                         niveau='COLLEGE_8', annee_scolaire=COURANTE)
        ClasseNote.objects.filter(ecole=self.ecole, annee_scolaire=COURANTE).update(niveau='COLLEGE_9')
        aminata = self._promouvoir(self.aminata, self.classe_8)
        self._noter(aminata, self.maths_8, COURANTE, '16')

        aminata.classe = classe_c
        aminata.save()

        note = NoteMensuelle.objects.get(eleve=aminata)
        self.assertEqual(note.matiere.classe.annee_scolaire, note.annee_scolaire)

    def test_une_note_mal_rattachee_n_affiche_pas_les_notes_d_une_autre_annee(self):
        # Donnée abîmée par l'ancien transfert : note de 2024-2025 sur une matière de 2025-2026.
        NoteMensuelle.objects.create(eleve=self.aminata, matiere=self.maths_8, mois='NOVEMBRE',
                                     annee_scolaire=PASSEE, note=Decimal('9'))
        aminata = self._promouvoir(self.aminata, self.classe_8)
        self._noter(aminata, self.maths_8, COURANTE, '16')

        parcours = self._parcours(aminata)

        self.assertEqual(parcours[PASSEE]['classe_nom'], self.classe_7.nom)
        self.assertIsNone(parcours[PASSEE]['matieres_data'][0]['sem1_compo'])
        self.assertEqual(parcours[COURANTE]['matieres_data'][0]['sem1_compo'], 16)

    def test_une_classe_sans_classe_de_notes_ne_prend_pas_celle_d_une_autre_classe(self):
        classe_9, _, _ = self._classe('9ÈME ANNÉE A', COURANTE, avec_notes=False)
        fatou = self._eleve('LIV-003', 'Fatou', classe_9)
        self._noter(self.binta, self.maths_8, COURANTE, '15')

        parcours = self._parcours(fatou)

        self.assertEqual(parcours[COURANTE]['matieres_data'], [])
        self.assertEqual(parcours[COURANTE]['effectif_classe'], 0)

    def test_une_classe_saisie_avec_une_autre_casse_est_retrouvee(self):
        classe = Classe.objects.create(ecole=self.ecole, nom='9ÈME ANNÉE A',
                                       niveau='COLLEGE_9', annee_scolaire=COURANTE)
        classe_note = ClasseNote.objects.create(ecole=self.ecole, nom='9ème Année (A)',
                                                niveau='COLLEGE_9', annee_scolaire=COURANTE)
        MatiereNote.objects.create(classe=classe_note, nom='Français', code='FR', coefficient=1)
        fatou = self._eleve('LIV-003', 'Fatou', classe)

        self.assertEqual(self._parcours(fatou)[COURANTE]['matieres_data'][0]['nom'], 'Français')

    def test_le_rang_d_une_annee_passee_compte_les_eleves_deja_promus(self):
        self._noter(self.aminata, self.maths_7, PASSEE, '14')
        self._noter(self.binta, self.maths_7, PASSEE, '10')
        aminata = self._promouvoir(self.aminata, self.classe_8)
        self._promouvoir(self.binta, self.classe_8)

        passee = self._parcours(aminata)[PASSEE]

        self.assertEqual(passee['rang'], '1eme/2')
        self.assertEqual(passee['effectif_classe'], 2)
        self.assertIsNotNone(passee['moy_classe'])

    def test_l_annulation_d_une_annee_ne_renomme_pas_la_classe_precedente(self):
        classe_7_bis, _, _ = self._classe(SEPTIEME, COURANTE)
        self._noter(self.aminata, self.maths_7, PASSEE, '8')
        HistoriqueEleve.objects.create(
            eleve=self.aminata, action='CHANGEMENT_CLASSE',
            description=f"Annulation de l'année {COURANTE}: {self.classe_8.nom} → retour en "
                        f"{self.classe_7.nom} ({PASSEE}). Nouveau matricule: X",
        )
        self.aminata.classe = classe_7_bis
        self.aminata._passage_nouvelle_annee = True
        self.aminata.save()
        HistoriqueEleve.objects.create(
            eleve=self.aminata, action='CHANGEMENT_CLASSE',
            description=f"Redoublant — année {COURANTE}: maintenu(e) en {self.classe_7.nom} (moyenne: 8)",
        )

        parcours = self._parcours(self.aminata)

        self.assertEqual(parcours[PASSEE]['classe_nom'], self.classe_7.nom)
        self.assertEqual(parcours[COURANTE]['classe_nom'], classe_7_bis.nom)

    def test_un_redoublant_sans_note_garde_son_annee_passee(self):
        classe_7_bis, _, _ = self._classe(SEPTIEME, COURANTE)
        self.aminata.classe = classe_7_bis
        self.aminata._passage_nouvelle_annee = True
        self.aminata.save()
        HistoriqueEleve.objects.create(
            eleve=self.aminata, action='CHANGEMENT_CLASSE',
            description=f"Redoublant — année {COURANTE}: maintenu(e) en {self.classe_7.nom} (moyenne: 8)",
        )

        self.assertEqual(self._parcours(self.aminata)[PASSEE]['classe_nom'], self.classe_7.nom)

    def test_un_matricule_contenant_une_annee_ne_cree_pas_d_annee_fantome(self):
        aminata = self._promouvoir(self.aminata, self.classe_8)
        HistoriqueEleve.objects.filter(eleve=aminata).delete()
        HistoriqueEleve.objects.create(
            eleve=aminata, action='CHANGEMENT_CLASSE',
            description=f"Changement de classe: {self.classe_8b.nom} → {self.classe_8.nom}. "
                        f"Ancien matricule: 8B-2022-2023-001, Nouveau matricule: 8A-001",
        )

        self.assertEqual(list(self._parcours(aminata)), [COURANTE])

    def test_pas_de_decision_de_passage_pour_une_annee_en_cours(self):
        en_cours = '2099-2100'
        classe, _, matiere = self._classe(HUITIEME, en_cours)
        fatou = self._eleve('LIV-003', 'Fatou', classe)
        self._noter(fatou, matiere, en_cours, '4')

        entree = self._parcours(fatou)[en_cours]

        self.assertEqual(entree['decision_txt'], '')
        self.assertEqual(entree['passe_en'], '')

    def test_decision_de_passage_conservee_pour_une_annee_terminee(self):
        self._noter(self.aminata, self.maths_7, PASSEE, '4')
        aminata = self._promouvoir(self.aminata, self.classe_8)

        self.assertEqual(self._parcours(aminata)[PASSEE]['decision_txt'], 'Redouble la classe')


class SelectionParAnneeTest(LivretBase):
    def setUp(self):
        super().setUp()
        self._noter(self.aminata, self.maths_7, PASSEE, '12')
        self.aminata = self._promouvoir(self.aminata, self.classe_8)
        self._noter(self.aminata, self.maths_8, COURANTE, '16')
        User = get_user_model()
        user = User.objects.create_user('directeur-livret', 'd@l.gn', 'x')
        Profil.objects.filter(user=user).update(
            role='DIRECTEUR', ecole=self.ecole, is_validated=True, actif=True,
        )
        self.client.force_login(User.objects.get(pk=user.pk))

    def _choisir_annee(self, annee):
        session = self.client.session
        session[SESSION_ANNEE_ACTIVE] = annee
        session.save()

    def test_une_classe_d_une_annee_passee_liste_ses_anciens_eleves(self):
        self._choisir_annee(PASSEE)

        reponse = self.client.get(reverse('notes:livret_scolaire'), {'classe_id': self.classe_7.pk})

        self.assertIn(self.aminata, list(reponse.context['eleves']))
        lien = reverse('notes:livret_scolaire_annuel_pdf', args=[self.aminata.pk])
        self.assertContains(reponse, f'{lien}?annee={PASSEE}')

    def test_annee_courante_liste_seulement_les_eleves_actifs_de_la_classe(self):
        self.binta.classe = self.classe_8
        self.binta.statut = 'TRANSFERE'
        self.binta._passage_nouvelle_annee = True
        self.binta.save()

        reponse = self.client.get(reverse('notes:livret_scolaire'), {'classe_id': self.classe_8.pk})

        self.assertEqual(list(reponse.context['eleves']), [self.aminata])

    def test_le_livret_annuel_d_une_annee_passee_se_genere(self):
        lien = reverse('notes:livret_scolaire_annuel_pdf', args=[self.aminata.pk])

        reponse = self.client.get(lien, {'annee': PASSEE})

        self.assertEqual(reponse['Content-Type'], 'application/pdf')
        self.assertIn(PASSEE, reponse['Content-Disposition'])
        texte = self._texte_pdf(reponse)
        self.assertIn(f'Année scolaire : {PASSEE}', texte)
        self.assertIn(self.classe_7.nom, texte)
        self.assertIn('12.00', texte)
        self.assertNotIn(COURANTE, texte)
        self.assertNotIn('16.00', texte)

    def _texte_pdf(self, reponse):
        lecteur = PdfReader(BytesIO(reponse.content))
        return '\n'.join(page.extract_text() for page in lecteur.pages)

    def test_le_livret_complet_se_genere_sur_plusieurs_annees(self):
        reponse = self.client.get(reverse('notes:livret_scolaire_pdf', args=[self.aminata.pk]))

        self.assertEqual(reponse['Content-Type'], 'application/pdf')

    def test_le_livret_de_classe_d_une_annee_passee_contient_ses_anciens_eleves(self):
        reponse = self.client.get(reverse('notes:livret_scolaire_classe_pdf', args=[self.classe_7.pk]))

        self.assertEqual(reponse['Content-Type'], 'application/pdf')


class ReparationAnneesNotesTest(LivretBase):
    def setUp(self):
        super().setUp()
        self._noter(self.aminata, self.maths_7, PASSEE, '12')
        self.aminata = self._promouvoir(self.aminata, self.classe_8)
        # Reproduit l'ancien transfert : la note de 2024-2025 est partie sur une matière de 2025-2026.
        NoteMensuelle.objects.filter(eleve=self.aminata).update(matiere=self.maths_8b)
        CompositionNote.objects.filter(eleve=self.aminata).update(matiere=self.maths_8b)

    def _lancer(self, *args):
        sortie = StringIO()
        call_command('reparer_annees_notes', *args, stdout=sortie)
        return sortie.getvalue()

    def test_sans_option_rien_n_est_modifie(self):
        sortie = self._lancer()

        self.assertIn('2 note(s) réparable(s)', sortie)
        self.assertEqual(NoteMensuelle.objects.get(eleve=self.aminata).matiere, self.maths_8b)

    def test_appliquer_ramene_la_note_dans_la_classe_de_son_annee(self):
        self._lancer('--appliquer')

        self.assertEqual(NoteMensuelle.objects.get(eleve=self.aminata).matiere, self.maths_7)
        self.assertEqual(CompositionNote.objects.get(eleve=self.aminata).matiere, self.maths_7)
        self.assertEqual(self._parcours(self.aminata)[PASSEE]['matieres_data'][0]['sem1_compo'], 12)

    def test_une_note_deja_presente_a_destination_n_est_pas_ecrasee(self):
        existante = NoteMensuelle.objects.create(eleve=self.aminata, matiere=self.maths_7, mois='OCTOBRE',
                                                 annee_scolaire=PASSEE, note=Decimal('11'))

        sortie = self._lancer('--appliquer')

        self.assertIn('1 note(s) à vérifier', sortie)
        existante.refresh_from_db()
        self.assertEqual(existante.note, Decimal('11'))
        self.assertEqual(NoteMensuelle.objects.exclude(pk=existante.pk).get(eleve=self.aminata).matiere,
                         self.maths_8b)

    def test_sans_classe_connue_pour_l_annee_la_note_reste_a_verifier(self):
        HistoriqueEleve.objects.filter(eleve=self.aminata).delete()

        sortie = self._lancer('--appliquer')

        self.assertIn('2 note(s) à vérifier', sortie)
        self.assertEqual(NoteMensuelle.objects.get(eleve=self.aminata).matiere, self.maths_8b)
