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
