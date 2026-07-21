# Requirements Document

## Introduction

Pipeline de traitement des données DigIF brutes (VITA-49) reçues via AWS Ground Station S3 Data Delivery jusqu'à la production de fichiers SDR + GEO (HDF5 Level 1) calibrés. Ce pipeline constitue la chaîne amont complète, dont la sortie alimente le spec aval `noaa20-viirs-visualization`.

### Chaîne de traitement complète

```
.pcap (VITA-49 DigIF) → Extraction I/Q → SatDump npp_hrd → .cadu → RT-STPS → RDR (HDF5) → CSPP SDR → SDR + GEO (HDF5 Level 1)
```

### Position dans l'architecture

```
[AWS Ground Station — S3 Data Delivery]
.pcap files (VITA-49 DigIF, ~40 GB par contact)
              │
              ▼
[CETTE SPEC — noaa20-cadu-to-tiff]
.pcap → I/Q extraction → SatDump → .cadu → RT-STPS → RDR → CSPP → SDR + GEO (HDF5 Level 1)
              │
              ▼
[Spec aval: noaa20-viirs-visualization]
SDR + GEO (HDF5 Level 1) → Georeferenced PNG/GeoTIFF + metadata JSON
```

### Contrainte d'environnement

- L'infrastructure est déployée via Terraform, région `eu-central-1`
- EC2 est **bloqué** par une SCP organisationnelle — seuls CodeBuild et ECS Fargate sont disponibles comme compute
- Le pipeline tourne actuellement sur AWS CodeBuild (`BUILD_GENERAL1_LARGE`)
- Le bucket source est `aws-groundstation-demo-reception-471112743408`

### Caractéristiques du signal (mesurées sur contact réel 2026-06-19)

| Paramètre | Valeur |
|-----------|--------|
| Format fichier | .pcap (Ethernet → IP → UDP → VITA-49) |
| Largeur de bande | 30.5 MHz |
| Fréquence centrale RF | 7812 MHz (downconvertie à 862 MHz IF) |
| Taux d'échantillonnage | 34.3125 MSps |
| Format d'échantillon | Complex Cartesian (I/Q), 8-bit signé par composante |
| Taille par échantillon | 2 octets (1 octet I + 1 octet Q) |
| Gain front-end | 33 dB |
| Niveau de référence | -28 dBm |
| Fichiers par contact | 19 chunks de ~30 secondes |
| Taille totale par contact | ~40 GB (19 × 2.18 GB) |
| Durée extraction I/Q (Python) | ~7.8 secondes par chunk |
| Durée SatDump par chunk | ~5 minutes |

### Logiciels de la chaîne de traitement

| Outil | Version | Licence | Rôle dans la chaîne |
|-------|---------|---------|---------------------|
| **Script Python extraction** | custom | — | Strip pcap/Ethernet/IP/UDP/VITA-49 headers → raw .cs8 |
| **SatDump** | v1.2.0 | GPL-3.0 | Baseband → CADU (pipeline `npp_hrd`, QPSK demod + Viterbi + RS + frame sync) |
| **RT-STPS** (NASA) | 7.x | Propriétaire/gratuit | CADU → RDR (CCSDS Level 0 → HDF5) |
| **CSPP SDR** (CIMSS/NOAA) | latest | Open source | RDR → SDR + GEO (produits calibrés HDF5 Level 1) |

### Sortie attendue (contrat avec le spec aval)

| Type de fichier | Pattern de nommage | Contenu |
|-----------------|-------------------|---------|
| SDR réflectance (bandes I) | `SVI0{1,2,3}_npp_d{date}_t{time}_*.h5` | Reflectance_Factors (scale+offset) |
| SDR radiance (bandes M) | `SVM{01-16}_npp_d{date}_t{time}_*.h5` | Radiance_Factors (scale+offset) |
| SDR DNB | `SVDNB_npp_d{date}_t{time}_*.h5` | Radiance Day/Night Band |
| GEO I-band | `GIGTO_npp_d{date}_t{time}_*.h5` | Latitude/Longitude par pixel (375m) |
| GEO M-band | `GMODO_npp_d{date}_t{time}_*.h5` | Latitude/Longitude par pixel (750m) |
| GEO DNB | `GDNBO_npp_d{date}_t{time}_*.h5` | Latitude/Longitude DNB (750m) |

## Glossary

