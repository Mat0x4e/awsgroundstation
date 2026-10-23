# Satellite-Comms / Ground-Segment Expert Review

Reviewer lens: RF downlink, CCSDS framing, NOAA/JPSS direct broadcast, AWS Ground Station.
Scope: domain correctness and clarity of the satcom aspects, missed opportunities, and a
project SWOT.

Repo: `/projects/sandbox/awsgroundstation` (branch `docs/post-batch-accuracy-review`).

Files examined:
- `README.md`
- `docs/ARCHITECTURE.md`
- `docs/SATDUMP.md`
- `docs/MEDITERRANEAN_PASS.md`
- `docs/CONTACTS.md`
- `docs/DEPLOYMENT.md`
- `docs/SYNC_PIPELINE.md`
- `docs/SYNC_ONBOARDING_INVESTIGATION.md`
- `scripts/plan_pass.py`, `scripts/satdump_process.sh`, `scripts/iq_extract.py`,
  `scripts/vita49_extract.py`
- `articles/article-4-file-formats.md`

---

## 1. Executive summary

This is an unusually rigorous ground-segment demonstrator. It owns the whole chain from a
rented AWS antenna to calibrated, geolocated VIIRS Level-1 products, and the documentation
is honest about where it fails: it distinguishes "reception clean" from "target
calibrated", measures geolocation against coastlines instead of asserting it, and records
the exact failure modes (PN double-derandomization, RDR filename parsing, the SatDump 0-GCP
segfault) that cost real time.

On domain correctness the signal chain is essentially right, and in several places more
careful than typical write-ups: the DigIF-vs-demod/decode PN distinction is correctly
inverted between the two pipelines (`PnEncoded=false` for the SatDump-upstream DigIF path,
`PnEncoded=true` for the on-antenna demod/decode path), the RF parameters
(7812 MHz / 30 MHz / RHCP) match the AWS JPSS reference, and the link-budget reasoning
behind the Mediterranean granule shortfall is sound and was correctly predicted before the
pass flew. I found no outright-wrong RF physics. The issues are of emphasis and a few
under-specified numbers (symbol rate vs sample rate, the CADU/VCDU frame-size arithmetic,
the "~30x" DigIF ratio), detailed in section 2.

The largest gap is not correctness but reach: the project is a single-satellite,
single-usable-station, single-instrument (VIIRS) demonstrator that has not yet exploited the
multi-instrument payload it already downlinks, the narrowband demod/decode path that would
cut cost ~30x, or multi-pass/multi-station stitching. Section 4's SWOT frames the real
external threats, chief among them the JPSS HRD direct-broadcast sunset horizon that
`docs/CONTACTS.md` already flags.

Bottom line: domain-accurate and well-told; the next value is in breadth (instruments,
stitching, latency) and in de-risking the external dependencies (HRD sunset, demod/decode
onboarding, CSPP/RT-STPS licensing).

---

## 2. Domain-correctness findings

Convention: "correct" = matches JPSS/CCSDS/AWS-GS reality; "refine" = not wrong but
under-specified or potentially misleading; "fix" = a statement a satcom reader would
challenge.

### 2.1 RF front end and polarization  -  CORRECT

`docs/SYNC_ONBOARDING_INVESTIGATION.md` (conformance table) lists
`7812 MHz / 30 MHz / RIGHT_HAND` and the demod chain
`IQ-Recombiner -> CCSDS_171_133_VITERBI_DECODER(ONE_HALF) -> NRZ_M_DECODER ->
UNCODED_FRAMES_EGRESS`. This is the JPSS-1/NOAA-20 X-band HRD service: ~7812 MHz center,
~30 MHz channel, RHCP, QPSK, rate-1/2 convolutional (Viterbi), NRZ-M, CCSDS (171,133)
Reed-Solomon, PN randomization. All consistent with the AWS public-broadcast JPSS
reference. Good.

One nuance worth stating explicitly somewhere: the `(171,133)` naming in the Viterbi node
is the convolutional code's generator polynomials (octal G1=171, G2=133, K=7, rate 1/2)  - 
it is NOT the Reed-Solomon (255,223). The current docs never spell out the inner/outer code
split (inner: K=7 r=1/2 convolutional + Viterbi; outer: RS(255,223) interleave depth 4).
A satcom reader can infer it; a newcomer cannot. **Refine.**

### 2.2 Symbol rate vs sample rate  -  REFINE (two numbers, both right, never reconciled)

