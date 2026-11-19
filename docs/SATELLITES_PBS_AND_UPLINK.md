# Satellites: AWS Ground Station public broadcast and European uplink-capable satellites

Compiled 2026-11-18. Satellite status changes often; re-check the sources before relying on it.

---

## Part 1 - AWS Ground Station public broadcast satellites (PBS)

### Terminology used in this document

| Category | Definition | Onboarding procedure |
|---|---|---|
| **Public broadcast satellites (PBS)** | the Amazon Web Services (AWS) term: satellites with a public, downlink-only direct-broadcast path that AWS has validated. Aqua, Terra, Suomi National Polar-orbiting Partnership (SNPP), Joint Polar Satellite System 1, also called National Oceanic and Atmospheric Administration 20 (JPSS-1/NOAA-20) in the user guide; NOAA-21 in practice. | On request, through the Satellite Onboarding Questionnaire, for the ground stations you name. Not enabled by default. AWS handles licensing for sites not yet approved. |
| **Operator-authorized satellites (non-PBS)** | Our term; AWS has none. Any other satellite, including ones whose data is public. AWS documentation treats it as "your satellite". | Full onboarding: Non-disclosure agreements (NDAs), technical validation, per-site spectrum licensing, and the satellite operator's authorization. |

"Public data" does not mean "public downlink": a satellite whose products are freely published
is not PBS unless AWS lists it.

### The list

The AWS user guide lists **4** public broadcast satellites. Account `471112743408` also has
NOAA-21 onboarded, which makes **5** in practice.

| Satellite | North American Aerospace Defense Command (NORAD) | In the AWS doc list | Onboarded in account 471112743408 (ground stations) |
|---|---|---|---|
| Aqua | 27424 | yes | no |
| Terra | 25994 | yes | yes - Cape Town 1, Hawaii 1, Ireland 1, Ohio 1, Oregon 1, Stockholm 1 |
| Suomi National Polar-orbiting Partnership (NPP) | 37849 | yes | yes - Cape Town 1, Hawaii 1, Ohio 1, Oregon 1, Stockholm 1 |
| JPSS-1 / NOAA-20 | 43013 | yes | yes - Cape Town 1, Hawaii 1, Ohio 1, Oregon 1, Stockholm 1 |
| JPSS-2 / NOAA-21 | 54234 | not yet | yes - Cape Town 1, Ohio 1, Oregon 1, Singapore 1, Stockholm 1 |

