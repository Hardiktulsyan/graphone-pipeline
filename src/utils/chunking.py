# Standard library import for splitting text into sentences.
import re

# Third-party HTML parser.
from bs4 import BeautifulSoup

# Project logger helper.
from src.utils.logger import get_logger

# Create a module-level logger.
logger = get_logger(__name__)

# Fractions of the character budget kept from the start and end when truncating.
START_BUDGET_RATIO = 0.60
END_BUDGET_RATIO = 0.10

# Pattern that splits text on whitespace following sentence-ending punctuation.
SENTENCE_SPLIT_PATTERN = re.compile(r"(?<=[.!?])\s+")


# Collapse repeated spaces, tabs, and newlines into single spaces.
def collapse_whitespace(text: str) -> str:
    # Return an empty string if the input is missing or not a string.
    if not isinstance(text, str):
        return ""

    # Split on any whitespace and join with single spaces.
    return " ".join(text.split())


# Extract the page title from a BeautifulSoup document, or return None if missing.
def extract_title(soup: BeautifulSoup):
    # Check if a <title> tag exists.
    if soup.title is None:
        return None

    # Clean whitespace and return None if blank.
    title_text = collapse_whitespace(soup.title.get_text(" ", strip=True))
    if title_text == "":
        return None
    return title_text


# Extract the meta description from a BeautifulSoup document, or return None if missing.
def extract_meta_description(soup: BeautifulSoup):
    # Loop through all <meta> tags and look for name="description".
    for meta_tag in soup.find_all("meta"):
        name_attr = meta_tag.get("name")
        if not isinstance(name_attr, str):
            continue
        if name_attr.strip().lower() != "description":
            continue

        # Read and clean the content attribute.
        content_attr = meta_tag.get("content")
        description = collapse_whitespace(content_attr)
        if description != "":
            return description

    # Return None if no meta description was found.
    return None


# Split text into a list of complete sentences.
def split_sentences(text: str) -> list:
    # Return an empty list if the text is blank.
    if text == "":
        return []

    # Split on sentence boundaries and keep only non-empty sentences.
    raw_parts = SENTENCE_SPLIT_PATTERN.split(text)
    sentences = []
    for part in raw_parts:
        cleaned = part.strip()
        if cleaned != "":
            sentences.append(cleaned)
    return sentences


# Keep complete sentences from the start of the list without exceeding char_budget.
def take_sentences_from_start(sentences: list, char_budget: int) -> str:
    # Return an empty string if the budget is zero or negative.
    if char_budget <= 0:
        return ""

    # Collect whole sentences from the front while staying within budget.
    kept = []
    current_length = 0

    for sentence in sentences:
        # Account for the single space added between sentences.
        extra_space = 0
        if len(kept) > 0:
            extra_space = 1

        next_length = current_length + extra_space + len(sentence)
        if next_length > char_budget:
            break

        kept.append(sentence)
        current_length = next_length

    # Join the kept sentences with spaces.
    return " ".join(kept)


# Keep complete sentences from the end of the list without exceeding char_budget.
def take_sentences_from_end(sentences: list, char_budget: int) -> str:
    # Return an empty string if the budget is zero or negative.
    if char_budget <= 0:
        return ""

    # Collect whole sentences walking backward from the end.
    kept_reversed = []
    current_length = 0

    for sentence in reversed(sentences):
        # Account for the single space added between sentences.
        extra_space = 0
        if len(kept_reversed) > 0:
            extra_space = 1

        next_length = current_length + extra_space + len(sentence)
        if next_length > char_budget:
            break

        kept_reversed.append(sentence)
        current_length = next_length

    # Restore the original sentence order and join with spaces.
    kept_reversed.reverse()
    return " ".join(kept_reversed)


# Clean HTML into plain text and truncate on sentence boundaries if it exceeds max_chars.
def prepare_text(html, max_chars: int = 12000) -> tuple:
    # Return an empty string and False if html is missing or blank.
    if not isinstance(html, str) or html.strip() == "":
        return "", False

    # Parse the HTML with BeautifulSoup.
    soup = BeautifulSoup(html, "html.parser")

    # Read title and meta description before removing page elements.
    page_title = extract_title(soup)
    meta_description = extract_meta_description(soup)

    # Remove script, style, nav, footer, and header tags.
    unwanted_tags = soup.find_all(["script", "style", "nav", "footer", "header"])
    for tag in unwanted_tags:
        tag.decompose()

    # Extract all remaining text and collapse whitespace.
    main_text = collapse_whitespace(soup.get_text(" "))

    # Return immediately if the cleaned text already fits within max_chars.
    if len(main_text) <= max_chars:
        return main_text, False

    # Split the main text into complete sentences so we never cut mid-sentence.
    sentences = split_sentences(main_text)

    # Calculate character budgets for the start (60 percent) and end (10 percent).
    start_budget = int(max_chars * START_BUDGET_RATIO)
    end_budget = int(max_chars * END_BUDGET_RATIO)

    # Select whole sentences from the beginning and the end.
    start_chunk = take_sentences_from_start(sentences, start_budget)
    end_chunk = take_sentences_from_end(sentences, end_budget)

    # Assemble the final truncated text from the available parts.
    parts = []
    if page_title is not None:
        parts.append("Title: " + page_title)
    if meta_description is not None:
        parts.append("Description: " + meta_description)
    if start_chunk != "":
        parts.append(start_chunk)
    if end_chunk != "":
        parts.append(end_chunk)

    # Join the parts with blank lines and return with truncated=True.
    final_text = "\n\n".join(parts)
    return final_text, True


# Run tests on a short page, a very long page, and an empty string.
if __name__ == "__main__":
    # Build a short sample HTML page.
    short_html = (
        "<html><head><title>Short Page</title>"
        "<meta name='description' content='A short test page.'>"
        "<style>body { color: black; }</style></head>"
        "<body><header>Top Header</header><nav>Menu</nav>"
        "<main><p>This is a short page about AI startups. It has two sentences.</p></main>"
        "<script>var x = 1;</script><footer>Bottom Footer</footer></body></html>"
    )

    # Build a very long sample HTML page exceeding 12,000 characters.
    repeated_sentences = []
    for index in range(1, 251):
        sentence = (
            "Sentence number "
            + str(index)
            + " describes a real data pipeline step for collecting AI news cleanly."
        )
        repeated_sentences.append(sentence)
    long_body = " ".join(repeated_sentences)

    long_html = (
        "<html><head><title>Very Long Page</title>"
        "<meta name='description' content='A long test page for truncation checks.'>"
        "</head><body><nav>Navigation Links</nav>"
        "<main><p>"
        + long_body
        + "</p></main><footer>Site Footer</footer></body></html>"
    )

    # Test 1: Short page.
    short_text, short_truncated = prepare_text(short_html)
    logger.info(
        "Short page | length=%d | truncated=%s",
        len(short_text),
        short_truncated,
    )

    # Test 2: Very long page.
    long_text, long_truncated = prepare_text(long_html)
    logger.info(
        "Long page | length=%d | truncated=%s",
        len(long_text),
        long_truncated,
    )

    # Test 3: Empty string.
    empty_text, empty_truncated = prepare_text("")
    logger.info(
        "Empty string | length=%d | truncated=%s",
        len(empty_text),
        empty_truncated,
    )