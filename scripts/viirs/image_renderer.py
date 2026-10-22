"""VIIRS image renderer for the NASA processing path.

Applies contrast stretching, gamma correction, destriping, and true-colour
assembly to calibrated VIIRS reflectance bands.  Used downstream of
:class:`~scripts.viirs.sdr_reader.SDRReader` to produce display-ready RGB
imagery from I-band SDR granules.

Processing pipeline per band::

    SDRReader.read_reflectance()
        → contrast_stretch()   # clip [p2, p98], scale to [0, 1]
        → gamma_correct()      # pixel^γ  (γ = 0.5)
        → assemble_true_color() # stack I1/I2/I3 → (H, W, 3) RGB

Destriping is an optional pre-processing step that corrects the systematic
scan-line offset introduced by VIIRS's 16-detector array before the contrast
stretch is applied::

    destripe()   # subtract per-detector column-wise median
        → contrast_stretch()
        → gamma_correct()
"""

from __future__ import annotations

import numpy as np


class ImageRenderer:
    """Applies contrast stretch, gamma correction, and destriping for NASA path.

    All methods accept and return ``numpy`` arrays.  Masked arrays are handled
    in :meth:`contrast_stretch` and :meth:`assemble_true_color`; the remaining
    methods operate on plain ``ndarray`` values produced after stretching.

    Constants
    ---------
    GAMMA : float
        Default gamma exponent for :meth:`gamma_correct` (γ = 0.5).
    PERCENTILE_LOW : int
        Lower percentile for contrast stretching (p2).
    PERCENTILE_HIGH : int
        Upper percentile for contrast stretching (p98).
    VIIRS_DETECTORS : int
        Number of detectors in the VIIRS focal plane array (16).
    """

    GAMMA: float = 0.5
    PERCENTILE_LOW: int = 2
    PERCENTILE_HIGH: int = 98
    VIIRS_DETECTORS: int = 16

    # ------------------------------------------------------------------ #
    # Public interface                                                      #
    # ------------------------------------------------------------------ #

    def contrast_stretch(self, band: np.ma.MaskedArray) -> np.ndarray:
        """Clip to [p2, p98] percentile range, then scale to [0, 1].

        Masked (fill) pixels are excluded from percentile computation so that
        invalid-data sentinels do not skew the stretch range.  After scaling,
        the result is a plain ``ndarray`` — the mask is consumed here and
        clipped-zero values are used in place of masked pixels (they will be
        gamma-compressed to 0, which renders as black).

        Parameters
        ----------
        band:
            Calibrated reflectance as a masked array of shape ``(H, W)``.
            Masked pixels (fill values) are ignored in percentile statistics.

        Returns
        -------
        np.ndarray
            Float32 array of shape ``(H, W)`` with values in ``[0, 1]``.
        """
        # Compute percentiles from valid (unmasked) data only.
        valid_data = band.compressed()  # 1-D array of unmasked values

        if valid_data.size == 0:
            # Entire band is masked — return zeros.
            return np.zeros(band.shape, dtype=np.float32)

        p_low = float(np.percentile(valid_data, self.PERCENTILE_LOW))
        p_high = float(np.percentile(valid_data, self.PERCENTILE_HIGH))

        # Retrieve underlying data (fill pixels become 0 after clipping).
        raw = np.ma.filled(band, fill_value=0.0).astype(np.float32)

        # Clip to [p_low, p_high] and scale to [0, 1].
        if p_high == p_low:
            # Degenerate case: flat band — avoid division by zero.
            return np.zeros(band.shape, dtype=np.float32)

        stretched = (raw - p_low) / (p_high - p_low)
        return np.clip(stretched, 0.0, 1.0).astype(np.float32)

    def gamma_correct(self, band: np.ndarray, gamma: float = GAMMA) -> np.ndarray:
        """Apply gamma correction: ``output = input ^ gamma``.

        Assumes input values are in ``[0, 1]``.  Raising to a fractional
        exponent (default γ = 0.5, i.e. square root) brightens mid-tones and
        makes dim surface features visible.

        Parameters
        ----------
        band:
            Float32 array in ``[0, 1]``, shape ``(H, W)``.
        gamma:
            Exponent applied element-wise.  Defaults to :attr:`GAMMA` (0.5).

        Returns
        -------
        np.ndarray
            Float32 array of the same shape as *band*, values in ``[0, 1]``.
        """
        # np.power handles the [0, 1] domain safely; negative values would
        # produce NaN for fractional exponents, but contrast_stretch guarantees
        # the input is non-negative.
        return np.power(band.astype(np.float32), gamma, dtype=np.float32)

    def destripe(self, band: np.ma.MaskedArray) -> np.ma.MaskedArray:
        """Correct inter-detector striping by subtracting column-wise medians.

        VIIRS images lines sequentially using 16 detectors arranged in an
        along-scan array.  Lines are assigned to detectors cyclically: line 0
        -> detector 0, line 1 -> detector 1, ..., line 15 -> detector 15, line 16
        -> detector 0 again, etc.  Each detector has a slightly different
        radiometric response, producing horizontal stripes every 16 lines.

        This method removes the systematic offset for each detector by
        computing the **column-wise median** across all rows belonging to that
        detector and subtracting it from those rows.  The correction is applied
        independently per column so that real along-track gradients (land/sea
        transitions, cloud edges) are preserved.

        Masked pixels are excluded from the median computation (Bug #3 fix):
        np.ma.median is used so that fill/invalid pixels do not corrupt the
        per-detector column medians.  For fully-masked columns the median is
        undefined; a neutral correction of 0.0 is applied (fill=0.0).  The
        input mask is restored on the output so downstream callers can
        distinguish valid from invalid pixels.

        The result is clipped to ``[0, 1]`` to prevent negative artefacts from
        over-correction.  This is appropriate for both raw reflectance (which
        is approximately [0,1] for VIIRS I-bands) and for contrast-stretched
        data.

        Parameters
        ----------
        band:
            Masked array of shape ``(H, W)`` -- either raw reflectance
            (before contrast_stretch) or a stretched plain ndarray wrapped in
            a masked array.  Plain ndarrays are also accepted (treated as
            fully unmasked).

        Returns
        -------
        np.ma.MaskedArray
            Destriped masked array of the same shape, clipped to ``[0, 1]``,
            with the original mask preserved.
        """
        # Extract mask and data; always work on a float32 copy
        if isinstance(band, np.ma.MaskedArray):
            original_mask = np.ma.getmaskarray(band).copy()
            data = band.data.astype(np.float32).copy()
        else:
            original_mask = np.zeros(band.shape, dtype=bool)
            data = np.asarray(band, dtype=np.float32).copy()

        n_rows = data.shape[0]

        for detector in range(self.VIIRS_DETECTORS):
            row_indices = np.arange(detector, n_rows, self.VIIRS_DETECTORS)
            if row_indices.size == 0:
                continue

            # Build a masked sub-array for this detector's rows so that
            # np.ma.median excludes masked (invalid) pixels from the statistic.
            det_data = data[row_indices, :]
            det_mask = original_mask[row_indices, :]
            det_masked = np.ma.masked_array(det_data, mask=det_mask)

            # Per-column median computed only from unmasked pixels.
            # For fully-masked columns the median is masked; fill with 0.0
            # (neutral correction -- do not modify those columns).
            col_median_ma = np.ma.median(det_masked, axis=0)
            col_median = np.ma.filled(col_median_ma, fill_value=0.0).astype(np.float32)

            data[row_indices, :] -= col_median

        clipped = np.clip(data, 0.0, 1.0).astype(np.float32)
        return np.ma.masked_array(clipped, mask=original_mask)

    def assemble_true_color(
        self,
        i1: np.ma.MaskedArray,
        i2: np.ma.MaskedArray,
        i3: np.ma.MaskedArray,
    ) -> np.ndarray:
        """Assemble a true-colour RGB image from VIIRS I1, I2, and I3 bands.

        Each band is independently contrast-stretched and gamma-corrected
        before being stacked into the RGB array.  The channel assignment
        follows the standard VIIRS true-colour convention:

        * **Red**   → I1 (0.64 µm, visible red)
        * **Green** → I2 (0.86 µm, near-infrared, substituted for green)
        * **Blue**  → I3 (1.61 µm, shortwave infrared, substituted for blue)

        Parameters
        ----------
        i1:
            Calibrated reflectance for band I1 (red channel), shape ``(H, W)``.
        i2:
            Calibrated reflectance for band I2 (green channel), shape ``(H, W)``.
        i3:
            Calibrated reflectance for band I3 (blue channel), shape ``(H, W)``.

        Returns
        -------
        np.ndarray
            Float32 RGB array of shape ``(H, W, 3)`` with values in ``[0, 1]``.
        """
        red = self.gamma_correct(self.contrast_stretch(i1))
        green = self.gamma_correct(self.contrast_stretch(i2))
        blue = self.gamma_correct(self.contrast_stretch(i3))

        # Stack along the last axis to form (H, W, 3).
        return np.stack([red, green, blue], axis=-1).astype(np.float32)
