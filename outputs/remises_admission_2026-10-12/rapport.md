# Remises sur l'inscription et la réinscription

## Utilisation après mise à jour

1. Dans **Configuration de l'école → Remises de scolarité**, cocher **Autoriser les remises sur l'inscription et la réinscription**, puis enregistrer.
2. Sur un paiement **en attente**, ouvrir **Appliquer des remises**.
3. Cocher les frais d'inscription ou de réinscription, seuls ou avec les tranches concernées.
4. Choisir un pourcentage ou une remise du catalogue en montant fixe, le motif et la base de calcul.
5. Si le montant saisi est le tarif avant remise, cocher l'option correspondante ; vérifier le net à encaisser, puis appliquer.

L'autorisation est désactivée par défaut et propre à chaque école. La désactiver ultérieurement ne supprime pas les remises déjà accordées. Les règles historiques sans remise sur l'admission conservent leur ventilation.

## Calculs et affichage

- La remise est répartie sur les postes sélectionnés, avec des arrondis au GNF dont la somme est exactement le montant de la remise.
- Les champs « déjà payé » contiennent uniquement les encaissements réels. Le reste à payer tient compte des encaissements et des remises validées.
- Les versements successifs ne remisent pas à nouveau une admission déjà couverte lorsque la base choisie est le paiement à l'échéance.
- Le cumul des remises ne peut pas dépasser le tarif des postes concernés.
- Les reçus, exports, rapports et indicateurs financiers utilisent la ventilation des encaissements après remise.
- L'échéancier affiche le tarif, la remise, le montant déjà payé et le reste par poste. Une admission couverte apparaît soldée et n'entraîne pas de rappel pour des tranches encore futures.
- L'option de l'école et la répartition de la remise sont incluses dans les données synchronisées.

Exemple vérifié : frais d'inscription de **210 000 GNF** et remise de **50 %** sur ces seuls frais. Le montant à encaisser pour l'inscription est **105 000 GNF**, la remise est **105 000 GNF** et le reste sur l'inscription est **0 GNF**.

## Vérifications

Tests exécutés avec `scripts/run_project_tests.py`, sur SQLite temporaire, fichiers temporaires et réseau désactivé :

- Première suite complète : **843 tests réussis**, 350,869 s.
- Après les derniers ajustements : **326 tests paiements/élèves réussis**, 94,923 s, dont **23 nouveaux tests** pour cette fonctionnalité.
- Aperçu JavaScript exécuté : scolarité seule, admission incluse, admission seule, absence de sélection ; résultats corrects.
- Contrôle Django et détection des migrations : aucune erreur, aucune migration manquante.
- Application des migrations sur base temporaire : réussie, notamment `eleves.0022_ecole_autoriser_remise_inscription`.
- `git diff --check` : réussi.

La nouvelle migration doit être appliquée lors du déploiement. La base du client n'a pas été modifiée par ces vérifications.
