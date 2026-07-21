# Design Document: NOAA-20 VIIRS Visualization Pipeline

## Overview

Pipeline de post-traitement serverless qui transforme les sorties du pipeline amont (`noaa20-cadu-to-tiff`) en images géoréférencées PNG annotées (Cartopy) et GeoTIFF (EPSG:4326), accompagnées de métadonnées JSON. Le pipeline supporte deux chemins de visualisation parallèles selon le type de données disponibles.

### Décisions de conception clés

1. **Lambda orchestratrice + CodeBuild compute** — Lambda détecte le chemin (SatDump vs NASA) et soumet le job CodeBuild approprié. CodeBuild `BUILD_GENERAL1_MEDIUM` (4 vCPU, 7 GB RAM) suffit pour le rendu Cartopy + traitement d'image.

2. **Un seul CodeBuild project, deux buildspecs** — Même image Docker avec toutes les dépendances Python. Le buildspec est sélectionné dynamiquement par la Lambda selon le chemin détecté.

3. **Bucket de sortie dédié `products/`** — Les visualisations sont stockées dans le même bucket SDR output sous le préfixe `products/` pour éviter la multiplication des buckets.

4. **Détection de chemin par pattern S3** — La Lambda inspecte les fichiers disponibles dans le dossier contact : présence de `viirs_rgb_*.png` → SatDump ; présence de `SVI0*_npp_*.h5` → NASA.

5. **Composants partagés** — Le Cartopy_Renderer et le Metadata_Generator sont des modules Python réutilisés par les deux chemins. Seule la source de données et le calcul de géolocalisation diffèrent.

6. **GeoTIFF optionnel via variable d'environnement** — `ENABLE_GEOTIFF=true|false` contrôle la production du GeoTIFF sans modifier le code.

7. **Déclenchement par EventBridge sur le bucket SDR output** — Quand le pipeline amont dépose ses résultats (manifest.json ou composites PNG), EventBridge déclenche la Lambda orchestratrice.

### Position dans la chaîne de traitement

```
[Spec amont: noaa20-cadu-to-tiff — module sdr_pipeline]
        │
        ▼
S3: {project}-sdr-output-{account_id}/contacts/{date}/{contact_id}/
  ├── chunks/chunk_XXX/  (SDR HDF5 + GEO HDF5 + dataset.json)
  ├── coordinates/       (geolocation JSON per chunk)
  ├── satdump/           (composites PNG + product.cbor)
  └── manifest.json
        │
        ▼  EventBridge ObjectCreated (manifest.json ou composites PNG)
        │
[CETTE SPEC — viirs_visualization module]
  Lambda orchestratrice → CodeBuild → S3 products/
```

## Architecture

```mermaid
flowchart TD
    A[S3: SDR Output Bucket<br/>contacts/{date}/{contact_id}/] -->|EventBridge<br/>ObjectCreated: manifest.json| B[Lambda: Visualization Orchestrator<br/>Python 3.12, 512 MB, 60s timeout]

    B -->|Detect path:<br/>SatDump composites?| C{Path Detection}
    C -->|viirs_rgb_*.png found| D[CodeBuild: SatDump Visualization<br/>BUILD_GENERAL1_MEDIUM]
    C -->|SVI0*_npp_*.h5 found| E[CodeBuild: NASA Visualization<br/>BUILD_GENERAL1_MEDIUM]
    C -->|Neither found| F[Log warning + skip]

    subgraph "CodeBuild — SatDump Path"
        D --> D1[CBOR_Reader<br/>product.cbor → metadata]
        D1 --> D2[BBox_Calculator<br/>TLE/sgp4 or .georef → bbox]
        D2 --> D3[SatDump_Visualizer<br/>PNG composites → normalized arrays]
        D3 --> D4[Cartopy_Renderer<br/>Overlay coastlines, borders, grid, POI]
        D4 --> D5[Metadata_Generator<br/>JSON sidecar]
        D5 --> D6[GeoTIFF_Exporter<br/>Optional: EPSG:4326 export]
    end

    subgraph "CodeBuild — NASA Path"
        E --> E1[SDR_Reader<br/>HDF5 → calibrated arrays]
        E1 --> E2[GEO_Reader<br/>HDF5 → lat/lon per pixel]
        E2 --> E3[BT_Converter<br/>Radiance → Brightness Temp]
        E3 --> E4[Image_Renderer<br/>Gamma + Destripe + RGB assembly]
        E4 --> E5[Cartopy_Renderer<br/>Per-pixel geolocation overlay]
        E5 --> E6[Metadata_Generator<br/>JSON sidecar]
        E6 --> E7[GeoTIFF_Exporter<br/>Optional: interpolated grid]
    end

    D6 --> G[S3: products/{YYYY}/{MM}/{DD}/{pass_id}/]
    E7 --> G

    B -->|On failure| H[CloudWatch Logs<br/>Error context + input files]
```

### Ressources AWS existantes utilisées

| Ressource existante | Utilisation |
|---|---|
| `module.sdr_pipeline` (SDR output bucket) | Source — composites SatDump + HDF5 SDR/GEO |
| `module.security` (KMS CMK) | Chiffrement au repos des sorties |
| `module.security` (SNS topic) | Notifications d'échec |
| `module.observability` | Métriques CloudWatch |

