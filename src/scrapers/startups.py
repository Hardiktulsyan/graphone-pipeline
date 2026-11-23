# Standard library imports for async execution, timestamps, JSON, and file paths.
import asyncio
from datetime import datetime, timezone
import json
import os

# Third-party async HTTP library.
import aiohttp

# Project imports for entity resolution, logging, and HTTP retries.
from src.resolver.entity_resolver import EntityResolver
from src.utils.logger import get_logger
from src.utils.retry import fetch_with_retry

# Create a module-level logger.
logger = get_logger(__name__)

# Data source URL for the Y Combinator company list.
YC_COMPANIES_URL = "https://yc-oss.github.io/api/companies/all.json"

# Maximum number of startup records to keep.
MAX_RECORDS = 2200

# Exact AI-related tags required to keep a company.
AI_TAGS = {
    "AI",
    "Machine Learning",
    "Generative AI",
    "AI Assistant",
    "Conversational AI",
    "AIOps",
    "AI-powered Drug Discovery",
    "AI-Enhanced Learning",
    "Swarm AI",
}

# Output file paths used when running this script directly.
OUTPUT_DIR = "output"
STARTUPS_OUTPUT_PATH = os.path.join(OUTPUT_DIR, "startups.json")
ENTITY_LOG_OUTPUT_PATH = os.path.join(OUTPUT_DIR, "entity_log_startups.json")


# Convert raw team_size into an integer, or return None if missing or invalid.
def parse_employee_count(raw_value):
    # Return None if the value is missing or is a boolean.
    if raw_value is None:
        return None
    if isinstance(raw_value, bool):
        return None

    # Try converting the value to an integer.
    try:
        return int(raw_value)
    except (ValueError, TypeError):
        return None


# Return True if the tags list contains at least one exact AI tag.
def has_matching_ai_tag(tags) -> bool:
    # Return False if tags is missing or is not a list.
    if not isinstance(tags, list):
        return False

    # Check each tag against the allowed AI tag set.
    for tag in tags:
        if tag in AI_TAGS:
            return True

    # No matching tag was found.
    return False


# Check whether a company meets all filter requirements.
def should_keep_company(company: dict) -> bool:
    # Require status to be exactly "Active".
    status = company.get("status")
    if status != "Active":
        return False

    # Require website to be a non-empty string.
    website = company.get("website")
    if not isinstance(website, str):
        return False
    if website.strip() == "":
        return False

    # Require at least one matching AI tag.
    tags = company.get("tags")
    if not has_matching_ai_tag(tags):
        return False

    # All filter checks passed.
    return True


# Build one startup record matching the required pipeline schema.
def build_startup_record(company: dict, canonical_name: str, collected_at: str) -> dict:
    # Read the source URL and set to None if missing or blank.
    raw_url = company.get("url")
    source_url = None
    if isinstance(raw_url, str) and raw_url.strip() != "":
        source_url = raw_url.strip()

    # Read the website and clean outer whitespace.
    raw_website = company.get("website")
    website = None
    if isinstance(raw_website, str) and raw_website.strip() != "":
        website = raw_website.strip()

    # Parse team_size as an integer or None.
    employee_count = parse_employee_count(company.get("team_size"))

    # Return the structured record dictionary.
    return {
        "schemaVersion": "1.0",
        "recordType": "STARTUP",
        "source": {
            "name": "Y Combinator",
            "url": source_url,
        },
        "content": {
            "entityName": canonical_name,
            "data": {
                "employeeCount": employee_count,
                "website": website,
            },
        },
        "collectedAt": collected_at,
    }


# Download, filter, resolve, and deduplicate Y Combinator AI startup records.
async def scrape_startups(resolver: EntityResolver) -> list:
    # Log the start of the scrape.
    logger.info("Fetching Y Combinator companies from %s", YC_COMPANIES_URL)

    # Open an async HTTP session and fetch the data with retries.
    async with aiohttp.ClientSession() as session:
        raw_response = await fetch_with_retry(session, YC_COMPANIES_URL)

    # Return an empty list if nothing was returned.
    if raw_response is None:
        logger.error("No data returned from %s", YC_COMPANIES_URL)
        return []

    # Parse JSON manually if fetch_with_retry returned raw text.
    if isinstance(raw_response, str):
        try:
            companies = json.loads(raw_response)
        except json.JSONDecodeError as error:
            logger.error("Failed to parse JSON response: %s", error)
            return []
    else:
        companies = raw_response

    # Confirm the parsed response is a list of companies.
    if not isinstance(companies, list):
        logger.error("Expected a list of companies, got %s", type(companies).__name__)
        return []

    # Record a single ISO-8601 UTC timestamp for this collection run.
    collected_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Track kept records and canonical names already seen.
    records = []
    seen_names = set()

    # Loop through all downloaded companies and apply filters and deduplication.
    for company in companies:
        # Stop once we have reached the maximum record limit.
        if len(records) >= MAX_RECORDS:
            break

        # Skip malformed entries that are not dictionaries.
        if not isinstance(company, dict):
            continue

        # Apply status, website, and AI tag filters.
        if not should_keep_company(company):
            continue

        # Read the raw company name and skip if missing.
        raw_name = company.get("name")
        if not isinstance(raw_name, str) or raw_name.strip() == "":
            continue

        # Resolve the company name to its canonical form.
        canonical_name = resolver.resolve(raw_name)
        if canonical_name is None:
            continue

        # Skip duplicates based on the canonical name.
        if canonical_name in seen_names:
            continue
        seen_names.add(canonical_name)

        # Build and store the startup record.
        record = build_startup_record(company, canonical_name, collected_at)
        records.append(record)

    # Log the final record count.
    logger.info("Collected %d unique AI startup records.", len(records))
    return records


# Save a Python object to a JSON file, creating the output folder if needed.
def save_json_file(file_path: str, data):
    # Create the parent directory if it does not exist.
    parent_dir = os.path.dirname(file_path)
    if parent_dir != "":
        os.makedirs(parent_dir, exist_ok=True)

    # Write formatted JSON to disk.
    with open(file_path, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, ensure_ascii=False)


# Main entry point to run the scraper, save outputs, and log summary stats.
async def main():
    # Initialize the entity resolver.
    resolver = EntityResolver()

    # Run the startup scraper.
    records = await scrape_startups(resolver)

    # Save the startup records and entity resolution log to the output folder.
    save_json_file(STARTUPS_OUTPUT_PATH, records)
    save_json_file(ENTITY_LOG_OUTPUT_PATH, resolver.get_log())

    # Count how many records have employeeCount set to None.
    missing_employee_count = 0
    for record in records:
        employee_count = record["content"]["data"]["employeeCount"]
        if employee_count is None:
            missing_employee_count += 1

    # Log total count, missing employeeCount total, and the first 2 records.
    logger.info("Total records saved: %d", len(records))
    logger.info("Records with employeeCount None: %d", missing_employee_count)
    first_two = records[:2]
    logger.info("First 2 records:\n%s", json.dumps(first_two, indent=2))


# Run the async main function when executed directly.
if __name__ == "__main__":
    asyncio.run(main())