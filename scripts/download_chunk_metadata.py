"""Download chunk metadata from S3 for the NOAA-20 SDR pipeline — Final Aggregation phase.

Lists all chunk subdirectories under the contact's S3 prefix and downloads
``dataset.json`` and any ``_failed.json`` markers to the local aggregation
directory, maintaining the ``chunks/{chunk_id}/`` structure expected by the
downstream ``generate_manifest.py`` and ``geolocation.py`` scripts.

Usage:
    python download_chunk_metadata.py <output_bucket> <contact_id> <local_output_dir>

Environment variables (optional):
    CONTACT_DATE  — ISO date string, e.g. "2026-06-19".  When set, the script
                    uses the precise S3 prefix
                    ``contacts/{contact_date}/{contact_id}/chunks/`` instead of
                    scanning the broader ``contacts/`` tree.

Exit codes:
    0  success (all reachable metadata downloaded)
    1  error   (bad arguments, S3 access failure, or no chunks found)
"""

import logging
import os
import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# Filenames to attempt downloading for every chunk subdirectory.
_METADATA_FILES = ("dataset.json", "_failed.json")


# ---------------------------------------------------------------------------
# Core download logic
# ---------------------------------------------------------------------------


def _list_chunk_ids(s3_client, bucket: str, prefix: str) -> list[str]:
    """Return sorted unique chunk IDs found under *prefix*.

    Iterates all object keys under the prefix and extracts the immediate
    subdirectory component that follows it (e.g. ``chunk_001``).  Only names
    that start with ``chunk_`` are considered chunk IDs; everything else is
    silently ignored.

    Args:
        s3_client: Boto3 S3 client.
        bucket:    S3 bucket name.
        prefix:    S3 key prefix ending with ``/``, e.g.
                   ``contacts/2026-06-19/abc123/chunks/``.

    Returns:
        Sorted list of unique chunk IDs, e.g. ``["chunk_001", "chunk_002"]``.
    """
    paginator = s3_client.get_paginator("list_objects_v2")
    chunk_ids: set[str] = set()

    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            # Strip the prefix and take the first path component.
            remainder = key[len(prefix):]
            if not remainder:
                continue
            component = remainder.split("/")[0]
            if component.startswith("chunk_"):
                chunk_ids.add(component)

    return sorted(chunk_ids)


def _download_file(s3_client, bucket: str, key: str, local_path: Path) -> bool:
    """Download a single S3 object to *local_path*.

    Creates parent directories as needed.  Returns ``True`` on success,
    ``False`` when the object does not exist (NoSuchKey / 404).  Re-raises
    all other ``ClientError`` exceptions so genuine access failures surface.

    Args:
        s3_client:  Boto3 S3 client.
        bucket:     S3 bucket name.
        key:        Full S3 object key.
        local_path: Destination path on the local file system.

    Returns:
        ``True`` if the file was downloaded, ``False`` if it was not found.

    Raises:
        ClientError: For S3 errors other than NoSuchKey / 404.
    """
    local_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        s3_client.download_file(bucket, key, str(local_path))
        logger.info("Downloaded s3://%s/%s → %s", bucket, key, local_path)
        return True
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "")
        if error_code in ("NoSuchKey", "404"):
            return False
        raise


def download_chunk_metadata(
    bucket: str,
    contact_id: str,
    local_output_dir: str,
    contact_date: str | None = None,
) -> int:
    """Download chunk metadata files from S3 into the local aggregation tree.

    For each chunk subdirectory discovered under the contact's S3 prefix,
    attempts to download ``dataset.json`` and ``_failed.json`` into::

        {local_output_dir}/chunks/{chunk_id}/

    Args:
        bucket:           S3 output bucket name.
        contact_id:       Contact identifier string.
        local_output_dir: Root of the local aggregation tree (created if absent).
        contact_date:     ISO date string used to build a precise prefix.
                          When ``None``, the broader ``contacts/`` prefix is
                          scanned and keys are filtered by ``contact_id``.

    Returns:
        Number of chunks processed (downloaded or skipped with a warning).

    Raises:
        SystemExit(1): If no chunks are found or an unrecoverable S3 error occurs.
    """
    s3 = boto3.client("s3")
    local_chunks_root = Path(local_output_dir) / "chunks"

    # Build the S3 prefix to list.
    if contact_date:
        prefix = f"contacts/{contact_date}/{contact_id}/chunks/"
        logger.info("Using precise prefix: s3://%s/%s", bucket, prefix)
    else:
        # Broader scan: list under contacts/ and filter by contact_id.
        # This is slower but works when CONTACT_DATE is unavailable.
        prefix = f"contacts/"
        logger.info(
            "CONTACT_DATE not set — scanning s3://%s/%s and filtering by contact_id=%r",
            bucket,
            prefix,
            contact_id,
        )
        # Re-map to a filtered helper that injects the contact_id check.
        return _download_with_filter(
            s3, bucket, prefix, contact_id, local_chunks_root
        )

    chunk_ids = _list_chunk_ids(s3, bucket, prefix)
    if not chunk_ids:
        logger.error(
            "No chunk subdirectories found under s3://%s/%s", bucket, prefix
        )
        sys.exit(1)

    logger.info("Found %d chunk(s): %s", len(chunk_ids), ", ".join(chunk_ids))
    return _process_chunks(s3, bucket, prefix, chunk_ids, local_chunks_root, contact_id)


