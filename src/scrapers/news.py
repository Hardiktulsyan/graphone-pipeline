# Standard library imports for async tasks, timestamps, JSON, paths, regex, and XML parsing.
import asyncio
from datetime import datetime, timezone
import json
import os
import re
import xml.etree.ElementTree as ET

# Third-party imports for async HTTP requests and HTML text extraction.
import aiohttp
from bs4 import BeautifulSoup

# Project imports for date parsing, logging, and HTTP retries.
from src.utils.dates import is_within_24_hours, parse_date
from src.utils.logger import get_logger
from src.utils.retry import fetch_with_retry

# Create a module-level logger.
logger = get_logger(__name__)

# List of AI news RSS and Atom feeds to scrape.
NEWS_FEEDS = [
    {
        "name": "TechCrunch AI",
        "url": "https://techcrunch.com/category/artificial-intelligence/feed/",
    },
    {
        "name": "Ars Technica AI",
        "url": "https://arstechnica.com/ai/feed/",
    },
    {
        "name": "MIT Technology Review AI",
        "url": "https://www.technologyreview.com/topic/artificial-intelligence/feed",
    },
    {
        "name": "The Verge AI",
        "url": "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
    },
    {
        "name": "Hugging Face blog",
        "url": "https://huggingface.co/blog/feed.xml",
    },
]

# Minimum article text length required to keep a news record.
MIN_TEXT_LENGTH = 300

# Output file path used when running this script directly.
OUTPUT_DIR = "output"
NEWS_OUTPUT_PATH = os.path.join(OUTPUT_DIR, "news.json")

# Pattern to detect a clock time like 14:30 or 9:05 inside a date string.
TIME_OF_DAY_PATTERN = re.compile(r"\d{1,2}:\d{2}")

# Pattern to detect a date-only ISO string like 2026-11-22.
DATE_ONLY_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Pattern to detect sub-day relative times such as "2 hours ago" or "15 minutes ago".
SUB_DAY_RELATIVE_PATTERN = re.compile(
    r"^(\d+|a|an)\s+(second|seconds|minute|minutes|hour|hours)\s+ago$"
)


# Remove any XML namespace prefix like "{http://www.w3.org/2005/Atom}" from a tag name.
def strip_namespace(tag: str) -> str:
    # Return the part after "}" if a namespace is present.
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


# Return cleaned text from an XML element, or None if missing or blank.
def get_clean_text(element):
    # Return None if the element or its text is missing.
    if element is None or element.text is None:
        return None

    # Strip outer whitespace and return None if blank.
    cleaned = element.text.strip()
    if cleaned == "":
        return None
    return cleaned


# Extract the article link from an Atom entry element.
def extract_atom_link(entry):
    # Keep track of the first available href as a fallback.
    fallback_url = None

    # Check every child tag named "link".
    for child in entry:
        if strip_namespace(child.tag) != "link":
            continue

        # Read the href attribute first, or fall back to element text.
        href = child.attrib.get("href")
        if href is None:
            href = get_clean_text(child)
        else:
            href = href.strip()

        if href is None or href == "":
            continue

        # Prefer links marked as rel="alternate" or without a rel attribute.
        rel = child.attrib.get("rel", "alternate")
        if rel == "alternate":
            return href

        if fallback_url is None:
            fallback_url = href

    # Return the fallback URL if no alternate link was found.
    return fallback_url


