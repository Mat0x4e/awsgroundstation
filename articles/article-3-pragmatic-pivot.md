# Getting Labelled Earth Images from Space — Part 3: SatDump Native Projection, Engineering Lessons, and Costs

The series so far: [Part 1](./article-1-cloud-opensource.md) described a cloud pipeline that turns raw NOAA-20 radio signals into VIIRS imagery, with map overlays off by 100–300 km. [Part 2](./article-2-nasa-software.md) covered NASA's processing software: RT-STPS works in a container, but CSPP SDR's database initialization is incompatible with ephemeral build environments.

This final part covers the solution the project converged on, the main engineering lessons, and the total cost.

## Geolocation without CSPP

SatDump computes per-pixel geolocation internally as part of normal operation: it reads timestamps from the CADU frames and propagates the spacecraft ephemeris for every scan line. By default it does not export those coordinates. One configuration block changes that:

```json
"project": {
  "config": { "type": "equirec", "auto": true },
  "img_format": ".tif"
}
```

With this setting, SatDump outputs equirectangular-projected GeoTIFFs — WGS84 coordinates, affine transform, CRS metadata. Coastlines align with the imagery. No RT-STPS, no CSPP, no additional Level 1 processing step.

The trade-off is quantifiable. SatDump propagates community TLE orbit data, giving roughly 1–5 km accuracy; CSPP uses NOAA's corrected ephemeris plus terrain correction for sub-kilometre results. For science-grade radiometry the NASA chain remains necessary — on a persistent EC2 instance rather than a container. For a demonstrator whose goal is labelled imagery, 1–5 km is sufficient for coastlines to align visually, at the cost of one JSON stanza.

The general point: before adding a heavyweight component to address a limitation, it is worth checking whether a tool already in the pipeline provides the capability behind a configuration option.

## Five engineering lessons

**1. Fork the pipeline before the fragile parts.** Initially, SatDump's composites were produced per-chunk but only uploaded after RT-STPS ran — so when RT-STPS failed, finished imagery was lost with it. The revised design uploads every intermediate product to S3 as soon as it exists and makes downstream steps non-fatal.

**2. Pin behavior, not names.** A CodeBuild project referencing an ECR image without an explicit `:latest` tag cached the image indefinitely; fixes were being built and pushed but never deployed. Any reference that resolves lazily can resolve to stale content.

**3. Know which shell you are in.** CodeBuild inline buildspecs run under `/bin/sh`, not bash. Parentheses inside an `echo` message produced syntax errors that appeared as pipeline failures until traced to the shell.

**4. Automation that spends money needs its off switch in code.** The contact scheduler ran on an EventBridge cron rule, which was disabled manually in the console after the first passes. A subsequent `terraform apply` restored the rule to its declared state — enabled — and the scheduler booked two additional satellite passes, about $200 of antenna time, before this was noticed. Infrastructure-as-code reverts manual changes by design; anything capable of spending money must be disabled in the code itself, ideally with an approval step in front of it.

**5. Do not stretch swath imagery onto a bounding box.** Rendering a satellite swath with `imshow(extent=...)` onto geographic bounds always distorts it. The working approach is to render at native pixel resolution and project the map onto the image, rather than the image onto the map.

## Costs

For reference, the cost of real satellite data in 2026:

- **Antenna time:** ~$100–130 per 10-minute X-band contact; four contacts in total (two of them the result of lesson 4).
- **Storage:** ~190 GB of raw DigIF in S3, a few dollars per month.
- **Compute:** parallel CodeBuild containers, minutes per pass — single-digit dollars per run.

The full experiment came to roughly $500: raw RF from an operational weather satellite, received by rented antennas in Hawaii, Oregon and Stockholm, processed automatically into georeferenced imagery by open-source software. The hardware barrier that once defined this activity is gone; what remains is a Terraform repository and the engineering work described in these articles.

Producing the pixels was the smaller part of that work. Attaching accurate coordinates to them was the larger part — and knowing when to stop pursuing the heavyweight solution was part of the engineering.

*~660 words*