### Nouvelles ressources AWS

| Ressource | Objectif |
|---|---|
| Lambda Function (orchestratrice) | Détecte le chemin, soumet CodeBuild |
| CodeBuild Project | Exécute le rendu (image Docker Python + Cartopy) |
| EventBridge Rule | Capte ObjectCreated sur SDR output bucket |
| IAM Roles | Lambda execution role, CodeBuild service role |
| CloudWatch Log Groups | Logs Lambda + CodeBuild, rétention 90 jours |

## Components and Interfaces

### 1. Lambda Orchestratrice (path detection + CodeBuild invocation)

**Runtime**: Python 3.12, 512 MB, timeout 60s

Déclenché par EventBridge quand le pipeline amont dépose `manifest.json` ou des composites PNG dans le bucket SDR output.

```python
class VisualizationOrchestrator:
    """Detects visualization path and submits CodeBuild job."""

    SATDUMP_PATTERNS = ["viirs_rgb_", "viirs_*_Thermal_IR_"]
    NASA_PATTERNS = ["SVI0", "SVOM15"]

    def handle(self, event: dict) -> dict:
        """
        EventBridge event handler.
        1. Extract contact_id + contact_date from S3 key
        2. List objects in contact folder
        3. Detect path (SatDump vs NASA)
        4. Submit CodeBuild with appropriate env vars
        5. Return build ID for monitoring

        Returns: { "build_id": str, "path": "satdump"|"nasa", "contact_id": str }
        Raises: NoVisualizableDataError if neither path detected
        """

    def _detect_path(self, s3_keys: list[str]) -> str:
        """
        Returns "satdump" if any key matches SATDUMP_PATTERNS.
        Returns "nasa" if any key matches NASA_PATTERNS.
        Raises NoVisualizableDataError if neither.
        SatDump takes priority if both are present.
        """

    def _submit_codebuild(self, path: str, contact_id: str,
                          contact_date: str, input_prefix: str) -> str:
        """Starts CodeBuild with env overrides for the selected path."""
```

### 2. SatDump Visualizer (CodeBuild — chemin SatDump)

Lit les composites PNG SatDump, normalise, calcule le bounding box, et passe au Cartopy_Renderer.

```python
class SatDumpVisualizer:
    """Reads SatDump PNG composites and prepares them for Cartopy rendering."""

    SUPPORTED_COMPOSITES = {
        "True Color": "viirs_rgb_True_Color.png",
        "Thermal IR": "viirs_10.8um_Thermal_IR_(Uncalibrated).png",
        "False Color": "viirs_rgb_False_Color.png",
        "Day Microphysics": "viirs_rgb_Day_Microphysics.png",
        "Night Microphysics": "viirs_rgb_Night_Microphysics.png",
        "Natural Color": "viirs_rgb_Natural_Color.png",
    }
    NODATA_THRESHOLD = 1e-6

    def discover_composites(self, folder: Path) -> list[CompositeInfo]:
        """
        Scans folder for recognized composite PNGs.
        Returns list of CompositeInfo(path, composite_type, bit_depth).
        Raises NoCompositesError if none found (lists found files).
        """

    def load_and_normalize(self, composite: CompositeInfo) -> np.ndarray:
        """
        Loads PNG via Pillow.
        16-bit (mode I;16): divides by 65535 → [0, 1]
        8-bit (mode RGB/L): divides by 255 → [0, 1]
        Masks pixels < NODATA_THRESHOLD as NaN.
        Returns: float32 array shape (H, W) or (H, W, 3)
        """
```

### 3. CBOR Reader (extraction métadonnées SatDump)

```python
class CBORReader:
    """Reads SatDump product.cbor metadata file."""

    DEFAULT_SATELLITE = "NOAA-20"
    DEFAULT_DATETIME = "unknown"

    def read(self, folder: Path) -> CBORMetadata:
        """
        Searches folder (and subdirs) for product.cbor.
        Extracts: timestamp (UTC), satellite name, projection coordinates.
        Falls back to defaults if file missing or unparseable.

        Returns: CBORMetadata {
            timestamp: datetime | None,
            satellite: str,
            projection_coords: dict | None,
            raw_data: dict
        }
        """
```

### 4. BBox Calculator (géolocalisation par TLE/sgp4)

```python
class BBoxCalculator:
    """Computes geographic bounding box from TLE propagation or static sources."""

    NOAA20_NORAD_ID = 43013
    VIIRS_CROSS_TRACK_ANGLE_DEG = 56.0  # ±56°
    CELESTRAK_URL = "https://celestrak.org/NORAD/elements/gp.php?CATNR=43013&FORMAT=3LE"

    def compute(self, cbor_meta: CBORMetadata, folder: Path) -> BoundingBox:
        """
        Priority order:
        1. .georef file (corner coordinates) if present
        2. CBOR projection coordinates if present
        3. TLE + sgp4 propagation from timestamps

        Returns: BoundingBox(lat_min, lat_max, lon_min, lon_max) in WGS84 degrees.
        Raises: NoBBoxSourceError if no source available.
        """

    def _from_georef(self, georef_path: Path) -> BoundingBox:
        """Reads corner coords from .georef JSON file."""

    def _from_cbor_projection(self, coords: dict) -> BoundingBox:
        """Extracts bbox from CBOR projection metadata."""

    def _from_tle(self, timestamp: datetime) -> BoundingBox:
        """
        Fetches TLE from CelesTrak (fallback: embedded TLE).
        Propagates orbit via sgp4 for pass duration (~10 min).
        Extends nadir track by ±56° cross-track angle.
        """
```

