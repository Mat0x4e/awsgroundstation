"""VIIRS satellite data processing package.

Provides calibration, geolocation, rendering, and export utilities for VIIRS
I-band and M-band data from both NASA CSPP and NASA science data formats.

Public API organized by functional domain:

- **Calibration & I/O**: CBORReader, GEOReader, SDRReader
- **Processing**: BTConverter, ImageRenderer, MetadataGenerator
- **Output**: CartopyRenderer, GeoTIFFExporter, SatDumpVisualizer
- **Models**: BoundingBox, CBORMetadata, CompositeInfo, NASAMetadata
- **Exceptions**: InvalidGEOFileError, InvalidSDRFileError, NoCompositesError
"""

from .bbox_calculator import BBoxCalculator
from .bt_converter import BTConverter
from .cartopy_renderer import CartopyRenderer
from .cbor_reader import CBORReader
from .geo_reader import GEOReader, InvalidGEOFileError
from .geotiff_exporter import GeoTIFFExporter
from .image_renderer import ImageRenderer
from .metadata_generator import MetadataGenerator
from .models import BoundingBox, CBORMetadata, CompositeInfo, NASAMetadata
from .satdump_visualizer import NoCompositesError, SatDumpVisualizer
from .sdr_reader import InvalidSDRFileError, SDRReader

__all__ = [
    # Existing modules — Data sources
    "CBORReader",
    "GEOReader",
    "SDRReader",
    # Existing modules — Processing
    "BBoxCalculator",
    "CartopyRenderer",
    "MetadataGenerator",
    "SatDumpVisualizer",
    # Existing modules — Output
    "GeoTIFFExporter",
    # New NASA path modules — Processing
    "BTConverter",
    "ImageRenderer",
    # Models
    "BoundingBox",
    "CBORMetadata",
    "CompositeInfo",
    "NASAMetadata",
    # Exceptions
    "InvalidGEOFileError",
    "InvalidSDRFileError",
    "NoCompositesError",
]
