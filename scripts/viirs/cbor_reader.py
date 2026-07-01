"""CBOR metadata reader for SatDump product.cbor files.

Searches a folder (and subdirectories) for ``product.cbor``, parses it with
cbor2, and returns a :class:`CBORMetadata` dataclass.  The reader is
deliberately defensive: any missing file or parse failure is logged as a
warning and default values are returned — it never raises to callers.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

try:
    import cbor2
except ImportError:  # pragma: no cover
    cbor2 = None  # type: ignore[assignment]

from .models import CBORMetadata

logger = logging.getLogger(__name__)

_CBOR_FILENAME = "product.cbor"

# Fields tried in priority order for each piece of metadata
_TIMESTAMP_KEYS = ("timestamp", "start_timestamp")
_SATELLITE_KEYS = ("satellite", "sat_name")
_PROJECTION_KEYS = ("projection", "geo_correction")


class CBORReader:
    """Reads SatDump ``product.cbor`` metadata files.

    The reader searches *folder* and all of its subdirectories for the first
    ``product.cbor`` file it finds.  All extraction steps fall back to safe
    defaults on failure.
    """

    DEFAULT_SATELLITE: str = "NOAA-20"

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def read(self, folder: Path) -> CBORMetadata:
        """Search *folder* (recursively) for ``product.cbor`` and extract metadata.

        Returns a :class:`CBORMetadata` with populated fields where the data
        was found, and default values otherwise.  Never raises.

        Args:
            folder: Root directory to search.  Subdirectories are scanned
                    recursively via :py:func:`Path.rglob`.

        Returns:
            A :class:`CBORMetadata` instance.
        """
        cbor_path = self._find_cbor(folder)

        if cbor_path is None:
            logger.warning(
                "CBORReader: no %s found under %s — using defaults",
                _CBOR_FILENAME,
                folder,
            )
            return CBORMetadata()

        raw: dict = self._parse_cbor(cbor_path)
        if not raw:
            # _parse_cbor already logged the warning
            return CBORMetadata()

        ephemeris, scan_angle, image_width, projection_cfg = self._extract_projection_cfg(raw)

        return CBORMetadata(
            timestamp=self._extract_timestamp(raw),
            satellite=self._extract_satellite(raw),
            projection_coords=projection_cfg or self._extract_projection(raw),
            raw_data=raw,
            ephemeris=ephemeris,
            scan_angle=scan_angle,
            image_width=image_width,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _find_cbor(self, folder: Path) -> Path | None:
        """Return the first ``product.cbor`` found under *folder*, or ``None``."""
        try:
            matches = list(folder.rglob(_CBOR_FILENAME))
        except (OSError, PermissionError) as exc:
            logger.warning("CBORReader: could not search %s: %s", folder, exc)
            return None

        if not matches:
            return None

        if len(matches) > 1:
            logger.warning(
                "CBORReader: found %d %s files under %s — using %s",
                len(matches),
                _CBOR_FILENAME,
                folder,
                matches[0],
            )

        return matches[0]

    def _parse_cbor(self, path: Path) -> dict:
        """Parse *path* with cbor2 and return the decoded dict (empty on failure)."""
        if cbor2 is None:
            logger.warning(
                "CBORReader: cbor2 library is not installed — cannot parse %s", path
            )
            return {}

        try:
            with path.open("rb") as fh:
                data = cbor2.load(fh)
        except (OSError, PermissionError) as exc:
            logger.warning("CBORReader: cannot open %s: %s", path, exc)
            return {}
        except Exception as exc:  # noqa: BLE001 — cbor2 raises various exceptions
            logger.warning("CBORReader: failed to parse %s: %s", path, exc)
            return {}

        if not isinstance(data, dict):
            logger.warning(
                "CBORReader: expected dict in %s, got %s — using defaults",
                path,
                type(data).__name__,
            )
            return {}

        return data

    def _extract_timestamp(self, raw: dict) -> datetime | None:
        """Extract a UTC datetime from *raw*, trying :data:`_TIMESTAMP_KEYS` in order.

        Handles three cases returned by cbor2:

        * ``datetime`` — already parsed (CBOR tag 0/1); ensure UTC-aware.
        * ``int`` / ``float`` — Unix epoch seconds; convert to UTC datetime.
        * Anything else — log and return ``None``.
        """
        for key in _TIMESTAMP_KEYS:
            value = raw.get(key)
            if value is None:
                continue

            if isinstance(value, datetime):
                if value.tzinfo is None:
                    # Treat naive datetime as UTC
                    return value.replace(tzinfo=timezone.utc)
                return value.astimezone(timezone.utc)

            if isinstance(value, (int, float)):
                try:
                    return datetime.fromtimestamp(float(value), tz=timezone.utc)
                except (OSError, OverflowError, ValueError) as exc:
                    logger.warning(
                        "CBORReader: cannot convert %s=%r to datetime: %s",
                        key,
                        value,
                        exc,
                    )
                    return None

            logger.warning(
                "CBORReader: unexpected type for %s (%s) — ignoring",
                key,
                type(value).__name__,
            )
            return None

        # None of the timestamp keys were present
        logger.warning(
            "CBORReader: no timestamp field (%s) found in CBOR data",
            ", ".join(_TIMESTAMP_KEYS),
        )
        return None

    def _extract_satellite(self, raw: dict) -> str:
        """Extract the satellite name from *raw*, defaulting to :data:`DEFAULT_SATELLITE`."""
        for key in _SATELLITE_KEYS:
            value = raw.get(key)
            if value is not None:
                if isinstance(value, str) and value.strip():
                    return value.strip()
                logger.warning(
                    "CBORReader: %s=%r is not a non-empty string — ignoring", key, value
                )

        logger.warning(
            "CBORReader: no satellite field (%s) found — defaulting to %s",
            ", ".join(_SATELLITE_KEYS),
            self.DEFAULT_SATELLITE,
        )
        return self.DEFAULT_SATELLITE

    def _extract_projection_cfg(
        self, raw: dict
    ) -> tuple[list[dict] | None, float, int, dict | None]:
        """Extract ephemeris, scan_angle, image_width and the full projection_cfg dict.

        Returns
        -------
        (ephemeris, scan_angle, image_width, projection_cfg)
        where each element falls back to its default when absent or unparseable.
        """
        proj_cfg = raw.get("projection_cfg")
        if proj_cfg is None:
            return None, 112.0, 6400, None

        # SatDump sometimes stores projection_cfg as a string repr of a dict
        if isinstance(proj_cfg, str):
            import ast
            try:
                proj_cfg = ast.literal_eval(proj_cfg)
            except (ValueError, SyntaxError) as exc:
                logger.warning(
                    "CBORReader: could not parse projection_cfg string: %s", exc
                )
                return None, 112.0, 6400, None

        if not isinstance(proj_cfg, dict):
            logger.warning(
                "CBORReader: projection_cfg is not a dict (got %s) — ignoring",
                type(proj_cfg).__name__,
            )
            return None, 112.0, 6400, None

        ephemeris = proj_cfg.get("ephemeris")
        if not isinstance(ephemeris, list) or len(ephemeris) == 0:
            ephemeris = None

        scan_angle = proj_cfg.get("scan_angle", 112.0)
        try:
            scan_angle = float(scan_angle)
        except (TypeError, ValueError):
            scan_angle = 112.0

        image_width = proj_cfg.get("image_width", 6400)
        try:
            image_width = int(image_width)
        except (TypeError, ValueError):
            image_width = 6400

        return ephemeris, scan_angle, image_width, proj_cfg

    def _extract_projection(self, raw: dict) -> dict | None:
        """Extract projection coordinates from *raw*, returning ``None`` if absent."""
        for key in _PROJECTION_KEYS:
            value = raw.get(key)
            if value is not None:
                if isinstance(value, dict):
                    return value
                logger.warning(
                    "CBORReader: %s=%r is not a dict — ignoring", key, value
                )

        return None