### 5. Cartopy Renderer (composant partagé — les deux chemins)

```python
class CartopyRenderer:
    """Renders georeferenced PNG with cartographic overlays via Cartopy."""

    DPI = 300
    COASTLINE_RESOLUTION = "10m"
    COASTLINE_COLOR = "white"
    COASTLINE_WIDTH = 0.8
    BORDER_COLOR = "yellow"
    BORDER_WIDTH = 0.6
    BORDER_STYLE = "--"
    LAKE_ALPHA = 0.2
    GRID_ALPHA = 0.6
    GRID_COLOR = "white"
    GRID_STYLE = "--"
    POI_BG_ALPHA = 0.45
    MARGIN_DEG = 1.5

    # Grid step auto-selection
    GRID_STEPS = [(40, 10), (20, 5), (0, 2)]  # (span_threshold, step_degrees)

    # Thermal colormaps
    THERMAL_CMAP = "RdYlBu_r"
    THERMAL_COLORBAR_LABEL = "Valeur normalisée SatDump"
    NASA_THERMAL_RANGE = (210, 305)  # Kelvin

    def render_satdump(self, data: np.ndarray, bbox: BoundingBox,
                       composite_type: str, metadata: CBORMetadata,
                       output_path: Path) -> Path:
        """
        Renders SatDump composite with Cartopy overlays.
        - PlateCarree projection centered on bbox + 1.5° margin
        - Coastlines 10m white 0.8px, borders yellow dashed 0.6px, lakes alpha 0.2
        - Auto-step lat/lon grid
        - POI labels (white on semi-transparent black)
        - Title: {composite_type} — {datetime UTC} — {satellite} — calibration note
        - Thermal IR: RdYlBu_r colormap + colorbar
        - True/False Color: RGB with bilinear interpolation
        """

    def render_nasa(self, data: np.ndarray, lat: np.ndarray, lon: np.ndarray,
                    mode: str, metadata: NASAMetadata,
                    output_path: Path) -> Path:
        """
        Renders NASA SDR data with per-pixel geolocation.
        - Uses pcolormesh with lat/lon coordinates
        - Same overlay layers as SatDump
        - Title: {mode} — {datetime UTC} — Orbit {orbit_number} — {granule_id}
        - Thermal: dual-axis colorbar (K and °C), range 210–305K
        - True Color: RGB composite after gamma correction
        """

    def _auto_grid_step(self, bbox: BoundingBox) -> float:
        """Returns grid step in degrees based on bbox span."""

    def _add_poi_labels(self, ax, bbox: BoundingBox) -> None:
        """Annotates visible POI with white text on semi-transparent black background."""

    def _add_overlays(self, ax, bbox: BoundingBox) -> None:
        """Adds coastlines, borders, lakes, grid to the axes."""
```

### 6. SDR Reader (chemin NASA)

```python
class SDRReader:
    """Reads HDF5 SDR files and applies linear calibration."""

    FILL_VALUE_INT = 65535
    REFLECTANCE_DATASET = "Reflectance"
    RADIANCE_DATASET = "Radiance"

    def read_reflectance(self, h5_path: Path) -> np.ma.MaskedArray:
        """
        Opens HDF5, finds SDR group, extracts Reflectance dataset.
        Applies: value × scale + offset → [0, 1] reflectance.
        Masks pixels where raw value == 65535.
        Returns: masked float32 array.
        Raises: InvalidSDRFileError if no SDR group found.
        """

    def read_radiance(self, h5_path: Path) -> np.ma.MaskedArray:
        """
        Opens HDF5, finds SDR group, extracts Radiance dataset.
        Applies: value × scale + offset → mW·m⁻²·sr⁻¹·µm⁻¹.
        Masks pixels where raw value == 65535.
        Returns: masked float32 array.
        Raises: InvalidSDRFileError if no SDR group found.
        """
```

### 7. GEO Reader (chemin NASA)

```python
class GEOReader:
    """Reads HDF5 geolocation files for per-pixel lat/lon."""

    IBAND_GROUP = "VIIRS-IMG-GEO_All"
    MBAND_GROUP = "VIIRS-MOD-GEO_All"
    INVALID_THRESHOLD = -900.0

    def read_iband(self, h5_path: Path) -> tuple[np.ma.MaskedArray, np.ma.MaskedArray]:
        """
        Extracts Latitude/Longitude from VIIRS-IMG-GEO_All group.
        Masks values < -900.
        Returns: (lat_array, lon_array) as float32 masked arrays.
        """

    def read_mband(self, h5_path: Path) -> tuple[np.ma.MaskedArray, np.ma.MaskedArray]:
        """
        Extracts Latitude/Longitude from VIIRS-MOD-GEO_All group.
        Masks values < -900.
        Returns: (lat_array, lon_array) as float32 masked arrays.
        """
```

### 8. BT Converter (radiance → température de brillance)

