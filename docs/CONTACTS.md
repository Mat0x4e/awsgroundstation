# Planned Contacts — NOAA-20 (JPSS-1)

## Contact #1

| Property | Value |
|----------|-------|
| **Contact ID** | `c14d25d6-d69c-4d9f-a255-85908ab17c13` |
| **Status** | ✅ COMPLETED |
| **Satellite** | NOAA-20 (NORAD 43013) |
| **Ground Station** | Hawaii 1 (`us-west-2`) |
| **Max Elevation** | 60.83° |
| **Pre-pass Start** | 2026-06-19 11:52:41 UTC (13:52 CEST) |
| **Visibility Start** | 2026-06-19 11:54:41 UTC (13:54 CEST) |
| **Visibility End** | 2026-06-19 12:04:01 UTC (14:04 CEST) |
| **Post-pass End** | 2026-06-19 12:06:01 UTC (14:06 CEST) |
| **Duration** | ~10 min |
| **Mission Profile** | `arn:aws:groundstation:eu-central-1:471112743408:mission-profile/2655b0f6-8196-44d3-bbc0-3782b1942d34` |
| **Dataflow** | antenna-downlink (Hawaii 1) → s3-recording (eu-central-1) |
| **Data Format** | VITA-49 DigIF (raw digitized RF, .pcap) |
| **S3 Destination** | `s3://aws-groundstation-demo-reception-471112743408/year=2026/month=06/day=19/satellite=33f035e1-73f7-47a5-9df8-fbc48636dca8/` |
| **Expected File** | `c14d25d6-d69c-4d9f-a255-85908ab17c13_*.pcap` |
| **Expected Size** | ~2 GB (10 min × 15 Mbps HRD) |
| **Actual Size** | **~40.9 GB** (19 files × ~2.18 GB — raw DigIF at 30 MHz BW) |
| **Files Delivered** | 19 .pcap files (30-second chunks) |
| **Estimated Cost** | ~$100 (on-demand narrowband X-band) |
| **Scheduled By** | Lambda `groundstation-noaa20-demo-contact-scheduler` |
| **Scheduled At** | 2026-06-19 ~11:30 UTC |

### Notifications

- Email → `mathieu.bonnet@soprasterianext.com` on:
  - Contact state change (COMPLETED / FAILED) ✅ confirmed
  - .pcap file arrival in S3 ✅ confirmed

### Next Steps

- [x] Confirm SNS email subscriptions (2 confirmation emails)
- [x] Wait for contact execution (~13:54 CEST)
- [x] Verify .pcap file in S3 after ~14:06 CEST — **19 files, ~40.9 GB total**
- [x] Process one chunk with SatDump (CodeBuild) — **VIIRS + ATMS imagery produced!**
- [x] Download and inspect VIIRS images — **Thermal IR over Pacific, ~37°N 150°W**
- [x] Process remaining 18 chunks — **SDR pipeline deployed and triggered 2026-06-25**
- [x] Create spec for automated DigIF → imagery pipeline — **`noaa20-cadu-to-tiff` spec implemented**

### Imagery Coordinates (chunk_001)

| Property | Value |
|----------|-------|
| Location | Open Pacific Ocean, ~1500 km NNW of Hawaii |
| Nadir track | 35.4°N–37.0°N, 149.8°W–150.3°W |
| Swath coverage | ~136°W → 164°W |
| Acquisition | 2026-06-19 11:54:35–11:55:04 UTC |
| Pass type | Nighttime (local ~01:55 HST) — thermal/IR only |
| Bands produced | I4, I5, M8–M16, DNB, ATMS 1–22 |

### SDR Pipeline Execution

| Property | Value |
|----------|-------|
| Execution name | `c14d25d6-d69c-4d9f-a255-85908ab17c13-run2` |
| Started | 2026-06-25 ~16:40 CEST |
| State machine | `groundstation-noaa20-sdr-pipeline` |
| Parallel chunks | 19 |

---

## Contact #2