- **Processing_Pipeline**: Le pipeline complet de traitement DigIF → SDR, orchestré par un déclencheur S3 et exécuté sur CodeBuild ou ECS Fargate
- **IQ_Extractor**: Composant logiciel (script Python) responsable du stripping des headers pcap/Ethernet/IP/UDP/VITA-49 et de la production d'un fichier raw .cs8 (complex 8-bit I/Q)
- **SatDump_Processor**: Composant logiciel (SatDump v1.2.0 CLI) responsable de la démodulation/décodage du baseband I/Q en trames CADU via le pipeline `npp_hrd`
- **RTSTPS_Processor**: Composant logiciel (RT-STPS NASA) responsable de la conversion des trames CADU en fichiers RDR (HDF5 Level 0)
- **CSPP_Processor**: Composant logiciel (CSPP SDR) responsable de la calibration et géolocalisation des RDR en fichiers SDR + GEO (HDF5 Level 1)
- **Geolocation_Calculator**: Composant logiciel responsable du calcul de la trace au sol et des coordonnées géographiques à partir des TLE et des timestamps
- **CADU**: Channel Access Data Unit — trame de 1024 octets avec marqueur de synchronisation 0x1ACFFC1D, contenant les données CCSDS encapsulées
- **RDR**: Raw Data Record — fichier HDF5 Level 0 contenant les paquets scientifiques non calibrés organisés par granule
- **SDR**: Sensor Data Record — fichier HDF5 Level 1 contenant les données calibrées (radiance ou réflectance) avec géolocalisation
- **GEO**: Geolocation file — fichier HDF5 compagnon du SDR contenant les coordonnées latitude/longitude par pixel
- **TLE**: Two-Line Element — éphémérides orbitales utilisées pour la propagation SGP4 et le calcul de la trace au sol
- **VITA-49**: Standard VRT (VITA Radio Transport) pour le transport de signaux numérisés sur IP, utilisé par AWS Ground Station pour le DigIF

## Requirements

### Requirement 1: Extraction I/Q depuis les fichiers VITA-49

**User Story:** En tant qu'ingénieur données, je veux extraire les échantillons I/Q bruts depuis les fichiers .pcap VITA-49 reçus d'AWS Ground Station, afin de produire un fichier baseband exploitable par SatDump.

#### Acceptance Criteria

1. WHEN un fichier .pcap est fourni en entrée, THE IQ_Extractor SHALL parser les couches pcap/Ethernet/IP/UDP/VITA-49 et extraire les payload I/Q en préservant l'ordre séquentiel des paquets
2. THE IQ_Extractor SHALL produire un fichier .cs8 contenant les échantillons Complex Cartesian 8-bit signé (1 octet I + 1 octet Q par échantillon) sans aucun header résiduel
3. THE IQ_Extractor SHALL valider que le sample rate déclaré dans les headers VITA-49 correspond à 34312500 Hz (±1 Hz de tolérance)
4. IF un paquet VITA-49 est malformé ou a un numéro de séquence manquant, THEN THE IQ_Extractor SHALL insérer des zéros pour les échantillons manquants et enregistrer le nombre de gaps dans les métadonnées de sortie
5. IF le fichier .pcap ne contient aucun paquet VITA-49 valide, THEN THE IQ_Extractor SHALL rejeter le fichier avec une erreur descriptive
6. WHEN le fichier .pcap contient au moins un paquet VITA-49 valide, THE IQ_Extractor SHALL accepter le fichier et procéder à l'extraction, même si d'autres erreurs de validation (sample rate mismatch, paquets malformés) sont présentes
7. THE IQ_Extractor SHALL traiter un chunk de 2.18 GB en moins de 30 secondes

### Requirement 2: Démodulation et décodage SatDump

**User Story:** En tant qu'ingénieur données, je veux démoduler et décoder le baseband I/Q pour produire des trames CADU, afin de disposer des données encapsulées prêtes pour RT-STPS.

#### Acceptance Criteria

1. WHEN un fichier .cs8 est fourni en entrée, THE SatDump_Processor SHALL exécuter le pipeline `npp_hrd` avec la commande : `satdump npp_hrd baseband <input.cs8> <output_dir> --samplerate 34312500 --baseband_format cs8`
2. THE SatDump_Processor SHALL produire un fichier .cadu contenant au moins une trame CADU décodée (QPSK demod → Viterbi → Reed-Solomon → frame sync) ; un fichier vide ne constitue pas une production valide
3. THE SatDump_Processor SHALL produire un fichier `dataset.json` contenant les métadonnées de décodage (satellite identifié, nombre de trames, statistiques de décodage)
4. IF SatDump retourne un code de sortie non-zéro, THEN THE SatDump_Processor SHALL capturer la sortie stderr et propager l'erreur avec le contexte complet
5. IF le fichier .cadu produit est vide (0 octets) ou ne contient aucune trame décodée, THEN THE SatDump_Processor SHALL signaler un échec de démodulation avec les statistiques de signal (SNR estimé si disponible dans dataset.json)
6. THE SatDump_Processor SHALL fonctionner avec la dépendance OpenCL stub (`ocl-icd-libopencl1`) installée, même en mode CPU-only

