# Standard library imports for CLI args, async, dates, json, file paths, regex, and XML parsing
import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET

# Third-party import for async HTTP requests
import aiohttp

# Project imports from existing modules
from src.config import GITHUB_TOKEN
from src.utils.logger import get_logger
from src.utils.retry import fetch_with_retry

# Initialize module logger
logger = get_logger(__name__)

# API endpoint templates and constants
HF_DAILY_PAPERS_URL = "https://huggingface.co/api/daily_papers?date={date_str}"
HF_PAPER_PAGE_URL = "https://huggingface.co/papers/{arxiv_id}"
ARXIV_API_URL = "http://export.arxiv.org/api/query?id_list={id_list}&max_results={max_results}"
GITHUB_API_URL = "https://api.github.com/repos/{owner}/{repo}"
OUTPUT_PATH = Path("output/papers.json")
MAX_RECORDS = 1200
ARXIV_BATCH_SIZE = 50
ARXIV_DELAY_SECONDS = 3.0


# Parse command-line arguments for the scraper
def parse_args() -> argparse.Namespace:
    # Create argument parser and define --days option
    parser = argparse.ArgumentParser(description="Scrape Hugging Face daily papers.")
    parser.add_argument(
        "--days",
        type=int,
        default=3,
        help="Number of recent days to scrape, newest first (default: 3).",
    )
    return parser.parse_args()


# Build a list of YYYY-MM-DD date strings for the last N days, newest first
def build_recent_dates(days: int) -> list[str]:
    # Start from today in UTC and step backward one day at a time
    today = datetime.now(timezone.utc).date()
    dates = []
    offset = 0
    while offset < days:
        target_date = today - timedelta(days=offset)
        dates.append(target_date.strftime("%Y-%m-%d"))
        offset = offset + 1

    return dates


# Extract arXiv ID from a record's paper_url field
def extract_arxiv_id_from_url(paper_url: str) -> str | None:
    # Check if paper_url is a valid string
    if not isinstance(paper_url, str) or not paper_url.strip():
        return None

    # Match the identifier after /abs/ in the arXiv URL
    match = re.search(r"arxiv\.org/abs/([^/?#]+)", paper_url.strip())
    if not match:
        return None

    return match.group(1)


# Load existing records from output/papers.json and collect seen arXiv IDs
def load_existing_records(output_path: Path) -> tuple[list[dict], set[str]]:
    # Return empty structures if the output file does not exist yet
    if not output_path.exists():
        logger.info(f"No existing output file found at {output_path}")
        return [], set()

    # Read and parse the existing JSON file safely
    try:
        with open(output_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logger.error(f"Failed to load existing file {output_path}: {exc}")
        return [], set()

    # Ensure the loaded data is a list
    if not isinstance(data, list):
        logger.warning(f"Existing file {output_path} does not contain a list")
        return [], set()

    # Collect existing records and extract their arXiv IDs
    records = []
    seen_ids = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        records.append(item)

        # Extract arXiv ID from content.paper_url if present
        content = item.get("content")
        if isinstance(content, dict):
            paper_url = content.get("paper_url")
            arxiv_id = extract_arxiv_id_from_url(paper_url)
            if arxiv_id:
                seen_ids.add(arxiv_id)

    logger.info(
        f"Loaded {len(records)} existing records with {len(seen_ids)} arXiv IDs from {output_path}"
    )
    return records, seen_ids


# Save the current list of records to output/papers.json
def save_records(records: list[dict], output_path: Path) -> None:
    # Create parent directories if needed and write formatted JSON
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, ensure_ascii=False)
    logger.info(f"Saved {len(records)} records to {output_path}")


# Extract owner and repository name from a GitHub URL
def parse_github_owner_repo(github_url: str) -> tuple[str | None, str | None]:
    # Return empty tuple if no URL was provided
    if not github_url:
        return None, None

    # Clean whitespace and trailing slashes or .git suffix
    cleaned = github_url.strip().rstrip("/")
    if cleaned.endswith(".git"):
        cleaned = cleaned[:-4]

    # Match standard github.com/owner/repo pattern
    match = re.search(r"github\.com/([^/]+)/([^/]+)", cleaned)
    if not match:
        return None, None

    owner = match.group(1)
    repo = match.group(2)
    return owner, repo