Two different rates appear and are never tied together:
- `scripts/satdump_process.sh` runs SatDump with `--samplerate 34312500` (34.3125 Msps),
  and `articles/article-4-file-formats.md` repeats 34,312,500 Hz as the I/Q sample rate.
- `docs/SYNC_ONBOARDING_INVESTIGATION.md` lists the demod config `STR 15 Msps` (symbol
  transmit rate) with RRC rolloff 0.5.

Both are correct but describe different things, and nothing in the docs connects them. For
a satcom reader: HRD is QPSK at ~15 Msym/s channel-symbol rate (2 bits/symbol, rate-1/2
inner code => ~15 Mbps information / "15 Mbps HRD" as the docs call it). The 34.3125 Msps
is the DigIF ADC sample rate chosen to over-sample the ~30 MHz channel (~2.29 samples per
channel symbol). The README's "15 Mbps" (`README.md` cinematic, `T+0 AOS X-band HRD,
15 Mbps`) and CONTACTS' "10 min x 15 Mbps HRD" conflate information rate and the headline
figure. **Recommend one sentence** reconciling: 15 Msym/s QPSK channel symbols -> ~15 Mbps
after rate-1/2 -> digitized at 34.3125 Msps complex for DigIF. Cite `satdump_process.sh:80`
and `SYNC_ONBOARDING_INVESTIGATION.md` conformance table.

### 2.3 The async-vs-sync PN / Reed-Solomon distinction  -  CORRECT and well-handled

This is the subtle one, and the docs get it right in both directions:
- DigIF path: SatDump demodulates, Viterbi-decodes, RS-decodes AND derandomizes PN upstream
  of RT-STPS, so RT-STPS runs `PnEncoded="false"` with the `pn` node removed
  (`docs/DEPLOYMENT.md`, "RT-STPS" hard-won specifics; `docs/CONTACTS.md` contact #3
  "`PnEncoded=false` + PN node removed").
- demod/decode path: `UncodedFramesEgress` delivers frames that are Viterbi+NRZ-M decoded
  but NOT RS-decoded and NOT PN-derandomized, so RT-STPS must do both ->
  `PnEncoded="true"`, the unmodified template (`docs/SYNC_PIPELINE.md`, "What is
  materially different", and the manual-prereqs table).

The failure-mode note  -  set it backwards and RT-STPS XORs clean data back into noise so RS
fails and every frame is silently discarded (`docs/SYNC_PIPELINE.md`; `docs/DEPLOYMENT.md`
RT-STPS note)  -  is exactly the real-world trap. This is the single best piece of domain
writing in the repo. **Keep.**

One precision point to tighten (**refine**): `docs/SYNC_PIPELINE.md` says
`UncodedFramesEgress` gives "QPSK demod + Viterbi + NRZ-M, no RS, no PN". That is the AWS
`UncodedFramesEgress` definition (it egresses frames that are still RS-coded and
PN-randomized). Good. But one sentence elsewhere (`SYNC_PIPELINE.md` sequence diagram note
`no Reed-Solomon, no PN`) could be misread as "RS was stripped." It means "RS/PN not yet
removed; RT-STPS does it." Worth a two-word clarification ("not yet removed") so a reader
does not conclude the frames are already clean. Note the name itself is a known AWS
footgun: "Uncoded" refers to the convolutional (inner) code having been removed, not the
outer RS.

### 2.4 CADU / VCDU framing and the frame-size arithmetic  -  REFINE

`docs/ARCHITECTURE.md` processing-chain diagram: ".cadu clean CCSDS frames 22 x 53.6 MiB".
`articles/article-4-file-formats.md`: ".cadu  -  1024-byte frames ~56 MB / chunk (40:1
reduction)". `docs/CONTACTS.md` contact #5: "22 x `npp_hrd.cadu` (53.6 MiB each)".

The CADU size is right in spirit and the 1024-byte frame length is correct for JPSS
(CADU = 4-byte ASM `1ACFFC1D` + 1020-byte RS-coded VCDU; the VCDU itself is 892 bytes of
M_PDU + RS(255,223) parity x interleave 4 = 128 bytes, plus the CCSDS frame structure  -  the
standard JPSS CADU is 1024 bytes). What is missing and would help a satcom reader:
- State that CADU = ASM + RS-codeblock(VCDU), 1024 bytes, and that at this stage the CADU
  still carries RS parity (SatDump has error-corrected but RT-STPS consumes the framed
  CADU). The article calls the step "demux + CCSDS reassembly"; a reader may wonder where RS
  lives. **Refine.**
- The "40:1 reduction" (article) and the DigIF "~30x" ratio (ARCHITECTURE cost table,
  SYNC_PIPELINE) are two different ratios cited as if independently obvious. 2.18 GB pcap ->
  ~53.6 MiB CADU is ~41:1, consistent with 34.3125 Msps complex int8 (2 B/sample =
  ~68.6 MB/s raw) down to ~15 Mbps coded frames (~1.9 MB/s) ~= 36x, close to the stated 30x
  once pcap/UDP/IP overhead is added. The numbers hang together; the docs would be stronger
  if one place derived the ~30-40x from first principles rather than quoting it. **Refine.**

### 2.5 RDR (L0) vs SDR (L1)  -  CORRECT

`docs/ARCHITECTURE.md` and `article-4` label RDR as Level 0 (raw counts, per instrument)
and SDR+GEO as Level 1 (calibrated radiances + per-pixel lat/lon). Correct for the JPSS/CSPP
nomenclature (RDR = Raw Data Record, SDR = Sensor Data Record; EDR would be the next tier).
The CSPP recipe (`docs/DEPLOYMENT.md`) correctly identifies RT-STPS 7.0 as CADU->RDR and
CSPP SDR 4.1.1 as RDR->SDR/GEO, with the J01 LUTs and `sdr_luts.sh` ancillary fetch. The
"keep the `_j01_` RDR filename or spacecraft parses as BAD" detail
(`docs/DEPLOYMENT.md` req 1) is a genuine CSPP gotcha. Good.

### 2.6 VIIRS granule (~85 s) and the link-budget shortfall  -  CORRECT, and a model of honest reporting

`docs/DEPLOYMENT.md` "Known limit": a VIIRS granule needs ~85 s of continuous downlink;
contact #5 got 8 science granules, GEO for 4, calibrated SDR for 1, and the early granules
failed `SDR_PREREQ_ABSENT VIIRS-SCIENCE-RDR` / `PRO_CROSSGRAN_FAIL`. The ~85.5 s VIIRS
granule (48 scans x 1.779 s) is correct. The reasoning  -  the Mediterranean is scanned in
the opening ~10-115 s at 11-17 deg elevation at maximum slant range, where link margin is
lowest and packets drop, so the opening granule is the one that fails to calibrate  -  is
textbook-correct direct-broadcast link-budget behavior, and `docs/MEDITERRANEAN_PASS.md` S3
predicted it before the pass flew. This is exactly how a ground-segment engineer should
reason. **Keep; this is a strength.**

One quantitative gap (**refine**): the docs assert "11-17 deg, max slant range, packets
lost" qualitatively but never show an actual link budget (EIRP, G/T, path loss at the
~2150 km slant range vs nadir ~833 km, required Eb/No for QPSK r=1/2 RS at the target BER,
resulting margin in dB). The whole project turns on this margin, and a ~6 dB extra path
loss at 17 deg vs zenith is the crux. A one-page link-budget table would convert a credible
narrative into a defensible one and would directly feed pass ranking (see 3.3).

### 2.7 Geolocation: scan model vs per-pixel GEO  -  CORRECT and carefully scoped

`docs/SATDUMP.md` and `docs/DEPLOYMENT.md` geolocation-accuracy table correctly separate:
- SatDump composite stretched over a bbox: 100-300 km (TLE-only, 5 s timing ~= 40 km nadir
  shift, no terrain correction, linear geo->pixel against a curvilinear bow-tie scan).
- SatDump `scan_geometry.py` per-pixel raytrace from CBOR ephemeris: tens of km, measured
  91.8% land/sea agreement, MCC 0.783.
- CSPP SDR + GITCO/GMTCO: sub-km, NOAA per-pixel terrain-corrected.

The VIIRS scan model params in `docs/SATDUMP.md` (112 deg scan = +/-56 deg, aggregation-zone
`forced_gcps_x`, roll/yaw offsets, 1.786 s/scan, 32 I-band lines/scan at 0.0555 s/line) are
consistent with real VIIRS geometry (3:1/2:1/1:1 aggregation zones, bow-tie overlap growing
toward the swath edge). The GITCO (I-band terrain-corrected) vs GMTCO (M-band
terrain-corrected) distinction in `docs/MEDITERRANEAN_PASS.md` S8 is correct, and preferring
terrain-corrected over ellipsoid (`*-GEO-TC`) is the right call. The honest accounting of
the two CBOR quirks (2^32 s epoch wrap; the wrapped clock leaking into the rotation frame)
is impressive debugging and correctly documented. **Keep.**

Minor (**refine**): the "5 s timing error ~= 40 km nadir shift"  -  at ~6.9 km/s ground-track
speed a 5 s error is ~34 km along-track; 40 km is fine as an order-of-magnitude but it is an
along-track shift, not an isotropic one. Worth stating it is along-track.

### 2.8 Swath geometry correction (contact #5 re-render)  -  CORRECT direction, verify the footprint

`docs/CONTACTS.md` contact #5 documents correcting the cross-track extension from a flat
tangent to a spherical ground arc (1494 km vs 1222 km half-swath for VIIRS, "matching the
documented ~3000 km swath"), split between lat/lon by ground-track bearing with lon scaled
by cos(lat). Direction is right (a 3060 km full swath => ~1530 km half-swath; the code's
`SWATH_HALF_KM = 1530.0` in `plan_pass.py` agrees). The re-render footprint "941 x 2930 km"
has cross-track (~2930 km) and along-track (~941? or 200 km?) that read oddly against "true
footprint ~3000 km cross-track by 200 km along-track" in the same paragraph  -  the final
sentence says 8.5 deg x 35.2 deg = 941 x 2930 km but then calls true ~3000 x 200. The
along-track figure for a ~30 s chunk should be ~200 km (6.9 km/s x 30 s), so a 941 km
along-track extent suggests the render still covers more than one chunk or mixes axes.
**Refine / verify**  -  reconcile which axis is 941 vs 2930 and whether it is one chunk or the
stitched pass.

### 2.9 Scripts  -  domain-accurate

- `scripts/iq_extract.py`: VITA-49 header bit layout (type bits 31-28, TSI/TSF, C/T,
  4-bit mod-16 packet count, 16-bit size in words) is correct per VITA-49.2; cs8 interleaved
  I/Q; sample-rate validation against 34,312,500 Hz +/-1 Hz; gap-fill with zeros on
  sequence discontinuity  -  all correct and the right thing to do for a demod front end
  (zero-fill keeps sample timing so the demod PLL does not slip). Good.
- `scripts/vita49_extract.py`: radix-20 64-bit and radix-7 16-bit fixed-point parsing for
  context fields is correct VITA-49.
- `scripts/plan_pass.py`: SGP4 + TEME->ECEF via GMST (IAU 1982) + WGS84 geodetic + swath
  sweep + sun glint angle. The glint-angle definition (angle between specular reflection and
  view direction, small = in the glint lobe) is correct and genuinely useful for the
  wake-detection discussion. `SWATH_HALF_KM=1530`, `SWATH_CORE_KM=1000` with the I-band
  ground-sample-degradation note is correct VIIRS behavior.
- `scripts/satdump_process.sh`: `--tle_override` to skip the broken CelesTrak updater, with
  a fresh-fetch-then-bundled-fallback and a hard refusal to run with no TLE, is the correct
  mitigation for the 0-GCP/global-bounds segfault. Good.

No domain errors found in the sampled scripts.

### 2.10 Summary table

| # | Topic | Verdict | Cite |
|---|---|---|---|
| 2.1 | 7812/30/RHCP, QPSK/Viterbi(171,133)/NRZ-M | correct; spell out inner vs outer code | `SYNC_ONBOARDING_INVESTIGATION.md` conformance table |
| 2.2 | symbol rate vs sample rate | refine; reconcile 15 Msym/s vs 34.3125 Msps | `satdump_process.sh:80`; `article-4` S1 |
| 2.3 | async PnEncoded=false vs sync PnEncoded=true | correct; best domain writing in repo | `DEPLOYMENT.md` RT-STPS; `SYNC_PIPELINE.md` |
| 2.4 | CADU/VCDU 1024 B, 30-40x reduction | refine; show the arithmetic + where RS lives | `ARCHITECTURE.md` chain; `article-4` |
| 2.5 | RDR L0 / SDR L1 | correct | `ARCHITECTURE.md`; `DEPLOYMENT.md` |
| 2.6 | ~85 s granule, link-margin shortfall | correct; add a real link budget | `DEPLOYMENT.md` Known limit; `MEDITERRANEAN_PASS.md` S3 |
| 2.7 | scan model vs per-pixel GEO, GITCO/GMTCO | correct | `SATDUMP.md`; `DEPLOYMENT.md` geo table |
| 2.8 | swath-arc re-render footprint | verify 941 vs 2930 km axis | `CONTACTS.md` contact #5 |
| 2.9 | scripts (VITA-49, SGP4, glint, TLE) | correct | `iq_extract.py`, `plan_pass.py`, `satdump_process.sh` |

---

## 3. Clarity assessment

### For a satcom-literate reader  -  strong
- The DigIF-vs-demod/decode split is the clearest thing in the repo, with the correct PN
  inversion called out at every layer (`SYNC_PIPELINE.md`, `DEPLOYMENT.md`).
- "Reception clean, target not calibrated" (`CONTACTS.md` contact #5) is exactly the right
  framing: it separates the RF link outcome from the science-product outcome, which many
  write-ups conflate.
- Geolocation is measured (land/sea agreement vs Natural Earth, MCC) not asserted  -  rare and
  commendable.

Where it is muddy for the expert:
- No consolidated link budget anywhere (2.6). The reader has to take "11-17 deg = packets
  lost" on faith.
- The two rate figures (2.2) are never reconciled; an expert will notice and wonder.
- The "~30x" / "40:1" / "53.6 MiB" numbers are scattered (2.4); one derivation would settle
  them.

### For a newcomer  -  mostly good, a few cliffs
- `article-4-file-formats.md` is an excellent on-ramp: "strip a container, add meaning", the
  byte-layout diagrams, the shrinking-data-growing-meaning throughline. Best newcomer doc.
- The `README.md` one-line pipeline + `ARCHITECTURE.md` cinematic sequence give the shape
  quickly.
- Cliffs: acronym density (CADU, VCDU, RDR, SDR, EDR, CSPP, RT-STPS, GITCO/GMTCO, HRD,
  DigIF, NRZ-M, PN, RS) with no single glossary. A newcomer meets "PnEncoded" before anyone
  defines PN randomization (why a downlink is scrambled: spectral whitening / bit-transition
  density for receiver clock recovery). **Add a one-page glossary + a two-sentence "why PN
  exists" note.**
- "Uncoded"FramesEgress is never explained as "inner convolutional code removed, RS/PN
  still present"  -  a newcomer will read "uncoded" as "no coding." (2.3)

### Concrete clarity recommendations
1. Add `docs/GLOSSARY.md` (or a README section): HRD, DigIF, VITA-49, I/Q, QPSK, Viterbi,
   NRZ-M, PN, RS, CADU/VCDU, RDR/SDR/EDR, CSPP/RT-STPS, GITCO/GMTCO, granule.
2. Add a one-page link budget to `MEDITERRANEAN_PASS.md` or a new `docs/LINK_BUDGET.md`.
3. One sentence reconciling symbol rate (15 Msym/s) and ADC sample rate (34.3125 Msps).
4. Spell out inner (K=7, r=1/2 convolutional + Viterbi) vs outer (RS(255,223), I=4) coding
   once, in `ARCHITECTURE.md`.

---

## 4. Missed opportunities (ranked)

Ranked by value-to-effort for this project as a ground-segment solution. Effort/value are
rough (S/M/L).

### MO-1. Exploit the other instruments already in the downlink (ATMS, CrIS, OMPS x2)
Value: High. Effort: M.
RT-STPS already produces 5 RDRs per pass  -  VIIRS, CrIS, ATMS, 2x OMPS (`CONTACTS.md`
contact #5 outcome; `DEPLOYMENT.md` "RT-STPS ... 5 RDRs"). The project calibrates only
VIIRS. ATMS (microwave sounder) and CrIS (IR sounder) are the high-value NWP-assimilation
payloads and are far more robust to partial passes than VIIRS imagery (sounder footprints
are large; a dropped granule is less catastrophic than for 375 m imagery). CSPP ships
ATMS/CrIS SDR packages. Adding ATMS/CrIS SDR would: (a) produce usable product from the
exact low-elevation passes where VIIRS fails to calibrate, (b) demonstrate the NWP /
operational-meteorology value proposition, (c) reuse the entire existing RT-STPS+CSPP+Batch
spine. This is the single highest-leverage unused asset: the bytes are already on disk.

### MO-2. Use demod/decode (narrowband) delivery to cut ~30x data volume and cost
Value: High. Effort: M (blocked upstream  -  see Threats).
The async DigIF path ships ~30x the demodulated bitstream (`ARCHITECTURE.md` cost table;
`SYNC_PIPELINE.md`). The sync pipeline is already fully built and Terraform-deployed; it is
blocked only by NOAA-20 demod/decode onboarding on this account
(`SYNC_ONBOARDING_INVESTIGATION.md`). Flying it would drop per-contact transfer/compute and
remove the entire chunk fan-out. The missed opportunity is partly outside the team's control
(onboarding), but two things are in control and not yet done: (a) clear the REL-5
packet-format validation gate offline-as-far-as-possible and have the capture tooling
rehearsed so the first real sync contact is productive; (b) quantify the exact cost delta
(the docs say "~30x" but never put a dollar figure on DigIF vs demod/decode per contact) to
make the business case to AWS for onboarding.

### MO-3. A real link budget feeding pass ranking (cost-per-usable-granule)
Value: High. Effort: S-M.
The planner ranks on coverage/swath/sun geometry (`plan_pass.py`) but not on predicted link
margin, and there is no link budget (2.6). The project's defining failure (basin scanned at
low elevation, opening granule lost) is a link-margin failure. Add: (a) a parametric link
budget (EIRP, G/T per station, free-space + atmospheric loss vs elevation, required Eb/No),
(b) a predicted-margin-vs-elevation curve, (c) a ranking metric of **cost-per-usable-granule**
= contact_cost / E[granules that survive to calibration], where the expectation folds in the
elevation-at-scan-time profile. This turns "we picked the right geometry and the link still
lost" (`MEDITERRANEAN_PASS.md` S3) into a predictive, defensible selection and would have
flagged contact #5 as low-yield before spending $110.

### MO-4. Multi-pass / multi-station stitching across consecutive orbits
Value: Medium-High. Effort: M-L.
Every pass is treated independently. NOAA-20's ~101 min orbit means consecutive passes over
a region (or the same pass caught by two onboarded stations) could be stitched into a larger
mosaic, and a target that is always in the low-elevation opening granule from Stockholm
might be mid-pass (high elevation) on an adjacent orbit or from Ohio/Oregon for other
regions. The planner already propagates SGP4 over N days (`plan_pass.py`); extending it to
rank *sequences* of contacts (and to de-duplicate overlapping swath coverage) is a natural
next step. Pairs with MO-3.

### MO-5. Low-latency / near-real-time path (direct-broadcast as intended)
Value: Medium-High. Effort: M (depends on MO-2).
Current wall-clock is ~100 min AOS-to-L1 (`README.md`, `ARCHITECTURE.md`). JPSS HRD direct
broadcast exists for low-latency regional use (fire, volcanic ash, NWP cutoff times). The
demod/decode path (MO-2) delivers frames live during the contact and runs RT-STPS once on
the receiver immediately after LOS (`SYNC_PIPELINE.md`)  -  a far shorter chain. Positioning
the project as a *low-latency* direct-broadcast demonstrator (minutes, not ~100 min) is a
stronger story than batch reprocessing and is the natural use of the already-built sync
path. Report a latency SLO (AOS-to-product) as a headline metric alongside cost.

### MO-6. Antenna / elevation-mask optimization and station selection per target
Value: Medium. Effort: S.
The effective Stockholm mask is ~10 deg (`MEDITERRANEAN_PASS.md` S2), which costs ~2
sub-basins per pass at the southern edge. The project uses whichever station reaches the
target, but does not optimize station choice against the target's elevation-at-scan-time or
model the per-station mask/horizon profile (terrain, local obstructions). A small addition:
per-station elevation-mask curves and an explicit "which station gives the best link while
the swath is on target" selector. Low effort given `plan_pass.py` already does per-station
elevation.

### MO-7. TLE / ephemeris quality handling
Value: Medium. Effort: S.
`satdump_process.sh` fetches a fresh TLE with a bundled fallback  -  good. But TLE age is a
first-order geolocation error (`DEPLOYMENT.md`: 5 s timing ~= 34-40 km). The project does
not (visibly) check or report TLE epoch age at pass time, nor prefer the freshest of
multiple sources (CelesTrak vs Space-Track vs onboard GPS ephemeris in the CBOR). The
`scan_geometry.py` path already proves the CBOR ephemeris agrees with SGP4 to 0.8 km
(`SATDUMP.md`)  -  so **prefer the CBOR/onboard ephemeris for geolocation and relegate the TLE
to planning only**, and emit a TLE-epoch-age warning when planning far from epoch.

### MO-8. Higher-level products (IPOPP EDRs, NWP assimilation)
Value: Medium (demonstrator) / High (operational). Effort: L.
The chain stops at SDR/GEO (L1). The JPSS direct-broadcast community standard for EDRs is
IPOPP (which the demod/decode reference explicitly pairs with RT-STPS  - 
`SYNC_ONBOARDING_INVESTIGATION.md` references the RT-STPS + IPOPP use case). Adding IPOPP (or
Polar2Grid for imagery EDRs, Satpy for flexible compositing) would produce cloud mask, SST,
active-fire, AOD  -  the products that make direct broadcast operationally valuable. Large
effort; frame as a roadmap item, not immediate.

### MO-9. S-band / other-satellite reuse (Aqua, Terra, S-NPP, Metop)
Value: Medium. Effort: M.
The spine (station -> S3/receiver -> SatDump/RT-STPS/CSPP -> product) is largely satellite-
agnostic. S-NPP (same VIIRS/ATMS/CrIS, `npp.xml` already noted in `DEPLOYMENT.md`) is the
obvious reuse  -  though note S-NPP HRD DB sunsets 2026-11-02 (`CONTACTS.md`). Aqua/Terra
(MODIS, X-band) and Metop (L-band/S-band) broaden the demonstrator and hedge the single-
satellite risk (Threats). Even documenting *what would change per satellite* (frequency,
modulation, RT-STPS config, CSPP package) is a cheap, valuable artifact.

### MO-10. Ground Station Agent (wideband) vs dataflow endpoint
Value: Low-Medium. Effort: M.
The docs reference the dataflow-endpoint model for sync (`SYNC_ONBOARDING_INVESTIGATION.md`).
AWS's newer Ground Station Agent (wideband DigIF over the agent, EC2-side) is an alternative
delivery mode worth at least a documented comparison vs the S3 DigIF and
dataflow-endpoint-demod/decode modes the project uses  -  delivery-mode selection is a genuine
ground-segment design axis the docs do not currently map.

---

## 5. SWOT

### Strengths
- **End-to-end ownership, correctly implemented.** RF (VITA-49 DigIF) -> I/Q -> SatDump
  (QPSK/Viterbi/RS/PN) -> CADU -> RT-STPS -> RDR -> CSPP -> SDR/GEO -> GeoTIFF, with the hard
  CSPP conditions pinned down deterministically (`DEPLOYMENT.md`).
- **Correct, subtle PN handling** differentiated between DigIF and demod/decode paths  -  the
  exact trap that breaks most first attempts (`SYNC_PIPELINE.md`, `DEPLOYMENT.md`).
- **Measured, not asserted, geolocation** (land/sea agreement, MCC, SGP4 cross-check to
  0.8 km) with three characterized accuracy tiers (`SATDUMP.md`, `DEPLOYMENT.md`).
- **Honest failure reporting**: "reception clean, target not calibrated"; the
  catch-to-success visualization trap flagged repeatedly; link-margin shortfall predicted
  before the pass (`CONTACTS.md`, `MEDITERRANEAN_PASS.md`).
- **Serverless, idempotent, cost-aware** compute (Batch Spot fan-out, on-demand aggregation,
  per-contact trigger, ~$1-2 compute vs ~$110 antenna) with the cost dominated correctly by
  antenna time (`ARCHITECTURE.md` cost table).
- **A fully-built second (demod/decode) pipeline** ready to fly the moment onboarding clears.
- **Strong documentation and a genuinely good newcomer article** (`article-4`).

### Weaknesses
- **Single instrument used (VIIRS) of five downlinked**  -  ATMS/CrIS/OMPS RDRs produced then
  discarded (MO-1).
- **No link budget**; pass ranking ignores predicted margin, the project's defining failure
  axis (MO-3, 2.6).
- **Single usable station for the flagship target** (Stockholm 1 only reaches the Med) and
  no multi-pass/multi-station stitching (MO-4).
- **~100 min AOS-to-L1 latency**  -  batch, not the low-latency direct broadcast the payload
  is designed for (MO-5).
- **Fragile manual dependencies**: `/opt/rt-stps` and `/opt/SDR_4_1` hand-installed, in no
  IaC; drift only *detected*, not reconstructable (`ARCHITECTURE.md` constraints).
- **SatDump native projection parked** (0-GCP segfault at 1.2.2); the active geolocation path
  is an explicitly "do not extend" fallback (`SATDUMP.md`).
- **A few unreconciled domain numbers** (symbol vs sample rate; 30x/40:1; the 941/2930 km
  footprint axis) that a reviewer will notice (2.2, 2.4, 2.8).

### Opportunities
- Calibrate ATMS/CrIS -> robust product from exactly the low-elevation passes where VIIRS
  fails; demonstrate NWP value (MO-1).
- Fly demod/decode once onboarded: ~30x less data, far lower cost, near-real-time (MO-2,
  MO-5); quantify the cost delta to make the onboarding business case.
- Cost-per-usable-granule ranking from a real link budget (MO-3); multi-pass stitching
  (MO-4).
- Prefer CBOR/onboard ephemeris for geolocation; TLE-age warnings (MO-7).
- Higher-level EDRs via IPOPP/Polar2Grid/Satpy (MO-8); multi-satellite reuse (MO-9).

### Threats (external / real-world)
- **JPSS HRD direct-broadcast sunset horizon.** S-NPP HRD DB ceases 2026-11-02; NOAA-20 is
  next-oldest in the series and a cessation notice would arrive on the same `cspp_registrants`
  list in the same format (`CONTACTS.md` program-horizon banner). The entire async pipeline's
  useful life is bounded by NOAA-20 HRD DB remaining live, and the sunset date is **not yet
  recorded**. This is the dominant existential threat; monitoring the list is mission-critical.
- **demod/decode onboarding / eligibility.** NOAA-20 shows zero schedulable sync windows on
  this account  -  onboarded for DigIF, not for antenna-downlink-demod-decode
  (`SYNC_ONBOARDING_INVESTIGATION.md`). The ~30x cost reduction and low-latency story are
  gated on an AWS-side process outside the team's control.
- **Spectrum / licensing.** X-band direct-broadcast reception is spectrum-licensed; AWS GS
  onboarding bundles spectrum licensing. Any change in NOAA broadcast policy or AWS GS
  satellite support removes access with no local recourse.
- **Reliance on licensed, non-redistributable tooling** (CSPP SDR, RT-STPS from CIMSS/SSEC;
  SatDump pinned at 1.2.2). The CSPP/RT-STPS install is "weeks of manual work" and not
  reconstructable from IaC (`ARCHITECTURE.md`); a vendor license change, a lost `/opt`
  volume, or a forced SatDump upgrade (which would re-break the projection patch  - 
  `SATDUMP.md`) each threaten the pipeline.
- **Spot capacity.** The chunk fan-out runs on Batch Spot; a Spot capacity crunch in-region
  lengthens or stalls processing (mitigated by idempotent chunks + `ToleratedFailurePercentage=100`,
  but still a throughput threat).
- **Link-margin ceiling.** Rented-antenna G/T + NOAA-20 EIRP fix an upper bound on usable
  granules per pass that no software change can raise; targets at a station's reach edge are
  structurally capped (`DEPLOYMENT.md` Known limit; `MEDITERRANEAN_PASS.md` S3).
- **TLE/ephemeris and shared-antenna contention.** CelesTrak reachability already bit the
  TLE updater (`SATDUMP.md`); Stockholm 1 is a shared antenna and geometrically-ideal passes
  are often simply not offered (`MEDITERRANEAN_PASS.md` S2), capping target revisit.

---

## 6. Priority recommendations (synthesis)

1. **Record the NOAA-20 HRD DB sunset date** (Threats #1)  -  it bounds everything; it is an
   information-gathering task, not engineering.
2. **Calibrate ATMS/CrIS** (MO-1)  -  highest value-to-effort; the bytes are already downlinked
   and RDR'd.
3. **Add a link budget + cost-per-usable-granule ranking** (MO-3)  -  converts the project's
   defining failure mode into a predictive capability.
4. **Press demod/decode onboarding with a quantified cost case + rehearse REL-5** (MO-2,
   MO-5)  -  unlocks ~30x cost and low-latency.
5. **Reconcile the domain numbers and add a glossary + inner/outer-code note** (2.2, 2.4,
   section 3)  -  cheap credibility fixes for a satcom-literate audience.

---

### Documentation / correctness nitpicks (quick list, all "refine")
- Reconcile 15 Msym/s symbol rate vs 34.3125 Msps ADC sample rate with one sentence
  (`satdump_process.sh:80`, `article-4` S1, `README.md` "15 Mbps").
- Spell out inner (conv K=7 r=1/2 + Viterbi) vs outer (RS(255,223), I=4) coding once
  (`ARCHITECTURE.md`).
- Clarify "UncodedFramesEgress" = inner code removed, RS/PN still present ("not yet
  removed") (`SYNC_PIPELINE.md`).
- Derive the ~30-40x DigIF-to-frame reduction once from sample rate vs coded bitrate;
  reconcile with "40:1" and "53.6 MiB" (`article-4`, `ARCHITECTURE.md`).
- State the CADU = ASM(1ACFFC1D) + RS-codeblock(VCDU) = 1024 B, and that CADU still carries
  RS parity at the SatDump->RT-STPS boundary (`article-4`, `ARCHITECTURE.md`).
- Note the "5 s ~= 40 km" shift is along-track (`DEPLOYMENT.md` geo table).
- Reconcile the contact #5 re-render footprint axes (941 vs 2930 km; one chunk vs stitched)
  (`CONTACTS.md` contact #5).
- Add `docs/GLOSSARY.md`.