Sources:
- [AWS - Onboard satellite: public broadcast satellites](https://docs.aws.amazon.com/ground-station/latest/ug/getting-started.step1.html#public-broadcast-satellites)
- `aws groundstation list-satellites --region eu-central-1` (run 2026-11-18). All ephemerides come from `SPACE_TRACK`.

Only the public downlink can be used on these satellites. Uplink is never allowed.

The account also has two **operator-authorized satellites (non-PBS)**, both approved for
Stockholm 1 only:

| Satellite | NORAD | Ground stations |
|---|---|---|
| Cloud-Aerosol Lidar and Infrared Pathfinder Satellite Observation (CALIPSO) | 29108 | Stockholm 1 |
| Jason-3 | 41240 | Stockholm 1 |

`[unverified]` Where these approvals came from. Check who requested them before relying on them.

### How to onboard a public broadcast satellite

1. In the AWS Ground Station console, open
   [Satellites and Resources](https://console.aws.amazon.com/groundstation/home#resources) and
   submit the Satellite Onboarding Questionnaire. Name the satellite and the ground stations you
   want. You can also mention whether you plan to use the digital twin.
2. AWS reviews the request. For any site that is not already approved, AWS handles the regulatory
   licensing. How long this takes depends on the location.
3. AWS notifies you when it is done. The satellite then appears in `list-satellites` and can be
   used straight away. AWS manages the ephemeris (from Space-Track).
4. Optional: name the satellite in the console with a `Name` tag. `tag-resource` needs the
   **regional** Amazon Resource Name (ARN), so insert the region into the global ARN that `list-satellites` returns:

   ```bash
   aws groundstation tag-resource --region eu-central-1 \
     --resource-arn arn:aws:groundstation:eu-central-1:471112743408:satellite/<satellite-id> \
     --tags '{"Name":"NOAA21"}'
   ```

Adding a ground station to a satellite that is already onboarded goes through the same process,
for example Bahrain 1 or Ireland 1 for NOAA-20.

To complete the PBS set, the account only needs to request **Aqua** (X-band 8160 MHz). It works
with the AWS `AquaSnppJpssTerraDigIF.yml` template.

AWS ground station locations (as of the doc page read on 2026-11-18; Seoul removed 2026-06-16):
Alaska 1, Bahrain 1, Cape Town 1, Dubbo 1, Hawaii 1, Ireland 1, Ohio 1, Oregon 1, Punta Arenas 1,
Singapore 1, Stockholm 1.
Source: [AWS Ground Station Locations](https://docs.aws.amazon.com/ground-station/latest/ug/aws-ground-station-antenna-locations.html).

### Operator-authorized satellites (non-PBS)

Outside the PBS list, onboarding is not self-service. AWS treats these as "your own satellites":
the questionnaire, NDAs, technical validation and per-site licensing all assume the satellite
operator is the one applying. Two limits rule out most other "public" weather satellites:

- **Frequency.** The shared antennas only receive S-band and X-band
  ([site capabilities](https://docs.aws.amazon.com/ground-station/latest/ug/locations.capabilities.html)).
  Satellites that broadcast in L-band (~1.7 GHz) are out of range, including the Advanced High Resolution Picture Transmission (AHRPT) of the Meteorological Operational (MetOp) satellites MetOp-B/C and
  the High Resolution Picture Transmission (HRPT) of NOAA-18/19. A Dedicated Antenna can go beyond those capabilities, but AWS builds those for
  each customer.
- **Authorization.** X-band missions such as Sentinel, Landsat or FengYun-3 are not open direct
  broadcast the way Aqua, Terra and JPSS are. The operator's authorization would be needed before
  AWS could license the downlink.

The realistic route to a new satellite is to ask AWS (questionnaire or AWS Support) whether it
plans to add it to the PBS list. NOAA-21 shows a satellite can be onboarded before the doc page is
updated. `[unverified]` Which other satellites, if any, AWS plans to add.

---

## Part 2 - Public European satellites accepting uplink (independent of AWS)

### CB (CiBi) does not work with satellites

CiBi, the French name for Citizens Band (CB) radio, is licence-free 27 MHz radio, and no satellite listens there. The satellites the public can
transmit to are **amateur radio satellites**. To use them you need:

- an amateur licence (in France: the French national frequency agency (ANFR) operator certificate and a callsign), and
- to stay inside amateur bands: 10 m, 2 m, 70 cm, 13 cm (and 5/3 cm for some).

Status below comes from the Radio Amateur Satellite Corporation (AMSAT) live lists:
[frequency modulation (FM)](https://www.amsat.org/live-fm-satellites/) (updated 2026-11-15),
[linear](https://www.amsat.org/live-linear-satellites/) and
[digipeater](https://www.amsat.org/live-digipeater-satellites/) (both updated 2026-10-31).
Check the [AMSAT status page](https://amsat.org/status/) before a pass. Country of origin is from
general knowledge, not from AMSAT.

### Voice (FM / SSB)

Amateur satellites are named by Orbiting Satellite Carrying Amateur Radio (OSCAR) numbers: a sponsor
prefix and a number. The prefixes below are AMSAT-OSCAR (AO), Spain-OSCAR (SO; Saudi-OSCAR for
SO-50), Jordan-OSCAR (JO), Italy-OSCAR (IO) and Qatar-OSCAR (QO). Modes:
single sideband (SSB), lower sideband (LSB), upper sideband (USB), continuous-wave Morse (CW) and digital
amateur television (DATV).

| Satellite | Origin | Uplink -> downlink (MHz) | Notes |
|---|---|---|---|
| QO-100 (Es'hail-2) | Qatari satellite, transponder by AMSAT-DL (Germany) | 2400.000-2400.500 -> 10489.500-10490.000 (SSB/CW); 2401.5-2409.5 -> 10491-10499 (DATV) | Geostationary at 25.9 E. Covers all of Europe 24/7 with no tracking. FM not allowed. Bandplan: [AMSAT-DL](https://amsat-dl.org/en/neuer-qo-100-bandplan/) |
| AO-73 (FUNcube-1) | United Kingdom (UK) | 435.130-435.150 -> 145.950-145.970 (LSB up / USB down) | Inverting linear transponder, active in eclipse |
| SO-132 (HADES-L) | Spain (AMSAT-EA) | 29.472 -> 436.665 (FM) | 10 m uplink, the band closest to CB |
| International Space Station (ISS) | International; Amateur Radio on the International Space Station (ARISS) antennas on the European Space Agency (ESA) Columbus module | 145.990 (67 Hz continuous tone-coded squelch system (CTCSS)) -> 437.800 FM; Automatic Packet Reporting System (APRS) 145.825 | Repeater only on when ARISS enables it |
| JO-97 (JY1Sat) | Jordan, FUNcube transponder from the UK | 435.100-435.120 -> 145.855-145.875 | Transponder works; telemetry is dead |

### Digital (AX.25 / APRS digipeaters)

| Satellite | Origin | Frequency (MHz) | Mode / callsign |
|---|---|---|---|
| SONATE-2 | Germany (Wuerzburg) | 145.825 | 1k2 audio frequency-shift keying (AFSK) APRS, DP0SNX - works with standard APRS gear |
| CroCube | Croatia | 436.775 | 9k6 Gaussian frequency-shift keying (GFSK) G3RUH, 9A0CC |
| KOSTKA | Czech Republic | 436.870 | 9k6 GFSK G3RUH, OK0KST |
| LASARsat | Czech Republic | 436.925 | 9k6 GFSK G3RUH, OK0LSR |
| Marina | Slovakia | 145.925 / 436.680 | 9k6 GFSK G3RUH, OM9MAR |
| GRBBeta | Slovakia / Hungary | 145.935 / 436.785 | 9k6 GFSK G3RUH, HA2GRB |
| Foresail-1p | Finland (Aalto University) | 437.125 | 9k6 Gaussian minimum-shift keying (GMSK), custom Skylink protocol |
| AO-129 (Erminaz-2U) | Italy | 435.900 | 9k6 GMSK, custom frame format |

### Not available any more, or not yet

- **IO-117 (GreenCube, Italy)** - listed by AMSAT as inoperative.
- **SO-121 (HADES-D, Spain)** - no longer on the live FM list.
- **HADES-SA (Spain)** - FM and Codec2 digital voice, still in testing. Do not transmit to it until
  AMSAT announces it open.

### European uplinks outside amateur radio

| System | Who can uplink | Notes |
|---|---|---|
| Galileo search and rescue (SAR), European Union (EU) | Anyone with a registered 406 MHz beacon (personal locator beacon (PLB), emergency position-indicating radio beacon (EPIRB), emergency locator transmitter (ELT)) | Licence-free once registered. Real emergencies only. |
| Argos (from the French space agency (CNES), flown on the European Organisation for the Exploitation of Meteorological Satellites (EUMETSAT) MetOp satellites) / Kineis (France) | Holders of a service contract with Collecte Localisation Satellites (CLS) or Kineis | Low-rate data from small transmitters. Paid, not open access. |
| Eutelsat OneWeb, Iridium and similar | Subscribers | Commercial only. |
| Infrastructure for Resilience, Interconnectivity and Security by Satellite (IRIS2), governmental satellite communications (GOVSATCOM) | Governments | Not public. |

### Recommendation for a club / CB-style use case in Europe

QO-100 fits best: one fixed dish and a 2.4 GHz uplink reach the whole continent at any time. It
still requires an amateur licence.

### Further resources

- [AMSAT satellite database](https://satdb.amsat.org)
- [Satellite Networked Open Ground Station (SatNOGS) network](https://network.satnogs.org) and [SatNOGS DB](https://db.satnogs.org)
- [F5KEE - active amateur satellites in 2026 (FR)](https://www.f5kee.fr/espace/satellites-radioamateurs-actifs-en-2026-frequences-modes-et-caracteristiques-techniques/)
- [ARISS](https://www.ariss.org)

Content from the sources above was rephrased for compliance with licensing restrictions.

---

## Part 3 - Non-AWS cost and onboarding delay of the uplink options

Compiled 2026-11-18. Prices are reseller or user-reported figures, excluding tax unless stated.
Several operators publish no prices at all; those are marked "quote". Delays that no source
states are marked `[unverified]`.

### Summary

| Option | One-off cost | Recurring cost | Onboarding delay | Who can use it |
|---|---|---|---|---|
| Argos (CLS) | Certified transmitter: quote | Joint Tariff Agreement (JTA) tariff: EUR 15/month + EUR 7.50/day, capped at EUR 105/month per device | Not published `[unverified]` | JTA: government / non-profit environmental users. Others: commercial quote |
| Kineis | KIM2 module or KIM2 Shield dev board: quote | Airtime: quote | Not published `[unverified]` | Business contract (Kineis, CLS or resellers) |
| Iridium Short Burst Data (SBD) | Modem (e.g. RockBLOCK 9603): store price, not checked | Prepaid: $17/month line rental + credits. Contract: $15.75-147/month | Days (buy, activate online, ship) | Anyone, through a reseller |
| Eutelsat OneWeb | Terminal: bundled or quote; $200 activation | $260-704/month fixed site; $325-3,500/month maritime; 12-month minimum | Weeks `[unverified]` | Enterprise, maritime, government via distributors |
| QO-100 | ~EUR 100-150 receive (RX) only; ~EUR 500-700 software-defined radio (SDR) transmit (TX) station; EUR 1,400-2,000+ transceiver station | None (no airtime) | ~1-3 months for the licence | Licensed radio amateurs |

### Argos (CLS / CNES)

- **Service tariff (Joint Tariff Agreement, JTA)**, as reported by
  [Sea Mammal Research Unit (SMRU) Instrumentation](https://smru-inst.wp.st-andrews.ac.uk/faqs/) (undated frequently asked questions (FAQ); check the
  current tariff with CLS):
  - EUR 15 for each calendar month in which the device transmits;
  - plus EUR 7.50 per day of use, counted in quarter-days (EUR 1.875 per 6-hour Coordinated Universal Time (UTC) slot with
    two or more uplinks received);
  - capped at 12 days per month under the reduced tariff, so **EUR 105/month per device at most**.
    Budget for the cap: a device that transmits whenever it can will reach it.
- The JTA only covers government and non-profit environmental use. Commercial users get a
  separate tariff on quote.
- **Hardware:** a CLS/CNES-certified Argos transmitter, a platform transmitter terminal (PTT). Prices are not published.
- **Onboarding steps:** contact the CLS Argos User Office -> sign the System Use Agreement
  (confirms JTA eligibility) -> purchase order -> request one PTT number per device. No source
  states how long this takes `[unverified]`.
- Classic Argos is mostly one-way (device to satellite) and data arrives about 2 hours after
  the satellite receives it, per the same FAQ.

### Kineis

- 25 low Earth orbit (LEO) nanosatellites at 650 km in 5 planes; two-way messaging and Global Positioning System (GPS)-free Doppler
  positioning ([Ground Control](https://www.groundcontrol.com/products/kineis/)).
- **Hardware:** KIM2 transceiver module, or the KIM2 Shield dev board (fits Arduino Uno /
  STM32 Nucleo, or Universal Serial Bus (USB) to a personal computer (PC)). Sold by CLS and resellers such as Ground Control. Prices are
  not published.
- **Airtime:** quote only, from Kineis, CLS or resellers (Ground Control, Netmore).
- **Delay:** not published `[unverified]`. A dev kit is a purchase; a custom production device
  needs Kineis certification, with no stated lead time `[unverified]`.

### Iridium

Figures from [Ground Control](https://www.groundcontrol.com/products/iridium/short-burst-data-range/short-burst-data-service-plans/)
(Tier 1 Iridium reseller, USD excluding tax):

- **Prepaid (RockBLOCK):** $17/month line rental, paid only for the months you use. Credits:
  1 per 50 bytes sent or received, and 1 per empty mailbox check. Credits expire only after
  12 months without use. No contract.
- **Contract SBD plans (12 months):**

  | Plan | Per month | Included | Overage | Activation |
  |---|---|---|---|---|
  | Low | $15.75 | 5,000 bytes | $3.15 / 1,000 bytes | $26.25 |
  | Mid | $36.75 | 25,000 bytes | $1.68 / 1,000 bytes | $31.50 |
  | High | $147.00 | 250,000 bytes | $0.79 / 1,000 bytes | $26.25 |

  The termination fee equals one month. Pooling costs $2.63/month.
- **Iridium Messaging Transport (IMT)**, up to 100 KB per message: about $17.50/month at 1 KB,
  $21 at 10-25 KB and $30 at 100 KB ([Ground Control](https://www.groundcontrol.com/blog/introducing-iridium-messaging-transport-imt/)).
- **Delay:** the modem ships from a web store and is activated online; prepaid needs no
  contract. In practice the wait is shipping time.

### Eutelsat OneWeb

- Not sold to individuals. You go through a distribution partner, and the service targets
  enterprise, maritime and government users.
- United States (US) reseller figures from [Global Satellite USA](https://globalsatellite.us/oneweb-plans/);
  European distributor prices may differ:

  | Service | Per month | Data |
  |---|---|---|
  | Hughes managed fixed site (HL1100W / HL1120W, standard antenna included) | $260 / $304 / $416 / $704 | 50 / 70 / 100 / 250 GB |
  | Maritime | $325 / $625 / $1,075 / $1,875 / $3,500 | 100 / 250 / 500 / 1,000 / 2,000 GB |

  Hughes plans: $200 activation, 12-month minimum term, 5% discount if prepaid annually.
- **Delay:** distributor account, site check and terminal delivery. No source states a lead
  time `[unverified]`.
- This is internet access through an operator's terminal, not a radio link you operate yourself.

### QO-100 (amateur radio)

**Licence (France): free, about 1-3 months**

Sources: [ANFR - Les certificats](https://www.anfr.fr/gerer/radioamateurs/les-certificats) and
[F5KEE - obtenir sa licence](https://www.f5kee.fr/comment-obtenir-sa-licence-radio-amateur/).

| Step | Cost | Delay |
|---|---|---|
| Prepare the exam (radio club, EXAM1 software, F6KGL videos) | Free | A few weeks |
| Book a session by phone at an ANFR regional centre (Villejuif, Lyon, Nancy, Marseille, Toulouse) | Free since Oct 2021 | Depends on slots `[unverified]` |
| Exam: 20 multiple-choice questions (MCQ) on regulation (15 min) + 20 on technique (30 min); 10/20 needed in each | Free | Same day. A passed part is kept for 1 year; retake no sooner than 2 months after a failure |
| Certificate (European Conference of Postal and Telecommunications Administrations (CEPT) Harmonised Amateur Radio Examination Certificate (HAREC) level) mailed home | Free | ~3 weeks |
| Request a callsign (F4xxx) through the ANFR online service | Free; the EUR 46 annual fee was removed in 2019 | Not stated `[unverified]` |

With a French licence you can operate for up to 3 months in most European countries under
CEPT TR 61-01.

**Station declaration to ANFR: required for any fixed QO-100 transmit station** (checked
2026-11-18). Article 5 of the
[arrete du 17 decembre 2007](https://www.legifrance.gouv.fr/loda/article_lc/LEGIARTI000044140859)
applies nationwide: a **fixed** amateur installation with an effective radiated power (ERP,
"PAR") above 5 W must be declared to ANFR **within two months of being installed**. You give
the World Geodetic System 1984 (WGS 84) coordinates and the maximum ERP per band (high frequency (HF), very high frequency (VHF), ultra high frequency (UHF), super high frequency (SHF)).

- It is a declaration, not an authorization: free, online at `teleservice-amateurs.anfr.fr`
  or with the ANFR paper form. Portable and mobile stations are not covered.
- The threshold is on radiated power, not amplifier output. An 80 cm dish has about 23 dBi at
  2.4 GHz, so 10 W at the feed gives roughly 1.8 kW effective isotropic radiated power (EIRP), about 1.1 kW ERP. Even 100 mW into
  that dish is above 5 W ERP, so in practice every fixed QO-100 uplink must be declared.
- ANFR also requires a declared fixed station before it will investigate an interference
  complaint ([ANFR Q&A](https://www.anfr.fr/gerer/radioamateurs/questions-/-reponses)).
- You are also responsible for meeting the public exposure limits of decret 2002-775 (same
  ANFR page). The dish beam is concentrated, so keep people out of it.
- The F5KEE statement that no extra registration is needed for home use is wrong for a fixed
  transmit station above 5 W ERP.

**Station hardware: one-off, no running cost**

| Tier | Typical content | Cost |
|---|---|---|
| Receive only | 60-80 cm dish, phase-locked loop (PLL) low-noise block downconverter (LNB), bias-tee, SDR dongle | ~EUR 100-150 (a portable build is reported at ~EUR 140: [OE2ROL](https://www.oe2rol.com/portable-qo-100-station.html)) |
| Entry transmit (SDR) | ADALM-Pluto + 10 W 2.4 GHz amplifier + cable pack, **EUR 349 incl. value-added tax (VAT), no antenna** ([passion-radio.fr](https://www.passion-radio.fr/satellite-qo-100/adalm-kit-100-948.html)), plus dish, dual-band feed, LNB, surface acoustic wave (SAW) filter (~EUR 28), bias-tee (~EUR 8.50) | ~EUR 500-700 total (estimate) |
| Transceiver station | Full-duplex rig (e.g. IC-9700) + upconverter + 80 cm dish ([hamradio.co.uk build](https://www.hamradio.co.uk/blog/how-to-build-a-complete-qo-100-satellite-station)) | EUR 1,430 for a documented build ([SV1UI](https://www.qsl.net/s/sv1ui/QO-100TRX/QO-100_SV1UI.pdf)); EUR 2,000+ with a commercial transceiver (estimate) |

Uplink power needed for SSB: about 2-5 W with a 1.2-1.8 m dish, about 10-12 W with a smaller
one ([Online Amateur Radio Community (OARC) wiki](https://wiki.oarc.uk/member-projects:gm5alx-qo100)). Using it costs nothing:
no airtime and no subscription.

Content from the sources above was rephrased for compliance with licensing restrictions.

---

## Conclusion - cheapest and quickest way to test an uplink

| Goal | Option | Cost | Delay |
|---|---|---|---|
| Cheapest uplink to a public satellite | Handheld FM radio + handheld Yagi on an amateur LEO satellite (ISS APRS 145.825 MHz, SO-50, AO-91) | ~EUR 50-200 hardware (estimate, not price-checked); licence free; no airtime | ~1-3 months (licence) |
| Quickest test, no licence | Iridium SBD modem (e.g. RockBLOCK) on prepaid | Modem (price not checked) + $17/month line rental + credits | Days (shipping) |
| Quickest test with your own radio | Have a licensed radio-club operator transmit while you observe | Free | Days to weeks |
| Permanent, 24/7 European coverage | QO-100 fixed station | ~EUR 500-700 SDR station; ANFR declaration (free) | Licence ~1-3 months |

Recommended path:

1. **Now:** receive only. Listen to SO-50, AO-91 or the ISS with an RTL-SDR or a handheld
   (licence-free), and use SatNOGS observations to learn pass timing and Doppler.
2. **In parallel:** prepare and book the ANFR exam (free). Contact a local radio club, which can
   also do a supervised first test with its licensed operators.
3. **Once licensed:** send an APRS packet through the ISS digipeater on 145.825 MHz from a
   handheld with an APRS app or terminal node controller (TNC). **Proof of uplink:** your packet shows up digipeated on
   aprs.fi / ariss.net. Check the ISS status first: the digipeater is not always on.
4. A handheld is a portable station, so no ANFR station declaration is needed. That is only
   required for fixed stations above 5 W ERP.

Argos and Kineis are not a quick test: both are quote-only, and Argos JTA terms are limited to
environmental, non-profit or government users. OneWeb is not a test platform: distributor,
12-month minimum, $260+/month.

---

## Can AWS Ground Station be used for the uplink test?

No: the recommended path uses no AWS Ground Station, and the radio frequency (RF) uplink itself cannot use it
(checked 2026-11-18):

| Constraint | Effect |
|---|---|
| Public broadcast satellites are downlink-only ([AWS](https://docs.aws.amazon.com/ground-station/latest/ug/getting-started.step1.html#public-broadcast-satellites)) | No uplink to Aqua, Terra, SNPP, NOAA-20 or NOAA-21 |
| AWS uplink is **S-band 2025-2110 MHz only**, EIRP 20-50 dBW; uplink echo is in the same range ([site capabilities](https://docs.aws.amazon.com/ground-station/latest/ug/locations.capabilities.html)) | Amateur uplinks (145 / 435 MHz) and QO-100 (2400 MHz) are out of range |
| AWS receive covers S-band and X-band (7750-8500 MHz for X-band) | AWS cannot even hear the amateur downlinks (145 / 435 MHz, QO-100 at 10489 MHz) to verify your uplink. Use SatNOGS or aprs.fi instead |
| AWS uplink requires a satellite onboarded by its operator, with spectrum licensing per site | Only possible with your own (or a partner's) S-band satellite |

What AWS Ground Station can still do:

- **Digital twin rehearsal of the uplink control plane.** The digital twin tests scheduling,
  config validation and error handling, with the same application programming interface (API) responses and EventBridge events,
  without production antenna time or spectrum licensing
  ([AWS](https://docs.aws.amazon.com/ground-station/latest/ug/digital-twin.html)). An
  `AntennaUplinkConfig` + `UplinkEchoConfig` mission profile can be exercised there. Limits:
  the digital twin delivers **no dataflows and no telemetry**, so no RF and no data path is
  tested. You request it through the Satellite Onboarding Questionnaire. In this account,
  `list-ground-stations` for NOAA-20 shows no "Digital Twin" stations, so it is not onboarded.
  `[unverified]` Digital twin pricing, and whether AWS accepts an uplink config against a
  downlink-only PBS satellite on the digital twin.
- **Digital twin: onboarding and availability** (checked 2026-11-18):
  - **Onboarding is required.** Request it through the Satellite Onboarding Questionnaire.
    Public broadcast satellites are accepted if you have no satellite of your own, but onboarding
    must be done **in a non-production AWS account**
    ([AWS Public Sector blog, 2025-03](https://aws.amazon.com/blogs/publicsector/test-and-integrate-ground-segment-with-aws-ground-station-digital-twin/)).
    For this project, that means a separate test account, not `471112743408`.
  - **Availability:** Generally available (GA) since August 2024, in every region where Ground Station is available,
    with a twin of every location
    ([What's New](https://aws.amazon.com/about-aws/whats-new/2024/08/digital-twin-available-aws-ground-station/)).
  - **This account:** `list-ground-stations` in 5 regions shows no "Digital Twin" station, and
    `list-contacts` on `Digital Twin Stockholm 1` returns 0 contacts with no error. So it is not
    onboarded.
  - **Behaviour:** contention with other contacts is emulated as in production, and EventBridge
    events and the contact lifecycle against the dataflow endpoints are exercised. No payload data
    and no telemetry are delivered.
  - `[unverified]` Exact price (AWS only says "significantly lower" than production contacts),
    quotas, onboarding lead time, and whether an `AntennaUplinkConfig` is accepted against a
    downlink-only PBS satellite on the twin. Ask these in the onboarding request.
- **Real uplink:** only as part of a satellite programme (own S-band satellite or a partner's
  hosted payload), with the full onboarding: NDA, technical validation and licensing.

Split the work: AWS Ground Station stays the downlink/processing side (NOAA-20 pipeline), and
the uplink test goes through amateur radio, verified with SatNOGS or aprs.fi.