# Extract a GitHub repository URL from a Hugging Face daily paper item
def extract_github_url(item: dict) -> str | None:
    # Check inside the nested paper object first
    paper = item.get("paper")
    if isinstance(paper, dict):
        repo_url = paper.get("githubRepo")
        if isinstance(repo_url, str) and "github.com/" in repo_url:
            return repo_url.strip()

        # Check projectPage as a fallback if it points to a GitHub repo
        project_page = paper.get("projectPage")
        if isinstance(project_page, str) and "github.com/" in project_page:
            return project_page.strip()

    # Check top-level fields on the item if present
    top_repo = item.get("githubRepo")
    if isinstance(top_repo, str) and "github.com/" in top_repo:
        return top_repo.strip()

    return None


# Extract the arXiv ID from a Hugging Face daily paper item
def extract_arxiv_id(item: dict) -> str | None:
    # Look inside the nested paper dictionary
    paper = item.get("paper")
    if isinstance(paper, dict):
        paper_id = paper.get("id")
        if isinstance(paper_id, str) and paper_id.strip():
            return paper_id.strip()

    return None


# Fetch daily papers JSON from Hugging Face for a single date
async def fetch_hf_daily_papers(
    session: aiohttp.ClientSession, date_str: str
) -> list[dict]:
    # Build the target URL for the given date
    url = HF_DAILY_PAPERS_URL.format(date_str=date_str)
    logger.info(f"Fetching Hugging Face daily papers for {date_str}")

    # Perform HTTP GET using the retry helper (returns parsed JSON)
    data = await fetch_with_retry(session, url)
    if not data:
        logger.warning(f"No response from Hugging Face for {date_str}")
        return []

    # Ensure the returned structure is a list
    if not isinstance(data, list):
        logger.warning(f"Unexpected Hugging Face response format for {date_str}")
        return []

    return data


# Split a list of arXiv IDs into batches of at most batch_size items
def chunk_ids(arxiv_ids: list[str], batch_size: int) -> list[list[str]]:
    # Slice the list sequentially into fixed-size chunks
    batches = []
    index = 0
    while index < len(arxiv_ids):
        batches.append(arxiv_ids[index : index + batch_size])
        index = index + batch_size
    return batches


# Match an arXiv Atom entry ID URL back to one of the requested arXiv IDs
def match_entry_arxiv_id(entry_id_text: str, requested_ids: set[str]) -> str | None:
    # Extract the identifier after /abs/ and strip version suffix if needed
    match = re.search(r"arxiv\.org/abs/([^/?#]+)", entry_id_text.strip())
    if not match:
        return None

    raw_id = match.group(1)
    if raw_id in requested_ids:
        return raw_id

    # Strip version suffix like v1 or v2 to match base ID
    base_id = re.sub(r"v\d+$", "", raw_id)
    if base_id in requested_ids:
        return base_id

    return None


# Fetch metadata for multiple arXiv IDs in batches of up to 50 with 3-second delays
async def fetch_arxiv_metadata_batch(
    session: aiohttp.ClientSession, arxiv_ids: list[str]
) -> dict[str, dict]:
    # Return empty dictionary if there are no IDs to query
    if not arxiv_ids:
        return {}

    # Split IDs into batches of up to ARXIV_BATCH_SIZE
    batches = chunk_ids(arxiv_ids, ARXIV_BATCH_SIZE)
    results = {}
    ns = {"atom": "http://www.w3.org/2005/Atom"}

    # Process each batch sequentially
    for batch_index, batch in enumerate(batches):
        # Wait 3 seconds between arXiv requests to avoid rate limiting
        if batch_index > 0:
            await asyncio.sleep(ARXIV_DELAY_SECONDS)

        # Build the comma-separated id_list query URL
        joined_ids = ",".join(batch)
        url = ARXIV_API_URL.format(id_list=joined_ids, max_results=len(batch))
        logger.info(
            f"Fetching arXiv batch {batch_index + 1}/{len(batches)} ({len(batch)} IDs)"
        )

        # Perform HTTP GET using the retry helper
        xml_text = await fetch_with_retry(session, url)
        if not xml_text or not isinstance(xml_text, str):
            logger.warning(f"No valid XML response from arXiv API for batch {batch_index + 1}")
            continue

        # Parse the Atom XML feed
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            logger.error(f"Failed to parse arXiv XML for batch {batch_index + 1}: {exc}")
            continue

        # Map each entry in the response to its requested arXiv ID
        requested_set = set(batch)
        for entry in root.findall("atom:entry", ns):
            id_el = entry.find("atom:id", ns)
            if id_el is None or not id_el.text:
                continue

            matched_id = match_entry_arxiv_id(id_el.text, requested_set)
            if not matched_id:
                continue

            # Extract and clean the paper title
            title_el = entry.find("atom:title", ns)
            if title_el is None or not title_el.text:
                continue
            title = " ".join(title_el.text.split())

            # Extract all author names into a plain list
            authors = []
            for author_el in entry.findall("atom:author", ns):
                name_el = author_el.find("atom:name", ns)
                if name_el is not None and name_el.text:
                    clean_name = name_el.text.strip()
                    if clean_name:
                        authors.append(clean_name)

            # Skip entry if no authors were found
            if not authors:
                continue

            # Extract the published date string in ISO-8601 format
            published_el = entry.find("atom:published", ns)
            if published_el is None or not published_el.text:
                continue
            published_date = published_el.text.strip()

            # Store parsed metadata keyed by the requested arXiv ID
            results[matched_id] = {
                "title": title,
                "authors": authors,
                "published_date": published_date,
                "paper_url": f"https://arxiv.org/abs/{matched_id}",
            }

    return results


