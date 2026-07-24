# CSPP SDR — Solved & Required (2026-07-24)

Authoritative record of the CSPP SDR 4.1.1 solution. Supersedes the earlier
"abandoned / non-deterministic / DB-init impossible" conclusions in
DEPLOYMENT_STATUS.md — those were wrong. CSPP works **deterministically** once
the requirements below are met.

---

## SOLID — proven, reproducible

- **The full RF→geolocated-image chain works end to end:**
  `AWS Ground Station .pcap → I/Q → SatDump → CADU → RT-STPS → RDR (HDF5) → CSPP SDR → geolocated GeoTIFF (EPSG:4326, sub-km per-pixel geolocation).`
- **CSPP SDR 4.1.1 runs deterministically in CodeBuild** with the recipe below.
  Verified on the contact-03 VIIRS RDR: `_spacecraft='J01'`, `Total Science RDRs: 10`,
  produced calibrated SDR (`SVI0x`/`SVMxx`) + terrain-corrected GEO
  (`GITCO` img, `GMTCO` mod, `GDNBO` DNB).
- **Deliverables already produced** (contact-03, Oregon 2026-06-30, a night pass →
  thermal only): sub-km M15 (750 m) and I5 (375 m) brightness-temperature GeoTIFFs +
  Cartopy coastline overlay, in `output/contact-03_oregon-1_2026-06-30/NASA-SDR/`.
- **The deployed NASA-viz module works against real CSPP output** after the committed
  fixes (globs `SVM15`/`GITCO`/`GMTCO`, reads terrain-corrected `VIIRS-*-GEO-TC_All`
  groups, memory-safe GeoTIFF export). See `scripts/viirs/visualize_nasa.py`,
  `geo_reader.py`, `geotiff_exporter.py`.

## REQUIRED — the exact conditions (omit any → failure)

1. **J01 shipped LUTs installed.** `CSPP_SDR_V4.1_static_luts_j01.tar.gz`
   (`s3://groundstation-noaa20-sdr-output-471112743408/software/`), `tar xzf -C /opt/`.
   Creates `/opt/SDR_4_1/anc/static/shipped_luts/j01/`. (straylight + ecotiles are
   already baked into the ECR image — do NOT re-extract them; the redundant 10 GB
   fills the CodeBuild disk and breaks `sdr_luts.sh`.)
2. **`sdr_luts.sh` must run ONLINE.** It populates the working cache DB from
   `jpssdb.ssec.wisc.edu`. **CodeBuild can reach jpssdb; the EC2 aggregation instance
   CANNOT** → EC2 can only build a partial cache (`sdr_luts.sh -l` offline) which yields
   `Required shortname missing from database`. **⇒ Run CSPP in CodeBuild, not EC2.**
3. **⭐ Preserve the original RDR filename.** `viirs_sdr.sh` reads the spacecraft from
   the filename's `_j01_` token. A renamed file (e.g. `rvirs.h5`) →
   `_spacecraft='BAD'` → `Total Science RDRs: 0`. Pass `RNSCA-RVIRS_j01_...h5` verbatim.
   **This one detail caused the entire multi-week "CSPP impossible" saga.** The deployed
   `aggregation.sh` already preserves RT-STPS's `_j01_` name, so the pipeline is fine.
4. **Feed the `.h5` RDR, not `.PDS`.** CSPP's `ADL_Unpacker` rejects PDS
   (`'...PDS' should end with .h5?`).
5. **AWS profile `AWSAdminAccess-471112743408`** (the shell default profile is a
   different, often-expired one — always `export AWS_PROFILE=AWSAdminAccess-471112743408`).

### Canonical invocation
```bash
export CSPP_SDR_HOME=/opt/SDR_4_1 CSPP_RT_HOME=/opt/SDR_4_1
tar xzf CSPP_SDR_V4.1_static_luts_j01.tar.gz -C /opt/     # req 1
/opt/SDR_4_1/bin/sdr_luts.sh                              # req 2 (online)
/opt/SDR_4_1/bin/viirs_sdr.sh --work-dir <wd> -p 4 \
    /tmp/rdr/RNSCA-RVIRS_j01_....h5                       # req 3 (keep name!)
```
Reference buildspec: `scripts/cspp_viirs_sdr.yml` (run via
`aws codebuild start-build --project-name groundstation-noaa20-sdr-pipeline --buildspec-override <content>`).

## Known DATA limit (not a CSPP bug)

The contact-03 RDR holds ~14 VIIRS science granules but only ~1 is complete enough to
fully calibrate (the rest are partial from RF packet loss). So `Total Science RDRs: 10`
but SDR for ~1 granule + GEO for 4. **A full-pass mosaic is capped by reception quality,
not by CSPP.**

## NOT required (red herrings chased during debugging — avoid re-litigating)

- The `anc/static/SDR_DB → SDR_4_1_DB` symlink: fixes only the Python
  `check_spacecraft_installed()` path check, **not** the `viirs_sdr` binary. Irrelevant
  once the filename is correct.
- Clearing/rebuilding the DMS cache, warm-vs-cold container theories, full 3-LUT
  install, `CSPP_DB_VER` env overrides, DB snapshots. None were the root cause.
