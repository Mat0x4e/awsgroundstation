# Glossary

Domain terms used across this project, in one place. The satellite-communications and
NOAA/JPSS processing vocabulary is dense; this is the single reference so the other docs can
use the terms without re-defining them each time. Grouped by theme; within each group, ordered
roughly by where it appears in the pipeline.

> Scope note: definitions here describe how each term is used **in this project** (NOAA-20
> X-band direct broadcast via AWS Ground Station). Some terms have broader meanings elsewhere.

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

## Satellites and orbital terms

| Term | Meaning |
|---|---|
| **NOAA-20** | The target satellite: a NOAA polar-orbiting weather satellite, part of the JPSS program. Also called **JPSS-1**. |
| **J01** | The spacecraft short-code for NOAA-20 / JPSS-1, used in CSPP filenames and LUT package names (e.g. `SVI01_j01_...`). |
| **SNPP** | Suomi National Polar-orbiting Partnership - NOAA-20's predecessor; carries the same instruments. Shares AWS Ground Station config templates with NOAA-20. |
| **NORAD ID** | The catalog number that identifies a satellite in the public orbital-element set. NOAA-20 = **43013**. |
| **TLE** | Two-Line Element set - the public orbital-state data used to predict where the satellite is. Propagated by `plan_pass.py` to rank passes, and passed to SatDump so it can geolocate imagery. |
| **ephemeris** | The satellite's position/velocity over time. SatDump records a per-scan ephemeris inside its product (CBOR); CSPP uses the on-board GEO ephemeris for per-pixel geolocation. |
| **AOS / LOS** | Acquisition Of Signal / Loss Of Signal - the start and end of a usable contact, i.e. when the satellite rises above / drops below the antenna's horizon mask. |
| **pass / contact** | One overhead passage of the satellite during which the antenna can receive it. A "contact" is the booked AWS Ground Station reservation for that pass. Logged in `docs/CONTACTS.md`. |
| **elevation / slant range** | Elevation is the satellite's angle above the horizon; slant range is the line-of-sight distance to it. Low elevation = long slant range = weaker link = more lost packets (the link-budget limit on usable data). |

## The radio downlink (RF layer)

| Term | Meaning |
|---|---|
| **X-band** | The ~8 GHz radio band used for NOAA-20's high-rate science downlink. This project uses ~7812 MHz center, 30 MHz bandwidth, right-hand circular polarization (RHCP). |
| **HRD** | High Rate Data - NOAA-20's high-rate direct-broadcast downlink carrying the full instrument science data (as opposed to lower-rate beacons). ~15 Mbps. |
| **direct broadcast** | The satellite continuously transmits its data to anyone in view with a capable antenna (no tasking, no store-and-forward). This is what we receive. |
| **QPSK** | Quadrature Phase-Shift Keying - the modulation scheme the downlink uses to carry bits on the radio carrier. Demodulation recovers the raw symbol stream. |
| **symbol rate** | The rate of modulated symbols on the carrier (15 Msps in the demod/decode config). Distinct from the SDR sample rate used when capturing DigIF. |
| **polarization (RHCP)** | The orientation of the radio wave; NOAA-20 X-band HRD is Right-Hand Circularly Polarized. The antenna must match it. |

## From radio to clean frames (demod + FEC decode)

