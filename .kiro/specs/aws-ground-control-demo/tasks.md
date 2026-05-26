# Implementation Plan: AWS Ground Control Demo — Gap Remediation

## Overview

Ce plan couvre uniquement les lacunes identifiées lors de l'analyse de conformité entre l'implémentation existante et les exigences. L'infrastructure principale est déjà déployée — ces tâches corrigent les écarts restants.

## Tasks

- [x] 1. Configurer la démodulation QPSK dans le profil antenne
  - [x] 1.1 Remplacer `antenna_downlink_config` par `antenna_downlink_demod_decode_config` dans le module mission_profile
    - Modifier `modules/mission_profile/main.tf` : remplacer le bloc `antenna_downlink_config` par `antenna_downlink_demod_decode_config` incluant les paramètres de démodulation QPSK (modulation, coding, unvalidated frame length)
    - Conserver les paramètres spectraux existants : 7812 MHz, 30 MHz BW, RHCP
    - Mettre à jour le dataflow edge dans le mission profile si l'ARN de la config change
    - _Requirements: 2.1_

  - [x] 1.2 Mettre à jour les tags et le nom de la ressource pour refléter demod-decode
    - Renommer la ressource de `antenna_downlink` à `antenna_downlink_demod_decode` (ou ajouter un suffixe explicite)
    - Mettre à jour les références dans `awscc_groundstation_mission_profile.noaa20_hrd.dataflow_edges`
    - _Requirements: 2.1_

- [x] 2. Ajouter l'action SNS à l'alarme S3 errors
  - [x] 2.1 Ajouter une variable `sns_topic_arn` au module s3_delivery
    - Ajouter la variable dans `modules/s3_delivery/variables.tf`
    - Passer `module.security.sns_topic_arn` depuis `main.tf` lors de l'appel du module
    - _Requirements: 1.5_

  - [x] 2.2 Configurer `alarm_actions` sur l'alarme `aws_cloudwatch_metric_alarm.s3_errors`
    - Ajouter `alarm_actions = [var.sns_topic_arn]` à la ressource dans `modules/s3_delivery/main.tf`
    - _Requirements: 1.5_

- [x] 3. Checkpoint — Valider la configuration
  - Ensure all tests pass, ask the user if questions arise.

- [x] 4. Ajouter la validation du satellite NOAA-20
  - [x] 4.1 Créer un data source ou une précondition pour vérifier l'enregistrement du satellite
    - Ajouter un `data "aws_groundstation_satellite"` ou un bloc `precondition` dans le module mission_profile (ou dans `main.tf`) vérifiant que le satellite NORAD ID 43013 est onboarded dans le compte
    - Si le data source n'existe pas dans le provider AWS, utiliser un `check` block Terraform ou un `precondition` sur le module avec un message d'erreur explicite
    - _Requirements: 7.3_

- [x] 5. Remplacer le widget texte statique par une métrique de coût calculée
  - [x] 5.1 Remplacer le widget `text` "Estimated Cost" par un widget `metric` dans le dashboard CloudWatch
    - Modifier `modules/observability/main.tf` : remplacer le dernier widget (type `text`) par un widget de type `metric` basé sur la durée des contacts (métrique `ContactDuration` ou calcul à partir de `ContactStatus` COMPLETED × durée moyenne)
    - Utiliser une expression mathématique CloudWatch si possible, sinon un widget `metric` avec la métrique `AWS/GroundStation` pertinente
    - _Requirements: 5.4_

- [x] 6. Nettoyer la variable `contact_max_duration_seconds` inutilisée
  - [x] 6.1 Supprimer ou documenter la variable `contact_max_duration_seconds`
    - La variable est déclarée dans `modules/mission_profile/variables.tf` mais n'est référencée nulle part dans le code
    - Option A : supprimer la variable si elle n'a pas de raison d'être
    - Option B : ajouter un commentaire expliquant son usage futur et l'utiliser dans une validation ou un tag
    - _Requirements: 2.4_

- [x] 7. Checkpoint final — Validation Terraform
  - Exécuter `terraform fmt -check -recursive`, `terraform validate`, et `terraform plan`
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Les tâches couvrent uniquement les écarts identifiés — l'infrastructure principale est déjà fonctionnelle
- La tâche 1 (QPSK demod) est critique car sans elle, les données reçues ne sont pas démodulées correctement
- La tâche 2 (alarm_actions) est importante pour que l'alarme S3 notifie réellement les opérateurs
- La tâche 4 (validation satellite) est un garde-fou pour éviter un `terraform apply` sur un compte sans satellite onboarded
- Les tâches 5 et 6 sont des améliorations de qualité (nice-to-have)
- Chaque tâche référence les exigences spécifiques pour la traçabilité

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "2.1", "4.1", "6.1"] },
    { "id": 1, "tasks": ["1.2", "2.2", "5.1"] }
  ]
}
```
