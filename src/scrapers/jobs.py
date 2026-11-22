# Standard library imports for async tasks, timestamps, JSON, paths, regex, and XML parsing.
import asyncio
from datetime import datetime, timezone
import json
import os
import re
import xml.etree.ElementTree as ET

# Third-party async HTTP library.
import aiohttp

# Project imports for entity resolution, date parsing, logging, and HTTP retries.
from src.resolver.entity_resolver import EntityResolver
from src.utils.dates import is_within_24_hours, parse_date
from src.utils.logger import get_logger
from src.utils.retry import fetch_with_retry

# Create a module-level logger.
logger = get_logger(__name__)

# Editable job source URLs and board lists.
REMOTEOK_URL = "https://remoteok.com/api"
REMOTIVE_URL = "https://remotive.com/api/remote-jobs?search=AI"
WEWORKREMOTELY_URL = "https://weworkremotely.com/categories/remote-programming-jobs.rss"

# Greenhouse board names to scrape.
GREENHOUSE_BOARDS = [
    "anthropic",
    "databricks",
    "scaleai",
    "togetherai",
    "glean",
]
GREENHOUSE_URL_TEMPLATE = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs"

# Lever board names to scrape.
LEVER_BOARDS = [
    "mistral",
    "palantir",
]
LEVER_URL_TEMPLATE = "https://api.lever.co/v0/postings/{board}?mode=json"

# Output file paths used when running this script directly.
OUTPUT_DIR = "output"
JOBS_OUTPUT_PATH = os.path.join(OUTPUT_DIR, "jobs.json")
ENTITY_LOG_OUTPUT_PATH = os.path.join(OUTPUT_DIR, "entity_log_jobs.json")

# Patterns used to verify that a raw date includes a time of day.
TIME_OF_DAY_PATTERN = re.compile(r"\d{1,2}:\d{2}")
DATE_ONLY_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SUB_DAY_RELATIVE_PATTERN = re.compile(
    r"^(\d+|a|an)\s+(second|seconds|minute|minutes|hour|hours)\s+ago$"
)
ML_WORD_PATTERN = re.compile(r"\bml\b")


# Parse a fetch_with_retry result into Python data whether it is already JSON or a text string.
def parse_json_response(raw_response):
    # Return None if nothing was returned.
    if raw_response is None:
        return None

    # Parse the text as JSON if fetch_with_retry returned a string.
    if isinstance(raw_response, str):
        try:
            return json.loads(raw_response)
        except json.JSONDecodeError as error:
            logger.error("Failed to parse JSON text: %s", error)
            return None

    # Return already-parsed JSON objects directly.
    return raw_response


# Clean a string field and return None if missing or blank.
def clean_string(value):
    # Return None if the value is not a string.
    if not isinstance(value, str):
        return None

    # Strip outer whitespace and return None if empty.
    cleaned = value.strip()
    if cleaned == "":
        return None
    return cleaned


# Check whether a raw date value includes a time of day.
def has_time_of_day(raw_date) -> bool:
    # Reject missing values and booleans.
    if raw_date is None or isinstance(raw_date, bool):
        return False

    # Accept numeric Unix timestamps in seconds or milliseconds.
    if isinstance(raw_date, (int, float)):
        return raw_date >= 100_000_000

    # Reject non-string values after numeric checks.
    if not isinstance(raw_date, str):
        return False

    # Normalize spaces and case for inspection.
    cleaned = " ".join(raw_date.strip().lower().split())
    if cleaned == "":
        return False

    # Reject date-only words and YYYY-MM-DD strings.
    if cleaned == "yesterday" or cleaned == "today":
        return False
    if DATE_ONLY_PATTERN.match(cleaned) is not None:
        return False

    # Accept strings containing a clock time like HH:MM.
    if TIME_OF_DAY_PATTERN.search(cleaned) is not None:
        return True

    # Accept "just now" and sub-day relative phrases.
    if cleaned == "just now":
        return True
    if SUB_DAY_RELATIVE_PATTERN.match(cleaned) is not None:
        return True

    # Accept numeric Unix timestamp strings.
    if cleaned.isdigit() and len(cleaned) >= 9:
        return True

    # Reject all other date-only or ambiguous strings.
    return False