### Requirement 3: Conversion CADU vers RDR (RT-STPS)

**User Story:** En tant qu'ingénieur données, je veux convertir les trames CADU en fichiers RDR (HDF5 Level 0), afin de disposer des données scientifiques organisées par granule pour la calibration CSPP.

#### Acceptance Criteria

1. WHEN un fichier .cadu est fourni en entrée, THE RTSTPS_Processor SHALL exécuter RT-STPS pour produire des fichiers RDR au format HDF5
2. THE RTSTPS_Processor SHALL produire des fichiers RDR séparés par instrument (VIIRS, ATMS, CrIS) et par granule temporelle
3. THE RTSTPS_Processor SHALL valider que les fichiers RDR produits contiennent au moins un granule VIIRS valide ; IF la validation échoue, THEN THE RTSTPS_Processor SHALL traiter cela comme un échec complet de traitement et arrêter le traitement de ce chunk
4. IF RT-STPS échoue ou ne produit aucun fichier RDR, THEN THE RTSTPS_Processor SHALL capturer les logs de RT-STPS et propager l'erreur
5. IF les fichiers RDR produits ne contiennent aucun granule pour un instrument non-critique (ATMS, CrIS), THEN THE RTSTPS_Processor SHALL émettre un avertissement indiquant l'instrument manquant sans interrompre le traitement

### Requirement 4: Calibration et géolocalisation CSPP SDR

**User Story:** En tant qu'ingénieur données, je veux calibrer les données RDR et calculer la géolocalisation par pixel, afin de produire des fichiers SDR + GEO exploitables par la chaîne de visualisation aval.

#### Acceptance Criteria

1. WHEN des fichiers RDR sont fournis en entrée, THE CSPP_Processor SHALL exécuter CSPP SDR pour produire des fichiers SDR (données calibrées) et GEO (géolocalisation par pixel)
2. THE CSPP_Processor SHALL produire des fichiers SDR pour toutes les bandes VIIRS disponibles dans les RDR (bandes I1-I5 à 375m, bandes M1-M16 à 750m, bande DNB à 750m)
3. THE CSPP_Processor SHALL produire des fichiers GEO compagnons (GIGTO, GMODO, GDNBO) contenant les coordonnées latitude/longitude par pixel
4. THE CSPP_Processor SHALL stocker les fichiers SDR + GEO dans le bucket de sortie S3 avec une organisation par date de contact et identifiant de contact
5. IF CSPP SDR échoue sur un granule, THEN THE CSPP_Processor SHALL continuer le traitement des granules restants, enregistrer les granules en échec, et marquer le statut global comme succès si au moins un granule a été traité avec succès
6. IF aucun fichier SDR n'est produit pour un contact (tous les granules ont échoué), THEN THE CSPP_Processor SHALL signaler l'échec complet et publier une notification SNS

### Requirement 5: Géolocalisation et calcul de coordonnées

**User Story:** En tant qu'utilisateur final, je veux que chaque chunk traité soit accompagné de métadonnées de géolocalisation (bounding box, trace au sol), afin de savoir quelle zone de la Terre est couverte.

#### Acceptance Criteria

1. THE Geolocation_Calculator SHALL calculer la trace au sol (ground track) du satellite pour chaque chunk traité, en utilisant les éphémérides TLE de NOAA-20 (NORAD 43013) et les timestamps extraits du fichier `dataset.json` de SatDump
2. THE Geolocation_Calculator SHALL produire un fichier `coordinates.json` pour chaque chunk contenant : la bounding box (north, south, east, west) de la trace au nadir, l'étendue approximative du swath VIIRS (±56° cross-track), et la liste des points de la trace au sol (latitude, longitude)
3. THE Geolocation_Calculator SHALL obtenir les TLE depuis CelesTrak (endpoint `https://celestrak.org/NORAD/elements/gp.php?CATNR=43013&FORMAT=3LE`). IF CelesTrak est inaccessible, THEN THE Geolocation_Calculator SHALL utiliser un TLE de fallback stocké dans la configuration et émettre un avertissement
4. THE Geolocation_Calculator SHALL utiliser la bibliothèque `sgp4` ou `pyorbital` (modèle SGP4) pour la propagation orbitale, avec une précision attendue de ±1.5 km au nadir lorsque les TLE datent de moins de 48 heures
5. IF les TLE disponibles datent de plus de 7 jours, THEN THE Geolocation_Calculator SHALL marquer la géolocalisation comme dégradée dans les métadonnées ; un avertissement complémentaire DEVRAIT être émis si le mécanisme est disponible, mais l'absence d'avertissement ne bloque pas le traitement tant que l'indicateur de dégradation est présent

