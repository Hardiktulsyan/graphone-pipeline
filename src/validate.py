import json
import os
import random
from datetime import datetime, timedelta, timezone

OUTPUT_FOLDER = "output"

# For each file: its name, the expected record type, and the minimum count (None means no minimum)
FILES = [
    ("startups.json", "STARTUP", 1000),
    ("products.json", "PRODUCT", 1000),
    ("papers.json", "RESEARCH_PAPER", 1000),
    ("jobs.json", "JOB", None),
    ("news.json", "NEWS", None),
]

ALLOWED_PRICING = ["FREE", "FREEMIUM", "PAID", "ENTERPRISE", None]


# Read a JSON file and return its list of records, or None if it cannot be read
def load_records(file_name):
    path = os.path.join(OUTPUT_FOLDER, file_name)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


# Turn an ISO-8601 text into a timezone-aware datetime, or None if it is not valid
def parse_iso(text):
    if not isinstance(text, str):
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


# Check that a value is a non-empty string
def is_text(value):
    return isinstance(value, str) and value.strip() != ""


# Check that a value is a web link
def is_link(value):
    return isinstance(value, str) and value.startswith("http")


# Check that a value is a real integer (not a boolean)
def is_integer(value):
    return isinstance(value, int) and not isinstance(value, bool)


# Checks for the content of a startup record
def check_startup(content):
    problems = []
    if not is_text(content.get("entityName")):
        problems.append("entityName is missing")
    data = content.get("data", {})
    count = data.get("employeeCount")
    if count is not None and not is_integer(count):
        problems.append("employeeCount is not an integer or null")
    return problems


# Checks for the content of a product record
def check_product(content):
    problems = []
    if not is_text(content.get("startupName")):
        problems.append("startupName is missing")
    if content.get("pricingModel") not in ALLOWED_PRICING:
        problems.append("pricingModel is not an allowed value")
    return problems


# Checks for the content of a research paper record
def check_paper(content):
    problems = []
    if not is_text(content.get("title")):
        problems.append("title is missing")
    authors = content.get("authors")
    if not isinstance(authors, list) or len(authors) == 0:
        problems.append("authors is not a list with names")
    if not is_link(content.get("paper_url")):
        problems.append("paper_url is not a link")
    if not is_link(content.get("github_url")):
        problems.append("github_url is not a link")
    if not is_integer(content.get("github_stars")):
        problems.append("github_stars is not an integer")
    if parse_iso(content.get("published_date")) is None:
        problems.append("published_date is not ISO-8601")
    return problems


# Checks for the content of a job record
def check_job(content):
    problems = []
    if not is_text(content.get("company")):
        problems.append("company is missing")
    if parse_iso(content.get("date")) is None:
        problems.append("date is not ISO-8601")
    if not isinstance(content.get("is_remote"), bool):
        problems.append("is_remote is not true or false")
    if not is_text(content.get("role_family")):
        problems.append("role_family is missing")
    return problems


# Checks for the content of a news record
def check_news(content):
    problems = []
    if not is_text(content.get("title")):
        problems.append("title is missing")
    if parse_iso(content.get("published_date")) is None:
        problems.append("published_date is not ISO-8601")
    if not is_text(content.get("full_text")):
        problems.append("full_text is missing")
    return problems


CHECKERS = {
    "STARTUP": check_startup,
    "PRODUCT": check_product,
    "RESEARCH_PAPER": check_paper,
    "JOB": check_job,
    "NEWS": check_news,
}


# Run the shared checks and the type-specific checks on one record
def check_record(record, expected_type):
    problems = []
    if record.get("schemaVersion") != "1.0":
        problems.append("schemaVersion is not 1.0")
    if record.get("recordType") != expected_type:
        problems.append("recordType is not " + expected_type)
    source = record.get("source", {})
    if not is_link(source.get("url")):
        problems.append("source.url is not a link")
    if parse_iso(record.get("collectedAt")) is None:
        problems.append("collectedAt is not ISO-8601")
    content = record.get("content", {})
    problems = problems + CHECKERS[expected_type](content)
    return problems


# Build the value used to spot duplicate records of each type
def get_duplicate_key(record, expected_type):
    content = record.get("content", {})
    if expected_type == "STARTUP":
        return str(content.get("entityName")).lower()
    if expected_type == "PRODUCT":
        return str(content.get("startupName")).lower()
    if expected_type == "RESEARCH_PAPER":
        return str(content.get("paper_url"))
    return str(record.get("source", {}).get("url"))


# Count records whose date is older than 24 hours (only used for jobs and news)
def count_stale(records):
    stale = 0
    limit = datetime.now(timezone.utc) - timedelta(hours=24)
    for record in records:
        content = record.get("content", {})
        date = parse_iso(content.get("date") or content.get("published_date"))
        if date is not None and date < limit:
            stale = stale + 1
    return stale


# Check one file and return True if it passes
def check_file(file_name, expected_type, minimum):
    print("----", file_name)
    records = load_records(file_name)
    if records is None:
        print("FAIL: file is missing or cannot be read")
        return False, []

    passed = True
    print("Records:", len(records))
    if minimum is not None and len(records) < minimum:
        print("FAIL: fewer than the minimum of", minimum)
        passed = False

    # Run the checks on every record and keep the first few problems as examples
    bad_records = 0
    examples = []
    for record in records:
        problems = check_record(record, expected_type)
        if len(problems) > 0:
            bad_records = bad_records + 1
            if len(examples) < 3:
                examples.append(problems)
    if bad_records > 0:
        print("FAIL:", bad_records, "records have problems. Examples:", examples)
        passed = False

    # Look for duplicates
    seen = set()
    duplicates = 0
    for record in records:
        key = get_duplicate_key(record, expected_type)
        if key in seen:
            duplicates = duplicates + 1
        seen.add(key)
    if duplicates > 0:
        print("FAIL:", duplicates, "duplicate records")
        passed = False

    # Jobs and news must be fresh, so report anything older than 24 hours
    if expected_type in ["JOB", "NEWS"]:
        stale = count_stale(records)
        if stale > 0:
            print("WARNING:", stale, "records are older than 24 hours")

    if passed:
        print("PASS")
    return passed, records


# Check every file, then print random source links to open and verify by hand
def main():
    all_passed = True
    sample_links = []
    for file_name, expected_type, minimum in FILES:
        passed, records = check_file(file_name, expected_type, minimum)
        if not passed:
            all_passed = False
        picked = random.sample(records, min(3, len(records)))
        for record in picked:
            sample_links.append(file_name + " | " + str(record.get("source", {}).get("url")))

    print("---- Open these links and check the page matches the record:")
    for link in sample_links:
        print(link)

    if all_passed:
        print("OVERALL: PASS")
    else:
        print("OVERALL: FAIL")


if __name__ == "__main__":
    main()