# Requirements Document

## Introduction

Pipeline de post-traitement des données VIIRS produites par la chaîne amont `noaa20-cadu-to-tiff`. Ce module transforme les sorties du pipeline amont en images géoréférencées (PNG avec annotations cartographiques Cartopy et/ou GeoTIFF EPSG:4326) accompagnées de métadonnées JSON, déployé sous forme de pipeline serverless AWS (Lambda + CodeBuild + S3).

### Deux chemins de visualisation parallèles

Le pipeline amont produit deux types de sortie selon l'état de la chaîne de traitement :

1. **Chemin SatDump** (opérationnel maintenant) — SatDump produit des composites PNG pré-rendus (True Color, Thermal IR, False Color, Day Microphysics, etc.) + un fichier `product.cbor` de métadonnées + des PNG par bande. La calibration est communautaire (non certifiée NOAA).

2. **Chemin NASA** (futur — nécessite correction RT-STPS) — RT-STPS + CSPP SDR produit des fichiers HDF5 Level 1 (SDR + GEO) avec calibration officielle NOAA. Nécessite lecture h5py, calibration radiométrique, et reprojection.

### Position dans la chaîne de traitement

```
[Spec amont: noaa20-cadu-to-tiff]

Chemin SatDump (opérationnel) :
.pcap DigIF → SatDump npp_hrd → Composites PNG + product.cbor + bandes PNG
                                              │
                                              ▼
                              [CETTE SPEC — Chemin SatDump]
                              PNG composite + TLE/sgp4 bbox → Cartopy overlay → Georeferenced PNG + JSON

Chemin NASA (futur) :
.pcap DigIF → SatDump → .cadu → RT-STPS → RDR → CSPP → SDR + GEO (HDF5 Level 1)
                                                              │
                                                              ▼
                                             [CETTE SPEC — Chemin NASA]
                                             HDF5 SDR + GEO → Calibration + Rendu → Georeferenced PNG + JSON
```

### Contrat d'entrée (frontière avec le spec amont)

#### Chemin SatDump

| Type de fichier | Pattern de nommage | Contenu |
|-----------------|-------------------|---------|
| Composite PNG | `viirs_rgb_True_Color.png`, `viirs_10.8um_Thermal_IR_(Uncalibrated).png`, etc. | Image composite pré-rendue 8/16 bits |
| Métadonnées CBOR | `product.cbor` | Timestamps, satellite, informations de projection |
| Bande PNG individuelle | `VIIRS-I1.png` à `VIIRS-M16.png` | Image par canal (non utilisé en entrée primaire) |

#### Chemin NASA

| Type de fichier | Pattern de nommage | Contenu |
|-----------------|-------------------|---------|
| SDR réflectance (bandes I) | `SVI0{1,2,3}_npp_d{date}_t{time}_*.h5` | Reflectance_Factors (scale+offset) |
| SDR radiance (bande M15) | `SVOM15_npp_d{date}_t{time}_*.h5` | Radiance_Factors (scale+offset) |
| GEO I-band | `GIGTO_npp_d{date}_t{time}_*.h5` | Latitude/Longitude par pixel (375m) |
| GEO M-band | `GMODO_npp_d{date}_t{time}_*.h5` | Latitude/Longitude par pixel (750m) |

### Contraintes d'environnement

- Infrastructure Terraform, région `eu-central-1`
- Python 3.12 (Lambdas + CodeBuild)
- Stockage S3 pour les entrées et les sorties
- Dépendances CodeBuild : Python 3.12, cartopy, matplotlib, numpy, Pillow, cbor2, sgp4, h5py, rasterio, scipy
- Lambda pour l'orchestration (détection du chemin + déclenchement CodeBuild)

### Priorité d'implémentation

Le chemin SatDump (Requirements 1–5) est implémenté en premier car il est opérationnel immédiatement. Le chemin NASA (Requirements 6–10) est implémenté ultérieurement quand RT-STPS sera corrigé.

## Glossary

