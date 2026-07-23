# Archived CSPP/RT-STPS debugging buildspecs (2026-07-17 → 2026-07-22)

These are **superseded** exploratory CodeBuild buildspecs from the effort to get
CSPP SDR 4.1.1 to process RT-STPS 7.0 RDRs. They are kept only for history — do
**not** run them. The working buildspec is [`../../cspp_rdr_input.yml`](../../cspp_rdr_input.yml).

## What actually fixed CSPP (see DEPLOYMENT_STATUS.md → "CSPP SDR — RESOLVED")

Two changes, both required, never combined until 2026-07-23:

1. Install the **J01 shipped LUTs** (`CSPP_SDR_V4.1_static_luts_j01.tar.gz`) and run
   `sdr_luts.sh` → the DMS working database initializes. The old 600-iteration
   "wait for working db initialization" hang was the *missing-LUTs* symptom, not a
   Docker/CodeBuild limitation as previously concluded.
2. Feed `viirs_sdr.sh` the **RDR HDF5** (`RNSCA-RVIRS_*.h5`), **not** the `.PDS`.
   CSPP's ADL_Unpacker rejects PDS input (`'...PDS' should end with .h5?`).

## Why each of these failed

| File | Approach | Why it didn't work |
|------|----------|--------------------|
| `cspp_with_luts.yml`, `cspp_correct_env.yml`, `cspp_rt_home_sdr.yml`, `cspp_fix_path.yml`, `cspp_final_attempt.yml`, `cspp_spacecraft_flag.yml` | CSPP env/path/flag permutations on RDR input | Pre-dated / mis-sequenced the LUT+DB setup; also `--spacecraft` flag doesn't exist in 4.1.1 |
| `cspp_pds_input.yml` | Feed CSPP the raw `.PDS` files | ADL_Unpacker only accepts `.h5` RDRs |
| `fix_rdr_run_cspp.yml`, `inspect_rdr*.yml`, `read_rdr_attrs.yml`, `check_luts_location.yml` | RDR metadata inspection / patching | Diagnostics; root cause was LUTs + input type, not RDR metadata |
| `rtstps_server_mode.yml`, `rtstps_xvfb_server.yml` | RT-STPS server mode under Xvfb | JSW wrapper won't start headless in containers (and unnecessary once above fixes found) |
| `satpy_rdr.yml`, `satpy_rdr_v2.yml` | Bypass CSPP with Satpy readers | Satpy has no reader for raw RNSCA-RVIRS RDRs |
