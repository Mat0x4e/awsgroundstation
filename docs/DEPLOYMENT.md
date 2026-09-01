# Deployment status & the CSPP recipe

What is deployed, what works, and the exact conditions the NASA stack needs.
Supersedes the former `DEPLOYMENT_STATUS.md` and `CSPP_SOLVED.md`.

**Last verified 2026-09-01.**

---

## The chain works end to end

```
AWS Ground Station RF (VITA-49 .pcap) → I/Q → SatDump → CADU
  → RT-STPS 7.0 → RDR HDF5 (Level 0)
  → CSPP SDR 4.1.1 → calibrated SDR + terrain-corrected GEO (Level 1)
  → geolocated GeoTIFF (EPSG:4326, sub-km per-pixel)
```

Delivered products:

| Pass | Result |
|---|---|
| contact-02 (Ohio, 2026-06-23, daytime) | Full-pass **true-colour** GeoTIFF + coastline overlay, **sub-km** alignment — 10 daytime granules. The hero image. |
| contact-03 (Oregon, 2026-06-30, night) | M15 (750 m) and I5 (375 m) brightness-temperature GeoTIFFs, sub-km |
| contact-05 (Stockholm, 2026-08-31, daytime) | 5 RDR + 35 SDR/GEO. **Only 1 granule calibrated**, and not over the target — see [`MEDITERRANEAN_PASS.md`](MEDITERRANEAN_PASS.md) |

---

## CSPP SDR — the required conditions

CSPP works **deterministically** once all of these hold. Omit any one and it fails, usually
with an error that points somewhere else entirely.

1. **⭐ Preserve the original RDR filename.** `viirs_sdr.sh` parses the spacecraft from the
   `_j01_` token *in the filename*. A renamed file (`rvirs.h5`) → `_spacecraft='BAD'` →
   `Total Science RDRs: 0`. **This single detail caused a multi-week "CSPP is impossible"
   dead end** — every failed diagnostic had renamed the RDR; the one success kept the name.
   Pass `RNSCA-RVIRS_j01_...h5` verbatim.

2. **Install the J01 shipped LUTs.** `CSPP_SDR_V4.1_static_luts_j01.tar.gz` (174 MB, in
   `s3://groundstation-noaa20-sdr-output-471112743408/software/`), `tar xzf -C /opt/`.
   Creates `/opt/SDR_4_1/anc/static/shipped_luts/j01/`. Without it CSPP aborts with
   `Installation problem .../anc/static/SDR_4_1_DB/package needs to exist`.
   **Do NOT re-extract straylight or ecotiles** — they are already in the ECR image, and the
   redundant ~10 GB fills the CodeBuild disk and breaks `sdr_luts.sh`.

3. **Run `sdr_luts.sh` ONLINE.** It populates the cache DB from `jpssdb.ssec.wisc.edu`
   (~10 min). **CodeBuild reaches it; the EC2 aggregation instance cannot** — its security
   group is SSM-outbound-only. Offline, CSPP times out five times over ~11 minutes and then
   dies inside its own error handler with `TypeError: object of type 'bool' has no len()`.
   **⇒ Run CSPP in CodeBuild, not on EC2.**

4. **Feed the `.h5` RDR, not `.PDS`.** CSPP's `ADL_Unpacker` rejects PDS.

5. **Use profile `AWSAdminAccess-471112743408`** — the shell default is a different,
   often-expired one.

### Canonical invocation

```bash
export CSPP_SDR_HOME=/opt/SDR_4_1 CSPP_RT_HOME=/opt/SDR_4_1
tar xzf CSPP_SDR_V4.1_static_luts_j01.tar.gz -C /opt/     # req 2
/opt/SDR_4_1/bin/sdr_luts.sh                              # req 3 (online)
/opt/SDR_4_1/bin/viirs_sdr.sh --work-dir <wd> -p 4 \
    /tmp/rdr/RNSCA-RVIRS_j01_....h5                       # req 1 (keep the name!)
```

Both steps are wired into [`../buildspecs/aggregation.yml`](../buildspecs/aggregation.yml),
which the Step Functions state machine reads via `file()` — so the pipeline and any manual
run use the same file. Reference/diagnostic buildspec: `../scripts/cspp_viirs_sdr.yml`.

### Not the cause — do not re-litigate

The `anc/static/SDR_DB → SDR_4_1_DB` symlink; clearing or rebuilding the DMS cache;
warm-vs-cold container theories; `CSPP_DB_VER` overrides; DB snapshots; a full three-tarball
install. All were chased and none was the root cause. Superseded debugging attempts are
archived in [`../scripts/archive/cspp-debug/`](../scripts/archive/cspp-debug/).

Two alternatives were explored and are **no longer needed**: replacing CSPP with **Satpy**,
and using **SatDump's native equirectangular projection** instead of per-pixel geolocation.
Both were reasonable while CSPP looked intractable; neither is worth revisiting now that the
recipe above is deterministic. SatDump projection remains a legitimate *cheap* option if
~1–5 km accuracy is acceptable.