| Property | Value |
|----------|-------|
| **Contact ID** | `7903eb3f-8126-4e3c-bb3d-74eef49f79b3` |
| **Status** | ✅ COMPLETED |
| **Satellite** | NOAA-20 (NORAD 43013) |
| **Ground Station** | Ohio 1 (`us-east-2`) — *corrected 2026-07-24; this table previously said "Hawaii 1", a copy-paste from contact #1. The delivered swath spans 91.8°W–64.4°W (Hudson Bay → Great Lakes → Florida → Cuba), which a Hawaii antenna cannot receive.* |
| **Visibility Start** | 2026-06-23 18:20:55 UTC (20:20 CEST — ~13:00 local solar time at the sub-satellite point) |
| **Visibility End** | 2026-06-23 18:43:32 UTC (20:43 CEST) |
| **Duration** | ~13 min |
| **Mission Profile** | `arn:aws:groundstation:eu-central-1:471112743408:mission-profile/2655b0f6-8196-44d3-bbc0-3782b1942d34` |
| **Dataflow** | antenna-downlink (Ohio 1) → s3-recording (eu-central-1) |
| **Data Format** | VITA-49 DigIF (raw digitized RF, .pcap) |
| **S3 Destination** | `s3://aws-groundstation-demo-reception-471112743408/year=2026/month=06/day=23/satellite=33f035e1-73f7-47a5-9df8-fbc48636dca8/` |
| **Expected File** | `7903eb3f-8126-4e3c-bb3d-74eef49f79b3_*.pcap` |
| **Actual Size** | **~58.7 GB** (27 files × ~2.18 GB — raw DigIF at 30 MHz BW) |
| **Files Delivered** | 27 .pcap files (30-second chunks) |
| **Estimated Cost** | ~$130 (on-demand narrowband X-band, longer pass) |

### SDR Pipeline Execution

| Property | Value |
|----------|-------|
| Execution name | `7903eb3f-8126-4e3c-bb3d-74eef49f79b3` |
| Started | 2026-06-25 ~16:40 CEST |
| State machine | `groundstation-noaa20-sdr-pipeline` |
| Parallel chunks | 27 |

---

## Contact #3

| Property | Value |
|----------|-------|
| **Contact ID** | `1ae80d1d-7c28-41c0-a032-1ee3e3e9f70b` |
| **Status** | ✅ COMPLETED |
| **Satellite** | NOAA-20 (NORAD 43013) |
| **Ground Station** | Oregon 1 (`us-west-2`) |
| **Max Elevation** | 89.19° (nearly overhead) |
| **Visibility Start** | 2026-06-30 09:58:05 UTC (11:58 CEST) |
| **Visibility End** | 2026-06-30 10:10:22 UTC (12:10 CEST) |
| **Duration** | ~12 min |
| **Actual Size** | **~54 GB** (25 files × ~2.18 GB) |
| **Files Delivered** | 25 .pcap files |
| **Estimated Cost** | ~$120 (on-demand narrowband X-band) |
| **Scheduled By** | Contact scheduler (unintended — cron re-enabled by terraform apply) |

### SDR Pipeline Execution

| Property | Value |
|----------|-------|
| Execution name | `1ae80d1d-run3` |
| Status | SatDump composites uploaded ✅ — RT-STPS succeeded ✅ — CSPP SDR blocked (missing J01 starter LUTs) |
| RT-STPS output | 5 RDR files: VIIRS (328 MB), CrIS (216 MB), ATMS (3.3 MB), 2×OMPS |
| RT-STPS fix | `batch.sh config/jpss1.xml combined.cadu` + `PnEncoded=false` + PN node removed |
| Composites in S3 | `contacts/2026/06/30/1ae80d1d-.../satdump/chunk_0/VIIRS/` |
| RDR in S3 | `contacts/2026/06/30/1ae80d1d-.../chunks/chunk_0/rdr/` |
| Pass coverage | ~68°N, descending over North Pacific → Bering Sea / Alaska |
| Composites | True Color, Thermal IR, Day Microphysics, 13 others |

### VIIRS Visualization

| Property | Value |
|----------|-------|
| Status | Rendered at native resolution (3200×272 px) |
| Known limitation | Cartographic overlay alignment is imprecise (~100-300 km offset) due to TLE-only geolocation without CPM correction |
| Products in S3 | `products/2026/06/30/1ae80d1d-.../viirs_satdump_true_color_*.png` |

---

## Contact #4

| Property | Value |
|----------|-------|
| **Contact ID** | `69c8c149-3b63-4d6e-887e-541c2bce917f` |
| **Status** | ✅ COMPLETED |
| **Satellite** | NOAA-20 (NORAD 43013) |
| **Ground Station** | Stockholm 1 (`eu-north-1`) |
| **Max Elevation** | 86.24° |
| **Visibility Start** | 2026-06-30 11:20:06 UTC (13:20 CEST) |
| **Visibility End** | 2026-06-30 11:28:29 UTC (13:28 CEST) |
| **Duration** | ~8 min |
| **Actual Size** | **~34 GB** (17 files × ~2.18 GB) |
| **Files Delivered** | 17 .pcap files |
| **Estimated Cost** | ~$80 (on-demand narrowband X-band) |
| **Scheduled By** | Contact scheduler (unintended — cron re-enabled by terraform apply) |

### SDR Pipeline Execution

