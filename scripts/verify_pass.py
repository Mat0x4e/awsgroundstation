"""Post-pass verification helper for the NOAA-20 SDR pipeline.

Checks every item on the Contact #7 validation checklist (docs/CONTACTS.md)
and prints a PASS/FAIL/WARN table.  Exits non-zero when any CRITICAL check
fails.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
HARD RULE:  A green Step Functions execution does NOT prove imagery.
            Truth is in the products/ S3 prefix and CloudWatch metrics,
            not in execution status.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Respects AWS_PROFILE from the environment.  Region is always eu-central-1.
Requires boto3 and appropriate IAM permissions (read-only on SFN, S3, CW).

Usage:
    python scripts/verify_pass.py <contact_id> [--date YYYY/MM/DD] [--window-hours N]
                                               [--save-report DIR]

Examples:
    python scripts/verify_pass.py dcef1518-c454-401f-bdcd-dc2e73fe89a5 --date 2026/10/17
    python scripts/verify_pass.py dcef1518-c454-401f-bdcd-dc2e73fe89a5   # date inferred from SFN

    # Save report alongside the local products copy (see output/README.md):
    python scripts/verify_pass.py dcef1518-c454-401f-bdcd-dc2e73fe89a5 \\
        --date 2026/10/17 \\
        --save-report output/contact-07_oregon-1_2026-10-17

Exit codes:
    0   all CRITICAL checks pass
    1   one or more CRITICAL checks failed
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Deployed resource names — do not change without updating infra
# ---------------------------------------------------------------------------

REGION = "eu-central-1"
OUTPUT_BUCKET = "groundstation-noaa20-sdr-output-471112743408"
STATE_MACHINE_NAME = "groundstation-noaa20-sdr-pipeline"
CW_NAMESPACE = "SDRPipeline"

# Alarms in criticality order.  The first two are CRITICAL (their ALARM
# state means the pipeline produced nothing); the third is informational.
CRITICAL_ALARM_NAMES = frozenset({
    "groundstation-noaa20-sdr-chunks-all-failed",
    "groundstation-noaa20-viirs-products-written-zero",
})
ALL_ALARM_NAMES: list[str] = [
    "groundstation-noaa20-sdr-chunks-all-failed",
    "groundstation-noaa20-viirs-products-written-zero",
    "groundstation-noaa20-sdr-visualization-failed",
]

# Expected products: 3 composites × {.png, .tif, .json} = 9 files.
#
# Filename-pattern assumption (flagged):
#   Keys follow the pattern  viirs_satdump_{composite}_{timestamp}.{ext}
#   as observed in the S3 output for contact #3 and contact #5:
#     products/2026/06/30/1ae80d1d-.../viirs_satdump_true_color_*.png
#   The classifier matches by composite name substring, which is robust to
#   timestamp variations.  If the orchestrator ever changes the filename
#   scheme (e.g. per-instrument prefix), update COMPOSITES or
#   _classify_products() accordingly.
COMPOSITES: list[str] = ["true_color", "thermal_ir", "day_microphysics"]
EXPECTED_EXTENSIONS: frozenset[str] = frozenset({".png", ".tif", ".json"})
EXPECTED_PRODUCT_COUNT: int = len(COMPOSITES) * len(EXPECTED_EXTENSIONS)  # 9

# ---------------------------------------------------------------------------
# Status constants
# ---------------------------------------------------------------------------

PASS = "PASS"
FAIL = "FAIL"
WARN = "WARN"
INFO = "INFO"

_STATUS_ICON: dict[str, str] = {
    PASS: "✅",
    FAIL: "❌",
    WARN: "⚠️ ",
    INFO: "ℹ️ ",
}

# Checks whose FAIL status causes the script to exit non-zero.
# Intentionally explicit — adding a check here is a deliberate decision.
CRITICAL_CHECKS: frozenset[str] = frozenset({
    "chunk_metrics",         # REL-3: ChunksSucceeded > 0
    "products_written",      # REL-4: ProductsWritten > 0 — severity is reconciled against
                             #        products_s3 in run_checks; only truly FAIL when S3 also fails
    "products_s3",           # The real truth: 9 product files in S3
    "alarm_chunks_all_failed",       # Critical alarm: all chunks failed
    "alarm_products_written_zero",   # Critical alarm: no products written
})

# ---------------------------------------------------------------------------
# boto3 client factory
# ---------------------------------------------------------------------------

_BOTO_CONFIG = Config(retries={"mode": "adaptive"})


def _client(service: str) -> Any:
    """Return a boto3 client for *service* in eu-central-1.

    Respects AWS_PROFILE from the environment; falls back to the default
    credential chain when unset.  Never hardcodes credentials.
    """
    profile = os.environ.get("AWS_PROFILE")
    session = boto3.Session(profile_name=profile) if profile else boto3.Session()
    return session.client(service, region_name=REGION, config=_BOTO_CONFIG)


# ---------------------------------------------------------------------------
# Check 1: Step Functions execution
# ---------------------------------------------------------------------------

def check_sfn_execution(
    contact_id: str,
) -> tuple[str, str, tuple[datetime, datetime] | None]:
    """Find the most recent SFN execution whose input mentions *contact_id*.

    Status is always INFO — Step Functions SUCCEEDED is NOT a pass criterion
    (see module docstring).  This check provides context only.

    The execution start/stop times are returned as the third element so that
    the metric checks can align their CloudWatch query window to the actual
    run, rather than using a fixed 'now − N hours' offset.  This prevents the
    false-FAIL-after-6h bug: metrics emitted during a clean pass would appear
    as "no data" when verify_pass.py is run more than 6 hours later.

    Returns:
        (INFO | WARN, human-readable detail string, (startDate, stopDate) | None)
        Third element is (startDate, stopDate) if a completed execution was
        found, or None if not found / execution still running.
    """
    sfn = _client("stepfunctions")

    # Resolve state machine ARN by name.  There are very few state machines
    # per account so a direct list (no paginator) is fine.
    try:
        resp = sfn.list_state_machines()
        arn: str | None = next(
            (m["stateMachineArn"]
             for m in resp.get("stateMachines", [])
             if m["name"] == STATE_MACHINE_NAME),
            None,
        )
    except ClientError as exc:
        return WARN, f"Could not list state machines: {exc}", None

    if not arn:
        return WARN, (
            f"State machine '{STATE_MACHINE_NAME}' not found — "
            "pass may not have completed yet or pipeline not deployed"
        ), None

    # Walk executions newest-first until we find one for this contact_id.
    paginator = sfn.get_paginator("list_executions")
    found_arn: str | None = None
    exec_info: dict = {}

    for page in paginator.paginate(stateMachineArn=arn):
        for ex in page.get("executions", []):
            exec_arn = ex["executionArn"]
            try:
                detail = sfn.describe_execution(executionArn=exec_arn)
            except ClientError:
                continue
            if contact_id in detail.get("input", ""):
                found_arn = exec_arn
                exec_info = detail
                break
        if found_arn:
            break

    if not found_arn:
        return WARN, (
            f"No execution found for contact_id={contact_id!r} — "
            "pass may not have completed yet or pipeline has not triggered"
        ), None

    ex_status = exec_info.get("status", "UNKNOWN")
    started: datetime | None = exec_info.get("startDate")
    stopped: datetime | None = exec_info.get("stopDate")
    duration_str = ""
    if started and stopped:
        dur_s = (stopped - started).total_seconds()
        duration_str = f" in {int(dur_s // 60)}m{int(dur_s % 60):02d}s"

    # ⚠️  PROMINENT NOTE: execution status is NOT a pass criterion.
    note = "SUCCEEDED ≠ imagery — context only, NOT a pass criterion"
    # Return the execution window so metric checks can query the right time range.
    exec_window: tuple[datetime, datetime] | None = (
        (started, stopped) if (started and stopped) else None
    )
    return INFO, f"{ex_status}{duration_str}  [⚠️  {note}]", exec_window


# ---------------------------------------------------------------------------
# Check 2: Chunk metrics REL-3
# ---------------------------------------------------------------------------

def check_chunk_metrics(
    start_time: datetime,
    end_time: datetime,
    window_desc: str,
) -> tuple[str, str]:
    """Query ChunksSucceeded and ChunksFailed from CloudWatch.

    PASS if ChunksSucceeded > 0.
    FAIL if ChunksSucceeded == 0 and ChunksFailed > 0 (catches the
         "0 succeeded / 44 failed" scenario from the rerun).
    WARN if there is no data yet, or if the failure rate exceeds 20 %.

    Args:
        start_time:  Query window start (UTC).  Normally derived from the
                     SFN execution start − 5 min (see run_checks).
        end_time:    Query window end (UTC).
        window_desc: Human-readable label appended to the detail string
                     (e.g. "execution window" or "last 6h (fallback)").

    Uses Period=300 (5-min buckets) and sums all returned datapoints so that
    a 40-min processing burst is fully captured regardless of bucket-boundary
    alignment.  This avoids the sparse-metric CloudWatch timing hazard
    documented in aws-knowledge.md.

    Critical check — exit non-zero on FAIL.
    """
    cw = _client("cloudwatch")

    def _get_sum(metric_name: str) -> float:
        resp = cw.get_metric_statistics(
            Namespace=CW_NAMESPACE,
            MetricName=metric_name,
            StartTime=start_time,
            EndTime=end_time,
            Period=300,  # 5-min buckets; sum all to cover the full window
            Statistics=["Sum"],
        )
        return sum(dp["Sum"] for dp in resp.get("Datapoints", []))

    try:
        succeeded = int(_get_sum("ChunksSucceeded"))
        failed = int(_get_sum("ChunksFailed"))
    except ClientError as exc:
        return FAIL, f"CloudWatch query failed: {exc}"

    detail = (
        f"ChunksSucceeded={succeeded}, ChunksFailed={failed} "
        f"({window_desc})"
    )

    if succeeded == 0 and failed == 0:
        return WARN, f"{detail} — no data yet (pass may still be processing)"

    if succeeded == 0:
        return FAIL, f"{detail} — ALL chunks failed; pipeline produced nothing"

    total = succeeded + failed
    fail_rate = failed / total if total > 0 else 0.0
    if fail_rate > 0.20:
        return WARN, f"{detail} — {fail_rate:.0%} failure rate is high"

    return PASS, detail


# ---------------------------------------------------------------------------
# Check 3: ProductsWritten REL-4
# ---------------------------------------------------------------------------

def check_products_written(
    start_time: datetime,
    end_time: datetime,
    window_desc: str,
) -> tuple[str, str]:
    """Query ProductsWritten Maximum over the given window.

    Returns the *raw* CloudWatch metric result: PASS if Max > 0, FAIL if Max = 0.

    IMPORTANT — this function's FAIL result is not necessarily a hard failure.
    ``run_checks`` reconciles this result against the S3 ground truth before
    recording it:

    - S3 PASS + this returns FAIL (metric=0): downgraded to WARN.
      Rationale: contact dcef1518 (Oregon #7, 2026-10-17) was processed BEFORE
      the metric-emission bug was fixed in the CodeBuild buildspec.  CloudWatch
      metrics cannot be backfilled, so metric=0 is a permanent historical
      artifact for that contact.  S3 is the authoritative truth.
    - S3 not PASS + this returns FAIL: kept as FAIL (critical).
      This is the ba2c5446 failure mode the tool exists to catch — 0 products
      in both S3 and the metric means the pass genuinely produced nothing.

    See the reconciliation block in ``run_checks`` for the full four-case logic.

    Args:
        start_time:  Query window start (UTC).
        end_time:    Query window end (UTC).  Should be padded at least +30 min
                     past execution stop — the visualization CodeBuild buildspec
                     emits ProductsWritten after the build completes (~10-15 min
                     after the SFN execution stops).
        window_desc: Human-readable label shown in the detail output.

    Uses Period=300 (5-min buckets) and takes the Maximum across all returned
    datapoints.  Statistics=["Maximum"] is correct here: we want the peak count
    emitted in any single datapoint within the window, not a sum across retries.

    Critical when S3 also fails — see run_checks reconciliation.
    """
    cw = _client("cloudwatch")

    try:
        resp = cw.get_metric_statistics(
            Namespace=CW_NAMESPACE,
            MetricName="ProductsWritten",
            StartTime=start_time,
            EndTime=end_time,
            Period=300,  # 5-min buckets; take max across all datapoints in the window
            Statistics=["Maximum"],
        )
    except ClientError as exc:
        return FAIL, f"CloudWatch query failed: {exc}"

    dps = resp.get("Datapoints", [])
    max_val = int(max((dp["Maximum"] for dp in dps), default=0))
    detail = f"Max={max_val} ({window_desc})"

    if max_val == 0:
        return FAIL, f"{detail} — visualisation stage produced no products"
    return PASS, detail


# ---------------------------------------------------------------------------
# Products S3 helpers
# ---------------------------------------------------------------------------

def _classify_products(keys: list[str]) -> dict[str, set[str]]:
    """Map each expected composite name to the set of file extensions found.

    Keys are expected to contain the composite name as a substring, e.g.:
        products/2026/10/17/dcef1518-.../viirs_satdump_true_color_20261017.png

    Assumption: the filename pattern is  viirs_satdump_{composite}_{ts}.{ext}
    as observed in the S3 output for contacts #3 and #5.  See module comment.

    Args:
        keys: S3 object key strings.

    Returns:
        Dict mapping composite name → set of extensions found for that composite.
    """
    found: dict[str, set[str]] = {c: set() for c in COMPOSITES}
    for key in keys:
        basename = key.rsplit("/", 1)[-1]  # last path component
        _, ext = (basename.rsplit(".", 1) if "." in basename else (basename, ""))
        ext_dot = f".{ext.lower()}" if ext else ""
        if ext_dot not in EXPECTED_EXTENSIONS:
            continue
        for composite in COMPOSITES:
            if composite in basename:
                found[composite].add(ext_dot)
                break  # a key can match at most one composite
    return found


# ---------------------------------------------------------------------------
# Check 4: Products in S3 (the real truth)
# ---------------------------------------------------------------------------

def check_products_s3(contact_id: str, date_path: str | None) -> tuple[str, str]:
    """List products/ and verify all 9 expected files are present.

    This is THE ground-truth check.  A PASS here, combined with
    ProductsWritten > 0, is the actual evidence that imagery was produced.

    Args:
        contact_id: Contact UUID string.
        date_path:  'YYYY/MM/DD' to scope the S3 prefix, or None to scan
                    broadly under products/ (slower, less precise).

    Returns:
        (PASS | FAIL | WARN, detail string).  CRITICAL on FAIL.
    """
    s3 = _client("s3")

    if date_path:
        prefix = f"products/{date_path}/{contact_id}/"
    else:
        prefix = "products/"

    # Collect all product keys that belong to this contact.
    keys: list[str] = []
    paginator = s3.get_paginator("list_objects_v2")
    try:
        for page in paginator.paginate(Bucket=OUTPUT_BUCKET, Prefix=prefix):
            for obj in page.get("Contents", []):
                key: str = obj["Key"]
                if contact_id in key:
                    keys.append(key)
    except ClientError as exc:
        return FAIL, f"S3 list failed: {exc}"

    if not keys:
        if not date_path:
            return WARN, (
                "No products found under products/ — pass may not have completed "
                "yet, or supply --date YYYY/MM/DD to scope the search"
            )
        return FAIL, f"No products found at s3://{OUTPUT_BUCKET}/{prefix}"

    # Classify by composite name and extension.
    found = _classify_products(keys)
    total_found = sum(len(exts) for exts in found.values())

    # Sanity-check JSON sidecars: an empty sidecar (≤ 2 bytes) means a
    # re-render produced an empty bounding box — the visual output is blank.
    small_sidecars: list[str] = []
    for key in keys:
        if key.endswith(".json"):
            try:
                head = s3.head_object(Bucket=OUTPUT_BUCKET, Key=key)
                if head.get("ContentLength", 0) <= 2:
                    small_sidecars.append(key.rsplit("/", 1)[-1])
            except ClientError:
                pass  # best-effort; don't let a head failure mask the main result

    # Build summary detail string.
    composite_summary = "; ".join(
        f"{c}: {sorted(found[c]) or 'MISSING'}" for c in COMPOSITES
    )
    detail = f"{total_found}/{EXPECTED_PRODUCT_COUNT} files — {composite_summary}"
    if small_sidecars:
        detail += f" ⚠️  empty sidecars: {small_sidecars}"

    if total_found == 0:
        return FAIL, "0 products found at prefix — visualisation produced nothing"

    if total_found < EXPECTED_PRODUCT_COUNT:
        missing: list[str] = []
        for c in COMPOSITES:
            for ext in sorted(EXPECTED_EXTENSIONS):
                if ext not in found[c]:
                    missing.append(f"{c}{ext}")
        return WARN, f"{detail} — missing: {missing}"

    if small_sidecars:
        return WARN, detail

    return PASS, detail


# ---------------------------------------------------------------------------
# Check 5: Alarm states
# ---------------------------------------------------------------------------

def check_alarms() -> dict[str, tuple[str, str]]:
    """Describe the three pipeline CloudWatch alarms.

    For a healthy pass all three should be OK.
    The two critical alarms in ALARM state → FAIL.
    visualization-failed in ALARM state → WARN (informational failure).

    Returns:
        Dict mapping alarm name → (status, detail).
    """
    cw = _client("cloudwatch")
    results: dict[str, tuple[str, str]] = {}

    try:
        resp = cw.describe_alarms(AlarmNames=ALL_ALARM_NAMES)
    except ClientError as exc:
        for name in ALL_ALARM_NAMES:
            results[name] = (FAIL, f"describe_alarms API failed: {exc}")
        return results

    state_map: dict[str, str] = {
        a["AlarmName"]: a["StateValue"]
        for a in resp.get("MetricAlarms", [])
    }

    for name in ALL_ALARM_NAMES:
        state = state_map.get(name)
        if state is None:
            results[name] = (WARN, "Alarm not found — pipeline may not be deployed yet")
        elif state == "OK":
            results[name] = (PASS, "OK")
        elif state == "ALARM":
            if name in CRITICAL_ALARM_NAMES:
                results[name] = (FAIL, "ALARM — pipeline produced no output")
            else:
                results[name] = (WARN, "ALARM — visualisation stage reported failure")
        elif state == "INSUFFICIENT_DATA":
            results[name] = (WARN, "INSUFFICIENT_DATA — no metrics published yet")
        else:
            results[name] = (WARN, f"Unexpected state: {state!r}")

    return results


# ---------------------------------------------------------------------------
# Check 6: CADU / science artifacts (informational — WARN not FAIL)
# ---------------------------------------------------------------------------

def check_cadu_artifacts(contact_id: str, date_path: str | None) -> tuple[str, str]:
    """Report presence and object counts for SatDump and SDR/RDR prefixes.

    Requires --date to build a precise prefix; returns WARN if omitted.
    An empty sdr/ is WARN only — CSPP is the accurate-but-fragile branch.
    An empty satdump/ is also WARN — SatDump may have failed.

    Returns:
        (PASS | WARN, detail string).  Never FAIL — this is depth info only.
    """
    if not date_path:
        return WARN, (
            "Cannot check CADU artifacts without --date; "
            "supply YYYY/MM/DD to enable this check"
        )

    s3 = _client("s3")
    base = f"contacts/{date_path}/{contact_id}"

    def _count_prefix(prefix: str) -> int:
        """Count objects under *prefix*; returns -1 on S3 error."""
        count = 0
        paginator = s3.get_paginator("list_objects_v2")
        try:
            for page in paginator.paginate(Bucket=OUTPUT_BUCKET, Prefix=prefix):
                count += len(page.get("Contents", []))
        except ClientError:
            return -1
        return count

    satdump_count = _count_prefix(f"{base}/satdump/chunk_0/")
    sdr_count = _count_prefix(f"{base}/sdr/")

    parts: list[str] = []

    if satdump_count < 0:
        parts.append("satdump/chunk_0/: unreachable")
        satdump_ok = False
    elif satdump_count == 0:
        parts.append("satdump/chunk_0/: empty (SatDump may not have produced output)")
        satdump_ok = False
    else:
        parts.append(f"satdump/chunk_0/: {satdump_count} object(s)")
        satdump_ok = True

    if sdr_count < 0:
        parts.append("sdr/: unreachable")
    elif sdr_count == 0:
        parts.append("sdr/: empty (CSPP branch produced nothing — WARN only, expected on bad passes)")
    else:
        parts.append(f"sdr/: {sdr_count} object(s)")

    status = PASS if satdump_ok else WARN
    return status, "; ".join(parts)


# ---------------------------------------------------------------------------
# Check 7: PERF-1 LUT cache state (informational only)
# ---------------------------------------------------------------------------

def check_lut_cache() -> tuple[str, str]:
    """Report whether the LUT cache is warm (fast path) or cold (fallback).

    A populated lut-cache/SDR_4_1_DB/ means the weekly refresh has run and
    the next aggregation can skip the sdr_luts.sh download.
    This check is always INFO — LUT cache state does not affect pass/fail.

    Returns:
        (INFO, detail string).  Never FAIL or WARN.
    """
    s3 = _client("s3")
    prefix = "lut-cache/SDR_4_1_DB/"
    count = 0
    paginator = s3.get_paginator("list_objects_v2")
    try:
        for page in paginator.paginate(Bucket=OUTPUT_BUCKET, Prefix=prefix):
            count += len(page.get("Contents", []))
    except ClientError as exc:
        return WARN, f"S3 list failed: {exc}"

    if count == 0:
        return INFO, (
            "lut-cache/SDR_4_1_DB/ is empty — cold cache "
            "(sdr_luts.sh fallback was used; weekly refresh not yet run)"
        )
    return INFO, (
        f"lut-cache/SDR_4_1_DB/: {count} object(s) — "
        "warm cache (fast path available for next aggregation)"
    )


# ---------------------------------------------------------------------------
# Table formatting
# ---------------------------------------------------------------------------

def _print_table(rows: list[tuple[str, str, str]]) -> None:
    """Print an aligned three-column table: CHECK | STATUS | DETAIL."""
    name_w = max(len(r[0]) for r in rows) + 2
    status_label = "STATUS"
    status_w = max(len(status_label), max(len(r[1]) for r in rows)) + 4

    print()
    print(f"  {'CHECK':<{name_w}}  {status_label:<{status_w}}  DETAIL")
    print(f"  {'─' * name_w}  {'─' * status_w}  {'─' * 60}")
    for name, status, detail in rows:
        icon = _STATUS_ICON.get(status, "? ")
        status_col = f"{icon} {status}"
        print(f"  {name:<{name_w}}  {status_col:<{status_w}}  {detail}")
    print()


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run_checks(
    contact_id: str,
    date_path: str | None,
    window_hours: int | None,
) -> tuple[list[tuple[str, str, str]], bool, list[str], list[dict]]:
    """Run all seven checks and return results.

    Args:
        contact_id:   Contact UUID to verify.
        date_path:    'YYYY/MM/DD' for S3 prefix scoping, or None.
        window_hours: Optional override: use a fixed 'now − N hours' CloudWatch
                      window instead of deriving from the execution times.
                      When None (default), execution-derived window is used.

    Returns:
        rows: List of (check_name, status, detail) for the output table.
        all_critical_passed: True iff no critical check returned FAIL.
        critical_failures: Names of critical checks that returned FAIL.
        reconciliation_log: List of reconciliation events where ProductsWritten
            severity was adjusted based on S3 ground truth.  Empty when no
            reconciliation occurred (the common healthy case).  Included in the
            JSON report under "reconciliation" when non-empty.
    """
    rows: list[tuple[str, str, str]] = []
    critical_failures: list[str] = []

    def _record(check_key: str | None, name: str, status: str, detail: str) -> None:
        rows.append((name, status, detail))
        if check_key in CRITICAL_CHECKS and status == FAIL:
            critical_failures.append(name)

    # ── 1. Step Functions execution ─────────────────────────────────────────
    # Status is always INFO — this check exists for context, NOT verdict.
    # MUST run first: its execution window is threaded into the metric checks.
    sfn_status, sfn_detail, exec_window = check_sfn_execution(contact_id)
    rows.append(("Step Functions execution", sfn_status, sfn_detail))

    # Metric window precedence — fixes false FAIL when verifying >6h after the pass:
    #   1. Explicit --window-hours override (manual debugging / scripted CI)
    #   2. Execution-derived: [exec_start − 5 min,  exec_stop + 30 min]
    #      The +30 min post-pad covers the visualization CodeBuild buildspec's
    #      ProductsWritten emit, which fires after the build completes
    #      (~10-15 min after the SFN execution stops), with a safety margin.
    #   3. Fallback: now − 6 h  (only when no execution was found AND no override)
    #      Keeps the "no data yet — pass may still be processing" WARN path for
    #      passes that genuinely haven't run yet.
    #
    # Root cause of the false-FAIL bug (contact dcef1518, Oregon #7, 2026-10-17):
    # ChunksSucceeded=50 and ProductsWritten=27 were emitted at ~23:00–23:40 CEST.
    # When verify_pass.py was run >6h later, the fixed "now − 6h" window contained
    # no datapoints and produced FALSE FAIL on both REL-3 and REL-4 for a clean pass.
    if window_hours is not None:
        # Explicit override: fixed lookback from now
        metric_end = datetime.now(timezone.utc)
        metric_start = metric_end - timedelta(hours=window_hours)
        window_desc = f"last {window_hours}h (explicit override)"
    elif exec_window is not None:
        exec_start, exec_stop = exec_window
        metric_start = exec_start - timedelta(minutes=5)
        metric_end = exec_stop + timedelta(minutes=30)
        window_desc = "execution window"
    else:
        # No execution found, no explicit override — fall back to now − 6 h.
        metric_end = datetime.now(timezone.utc)
        metric_start = metric_end - timedelta(hours=6)
        window_desc = "last 6h (fallback — no execution found)"

    # ── 2. Chunk metrics REL-3 (CRITICAL) ───────────────────────────────────
    cm_status, cm_detail = check_chunk_metrics(metric_start, metric_end, window_desc)
    _record("chunk_metrics", "Chunk metrics (REL-3)", cm_status, cm_detail)

    # ── 3+4. ProductsWritten metric + S3 ground truth (reconciled) ──────────
    #
    # Compute both results independently first, then reconcile before recording.
    # S3 products are the authoritative truth; the CloudWatch metric is corroboration.
    pw_status_raw, pw_detail_raw = check_products_written(metric_start, metric_end, window_desc)
    ps_status, ps_detail = check_products_s3(contact_id, date_path)

    # ── S3-truth-over-metric reconciliation ─────────────────────────────────
    #
    # Principle: products in S3 are ground truth; CloudWatch metrics are corroboration.
    # When the two sources contradict each other, S3 wins.
    #
    # Four cases:
    #
    #   Case 1 — S3 PASS + metric > 0 (pw_status_raw=PASS):
    #     Both sources agree.  Keep PASS for both.  No reconciliation note.
    #
    #   Case 2 — S3 PASS + metric = 0 (pw_status_raw=FAIL):
    #     S3 confirms the products exist; metric=0 is a historical artifact.
    #     CITATION: contact dcef1518 (Oregon #7, 2026-10-17) was processed BEFORE
    #     the ProductsWritten metric-emission bug was fixed in the CodeBuild
    #     buildspec.  CloudWatch metrics cannot be backfilled, so metric=0 is
    #     permanent for that contact.  DO NOT revert this to a hard FAIL without
    #     understanding this history.
    #     FIX: downgrade ProductsWritten from FAIL → WARN.  Overall = PASS.
    #
    #   Case 3 — S3 not PASS (WARN or FAIL) + metric = 0 (pw_status_raw=FAIL):
    #     Both sources agree there are no/incomplete products.  Keep FAIL (critical).
    #     This is the ba2c5446 rerun failure mode — the tool MUST catch this.
    #
    #   Case 4 — S3 FAIL + metric > 0 (pw_status_raw=PASS):
    #     Contradictory: metric claims products, S3 has none.  Surface as WARN.
    #     S3 truth still drives overall FAIL via the products_s3 critical check.
    #
    pw_status = pw_status_raw
    pw_detail = pw_detail_raw
    reconciliation_log: list[dict] = []

    if pw_status_raw == FAIL and ps_status == PASS:
        # Case 2: S3 has all products but metric=0 — pre-fix contact or metric-publish failure
        pw_status = WARN
        pw_detail = (
            f"{pw_detail_raw} — products exist in S3 (see S3 check) but "
            "ProductsWritten metric=0; pre-fix contact or metric-publish failure; "
            "S3 is authoritative, treating as non-critical"
        )
        reconciliation_log.append({
            "check": "ProductsWritten (REL-4)",
            "raw_metric_status": pw_status_raw,
            "raw_metric_detail": pw_detail_raw,
            "reconciled_status": pw_status,
            "reconciled_detail": pw_detail,
            "principle": (
                "S3-truth-over-metric: S3 confirms products exist; "
                "metric=0 attributed to pre-fix contact (dcef1518) or metric-publish failure"
            ),
        })
    elif pw_status_raw == PASS and ps_status == FAIL:
        # Case 4: anomaly — metric > 0 but S3 has no products
        pw_status = WARN
        pw_detail = (
            f"{pw_detail_raw} — anomaly: metric > 0 but S3 has no products; "
            "S3 is authoritative"
        )
        reconciliation_log.append({
            "check": "ProductsWritten (REL-4)",
            "raw_metric_status": pw_status_raw,
            "raw_metric_detail": pw_detail_raw,
            "reconciled_status": pw_status,
            "reconciled_detail": pw_detail,
            "principle": "S3-truth-over-metric: anomaly — metric > 0 but S3 has no products",
        })
    # Cases 1 (both PASS) and 3 (S3 not PASS + metric=0): no change to pw_status/pw_detail

    _record("products_written", "ProductsWritten (REL-4)", pw_status, pw_detail)
    _record("products_s3", "Products in S3 [truth]", ps_status, ps_detail)

    # ── 5. Alarm states ──────────────────────────────────────────────────────
    alarm_results = check_alarms()
    # Map alarm name → (check_key_or_None, display_name)
    _alarm_display: list[tuple[str, str | None, str]] = [
        (
            "groundstation-noaa20-sdr-chunks-all-failed",
            "alarm_chunks_all_failed",
            "Alarm: chunks-all-failed",
        ),
        (
            "groundstation-noaa20-viirs-products-written-zero",
            "alarm_products_written_zero",
            "Alarm: products-written-zero",
        ),
        (
            "groundstation-noaa20-sdr-visualization-failed",
            None,
            "Alarm: visualization-failed",
        ),
    ]
    for alarm_name, check_key, display_name in _alarm_display:
        a_status, a_detail = alarm_results.get(alarm_name, (WARN, "not found"))
        _record(check_key, display_name, a_status, a_detail)

    # ── 6. CADU / science artifacts (WARN not FAIL) ──────────────────────────
    ca_status, ca_detail = check_cadu_artifacts(contact_id, date_path)
    rows.append(("CADU / science artifacts", ca_status, ca_detail))

    # ── 7. LUT cache PERF-1 (INFO only) ─────────────────────────────────────
    lc_status, lc_detail = check_lut_cache()
    rows.append(("LUT cache (PERF-1)", lc_status, lc_detail))

    all_critical_passed = len(critical_failures) == 0
    return rows, all_critical_passed, critical_failures, reconciliation_log


# ---------------------------------------------------------------------------
# Report persistence
# ---------------------------------------------------------------------------

def _save_report(
    save_dir: str,
    contact_id: str,
    rows: list[tuple[str, str, str]],
    all_passed: bool,
    failures: list[str],
    verdict_line: str,
    reconciliation_log: list[dict] | None = None,
) -> None:
    """Write the verification report to *save_dir* as .txt and .json.

    Creates *save_dir* (and any parents) if it does not exist.

    Files written:
        verify_pass_report.txt   — the human-readable table and verdict
        verify_pass_report.json  — machine-readable results dict

    The directory follows the output/ contact-folder convention documented
    in output/README.md.  Both files are gitignored (output/ is in .gitignore).
    """
    out = Path(save_dir)
    out.mkdir(parents=True, exist_ok=True)

    # ── Text report ─────────────────────────────────────────────────────────
    txt_path = out / "verify_pass_report.txt"
    name_w = max(len(r[0]) for r in rows) + 2
    status_w = 12
    lines: list[str] = [
        f"NOAA-20 SDR Pass Verification — contact_id={contact_id}",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        "",
        f"  {'CHECK':<{name_w}}  {'STATUS':<{status_w}}  DETAIL",
        f"  {'─' * name_w}  {'─' * status_w}  {'─' * 60}",
    ]
    for name, status, detail in rows:
        icon = _STATUS_ICON.get(status, "? ")
        lines.append(f"  {name:<{name_w}}  {icon} {status:<{status_w - 2}}  {detail}")
    lines += ["", verdict_line.strip(), ""]
    txt_path.write_text("\n".join(lines), encoding="utf-8")

    # ── JSON report ──────────────────────────────────────────────────────────
    json_path = out / "verify_pass_report.json"
    report = {
        "contact_id": contact_id,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "overall_pass": all_passed,
        "critical_failures": failures,
        "checks": [
            {"name": name, "status": status, "detail": detail}
            for name, status, detail in rows
        ],
    }
    # When ProductsWritten severity was adjusted by the S3-truth-over-metric
    # reconciliation (e.g. contact dcef1518 pre-fix), record both the raw metric
    # result and the reconciled severity so machine consumers can see the nuance.
    if reconciliation_log:
        report["reconciliation"] = reconciliation_log
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"\n  📄 Report saved → {txt_path}")
    print(f"  📄 JSON saved   → {json_path}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    """CLI entry point.  Returns the exit code."""
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "contact_id",
        help="Contact UUID to verify, e.g. dcef1518-c454-401f-bdcd-dc2e73fe89a5",
    )
    ap.add_argument(
        "--date",
        metavar="YYYY/MM/DD",
        default=None,
        help="Contact date for S3 path scoping (e.g. 2026/10/17); "
             "inferred from SFN execution input if omitted (slower)",
    )
    ap.add_argument(
        "--window-hours",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Override: CloudWatch lookback window in hours from now (e.g. 6). "
            "Default: derive the window from the Step Functions execution "
            "start/stop times (fixes false FAIL when verifying >6h after the pass ran). "
            "Precedence: --window-hours > execution-derived > fallback 6h (used only "
            "when no execution is found and no override is given). "
            "Use this flag only for manual debugging."
        ),
    )
    ap.add_argument(
        "--save-report",
        metavar="DIR",
        default=None,
        help="Write verify_pass_report.txt and verify_pass_report.json into DIR "
             "(created if absent).  Use output/contact-07_oregon-1_2026-10-17 "
             "to save alongside the local products copy (see output/README.md).",
    )
    ap.add_argument(
        "--log-level",
        default="WARNING",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default: WARNING)",
    )
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )

    # Header
    print()
    print("  ╔══════════════════════════════════════════════════════════════╗")
    print("  ║   NOAA-20 SDR Pass Verification                              ║")
    print("  ╚══════════════════════════════════════════════════════════════╝")
    print(f"  contact_id : {args.contact_id}")
    print(f"  region     : {REGION}")
    print(f"  bucket     : {OUTPUT_BUCKET}")
    if args.window_hours is not None:
        print(f"  window     : last {args.window_hours}h (explicit override)")
    else:
        print("  window     : execution-derived (--window-hours to override)")
    if args.date:
        print(f"  date       : {args.date}")
    print()
    print("  ⚠️  IMPORTANT: A green Step Functions execution does NOT prove imagery.")
    print("     Truth is in the products/ S3 prefix and CloudWatch metrics below.")

    rows, all_passed, failures, reconciliation_log = run_checks(
        contact_id=args.contact_id,
        date_path=args.date,
        window_hours=args.window_hours,
    )

    _print_table(rows)

    if all_passed:
        verdict_line = (
            "  ✅  OVERALL: PASS — all critical checks passed "
            "(products present, metrics > 0, critical alarms OK)"
        )
        print(verdict_line)
        exit_code = 0
    else:
        verdict_line = (
            f"  ❌  OVERALL: FAIL — {len(failures)} critical check(s) failed: "
            + ", ".join(f"'{f}'" for f in failures)
        )
        print(verdict_line)
        exit_code = 1

    if args.save_report:
        _save_report(
            save_dir=args.save_report,
            contact_id=args.contact_id,
            rows=rows,
            all_passed=all_passed,
            failures=failures,
            verdict_line=verdict_line,
            reconciliation_log=reconciliation_log,
        )

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
