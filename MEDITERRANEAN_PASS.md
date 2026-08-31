# Mediterranean Pass — Planning & Booking Runbook

Goal: one daytime NOAA-20 contact that puts as much of the Mediterranean as possible
into a single sunlit, many-granule VIIRS acquisition.

Planner: [scripts/plan_pass.py](scripts/plan_pass.py) — SGP4 propagation + VIIRS swath
sweep + solar geometry, ranked against the windows AWS actually offers.

**Status 2026-08-27: all AWS-side facts below verified against the API.**

---

## 1. The reachability constraint

`ListContacts` answers *when the antenna sees the satellite*. It does not answer *what
VIIRS is looking at*. For a real-time direct-broadcast payload those are different
questions, and the second one is the binding constraint here.

Verified with `aws groundstation list-satellites` and `list-ground-stations`
(eu-central-1, 2026-08-27):

| | |
|---|---|
| Antennas onboarded for NOAA-20 | Cape Town 1, Hawaii 1, Ohio 1, Oregon 1, Stockholm 1 |
| Antennas reachable from eu-central-1 at all | the above + Ireland 1, Singapore 1 |
| Antennas within reach of the Mediterranean | **Stockholm 1 only** |
| Bahrain 1 | not available in this partition — the eastern-basin option does not exist |

Stockholm (59.4°N) to the basin is 1550–3100 km. NOAA-20 at 833 km clears a 10° horizon
out to ~2150 km, and VIIRS adds ±1530 km of cross-track swath. That reaches the western
and central Mediterranean and **never** reaches the eastern basin: across the full
offered set the Aegean, Sea of Crete and Levantine never once fall inside the swath
while the link is up. Gibraltar-to-Cyprus in one image is impossible anyway — the basin
is ~3800 km wide against a 3060 km swath.

## 2. Two constraints that only showed up against the live API

**The effective horizon mask is ~10°, not 5°.** Propagating the TLE through all 32
offered windows puts the elevation at the window edges in a tight 9.0–11.0° band
(median 10.5°). That is much higher than the 5° I first assumed, and it is *not* the
~19° that contact #4's logged duration implied — those CONTACTS.md figures are
unreliable. The higher mask pushes the reachable southern edge back past Sicily and
costs roughly two sub-basins per pass.

**This account cannot schedule contacts more than 7 days out.**
`ListContacts` returns `ResourceLimitExceededException` beyond that. Anything further
ahead can be planned but not reserved.

**AWS offers only a subset of the geometrically possible passes.** Of the daytime
~12:00 UTC passes the propagator finds, several inside the 7-day window are simply not
in the offer list (2026-08-30 12:19, 2026-09-01 11:42) — Stockholm 1 is a shared
antenna and those slots are taken. Plan against `ListContacts`, never against a
propagator alone.

## 3. The structural trade-off

The Mediterranean sits at the extreme southern edge of Stockholm's visibility, so it is
always scanned in the **first 10–115 seconds of the contact, at 11–24° link elevation**
— maximum slant range, minimum link margin, and inside the opening granule. That is the
regime that produced partial granules on contacts #1–#4.

The consequence, visible directly in the offered set:

- A pass with **high max elevation over Stockholm** (80–87°) puts the basin **1000–1500 km
  off nadir**, at the swath edge where the I-band ground sample degrades from 375 m to
  ~800 m. Four of the seven Med-capable offers are like this.
- A pass that puts the basin **near nadir** is by construction far from Stockholm →
  lower max elevation, weaker link.

Exactly one offered contact sits on the good side of that trade.

## 4. Recommendation — Monday 2026-08-31, 11:59:49 → 12:10:20 UTC

The only offered contact that gets the Mediterranean into the core swath. It is not a
close call: every other Med-capable offer is a swath-edge pass.

| | |
|---|---|
| Window (UTC) | 2026-08-31 11:59:49 → 12:10:20 (10m31s) |
| AWS max elevation | 51.23° |
| Sub-track | 40.3°N 15.2°E (Gulf of Taranto) → 75.2°N 15.6°W (Arctic) |
| Basins imaged | 5 — Balearic, Gulf of Lion, Ligurian, North Adriatic, South Adriatic |
| In the 375 m core swath | 4 of 5 (Balearic at the edge, 1013 km) |
| Sun elevation over the basin | 52–59° |
| Link elevation while scanning the basin | 11.6–17.4° |
| Expected volume | ~7.4 granules, ~21 × 2.18 GB `.pcap` chunks |

The North Adriatic passes 52 km off nadir and the Ligurian 428 km — both at full I-band
resolution. Late August is the clearest part of the Mediterranean year.

What it does **not** get: the Tyrrhenian and the Strait of Sicily are scanned *before*
AOS (the sub-track is already at 40.3°N when the link opens), so Italy's west coast
south of Naples is not in this acquisition.

## 5. What you will and will not see of an aircraft carrier

Straight answer: **you cannot see an aircraft carrier in VIIRS.** The arithmetic:

| | |
|---|---|
| VIIRS I-band ground sample | 375 m at nadir, ~800 m at swath edge |
| VIIRS M-band | 750 m |
| Nimitz-class, 333 m × 77 m deck | ~0.9 × 0.2 pixel, **~14 % of one I-band pixel** |
| Charles de Gaulle, 262 m × 64 m | ~12 % of one I-band pixel |

That 14 % fill is not nothing. Over calm dark water (reflectance ~0.03) a grey deck at
~0.2 lifts the mixed pixel to ~0.054 — roughly **1.8× the background**, comfortably
above I1 noise. So a carrier can produce **one anomalous bright pixel**. It cannot
produce a shape, and a single bright pixel is indistinguishable from a whitecap, a small
cumulus, a glint facet, or any of the thousands of merchant ships in the basin. A 400 m
container ship is *larger*.