# Classify a job into a role family using keyword rules on the job title only.
def classify_role_family(title) -> str:
    # Return "Other" if the title is missing.
    if not isinstance(title, str) or title.strip() == "":
        return "Other"

    # Lowercase the title for case-insensitive keyword checks.
    lower_title = title.lower()

    # Check Engineering keywords.
    for keyword in ("engineer", "developer", "devops", "software"):
        if keyword in lower_title:
            return "Engineering"

    # Check Research keywords.
    for keyword in ("research", "scientist"):
        if keyword in lower_title:
            return "Research"

    # Check Data keywords (use word boundary for "ml").
    for keyword in ("data", "analyst", "machine learning"):
        if keyword in lower_title:
            return "Data"
    if ML_WORD_PATTERN.search(lower_title) is not None:
        return "Data"

    # Check Product keywords.
    if "product" in lower_title:
        return "Product"

    # Check Design keywords.
    if "design" in lower_title:
        return "Design"

    # Check Sales keywords.
    for keyword in ("sales", "account"):
        if keyword in lower_title:
            return "Sales"

    # Check Marketing keywords.
    if "marketing" in lower_title:
        return "Marketing"

    # Check Operations keywords.
    for keyword in ("operations", "recruiter", "people", "finance"):
        if keyword in lower_title:
            return "Operations"

    # Default when no rule matched.
    return "Other"


# Build one JOB record matching the required pipeline schema.
def build_job_record(
    source_name: str,
    source_url,
    title,
    company,
    job_dt: datetime,
    is_remote: bool,
    collected_at: str,
) -> dict:
    # Return the structured JOB record dictionary.
    return {
        "schemaVersion": "1.0",
        "recordType": "JOB",
        "source": {
            "name": source_name,
            "url": clean_string(source_url),
        },
        "content": {
            "title": clean_string(title),
            "company": clean_string(company),
            "date": job_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "is_remote": is_remote,
            "role_family": classify_role_family(title),
        },
        "collectedAt": collected_at,
    }


# Build an empty result summary dictionary for a source.
def make_source_result(
    source_name: str,
    jobs_found: int,
    fresh: int,
    saved: int,
    skipped: int,
    records: list,
) -> dict:
    # Return the counts and records together.
    return {
        "source_name": source_name,
        "jobs_found": jobs_found,
        "fresh": fresh,
        "saved": saved,
        "skipped": skipped,
        "records": records,
    }


# Source 1: Scrape jobs from the RemoteOK API.
async def scrape_remoteok(
    session: aiohttp.ClientSession,
    resolver: EntityResolver,
    now: datetime,
    collected_at: str,
) -> dict:
    # Initialize counters and record storage.
    source_name = "RemoteOK"
    jobs_found = 0
    fresh = 0
    saved = 0
    skipped = 0
    records = []

    # Wrap the entire source so any error never stops other sources.
    try:
        logger.info("Fetching %s jobs from %s", source_name, REMOTEOK_URL)
        raw_response = await fetch_with_retry(session, REMOTEOK_URL)
        data = parse_json_response(raw_response)

        # Confirm the response is a list with at least one job after the legal notice.
        if not isinstance(data, list) or len(data) <= 1:
            logger.warning("No usable job list returned from %s", source_name)
            return make_source_result(source_name, 0, 0, 0, 0, [])

        # Skip the first item because it is a legal notice.
        job_items = data[1:]

        # Process each job entry.
        for item in job_items:
            if not isinstance(item, dict):
                continue

            jobs_found += 1

            # Use the date field first, or fall back to epoch.
            raw_date = item.get("date")
            if raw_date is None:
                raw_date = item.get("epoch")

            # Keep only items with a time of day that are within 24 hours.
            if not has_time_of_day(raw_date):
                skipped += 1
                continue

            job_dt = parse_date(raw_date, now)
            if job_dt is None or not is_within_24_hours(job_dt, now):
                skipped += 1
                continue

            fresh += 1

            # Resolve the company name through EntityResolver.
            raw_company = clean_string(item.get("company"))
            company = None
            if raw_company is not None:
                company = resolver.resolve(raw_company)

            # Read title and posting URL without inventing values.
            title = clean_string(item.get("position"))
            if title is None:
                title = clean_string(item.get("title"))

            job_url = clean_string(item.get("url"))
            if job_url is None:
                job_url = clean_string(item.get("apply_url"))

            # Build and save the job record.
            record = build_job_record(
                source_name,
                job_url,
                title,
                company,
                job_dt,
                True,
                collected_at,
            )
            records.append(record)
            saved += 1

    except Exception as error:
        logger.error("Error scraping %s: %s", source_name, error)

    # Return summary counts and collected records.
    return make_source_result(source_name, jobs_found, fresh, saved, skipped, records)