| Property | Value |
|----------|-------|
| Execution name | `69c8c149-run3` |
| Status | SatDump composites uploaded ✅ — RT-STPS failed (exit 254) |
| Composites in S3 | `contacts/2026/06/30/69c8c149-.../satdump/chunk_0/VIIRS/` |
| Pass coverage | Scandinavia (descending pass) |

---

## Contact #5 — Mediterranean

Planned target rather than an opportunistic pass. Selection rationale, coverage analysis
and the constraints behind it: [MEDITERRANEAN_PASS.md](MEDITERRANEAN_PASS.md).

| Property | Value |
|----------|-------|
| **Contact ID** | `ba2c5446-280f-4985-8ad8-dced8ae8b616` |
| **Status** | ✅ FLOWN 2026-08-31 — `COMPLETED` at the planned 51.23° |
| **Satellite** | NOAA-20 (NORAD 43013) |
| **Ground Station** | Stockholm 1 (`eu-north-1`) — the only onboarded antenna that reaches the Mediterranean |
| **Max Elevation** | 51.23° |
| **Pre-pass Start** | 2026-08-31 11:57:49 UTC (13:57 CEST) |
| **Visibility Start** | 2026-08-31 11:59:49 UTC (13:59 CEST) |
| **Visibility End** | 2026-08-31 12:10:20 UTC (14:10 CEST) |
| **Post-pass End** | 2026-08-31 12:12:20 UTC (14:12 CEST) |
| **Duration** | 10m31s |
| **Mission Profile** | `arn:aws:groundstation:eu-central-1:471112743408:mission-profile/2655b0f6-8196-44d3-bbc0-3782b1942d34` |
| **Dataflow** | antenna-downlink (Stockholm 1, `eu-north-1`) → s3-recording (`eu-central-1`) |
| **Data Format** | VITA-49 DigIF (raw digitized RF, .pcap) |
| **S3 Destination** | `s3://aws-groundstation-demo-reception-471112743408/year=2026/month=08/day=31/satellite=33f035e1-73f7-47a5-9df8-fbc48636dca8/` |
| **Received** | 22 × `.pcap`, **43.1 GiB**, gapless 30 s cadence (first +28 s after AOS, last +19 s after LOS) |
| **Estimated Cost** | ~$110 (on-demand narrowband X-band) |
| **Scheduled By** | Manual `reserve-contact` (scheduler cron remains DISABLED) |
| **Selected With** | [scripts/plan_pass.py](../scripts/plan_pass.py) |


### Outcome — reception clean, target not calibrated

| Stage | Result |
|---|---|
| Reception | 22 chunks, 43.1 GiB, no dropout |
| SatDump | 22 × `npp_hrd.cadu` (53.6 MiB each) |
| RT-STPS | 5 RDRs — VIIRS 639.5 MiB, CrIS 184.5, ROTCS 30.7, ATMS 2.9, RONPS 2.7 |
| CSPP | 8 science granules in → **GEO for 4, calibrated SDR for 1** (35 files, 2.7 GiB) |

**The calibrated granule does not cover the Mediterranean.** It spans 12:06:35–12:07:59 UTC,
6m46s after AOS, over Scandinavia; the earliest product of any kind starts 12:02:20. The
basin is scanned in the **first 10–115 s** of this contact, and those granules failed inside
CSPP with `SDR_PREREQ_ABSENT VIIRS-SCIENCE-RDR` / `PRO_CROSSGRAN_FAIL` — the science RDR was
too incomplete to calibrate.

That is the predicted failure mode, not a surprise: the basin sits at the southern edge of
Stockholm's reach and is scanned at 11.6–17.4° elevation at maximum slant range. The pass was
still the right pick — it was the only offer of 32 putting the basin in the 375 m core swath.
Retry booked as contact #6.

This contact was also used for three pipeline rehearsals on 2026-09-01; products from those
runs accumulate alongside the originals (RDR/SDR filenames embed a creation timestamp).
Pre-rehearsal copies: `s3://groundstation-noaa20-sdr-output-471112743408/rehearsal-backup/2026-09-01/ba2c5446/`.

### Expected Coverage

Ascending daytime pass, ~13:20 local solar time. Sub-track 40.3°N 15.2°E (Gulf of
Taranto) → 75.2°N 15.6°W (Arctic).

| Basin | Off nadir | After AOS | Swath | Sun elev | Glint |
|-------|-----------|-----------|-------|----------|-------|
| Balearic Sea | 1013 km | +35 s | edge | 58.9° | 74.5° |
| Gulf of Lion | 734 km | +75 s | core | 55.7° | 68.9° |
| Ligurian Sea | 428 km | +75 s | core | 54.2° | 57.8° |
| North Adriatic | 52 km | +75 s | core | 52.3° | 40.2° |
| South Adriatic | 222 km | +15 s | core | 53.6° | 28.3° |
| *Naples* | *63 km* | *+10 s* | *core* | — | *37.2°* |