```python
class BTConverter:
    """Converts spectral radiance to brightness temperature via inverse Planck."""

    C1 = 1.191042e8    # mW·µm⁴·m⁻²·sr⁻¹
    C2 = 1.4387752e4   # µm·K
    M15_WAVELENGTH = 10.7630  # µm (VIIRS M15 central wavelength)

    def convert(self, radiance: np.ma.MaskedArray,
                wavelength: float = M15_WAVELENGTH) -> np.ma.MaskedArray:
        """
        Applies inverse Planck law:
        BT = C2 / (λ × ln(C1 / (λ⁵ × L) + 1))

        Propagates mask from input radiance.
        Returns: brightness temperature in Kelvin (float32 masked array).
        """
```

### 9. Image Renderer (traitement d'image — chemin NASA)

```python
class ImageRenderer:
    """Applies contrast stretch, gamma correction, and destriping for NASA path."""

    GAMMA = 0.5
    PERCENTILE_LOW = 2
    PERCENTILE_HIGH = 98
    VIIRS_DETECTORS = 16

    def contrast_stretch(self, band: np.ma.MaskedArray) -> np.ndarray:
        """
        Clips to [p2, p98] percentile range, then scales to [0, 1].
        Returns: float32 array in [0, 1].
        """

    def gamma_correct(self, band: np.ndarray, gamma: float = GAMMA) -> np.ndarray:
        """Applies gamma correction: output = input^gamma."""

    def destripe(self, band: np.ndarray) -> np.ndarray:
        """
        Corrects inter-detector striping (16 detectors, cyclic period of 16 lines).
        For each detector d (0-15): subtracts column-wise median of rows d, d+16, d+32, ...
        Returns: corrected array.
        """

    def assemble_true_color(self, i1: np.ma.MaskedArray, i2: np.ma.MaskedArray,
                            i3: np.ma.MaskedArray) -> np.ndarray:
        """
        Assembles RGB from I1 (red), I2 (green), I3 (blue).
        Applies per-band: contrast_stretch → gamma_correct.
        Returns: (H, W, 3) float32 array in [0, 1].
        """
```

### 10. GeoTIFF Exporter (composant partagé — optionnel)

```python
class GeoTIFFExporter:
    """Exports data as GeoTIFF with EPSG:4326 CRS."""

    TARGET_CRS = "EPSG:4326"
    NASA_RESOLUTION_DEG = 0.0067  # ~750m

    def export_satdump(self, data: np.ndarray, bbox: BoundingBox,
                       output_path: Path) -> Path:
        """
        Exports SatDump data as GeoTIFF.
        - CRS EPSG:4326
        - Affine transform computed from bbox + image dimensions
        - RGB (3 bands float32) for True Color / False Color
        - Single band float32 for Thermal IR
        """

    def export_nasa(self, data: np.ndarray, lat: np.ndarray, lon: np.ndarray,
                    output_path: Path) -> Path:
        """
        Interpolates swath data onto regular lat/lon grid.
        - scipy.interpolate.griddata (linear method)
        - Resolution: ~0.0067° (~750m)
        - CRS EPSG:4326
        - Affine transform from grid bounds
        """
```

### 11. Metadata Generator (composant partagé)

```python
class MetadataGenerator:
    """Generates JSON metadata sidecar for each rendered image."""

    def generate_satdump(self, composite_type: str, satellite: str,
                         datetime_utc: str, bbox: BoundingBox,
                         output_png: Path) -> dict:
        """
        Returns dict with:
        - source: "SatDump"
        - satellite, datetime_utc, composite_type
        - bounding_box: {lat_min, lat_max, lon_min, lon_max}
        - calibration_note: "Communautaire SatDump — non certifiée NOAA"
        - visualization_path: "satdump"
        - output_file: relative path to PNG
        """

    def generate_nasa(self, granule_id: str, orbit_number: int,
                      datetime_utc: str, mode: str, bbox: BoundingBox,
                      swath_width_km: float, output_png: Path) -> dict:
        """
        Returns dict with:
        - granule_id, orbit_number, datetime_utc, mode
        - bounding_box: {lat_min, lat_max, lon_min, lon_max}
        - swath_width_km
        - visualization_path: "nasa"
        - output_file: relative path to PNG
        """

    def extract_nasa_metadata(self, sdr_h5_path: Path) -> NASAMetadata:
        """
        Extracts from HDF5 global attributes:
        - N_Granule_ID → granule_id
        - N_Beginning_Orbit_Number → orbit_number
        - N_Beginning_Time_IET → datetime (µs since 1958-01-01 → ISO 8601 UTC)
        Falls back to defaults if attributes missing.
        """
```

### 12. CodeBuild Environment

**Compute type**: `BUILD_GENERAL1_MEDIUM` (4 vCPU, 7 GB RAM, 120 GB disk)
**Image**: Custom Docker image (Python 3.12 + scientific stack)
**Timeout**: 15 minutes