- **Visualization_Pipeline**: Le pipeline serverless complet responsable de l'orchestration et de l'exécution du post-traitement VIIRS → images géoréférencées, couvrant les deux chemins (SatDump et NASA)
- **SatDump_Visualizer**: Composant logiciel responsable de la lecture des composites PNG SatDump et de leur enrichissement avec des annotations cartographiques Cartopy
- **CBOR_Reader**: Composant logiciel responsable de la lecture du fichier `product.cbor` de SatDump pour extraire timestamps, satellite, et métadonnées de projection
- **BBox_Calculator**: Composant logiciel responsable du calcul du bounding box géographique à partir des TLE (sgp4) et des timestamps extraits du CBOR
- **Cartopy_Renderer**: Composant logiciel responsable de la superposition des couches géographiques (côtes 10m, frontières, grille lat/lon, POI) sur les images via Cartopy — utilisé par les deux chemins
- **SDR_Reader**: Composant logiciel responsable de la lecture des fichiers HDF5 SDR et de l'application de la calibration (scale + offset) pour produire des valeurs physiques (réflectance ou radiance)
- **GEO_Reader**: Composant logiciel responsable de la lecture des fichiers HDF5 de géolocalisation et de l'extraction des tableaux latitude/longitude par pixel
- **BT_Converter**: Composant logiciel responsable de la conversion de la radiance spectrale en température de brillance via la loi de Planck inversée
- **Image_Renderer**: Composant logiciel responsable de la production d'images PNG géoréférencées avec traitement d'image (gamma, contraste, déstripage) — spécifique au chemin NASA
- **GeoTIFF_Exporter**: Composant logiciel responsable de l'interpolation sur grille régulière et de l'export en format GeoTIFF WGS84 (EPSG:4326) — optionnel pour les deux chemins
- **Metadata_Generator**: Composant logiciel responsable de la production du fichier JSON de métadonnées associé à chaque rendu — format commun aux deux chemins
- **Destriper**: Composant logiciel responsable de la correction du striping inter-détecteurs (16 détecteurs VIIRS) par soustraction de médiane — spécifique au chemin NASA
- **Composite_Type**: Type de composite produit par SatDump (True Color, Thermal IR, False Color, Day Microphysics, etc.)
- **Fill_Value**: Valeur sentinelle (65535 pour entiers 16 bits, -999.3 pour flottants) indiquant l'absence de données valides dans un pixel VIIRS
- **Swath**: Bande d'acquisition au sol correspondant à un passage du satellite
- **TLE**: Two-Line Element set — paramètres orbitaux utilisés par sgp4 pour propager la position du satellite
- **POI**: Point d'intérêt géographique annoté sur l'image (villes, îles, repères)

## Requirements

---

## Chemin SatDump (implémenter en premier)

### Requirement 1: Lecture des composites PNG SatDump

**User Story:** En tant qu'opérateur du pipeline, je veux que les composites PNG pré-rendus par SatDump soient lus et identifiés automatiquement, afin de les enrichir avec des annotations géographiques.

#### Acceptance Criteria

1. WHEN un dossier de sortie SatDump contenant des fichiers PNG composites est fourni, THE SatDump_Visualizer SHALL identifier les composites disponibles en recherchant les fichiers correspondant aux patterns `viirs_rgb_*.png` et `viirs_*_Thermal_IR_*.png`
2. THE SatDump_Visualizer SHALL supporter les Composite_Type suivants : True Color, Thermal IR (Uncalibrated), False Color, Day Microphysics, Night Microphysics, Natural Color
3. WHEN un composite PNG est en mode 16 bits (mode `I;16`), THE SatDump_Visualizer SHALL normaliser les valeurs en plage [0, 1] par division par 65535
4. WHEN un composite PNG est en mode 8 bits, THE SatDump_Visualizer SHALL normaliser les valeurs en plage [0, 1] par division par 255
5. WHEN un pixel du composite contient une valeur inférieure à 1e-6 après normalisation, THE SatDump_Visualizer SHALL masquer ce pixel comme donnée absente (no-data SatDump)
6. IF un dossier SatDump ne contient aucun fichier PNG composite reconnu, THEN THE SatDump_Visualizer SHALL retourner une erreur descriptive listant les fichiers trouvés dans le dossier

### Requirement 2: Lecture des métadonnées CBOR SatDump

**User Story:** En tant qu'opérateur du pipeline, je veux que les métadonnées `product.cbor` de SatDump soient lues pour extraire les éphémérides satellite, le timestamp et les informations de projection, afin de géoréférencer précisément les composites.

