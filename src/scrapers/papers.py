# Standard library imports for async, dates, json, file paths, regex, and XML parsing
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

# API endpoint templates
HF_DAILY_PAPERS_URL = "https://huggingface.co/api/daily_papers?date={date_str}"
ARXIV_API_URL = "http://export.arxiv.org/api/query?id_list={arxiv_id}"
GITHUB_API_URL = "https://api.github.com/repos/{owner}/{repo}"


# Generate a list of YYYY-MM-DD date strings between start_date and end_date inclusive
def build_date_list(start_date: str, end_date: str) -> list[str]:
    # Parse start and end strings into date objects
    start = datetime.strptime(start_date, "%Y-%m-%d").date()
    end = datetime.strptime(end_date, "%Y-%m-%d").date()

    # Collect every date in the range step by step
    dates = []
    current = start
    while current <= end:
        dates.append(current.strftime("%Y-%m-%d"))
        current = current + timedelta(days=1)

    return dates


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

    # Perform HTTP GET using the retry helper
    response_text = await fetch_with_retry(session, url)
    if not response_text:
        logger.warning(f"No response from Hugging Face for {date_str}")
        return []

    # Parse the JSON response safely
    try:
        data = response_text
    except json.JSONDecodeError as exc:
        logger.error(f"Failed to parse Hugging Face JSON for {date_str}: {exc}")
        return []

    # Ensure the returned structure is a list
    if not isinstance(data, list):
        logger.warning(f"Unexpected Hugging Face response format for {date_str}")
        return []

    return data


# Fetch title, authors, and published date from the arXiv Atom XML API
async def fetch_arxiv_metadata(
    session: aiohttp.ClientSession, arxiv_id: str
) -> dict | None:
    # Build the arXiv API query URL
    url = ARXIV_API_URL.format(arxiv_id=arxiv_id)
    logger.info(f"Fetching arXiv metadata for {arxiv_id}")

    # Perform HTTP GET using the retry helper
    xml_text = await fetch_with_retry(session, url)
    if not xml_text:
        logger.warning(f"No response from arXiv API for {arxiv_id}")
        return None

    # Parse the Atom XML feed
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        logger.error(f"Failed to parse arXiv XML for {arxiv_id}: {exc}")
        return None

    # Define Atom XML namespace
    ns = {"atom": "http://www.w3.org/2005/Atom"}

    # Locate the first entry element in the feed
    entry = root.find("atom:entry", ns)
    if entry is None:
        logger.warning(f"No entry found in arXiv response for {arxiv_id}")
        return None

    # Extract and clean the paper title
    title_el = entry.find("atom:title", ns)
    if title_el is None or not title_el.text:
        return None
    title = " ".join(title_el.text.split())

    # Extract all author names into a plain list
    authors = []
    for author_el in entry.findall("atom:author", ns):
        name_el = author_el.find("atom:name", ns)
        if name_el is not None and name_el.text:
            clean_name = name_el.text.strip()
            if clean_name:
                authors.append(clean_name)

    # Skip if no authors were found
    if not authors:
        return None

    # Extract the published date string in ISO-8601 format
    published_el = entry.find("atom:published", ns)
    if published_el is None or not published_el.text:
        return None
    published_date = published_el.text.strip()

    # Build the canonical arXiv abstract URL
    paper_url = f"https://arxiv.org/abs/{arxiv_id}"

    return {
        "title": title,
        "authors": authors,
        "published_date": published_date,
        "paper_url": paper_url,
    }


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

    # Perform HTTP GET using the retry helper
    response_text = await fetch_with_retry(session, url, headers=headers)
    if not response_text:
        logger.warning(f"No response from GitHub API for {owner}/{repo}")
        return None, None

    # Parse the JSON response
    try:
       data = response_text
    except json.JSONDecodeError as exc:
        logger.error(f"Failed to parse GitHub JSON for {owner}/{repo}: {exc}")
        return None, None

    # Read stargazers_count and verify it is an integer
    stars = data.get("stargazers_count")
    if not isinstance(stars, int):
        logger.warning(f"Missing stargazers_count for {owner}/{repo}")
        return None, None

    return stars, checked_at