```dockerfile
FROM python:3.12-slim

# System dependencies for Cartopy and GEOS
RUN apt-get update && apt-get install -y \
    libgeos-dev libproj-dev proj-data \
    libgdal-dev gdal-bin \
    && rm -rf /var/lib/apt/lists/*

# Python scientific stack
RUN pip install --no-cache-dir \
    cartopy==0.23.0 \
    matplotlib==3.9.0 \
    numpy==1.26.4 \
    Pillow==10.3.0 \
    cbor2==5.6.0 \
    sgp4==2.23 \
    h5py==3.11.0 \
    rasterio==1.3.10 \
    scipy==1.13.0 \
    boto3==1.34.0

# Natural Earth data for Cartopy (10m resolution)
RUN python -c "import cartopy; cartopy.io.shapereader.natural_earth(resolution='10m', category='physical', name='coastline')"
RUN python -c "import cartopy; cartopy.io.shapereader.natural_earth(resolution='10m', category='cultural', name='admin_0_boundary_lines_land')"

# Pipeline scripts
COPY scripts/ /opt/scripts/
RUN chmod +x /opt/scripts/*.py

ENV MPLBACKEND=Agg
```

### 13. Buildspec — SatDump Visualization

```yaml
version: 0.2

env:
  variables:
    ENABLE_GEOTIFF: "true"
    MPLBACKEND: "Agg"

phases:
  pre_build:
    commands:
      - echo "Downloading SatDump outputs from S3..."
      - mkdir -p /tmp/input /tmp/output
      - aws s3 sync "s3://${INPUT_BUCKET}/${INPUT_PREFIX}/satdump/" /tmp/input/satdump/ --exclude "*" --include "*.png" --include "*.cbor" --include "*.georef"
      - aws s3 sync "s3://${INPUT_BUCKET}/${INPUT_PREFIX}/coordinates/" /tmp/input/coordinates/

  build:
    commands:
      - echo "Running SatDump visualization pipeline..."
      - python3 /opt/scripts/visualize_satdump.py
          --input-dir /tmp/input/satdump
          --coordinates-dir /tmp/input/coordinates
          --output-dir /tmp/output
          --contact-id "${CONTACT_ID}"
          --contact-date "${CONTACT_DATE}"
          --enable-geotiff "${ENABLE_GEOTIFF}"

  post_build:
    commands:
      - echo "Uploading products to S3..."
      - aws s3 sync /tmp/output/ "s3://${INPUT_BUCKET}/products/${CONTACT_DATE}/${CONTACT_ID}/"
          --sse aws:kms --sse-kms-key-id "${KMS_KEY_ID}"
      - echo "Visualization complete"
```

### 14. Buildspec — NASA Visualization

```yaml
version: 0.2

env:
  variables:
    ENABLE_GEOTIFF: "true"
    ENABLE_DESTRIPE: "true"
    MPLBACKEND: "Agg"

phases:
  pre_build:
    commands:
      - echo "Downloading SDR + GEO files from S3..."
      - mkdir -p /tmp/input /tmp/output
      - aws s3 sync "s3://${INPUT_BUCKET}/${INPUT_PREFIX}/chunks/" /tmp/input/chunks/
          --exclude "*" --include "SVI0*.h5" --include "SVOM15*.h5"
          --include "GIGTO*.h5" --include "GMODO*.h5"

  build:
    commands:
      - echo "Running NASA visualization pipeline..."
      - python3 /opt/scripts/visualize_nasa.py
          --input-dir /tmp/input/chunks
          --output-dir /tmp/output
          --contact-id "${CONTACT_ID}"
          --contact-date "${CONTACT_DATE}"
          --enable-geotiff "${ENABLE_GEOTIFF}"
          --enable-destripe "${ENABLE_DESTRIPE}"

  post_build:
    commands:
      - echo "Uploading products to S3..."
      - aws s3 sync /tmp/output/ "s3://${INPUT_BUCKET}/products/${CONTACT_DATE}/${CONTACT_ID}/"
          --sse aws:kms --sse-kms-key-id "${KMS_KEY_ID}"
      - echo "Visualization complete"
```

### 15. Terraform Module Structure

```
modules/viirs_visualization/
├── main.tf              # Lambda function + CloudWatch log groups
├── codebuild.tf         # CodeBuild project + buildspec references
├── eventbridge.tf       # EventBridge rule (ObjectCreated on SDR output bucket)
├── iam.tf               # Lambda execution role + CodeBuild service role
├── variables.tf         # Module inputs
├── outputs.tf           # Module outputs (Lambda ARN, CodeBuild project name)
└── ecr.tf               # ECR repository for visualization Docker image
```

**Module interface (variables.tf)**:
```hcl
variable "project_name" { type = string }
variable "account_id" { type = string }
variable "sdr_output_bucket_name" { type = string }
variable "sdr_output_bucket_arn" { type = string }
variable "kms_key_arn" { type = string }
variable "kms_key_id" { type = string }
variable "sns_topic_arn" { type = string }
variable "enable_geotiff" { type = bool, default = true }
variable "tags" { type = map(string), default = {} }
```

**Integration in main.tf** (root):
```hcl
module "viirs_visualization" {
  count  = var.enable_sdr_pipeline ? 1 : 0
  source = "./modules/viirs_visualization"

  project_name           = var.project_name
  account_id             = data.aws_caller_identity.current.account_id
  sdr_output_bucket_name = module.sdr_pipeline[0].output_bucket_name
  sdr_output_bucket_arn  = module.sdr_pipeline[0].output_bucket_arn
  kms_key_arn            = module.security.kms_key_arn
  kms_key_id             = module.security.kms_key_id
  sns_topic_arn          = module.security.sns_topic_arn
  tags                   = local.common_tags
}
```