# Fetch repository star count from the GitHub REST API and record check timestamp
async def fetch_github_stars(
    session: aiohttp.ClientSession, owner: str, repo: str
) -> tuple[int | None, str | None]:
    # Build the GitHub API repository endpoint
    url = GITHUB_API_URL.format(owner=owner, repo=repo)
    logger.info(f"Fetching GitHub stars for {owner}/{repo}")

    # Prepare GitHub API headers including authorization token if available
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "ai-engineer-pipeline",
    }
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"

    # Record the UTC time at which the GitHub repository is checked
    checked_at = datetime.now(timezone.utc).isoformat()

    # Perform HTTP GET using the retry helper (returns parsed JSON)
    data = await fetch_with_retry(session, url, headers=headers)
    if not data or not isinstance(data, dict):
        logger.warning(f"No valid response from GitHub API for {owner}/{repo}")
        return None, None

    # Read stargazers_count and verify it is an integer
    stars = data.get("stargazers_count")
    if not isinstance(stars, int):
        logger.warning(f"Missing stargazers_count for {owner}/{repo}")
        return None, None

    return stars, checked_at


# Scrape and enrich daily papers for the last N days, newest first
async def scrape_papers(days: int, output_path: Path) -> list[dict]:
    # Load any existing records so restarts skip already-collected arXiv IDs
    records, seen_arxiv_ids = load_existing_records(output_path)

    # Track skip counts by reason for the final summary
    skip_reasons = {
        "invalid_item": 0,
        "missing_arxiv_id": 0,
        "duplicate_arxiv_id": 0,
        "missing_github_url": 0,
        "invalid_github_url": 0,
        "missing_arxiv_metadata": 0,
        "missing_github_stars": 0,
    }

    # Stop immediately if the existing file already reached the record limit
    if len(records) >= MAX_RECORDS:
        logger.info(f"Already have {len(records)} records (limit {MAX_RECORDS}); stopping early")
        log_summary(records, skip_reasons)
        return records

    # Build the list of recent dates, newest first
    dates = build_recent_dates(days)
    logger.info(f"Starting paper scrape for last {days} days: {dates}")

    # Open a single shared aiohttp session for all requests
    async with aiohttp.ClientSession() as session:
        # Process each date sequentially from newest to oldest
        for date_str in dates:
            if len(records) >= MAX_RECORDS:
                logger.info(f"Reached {MAX_RECORDS} unique records; stopping early")
                break

            items = await fetch_hf_daily_papers(session, date_str)
            candidates = []

            # Filter and validate candidate items for this date
            for item in items:
                if len(records) + len(candidates) >= MAX_RECORDS:
                    break

                if not isinstance(item, dict):
                    skip_reasons["invalid_item"] = skip_reasons["invalid_item"] + 1
                    continue

                # Extract arXiv ID and skip if missing or already seen
                arxiv_id = extract_arxiv_id(item)
                if not arxiv_id:
                    skip_reasons["missing_arxiv_id"] = skip_reasons["missing_arxiv_id"] + 1
                    continue
                if arxiv_id in seen_arxiv_ids:
                    skip_reasons["duplicate_arxiv_id"] = skip_reasons["duplicate_arxiv_id"] + 1
                    continue

                # Keep only papers that include a valid GitHub repository link
                github_url = extract_github_url(item)
                if not github_url:
                    skip_reasons["missing_github_url"] = skip_reasons["missing_github_url"] + 1
                    continue

                owner, repo = parse_github_owner_repo(github_url)
                if not owner or not repo:
                    skip_reasons["invalid_github_url"] = skip_reasons["invalid_github_url"] + 1
                    continue

                # Mark arXiv ID as seen and store candidate for batch enrichment
                seen_arxiv_ids.add(arxiv_id)
                candidates.append(
                    {
                        "arxiv_id": arxiv_id,
                        "owner": owner,
                        "repo": repo,
                    }
                )

            # Fetch arXiv metadata in batches of up to 50 for all candidates of this day
            candidate_ids = [c["arxiv_id"] for c in candidates]
            arxiv_metadata_map = await fetch_arxiv_metadata_batch(session, candidate_ids)

            # Enrich each candidate with arXiv metadata and GitHub stars
            for candidate in candidates:
                if len(records) >= MAX_RECORDS:
                    break

                arxiv_id = candidate["arxiv_id"]
                owner = candidate["owner"]
                repo = candidate["repo"]

                # Verify arXiv metadata was returned for this ID
                arxiv_data = arxiv_metadata_map.get(arxiv_id)
                if not arxiv_data:
                    skip_reasons["missing_arxiv_metadata"] = (
                        skip_reasons["missing_arxiv_metadata"] + 1
                    )
                    logger.warning(f"Skipping {arxiv_id} due to missing arXiv metadata")
                    continue

                # Fetch GitHub repository star count
                stars, checked_at = await fetch_github_stars(session, owner, repo)
                if stars is None or checked_at is None:
                    skip_reasons["missing_github_stars"] = (
                        skip_reasons["missing_github_stars"] + 1
                    )
                    logger.warning(f"Skipping {arxiv_id} due to missing GitHub stars")
                    continue

                # Build the record with individual Hugging Face paper page as source URL
                record = {
                    "schemaVersion": "1.0",
                    "recordType": "RESEARCH_PAPER",
                    "source": {
                        "name": "huggingface_daily_papers",
                        "url": HF_PAPER_PAGE_URL.format(arxiv_id=arxiv_id),
                    },
                    "content": {
                        "title": arxiv_data["title"],
                        "authors": arxiv_data["authors"],
                        "paper_url": arxiv_data["paper_url"],
                        "github_url": f"https://github.com/{owner}/{repo}",
                        "github_stars": stars,
                        "published_date": arxiv_data["published_date"],
                    },
                    "collectedAt": checked_at,
                }
                records.append(record)

            # Save progress to disk after finishing each day
            save_records(records, output_path)

            # Wait 3 seconds before moving to the next day's arXiv batch if more days remain
            if len(records) < MAX_RECORDS and candidates:
                await asyncio.sleep(ARXIV_DELAY_SECONDS)

    # Log final counts and skip breakdown
    log_summary(records, skip_reasons)
    return records


# Log final total records and breakdown of skipped items
def log_summary(records: list[dict], skip_reasons: dict[str, int]) -> None:
    # Compute total skipped items across all reasons
    total_skipped = sum(skip_reasons.values())
    logger.info(f"Total records in output: {len(records)}")
    logger.info(f"Total items skipped during run: {total_skipped}")

    # Log each skip reason and its count
    for reason, count in skip_reasons.items():
        logger.info(f"Skipped ({reason}): {count}")


# Entry point to parse CLI arguments and run the paper scraper
async def main() -> None:
    # Read --days from command-line arguments
    args = parse_args()

    # Run the scraper pipeline and save incrementally to output/papers.json
    records = await scrape_papers(days=args.days, output_path=OUTPUT_PATH)

    # Log the first 2 records as a quick sanity check
    logger.info(f"First 2 records: {json.dumps(records[:2], indent=2)}")


if __name__ == "__main__":
    asyncio.run(main())