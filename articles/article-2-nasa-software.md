# Getting Labelled Earth Images from Space — Part 2: The NASA Software Stack, Capabilities and Limits

[Part 1](./article-1-cloud-opensource.md) described a cloud-based pipeline that turns raw NOAA-20 radio signals into VIIRS imagery using AWS Ground Station and SatDump, with one limit: map overlays land 100–300 km from the actual terrain. This article covers the standard remedy — NASA's direct-broadcast processing software — what it adds, and what it requires to operate.

## The target: per-pixel geolocation

Government agencies process this downlink with a well-defined chain:

- **RT-STPS** (Real-Time Software Telemetry Processing System, NASA) ingests CADU frames and produces Level 0 *RDR* files — raw instrument science data in HDF5.
- **CSPP SDR** (Community Satellite Processing Package, University of Wisconsin/CIMSS) turns RDRs into Level 1 *SDR* products: calibrated radiances, plus GEO files containing terrain-corrected latitude and longitude for every pixel, at sub-kilometre accuracy.

Both packages are free to download. Both were designed for a long-lived Linux workstation rather than an ephemeral cloud container — a design assumption that determines most of what follows.

## RT-STPS: three configuration issues

Both packages were added to the pipeline's Docker image, with RT-STPS fed the CADU frames produced by SatDump. Getting from installed to working required resolving three issues.

**1. Spacecraft configuration.** RT-STPS ships one configuration file per satellite. `npp.xml` is for Suomi-NPP, NOAA-20's predecessor; NOAA-20 (JPSS-1) requires `jpss1.xml`. With the wrong file, RT-STPS produces no output and no error message.

**2. Insufficient data per chunk.** Each pipeline container processes one 30-second chunk, but a VIIRS granule requires about 85 seconds of data, so RT-STPS produced nothing per chunk. This changed the architecture: every container uploads its CADU to S3, and a final aggregation step concatenates all chunks (25 in the run used here) into a single 1.3 GB stream before running RT-STPS once.

**3. PN encoding mismatch.** Satellite downlinks are scrambled with a pseudo-noise (PN) sequence, and RT-STPS's default configuration removes it. SatDump, however, had already removed it during demodulation. RT-STPS therefore XORed clean frames with the PN sequence — corrupting them — after which every Reed-Solomon check failed and all 54,868 frames were discarded, without any error message. Frame inspection confirmed the input was valid: correct sync marker `1A CF FC 1D`, correct spacecraft ID 159, correct VIIRS virtual channel. The fix is two changes in the XML configuration: `PnEncoded="false"` and removing the `pn` node from the processing chain.

With these three fixes in place, RT-STPS processed a full pass into five RDR files — 344 MB of VIIRS, plus CrIS, ATMS and two OMPS instruments. Level 0 was operational.

## CSPP SDR: the blocking issue

CSPP SDR — the 845 MB package that provides the per-pixel geolocation — found the RDRs and started, then stopped at its installation check: missing NOAA-20 support files. Two supplementary tarballs (stray-light correction LUTs and static terrain tiles) resolved that.

The next error was of a different kind:

```
ERROR: Installation problem SDR_4_1_DB/package needs to exist
```

CSPP requires an initialized lookup-table cache database, created by an installation script called `sdr_luts.sh`. That script did not complete inside a Docker build on AWS CodeBuild under any tested variant: different flags, different working directories, execution at build time and at runtime. Increasing the hardware to CodeBuild's largest tier — 72 vCPUs, 145 GB of RAM, 90-minute timeouts — did not change the outcome. Creating the expected directory manually passed the installation check but led to an indefinite hang on `wait for cache db initialization`, as CSPP assumed another process was mid-setup.

The root cause is architectural rather than a bug: CSPP assumes a persistent machine — state that survives between runs, ancillary data refreshed from the internet, one installation maintained over time. An immutable container rebuilt from scratch violates each of those assumptions.

## Assessment

The NASA stack's capabilities are real: calibrated, science-grade Level 1 products with terrain-corrected coordinates for every pixel — beyond what the open-source path provides. RT-STPS, once its configuration issues are known, runs reliably in a container.

CSPP SDR, in this cloud-native setting, could not be made to run, and the attempt was eventually abandoned. The requirement it was meant to satisfy — usable per-pixel geolocation — was ultimately met by a capability already present in software the pipeline was running. That is the subject of Part 3.

*~700 words*