# Source 2: Scrape AI jobs from the Remotive API.
async def scrape_remotive(
    session: aiohttp.ClientSession,
    resolver: EntityResolver,
    now: datetime,
    collected_at: str,
) -> dict:
    # Initialize counters and record storage.
    source_name = "Remotive"
    jobs_found = 0
    fresh = 0
    saved = 0
    skipped = 0
    records = []

    # Wrap the entire source so any error never stops other sources.
    try:
        logger.info("Fetching %s jobs from %s", source_name, REMOTIVE_URL)
        raw_response = await fetch_with_retry(session, REMOTIVE_URL)
        data = parse_json_response(raw_response)

        # Confirm the response is a dictionary containing a jobs list.
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
            logger.warning("No usable job list returned from %s", source_name)
            return make_source_result(source_name, 0, 0, 0, 0, [])

        # Process each job entry.
        for item in data["jobs"]:
            if not isinstance(item, dict):
                continue

            jobs_found += 1

            # Parse publication_date and keep only fresh jobs with time of day.
            raw_date = item.get("publication_date")
            if not has_time_of_day(raw_date):
                skipped += 1
                continue

            job_dt = parse_date(raw_date, now)
            if job_dt is None or not is_within_24_hours(job_dt, now):
                skipped += 1
                continue

            fresh += 1

            # Resolve the company name through EntityResolver.
            raw_company = clean_string(item.get("company_name"))
            if raw_company is None:
                raw_company = clean_string(item.get("company"))
            company = None
            if raw_company is not None:
                company = resolver.resolve(raw_company)

            # Read title and posting URL.
            title = clean_string(item.get("title"))
            job_url = clean_string(item.get("url"))

            # Build and save the job record.
            record = build_job_record(
                source_name,
                job_url,
                title,
                company,
                job_dt,
                True,
                collected_at,
            )
            records.append(record)
            saved += 1

    except Exception as error:
        logger.error("Error scraping %s: %s", source_name, error)

    # Return summary counts and collected records.
    return make_source_result(source_name, jobs_found, fresh, saved, skipped, records)


# Remove an XML namespace prefix if present on a tag name.
def strip_namespace(tag: str) -> str:
    # Return the local tag name after "}".
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


# Source 3: Scrape programming jobs from the We Work Remotely RSS feed.
async def scrape_weworkremotely(
    session: aiohttp.ClientSession,
    resolver: EntityResolver,
    now: datetime,
    collected_at: str,
) -> dict:
    # Initialize counters and record storage.
    source_name = "We Work Remotely"
    jobs_found = 0
    fresh = 0
    saved = 0
    skipped = 0
    records = []

    # Wrap the entire source so any error never stops other sources.
    try:
        logger.info("Fetching %s jobs from %s", source_name, WEWORKREMOTELY_URL)
        xml_text = await fetch_with_retry(session, WEWORKREMOTELY_URL)

        # Confirm we received non-empty XML text.
        if not isinstance(xml_text, str) or xml_text.strip() == "":
            logger.warning("No XML text returned from %s", source_name)
            return make_source_result(source_name, 0, 0, 0, 0, [])

        # Parse the RSS XML with ElementTree.
        root = ET.fromstring(xml_text.strip())

        # Loop through every <item> element in the RSS feed.
        for element in root.iter():
            if strip_namespace(element.tag) != "item":
                continue

            jobs_found += 1

            raw_title = None
            raw_company = None
            job_url = None
            raw_date = None

            # Read child elements from the RSS item.
            for child in element:
                child_tag = strip_namespace(child.tag)
                child_text = clean_string(child.text)
                if child_tag == "title":
                    raw_title = child_text
                elif child_tag == "link":
                    job_url = child_text
                elif child_tag == "pubDate":
                    raw_date = child_text
                elif child_tag == "company":
                    raw_company = child_text

            # Keep only items with a time of day that are within 24 hours.
            if not has_time_of_day(raw_date):
                skipped += 1
                continue

            job_dt = parse_date(raw_date, now)
            if job_dt is None or not is_within_24_hours(job_dt, now):
                skipped += 1
                continue

            fresh += 1

            # Extract company from "Company: Role" title format if no company tag existed.
            title = raw_title
            if raw_company is None and raw_title is not None and ":" in raw_title:
                parts = raw_title.split(":", 1)
                raw_company = clean_string(parts[0])
                title = clean_string(parts[1])

            # Resolve the company name through EntityResolver.
            company = None
            if raw_company is not None:
                company = resolver.resolve(raw_company)

            # Build and save the job record.
            record = build_job_record(
                source_name,
                job_url,
                title,
                company,
                job_dt,
                True,
                collected_at,
            )
            records.append(record)
            saved += 1

    except Exception as error:
        logger.error("Error scraping %s: %s", source_name, error)

    # Return summary counts and collected records.
    return make_source_result(source_name, jobs_found, fresh, saved, skipped, records)