### Requirement 6: Orchestration du pipeline de traitement

**User Story:** En tant qu'opérateur, je veux que le pipeline soit déclenché automatiquement à la réception de nouveaux fichiers .pcap, et exécute les étapes séquentiellement avec gestion d'erreur, afin de produire des SDR sans intervention manuelle.

#### Acceptance Criteria

1. WHEN un ou plusieurs fichiers .pcap sont déposés dans le bucket S3 de réception (préfixe correspondant à un contact terminé), THE Processing_Pipeline SHALL déclencher automatiquement la chaîne de traitement
2. THE Processing_Pipeline SHALL exécuter les étapes dans l'ordre séquentiel suivant : extraction I/Q → SatDump → RT-STPS → CSPP SDR → géolocalisation → upload des résultats
3. THE Processing_Pipeline SHALL traiter chaque chunk .pcap indépendamment et paralléliser le traitement des chunks lorsque les ressources le permettent
4. IF une étape du pipeline échoue pour un chunk après 2 tentatives, THEN THE Processing_Pipeline SHALL marquer le chunk en erreur, continuer le traitement des autres chunks, et publier une notification SNS avec le détail de l'erreur
5. THE Processing_Pipeline SHALL enregistrer les métriques d'exécution dans CloudWatch : durée totale par contact, durée par étape par chunk, nombre de chunks traités/échoués, taille des SDR produits
6. THE Processing_Pipeline SHALL garantir l'idempotence : un même contact ne déclenche pas deux exécutions simultanées du pipeline
7. WHEN le traitement d'un contact complet est terminé, THE Processing_Pipeline SHALL produire un fichier manifeste JSON listant tous les fichiers SDR + GEO produits avec leurs bounding boxes et les chunks en erreur

### Requirement 7: Performance du traitement

**User Story:** En tant qu'ingénieur DevOps, je veux que le pipeline traite un contact complet (~40 GB, 19 chunks) en un temps raisonnable sans infrastructure permanente, afin de minimiser les coûts.

#### Acceptance Criteria

1. THE Processing_Pipeline SHALL traiter un chunk de 2.18 GB (30 secondes de signal) de bout en bout en moins de 15 minutes
2. THE Processing_Pipeline SHALL traiter un contact complet de 19 chunks en moins de 60 minutes lorsque les chunks sont parallélisés sur plusieurs builds CodeBuild concurrents
3. THE Processing_Pipeline SHALL fonctionner sur AWS CodeBuild (BUILD_GENERAL1_LARGE) ou ECS Fargate sans nécessiter d'instances EC2
4. THE Processing_Pipeline SHALL libérer les ressources compute dès la fin du traitement (pas d'infrastructure provisionnée en permanence) ; IF le job de traitement crash ou est terminé de manière inattendue, THEN THE Processing_Pipeline SHALL tout de même tenter le nettoyage des ressources via un mécanisme de cleanup (lifecycle hook, timeout, ou garbage collection)
5. IF la durée de traitement d'un chunk dépasse 20 minutes, THEN THE Processing_Pipeline SHALL interrompre le traitement du chunk et le marquer en timeout

### Requirement 8: Sécurité du pipeline

**User Story:** En tant que responsable sécurité, je veux que le pipeline respecte les exigences de chiffrement, moindre privilège et auditabilité, afin de maintenir la posture de sécurité du système.

#### Acceptance Criteria

1. THE Processing_Pipeline SHALL chiffrer tous les fichiers SDR + GEO au repos dans S3 avec la clé KMS CMK du projet
2. THE Processing_Pipeline SHALL utiliser un rôle IAM dédié avec uniquement les permissions nécessaires : lecture du bucket source, écriture du bucket de sortie, accès KMS, écriture CloudWatch Logs
3. THE Processing_Pipeline SHALL interdire tout accès public aux buckets source et de sortie, en maintenant les buckets privés et en exigeant une authentification IAM pour tout accès
4. THE Processing_Pipeline SHALL imposer le chiffrement en transit (TLS) pour toutes les opérations S3
5. THE Processing_Pipeline SHALL journaliser toutes les opérations S3 via Server Access Logging
6. IF une erreur de permission survient pendant le traitement, THEN THE Processing_Pipeline SHALL enregistrer les détails de l'opération échouée (action, resource ARN, error code) dans CloudWatch Logs
