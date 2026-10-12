# Vérification de la synchronisation — 12 octobre 2026

## Constat confirmé dans le journal fourni

Le 11 octobre 2026 à 12:10:45, `/api/v1/sync/push/` échoue avec une
`TransactionManagementError`, dans le traitement qui enregistre un changement
refusé. Le code tente d'écrire ce refus alors que la transaction du lot est
inutilisable. Le poste ne reçoit donc aucun acquittement exploitable et conserve
le lot pour un nouvel envoi.

Le journal ne contient pas l'erreur SQL initiale. Un deadlock constitue un
scénario de reproduction testé, sans être une cause confirmée sur ce serveur.
Les avertissements CSRF sans Referer sont uniquement journalisés par le
middleware supplémentaire ; ils ne prouvent pas un refus de synchronisation.

## Corrections dans les sources

- Serveur : chaque changement dispose de sa propre transaction. Le refus est
  enregistré après le rollback ; les autres changements peuvent continuer.
- Serveur : journalisation de l'exception initiale avec modèle, opération et
  position dans le lot. Les pannes temporaires de base sont indiquées au poste.
- Serveur : si le refus ne peut même pas être enregistré, réponse JSON HTTP 503
  sans faux acquittement ; le poste conserve ses données en attente.
- Poste : alternance d'un lot par direction dans les cycles ordinaires. Un échec
  d'envoi n'empêche plus la tentative de réception du même cycle.
- Poste : délai de connexion de 5 secondes, distinct du délai de lecture
  (25 secondes en réception, 120 secondes en envoi). Une tentative d'envoi lente
  peut donc encore retarder la réception pendant son délai de lecture.
- Poste : reprise après au moins une minute des refus temporaires et des parents
  manquants. Une ancienne valeur remplacée par un changement plus récent n'est
  pas remise automatiquement en file. Les refus de validation restent visibles.
- Poste : contrôle complet des index d'acquittement avant de modifier la file.
  Une réponse incomplète ou incohérente conserve le lot.
- Poste : les réessais de réception avancent selon la date du dernier essai,
  pour que les premières erreurs ne privent pas les suivantes de réessai.

## Validation

45 tests de synchronisation réussis sur une base SQLite temporaire, réseau
coupé. Ils couvrent notamment la perte des savepoints, le rollback des écritures
partielles, la poursuite du lot, une véritable violation SQL de contrainte,
l'indisponibilité de la base et la reprise des refus temporaires.

Le test existant de reprise initiale a également été validé avec les 45 tests
ci-dessus (46 tests réussis en 3,832 secondes). La limite de lots par défaut
est lue lors de l'appel ; la synchronisation automatique peut la réduire à un.

Suite complète : **803 tests réussis en 151,843 secondes** avec
`scripts/run_project_tests.py`. Journal final :
`tmp/tests_full_sync_final_20261012.log`. Contrôle `git diff --check` réussi.

## Mise en service

Les transactions plus courtes impliquent un commit par donnée ; le coût
d'écriture doit être mesuré sur le serveur réel après déploiement.

Ces changements sont locaux. Pour qu'ils agissent en production, il faut
mettre à jour les sources du serveur et recharger l'application web, puis
compiler et distribuer la mise à jour des postes. Aucun déploiement ni nouvelle
compilation n'a été effectué dans cette vérification. Les essais ne mesurent
pas les performances du serveur réel et ne remplacent pas un contrôle après
mise en service.


## Révérification après la fusion 5bb1344

### Problème supplémentaire reproduit

Un envoi peut être validé par le serveur alors que sa réponse se perd. Le poste
conserve alors le changement en attente. Son renvoi réappliquait l'ancienne
valeur, même lorsqu'une saisie plus récente avait été faite entre-temps.
Le test de reproduction échouait avant correction : « ANCIENNE VALEUR »
remplaçait « SAISIE PLUS RÉCENTE ».

### Protection ajoutée