#### Acceptance Criteria

1. WHEN un fichier `product.cbor` est présent dans le dossier SatDump ou ses sous-dossiers, THE CBOR_Reader SHALL extraire le champ `projection_cfg` qui contient les éphémérides satellite (positions ECI + timestamps)
2. WHEN le champ `projection_cfg.ephemeris` est présent, THE CBOR_Reader SHALL extraire la liste des positions satellite (x, y, z en km ECI) avec leurs timestamps pour calcul de la trace au sol
3. WHEN le champ `projection_cfg.scan_angle` est présent, THE CBOR_Reader SHALL extraire l'angle total de scan (112° pour VIIRS = ±56° cross-track)
4. WHEN le champ `projection_cfg.image_width` est présent, THE CBOR_Reader SHALL extraire la largeur native de l'image en pixels pour le calcul du rapport d'aspect
5. WHEN un fichier `product.cbor` contient un champ `satellite` ou `sat_name`, THE CBOR_Reader SHALL extraire le nom du satellite (valeur par défaut : "NOAA-20")
6. IF le fichier `product.cbor` est absent du dossier SatDump, THEN THE CBOR_Reader SHALL produire des métadonnées par défaut (satellite="NOAA-20", datetime="unknown") sans interrompre le pipeline
7. IF la bibliothèque cbor2 échoue à parser le fichier, THEN THE CBOR_Reader SHALL journaliser un avertissement et continuer avec les métadonnées par défaut

### Requirement 3: Calcul du bounding box par éphémérides CBOR

**User Story:** En tant qu'opérateur du pipeline, je veux que le bounding box géographique de la passe soit calculé automatiquement à partir des éphémérides satellite contenues dans le product.cbor, afin de géoréférencer précisément les composites sans intervention manuelle.

#### Acceptance Criteria

1. WHEN des éphémérides sont disponibles dans `projection_cfg.ephemeris`, THE BBox_Calculator SHALL convertir les positions ECI (x, y, z km) en coordonnées géodésiques (lat, lon) via rotation GMST pour chaque point de la trace au sol
2. THE BBox_Calculator SHALL étendre le bounding box nadir par l'angle cross-track (scan_angle/2, typiquement ±56°) en tenant compte de l'altitude satellite pour obtenir le bounding box du swath complet
3. WHEN un fichier `.georef` est présent dans le dossier SatDump, THE BBox_Calculator SHALL utiliser les coordonnées des coins (top_left, top_right, bottom_left, bottom_right) au lieu du calcul éphéméride
4. IF aucune source de géolocalisation n'est disponible (ni éphémérides CBOR, ni .georef), THEN THE BBox_Calculator SHALL tenter un calcul TLE/sgp4 si un timestamp est disponible, ou retourner une erreur demandant un bounding box manuel
5. THE BBox_Calculator SHALL produire un bounding box au format (lat_min, lat_max, lon_min, lon_max) en degrés décimaux WGS84

### Requirement 4: Rendu Cartopy sur composites SatDump

**User Story:** En tant qu'utilisateur final, je veux que les composites SatDump soient enrichis avec des repères géographiques (côtes, frontières, grille, POI) en respectant les dimensions natives du swath, afin de localiser les structures météorologiques sans déformation.

#### Acceptance Criteria

1. THE Cartopy_Renderer SHALL superposer sur le composite : les côtes (résolution 10m, trait blanc 0.8px), les frontières nationales (tirets jaunes 0.6px), et les lacs (alpha 0.2)
2. THE Cartopy_Renderer SHALL afficher une grille lat/lon avec des pas automatiques (10° si span > 40°, 5° si span > 20°, 2° sinon) en tirets blancs semi-transparents (alpha 0.6)
3. THE Cartopy_Renderer SHALL annoter les POI géographiques visibles dans l'emprise du bounding box avec des étiquettes blanches sur fond noir semi-transparent (alpha 0.45)
4. THE Cartopy_Renderer SHALL conserver le rapport d'aspect natif de l'image composite (width/height en pixels) lors du rendu géographique — le bounding box de la figure doit correspondre aux proportions pixel de l'image, pas à un ratio géographique arbitraire
5. THE Cartopy_Renderer SHALL appliquer un flip vertical (nord en haut) ET un flip horizontal (est à droite) pour corriger l'orientation du scan VIIRS descendant
6. THE Cartopy_Renderer SHALL produire un fichier PNG à 300 DPI avec un titre comprenant : le Composite_Type, la date/heure UTC, le satellite, et une mention de calibration ("Calibration communautaire SatDump — non certifiée NOAA")
7. WHEN le composite est de type Thermal IR, THE Cartopy_Renderer SHALL appliquer une colormap RdYlBu_r avec une barre de couleur annotée "Valeur normalisée SatDump"
8. WHEN le composite est de type True Color ou False Color, THE Cartopy_Renderer SHALL afficher le RGB directement avec interpolation bilinéaire