---

## Known limit — reception quality, not software

A VIIRS granule needs ~85 s of continuous downlink. A pass yields several granules, but only
those with enough intact science RDR calibrate:

- contact-03: ~14 granules present, ~1 complete enough to calibrate
- contact-05: 8 science granules → GEO for 4, calibrated SDR for **1**

Early granules fail with `SDR_PREREQ_ABSENT VIIRS-SCIENCE-RDR` / `PRO_CROSSGRAN_FAIL`. On a
target at the edge of the antenna's reach the basin is scanned in the opening ~90 s at
11–17° elevation, at maximum slant range — exactly where packets are lost. **A full-pass
mosaic is capped by link margin, not by CSPP.**

---

## What is deployed

| Component | Status |
|---|---|
| Terraform (`infra/`) | Deployed. `enable_sdr_pipeline = true`, `enable_processing_pipeline = false` |
| ECR image | SatDump 1.2.2 + RT-STPS 7.0+P1 + CSPP SDR 4.1.1 + straylight LUTs + ecotiles + AWS CLI |
| Per-chunk processing | Working — I/Q extraction + SatDump + S3 upload |
| RT-STPS | Working — 5 RDRs (VIIRS, CrIS, ATMS, 2×OMPS) from concatenated CADU |
| CSPP SDR | Working **in CodeBuild** (see recipe above) |
| Trigger | `groundstation-noaa20-contact-completed-sdr`, once per contact. `…-pcap-uploaded` superseded, DISABLED |
| VIIRS visualisation | Lambda + CodeBuild + ECR deployed. Invoked by the state machine (`StartVisualization`), **not** by S3 events — the output bucket deliberately emits none |
| Contact scheduler | **DISABLED on purpose.** Enabling it previously booked two unintended paid contacts. Reserve manually. |
| CloudTrail, contact_scheduler resources | Declared in Terraform, deliberately **not applied** |

### EC2 aggregation instance — retained, no longer in the path

| Property | Value |
|---|---|
| Instance | `i-01d21ecae10f99fbb`, r6i.xlarge, 300 GB gp3 (encrypted) |
| State | Stopped |
| Role | **None** — aggregation moved to CodeBuild (req 3) |
| Caution | `/opt/rt-stps` and `/opt/SDR_4_1` were installed **by hand** and exist in no Terraform, Dockerfile or user_data. Replacing the instance destroys them; `ignore_changes` covers `ami` and `root_block_device`. |

### Geolocation accuracy

| Path | Accuracy | Why |
|---|---|---|
| SatDump composites | 100–300 km | TLE-only; 5 s timing error ≈ 40 km nadir shift; no terrain correction; linear geo→pixel mapping against a curvilinear bowtie scan |
| CSPP SDR + GITCO/GMTCO | sub-km | NOAA per-pixel, terrain-corrected lat/lon |

---

## Hard-won specifics

**RT-STPS**
- `jpss1.xml` is for NOAA-20; `npp.xml` is Suomi-NPP.
- Invoke as `./bin/batch.sh config/jpss1.xml <cadu>` from `/opt/rt-stps`.
- Output goes to `../data` **relative to cwd** → `/opt/data`, a *sibling* of `RTSTPS_HOME`.
  `/opt/data` is shared across contacts and must be cleared, or a previous contact's RDR gets
  picked up and uploaded under this contact's prefix.
- `PnEncoded="true"` corrupts already-decoded frames: SatDump removes PN during demodulation,
  so RT-STPS would XOR clean data back into noise → RS fails → all frames silently discarded.
  Set `PnEncoded="false"` and remove the `pn` node from the `<links>` chain.
- One 30-second chunk is too short to form a granule (~85 s needed) — concatenate all chunks.

**CSPP layout**
- Installs to `/opt/SDR_4_1/`, **not** `/opt/cspp-sdr/`. Script is `bin/viirs_sdr.sh`, **not**
  `viirs/viirs_sdr.sh`. Requires `CSPP_SDR_HOME`.

**CodeBuild**
- Inline buildspec `echo` runs under `/bin/sh`, not bash — parentheses in echo text are a
  syntax error.
- The ECR image reference must include `:latest`, or the image is cached indefinitely.

**Everything else**
- Upload SatDump composites to S3 *before* RT-STPS runs, so a downstream failure cannot lose
  them.
- S3 allows one notification configuration per bucket; two `aws_s3_bucket_notification`
  resources on the same bucket silently erase each other.
- Shell scripts must be LF. A CRLF shebang makes Linux look for `/bin/bash\r` and fail with
  `cannot execute: required file not found`. See `.gitattributes`.
- The AWS CLI renders contact times with the **caller's local UTC offset**, not `Z`. Parsing
  with `fromisoformat().replace(tzinfo=None)` shifts everything two hours in CEST.

---

## Credentials

```bash
aws sso login --profile AWSAdminAccess-471112743408
export AWS_PROFILE=AWSAdminAccess-471112743408 AWS_REGION=eu-central-1
```