# Process one candidate paper through arXiv and GitHub APIs and build final record
async def build_paper_record(
    session: aiohttp.ClientSession,
    arxiv_id: str,
    github_url: str,
    source_url: str,
) -> dict | None:
    # Validate and parse owner and repo from the GitHub URL
    owner, repo = parse_github_owner_repo(github_url)
    if not owner or not repo:
        logger.warning(f"Invalid GitHub URL for {arxiv_id}: {github_url}")
        return None

    # Fetch metadata from arXiv API
    arxiv_data = await fetch_arxiv_metadata(session, arxiv_id)
    if not arxiv_data:
        logger.warning(f"Skipping {arxiv_id} due to missing arXiv metadata")
        return None

    # Fetch star count from GitHub API
    stars, checked_at = await fetch_github_stars(session, owner, repo)
    if stars is None or checked_at is None:
        logger.warning(f"Skipping {arxiv_id} due to missing GitHub stars")
        return None

    # Construct normalized canonical GitHub URL
    canonical_github_url = f"https://github.com/{owner}/{repo}"

    # Assemble the final record matching the required schema
    record = {
        "schemaVersion": "1.0",
        "recordType": "RESEARCH_PAPER",
        "source": {
            "name": "huggingface_daily_papers",
            "url": source_url,
        },
        "content": {
            "title": arxiv_data["title"],
            "authors": arxiv_data["authors"],
            "paper_url": arxiv_data["paper_url"],
            "github_url": canonical_github_url,
            "github_stars": stars,
            "published_date": arxiv_data["published_date"],
        },
        "collectedAt": checked_at,
    }

    return record


# Scrape and enrich daily papers across a date range with deduplication by arXiv ID
async def scrape_papers(start_date: str, end_date: str) -> list[dict]:
    # Build the list of dates to process
    dates = build_date_list(start_date, end_date)
    logger.info(f"Starting paper scrape from {start_date} to {end_date}")

    # Track seen arXiv IDs to deduplicate papers across dates
    seen_arxiv_ids = set()
    records = []

    # Open a single shared aiohttp session for all requests
    async with aiohttp.ClientSession() as session:
        # Loop through each date sequentially
        for date_str in dates:
            source_url = HF_DAILY_PAPERS_URL.format(date_str=date_str)
            items = await fetch_hf_daily_papers(session, date_str)

            # Inspect every paper returned for this date
            for item in items:
                if not isinstance(item, dict):
                    continue

                # Extract arXiv ID and skip if missing or already seen
                arxiv_id = extract_arxiv_id(item)
                if not arxiv_id:
                    continue
                if arxiv_id in seen_arxiv_ids:
                    logger.info(f"Skipping duplicate arXiv ID: {arxiv_id}")
                    continue

                # Keep only papers that include a GitHub repository link
                github_url = extract_github_url(item)
                if not github_url:
                    continue

                # Mark arXiv ID as seen before enriching
                seen_arxiv_ids.add(arxiv_id)

                # Fetch arXiv metadata and GitHub stars to construct record
                record = await build_paper_record(
                    session=session,
                    arxiv_id=arxiv_id,
                    github_url=github_url,
                    source_url=source_url,
                )

                # Save valid complete records
                if record is not None:
                    records.append(record)

                # Pause briefly between arXiv API calls to respect rate limits
                await asyncio.sleep(1.0)

    logger.info(f"Completed paper scrape with {len(records)} valid records")
    return records


# Entry point to test scraping 3 days of papers and save results to JSON
async def main() -> None:
    # Choose 3 recent weekday dates for testing
    start_date = "2026-11-18"
    end_date = "2026-11-20"

    # Run the scraper pipeline
    records = await scrape_papers(start_date, end_date)

    # Ensure output directory exists and save JSON file
    output_path = Path("output/papers.json")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, ensure_ascii=False)

    # Log and display record count and the first 2 records
    logger.info(f"Saved {len(records)} records to {output_path}")
    logger.info(f"First 2 records: {json.dumps(records[:2], indent=2)}")


if __name__ == "__main__":
    asyncio.run(main())