def _download_with_filter(
    s3_client,
    bucket: str,
    broad_prefix: str,
    contact_id: str,
    local_chunks_root: Path,
) -> int:
    """Scan *broad_prefix*, locate the contact's chunks/ prefix, then download.

    This path is taken when ``CONTACT_DATE`` is not available.  It scans
    ``contacts/`` and looks for keys that contain ``/{contact_id}/chunks/``
    to derive the date-qualified prefix automatically.

    Args:
        s3_client:        Boto3 S3 client.
        bucket:           S3 bucket name.
        broad_prefix:     Top-level prefix to scan (``contacts/``).
        contact_id:       Contact ID to filter on.
        local_chunks_root: Local destination root for chunk directories.

    Returns:
        Number of chunks processed.
    """
    paginator = s3_client.get_paginator("list_objects_v2")
    chunks_prefix: str | None = None

    for page in paginator.paginate(Bucket=bucket, Prefix=broad_prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            marker = f"/{contact_id}/chunks/"
            if marker in key:
                # Derive the chunks prefix up to and including the marker.
                idx = key.index(marker)
                chunks_prefix = key[: idx + len(marker)]
                break
        if chunks_prefix:
            break

    if not chunks_prefix:
        logger.error(
            "Could not find chunks prefix for contact_id=%r under s3://%s/%s",
            contact_id,
            bucket,
            broad_prefix,
        )
        sys.exit(1)

    logger.info(
        "Resolved chunks prefix from broad scan: s3://%s/%s",
        bucket,
        chunks_prefix,
    )
    chunk_ids = _list_chunk_ids(s3_client, bucket, chunks_prefix)
    if not chunk_ids:
        logger.error(
            "No chunk subdirectories found under s3://%s/%s", bucket, chunks_prefix
        )
        sys.exit(1)

    logger.info("Found %d chunk(s): %s", len(chunk_ids), ", ".join(chunk_ids))
    return _process_chunks(s3_client, bucket, chunks_prefix, chunk_ids, local_chunks_root, contact_id)


def _process_chunks(
    s3_client,
    bucket: str,
    chunks_prefix: str,
    chunk_ids: list[str],
    local_chunks_root: Path,
    contact_id: str,
) -> int:
    """Download metadata files for each chunk ID and return the count processed.

    For every chunk, attempts ``dataset.json`` then ``_failed.json``.  Logs a
    warning if neither file is present (chunk has no metadata at all).

    Args:
        s3_client:        Boto3 S3 client.
        bucket:           S3 bucket name.
        chunks_prefix:    S3 prefix ending with ``chunks/``.
        chunk_ids:        Sorted list of chunk subdirectory names.
        local_chunks_root: Local root under which ``{chunk_id}/`` dirs are created.
        contact_id:       Contact identifier, used in the summary output.

    Returns:
        Number of chunks processed (= ``len(chunk_ids)``).
    """
    downloaded_count = 0
    skipped_count = 0
    warning_count = 0

    for chunk_id in chunk_ids:
        chunk_prefix = f"{chunks_prefix}{chunk_id}/"
        local_chunk_dir = local_chunks_root / chunk_id
        found_any = False

        for filename in _METADATA_FILES:
            s3_key = f"{chunk_prefix}{filename}"
            local_path = local_chunk_dir / filename
            try:
                found = _download_file(s3_client, bucket, s3_key, local_path)
            except ClientError as exc:
                logger.warning(
                    "S3 error downloading %s for chunk %s: %s — skipping file",
                    filename,
                    chunk_id,
                    exc,
                )
                skipped_count += 1
                continue

            if found:
                found_any = True
                downloaded_count += 1
            else:
                logger.debug(
                    "s3://%s/%s not found — skipping", bucket, s3_key
                )

        if not found_any:
            logger.warning(
                "Chunk %s: neither dataset.json nor _failed.json found in S3", chunk_id
            )
            warning_count += 1

    # Summary
    print(f"Chunk metadata download complete — contact_id={contact_id}")
    print(f"  Chunks processed       : {len(chunk_ids)}")
    print(f"  Files downloaded       : {downloaded_count}")
    print(f"  Files not found        : {skipped_count}")
    print(f"  Chunks with no metadata: {warning_count}")

    return len(chunk_ids)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """CLI wrapper: download_chunk_metadata.py <output_bucket> <contact_id> <local_output_dir>."""
    if len(sys.argv) != 4:
        print(
            f"Usage: {sys.argv[0]} <output_bucket> <contact_id> <local_output_dir>",
            file=sys.stderr,
        )
        sys.exit(1)

    output_bucket = sys.argv[1]
    contact_id = sys.argv[2]
    local_output_dir = sys.argv[3]

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    contact_date = os.environ.get("CONTACT_DATE") or None

    try:
        download_chunk_metadata(
            bucket=output_bucket,
            contact_id=contact_id,
            local_output_dir=local_output_dir,
            contact_date=contact_date,
        )
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected error: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
