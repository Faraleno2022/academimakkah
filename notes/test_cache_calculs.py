"""Clés compatibles memcached et fraîcheur des calculs après invalidation."""
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch
import warnings

from django.core.cache import cache
from django.core.cache.backends.base import CacheKeyWarning
from django.test import SimpleTestCase, TestCase

from eleves.models import Classe, Ecole, Eleve
from .calculs_moyennes import calculer_classement_classe, calculer_moyennes_classe_optimise
from .models import ClasseNote, MatiereNote, NoteMensuelle
from .utils_rangs import calculer_rangs_classe_periode, invalider_cache_rangs


class CacheInvalidationTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.classe = SimpleNamespace(id=81)

    def test_invalidation_ne_genere_aucun_avertissement_pour_les_periodes_du_journal(self):
        with warnings.catch_warnings(record=True) as capture:
            warnings.simplefilter('always', CacheKeyWarning)
            invalider_cache_rangs(self.classe)
        alertes = [w for w in capture if issubclass(w.category, CacheKeyWarning)]
        self.assertEqual(len(alertes), 0)

    def test_invalidation_reste_constante_sans_balayage_de_toutes_les_periodes(self):
        with patch.object(cache, 'get', wraps=cache.get) as lire, \
             patch.object(cache, 'set', wraps=cache.set) as ecrire, \
             patch.object(cache, 'delete', wraps=cache.delete) as supprimer:
            invalider_cache_rangs(self.classe)
        self.assertLessEqual(lire.call_count + ecrire.call_count + supprimer.call_count, 3)

    def test_cles_sont_courtes_ascii_et_distinguent_les_periodes(self):
        from .cache_calculs import cle_cache_calcul
        cles = set()
        for periode in ('1er Trimestre', '1er_Trimestre', '2ème Semestre',
                        'OCTOBRE', 'période avec\tcontrôle\n' * 1000):
            cle = cle_cache_calcul('classement', 81, periode=periode, systeme='trimestriel')
            cles.add(cle)
            self.assertTrue(cle.isascii())
            self.assertLess(len(cle), 250)
            with warnings.catch_warnings():
                warnings.simplefilter('error', CacheKeyWarning)
                cache.set(cle, 'OK')
                self.assertEqual(cache.get(cle), 'OK')
        self.assertEqual(len(cles), 5)

    def test_cle_separe_classe_generation_revision_et_selection(self):
        from .cache_calculs import cle_cache_calcul
        params = dict(periode='OCTOBRE', systeme='mensuel', version='v1',
                      revision='r1', selection='eleves_1_2')
        cles = {cle_cache_calcul('moyennes', 81, **params)}
        cles.add(cle_cache_calcul('moyennes', 82, **params))
        for champ, valeur in [('periode', 'NOVEMBRE'), ('systeme', 'trimestre'),
                              ('version', 'v2'), ('revision', 'r2'), ('selection', 'eleve_1')]:
            cles.add(cle_cache_calcul('moyennes', 81, **{**params, champ: valeur}))
        self.assertEqual(len(cles), 7)

    def test_generation_ne_reutilise_pas_un_ancien_cache_apres_eviction(self):
        from .cache_calculs import version_cache_classe
        avant = version_cache_classe(self.classe.id)
        cache.delete(f'moy_version_classe_{self.classe.id}')
        apres = version_cache_classe(self.classe.id)
        self.assertNotEqual(avant, apres)
        invalider_cache_rangs(self.classe, 'OCTOBRE')
        self.assertNotEqual(apres, version_cache_classe(self.classe.id))