| Term | Meaning |
|---|---|
| **DigIF** | Digitized Intermediate Frequency - raw digitized radio samples (the I/Q waveform), delivered by AWS Ground Station as VITA-49 packets to S3. ~30x larger than the decoded bitstream, but lets us run the whole signal chain ourselves. This is the **asynchronous** delivery mode this project flies. |
| **VITA-49** | The packet standard AWS Ground Station uses to ship digitized RF. Two flavours matter here: "VITA-49 Signal Data/IP" (DigIF, the async path) and the "VITA-49 Extension" format (demod/decode egress, the sync path) - their packet headers differ, which is the crux of the REL-5 packet-format gate. |
| **I/Q** | In-phase / Quadrature - the two components of the complex radio waveform. `iq_extract.py` turns the VITA-49 DigIF `.pcap` into a raw `.cs8` I/Q stream SatDump can demodulate. |
| **demod / demodulation** | Recovering the digital symbol stream from the modulated radio waveform. In the async path SatDump does this; in the sync path the AWS antenna does it ("demod/decode delivery"). |
| **Viterbi** | The convolutional forward-error-correction (FEC) decoder - CCSDS rate-1/2 (171,133). Corrects bit errors introduced over the radio link. |
| **NRZ-M** | Non-Return-to-Zero Mark - a line-coding step that must be decoded after Viterbi. |
| **Reed-Solomon (RS)** | A block FEC code applied over the frames; RS decoding corrects residual errors that Viterbi did not. |
| **PN / PN derandomization** | Pseudo-Noise randomization applied to the transmitted bitstream (so the radio doesn't send long runs of identical bits). It must be "derandomized" (XORed back) before frame sync. **Key project subtlety:** in the async path SatDump derandomizes PN upstream, so RT-STPS runs with `PnEncoded=false`; in the sync (demod/decode) path the antenna does NOT, so RT-STPS must run `PnEncoded=true`. Getting this backwards silently discards every frame. |
| **UncodedFramesEgress** | The egress node of the AWS Ground Station demod/decode config graph that emits frames after QPSK demod + Viterbi + NRZ-M, but WITHOUT Reed-Solomon or PN handling (RT-STPS does those downstream). The sync-path output. |
| **frame sync** | Locking onto the start of each data frame in the decoded stream (RT-STPS). If the packet-format assumption is wrong, frame sync fails and zero RDR is produced. |

## CCSDS framing and processing levels

| Term | Meaning |
|---|---|
| **CCSDS** | Consultative Committee for Space Data Systems - the standards body whose framing (VCDU/CADU) and FEC the downlink uses. |
| **VCDU** | Virtual Channel Data Unit - a CCSDS transfer frame carrying one virtual channel's data. |
| **CADU** | Channel Access Data Unit - a VCDU plus sync marker and (pre-RS) framing; the "clean CCSDS frames" SatDump emits (`.cadu`, ~53.6 MiB per chunk). RT-STPS consumes concatenated CADU. |
| **RDR** | Raw Data Record - **Level 0**. Reconstructed raw instrument data in HDF5, produced by RT-STPS from the CADU. One per instrument (VIIRS, CrIS, ATMS, 2x OMPS). |
| **SDR** | Sensor Data Record - **Level 1**. Calibrated, geolocated instrument data in HDF5, produced by CSPP from the RDR. "Calibrated" = raw counts converted to physical radiances. |
| **GEO** | The geolocation companion to an SDR: per-pixel latitude/longitude (terrain-corrected). CSPP writes `GITCO`/`GMTCO` (terrain-corrected) and older `GIGTO`/`GMODO` (ellipsoid) variants. |
| **EDR** | Environmental Data Record - **Level 2** geophysical products (e.g. cloud mask, SST). Produced by IPOPP, a higher stage this project does not yet run. |
| **SVI / SVM** | CSPP SDR output filename prefixes: SVI = VIIRS imagery bands (375 m, e.g. SVI05 = I5), SVM = VIIRS moderate bands (750 m, e.g. SVM15 = M15). `SVOM15` is the older ellipsoid-geolocation name for the same band. |
| **granule** | A fixed-length time slice of an instrument's data (~85 s for VIIRS). A pass yields several granules; only those with enough intact science RDR will calibrate - the link-margin ceiling on usable imagery. |

## Instruments

| Term | Meaning |
|---|---|
| **VIIRS** | Visible Infrared Imaging Radiometer Suite - the imaging instrument this project renders (true-color, thermal IR, day microphysics). The hero output. |
| **ATMS** | Advanced Technology Microwave Sounder - atmospheric temperature/humidity sounding. Downlinked and RT-STPS-decoded but not yet calibrated/rendered here. |
| **CrIS** | Cross-track Infrared Sounder - hyperspectral IR sounding. Same status as ATMS. |
| **OMPS** | Ozone Mapping and Profiler Suite (two sensors). Same status. |

## Tooling and software

| Term | Meaning |
|---|---|
| **SatDump** | Open-source SDR software. In the async path it demodulates/decodes DigIF I/Q to CADU and renders composite imagery; it can also georeference its own composites via a built-in VIIRS scan model (parked at 1.2.2 - see `docs/SATDUMP.md`). |
| **RT-STPS** | Real-Time Software Telemetry Processing System (NASA DRL) - turns CADU (or demod/decode frames) into Level-0 RDR HDF5. Licensed; installed in the container image. |
| **CSPP** | Community Satellite Processing Package (CSPP SDR 4.1.1, from CIMSS/SSEC) - turns RDR into calibrated+geolocated Level-1 SDR/GEO. Licensed. Needs the J01 static LUTs and internet access for `sdr_luts.sh`. |
| **IPOPP** | International Planetary Observation Processing Package (NASA DRL) - the Level-2 EDR stage. Not run in this project (a missed-opportunity candidate). |
| **LUT** | Look-Up Table - CSPP's calibration/ancillary data. `sdr_luts.sh` fetches/stages them from `jpssdb.ssec.wisc.edu`; PERF-1 caches them in S3 to skip the ~10-min fetch on warm runs. |
| **CIMSS / SSEC** | Cooperative Institute for Meteorological Satellite Studies / Space Science and Engineering Center (University of Wisconsin-Madison) - the source of CSPP and the LUT server. |

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
