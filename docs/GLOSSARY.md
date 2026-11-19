# Glossary

Domain terms used across this project, in one place. The satellite-communications and
NOAA/JPSS processing vocabulary is dense; this is the single reference so the other docs can
use the terms without re-defining them each time.

This glossary is a **superset** of the per-part glossaries in the article series
([article 1](../articles/article-1-cloud-opensource.md#glossary) covers reception and
demodulation; [article 2](../articles/article-2-nasa-software.md#glossary) covers the NASA
Level-0/1 stack). Definitions here are kept consistent with those; this file adds the AWS
Ground Station, RF-config, and project-specific terms the articles do not cover.

The themes below mirror the article glossaries' grouping (orbit and delivery; the signal
chain; instruments and scan geometry; positioning and output; software; processing levels;
physical vocabulary), then extend with cloud/ops themes.

> Scope note: definitions describe how each term is used **in this project** (NOAA-20 X-band
> direct broadcast via AWS Ground Station). Some terms have broader meanings elsewhere.

---

## The one-paragraph version

This project rents an AWS Ground Station antenna to receive the **NOAA-20** weather
satellite's **X-band** direct-broadcast downlink as raw digitized radio (**DigIF**, in
**VITA-49** packets), delivered to S3. Software then demodulates and decodes that radio into
clean data frames (**CADU**), assembles them into Level-0 **RDR** files (**RT-STPS**),
calibrates and geolocates them into Level-1 **SDR + GEO** files (**CSPP**), and renders
georeferenced **VIIRS** imagery (GeoTIFF/PNG). An alternative "synchronous" path would let the
antenna do the demod/decode itself (**demod/decode** delivery) - but that path is blocked on
satellite onboarding (see `docs/SYNC_ONBOARDING_INVESTIGATION.md`).

---

## Orbit and delivery

| Term | Meaning |
|---|---|
| **NOAA-20** | The target satellite: a NOAA polar-orbiting weather satellite, part of the JPSS program. Also called **JPSS-1**. |
| **J01** | The spacecraft short-code for NOAA-20 / JPSS-1, used in CSPP filenames and LUT package names (e.g. `SVI01_j01_...`). |
| **SNPP** | Suomi National Polar-orbiting Partnership - NOAA-20's predecessor; carries the same instruments. Shares AWS Ground Station config templates with NOAA-20. |
| **NORAD ID** | The catalog number that identifies a satellite in the public orbital-element set. NOAA-20 = **43013**. |
| **sun-synchronous orbit** | An orbit whose plane precesses with the Sun, so the spacecraft crosses each latitude at the same local solar time every pass - which is why a daytime node is always sunlit (and why daytime true-color is only available on the right passes). |
| **TLE** | Two-Line Element set - the compact public orbital-state data, propagated with SGP4 to predict where the satellite is. Used by `plan_pass.py` to rank passes, and passed to SatDump so it can geolocate imagery. |
| **ephemeris** | The satellite's position/velocity over time. SatDump records a per-scan ephemeris inside its product (CBOR); CSPP uses the on-board GEO ephemeris for per-pixel geolocation. |
| **AOS / LOS** | Acquisition Of Signal / Loss Of Signal - the start and end of a usable contact, i.e. when the satellite rises above / drops below the antenna's horizon mask. |
| **pass / contact** | One overhead passage of the satellite during which the antenna can receive it. A "contact" is the booked AWS Ground Station reservation for that pass. Logged in `docs/CONTACTS.md`. |
| **elevation / slant range** | Elevation is the satellite's angle above the horizon; slant range is the line-of-sight distance to it. Low elevation = long slant range = weaker link = more lost packets (the link-budget limit on usable data). |
| **direct broadcast** | The satellite transmitting continuously to whoever is in view with a capable antenna, rather than storing data for a scheduled dump to an agency ground station. It is what makes a private receiver possible at all. |

## The radio downlink (RF layer)