# Source 4: Scrape jobs from all configured Greenhouse boards.
async def scrape_greenhouse(
    session: aiohttp.ClientSession,
    resolver: EntityResolver,
    now: datetime,
    collected_at: str,
) -> dict:
    # Initialize counters and record storage.
    source_name = "Greenhouse"
    jobs_found = 0
    fresh = 0
    saved = 0
    skipped = 0
    records = []

    # Wrap the entire source so any error never stops other sources.
    try:
        # Loop over every configured Greenhouse board name.
        for board in GREENHOUSE_BOARDS:
            board_url = GREENHOUSE_URL_TEMPLATE.format(board=board)
            logger.info("Fetching %s board '%s' from %s", source_name, board, board_url)

            raw_response = await fetch_with_retry(session, board_url)
            data = parse_json_response(raw_response)

            # Log a warning and move on if a board returns nothing.
            if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
                logger.warning("Greenhouse board '%s' returned no jobs.", board)
                continue

            board_jobs = data["jobs"]
            if len(board_jobs) == 0:
                logger.warning("Greenhouse board '%s' returned an empty job list.", board)
                continue

            # Resolve the board name as the company name.
            company = resolver.resolve(board)

            # Process each job on this board.
            for item in board_jobs:
                if not isinstance(item, dict):
                    continue

                jobs_found += 1

                # Require first_published; skip and count if missing.
                raw_date = item.get("first_published")
                if raw_date is None or not has_time_of_day(raw_date):
                    skipped += 1
                    continue

                job_dt = parse_date(raw_date, now)
                if job_dt is None or not is_within_24_hours(job_dt, now):
                    skipped += 1
                    continue

                fresh += 1

                # Determine is_remote from location text containing "remote".
                is_remote = False
                location_obj = item.get("location")
                if isinstance(location_obj, dict):
                    location_name = clean_string(location_obj.get("name"))
                    if location_name is not None and "remote" in location_name.lower():
                        is_remote = True

                # Read title and posting URL.
                title = clean_string(item.get("title"))
                job_url = clean_string(item.get("absolute_url"))
                if job_url is None:
                    job_url = clean_string(item.get("url"))

                # Build and save the job record.
                record = build_job_record(
                    source_name,
                    job_url,
                    title,
                    company,
                    job_dt,
                    is_remote,
                    collected_at,
                )
                records.append(record)
                saved += 1

    except Exception as error:
        logger.error("Error scraping %s: %s", source_name, error)

    # Return summary counts and collected records.
    return make_source_result(source_name, jobs_found, fresh, saved, skipped, records)


# Determine whether a Lever job posting is remote.
def is_lever_job_remote(item: dict) -> bool:
    # Check Lever's workplaceType field first.
    workplace_type = clean_string(item.get("workplaceType"))
    if workplace_type is not None and workplace_type.lower() == "remote":
        return True

    # Check the location text inside categories.
    categories = item.get("categories")
    if isinstance(categories, dict):
        location_text = clean_string(categories.get("location"))
        if location_text is not None and "remote" in location_text.lower():
            return True

        all_locations = categories.get("allLocations")
        if isinstance(all_locations, list):
            for loc in all_locations:
                if isinstance(loc, str) and "remote" in loc.lower():
                    return True

    # Default to False if no remote signal is present.
    return False