# Parse RSS (<item>) or Atom (<entry>) elements from XML text into a list of dicts.
def parse_feed_items(xml_text: str) -> list:
    # Return an empty list if XML text is missing or blank.
    if not isinstance(xml_text, str) or xml_text.strip() == "":
        return []

    # Parse the XML text into an ElementTree root element.
    try:
        root = ET.fromstring(xml_text.strip())
    except ET.ParseError as error:
        logger.error("Failed to parse feed XML: %s", error)
        return []

    # Collect parsed feed items here.
    items = []

    # Walk all elements to support both RSS <item> and Atom <entry> structures.
    for element in root.iter():
        tag_name = strip_namespace(element.tag)

        # Handle standard RSS <item> elements.
        if tag_name == "item":
            title = None
            link = None
            raw_date = None

            # Read title, link, and pubDate child elements.
            for child in element:
                child_tag = strip_namespace(child.tag)
                if child_tag == "title":
                    title = get_clean_text(child)
                elif child_tag == "link":
                    link = get_clean_text(child)
                elif child_tag == "pubDate":
                    raw_date = get_clean_text(child)

            items.append(
                {
                    "title": title,
                    "url": link,
                    "raw_date": raw_date,
                }
            )

        # Handle Atom <entry> elements.
        elif tag_name == "entry":
            title = None
            published = None
            updated = None

            # Read title, published, and updated child elements.
            for child in element:
                child_tag = strip_namespace(child.tag)
                if child_tag == "title":
                    title = get_clean_text(child)
                elif child_tag == "published":
                    published = get_clean_text(child)
                elif child_tag == "updated":
                    updated = get_clean_text(child)

            # Use published when available, otherwise fall back to updated.
            raw_date = published
            if raw_date is None:
                raw_date = updated

            # Extract the article link from Atom <link> tags.
            link = extract_atom_link(element)

            items.append(
                {
                    "title": title,
                    "url": link,
                    "raw_date": raw_date,
                }
            )

    # Return all parsed items.
    return items


# Check whether a raw date string includes a time of day (rejects missing, date-only, and "yesterday").
def has_time_of_day(raw_date) -> bool:
    # Reject missing or non-string dates.
    if not isinstance(raw_date, str):
        return False

    # Normalize spaces and case for inspection.
    cleaned = " ".join(raw_date.strip().lower().split())
    if cleaned == "":
        return False

    # Explicitly reject "yesterday" and "today".
    if cleaned == "yesterday" or cleaned == "today":
        return False

    # Explicitly reject date-only ISO strings like "2026-11-22".
    if DATE_ONLY_PATTERN.match(cleaned) is not None:
        return False

    # Accept strings that include a clock time such as HH:MM.
    if TIME_OF_DAY_PATTERN.search(cleaned) is not None:
        return True

    # Accept "just now" and sub-day relative phrases like "2 hours ago".
    if cleaned == "just now":
        return True
    if SUB_DAY_RELATIVE_PATTERN.match(cleaned) is not None:
        return True

    # Accept numeric Unix timestamps in seconds or milliseconds.
    if cleaned.isdigit() and len(cleaned) >= 9:
        return True

    # Reject all other date-only or ambiguous strings.
    return False


# Extract clean article text from HTML by removing unwanted tags and joining <p> tags.
def extract_article_text(html_text: str) -> str:
    # Return an empty string if the HTML is missing or not a string.
    if not isinstance(html_text, str) or html_text.strip() == "":
        return ""

    # Parse the HTML document with BeautifulSoup.
    soup = BeautifulSoup(html_text, "html.parser")

    # Remove script, style, nav, header, and footer tags from the page.
    unwanted_tags = soup.find_all(["script", "style", "nav", "header", "footer"])
    for tag in unwanted_tags:
        tag.decompose()

    # Collect non-empty text from every <p> tag.
    paragraphs = []
    for p_tag in soup.find_all("p"):
        text = p_tag.get_text(" ", strip=True)
        if text != "":
            paragraphs.append(text)

    # Join all paragraphs with blank lines between them.
    return "\n\n".join(paragraphs)