The one signature with real geometric extent is the **wake**: a carrier at 30 kt drags a
turbulent wake a few hundred metres wide and tens of km long, and the damped capillary
waves inside it change surface roughness. Under moderate sunglint that shows up as a
dark or bright **linear streak several pixels long** — a documented MODIS/VIIRS
phenomenon.

The 2026-08-31 pass is a reasonable shot at that, by luck rather than design:

| Location | Off nadir | After AOS | Glint angle |
|---|---|---|---|
| **Naples (US 6th Fleet)** | **63 km** | +10 s | 37.2° |
| Toulon (FR carrier base) | 642 km | +80 s | 66.1° |
| South Adriatic | 222 km | +15 s | **28.3°** |

Naples is essentially at nadir at full 375 m — the best near-nadir look at a naval area
in the whole offered set — though at +10 s it lands in the opening granule, the one most
exposed to acquisition transients. Glint 37° is outside the tight specular lobe; the
South Adriatic at 28° is the part of this scene most likely to show wake streaks.
Souda Bay and Augusta Bay are both scanned before AOS on this pass and will not be in
the data.

If the actual goal is to identify a carrier, the honest tool is **Sentinel-2 L1C at
10 m** — free on the AWS Registry of Open Data, ~5-day revisit, and a carrier is a
recognisable 33 × 8 pixel object. This project's value is owning the RF-to-imagery
chain, not the resolution.

## 6. Booking

The scheduler cron is `DISABLED` in code
([modules/contact_scheduler/main.tf:44](modules/contact_scheduler/main.tf#L44)) and must
stay that way — re-enabling it via `terraform apply` previously booked two unintended
paid contacts. **Reserve manually.**

```bash
export AWS_PROFILE=AWSAdminAccess-471112743408 AWS_REGION=eu-central-1
export MP=arn:aws:groundstation:eu-central-1:471112743408:mission-profile/2655b0f6-8196-44d3-bbc0-3782b1942d34
export SAT=arn:aws:groundstation::471112743408:satellite/33f035e1-73f7-47a5-9df8-fbc48636dca8

# 1. Refresh the offer list (max 7 days ahead, or it throws)
aws groundstation list-contacts --status-list AVAILABLE \
  --ground-station "Stockholm 1" --mission-profile-arn "$MP" --satellite-arn "$SAT" \
  --start-time 2026-08-27T13:00:00Z --end-time 2026-09-03T12:50:00Z > contacts.json

# 2. Re-rank against the real windows
./test_env/Scripts/python.exe scripts/plan_pass.py --days 7 --contacts-json contacts.json --step 5

# 3. Reserve
aws groundstation reserve-contact --mission-profile-arn "$MP" --satellite-arn "$SAT" \
  --ground-station "Stockholm 1" \
  --start-time 2026-08-31T11:59:49Z --end-time 2026-08-31T12:10:20Z
```

Cost: ~$110–130 for a ~10.5-minute on-demand narrowband X-band contact, plus CodeBuild
processing. Same order as contacts #1–#4.

Check the forecast 24 h out before committing — clouds do not care about the budget.

## 7. Looking past the 7-day wall

The propagator (ignoring the offer set, 10° mask) finds better geometry in the second
week — 2026-09-04 12:24 and 2026-09-06 11:46 both reach 6–7 basins. Neither can be
reserved today. To chase one, re-run step 1 of §6 daily as they enter the 7-day window
and check whether AWS actually offers them; given the contention seen at Stockholm 1,
assume roughly even odds. Booking 2026-08-31 does not preclude this.

## 8. Processing notes

- Use the working CSPP recipe (see [CSPP_SOLVED.md](CSPP_SOLVED.md)) — keep the original
  `RNSCA-RVIRS_j01_*.h5` filename, install the J01 LUTs, run `sdr_luts.sh` online in
  CodeBuild.
- For anything ship-related, work in the **I-bands** (`SVI01`–`SVI05`, 375 m) with
  `GITCO` terrain-corrected geolocation, not the M-bands.
- **Product names: resolved 2026-08-27.**
  [scripts/viirs/geo_reader.py](scripts/viirs/geo_reader.py) and
  [scripts/viirs/visualize_nasa.py](scripts/viirs/visualize_nasa.py) accept the real
  CSPP names (`SVM15`/`GITCO`/`GMTCO`, `*-GEO-TC_All`) with the ellipsoid variants as
  fallback, terrain-corrected preferred. Verified end-to-end against contact #3's real
  `GMTCO`/`GITCO` from S3: (768, 3200) and (1536, 6400) per granule, sane lat/lon, zero
  masked pixels. Docstrings and error messages were still naming the fallbacks as
  primary and have been corrected.
- `h5py` is needed locally to exercise `geo_reader` outside the container; it is now
  installed in `test_env`.
- **RT-STPS output directory: resolved 2026-08-27.** 4 tests in
  `tests/test_rtstps_process.py` were failing. The production code was right and the
  tests were wrong: `config/jpss1.xml` declares
  `<RDR ... directory="../data"/>`, so RDRs land in a *sibling* of the working
  directory, never inside it — which is exactly what the deployed `aggregation.sh`
  relies on (`cd /opt/rt-stps`, harvest `/opt/data`). Fixtures now mirror that layout
  via an `rtstps_dirs` fixture, and a regression test pins the contract. Full suite: 144
  passing.
- The basin is scanned in the opening ~2 minutes at 11–17° link elevation. If granules
  come back partial, that is the cause, and it is inherent to imaging the Mediterranean
  from Stockholm — not a pipeline regression.