### Requirement 5: Métadonnées JSON pour le chemin SatDump

**User Story:** En tant qu'opérateur du pipeline, je veux un fichier JSON de métadonnées pour chaque image SatDump enrichie, afin de cataloguer et indexer les produits dans S3.

#### Acceptance Criteria

1. THE Metadata_Generator SHALL produire un fichier JSON contenant : source ("SatDump"), satellite, datetime_utc, composite_type, bounding box (lat_min, lat_max, lon_min, lon_max), note de calibration, et chemin du fichier PNG de sortie
2. THE Metadata_Generator SHALL nommer le fichier JSON avec le même nom de base que le PNG de sortie, avec l'extension `.json`
3. THE Metadata_Generator SHALL inclure un champ `calibration_note` avec la valeur "Communautaire SatDump — non certifiée NOAA"
4. THE Metadata_Generator SHALL inclure un champ `visualization_path` avec la valeur "satdump" pour distinguer ce chemin du chemin NASA

---

## Chemin NASA (implémenter ultérieurement)

### Requirement 6: Lecture et calibration des données SDR

**User Story:** En tant qu'opérateur du pipeline, je veux que les fichiers HDF5 SDR soient lus et calibrés automatiquement, afin d'obtenir des valeurs physiques exploitables pour le rendu.

#### Acceptance Criteria

1. WHEN un fichier SDR de réflectance est fourni, THE SDR_Reader SHALL extraire le dataset `Reflectance` et appliquer la calibration linéaire (valeur × scale + offset) pour produire des valeurs de réflectance dans la plage [0, 1]
2. WHEN un fichier SDR de radiance est fourni, THE SDR_Reader SHALL extraire le dataset `Radiance` et appliquer la calibration linéaire (valeur × scale + offset) pour produire des valeurs de radiance en mW·m⁻²·sr⁻¹·µm⁻¹
3. WHEN un pixel contient la Fill_Value entière (65535), THE SDR_Reader SHALL masquer ce pixel et l'exclure de tout traitement ultérieur
4. IF un fichier SDR ne contient aucun groupe HDF5 avec "SDR" dans son nom, THEN THE SDR_Reader SHALL retourner une erreur descriptive identifiant le fichier concerné

### Requirement 7: Lecture de la géolocalisation HDF5

**User Story:** En tant qu'opérateur du pipeline, je veux que les fichiers GEO soient lus correctement avec le bon type de bande, afin que chaque pixel soit associé à ses coordonnées géographiques précises (plus précis que le TLE du chemin SatDump).

#### Acceptance Criteria

1. WHEN un fichier GEO I-band (GIGTO) est fourni, THE GEO_Reader SHALL extraire les datasets Latitude et Longitude depuis le groupe `VIIRS-IMG-GEO_All` avec une précision float32
2. WHEN un fichier GEO M-band (GMODO) est fourni, THE GEO_Reader SHALL extraire les datasets Latitude et Longitude depuis le groupe `VIIRS-MOD-GEO_All` avec une précision float32
3. WHEN un pixel de géolocalisation contient une valeur inférieure à -900, THE GEO_Reader SHALL masquer ce pixel comme donnée invalide
4. THE GEO_Reader SHALL produire des tableaux lat/lon de même dimension que les données SDR correspondantes

### Requirement 8: Conversion radiance → température de brillance et traitement d'image

**User Story:** En tant qu'opérateur du pipeline, je veux que la radiance soit convertie en température de brillance et que les images soient traitées pour une lecture optimale, afin d'exploiter les données thermiques et visuelles de manière intuitive.

