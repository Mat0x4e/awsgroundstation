"""VIIRS GEO HDF5 file reader for per-pixel geolocation.

Handles opening CSPP-produced VIIRS GEO HDF5 files (GIGTO for I-band,
GMODO for M-band), extracting Latitude and Longitude datasets, and
masking invalid pixels (fill values below -900).

Typical CSPP HDF5 structure::

    All_Data/
        VIIRS-IMG-GEO_All/
            Latitude    # float32 per-pixel latitudes
            Longitude   # float32 per-pixel longitudes
        VIIRS-MOD-GEO_All/
            Latitude    # float32 per-pixel latitudes
            Longitude   # float32 per-pixel longitudes
"""

from __future__ import annotations

from pathlib import Path

import h5py
import numpy as np


class InvalidGEOFileError(Exception):
    """Raised when an HDF5 file does not contain the expected GEO group."""

    def __init__(self, h5_path: Path, expected_group: str, groups_found: list[str]) -> None:
        self.h5_path = h5_path
        self.expected_group = expected_group
        self.groups_found = groups_found
        groups_str = ", ".join(groups_found) if groups_found else "(no groups found)"
        super().__init__(
            f"Expected GEO group '{expected_group}' not found in '{h5_path}'. "
            f"Groups present: {groups_str}"
        )


