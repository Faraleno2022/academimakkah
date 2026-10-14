# Nouvelle année : scolarité, bus et cantine

Date : 14 octobre 2026. Branche : `codex/gestion-financiere-nouvelle-annee-2026-10-14`.

## Corrections

- Un tarif de scolarité absent bloque le passage de l'élève avant le changement de classe. Aucun échéancier gratuit n'est inventé. Une grille explicitement définie à zéro reste utilisable.
- Le passage utilise la grille du niveau de destination et la nature Réinscription. Une inscription directe dans la nouvelle année utilise la nature et le tarif d'inscription correspondants.
- Les paiements de l'ancienne année restent dans leur année. Les versements anticipés validés pour la nouvelle année sont repris dans son échéancier.
- Les dates personnalisées des grilles de scolarité et de bus sont copiées avec un décalage d'un an. Les grilles déjà configurées pour la nouvelle année sont conservées.
- Un passage incomplet affiche les tarifs manquants. L'aperçu peut reprendre la même transition et la gestion des années propose « Compléter le passage ». Aucun élève déjà passé n'est traité une deuxième fois.
- La cantine possède désormais sa propre année scolaire. Listes, totaux, périodicités, remises, alertes et exports bus/cantine respectent la sélection d'année. « Toutes les années — historique » conserve l'accès aux années antérieures.
- Les comptes de direction et superadministrateurs associés à une école utilisent aussi l'année active par défaut. Le périmètre des écoles autorisées est conservé.
- Le renouvellement bus propose la grille de la même zone pour l'année actuelle de l'élève. Une ancienne grille encore active n'est pas réutilisée faute de nouvelle grille.
- Le renouvellement cantine préremplit le tarif brut et la remise dans le formulaire. Changer d'élève efface les montants et remises repris du précédent ; une réponse tardive de l'API ne peut pas écraser le nouvel élève sélectionné.
- Les exports Excel et PDF incluent l'année des abonnements.

## Paiements et soldes conservés

Aucun abonnement ou reçu bus/cantine n'est créé automatiquement lors du passage. Un renouvellement nécessite un nouvel enregistrement par l'utilisateur. Les dettes, avances, remises et reçus existants restent dans leur année et ne sont pas dupliqués sur la nouvelle scolarité. Les remises individuelles bus ne sont pas reconduites automatiquement sur une nouvelle grille.

## Migration

`bus.0012_abonnements_annee_scolaire` ajoute le champ d'année de la cantine. Les années manquantes sont renseignées par lots de 500 : grille du bus en priorité, sinon date de début de l'abonnement. Le début d'année est septembre par défaut (`ANNEE_SCOLAIRE_DEBUT_MOIS` configurable). Les années déjà renseignées du bus sont conservées. Aucun montant, reçu ou paiement n'est modifié. L'année d'un ancien abonnement cantine reste modifiable dans son formulaire.

À appliquer avant de démarrer le code mis à jour :

```bash
python manage.py migrate
```

Pour les postes installés, une nouvelle compilation sera nécessaire. L'installateur existant 1.3.19 ne contient pas cette correction.

## Vérifications

- 387 tests financiers, bus/cantine, rapports, synchronisation temps réel et médias : OK (97,636 s).
- Après les derniers ajustements : 35 tests ciblés de passage d'année et d'abonnements : OK (7,374 s), dont une migration de 501 lignes sur plusieurs lots.
- Contrôle Django, cohérence des migrations et application sur SQLite temporaire : OK.
- Navigateur Edge isolé : annulation sans POST, création en 3 lots, aucun rechargement interrompant la création, filtres des deux années dans le bus et la cantine, reprise du tarif et de la remise, changement d'élève sans conserver sa remise : OK, aucune erreur JavaScript.
- Mesure locale pour 3 élèves : création 1 589 ms, parcours complet 3 786 ms. Ces mesures ne constituent pas une mesure du serveur de production.

Les tests utilisent SQLite, fichiers et navigateur temporaires, avec réseau externe désactivé. La base du client n'a pas été utilisée. Aucune compilation ni publication GitHub réalisée pendant cette correction.