Not covered: the Tyrrhenian and the Strait of Sicily are scanned before AOS — the
sub-track is already at 40.3°N when the link opens. The eastern basin (Aegean, Crete,
Levantine) is unreachable from Stockholm at any time.

The basin is scanned in the opening ~2 minutes at 11–17° link elevation, i.e. at
maximum slant range. Partial granules there are expected and inherent to the geometry,
not a pipeline regression.

### Next Steps

- [ ] Check cloud forecast for the western/central Med on 2026-08-30
- [ ] Confirm `contactStatus` reaches `SCHEDULED`
- [ ] Verify .pcap arrival after 14:12 CEST
- [ ] Run SDR pipeline with the working CSPP recipe ([DEPLOYMENT.md](DEPLOYMENT.md))
- [x] Fix `SVOM15`/`GMODO`/`GIGTO` constants in `scripts/viirs/` before the NASA path — **done 2026-08-27**, verified against contact #3's real `GMTCO`/`GITCO`
- [ ] True-colour composite + I-band (375 m) look at the Adriatic/Naples area

---

## Cancelled Contacts

| Contact ID | Date | Ground Station | Reason |
|-----------|------|----------------|--------|
| `9ffe7769` | 2026-06-25 01:21 UTC | Stockholm 1 | Cancelled manually |
| `bd82c917` | 2026-06-25 11:13 UTC | Stockholm 1 | Cancelled manually |

---

## SDR Pipeline Infrastructure

Deployed 2026-06-25 via Terraform (`enable_sdr_pipeline = true`).

| Resource | Value |
|----------|-------|
| State Machine | `arn:aws:states:eu-central-1:471112743408:stateMachine:groundstation-noaa20-sdr-pipeline` |
| CodeBuild Project | `groundstation-noaa20-sdr-pipeline` |
| ECR Repository | `471112743408.dkr.ecr.eu-central-1.amazonaws.com/groundstation-noaa20-sdr-pipeline` |
| Docker Image | `latest` / `v7` (SatDump 1.2.2 + RT-STPS 7.0+P1 + CSPP SDR 4.1.1) |
| Output Bucket | `groundstation-noaa20-sdr-output-471112743408` |
| EventBridge Rule | `groundstation-noaa20-pcap-uploaded` (auto-triggers on new .pcap uploads) |
| Region | `eu-central-1` |

### Pipeline Processing Chain

```
.pcap (VITA-49 DigIF) → I/Q Extraction (.cs8) → SatDump npp_hrd (.cadu)
→ RT-STPS 7.0 (RDR HDF5) → CSPP SDR 4.1.1 (SDR + GEO HDF5 Level 1)
```

### Software Versions (in Docker image)

| Tool | Version | Role |
|------|---------|------|
| SatDump | 1.2.2 | Baseband → CADU (QPSK demod + Viterbi + RS) |
| RT-STPS | 7.0 + Patch 1 | CADU → RDR (CCSDS → HDF5 Level 0) |
| CSPP SDR | 4.1.1 | RDR → SDR + GEO (calibrated HDF5 Level 1) |
| Python | 3.x | I/Q extraction, geolocation, manifest, metrics |

---

## Contact #6 — Mediterranean retry (scheduled)

Second attempt at the objective contact #5 missed. Chosen on the metric that actually
failed: link elevation **while the basin is scanned**.

| Property | Value |
|----------|-------|
| **Contact ID** | `8fb38af8-080a-445a-bf10-50d14f4ba85e` |
| **Status** | 📅 SCHEDULED (reserved 2026-08-31) |
| **Ground Station** | Stockholm 1 (`eu-north-1`) |
| **Max Elevation** | 61.27° (AWS) — planner computed 56.9°, a 4.4° spread worth watching |
| **Visibility** | 2026-09-06 11:47:16 → 11:57:59 UTC (10m43s) |
| **Pre/post pass** | 11:45:16 → 11:59:59 UTC |
| **Basins** | 6 imaged, 5 in the 375 m core swath |
| **Link over basin** | Gulf of Lion and Ligurian at **19.5°**, N. Adriatic **18.8°** (293 km off nadir) — against 11.6–17.4° on contact #5 |
| **Best glint** | 39.3° — clean true-colour side of the glint conflict |
| **Estimated Cost** | ~$110 |

**Residual risk:** every offered Stockholm pass scans the basin in the first 5–115 s after
AOS — structural, since the Mediterranean sits at the southern edge of reach. On contact #5
nothing before ~AOS+2.5 min survived calibration. Higher elevation improves the link budget
but does not move the basin out of that window.