Le poste joint désormais un UUID stable à chaque changement. Il est calculé
à partir de l'identifiant du poste, de la ligne locale et de sa date de création ;
un redémarrage ou un renvoi conserve cet UUID. Deux modifications distinctes
restent distinctes, même si elles portent sur le même objet et la même valeur.

Le serveur conserve cet identifiant dans `SyncChange.source_event_id`, avec
une contrainte unique indexée par poste. Lorsqu'un changement déjà validé
revient, le serveur renvoie son acquittement original sans réappliquer ses
écritures. Un verrou court du poste et la contrainte unique protègent aussi
les requêtes simultanées. Réutiliser le même identifiant avec des données ou
une école différentes est refusé.

Un changement refusé ne réserve pas l'identifiant : il peut réussir plus tard,
après résolution d'une panne. Les anciens postes qui n'envoient pas d'UUID
restent compatibles, mais cette protection nécessite la mise à jour des deux
côtés et concerne les envois disposant de cet identifiant.

### Contrôles supplémentaires

- 54 tests de synchronisation réussis en 10,348 secondes sur l'état final.
- Réponse perdue : acquittement stable, une seule application, saisie récente préservée.
- Suppression renvoyée : un objet recréé entre-temps reste présent.
- Nouvelle modification identique à une ancienne : appliquée normalement.
- Panne temporaire puis reprise, identifiant invalide, isolation par poste et par école.
- Modèles et migrations cohérents (`makemigrations --check` sans changement).
- Migration `0006_syncchange_source_event_id` appliquée sur une base temporaire.

Suite complète : **811 tests réussis en 191,597 secondes**
(`tmp/tests_full_sync_idempotence_20261012.log`). Après l'ajout du dernier
contrôle sur le changement d'école d'un poste, les 54 tests de synchronisation
ont été validés sur les sources finales
(`tmp/tests_sync_idempotence_final_20261012.log`).

La vérification complète des migrations s'est terminée avec succès
(`tmp/schema_sync_idempotence_20261012.log`). Contrôle `git diff --check` réussi.

### Mise en service de ce complément

Ce complément reste local et n'a pas été compilé ni déployé. Il ajoute une
migration : exécuter `python manage.py migrate` sur le serveur avant de
recharger l'application et distribuer ensuite une version mise à jour des
postes. Les essais ont utilisé des bases temporaires, avec le réseau coupé.

## Révérification de la rapidité et de l'affichage entre postes

### Retards identifiés et corrigés

- L'envoi et la réception utilisaient encore la même tâche : un envoi en attente
  de réponse pouvait retenir la réception pendant 120 secondes. Deux tâches
  indépendantes ont désormais chacune leur connexion HTTP persistante et leur
  connexion Django. Un test bloque réellement l'envoi et vérifie que la réception
  se termine avant sa libération.
- Le réveil d'envoi est déclenché après le commit de la saisie. Un réveil reçu
  pendant le traitement d'un lot reste actif ; il n'est plus effacé avant
  l'attente suivante. Les lots suivants repartent après 0,1 seconde.
- Le sondage de réception par défaut passe de 4 à 2 secondes. Une valeur déjà
  inscrite dans `sync_config.json` est respectée. La configuration générée pour
  une nouvelle liaison utilise 2 secondes. Le délai initial reste de 15 secondes.
- Le poste d'origine récupère lui aussi l'état validé du serveur. L'application
  des données reçues reste muette : elle ne les remet pas dans la file d'envoi.
- Les pages authentifiées utilisant le gabarit commun vérifient leur révision
  toutes les 2 secondes. Les données nouvelles déclenchent une actualisation
  conservant l'URL, les filtres et la position de défilement.
- Une saisie modifiée, un champ actif, une fenêtre de saisie ou un formulaire
  en cours d'envoi reportent l'actualisation. Un bandeau signale les nouvelles
  données. Les onglets cachés suspendent les sondages et contrôlent la révision
  dès leur retour au premier plan. Les liens temporaires d'enseignants gardent
  leur périmètre d'accès existant.