class CacheCalculsNotesTests(TestCase):
    def setUp(self):
        cache.clear()
        self.ecole = Ecole.objects.create(nom='École cache', adresse='Conakry',
            telephone='600222333', directeur='Direction')
        self.classe = Classe.objects.create(ecole=self.ecole, nom='1ÈRE ANNÉE',
            niveau='PRIMAIRE_1', annee_scolaire='2026-2027')
        self.classe_note = ClasseNote.objects.create(ecole=self.ecole, nom=self.classe.nom,
            niveau=self.classe.niveau, niveau_enseignement='PRIMAIRE', annee_scolaire='2026-2027')
        self.matiere = MatiereNote.objects.create(classe=self.classe_note, nom='Mathématiques',
            code='MAT', coefficient=1)
        self.eleve = Eleve.objects.create(classe=self.classe, matricule='CACHE-001', nom='Camara', prenom='Awa')
        self.autre = Eleve.objects.create(classe=self.classe, matricule='CACHE-002', nom='Bah', prenom='Ali')
        self.note = NoteMensuelle.objects.create(eleve=self.eleve, matiere=self.matiere, mois='OCTOBRE',
            annee_scolaire='2026-2027', note=Decimal('4'))
        NoteMensuelle.objects.create(eleve=self.autre, matiere=self.matiere, mois='OCTOBRE',
            annee_scolaire='2026-2027', note=Decimal('8'))
        self.eleves = [self.eleve, self.autre]
        self.matieres = [self.matiere]

    def calculer(self, periode='OCTOBRE', use_cache=True):
        systeme = 'mensuel' if periode == 'OCTOBRE' else 'trimestre'
        return (
            calculer_moyennes_classe_optimise(self.eleves, self.matieres, periode, systeme, use_cache=use_cache),
            calculer_classement_classe(self.eleves, self.matieres, periode, systeme, use_cache=use_cache),
            calculer_rangs_classe_periode(self.classe_note, periode, use_cache=use_cache),
        )

    def test_libelles_du_journal_passent_la_validation_des_cles_memcached(self):
        with warnings.catch_warnings():
            warnings.simplefilter('error', CacheKeyWarning)
            for periode in ('1er Trimestre', '2ème Trimestre', '3ème Trimestre'):
                self.assertEqual(self.calculer(periode), self.calculer(periode))
                self.assertEqual(self.calculer(periode), self.calculer(periode, use_cache=False))
            invalider_cache_rangs(self.classe_note)

    def test_modification_note_actualise_moyennes_classement_et_rangs(self):
        avant = self.calculer()
        self.assertEqual(avant[2][self.eleve.pk]['rang_num'], 2)
        self.note.note = Decimal('9')
        self.note.save()
        apres = self.calculer()
        self.assertNotEqual(avant, apres)
        self.assertEqual(apres[2][self.eleve.pk]['rang_num'], 1)
        self.assertEqual(apres, self.calculer(use_cache=False))

    def test_invalidation_apres_import_en_bloc_actualise_aussi_le_trimestre(self):
        avant = self.calculer('TRIMESTRE_1')
        NoteMensuelle.objects.filter(pk=self.note.pk).update(note=Decimal('9'))
        invalider_cache_rangs(self.classe_note, 'OCTOBRE')
        apres = self.calculer('TRIMESTRE_1')
        self.assertNotEqual(avant, apres)
        self.assertEqual(apres, self.calculer('TRIMESTRE_1', use_cache=False))

    def test_cache_d_un_sous_groupe_ne_remplace_pas_la_classe_complete(self):
        complet = calculer_classement_classe(self.eleves, self.matieres, 'OCTOBRE')
        partiel = calculer_classement_classe([self.eleve], self.matieres, 'OCTOBRE')
        self.assertEqual(len(partiel['rang_map']), 1)
        self.assertEqual(len(complet['rang_map']), 2)
        self.assertEqual(complet, calculer_classement_classe(self.eleves, self.matieres, 'OCTOBRE'))

    def test_revision_recente_invalide_les_calculs_meme_sans_signal_local(self):
        with patch('synchronisation.realtime.revision_ecole', return_value='avant'), \
             patch('notes.utils_rangs.revision_ecole', return_value='avant'):
            avant = self.calculer()
        NoteMensuelle.objects.filter(pk=self.note.pk).update(note=Decimal('9'))
        with patch('synchronisation.realtime.revision_ecole', return_value='apres'), \
             patch('notes.utils_rangs.revision_ecole', return_value='apres'):
            apres = self.calculer()
        self.assertNotEqual(avant, apres)
        self.assertEqual(apres[2][self.eleve.pk]['rang_num'], 1)

    def test_use_cache_false_ne_lit_et_n_ecrit_pas_dans_le_cache(self):
        with patch.object(cache, 'get', wraps=cache.get) as lire, \
             patch.object(cache, 'set', wraps=cache.set) as ecrire:
            self.calculer(use_cache=False)
        self.assertEqual(lire.call_count, 0)
        self.assertEqual(ecrire.call_count, 0)