#### Acceptance Criteria

1. WHEN des données de radiance calibrées de la bande M15 sont disponibles, THE BT_Converter SHALL appliquer la loi de Planck inversée avec les constantes C1 = 1.191042×10⁸ mW·µm⁴·m⁻²·sr⁻¹, C2 = 1.4387752×10⁴ µm·K, et la longueur d'onde centrale λ = 10.7630 µm
2. THE BT_Converter SHALL produire des valeurs de température de brillance en Kelvin
3. WHEN un pixel de radiance est masqué (Fill_Value), THE BT_Converter SHALL propager le masque sur la température de brillance correspondante
4. THE Image_Renderer SHALL appliquer un Contrast_Stretch par percentiles (p_low=2, p_high=98) suivi d'une Gamma_Correction (γ=0.5) sur chaque bande de réflectance avant assemblage RGB
5. WHERE l'option déstripage est activée, THE Destriper SHALL corriger le striping inter-détecteurs en soustrayant la médiane par détecteur (16 détecteurs, pas cyclique de 16 lignes) de chaque colonne

### Requirement 9: Rendu PNG géoréférencé pour le chemin NASA

**User Story:** En tant qu'utilisateur final, je veux une image PNG haute résolution avec des repères géographiques issus de la géolocalisation per-pixel HDF5, afin de bénéficier de la précision supérieure de la chaîne CSPP SDR.

#### Acceptance Criteria

1. THE Cartopy_Renderer SHALL produire un fichier PNG à 300 DPI avec projection PlateCarree centrée sur le swath, en utilisant les coordonnées lat/lon per-pixel des fichiers GEO
2. THE Cartopy_Renderer SHALL superposer les côtes (résolution 10m, trait blanc), les frontières nationales (tirets jaunes), et les lacs (alpha 0.2) sur l'image
3. THE Cartopy_Renderer SHALL afficher une grille lat/lon avec des pas automatiques en tirets blancs semi-transparents
4. THE Cartopy_Renderer SHALL annoter les POI géographiques visibles dans l'emprise du swath
5. THE Cartopy_Renderer SHALL afficher un titre comprenant le mode de rendu, la date/heure UTC, le numéro d'orbite, et l'identifiant de granule
6. WHEN le mode thermique est actif, THE Cartopy_Renderer SHALL afficher une barre de couleur avec double axe (Kelvin et °Celsius) et une plage fixe de 210K à 305K
7. WHEN le mode True Color est actif, THE Image_Renderer SHALL assembler un composite RGB à partir des bandes I1 (rouge), I2 (vert), I3 (bleu) après correction gamma individuelle

### Requirement 10: Métadonnées JSON et GeoTIFF pour le chemin NASA

**User Story:** En tant qu'opérateur du pipeline et analyste géospatial, je veux un JSON de métadonnées et un GeoTIFF optionnel pour le chemin NASA, afin de cataloguer les produits et les intégrer dans un SIG.

#### Acceptance Criteria

1. THE Metadata_Generator SHALL produire un fichier JSON contenant : granule_id, datetime_utc, orbit_number, mode de rendu, bounding box (lat_min, lat_max, lon_min, lon_max), largeur de swath estimée en km, chemin du fichier PNG, et un champ `visualization_path` avec la valeur "nasa"
2. THE Metadata_Generator SHALL extraire le granule_id, l'orbit_number et le datetime depuis les attributs globaux HDF5 du premier fichier SDR (N_Granule_ID, N_Beginning_Orbit_Number, N_Beginning_Time_IET)
3. WHEN l'attribut N_Beginning_Time_IET est présent, THE Metadata_Generator SHALL convertir le timestamp IET (microsecondes depuis 1958-01-01) en datetime UTC ISO 8601
4. IF les attributs de métadonnées sont absents du fichier HDF5, THEN THE Metadata_Generator SHALL produire le JSON avec des valeurs par défaut ("unknown", 0) sans interrompre le pipeline
5. WHERE l'option GeoTIFF est activée, THE GeoTIFF_Exporter SHALL interpoler les données du swath sur une grille régulière lat/lon avec une résolution de ~0.0067° (~750m)
6. WHERE l'option GeoTIFF est activée, THE GeoTIFF_Exporter SHALL produire un fichier GeoTIFF avec CRS EPSG:4326 et une transformation affine calculée depuis le bounding box

