# Audit de synchronisation et de performance — 12 octobre 2026

## Périmètre

Audit du dépôt ouvert, sur `main` après la fusion `82608e9`, avec les mises à
jour de remise et de synchronisation existantes. L'objectif retenu est de
supporter des volumes importants et plusieurs postes tout en gardant les
saisies et les réceptions utilisables.

Les essais utilisent exclusivement des bases temporaires. Le réseau est
désactivé par `scripts/run_project_tests.py`. Aucune migration n'a été exécutée
sur la base du client et aucun déploiement n'a été effectué.

## Défauts confirmés et corrections

### 1. Requêtes répétées pendant l'envoi des données aux postes

La préparation de 200 élèves effectuait 406 requêtes SQL. Chaque ligne du
journal relisait son objet, puis les mêmes classes et parents étaient relus
pendant la sérialisation. Un instantané de 200 élèves effectuait 203 requêtes.

Le serveur charge désormais les objets et leurs relations par modèle, pour
toute la page. Le cache de ces relations est limité à la requête et conserve
les contrôles d'établissement. Les relations vers les comptes utilisateurs,
qui ne sont jamais transmises, ne sont plus lues inutilement.

### 2. Recherche des réceptions dans un journal JSON non indexé

Le poste recherchait les identifiants déjà reçus dans le champ JSON de tout
son historique. Ce travail était répété à chaque page.

La migration `synchronisation.0008_syncchange_reception_indexes` ajoute
`server_change_id`, reprend les anciens identifiants par lots de 1 000 et
ajoute les index nécessaires aux réceptions, aux envois et à la diffusion
par école. Le contrôle de non-réapplication utilise ce champ et l'école.
Le plan SQLite vérifié utilise `sync_school_server_status`.

Un même identifiant dupliqué dans une page n'est plus appliqué deux fois.
Un changement reçu plusieurs fois en échec réutilise son entrée d'erreur.

### 3. Verrou d'écriture gardé pendant une page de réception

Une transaction englobait jusqu'à 500 changements, même si chaque changement
avait aussi son savepoint. Le test de validation avant la donnée suivante
échouait : le commit attendait encore la fin de la page.

Chaque donnée est maintenant validée séparément, y compris pendant le
rattrapage des modèles ajoutés. Le curseur ne progresse qu'après le traitement
de la page. Si son enregistrement est interrompu, les données déjà validées
restent présentes et les identifiants indexés évitent de les réappliquer.
Une erreur dont l'enregistrement est impossible conserve le curseur de reprise.

### 4. Ancien réessai écrasant une nouvelle saisie locale

Reproduction : une réception ancienne reste en échec, puis l'utilisateur
modifie le même objet. Le réessai remplaçait la nouvelle saisie locale par
l'ancienne valeur du serveur, alors que son envoi était encore en attente.

Le réessai ancien est désormais abandonné lorsqu'un changement plus récent
du même objet est appliqué ou en attente. Le test conserve la nouvelle saisie.
Cette protection concerne ce scénario de réessai ; elle ne constitue pas un
système de fusion de modifications simultanées du même champ entre postes.

### 5. Reprise initiale bloquée sur un curseur expiré

Les curseurs d'instantané expirent après 24 heures. Le poste renvoyait le
même curseur refusé à chaque cycle sans pouvoir terminer sa première réception.

Le serveur indique explicitement qu'un nouvel instantané est nécessaire.
Le poste reprend automatiquement depuis son début, sans supprimer sa base.
Une panne ordinaire de transport ne déclenche pas cette reprise.

### 6. Gros rattrapage exécuté dans une seule requête

La première requête d'un processus pouvait mettre en file tout l'historique
des nouveaux modèles, puis numéroter tout le journal avant de répondre.

Le rattrapage inspecte désormais au plus 50 objets par appel et conserve sa
progression. Un objet qui échoue n'est pas sauté. Un autre poste peut lire
les données déjà disponibles sans attendre le verrou de ce rattrapage.
La numérotation appelée par l'API est limitée à un lot de 2 000 changements ;
les lots suivants conservent l'ordre de diffusion.

Les cycles sans changement sont journalisés au niveau DEBUG au lieu de
produire plusieurs lignes INFO toutes les deux secondes.

### 7. Historique des salaires chargé avant la pagination

Pour afficher 15 périodes parmi 60, la vue faisait 128 requêtes et chargeait
les états de toutes les périodes. Les compteurs filtrés relançaient ensuite
des requêtes malgré le préchargement.

Les compteurs sont calculés en SQL, et seule la page demandée est chargée.
Les nombres d'états calculés, validés et payés sont conservés. Le même scénario
utilise maintenant 8 requêtes.

## Mesures reproductibles

| Scénario | Avant | Après |
| --- | ---: | ---: |
| Préparation de 200 élèves à transmettre | 406 requêtes | 7 requêtes |
| Instantané de 200 élèves | 203 requêtes | 5 requêtes |
| Page de 15 périodes parmi 60 | 128 requêtes | 8 requêtes |

Essai supplémentaire : **10 000 élèves et 100 000 événements** de
synchronisation, soit dix événements par élève.

- Préparation d'une page de 500 événements : 7 requêtes, 148,8 ms.
- Sérialisation des 10 000 élèves : 21 appels paginés, 2 436,0 ms, aucun élève manquant.
- Taille des pages d'instantané : au plus 500 objets.
- Les 500 derniers événements sont parcourus jusqu'au curseur 100 000.

Ces temps sont ceux d'un passage local sur SQLite, en mémoire, sans Internet.
Ils mesurent la préparation côté serveur et la sérialisation, pas le délai
complet d'importation sur les postes. Le scénario existant de propagation
entre un serveur et trois postes utilise quatre bases indépendantes et reste
couvert par les tests.

## Vérifications

- 83 tests ciblés réussis en 54,646 s avant le dernier complément sur le verrou
  de rattrapage et son isolation par école.
- 7 tests JavaScript d'actualisation réussis : filtres et défilement conservés,
  saisie protégée, onglet caché, session expirée et reprise réseau.
- Contrôle du schéma réussi, aucune migration manquante.
- Toutes les migrations, dont `0008`, appliquées sur une base temporaire.
- Reprise des identifiants historiques testée avec des valeurs valides et invalides.
- Suite complète sur les sources finales : **866 tests réussis en 187.160 s**.
- Le premier passage avait signalé la distinction HTTP 400/409 pour un
  curseur appartenant à une autre école. Le refus HTTP 400 est conservé ;
  les trois tests de curseur puis la suite complète ont été validés après correction.

Journaux :

- `tmp/audit_performance_avant_20261012.log`
- `tmp/audit_paie_avant_20261012.log`
- `tmp/audit_sync_performance_final_20261012.log`
- `tmp/audit_sync_schema_20261012.log`
- `tmp/audit_performance_suite_validee_20261012.log`

## Mise en service et limites

Les corrections sont dans les sources locales. Pour les utiliser, mettre à
jour le serveur, exécuter ses migrations puis recharger l'application, et
compiler/distribuer les postes avec la nouvelle migration.

L'installateur 1.3.18 produit avant cet audit ne contient pas ces nouvelles
corrections. Il n'a pas été remplacé pendant cette vérification.

Les performances du serveur MySQL, du réseau réel et de postes concurrents
en production n'ont pas été mesurées ici. Le test à 100 000 événements n'est
pas un test de 100 000 utilisateurs simultanés. Le délai visible dépend aussi
du sondage des postes, de l'actualisation de la page, du traitement local et
d'une éventuelle saisie en cours.