| Term | Meaning |
|---|---|
| **X-band** | The ~8 GHz radio band used for NOAA-20's high-rate science downlink. This project uses ~7812 MHz center, 30 MHz bandwidth, right-hand circular polarization (RHCP). |
| **HRD** | High Rate Data - the ~15 Mbps X-band broadcast carrying VIIRS and the other instruments (as opposed to lower-rate beacons). |
| **DigIF** | Digitized Intermediate Frequency - AWS delivers raw digitized spectrum (the I/Q waveform) rather than a demodulated bitstream, so demodulation becomes yours to do. Shipped as VITA-49 packets to S3; ~30x larger than the decoded bitstream, but lets us run the whole signal chain. This is the **asynchronous** delivery mode this project flies. |
| **VITA-49** | The packet format DigIF spectrum arrives in, with signal metadata in separate context packets. Two flavours matter here: "VITA-49 Signal Data/IP" (DigIF, the async path) and the "VITA-49 Extension" format (demod/decode egress, the sync path) - their packet headers differ, which is the crux of the REL-5 packet-format gate. |
| **QPSK** | Quadrature Phase-Shift Keying - the downlink's modulation; demodulation recovers bits from the I/Q waveform. |
| **symbol rate** | The rate of modulated symbols on the carrier (15 Msps in the demod/decode config). Distinct from the SDR sample rate used when capturing DigIF. |
| **polarization (RHCP)** | The orientation of the radio wave; NOAA-20 X-band HRD is Right-Hand Circularly Polarized. The antenna must match it. |

## The signal chain (demod + FEC decode)

| Term | Meaning |
|---|---|
| **I/Q** | In-phase and Quadrature samples - the complex representation of a radio signal. Written here as `.cs8` (8-bit signed pairs). `iq_extract.py` turns the VITA-49 DigIF `.pcap` into the `.cs8` stream SatDump demodulates. |
| **demod / demodulation** | Recovering the digital symbol stream from the modulated radio waveform. In the async path SatDump does this; in the sync path the AWS antenna does it ("demod/decode delivery"). |
| **Viterbi** | The convolutional forward-error-correction (FEC) decoder - CCSDS rate-1/2 (171,133). Corrects bit errors introduced over the radio link. |
| **NRZ-M** | Non-Return-to-Zero Mark - a line-coding step that must be decoded after Viterbi. |
| **Reed-Solomon (RS)** | A block FEC code applied over the frames; RS decoding corrects the residual byte errors that survive Viterbi. |
| **PN / PN derandomization** | Pseudo-Noise randomization (scrambling): a known bit pattern XORed onto the downlink to keep the signal spectrally well-behaved. It must be derandomized (XORed back) before frame sync. **Key project subtlety:** removing it twice is the same as not removing it - in the async path SatDump derandomizes PN upstream, so RT-STPS runs `PnEncoded=false`; in the sync (demod/decode) path the antenna does NOT, so RT-STPS must run `PnEncoded=true`. Getting this backwards silently discards every frame. |
| **UncodedFramesEgress** | The egress node of the AWS Ground Station demod/decode config graph that emits frames after QPSK demod + Viterbi + NRZ-M, but WITHOUT Reed-Solomon or PN handling (RT-STPS does those downstream). The sync-path output. |
| **frame sync** | Locking onto the start of each data frame in the decoded stream (RT-STPS). If the packet-format assumption is wrong, frame sync fails and zero RDR is produced. |

## CCSDS framing

| Term | Meaning |
|---|---|
| **CCSDS** | Consultative Committee for Space Data Systems - the space-agency packet standards the downlink follows; RT-STPS reassembles these packets from the frame stream. |
| **VCDU** | Virtual Channel Data Unit - a CCSDS transfer frame carrying one virtual channel's data. |
| **CADU** | Channel Access Data Unit - a fixed-length CCSDS frame (VCDU plus sync marker and framing), and the first clean digital product of the chain (`.cadu`, ~53.6 MiB per chunk). RT-STPS consumes concatenated CADU. |

## Instruments and scan geometry