# Source 5: Scrape jobs from all configured Lever boards.
async def scrape_lever(
    session: aiohttp.ClientSession,
    resolver: EntityResolver,
    now: datetime,
    collected_at: str,
) -> dict:
    # Initialize counters and record storage.
    source_name = "Lever"
    jobs_found = 0
    fresh = 0
    saved = 0
    skipped = 0
    records = []

    # Wrap the entire source so any error never stops other sources.
    try:
        # Loop over every configured Lever board name.
        for board in LEVER_BOARDS:
            board_url = LEVER_URL_TEMPLATE.format(board=board)
            logger.info("Fetching %s board '%s' from %s", source_name, board, board_url)

            raw_response = await fetch_with_retry(session, board_url)
            data = parse_json_response(raw_response)

            # Log a warning and move on if a board returns nothing.
            if not isinstance(data, list) or len(data) == 0:
                logger.warning("Lever board '%s' returned no jobs.", board)
                continue

            # Resolve the board name as the company name.
            company = resolver.resolve(board)

            # Process each posting on this board.
            for item in data:
                if not isinstance(item, dict):
                    continue

                jobs_found += 1

                # Use createdAt (milliseconds) as the job date.
                raw_date = item.get("createdAt")
                if not has_time_of_day(raw_date):
                    skipped += 1
                    continue

                job_dt = parse_date(raw_date, now)
                if job_dt is None or not is_within_24_hours(job_dt, now):
                    skipped += 1
                    continue

                fresh += 1

                # Check remote status from workplaceType and location text.
                is_remote = is_lever_job_remote(item)

                # Read title and posting URL.
                title = clean_string(item.get("text"))
                if title is None:
                    title = clean_string(item.get("title"))

                job_url = clean_string(item.get("hostedUrl"))
                if job_url is None:
                    job_url = clean_string(item.get("applyUrl"))

                # Build and save the job record.
                record = build_job_record(
                    source_name,
                    job_url,
                    title,
                    company,
                    job_dt,
                    is_remote,
                    collected_at,
                )
                records.append(record)
                saved += 1

    except Exception as error:
        logger.error("Error scraping %s: %s", source_name, error)

    # Return summary counts and collected records.
    return make_source_result(source_name, jobs_found, fresh, saved, skipped, records)


# Run all 5 job scrapers concurrently and return combined records plus per-source stats.
async def scrape_jobs(resolver: EntityResolver) -> tuple:
    # Capture one UTC reference timestamp for the entire run.
    now = datetime.now(timezone.utc)
    collected_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    # Open a shared async HTTP session and run all 5 sources at the same time.
    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(
            scrape_remoteok(session, resolver, now, collected_at),
            scrape_remotive(session, resolver, now, collected_at),
            scrape_weworkremotely(session, resolver, now, collected_at),
            scrape_greenhouse(session, resolver, now, collected_at),
            scrape_lever(session, resolver, now, collected_at),
        )

    # Combine all records and build the per-source statistics list.
    all_records = []
    source_stats = []
    for result in results:
        for record in result["records"]:
            all_records.append(record)

        source_stats.append(
            {
                "source_name": result["source_name"],
                "jobs_found": result["jobs_found"],
                "fresh": result["fresh"],
                "saved": result["saved"],
                "skipped": result["skipped"],
            }
        )

    # Return the combined records and per-source stats.
    return all_records, source_stats


# Save a Python object to a JSON file, creating the output folder if needed.
def save_json_file(file_path: str, data):
    # Create the parent directory if it does not exist.
    parent_dir = os.path.dirname(file_path)
    if parent_dir != "":
        os.makedirs(parent_dir, exist_ok=True)

    # Write formatted JSON to disk.
    with open(file_path, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, ensure_ascii=False)


# Main entry point to run all 5 job scrapers, save outputs, and log summary stats.
async def main():
    # Initialize the entity resolver.
    resolver = EntityResolver()

    # Run all 5 job sources concurrently.
    records, source_stats = await scrape_jobs(resolver)

    # Save the collected jobs and entity resolution log to the output folder.
    save_json_file(JOBS_OUTPUT_PATH, records)
    save_json_file(ENTITY_LOG_OUTPUT_PATH, resolver.get_log())

    # Log summary counts for each source.
    for stats in source_stats:
        logger.info(
            "Source=%s | jobs_found=%d | fresh=%d | saved=%d | skipped=%d",
            stats["source_name"],
            stats["jobs_found"],
            stats["fresh"],
            stats["saved"],
            stats["skipped"],
        )

    # Log the first record if at least one job was saved.
    if len(records) > 0:
        logger.info("First record:\n%s", json.dumps(records[0], indent=2))
    else:
        logger.info("No job records were saved in this run.")


# Run the async main function when executed directly.
if __name__ == "__main__":
    asyncio.run(main())