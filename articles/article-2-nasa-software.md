# Getting Labelled Earth Images from Space — Part 2: What the NASA Stack Adds

[Part 1](./article-1-cloud-opensource.md) built a cloud pipeline that turns raw NOAA-20 radio signals into VIIRS imagery for about $160 a pass, with one limit: map overlays landed 100–300 km from the terrain, because the composites carry no per-pixel coordinates.

This part closes that gap with the software the agencies use themselves. The result, from the same 13-minute pass:

![Contact #2 true colour with coastline overlay](../output/contact-02_ohio-1_2026-06-23/NASA-SDR/noaa20_viirs_truecolor_overlay_contact02.png)

Hudson Bay, the Great Lakes, Florida, Cuba and the Bahamas, each sitting on its own coastline. Same antenna, same $130 of downlink, same zero licence fees — the error went from 100–300 km to **sub-kilometre**.

## Two chains, two file trails

Both pipelines share a front end: the clean CADU frames that [Part 1](./article-1-cloud-opensource.md)'s SatDump step produces. From there they diverge, and the divergence is entirely about what each file format carries.

```mermaid
flowchart TD
    PCAP[".pcap — VITA-49 DigIF<br/>raw digitized RF · ~58 GB / pass"]
    CS8[".cs8 — raw I/Q<br/>headerless complex int8"]
    CADU[".cadu — clean CCSDS frames"]
    PCAP -->|"de-encapsulation"| CS8
    CS8 -->|"SatDump: QPSK + Viterbi + Reed-Solomon"| CADU

    subgraph OS["Open-source path — Part 1"]
      PNG["composite PNG<br/>display image · pixels only<br/>coordinates estimated from orbit<br/>→ 100–300 km error"]
    end
    subgraph NASA["NASA path — Part 2"]
      RDR["RDR HDF5 · Level 0<br/>raw counts, per instrument"]
      SDR["SDR + GEO HDF5 · Level 1<br/>radiances + per-pixel lat/lon"]
      TIF["GeoTIFF<br/>georeferenced raster → sub-km"]
      RDR -->|"CSPP SDR: calibrate + geolocate"| SDR
      SDR -->|"project + render"| TIF
    end
    CADU -->|"SatDump render"| PNG
    CADU -->|"RT-STPS: demux + CCSDS reassembly"| RDR
```

[SatDump](./article-1-cloud-opensource.md) renders CADUs straight to composite PNGs — display images whose pixels are correct but whose coordinates are attached afterwards by propagating the orbit, which is where the 100–300 km error lives. The NASA chain instead keeps the science all the way down: RDRs hold raw counts, SDRs hold calibrated physical radiances, and GEO files hold terrain-corrected latitude and longitude for *every pixel*.

## What the official chain produces

Two free packages do the work:

- **RT-STPS** (NASA) ingests CADU frames and produces Level 0 **RDR** files — raw instrument science data in HDF5.
- **CSPP SDR** (University of Wisconsin/CIMSS) turns RDRs into Level 1 **SDR** products: calibrated radiances and brightness temperatures, plus **GEO** files carrying terrain-corrected latitude and longitude for *every pixel*.

That last item is the whole point. Part 1 estimated where the swath probably was; CSPP states where each pixel actually is, corrected for terrain. Calibration matters just as much: [SatDump](./article-1-cloud-opensource.md)'s composites are display images, while SDR radiances are physical quantities you can do science on.

![Architecture](diagrams/out/article-2-nasa-stack.png)

## What running them on ephemeral cloud requires

Both packages were written for a long-lived Linux workstation. Running them on ephemeral AWS infrastructure sets five requirements; each has a concrete solution, and with all five met the chain runs deterministically.

**A full granule of contiguous data.** A VIIRS granule needs ~85 seconds of continuous downlink, while the Part 1 pipeline works in 30-second chunks. *Solution:* every container uploads its CADUs to S3, and a separate step concatenates all 27 chunks into one stream, processed once. For contact #2 that produced an **863 MB VIIRS RDR** (plus CrIS, ATMS and two OMPS instruments).

**The NOAA-20 spacecraft configuration.** RT-STPS ships one XML per satellite. *Solution:* select `jpss1.xml` for NOAA-20 (`npp.xml` is Suomi-NPP).

**Frames descrambled exactly once.** Downlinks are scrambled with a pseudo-noise sequence, and SatDump already removes it during demodulation. *Solution:* tell RT-STPS the frames are already clean — `PnEncoded="false"` with the `pn` node dropped — so it does not XOR clean frames back into noise.

**The spacecraft token preserved in the RDR filename.** `viirs_sdr.sh` reads the spacecraft from the `_j01_` token *in the filename*. *Solution:* keep the `RNSCA-RVIRS_j01_….h5` name that RT-STPS emits; the token has to survive to the SDR step intact.

**A network path to the calibration LUT server.** This requirement is about where you run, not how. CSPP populates a calibration lookup-table cache by fetching from `jpssdb.ssec.wisc.edu` at setup time. CodeBuild reaches that host; the EC2 aggregation instance in this account does not. *Solution:* run the CSPP step in CodeBuild — chosen for egress, not compute — where it takes about 14 minutes.

That last requirement is the durable lesson, and it survives the fact that the chain works: software written for a persistent workstation expects state and network access that ephemeral, locked-down cloud compute does not grant by default. Deciding which execution environment holds the network path is an architecture question, not a bug to fix.

## What it's worth

The value is easy to state: the same pass and the same free software, with per-pixel geolocation improved by two to three orders of magnitude — from 100–300 km to sub-kilometre. The cost was entirely engineering time; the compute is ~14 CodeBuild minutes on top of a $160 pass.

## What makes a contact worth processing

The software can only calibrate what the antenna cleanly heard, so a pass's value is decided before any of this code runs. Three conditions separate a contact that yields a full true-colour mosaic from one that yields a single thermal granule:

- **Daylight over the target.** VIIRS's reflective bands — the ones true colour is built from — need sunlight. Contact #2 was a daytime pass and produced **10 fully calibrated granules**; a night pass has no reflective bands at all and gives thermal only. NOAA-20's sun-synchronous orbit crosses its daytime node near 13:25 local solar time, so booking that node is what makes true colour possible in the first place.
- **High elevation and clean RF.** Calibration is all-or-nothing per granule: a partial granule from RF packet loss cannot be calibrated and is dropped. A high-max-elevation pass keeps the spacecraft above the horizon longer on a stronger link, so more complete granules survive. Contact #3, at night and from a comparable raw RDR, yielded just one usable thermal granule.
- **A long enough pass to fill granules.** Granules are ~85 seconds each, so short or low passes produce fewer complete ones. The mosaic you can build is capped by how many contiguous granules the pass delivered — by what the antenna heard, not by what the software can do.

---

*Figures: architecture diagram generated with [awslabs/diagram-as-code](https://github.com/awslabs/diagram-as-code) from [`diagrams/article-2-nasa-stack.yaml`](diagrams/article-2-nasa-stack.yaml); the file-transformation diagram renders natively on GitHub/GitLab — export it as an image (e.g. via mermaid.live) before publishing to Medium or dev.to. The full CSPP recipe is in [`docs/DEPLOYMENT.md`](../DEPLOYMENT.md).*