| Term | Meaning |
|---|---|
| **VIIRS** | Visible Infrared Imaging Radiometer Suite - the imager aboard NOAA-20, sweeping a ~3,000 km swath across the ground track. The instrument this project renders (true-color, thermal IR, day microphysics); the hero output. |
| **CrIS / ATMS / OMPS** | The other JPSS instruments carried in the same downlink - a hyperspectral infrared sounder (CrIS), a microwave temperature/humidity sounder (ATMS), and an ozone mapper/profiler (OMPS, two sensors). RT-STPS separates them from VIIRS; this project decodes them to RDR but does not yet calibrate/render them. |
| **nadir** | The point directly beneath the spacecraft; scan angle is measured from it. |
| **swath** | The strip of ground one scan covers - for VIIRS ~3,000 km wide, scanning +/-56 degrees either side of nadir (112 degrees total; see `scripts/viirs/bbox_calculator.py`). |
| **detector aggregation** | VIIRS combines several samples into one pixel near nadir (3:1), fewer further out (2:1, then 1:1), keeping ground resolution roughly even across the swath - and making column index non-linear in scan angle. The aggregation zones live in SatDump's VIIRS scan model (`forced_gcps_x`). |
| **granule** | The fixed-length time slice CSPP calibrates - ~85 s of continuous VIIRS data. Calibration is all-or-nothing per granule: an incomplete one is dropped rather than partially processed. A pass yields several granules; only those with enough intact science RDR calibrate - the link-margin ceiling on usable imagery. |

## Positioning and output

| Term | Meaning |
|---|---|
| **terrain correction** | Adjusting a pixel's coordinates for the elevation of the ground it fell on. A ray traced to a smooth ellipsoid misplaces a mountainside; the "TC" in `GITCO`/`GMTCO`. |
| **WGS84** | The reference ellipsoid that coordinates are expressed against; each instrument look-ray is intersected with it. Bounding boxes are in WGS84 degrees. |
| **equirectangular** | A projection mapping latitude and longitude directly to y and x - the north-up grid that composites are resampled onto. |
| **GeoTIFF** | A TIFF carrying its own georeferencing (extent + projection), so other tools can place it without external metadata. A primary product format. |
| **composite** | An image built from several instrument bands (e.g. True Color = three reflective bands), stretched for display rather than measured. SatDump produces these per chunk. |

## Processing levels and product names

| Term | Meaning |
|---|---|
| **Level 0 / Level 1 / Level 2** | Processing levels. Level 0 is raw instrument counts as downlinked; Level 1 is those counts converted to physical units and tied to positions on the ground; Level 2 is derived geophysical products. |
| **RDR** | Raw Data Record - the JPSS name for the **Level 0** product RT-STPS writes (HDF5). One per instrument (VIIRS, CrIS, ATMS, 2x OMPS). |
| **SDR** | Sensor Data Record - the JPSS **Level 1** product: calibrated radiances / brightness temperatures (HDF5), produced by CSPP from the RDR. Note the collision with "software-defined radio", which this is not. |
| **GEO** | The geolocation companion to an SDR, holding latitude/longitude for every pixel (terrain-corrected). CSPP writes `GITCO` (imaging bands) / `GMTCO` (moderate bands), and older ellipsoid variants `GIGTO` / `GMODO`. |
| **EDR** | Environmental Data Record - **Level 2** geophysical products (e.g. cloud mask, SST). Produced by IPOPP, a higher stage this project does not yet run. |
| **SVI / SVM** | CSPP SDR output filename prefixes: SVI = VIIRS imagery bands (375 m, e.g. SVI05 = I5), SVM = VIIRS moderate bands (750 m, e.g. SVM15 = M15). `SVOM15` is the older ellipsoid-geolocation name for the same band. |
| **HDF5** | The self-describing container format RDR, SDR and GEO products use, holding multi-dimensional arrays with their metadata. |

## Physical vocabulary

| Term | Meaning |
|---|---|
| **radiance** | The physical quantity the instrument measures - energy per area, solid angle and wavelength - as opposed to a display value stretched for contrast. |
| **brightness temperature** | An infrared radiance expressed as the temperature a perfect emitter would need to produce it. Convenient because it reads directly as degrees. |
| **calibration LUT** | Look-Up Table: lookup tables converting raw counts to radiances, updated as the instrument ages. CSPP fetches them from `jpssdb.ssec.wisc.edu` (why that step needs network egress); PERF-1 caches them in S3 to skip the ~10-min fetch on warm runs. |

