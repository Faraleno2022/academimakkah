# Cache des moyennes, rangs et classements

Date : 14 octobre 2026.

## Journal transmis

Sur 1 462 lignes, 690 avertissements CacheKeyWarning occupent 1 380 lignes avec leurs indications de source. Les périodes « 1er Trimestre », « 2ème Trimestre », « 3ème Trimestre », « 1er Semestre » et « 2ème Semestre » sont incorporées directement dans les anciennes clés. Les espaces rendent ces clés incompatibles avec memcached et déclenchent les avertissements de validation Django, y compris avec le cache mémoire configuré dans le projet.

Une autre ligne signale « OSError : erreur d'écriture » le 13 octobre à 14:27. Elle ne contient ni traceback, ni requête, ni fichier concerné. Sa cause ne peut pas être établie avec cet extrait. Les connexions et expirations de session visibles ne permettent pas de l'expliquer.

## Cause reproduite

Une invalidation complète parcourait 22 périodes et 6 variantes de clés : 132 suppressions, puis lecture et écriture du compteur de version. 30 suppressions généraient des avertissements. Ces clés supprimées ne correspondaient plus aux clés utilisées, qui incluaient version, sélection d'élèves/matières et révision de synchronisation. Le test ajouté échoue avant correction avec « 30 != 0 ».

## Correction

- Construction commune de clés courtes ASCII à partir d'une empreinte incluant période, système, classe, génération, sélection et révision. Les libellés affichés restent inchangés.
- Une seule écriture d'une nouvelle génération invalide les moyennes, rangs, classements et périodes dépendantes. Le balayage des anciennes clés est supprimé.
- Les générations sont des jetons uniques : deux invalidations concurrentes ne peuvent pas perdre une incrémentation. Une génération évincée est recréée sans réutiliser d'anciens résultats.
- Les deux chemins de sauvegarde des notes utilisent la même invalidation. Le second incrément manuel et la suppression par préfixe obsolète sont retirés.
- Le mode sans cache ne lit plus la version de cache.

La réduction de 134 opérations à une écriture concerne l'invalidation complète d'une classe. Ce n'est pas une mesure de vitesse globale de l'application ou du serveur de production.

## Vérifications

- 11 tests ciblés : OK, 0,766 s, aucun CacheKeyWarning.
- 141 tests notes, calculs, synchronisation temps réel et création d'année : OK, 33,079 s, aucun CacheKeyWarning. Les 11 tests ciblés sont inclus dans cette suite.
- Vérification des libellés français, caractères de contrôle et périodes très longues contre la validation Django des clés ; distinction des classes, sélections, périodes et révisions.
- Recalcul des moyennes et rangs après modification, import en bloc et nouvelle révision reçue ; égalité avec le calcul sans cache.
- Contrôle d'absence de réactivation d'anciens résultats après éviction de la génération.
- `git diff --check` : OK.

## Installation

Cette correction de cache ne nécessite pas de nouvelle migration. Mettre à jour le code puis recharger l'application serveur. Les postes installés auront besoin d'une compilation contenant ces changements.

Les corrections financières de nouvelle année déjà présentes dans la branche sont conservées ; leur migration bus.0012 reste nécessaire lors du déploiement commun. Aucune compilation, migration de la base du client ou publication GitHub n'a été réalisée pour ce diagnostic. Les tests ont utilisé une base temporaire et un réseau externe désactivé. Le journal brut contenant des identités et adresses IP n'a pas été ajouté au dépôt.
