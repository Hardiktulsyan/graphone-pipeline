# Standard library imports for dates, RFC 2822 parsing, and regular expressions.
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import re

# Project logger helper.
from src.utils.logger import get_logger

# Create a module-level logger.
logger = get_logger(__name__)

# Pattern matching relative strings like "2 hours ago" or "3 days ago".
RELATIVE_PATTERN = re.compile(
    r"^(\d+|a|an)\s+(second|seconds|minute|minutes|hour|hours|day|days|week|weeks)\s+ago$"
)


# Convert a datetime to a timezone-aware UTC datetime.
def _ensure_utc(dt: datetime) -> datetime:
    # Attach UTC if the datetime has no timezone information.
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)

    # Convert to UTC if it has another timezone offset.
    return dt.astimezone(timezone.utc)


# Try to parse a Unix timestamp in seconds or milliseconds.
def _parse_unix_timestamp(value):
    # Reject booleans because bool is a subclass of int in Python.
    if isinstance(value, bool):
        return None

    # Convert numbers or numeric strings into a float.
    if isinstance(value, (int, float)):
        numeric_value = float(value)
    elif isinstance(value, str):
        cleaned = value.strip()
        if cleaned == "":
            return None
        try:
            numeric_value = float(cleaned)
        except ValueError:
            return None
    else:
        return None

    # Ignore very small numbers so values like years ("2026") are not treated as 1970 timestamps.
    if numeric_value < 100_000_000:
        return None

    # Treat values at or above 1e12 as milliseconds and convert them to seconds.
    if numeric_value >= 1_000_000_000_000:
        seconds = numeric_value / 1000.0
    else:
        seconds = numeric_value

    # Convert seconds into a timezone-aware UTC datetime.
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (ValueError, OSError, OverflowError):
        return None


# Try to parse relative date strings such as "Just now", "yesterday", or "2 hours ago".
def _parse_relative_date(cleaned_text: str, now_utc: datetime):
    # Normalize the text to lowercase and single spaces.
    lower_text = " ".join(cleaned_text.lower().split())

    # Handle "just now".
    if lower_text == "just now":
        return now_utc

    # Handle "yesterday".
    if lower_text == "yesterday":
        return now_utc - timedelta(days=1)

    # Handle "today".
    if lower_text == "today":
        return now_utc

    # Check against the "<amount> <unit> ago" pattern.
    match = RELATIVE_PATTERN.match(lower_text)
    if match is None:
        return None

    # Read the number part, treating "a" or "an" as 1.
    amount_text = match.group(1)
    if amount_text == "a" or amount_text == "an":
        amount = 1
    else:
        amount = int(amount_text)

    # Subtract the matching time unit from now_utc.
    unit = match.group(2)
    if unit in ("second", "seconds"):
        return now_utc - timedelta(seconds=amount)
    if unit in ("minute", "minutes"):
        return now_utc - timedelta(minutes=amount)
    if unit in ("hour", "hours"):
        return now_utc - timedelta(hours=amount)
    if unit in ("day", "days"):
        return now_utc - timedelta(days=amount)
    if unit in ("week", "weeks"):
        return now_utc - timedelta(weeks=amount)

    # Return None if the unit was not recognized.
    return None


# Try to parse an ISO-8601 date or datetime string.
def _parse_iso_date(cleaned_text: str):
    # Use Python 3.11's built-in ISO parser.
    try:
        parsed = datetime.fromisoformat(cleaned_text)
    except ValueError:
        return None

    # Ensure the result is timezone-aware UTC.
    return _ensure_utc(parsed)


# Try to parse an RFC 2822 date string (used in RSS pubDate fields).
def _parse_rfc2822_date(cleaned_text: str):
    # Use the standard library email date parser.
    try:
        parsed = parsedate_to_datetime(cleaned_text)
    except (TypeError, ValueError):
        return None

    # Return None if parsing produced no datetime.
    if parsed is None:
        return None

    # Ensure the result is timezone-aware UTC.
    return _ensure_utc(parsed)


# Parse a date value into a timezone-aware UTC datetime, or return None if unparseable.
def parse_date(text, now=None):
    # Return None immediately when input is missing.
    if text is None:
        return None

    # Prepare the reference "now" timestamp in UTC.
    if now is None:
        now_utc = datetime.now(timezone.utc)
    else:
        now_utc = _ensure_utc(now)

    # Step 1: Try parsing Unix timestamps (supports int, float, or numeric string).
    unix_result = _parse_unix_timestamp(text)
    if unix_result is not None:
        return unix_result

    # Stop if the remaining input is not a string.
    if not isinstance(text, str):
        return None

    # Clean outer whitespace and return None if blank.
    cleaned = text.strip()
    if cleaned == "":
        return None

    # Step 2: Try parsing relative phrases like "Just now", "yesterday", or "2 hours ago".
    relative_result = _parse_relative_date(cleaned, now_utc)
    if relative_result is not None:
        return relative_result

    # Step 3: Try parsing ISO-8601 date and datetime strings.
    iso_result = _parse_iso_date(cleaned)
    if iso_result is not None:
        return iso_result

    # Step 4: Try parsing RFC 2822 strings (RSS pubDate).
    rfc_result = _parse_rfc2822_date(cleaned)
    if rfc_result is not None:
        return rfc_result

    # Return None when none of the formats matched.
    return None


# Return True if dt is between 0 and 24 hours before now, otherwise False.
def is_within_24_hours(dt, now=None) -> bool:
    # Return False if dt is missing.
    if dt is None:
        return False

    # Prepare both timestamps in UTC.
    dt_utc = _ensure_utc(dt)
    if now is None:
        now_utc = datetime.now(timezone.utc)
    else:
        now_utc = _ensure_utc(now)

    # Compute the elapsed time from dt to now.
    difference = now_utc - dt_utc

    # Return True only if dt is not in the future and is at most 24 hours old.
    if difference < timedelta(0):
        return False
    if difference > timedelta(hours=24):
        return False
    return True


# Run 15 test inputs when executing this module directly.
if __name__ == "__main__":
    # Fixed UTC reference time so test outputs are easy to verify.
    fixed_now = datetime(2026, 11, 22, 19, 30, 0, tzinfo=timezone.utc)

    # Define 15 test cases covering all supported formats and invalid inputs.
    test_inputs = [
        "2026-11-22T18:30:00Z",
        "2026-11-22T23:00:00+05:30",
        "2026-11-22 10:15:00",
        "2026-11-22",
        "Sun, 22 Nov 2026 15:00:00 GMT",
        "Sun, 22 Nov 2026 10:00:00 -0500",
        "Just now",
        "2 hours ago",
        "45 minutes ago",
        "yesterday",
        "3 days ago",
        1795375800,
        "1795375800000",
        "",
        "not a real date",
    ]

    # Parse each test input and log the input, parsed UTC datetime, and 24-hour check.
    for item in test_inputs:
        parsed_dt = parse_date(item, fixed_now)
        within_24h = is_within_24_hours(parsed_dt, fixed_now)
        logger.info(
            "input=%r | parsed=%s | within_24h=%s",
            item,
            parsed_dt,
            within_24h,
        )