- Les caches d'école, de classes, de recherche de responsables, de statistiques,
  de matières, de moyennes et de rangs utilisent la révision des données. Une
  réception ne laisse plus ces pages attendre l'ancien délai de cache.
- Un index par école, statut et date d'application accélère la lecture de révision.
  Une ancienne donnée reprise après erreur fait elle aussi changer la révision.
- Les sondages d'affichage ne prolongent pas la session et n'écrivent pas son
  activité toutes les 2 secondes. Les contrôles d'expiration et d'accès demeurent
  appliqués. Le suivi de connexion des postes est écrit au plus toutes les
  30 secondes pour limiter les écritures sans données nouvelles.

### Scénario entre quatre bases distinctes

Le test utilise un serveur et trois postes A, B, C, avec une base SQLite
temporaire distincte pour chacun. Il exécute les vrais modèles, signaux, API,
acquittements, curseurs et pages bus/cantine. Seul le transport réseau est remplacé
par un appel au client HTTP de test Django.

1. Création d'un élève sur A ; réception par le serveur, B, C et A.
2. Création des abonnements bus et cantine sur B ; réception partout.
3. Correction du montant cantine sur C et du nom de l'élève sur le serveur ;
   réception sur les trois postes.
4. Vérification des données et des pages affichées dans les quatre bases :
   cantine due 270 000 GNF, déjà payé 250 000 GNF, reste 20 000 GNF.
5. Nouvel envoi et nouvelle réception à vide : aucun doublon, aucun renvoi en
   boucle, aucun changement en échec.

Mesures locales du premier passage complet réussi, hors réseau et sondage :

| Parcours | Traitement total |
| --- | ---: |
| Élève de A vers serveur et trois postes | 351,9 ms |
| Bus/cantine de B vers serveur et trois postes | 399,2 ms |
| Corrections de C et du serveur vers tous | 525,7 ms |

Ces valeurs mesurent l'application du scénario dans les bases de test. Elles
ne mesurent ni la connexion Internet, ni MySQL, ni la charge du serveur réel.
Avec un intervalle poste de 2 secondes, il faut ajouter jusqu'à un intervalle
pour la récupération puis jusqu'à 2 secondes pour la détection dans la page,
ainsi que le transport, le traitement et le rendu. Un rattrapage volumineux ou
une saisie en cours peut prolonger ce délai. Aucun délai de production n'est
garanti par ces mesures.

### Validation de l'affichage

- 7 tests JavaScript réussis avec `node --test scripts/test_sync_refresh.cjs` :
  absence de changement, actualisation unique, conservation des filtres et du
  défilement, protection des saisies et des fenêtres ouvertes, onglet caché,
  session expirée, requête unique et reprise après coupure réseau.
- 51 tests ciblés réussis sur les sources finales en 70,825 secondes : propagation,
  caches d'élèves et de notes, sessions, tâches indépendantes et régressions
  d'audit. Journal : `tmp/tests_sync_realtime_final_20261012.log`.
- Suite complète : **824 tests réussis en 252,502 secondes**.
  Journal : `tmp/tests_full_sync_realtime_20261012.log`. Ce lancement précédait
  le dernier ajustement du sondage pour ne pas prolonger la session ; cet
  ajustement et sa nouvelle régression ont été vérifiés dans les 51 tests
  ciblés ci-dessus.
- Contrôle du schéma réussi : aucun changement de migration manquant et toutes
  les migrations appliquées, y compris `0006` et `0007`, sur une base temporaire.
  Journal : `tmp/schema_sync_realtime_20261012.log`.

### Mise en service

La mise en service nécessite les migrations `0006` et `0007`, la mise
à jour et le rechargement du serveur, puis la distribution des postes mis à jour.
Les versions installées des autres postes n'ont pas été modifiées pendant ces
tests. Aucune base du client n'a été utilisée ou modifiée.