---

## Exigences partagées (les deux chemins)

### Requirement 11: Orchestration serverless AWS et détection de chemin

**User Story:** En tant qu'opérateur du pipeline, je veux que le traitement soit déclenché automatiquement quand de nouveaux fichiers arrivent dans S3, et que le bon chemin de visualisation soit sélectionné en fonction des fichiers disponibles, afin d'obtenir les produits visuels sans intervention manuelle.

#### Acceptance Criteria

1. WHEN des fichiers PNG composites SatDump (pattern `viirs_rgb_*.png` ou `viirs_*_Thermal_IR_*.png`) sont déposés dans le bucket S3 d'entrée, THE Visualization_Pipeline SHALL déclencher le chemin de visualisation SatDump
2. WHEN des fichiers SDR HDF5 (pattern `SVI0*_npp_*.h5` ou `SVOM15_npp_*.h5`) sont déposés dans le bucket S3 d'entrée, THE Visualization_Pipeline SHALL déclencher le chemin de visualisation NASA
3. THE Visualization_Pipeline SHALL utiliser une Lambda d'orchestration (Python 3.12) pour détecter le type de fichiers disponibles, sélectionner le chemin approprié, et soumettre le job CodeBuild correspondant
4. THE Visualization_Pipeline SHALL exécuter le rendu dans un environnement CodeBuild (Python 3.12) avec les dépendances : cartopy, matplotlib, numpy, Pillow, cbor2, sgp4, h5py, rasterio, scipy
5. IF le traitement échoue, THEN THE Visualization_Pipeline SHALL journaliser l'erreur dans CloudWatch Logs avec le contexte (fichiers d'entrée, chemin sélectionné, message d'erreur) et ne pas interrompre les traitements suivants

### Requirement 12: Export GeoTIFF optionnel (les deux chemins)

**User Story:** En tant qu'analyste géospatial, je veux un GeoTIFF projeté en WGS84 disponible pour les deux chemins de visualisation, afin d'intégrer les données VIIRS dans mon SIG quel que soit le chemin utilisé.

#### Acceptance Criteria

1. WHERE l'option GeoTIFF est activée pour le chemin SatDump, THE GeoTIFF_Exporter SHALL produire un fichier GeoTIFF avec CRS EPSG:4326, les bandes RGB (True Color) ou 1 bande (Thermal) en float32, en utilisant le bounding box TLE comme transformation affine
2. WHERE l'option GeoTIFF est activée pour le chemin NASA, THE GeoTIFF_Exporter SHALL interpoler les données du swath curviligne sur une grille régulière via interpolation linéaire (scipy griddata) avec une résolution de ~0.0067°
3. THE GeoTIFF_Exporter SHALL nommer le fichier GeoTIFF selon le pattern `viirs_{composite_type}_{identifier}.tif`

### Requirement 13: Stockage et organisation des produits de sortie dans S3

**User Story:** En tant qu'opérateur du pipeline, je veux que les produits de sortie des deux chemins soient organisés de manière prévisible et cohérente dans S3, afin de faciliter la consultation et l'archivage.

#### Acceptance Criteria

1. THE Visualization_Pipeline SHALL stocker les produits de sortie dans le bucket S3 sous le préfixe `products/{YYYY}/{MM}/{DD}/{pass_id}/`
2. THE Visualization_Pipeline SHALL nommer le fichier PNG selon le pattern `viirs_{path}_{composite_type}_{pass_id}.png` où `path` est "satdump" ou "nasa"
3. THE Visualization_Pipeline SHALL nommer le fichier de métadonnées JSON selon le pattern `viirs_{path}_{composite_type}_{pass_id}.json`
4. WHERE l'option GeoTIFF est activée, THE Visualization_Pipeline SHALL nommer le fichier GeoTIFF selon le pattern `viirs_{path}_{composite_type}_{pass_id}.tif`
5. THE Visualization_Pipeline SHALL produire un format de sortie identique (PNG géoréférencé + JSON + GeoTIFF optionnel) quel que soit le chemin de visualisation utilisé
