"""VIIRS brightness temperature converter using the inverse Planck law.

Converts calibrated spectral radiance (mW·m⁻²·sr⁻¹·µm⁻¹) to brightness
temperature in Kelvin via the inverse Planck function.  Designed for use
with VIIRS band M15 (central wavelength 10.7630 µm) in the NASA processing
path of the visualization pipeline.

Physical model::

    BT = C2 / (λ × ln(C1 / (λ⁵ × L) + 1))

where:
    C1 = 1.191042 × 10⁸  mW·µm⁴·m⁻²·sr⁻¹   (first radiation constant)
    C2 = 1.4387752 × 10⁴  µm·K               (second radiation constant)
    λ  = central wavelength in µm
    L  = spectral radiance in mW·m⁻²·sr⁻¹·µm⁻¹
"""

from __future__ import annotations

import numpy as np


class BTConverter:
    """Converts spectral radiance to brightness temperature via inverse Planck.

    Constants
    ---------
    C1 : float
        First radiation constant: 1.191042 × 10⁸ mW·µm⁴·m⁻²·sr⁻¹.
    C2 : float
        Second radiation constant: 1.4387752 × 10⁴ µm·K.
    M15_WAVELENGTH : float
        VIIRS M15 central wavelength: 10.7630 µm.
    """

    C1: float = 1.191042e8      # mW·µm⁴·m⁻²·sr⁻¹
    C2: float = 1.4387752e4     # µm·K
    M15_WAVELENGTH: float = 10.7630  # µm (VIIRS M15 central wavelength)

    def convert(
        self,
        radiance: np.ma.MaskedArray,
        wavelength: float = M15_WAVELENGTH,
    ) -> np.ma.MaskedArray:
        """Apply the inverse Planck law to convert radiance to brightness temperature.

        Applies the inverse Planck function element-wise::

            BT = C2 / (λ × ln(C1 / (λ⁵ × L) + 1))

        The mask from the input radiance array is propagated unchanged to the
        output — fill or invalid pixels in the radiance are fill in the
        brightness temperature.  Arithmetic is performed only on unmasked
        pixels.

        Parameters
        ----------
        radiance:
            Calibrated spectral radiance in mW·m⁻²·sr⁻¹·µm⁻¹.  Must be a
            ``numpy.ma.MaskedArray`` (as returned by
            :class:`~scripts.viirs.sdr_reader.SDRReader`).
        wavelength:
            Central wavelength in µm.  Defaults to the VIIRS M15 value
            (10.7630 µm).

        Returns
        -------
        numpy.ma.MaskedArray
            Brightness temperature in Kelvin.  Float32 masked array with the
            same shape as *radiance*.  Masked pixels retain the input mask;
            ``fill_value`` is ``numpy.nan``.

        Notes
        -----
        The computation is performed in float64 for numerical precision and
        then cast to float32 to match the pipeline's standard output dtype.
        Using float32 throughout would introduce rounding errors of ~0.01 K
        for typical thermal infrared radiances.
        """
        # Work in float64 for numerical stability; cast to float32 at the end.
        L = radiance.data.astype(np.float64)

        lambda5 = wavelength ** 5  # λ⁵  (scalar)

        # Inverse Planck: BT = C2 / (λ × ln(C1 / (λ⁵ × L) + 1))
        bt_data = self.C2 / (wavelength * np.log(self.C1 / (lambda5 * L) + 1.0))

        return np.ma.masked_array(
            data=bt_data.astype(np.float32),
            mask=np.ma.getmaskarray(radiance),
            fill_value=np.nan,
            dtype=np.float32,
        )