class GEOReader:
    """Reads HDF5 geolocation files for per-pixel lat/lon.

    Supports VIIRS GEO files produced by the CSPP (Community Satellite
    Processing Package) and similar NASA-standard HDF5 products.  The
    reader locates the expected group inside ``All_Data/``, extracts
    the ``Latitude`` and ``Longitude`` datasets as float32, and masks
    any pixel whose value falls below ``INVALID_THRESHOLD`` (-900.0).

    Constants
    ---------
    IBAND_GROUP : str
        HDF5 group name for I-band geolocation (GIGTO files).
    MBAND_GROUP : str
        HDF5 group name for M-band geolocation (GMODO files).
    INVALID_THRESHOLD : float
        Pixel values below this threshold are masked as invalid.
        NASA VIIRS GEO fill values are typically -999.3 or -999.9.
    """

    IBAND_GROUP: str = "VIIRS-IMG-GEO_All"
    MBAND_GROUP: str = "VIIRS-MOD-GEO_All"
    INVALID_THRESHOLD: float = -900.0

    # ------------------------------------------------------------------ #
    # Public interface                                                      #
    # ------------------------------------------------------------------ #

    def read_iband(
        self, h5_path: Path
    ) -> tuple[np.ma.MaskedArray, np.ma.MaskedArray]:
        """Open a GIGTO GEO HDF5 file and return I-band lat/lon arrays.

        Extracts ``Latitude`` and ``Longitude`` from the
        ``VIIRS-IMG-GEO_All`` group, casts to float32, and masks pixels
        whose value is less than -900.  The resulting arrays match the
        spatial dimensions of the corresponding I-band SDR data.

        Parameters
        ----------
        h5_path:
            Path to the VIIRS I-band GEO HDF5 file (GIGTO).

        Returns
        -------
        tuple[np.ma.MaskedArray, np.ma.MaskedArray]
            ``(lat, lon)`` — float32 masked arrays of shape ``(H, W)``.
            Pixels where the raw value was below -900 are masked
            (``fill_value=np.nan``).

        Raises
        ------
        InvalidGEOFileError
            If the file does not contain the ``VIIRS-IMG-GEO_All`` group
            under ``All_Data/``.
        """
        return self._read_latlon(h5_path, self.IBAND_GROUP)

    def read_mband(
        self, h5_path: Path
    ) -> tuple[np.ma.MaskedArray, np.ma.MaskedArray]:
        """Open a GMODO GEO HDF5 file and return M-band lat/lon arrays.

        Extracts ``Latitude`` and ``Longitude`` from the
        ``VIIRS-MOD-GEO_All`` group, casts to float32, and masks pixels
        whose value is less than -900.  The resulting arrays match the
        spatial dimensions of the corresponding M-band SDR data.

        Parameters
        ----------
        h5_path:
            Path to the VIIRS M-band GEO HDF5 file (GMODO).

        Returns
        -------
        tuple[np.ma.MaskedArray, np.ma.MaskedArray]
            ``(lat, lon)`` — float32 masked arrays of shape ``(H, W)``.
            Pixels where the raw value was below -900 are masked
            (``fill_value=np.nan``).

        Raises
        ------
        InvalidGEOFileError
            If the file does not contain the ``VIIRS-MOD-GEO_All`` group
            under ``All_Data/``.
        """
        return self._read_latlon(h5_path, self.MBAND_GROUP)

    # ------------------------------------------------------------------ #
    # Private helpers                                                       #
    # ------------------------------------------------------------------ #

    def _find_geo_group(self, h5file: h5py.File, group_name: str) -> h5py.Group:
        """Return the HDF5 group matching *group_name* inside ``All_Data/``.

        Searches recursively through the file for a group whose name
        ends with *group_name* (the CSPP convention places it under
        ``All_Data/<group_name>``).

        Parameters
        ----------
        h5file:
            An open ``h5py.File`` object.
        group_name:
            The target group name, e.g. ``"VIIRS-IMG-GEO_All"``.

        Returns
        -------
        h5py.Group
            The located GEO group.

        Raises
        ------
        InvalidGEOFileError
            If no group matching *group_name* is found in the file.
        """
        geo_group: h5py.Group | None = None
        all_group_names: list[str] = []

        def _visitor(name: str, obj: h5py.HLObject) -> None:
            nonlocal geo_group
            if isinstance(obj, h5py.Group):
                all_group_names.append(name)
                # Match on the final path component so "All_Data/VIIRS-IMG-GEO_All"
                # is found whether or not there are additional path prefixes.
                if name.split("/")[-1] == group_name and geo_group is None:
                    geo_group = obj  # type: ignore[assignment]

        h5file.visititems(_visitor)

        if geo_group is None:
            raise InvalidGEOFileError(
                h5_path=Path(h5file.filename),
                expected_group=group_name,
                groups_found=all_group_names,
            )

        return geo_group

    def _read_latlon(
        self, h5_path: Path, group_name: str
    ) -> tuple[np.ma.MaskedArray, np.ma.MaskedArray]:
        """Core reader: open the file, locate the GEO group, mask, return arrays.

        Parameters
        ----------
        h5_path:
            Path to the VIIRS GEO HDF5 file.
        group_name:
            Target HDF5 group name (``IBAND_GROUP`` or ``MBAND_GROUP``).

        Returns
        -------
        tuple[np.ma.MaskedArray, np.ma.MaskedArray]
            ``(lat, lon)`` as float32 masked arrays.  Pixels below
            ``INVALID_THRESHOLD`` are masked with ``fill_value=np.nan``.
        """
        with h5py.File(h5_path, "r") as h5file:
            geo_group = self._find_geo_group(h5file, group_name)

            # Read raw arrays and cast to float32 immediately.
            lat_raw: np.ndarray = geo_group["Latitude"][()].astype(np.float32)
            lon_raw: np.ndarray = geo_group["Longitude"][()].astype(np.float32)

        # Build invalid-pixel masks: any value below the threshold is fill data.
        lat_mask = lat_raw < self.INVALID_THRESHOLD
        lon_mask = lon_raw < self.INVALID_THRESHOLD

        lat = np.ma.masked_array(
            data=lat_raw,
            mask=lat_mask,
            fill_value=np.nan,
            dtype=np.float32,
        )
        lon = np.ma.masked_array(
            data=lon_raw,
            mask=lon_mask,
            fill_value=np.nan,
            dtype=np.float32,
        )

        return lat, lon
