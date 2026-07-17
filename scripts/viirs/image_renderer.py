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

    def destripe(self, band: np.ndarray) -> np.ndarray:
        """Correct inter-detector striping by subtracting column-wise medians.

        VIIRS images lines sequentially using 16 detectors arranged in an
        along-scan array.  Lines are assigned to detectors cyclically: line 0
        → detector 0, line 1 → detector 1, …, line 15 → detector 15, line 16
        → detector 0 again, etc.  Each detector has a slightly different
        radiometric response, producing horizontal stripes every 16 lines.

        This method removes the systematic offset for each detector by
        computing the **column-wise median** across all rows belonging to that
        detector and subtracting it from those rows.  The correction is applied
        independently per column so that real along-track gradients (land/sea
        transitions, cloud edges) are preserved.

        The result is clipped to ``[0, 1]`` to prevent negative artefacts from
        over-correction.

        Parameters
        ----------
        band:
            Float32 array of shape ``(H, W)`` with values in ``[0, 1]``.
            ``H`` does not need to be an exact multiple of
            :attr:`VIIRS_DETECTORS` — the final partial cycle is handled
            correctly.

        Returns
        -------
        np.ndarray
            Destriped float32 array of the same shape, clipped to ``[0, 1]``.
        """
        corrected = band.copy().astype(np.float32)
        n_rows = band.shape[0]

        for detector in range(self.VIIRS_DETECTORS):
            # Collect indices of all rows belonging to this detector.
            row_indices = np.arange(detector, n_rows, self.VIIRS_DETECTORS)

            if row_indices.size == 0:
                continue

            # Sub-array of shape (n_detector_rows, W).
            detector_rows = corrected[row_indices, :]

            # Column-wise median: shape (W,).  Using np.median along axis=0
            # computes the median independently for each column.
            col_median = np.median(detector_rows, axis=0)

            # Subtract the per-column median from every row of this detector.
            corrected[row_indices, :] -= col_median

        return np.clip(corrected, 0.0, 1.0).astype(np.float32)

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
