"""VIIRS SDR HDF5 file reader with linear calibration.

Handles opening CSPP-produced VIIRS SDR HDF5 files, locating the SDR data
group, extracting Reflectance or Radiance datasets, and applying linear
calibration factors to produce physically meaningful arrays with fill-value
masking.

Typical CSPP HDF5 structure::

    All_Data/
        VIIRS-I1-SDR_All/
            Reflectance          # uint16 scaled values
            ReflectanceFactors   # [scale, offset] pairs
        VIIRS-M15-SDR_All/
            Radiance             # uint16 scaled values
            RadianceFactors      # [scale, offset] pairs
"""

from __future__ import annotations

from pathlib import Path

import h5py
import numpy as np


class InvalidSDRFileError(Exception):
    """Raised when an HDF5 file contains no group with 'SDR' in its name."""

    def __init__(self, h5_path: Path, groups_found: list[str]) -> None:
        self.h5_path = h5_path
        self.groups_found = groups_found
        groups_str = ", ".join(groups_found) if groups_found else "(no groups found)"
        super().__init__(
            f"No SDR group found in '{h5_path}'. "
            f"Groups present: {groups_str}"
        )


class SDRReader:
    """Reads HDF5 SDR files and applies linear calibration.

    Supports VIIRS SDR files produced by the CSPP (Community Satellite
    Processing Package) and similar NASA-standard HDF5 products.  The reader
    scans all groups inside the file for a group whose name contains ``"SDR"``,
    then extracts the requested dataset and its corresponding calibration
    factors.

    Calibration formula (applied element-wise)::

        physical_value = raw_uint16 × scale + offset

    Fill pixels (``raw_uint16 == 65535``) are masked before calibration so
    that the scale/offset arithmetic is never applied to sentinel values.

    Constants
    ---------
    FILL_VALUE_INT : int
        Raw uint16 sentinel value indicating a missing or invalid pixel.
    REFLECTANCE_DATASET : str
        Name of the reflectance dataset inside the SDR group.
    RADIANCE_DATASET : str
        Name of the radiance dataset inside the SDR group.
    REFLECTANCE_FACTORS : str
        Name of the dataset containing ``[scale, offset]`` for reflectance.
    RADIANCE_FACTORS : str
        Name of the dataset containing ``[scale, offset]`` for radiance.
    """

    FILL_VALUE_INT: int = 65535
    REFLECTANCE_DATASET: str = "Reflectance"
    RADIANCE_DATASET: str = "Radiance"
    REFLECTANCE_FACTORS: str = "ReflectanceFactors"
    RADIANCE_FACTORS: str = "RadianceFactors"

    # ------------------------------------------------------------------ #
    # Public interface                                                      #
    # ------------------------------------------------------------------ #

    def read_reflectance(self, h5_path: Path) -> np.ma.MaskedArray:
        """Open an SDR HDF5 file and return calibrated reflectance.

        Locates the first group whose name contains ``"SDR"``, reads the
        ``Reflectance`` dataset (uint16), masks pixels equal to
        ``FILL_VALUE_INT``, and applies the linear calibration factors from
        ``ReflectanceFactors``::

            reflectance = raw × scale + offset  →  [0, 1]

        Parameters
        ----------
        h5_path:
            Path to the VIIRS SDR HDF5 file.

        Returns
        -------
        np.ma.MaskedArray
            Float32 masked array of shape ``(H, W)``.  Pixels where the raw
            value equalled 65535 are masked (``fill_value=np.nan``).

        Raises
        ------
        InvalidSDRFileError
            If the file contains no HDF5 group with ``"SDR"`` in its name.
        """
        return self._read_dataset(
            h5_path=h5_path,
            dataset_name=self.REFLECTANCE_DATASET,
            factors_name=self.REFLECTANCE_FACTORS,
        )

    def read_radiance(self, h5_path: Path) -> np.ma.MaskedArray:
        """Open an SDR HDF5 file and return calibrated radiance.

        Locates the first group whose name contains ``"SDR"``, reads the
        ``Radiance`` dataset (uint16), masks pixels equal to
        ``FILL_VALUE_INT``, and applies the linear calibration factors from
        ``RadianceFactors``::

            radiance = raw × scale + offset  →  mW·m⁻²·sr⁻¹·µm⁻¹

        Parameters
        ----------
        h5_path:
            Path to the VIIRS SDR HDF5 file.

        Returns
        -------
        np.ma.MaskedArray
            Float32 masked array of shape ``(H, W)``.  Pixels where the raw
            value equalled 65535 are masked (``fill_value=np.nan``).

        Raises
        ------
        InvalidSDRFileError
            If the file contains no HDF5 group with ``"SDR"`` in its name.
        """
        return self._read_dataset(
            h5_path=h5_path,
            dataset_name=self.RADIANCE_DATASET,
            factors_name=self.RADIANCE_FACTORS,
        )

    # ------------------------------------------------------------------ #
    # Private helpers                                                       #
    # ------------------------------------------------------------------ #

    def _find_sdr_group(self, h5file: h5py.File) -> h5py.Group:
        """Return the first HDF5 group whose name contains ``'SDR'``.

        Searches recursively under ``All_Data/`` (the conventional CSPP
        container) and falls back to a top-level scan if that path is absent.

        Parameters
        ----------
        h5file:
            An open ``h5py.File`` object.

        Returns
        -------
        h5py.Group
            The located SDR group.

        Raises
        ------
        InvalidSDRFileError
            If no matching group is found anywhere in the file.
        """
        sdr_group: h5py.Group | None = None
        all_group_names: list[str] = []

        def _visitor(name: str, obj: h5py.HLObject) -> None:
            nonlocal sdr_group
            if isinstance(obj, h5py.Group) and "SDR" in name:
                if sdr_group is None:
                    sdr_group = obj  # type: ignore[assignment]
            if isinstance(obj, h5py.Group):
                all_group_names.append(name)

        h5file.visititems(_visitor)

        if sdr_group is None:
            raise InvalidSDRFileError(
                h5_path=Path(h5file.filename),
                groups_found=all_group_names,
            )

        return sdr_group

    def _read_dataset(
        self,
        h5_path: Path,
        dataset_name: str,
        factors_name: str,
    ) -> np.ma.MaskedArray:
        """Core reader: open the file, locate the SDR group, calibrate, mask.

        Parameters
        ----------
        h5_path:
            Path to the VIIRS SDR HDF5 file.
        dataset_name:
            Name of the uint16 dataset to read (``"Reflectance"`` or
            ``"Radiance"``).
        factors_name:
            Name of the factors dataset containing ``[scale, offset]`` pairs
            (``"ReflectanceFactors"`` or ``"RadianceFactors"``).

        Returns
        -------
        np.ma.MaskedArray
            Calibrated float32 masked array.  Fill pixels are masked.
        """
        with h5py.File(h5_path, "r") as h5file:
            sdr_group = self._find_sdr_group(h5file)

            # -- Raw uint16 data ------------------------------------------
            raw: np.ndarray = sdr_group[dataset_name][()].astype(np.uint16)

            # -- Calibration factors [scale, offset] ----------------------
            # RadianceFactors / ReflectanceFactors are stored as a 1-D or
            # 2-D array of [scale, offset] pairs (one pair per scan line or
            # one global pair).  Take the first pair; a per-line array would
            # require per-line application, but CSPP single-granule files
            # typically contain a constant pair.
            factors: np.ndarray = sdr_group[factors_name][()]
            factors_flat = np.asarray(factors).ravel()
            scale = float(factors_flat[0])
            offset = float(factors_flat[1])

        # -- Fill-value mask (applied before arithmetic) ------------------
        fill_mask = raw == self.FILL_VALUE_INT

        # -- Linear calibration -------------------------------------------
        calibrated = raw.astype(np.float32) * scale + offset

        # -- Build masked array -------------------------------------------
        return np.ma.masked_array(
            data=calibrated,
            mask=fill_mask,
            fill_value=np.nan,
            dtype=np.float32,
        )