## Data Models

### S3 Output Layout

```
{project}-sdr-output-{account_id}/
  products/
    {YYYY}/
      {MM}/
        {DD}/
          {pass_id}/
            viirs_satdump_true_color_{pass_id}.png
            viirs_satdump_true_color_{pass_id}.json
            viirs_satdump_true_color_{pass_id}.tif        # optional
            viirs_satdump_thermal_ir_{pass_id}.png
            viirs_satdump_thermal_ir_{pass_id}.json
            viirs_satdump_thermal_ir_{pass_id}.tif        # optional
            viirs_nasa_true_color_{pass_id}.png           # future
            viirs_nasa_true_color_{pass_id}.json          # future
            viirs_nasa_thermal_{pass_id}.png              # future
            viirs_nasa_thermal_{pass_id}.json             # future
```

### Metadata JSON Schema — SatDump Path

```json
{
  "source": "SatDump",
  "visualization_path": "satdump",
  "satellite": "NOAA-20",
  "datetime_utc": "2026-06-19T14:23:00Z",
  "composite_type": "True Color",
  "bounding_box": {
    "lat_min": 45.2,
    "lat_max": 55.8,
    "lon_min": 2.1,
    "lon_max": 18.5
  },
  "calibration_note": "Communautaire SatDump — non certifiée NOAA",
  "output_file": "viirs_satdump_true_color_abc123.png",
  "geotiff_file": "viirs_satdump_true_color_abc123.tif",
  "pipeline_version": "1.0.0",
  "processing_timestamp": "2026-06-19T15:45:00Z"
}
```

### Metadata JSON Schema — NASA Path

```json
{
  "source": "NASA CSPP SDR",
  "visualization_path": "nasa",
  "granule_id": "NPP002145678",
  "orbit_number": 65432,
  "datetime_utc": "2026-06-19T14:23:00Z",
  "mode": "True Color",
  "bounding_box": {
    "lat_min": 45.2,
    "lat_max": 55.8,
    "lon_min": 2.1,
    "lon_max": 18.5
  },
  "swath_width_km": 3060.0,
  "output_file": "viirs_nasa_true_color_abc123.png",
  "geotiff_file": "viirs_nasa_true_color_abc123.tif",
  "pipeline_version": "1.0.0",
  "processing_timestamp": "2026-06-19T15:45:00Z"
}
```

### BoundingBox Data Class

```python
@dataclass
class BoundingBox:
    lat_min: float  # degrees, WGS84
    lat_max: float  # degrees, WGS84
    lon_min: float  # degrees, WGS84
    lon_max: float  # degrees, WGS84

    def span_lat(self) -> float:
        return self.lat_max - self.lat_min

    def span_lon(self) -> float:
        return self.lon_max - self.lon_min

    def center(self) -> tuple[float, float]:
        return ((self.lat_min + self.lat_max) / 2,
                (self.lon_min + self.lon_max) / 2)

    def with_margin(self, margin_deg: float) -> "BoundingBox":
        return BoundingBox(
            lat_min=max(-90, self.lat_min - margin_deg),
            lat_max=min(90, self.lat_max + margin_deg),
            lon_min=max(-180, self.lon_min - margin_deg),
            lon_max=min(180, self.lon_max + margin_deg),
        )

    def to_dict(self) -> dict:
        return {"lat_min": self.lat_min, "lat_max": self.lat_max,
                "lon_min": self.lon_min, "lon_max": self.lon_max}
```

### NASAMetadata Data Class

```python
@dataclass
class NASAMetadata:
    granule_id: str = "unknown"
    orbit_number: int = 0
    datetime_utc: str = "unknown"

    # IET epoch: 1958-01-01T00:00:00Z
    IET_EPOCH = datetime(1958, 1, 1, tzinfo=timezone.utc)

    @classmethod
    def from_iet(cls, iet_microseconds: int, granule_id: str,
                 orbit_number: int) -> "NASAMetadata":
        dt = cls.IET_EPOCH + timedelta(microseconds=iet_microseconds)
        return cls(
            granule_id=granule_id,
            orbit_number=orbit_number,
            datetime_utc=dt.isoformat() + "Z",
        )
```

### Environment Variables (CodeBuild)

| Variable | Source | Description |
|---|---|---|
| `INPUT_BUCKET` | Terraform | SDR output bucket name |
| `INPUT_PREFIX` | Lambda env override | S3 prefix for contact (contacts/{date}/{id}) |
| `CONTACT_ID` | Lambda env override | Contact identifier (pass_id) |
| `CONTACT_DATE` | Lambda env override | Contact date (YYYY/MM/DD) |
| `KMS_KEY_ID` | Terraform | KMS key ID for SSE |
| `ENABLE_GEOTIFF` | Terraform | "true" or "false" |
| `ENABLE_DESTRIPE` | Terraform | "true" or "false" (NASA only) |
| `VIZ_PATH` | Lambda env override | "satdump" or "nasa" |
| `TLE_URL` | Terraform | CelesTrak endpoint |
| `TLE_FALLBACK` | Terraform | Inline fallback TLE |

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1: Normalization produces valid range and correct masking

