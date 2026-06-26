"""SatDump composite PNG discovery and normalization.

Handles scanning a folder for recognized SatDump VIIRS composite files,
loading them via Pillow, and normalizing pixel values to float32 [0, 1]
with NaN masking for no-data pixels.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

from scripts.viirs.models import CompositeInfo


class NoCompositesError(Exception):
    """Raised when no recognized SatDump composites are found in a folder."""

    def __init__(self, folder: Path, found_files: list[str]) -> None:
        self.folder = folder
        self.found_files = found_files
        files_str = "\n  ".join(found_files) if found_files else "(empty folder)"
        super().__init__(
            f"No recognized SatDump composites found in '{folder}'.\n"
            f"Files present:\n  {files_str}"
        )


class SatDumpVisualizer:
    """Reads SatDump PNG composites and prepares them for Cartopy rendering.

    Supports discovery of recognized composite PNGs in a folder and
    normalization of pixel data to float32 arrays in [0, 1] with NaN
    masking for no-data pixels.
    """

    SUPPORTED_COMPOSITES: dict[str, str] = {
        "True Color": "viirs_rgb_True_Color.png",
        "Thermal IR": "viirs_10.8um_Thermal_IR_(Uncalibrated).png",
        "False Color": "viirs_rgb_False_Color.png",
        "Day Microphysics": "viirs_rgb_Day_Microphysics.png",
        "Night Microphysics": "viirs_rgb_Night_Microphysics.png",
        "Natural Color": "viirs_rgb_Natural_Color.png",
    }

    NODATA_THRESHOLD: float = 1e-6

    def discover_composites(self, folder: Path) -> list[CompositeInfo]:
        """Scan folder for recognized SatDump composite PNGs.

        Iterates over SUPPORTED_COMPOSITES and checks whether each expected
        file (or a glob-matched variant) exists in the folder. For each
        found composite, detects the bit depth from the Pillow image mode.

        Args:
            folder: Path to the folder containing SatDump outputs.

        Returns:
            List of CompositeInfo(path, composite_type, bit_depth) for each
            recognized composite found.

        Raises:
            NoCompositesError: If no recognized composites are found, listing
                all files present in the folder.
        """
        found: list[CompositeInfo] = []

        for composite_type, filename in self.SUPPORTED_COMPOSITES.items():
            # Use glob for flexibility — actual filenames may vary slightly.
            # Build a glob pattern from the filename (exact match first, then stem wildcard).
            stem = Path(filename).stem
            suffix = Path(filename).suffix

            # Try exact match first
            exact = folder / filename
            candidates = [exact] if exact.exists() else list(folder.glob(f"{stem}*{suffix}"))

            if not candidates:
                continue

            # Use the first match
            match_path = candidates[0]

            # Detect bit depth from Pillow mode
            with Image.open(match_path) as img:
                mode = img.mode

            bit_depth = 16 if mode == "I;16" else 8
            found.append(CompositeInfo(path=match_path, composite_type=composite_type, bit_depth=bit_depth))

        if not found:
            all_files = sorted(p.name for p in folder.iterdir() if p.is_file()) if folder.exists() else []
            raise NoCompositesError(folder=folder, found_files=all_files)

        return found

    def load_and_normalize(self, composite: CompositeInfo) -> np.ndarray:
        """Load a composite PNG and normalize pixel values to float32 [0, 1].

        Detects the image mode and applies the appropriate divisor:
        - ``I;16``: uint16 raw values divided by 65535 → shape (H, W)
        - ``RGB``: uint8 values divided by 255 → shape (H, W, 3)
        - ``L``:   uint8 values divided by 255 → shape (H, W)

        Pixels with normalized value < NODATA_THRESHOLD are replaced with
        NaN (no-data masking).

        Args:
            composite: CompositeInfo describing the PNG file to load.

        Returns:
            float32 numpy array of shape (H, W) for grayscale/thermal, or
            (H, W, 3) for RGB composites. No-data pixels are NaN.
        """
        with Image.open(composite.path) as img:
            mode = img.mode

            if mode == "I;16":
                # 16-bit grayscale: raw uint16 values, normalize by 65535
                raw = np.frombuffer(img.tobytes(), dtype=np.uint16).reshape(img.size[1], img.size[0])
                normalized = raw.astype(np.float32) / 65535.0

            elif mode == "RGB":
                # 8-bit RGB: normalize by 255, shape (H, W, 3)
                raw = np.array(img, dtype=np.uint8)
                normalized = raw.astype(np.float32) / 255.0

            elif mode == "L":
                # 8-bit grayscale: normalize by 255, shape (H, W)
                raw = np.array(img, dtype=np.uint8)
                normalized = raw.astype(np.float32) / 255.0

            else:
                # Attempt to convert unknown modes to the closest supported mode
                if img.mode in ("I", "F"):
                    # 32-bit integer or float — convert to 16-bit for consistent handling
                    converted = img.convert("I;16" if False else "L")  # fallback: L
                    raw = np.array(converted, dtype=np.uint8)
                    normalized = raw.astype(np.float32) / 255.0
                elif "A" in img.mode or img.mode == "RGBA":
                    # Strip alpha, treat as RGB
                    rgb = img.convert("RGB")
                    raw = np.array(rgb, dtype=np.uint8)
                    normalized = raw.astype(np.float32) / 255.0
                else:
                    # Generic fallback: convert to L (grayscale)
                    converted = img.convert("L")
                    raw = np.array(converted, dtype=np.uint8)
                    normalized = raw.astype(np.float32) / 255.0

        # Mask no-data pixels: values below threshold become NaN
        masked = np.where(normalized < self.NODATA_THRESHOLD, np.nan, normalized)

        return masked.astype(np.float32)