## Tooling and software

| Term | Meaning |
|---|---|
| **SatDump** | Open-source SDR software. In the async path it demodulates/decodes DigIF I/Q to CADU and renders composite imagery; it can also georeference its own composites via a built-in VIIRS scan model (parked at 1.2.2 - see `docs/SATDUMP.md`). |
| **RT-STPS** | Real-Time Software Telemetry Processing System (NASA DRL) - demultiplexes CADU frames per instrument and reassembles CCSDS packets into Level-0 RDR HDF5. Licensed; installed in the container image. |
| **CSPP** | Community Satellite Processing Package (CSPP SDR 4.1.1, University of Wisconsin / CIMSS) - turns Level-0 RDR into calibrated, geolocated Level-1 SDR/GEO. Licensed. Needs the J01 static LUTs and internet access for `sdr_luts.sh`. |
| **IPOPP** | International Planetary Observation Processing Package (NASA DRL) - the Level-2 EDR stage. Not run in this project (a missed-opportunity candidate). |
| **CIMSS / SSEC** | Cooperative Institute for Meteorological Satellite Studies / Space Science and Engineering Center (University of Wisconsin-Madison) - the source of CSPP and the LUT server `jpssdb.ssec.wisc.edu`. |

## AWS Ground Station concepts

| Term | Meaning |
|---|---|
| **mission profile** | The AWS Ground Station object that ties together the antenna config + tracking + data delivery for a satellite. This project has an async (DigIF -> S3) profile and a separate sync (demod/decode) profile. |
| **dataflow endpoint / endpoint group** | For synchronous delivery, the network endpoint (on a receiver EC2's ENI) that AWS Ground Station streams demod/decoded frames to live during the contact. |
| **S3 data delivery vs demod/decode delivery** | Two AWS Ground Station delivery modes. S3 delivery drops raw DigIF into a bucket (async; what we fly). Demod/decode delivery streams decoded frames to a live endpoint (sync; built but onboarding-blocked). They are onboarded separately. |
| **onboarding** | The AWS process (data collection, technical validation, spectrum licensing, NDAs) that authorizes a satellite for a given delivery mode on an account. NOAA-20 is onboarded here for DigIF but NOT for demod/decode (see `docs/SYNC_ONBOARDING_INVESTIGATION.md`). |

## Project-specific terms

| Term | Meaning |
|---|---|
| **async / DigIF pipeline** | The reception path that is deployed and flown: S3-delivered DigIF -> SatDump -> RT-STPS -> CSPP -> VIIRS imagery. |
| **sync / demod-decode pipeline** | The alternative path: live antenna-side demod/decode streamed to a receiver EC2, which runs RT-STPS locally, then CSPP. Built and deployed but never flown (onboarding-blocked). |
| **fan-out / aggregation / render** | The three runtime compute stages, all on AWS Batch since 2026-10-22: fan-out = per-chunk SatDump (Spot); aggregation = RT-STPS + CSPP over the whole pass (on-demand); render = VIIRS visualization (on-demand, dedicated image). |
| **chunk** | One ~30 s segment of the DigIF downlink, delivered as one `.pcap` object and processed by one fan-out job. A pass is ~17-27 chunks. |
| **PERF-1 / REL-x / OE-x** | Well-Architected refinement tracking IDs (Performance, Reliability, Operational Excellence) from `docs/ARCHITECTURE_REFINEMENT.md`; e.g. PERF-1 = the S3 LUT cache, REL-5 = the sync packet-format validation gate. |
| **ADR-001 / ADR-002** | Architecture Decision Records in `docs/ARCHITECTURE_REFINEMENT.md`: ADR-001 chose AWS Batch on Spot for fan-out; ADR-002 extended that to remove CodeBuild from the whole runtime (implemented via the `clean-compute-pipeline` spec). |