*For any* PNG image array (8-bit or 16-bit), normalizing by the appropriate divisor (255 or 65535) SHALL produce all values in [0, 1], and subsequently masking pixels with value < 1e-6 SHALL mark exactly and only those pixels as no-data (NaN).

**Validates: Requirements 1.3, 1.4, 1.5**

### Property 2: CBOR metadata round-trip

*For any* valid CBOR metadata dictionary containing timestamp, satellite name, and projection coordinates, serializing with cbor2 then reading with the CBOR_Reader SHALL produce identical values for all extracted fields.

**Validates: Requirements 2.1, 2.2, 2.3**

### Property 3: Bounding box validity invariant

*For any* valid TLE and timestamp range, the BBox_Calculator SHALL produce a bounding box where lat_min ≤ lat_max, lon_min ≤ lon_max, all latitudes are in [-90, 90], and all longitudes are in [-180, 180].

**Validates: Requirements 3.1, 3.6**

### Property 4: Swath bounding box contains nadir bounding box

*For any* nadir bounding box computed from a ground track, extending by the VIIRS cross-track angle (±56°) SHALL produce a swath bounding box that is a superset of the nadir bounding box (each bound is wider or equal).

**Validates: Requirements 3.2**

### Property 5: SDR linear calibration correctness and fill masking

*For any* array of uint16 values with known scale and offset, the SDR_Reader SHALL produce calibrated values equal to (raw × scale + offset) for all non-fill pixels, and SHALL mask all pixels where raw == 65535. The set of masked pixels in the output SHALL be exactly the set of fill-value pixels in the input.

**Validates: Requirements 6.1, 6.2, 6.3**

### Property 6: Inverse Planck monotonicity and mask propagation

*For any* two positive radiance values L1 < L2, the BT_Converter SHALL produce BT(L1) < BT(L2) (monotonically increasing). Additionally, *for any* masked radiance array, the mask positions in the brightness temperature output SHALL be identical to the mask positions in the input.

**Validates: Requirements 8.1, 8.2, 8.3**

### Property 7: Image processing output range invariant

*For any* input array of real-valued data, applying contrast_stretch (percentiles 2–98) followed by gamma_correct (γ=0.5) SHALL produce all output values in [0, 1].

**Validates: Requirements 8.4**

### Property 8: Destriping eliminates detector bias

*For any* input array with dimensions divisible by 16 (VIIRS detector count), after applying the Destriper, the per-detector column-wise median SHALL be approximately zero (|median| < ε where ε accounts for floating-point precision).

**Validates: Requirements 8.5**

### Property 9: Path detection correctness

*For any* set of S3 keys containing at least one file matching `viirs_rgb_*.png` or `viirs_*_Thermal_IR_*.png`, the path detector SHALL return "satdump". *For any* set of S3 keys containing at least one file matching `SVI0*_npp_*.h5` or `SVOM15_npp_*.h5` but no SatDump patterns, the path detector SHALL return "nasa". SatDump takes priority when both patterns are present.

**Validates: Requirements 11.1, 11.2**

### Property 10: Metadata JSON completeness

*For any* valid combination of inputs (satellite, datetime, composite_type, bounding box for SatDump; granule_id, orbit_number, datetime, mode, bbox for NASA), the Metadata_Generator SHALL produce a JSON dict containing all required fields with non-empty values and the correct `visualization_path` discriminator.

**Validates: Requirements 5.1, 10.1**

### Property 11: Output file naming pattern consistency

*For any* valid combination of (path ∈ {"satdump", "nasa"}, composite_type, pass_id), the output PNG filename SHALL match the pattern `viirs_{path}_{composite_type}_{pass_id}.png`, the JSON SHALL have extension `.json` with the same base name, and the optional GeoTIFF SHALL have extension `.tif` with the same base name.

**Validates: Requirements 13.2, 13.3, 13.4**

### Property 12: IET timestamp conversion round-trip

*For any* integer representing microseconds since 1958-01-01T00:00:00Z (IET format) within a valid range (1958–2100), converting to ISO 8601 UTC string and parsing back SHALL produce a datetime within 1 microsecond of the original IET value.

**Validates: Requirements 10.3**

## Error Handling