# Fetch, filter, and build NEWS records for a single feed.
async def process_feed(session: aiohttp.ClientSession, feed: dict, now: datetime) -> dict:
    # Read feed name and URL.
    feed_name = feed["name"]
    feed_url = feed["url"]

    # Initialize counters and record list for this feed.
    records = []
    items_in_feed = 0
    fresh_items = 0
    saved_items = 0
    skipped_items = 0

    # Fetch the raw XML text for the feed.
    logger.info("Fetching feed: %s (%s)", feed_name, feed_url)
    xml_text = await fetch_with_retry(session, feed_url)

    # Return empty stats if the feed could not be downloaded.
    if not isinstance(xml_text, str):
        logger.warning("Could not read XML text for feed: %s", feed_name)
        return {
            "feed_name": feed_name,
            "items_in_feed": 0,
            "fresh_items": 0,
            "saved_items": 0,
            "skipped_items": 0,
            "records": [],
        }

    # Parse all RSS or Atom items from the feed XML.
    items = parse_feed_items(xml_text)
    items_in_feed = len(items)

    # Record a single ISO-8601 UTC timestamp for collectedAt.
    collected_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    # Filter each item by date and fetch full article text for fresh items.
    for item in items:
        raw_date = item.get("raw_date")

        # Skip items with missing dates, date-only values, or "yesterday".
        if not has_time_of_day(raw_date):
            skipped_items += 1
            continue

        # Parse the date and keep only items within the last 24 hours.
        parsed_dt = parse_date(raw_date, now)
        if parsed_dt is None or not is_within_24_hours(parsed_dt, now):
            skipped_items += 1
            continue

        # Count this item as fresh.
        fresh_items += 1

        # Require an article URL before fetching the page.
        article_url = item.get("url")
        if not isinstance(article_url, str) or article_url.strip() == "":
            skipped_items += 1
            continue

        # Fetch the full article HTML page.
        article_html = await fetch_with_retry(session, article_url)
        if not isinstance(article_html, str):
            skipped_items += 1
            continue

        # Extract paragraph text and skip articles shorter than 300 characters.
        full_text = extract_article_text(article_html)
        if len(full_text) < MIN_TEXT_LENGTH:
            skipped_items += 1
            continue

        # Build the NEWS record using only values present in the source.
        record = {
            "schemaVersion": "1.0",
            "recordType": "NEWS",
            "source": {
                "name": feed_name,
                "url": article_url,
            },
            "content": {
                "title": item.get("title"),
                "published_date": parsed_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "full_text": full_text,
            },
            "collectedAt": collected_at,
        }

        records.append(record)
        saved_items += 1

    # Return both the collected records and the summary counts for this feed.
    return {
        "feed_name": feed_name,
        "items_in_feed": items_in_feed,
        "fresh_items": fresh_items,
        "saved_items": saved_items,
        "skipped_items": skipped_items,
        "records": records,
    }


# Scrape all configured news feeds concurrently and return records plus per-feed stats.
async def scrape_news() -> tuple:
    # Capture one UTC reference timestamp for the entire run.
    now = datetime.now(timezone.utc)

    # Open a shared async HTTP session and run all 5 feed tasks concurrently.
    async with aiohttp.ClientSession() as session:
        tasks = []
        for feed in NEWS_FEEDS:
            tasks.append(process_feed(session, feed, now))
        feed_results = await asyncio.gather(*tasks)

    # Combine all records and collect per-feed statistics.
    all_records = []
    feed_stats = []
    for result in feed_results:
        for record in result["records"]:
            all_records.append(record)

        feed_stats.append(
            {
                "feed_name": result["feed_name"],
                "items_in_feed": result["items_in_feed"],
                "fresh_items": result["fresh_items"],
                "saved_items": result["saved_items"],
                "skipped_items": result["skipped_items"],
            }
        )

    # Return the combined records list and per-feed summary stats.
    return all_records, feed_stats


# Save a Python object to a JSON file, creating the output folder if needed.
def save_json_file(file_path: str, data):
    # Create the parent directory if it does not exist.
    parent_dir = os.path.dirname(file_path)
    if parent_dir != "":
        os.makedirs(parent_dir, exist_ok=True)

    # Write formatted JSON to disk.
    with open(file_path, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, ensure_ascii=False)


# Main entry point to run the news scraper, save output, and log summary stats.
async def main():
    # Scrape all 5 feeds concurrently.
    records, feed_stats = await scrape_news()

    # Save all collected news records to output/news.json.
    save_json_file(NEWS_OUTPUT_PATH, records)

    # Log the counts for each feed.
    for stats in feed_stats:
        logger.info(
            "Feed=%s | items_in_feed=%d | fresh_items=%d | saved_items=%d | skipped_items=%d",
            stats["feed_name"],
            stats["items_in_feed"],
            stats["fresh_items"],
            stats["saved_items"],
            stats["skipped_items"],
        )

    # Log the first record's title and published date if at least one record was saved.
    if len(records) > 0:
        first_record = records[0]
        logger.info(
            "First record | title=%s | published_date=%s",
            first_record["content"]["title"],
            first_record["content"]["published_date"],
        )
    else:
        logger.info("No news records were saved in this run.")


# Run the async main function when executed directly.
if __name__ == "__main__":
    asyncio.run(main())