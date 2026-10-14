# Création de nouvelle année scolaire

Date : 14 octobre 2026.

## Cause du bouton qui tourne

Le bouton appelait `lancerAnnee(this)` pendant son événement `click`.
Cette fonction désactivait immédiatement le bouton de soumission. Le navigateur
annulait alors son action par défaut : aucune requête POST n'était envoyée,
alors que le bouton affichait « Création en cours… ».

Le comportement a été reproduit dans Edge isolé avec le script original :
bouton désactivé, indicateur visible, zéro requête POST après confirmation.
Les anciens tests appelaient directement la vue Django et ne pouvaient pas
détecter cette erreur du bouton dans le navigateur.

## Correction

- Confirmation et désactivation déplacées sur l'événement `submit` du formulaire.
  Le clic et la soumission au clavier peuvent envoyer la création normalement.
- Protection contre les doubles soumissions, avec restauration du bouton lors
  d'un retour depuis le cache de navigation.
- Formulaires de création et de progression protégés contre l'actualisation
  automatique de synchronisation, qui pouvait interrompre la chaîne de lots.
- Lots suivants soumis avec `requestSubmit()`, en passant par la même protection
  que le bouton manuel « Continuer ».
- Contrôle du format des années, de leur succession et de l'existence de l'année
  source dans l'école. Une année source identique à la destination ne peut plus
  déclencher des lots sans fin.
- Élève non déplacé : erreur explicite ; exclusion des réessais du même passage.
  Le nombre d'élèves restants est recalculé avec les erreurs du lot courant.
- Compteurs restaurés après annulation de la transaction d'un élève, notamment
  si son nouvel échéancier échoue. Le bilan ne compte pas un passage annulé.
- La nouvelle année devient l'année active de la session une fois la création
  terminée, même si l'utilisateur consultait auparavant l'ancienne année.

## Vérifications

Les 255 tests Django de nouvelle année, paiements et propagation de
synchronisation passent en 81,055 secondes. Aucun problème n'est signalé
par le contrôle système Django. Le parcours de navigateur final réussit aussi.

Le parcours de navigateur exécute les vraies pages, vues, sessions, contrôles
CSRF et middleware du projet, avec une base et des fichiers temporaires.
La licence est exclue de ce test ; les connexions externes sont désactivées.

Résultat du parcours final dans Edge :

- Annulation de la confirmation : aucun POST et bouton encore utilisable.
- Confirmation : trois POST de navigation, un par lot de test.
- Trois lots enchaînés automatiquement, aucun GET de rechargement sur l'URL de création.
- Deux classes créées pour 2026-2027, trois élèves déplacés en classe supérieure,
  trois échéanciers créés, puis affichage de 2026-2027.
- Aucune erreur JavaScript ; parcours local de trois élèves en 2,161 secondes.
  Ce délai est une mesure locale, pas une estimation pour une école complète.

Les tests Django couvrent aussi les notes et classements de l'ancienne année,
les échéanciers existants, le passage en lots, la reprise sans session,
les années invalides, l'activation de l'année et les erreurs d'élève/échéancier.

Commandes :

```text
python scripts/run_project_tests.py eleves.test_nouvelle_annee paiements synchronisation.test_realtime
python scripts/verifier_nouvelle_annee_navigateur.py
```

Le test de navigateur requiert Playwright pour Node et Edge (ou un navigateur
Chromium indiqué par `MYSCHOOL_TEST_BROWSER`). `NODE_PATH` peut indiquer le
répertoire des dépendances Node disponibles sur la machine de vérification.

## Livraison

Correction préparée dans le code local. Aucune migration nécessaire pour cette
correction. Elle doit être livrée sur le serveur ; l'exécutable des postes devra
être recompilé pour l'inclure. L'installateur 1.3.19 produit avant cette correction
ne la contient pas.

Aucune année n'a été créée dans la base du client pendant les vérifications.