| Scénario d'erreur | Détection | Réponse |
|---|---|---|
| Aucun composite PNG reconnu dans le dossier SatDump | Pattern scan vide | Erreur descriptive listant les fichiers trouvés |
| `product.cbor` absent ou illisible | FileNotFound / cbor2.CBORDecodeError | Warning log, continue avec métadonnées par défaut |
| Aucune source de géolocalisation disponible | BBoxCalculator checks | Erreur, pipeline s'arrête pour ce contact |
| Fichier `.georef` malformé | JSON parse error | Warning, fallback vers TLE/CBOR |
| CelesTrak inaccessible | HTTP timeout/error | Fallback TLE embarqué, warning log |
| Fichier SDR sans groupe HDF5 "SDR" | Group scan | Erreur descriptive identifiant le fichier |
| GEO file dimensions != SDR dimensions | Shape comparison | Erreur, skip cette granule |
| Fill values dominant (>95% du swath) | Pixel count | Warning log, produce image avec mention "mostly fill" |
| Radiance valeur négative (après calibration) | Value check | Masquer comme no-data (invalide physiquement) |
| CodeBuild timeout (>15 min) | Build timeout | Log erreur, pas de retry (données non urgentes) |
| Lambda ne trouve ni SatDump ni NASA | Path detection | Log warning, exit graceful (pas d'erreur fatale) |
| Erreur matplotlib/Cartopy (mémoire, projection) | Exception catch | Log stack trace + context, exit 1 |
| Écriture S3 échoue | boto3 exception | Retry SDK standard (3 tentatives), puis fail |
| KMS decrypt/encrypt échec | KMS exception | Log + fail (problème de permissions) |

### Stratégie de retry

| Composant | Max retries | Notes |
|---|---|---|
| CodeBuild visualization | 0 | Pas de retry — données non urgentes, retry manuel possible |
| CelesTrak HTTP | 3 | Linear backoff (5s, 10s, 15s), fallback TLE |
| S3 read/write | 3 | SDK standard exponential backoff |
| Lambda invocation | 2 | EventBridge retry policy |

### Philosophie

Le pipeline de visualisation est **non-critique** — un échec de rendu n'empêche pas la conservation des données brutes (SDR/GEO). La stratégie privilégie la résilience avec dégradation gracieuse (métadonnées par défaut, skip de granules problématiques) plutôt que l'échec total. Les erreurs sont loggées dans CloudWatch pour diagnostic post-hoc.

## Testing Strategy

### Approche duale

- **Tests unitaires (pytest)** : Vérifient la logique métier des composants Python (normalisation, CBOR, bbox, calibration, Planck, metadata)
- **Tests property-based (Hypothesis)** : Vérifient les propriétés universelles (normalisation, calibration, monotonicity, path detection, naming)
- **Tests d'intégration** : Vérifient l'exécution bout-en-bout avec Cartopy, rasterio, et fichiers réels
- **Tests d'infrastructure (Terraform)** : `terraform validate` + Checkov

### Property-Based Tests (Hypothesis)

Property-based testing est approprié pour ce pipeline — les composants de normalisation, calibration, conversion de température, détection de chemin, et génération de noms sont des fonctions pures avec un espace d'entrée large.

**Library**: [Hypothesis](https://hypothesis.readthedocs.io/) pour Python 3.12
**Configuration**: Minimum 100 itérations par test de propriété (`@settings(max_examples=100)`)
**Tag format**: `Feature: noaa20-viirs-visualization, Property {N}: {property_text}`

Properties testées :
- Property 1: Normalization + nodata masking (uint8/uint16 → [0, 1], mask < 1e-6)
- Property 2: CBOR metadata round-trip (serialize → read → identical fields)
- Property 3: BBox validity invariant (lat/lon ranges, min ≤ max)
- Property 4: Swath extension containment (swath bbox ⊇ nadir bbox)
- Property 5: SDR calibration + fill masking (linear transform, fill → masked)
- Property 6: Planck inversion monotonicity + mask propagation
- Property 7: Image processing output range [0, 1]
- Property 8: Destriping eliminates detector bias (per-detector median → ~0)
- Property 9: Path detection correctness (pattern priority)
- Property 10: Metadata completeness (all required fields present)
- Property 11: Output naming pattern consistency (pattern match)
- Property 12: IET timestamp conversion round-trip

### Unit Tests (pytest — example-based)

- SatDump composite discovery : known folder structures → expected composites
- CBOR Reader : missing file, corrupt file, valid file with various field names
- BBox Calculator : priority order (.georef > CBOR > TLE), error on no source
- Cartopy Renderer : output file exists, correct DPI, title contains expected text
- SDR Reader : invalid HDF5 (no SDR group), valid file → correct shape
- GEO Reader : specific HDF5 groups (VIIRS-IMG-GEO_All vs VIIRS-MOD-GEO_All)
- Metadata Generator : fixed fields (calibration_note, visualization_path)
- Lambda orchestrator : path detection with mixed file sets, error cases

### Integration Tests

- **End-to-end SatDump** : Real SatDump composite PNG + product.cbor → Cartopy rendered output exists + JSON sidecar valid
- **Cartopy rendering** : Verify output PNG dimensions, DPI (300), file size > 0
- **GeoTIFF export** : Verify CRS, transform, band count with rasterio.open()
- **Lambda + CodeBuild** : Mock CodeBuild client, verify correct env vars passed

### Infrastructure Tests (Terraform)

- `terraform validate` pour la syntaxe
- `checkov -d . --quiet` pour la sécurité (encryption, public access, IAM least privilege)
- Pas de PBT pour l'IaC (déclaratif — snapshot tests + policy checks)

### CloudWatch Metrics

| Metric | Namespace | Dimensions | Description |
|---|---|---|---|
| `VisualizationDuration` | `VIIRSVisualization` | contact_id, path | Durée totale du rendu (secondes) |
| `CompositesRendered` | `VIIRSVisualization` | contact_id, path | Nombre de composites rendus |
| `PathDetected` | `VIIRSVisualization` | path | Count par chemin détecté |
| `VisualizationErrors` | `VIIRSVisualization` | contact_id, error_type | Count d'erreurs par type |
| `GeoTIFFExported` | `VIIRSVisualization` | contact_id | Count de GeoTIFF produits |
