# Standard library imports for timestamps, hashing, paths, SQLite, and URL parsing.
from datetime import datetime, timezone
import hashlib
import os
import sqlite3
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Project logger helper.
from src.utils.logger import get_logger

# Create a module-level logger.
logger = get_logger(__name__)

# Path to the local SQLite database file.
DEFAULT_DB_PATH = os.path.join("output", "seen.db")

# Tracking query parameter names removed during URL normalization (in addition to any "utm_*" key).
TRACKING_PARAMS = {
    "fbclid",
    "gclid",
    "dclid",
    "msclkid",
    "mc_cid",
    "mc_eid",
    "ref",
    "ref_src",
    "ref_url",
    "source",
    "igshid",
    "yclid",
    "_ga",
    "_gl",
    "spm",
}

# Scaling note for multi-node crawlers:
# SQLite works well for a single machine, but multiple crawler nodes writing at once
# will contend on the local file lock and cannot share a local disk database cleanly.
#
# Option 1 - Redis SET with NX:
# Replace is_seen() and mark_seen() with a single atomic Redis command:
#   SET seen:<url_hash> <utc_timestamp> NX
# Redis returns True only for the first node that sets the key and None if another node
# already claimed it. You can also add EX <seconds> if you want old URLs to expire.
#
# Option 2 - PostgreSQL unique constraint:
# Create the same table in Postgres with url_hash TEXT PRIMARY KEY (or a UNIQUE constraint)
# and run:
#   INSERT INTO seen (url_hash, first_seen_at) VALUES (%s, %s)
#   ON CONFLICT (url_hash) DO NOTHING
# Postgres enforces uniqueness atomically across all crawler nodes without race conditions.


# Ensure the database folder and seen table exist, then return an open SQLite connection.
def _get_connection(db_path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    # Create the parent folder if it does not exist yet.
    parent_dir = os.path.dirname(db_path)
    if parent_dir != "":
        os.makedirs(parent_dir, exist_ok=True)

    # Open the SQLite database file.
    conn = sqlite3.connect(db_path)

    # Create the seen table if it does not already exist.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS seen ("
        "url_hash TEXT PRIMARY KEY, "
        "first_seen_at TEXT"
        ")"
    )
    conn.commit()
    return conn


# Normalize a URL by lowercasing the host, removing tracking params, fragment, and trailing slash.
def normalize_url(url) -> str:
    # Return an empty string if the input is missing or blank.
    if not isinstance(url, str) or url.strip() == "":
        return ""

    # Split the URL into scheme, host (netloc), path, query, and fragment.
    cleaned = url.strip()
    parts = urlsplit(cleaned)

    # Lowercase the scheme and host.
    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()

    # Remove any trailing slash from the path.
    path = parts.path.rstrip("/")

    # Parse query parameters and drop any "utm_" or known tracking parameters.
    raw_params = parse_qsl(parts.query, keep_blank_values=True)
    kept_params = []
    for key, value in raw_params:
        lower_key = key.lower()
        if lower_key.startswith("utm_"):
            continue
        if lower_key in TRACKING_PARAMS:
            continue
        kept_params.append((key, value))

    # Sort kept query parameters so parameter order does not affect matching.
    kept_params.sort()
    clean_query = urlencode(kept_params, doseq=True)

    # Rebuild the URL with an empty fragment.
    return urlunsplit((scheme, netloc, path, clean_query, ""))


# Hash a normalized URL using SHA-256, or return None if the URL is empty.
def _hash_url(url):
    # Normalize the URL before hashing.
    normalized = normalize_url(url)
    if normalized == "":
        return None

    # Return the hexadecimal SHA-256 digest.
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


# Return True if the hash of the normalized URL is already in the seen table.
def is_seen(url, db_path: str = DEFAULT_DB_PATH) -> bool:
    # Compute the normalized URL hash.
    url_hash = _hash_url(url)
    if url_hash is None:
        return False

    # Look up the hash in SQLite and close the connection cleanly.
    conn = _get_connection(db_path)
    try:
        cursor = conn.execute(
            "SELECT 1 FROM seen WHERE url_hash = ? LIMIT 1",
            (url_hash,),
        )
        row = cursor.fetchone()
        return row is not None
    finally:
        conn.close()


# Insert the normalized URL hash and current UTC time, doing nothing if it already exists.
def mark_seen(url, db_path: str = DEFAULT_DB_PATH):
    # Compute the normalized URL hash.
    url_hash = _hash_url(url)
    if url_hash is None:
        return

    # Record the current UTC timestamp in ISO-8601 format.
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Insert into SQLite using INSERT OR IGNORE so existing rows are left untouched.
    conn = _get_connection(db_path)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO seen (url_hash, first_seen_at) VALUES (?, ?)",
            (url_hash, now_utc),
        )
        conn.commit()
    finally:
        conn.close()


# Run basic tests verifying new URL, mark_seen, and utm_ parameter matching.
if __name__ == "__main__":
    # Define a sample URL and a variant with utm_ tracking parameters and trailing slash.
    base_url = "https://Example.com/articles/ai-pipeline/"
    utm_url = (
        "https://example.com/articles/ai-pipeline"
        "?utm_source=newsletter&utm_medium=email#comments"
    )

    # Clear the test URL hash first so repeated test runs always start clean.
    test_hash = _hash_url(base_url)
    setup_conn = _get_connection(DEFAULT_DB_PATH)
    try:
        setup_conn.execute("DELETE FROM seen WHERE url_hash = ?", (test_hash,))
        setup_conn.commit()
    finally:
        setup_conn.close()

    # Test 1: A new URL should not be seen yet.
    seen_before = is_seen(base_url)
    logger.info("Test 1 | new URL is_seen=%s (expected False)", seen_before)

    # Test 2: After mark_seen, the URL should be seen.
    mark_seen(base_url)
    seen_after = is_seen(base_url)
    logger.info("Test 2 | after mark_seen is_seen=%s (expected True)", seen_after)

    # Test 3: The same URL with utm_ parameters and a fragment should count as seen.
    seen_utm = is_seen(utm_url)
    logger.info("Test 3 | same URL with utm_ is_seen=%s (expected True)", seen_utm)