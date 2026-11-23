# Standard library imports for CLI arguments, async tasks, timestamps, JSON, paths, and regex.
import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
import re

# Third-party async HTTP library.
import aiohttp

# Project imports for concurrency limit, LLM extraction, chunking, logging, and retries.
from src.config import MAX_CONCURRENCY
from src.llm.orchestrator import extract_json
from src.utils.chunking import prepare_text
from src.utils.logger import get_logger
from src.utils.retry import fetch_with_retry

# Create a module-level logger.
logger = get_logger(__name__)

# Input, output, and temporary atomic-save file paths.
INPUT_STARTUPS_PATH = os.path.join("output", "startups.json")
OUTPUT_PRODUCTS_PATH = os.path.join("output", "products.json")
OUTPUT_PRODUCTS_TMP_PATH = os.path.join("output", "products.tmp.json")

# User-Agent header sent on all website requests.
USER_AGENT = "graphone-pipeline-trial/1.0"

# Number of startups processed per checkpoint batch.
SAVE_BATCH_SIZE = 50

# Maximum time in seconds allowed to process one startup before skipping it.
STARTUP_TIMEOUT_SECONDS = 45

# Maximum length for pricingEvidence snippets taken from original page text.
EVIDENCE_MAX_CHARS = 200

# Maximum number of characters after a dollar amount to look for a billing period.
BILLING_PERIOD_WINDOW = 25

# Allowed pricing_model values.
VALID_PRICING_MODELS = {"FREE", "FREEMIUM", "PAID", "ENTERPRISE"}

# Allowed billing period phrases that must follow a dollar amount to count as pricing.
BILLING_PERIODS = [
    "/mo",
    "/month",
    "per month",
    "a month",
    "/year",
    "per year",
    "/user",
    "per user",
    "/seat",
]

# Phrases that support PAID even without a dollar billing period.
PAY_AS_YOU_GO_PHRASES = [
    "pay as you go",
    "pay-as-you-go",
]

# Phrases that support ENTERPRISE.
ENTERPRISE_PHRASES = [
    "contact sales",
    "request a quote",
    "talk to sales",
    "custom pricing",
]

# Phrases that support FREEMIUM when paired with a billing price, "upgrade", or "paid".
FREEMIUM_FREE_PHRASES = [
    "free plan",
    "free tier",
    "free forever",
    "forever free",
    "start for free",
    "get started for free",
    "free version",
]

# Upgrade or paid words that pair with FREEMIUM_FREE_PHRASES.
FREEMIUM_PAID_WORDS = [
    "upgrade",
    "paid",
]

# Phrases that support FREE.
FREE_ONLY_PHRASES = [
    "free forever",
    "100% free",
    "completely free",
    "always free",
    "free to use",
]

# General pricing words used to detect whether a page mentions pricing at all.
GENERAL_PRICING_WORDS = [
    "pricing",
    "price",
    "plan",
    "plans",
    "billing",
    "subscription",
    "enterprise",
    "tier",
    "trial",
    "per user",
    "per seat",
    "/mo",
    "/year",
    "per year",
    "annually",
    "monthly",
]

# Regular expression matching a dollar amount such as "$20" or "$9.99".
DOLLAR_PRICE_PATTERN = re.compile(r"\$\s*\d+(?:[.,]\d+)?")

# Regular expressions for offers that never count for FREE or FREEMIUM.
DISQUALIFIED_FREE_PATTERNS = [
    re.compile(r"\bfree\s+credits?\b", re.IGNORECASE),
    re.compile(r"\bfree\s+trials?\b", re.IGNORECASE),
    re.compile(r"\bdownload\s+free\s+for\s+\d+\s+days?\b", re.IGNORECASE),
]

# Field description passed to extract_json when LLM fallback is needed.
PRICING_LLM_FIELDS = {
    "pricing_model": "Pricing model of the product: FREE, FREEMIUM, PAID, ENTERPRISE, or null",
}


# Build a case-insensitive whole-word regex pattern for a phrase using \b boundaries.
def build_whole_word_pattern(phrase: str) -> re.Pattern:
    # Add a leading \b boundary when the phrase starts with a letter or digit.
    prefix = ""
    if phrase != "" and phrase[0].isalnum():
        prefix = r"\b"

    # Add a trailing \b boundary when the phrase ends with a letter or digit.
    suffix = ""
    if phrase != "" and phrase[-1].isalnum():
        suffix = r"\b"

    # Compile and return the case-insensitive pattern.
    escaped = re.escape(phrase)
    return re.compile(prefix + escaped + suffix, re.IGNORECASE)


# Find the (start, end) span of the first whole-word matching phrase in text, or return None.
def find_first_phrase_span(text: str, phrase_list: list):
    # Check each phrase using whole-word regex matching.
    for phrase in phrase_list:
        pattern = build_whole_word_pattern(phrase)
        match = pattern.search(text)
        if match is not None:
            return match.start(), match.end()
    return None


# Find a dollar amount followed within 25 characters by a whole-word billing period, or return None.
def find_billing_price_span(text: str):
    # Check every dollar amount in the text.
    for match in DOLLAR_PRICE_PATTERN.finditer(text):
        amount_start = match.start()
        amount_end = match.end()

        # Inspect the next 25 characters after the dollar amount.
        window_text = text[amount_end : amount_end + BILLING_PERIOD_WINDOW]

        # Check each billing period using whole-word regex matching.
        for period in BILLING_PERIODS:
            period_pattern = build_whole_word_pattern(period)
            period_match = period_pattern.search(window_text)
            if period_match is not None:
                full_end = amount_end + period_match.end()
                return amount_start, full_end

    # No dollar amount with a nearby billing period was found.
    return None


# Return True if any dollar figure appears anywhere in text.
def has_any_dollar_figure(text: str) -> bool:
    # Check for any match of the dollar pattern.
    match = DOLLAR_PRICE_PATTERN.search(text)
    if match is not None:
        return True
    return False


# Return True if text mentions free credits, free trials, or "download free for N days".
def has_disqualified_free_offer(text: str) -> bool:
    # Check each disqualified pattern against the text.
    for pattern in DISQUALIFIED_FREE_PATTERNS:
        if pattern.search(text) is not None:
            return True
    return False


# Return up to max_len characters of original_text centered around (match_start, match_end).
def extract_evidence_snippet(
    original_text: str,
    match_start: int,
    match_end: int,
    max_len: int = EVIDENCE_MAX_CHARS,
) -> str:
    # Return empty string if original_text is empty.
    text_length = len(original_text)
    if text_length == 0:
        return ""

    # Clamp match bounds safely inside original_text.
    if match_start < 0:
        match_start = 0
    if match_end > text_length:
        match_end = text_length

    # Compute how many extra characters can be shown before and after the match.
    match_length = match_end - match_start
    if match_length >= max_len:
        snippet_start = match_start
        snippet_end = match_start + max_len
    else:
        remaining = max_len - match_length
        before = remaining // 2
        after = remaining - before
        snippet_start = match_start - before
        snippet_end = match_end + after

        # Shift the window right if it went past the beginning of the text.
        if snippet_start < 0:
            snippet_end = snippet_end + (-snippet_start)
            snippet_start = 0

        # Shift the window left if it went past the end of the text.
        if snippet_end > text_length:
            overflow = snippet_end - text_length
            snippet_start = snippet_start - overflow
            if snippet_start < 0:
                snippet_start = 0
            snippet_end = text_length

    # Slice the original text so capitalization and wording match the page.
    return original_text[snippet_start:snippet_end].strip()


# Verify that a stored evidence snippet actually supports the given pricing_model.
def evidence_supports(pricing_model: str, snippet: str) -> bool:
    # Reject missing model or empty snippet.
    if not isinstance(pricing_model, str) or not isinstance(snippet, str):
        return False
    if snippet.strip() == "":
        return False

    # Rule for PAID: billing-period dollar price, or "pay as you go" / "pay-as-you-go".
    if pricing_model == "PAID":
        if find_billing_price_span(snippet) is not None:
            return True
        if find_first_phrase_span(snippet, PAY_AS_YOU_GO_PHRASES) is not None:
            return True
        return False

    # Rule for FREEMIUM: freemium free phrase + (billing price or "upgrade" or "paid"), no free trial/credit.
    if pricing_model == "FREEMIUM":
        if has_disqualified_free_offer(snippet):
            return False
        has_free_phrase = find_first_phrase_span(snippet, FREEMIUM_FREE_PHRASES) is not None
        if not has_free_phrase:
            return False
        has_billing = find_billing_price_span(snippet) is not None
        has_upgrade_or_paid = find_first_phrase_span(snippet, FREEMIUM_PAID_WORDS) is not None
        if has_billing or has_upgrade_or_paid:
            return True
        return False

    # Rule for FREE: strict free phrase, no free trial/credit/download-free-for-N-days.
    if pricing_model == "FREE":
        if has_disqualified_free_offer(snippet):
            return False
        if find_first_phrase_span(snippet, FREE_ONLY_PHRASES) is not None:
            return True
        return False

    # Rule for ENTERPRISE: enterprise sales or custom pricing phrase.
    if pricing_model == "ENTERPRISE":
        if find_first_phrase_span(snippet, ENTERPRISE_PHRASES) is not None:
            return True
        return False

    # Reject any unrecognized pricing model.
    return False


# Trim an LLM evidence string to at most 200 characters around pricing words if it is too long.
def trim_llm_evidence(raw_evidence: str, max_len: int = EVIDENCE_MAX_CHARS) -> str:
    # Return empty string if evidence is missing.
    if not isinstance(raw_evidence, str):
        return ""

    cleaned = raw_evidence.strip()
    if len(cleaned) <= max_len:
        return cleaned

    # Look for the first pricing anchor: "$", whole-word "free", or whole-word "contact sales".
    anchor_start = None
    anchor_end = None

    dollar_index = cleaned.find("$")
    if dollar_index != -1:
        anchor_start = dollar_index
        anchor_end = dollar_index + 1

    if anchor_start is None:
        free_match = build_whole_word_pattern("free").search(cleaned)
        if free_match is not None:
            anchor_start = free_match.start()
            anchor_end = free_match.end()

    if anchor_start is None:
        sales_match = build_whole_word_pattern("contact sales").search(cleaned)
        if sales_match is not None:
            anchor_start = sales_match.start()
            anchor_end = sales_match.end()

    # Center a 200-character snippet around the pricing anchor when found.
    if anchor_start is not None:
        return extract_evidence_snippet(cleaned, anchor_start, anchor_end, max_len)

    # Fall back to the first 200 characters if no anchor appeared.
    return cleaned[:max_len].strip()


# Apply deterministic pricing rules to page text, build the 200-char snippet first, and verify it.
# Returns (pricing_model, evidence_snippet, needs_llm, has_billing_price).
def apply_pricing_rules(page_text: str, is_pricing_page: bool) -> tuple:
    # Return None with no LLM call if the page text is empty.
    if not isinstance(page_text, str) or page_text.strip() == "":
        return None, None, False, False

    # Search for each category using whole-word regex matching on original page_text.
    enterprise_span = find_first_phrase_span(page_text, ENTERPRISE_PHRASES)
    freemium_free_span = find_first_phrase_span(page_text, FREEMIUM_FREE_PHRASES)
    free_only_span = find_first_phrase_span(page_text, FREE_ONLY_PHRASES)
    billing_price_span = find_billing_price_span(page_text)
    payg_span = find_first_phrase_span(page_text, PAY_AS_YOU_GO_PHRASES)
    freemium_paid_word_span = find_first_phrase_span(page_text, FREEMIUM_PAID_WORDS)
    general_word_span = find_first_phrase_span(page_text, GENERAL_PRICING_WORDS)

    has_billing_price = billing_price_span is not None
    has_paid_signal = has_billing_price or (payg_span is not None)
    has_bare_dollar = has_any_dollar_figure(page_text) and not has_billing_price

    # If a dollar figure appears with no billing period, treat the page as unclear and use LLM.
    if has_bare_dollar:
        return None, None, True, False

    # If there are no pricing words at all, return None and do not call the LLM.
    if (
        enterprise_span is None
        and freemium_free_span is None
        and free_only_span is None
        and billing_price_span is None
        and payg_span is None
        and general_word_span is None
    ):
        return None, None, False, False

    # Evaluate each pricing rule independently.
    is_enterprise = enterprise_span is not None
    is_freemium = freemium_free_span is not None and (
        has_billing_price or freemium_paid_word_span is not None
    )
    is_paid = has_paid_signal and freemium_free_span is None
    is_free = (
        free_only_span is not None
        and not has_paid_signal
        and enterprise_span is None
    )

    # Collect every matching candidate along with its character span for snippet extraction.
    matched_rules = []
    if is_enterprise:
        matched_rules.append(("ENTERPRISE", enterprise_span))
    if is_freemium:
        # Center around both the free phrase and price/paid phrase when they fit together.
        partner_span = billing_price_span
        if partner_span is None:
            partner_span = freemium_paid_word_span
        combined_start = min(freemium_free_span[0], partner_span[0])
        combined_end = max(freemium_free_span[1], partner_span[1])
        if (combined_end - combined_start) <= EVIDENCE_MAX_CHARS:
            matched_rules.append(("FREEMIUM", (combined_start, combined_end)))
        else:
            matched_rules.append(("FREEMIUM", freemium_free_span))
    if is_paid:
        paid_span = billing_price_span
        if paid_span is None:
            paid_span = payg_span
        matched_rules.append(("PAID", paid_span))
    if is_free:
        matched_rules.append(("FREE", free_only_span))

    # Validate the single matching rule against its exact 200-char snippet.
    if len(matched_rules) == 1:
        model_name, span = matched_rules[0]

        # Never decide PAID or FREEMIUM from the homepage alone unless a billing period price was found.
        if (
            not is_pricing_page
            and model_name in ("PAID", "FREEMIUM")
            and not has_billing_price
        ):
            return None, None, False, has_billing_price

        # Step 1: Build the 200-char snippet from original page_text first.
        evidence_snippet = extract_evidence_snippet(
            page_text,
            span[0],
            span[1],
            EVIDENCE_MAX_CHARS,
        )

        # Step 2: Run evidence_supports on that exact snippet before accepting it.
        if evidence_supports(model_name, evidence_snippet):
            return model_name, evidence_snippet, False, has_billing_price

        # Snippet failed its rule: return None and method "none".
        return None, None, False, has_billing_price

    # Two rules disagreed, or pricing words exist without a clean rule match: request LLM fallback.
    return None, None, True, has_billing_price


# Fetch a single page and return its HTML string, or None if unavailable.
async def fetch_html_page(session: aiohttp.ClientSession, url: str):
    # Fetch the URL using fetch_with_retry; 403/404 or failures return None.
    try:
        response = await fetch_with_retry(session, url)
    except Exception as error:
        logger.warning("Error fetching %s: %s", url, error)
        return None

    # Keep only non-empty string responses.
    if not isinstance(response, str):
        return None
    if response.strip() == "":
        return None

    return response


# Process one startup record: fetch pages, run rules, optionally call LLM, and build PRODUCT record.
async def process_single_startup(
    session: aiohttp.ClientSession,
    llm_lock: asyncio.Lock,
    llm_state: dict,
    startup: dict,
) -> dict:
    # Read entityName and website from the startup record.
    content = startup.get("content")
    if not isinstance(content, dict):
        return {"status": "skipped", "reason": "no_website", "record": None}

    entity_name = content.get("entityName")
    data = content.get("data")
    website = None
    if isinstance(data, dict):
        raw_website = data.get("website")
        if isinstance(raw_website, str) and raw_website.strip() != "":
            website = raw_website.strip()

    # Skip startups with no website.
    if website is None:
        return {"status": "skipped", "reason": "no_website", "record": None}

    # Build the homepage and /pricing URLs.
    homepage_url = website
    pricing_url = homepage_url.rstrip("/") + "/pricing"

    # Step 1: Fetch the homepage, then fetch website + "/pricing".
    homepage_html = await fetch_html_page(session, homepage_url)
    pricing_html = await fetch_html_page(session, pricing_url)

    # Step 2: Skip if both pages failed.
    if homepage_html is None and pricing_html is None:
        return {"status": "skipped", "reason": "no_page", "record": None}

    # Step 3: Prefer the pricing page when fetched, otherwise use the homepage.
    if pricing_html is not None:
        chosen_html = pricing_html
        chosen_url = pricing_url
        is_pricing_page = True
    else:
        chosen_html = homepage_html
        chosen_url = homepage_url
        is_pricing_page = False

    # Step 4: Clean and prepare text using prepare_text.
    page_text, was_truncated = prepare_text(chosen_html)

    # Step 5: Apply deterministic pricing rules first.
    pricing_model, pricing_evidence, needs_llm, has_billing_price = (
        apply_pricing_rules(page_text, is_pricing_page)
    )
    pricing_method = "none"

    if pricing_model is not None:
        pricing_method = "rule"
    elif needs_llm:
        # Reserve an LLM call slot under the lock if the limit has not been reached.
        can_call_llm = False
        async with llm_lock:
            if llm_state["calls_used"] < llm_state["llm_limit"]:
                llm_state["calls_used"] += 1
                can_call_llm = True

        # Call extract_json only if within the LLM call limit.
        if can_call_llm:
            values, evidence, provider, trace = await extract_json(
                session,
                page_text,
                PRICING_LLM_FIELDS,
            )
            raw_model = values.get("pricing_model")
            raw_evidence = evidence.get("pricing_model")

            # Validate the LLM output using the trimmed 200-char snippet and evidence_supports.
            if (
                isinstance(raw_model, str)
                and raw_model.strip().upper() in VALID_PRICING_MODELS
                and isinstance(raw_evidence, str)
                and raw_evidence.strip() != ""
            ):
                candidate_model = raw_model.strip().upper()
                candidate_snippet = trim_llm_evidence(
                    raw_evidence,
                    EVIDENCE_MAX_CHARS,
                )

                # Reject PAID or FREEMIUM from homepage alone without a billing period price.
                homepage_blocked = (
                    not is_pricing_page
                    and candidate_model in ("PAID", "FREEMIUM")
                    and not has_billing_price
                )

                # Accept only if not homepage-blocked and evidence_supports passes on the snippet.
                if not homepage_blocked and evidence_supports(
                    candidate_model,
                    candidate_snippet,
                ):
                    pricing_model = candidate_model
                    pricing_evidence = candidate_snippet
                    pricing_method = "llm"
                else:
                    pricing_model = None
                    pricing_evidence = None
                    pricing_method = "none"
            else:
                pricing_model = None
                pricing_evidence = None
                pricing_method = "none"

    # Step 6: Build the PRODUCT record with ISO-8601 UTC collectedAt.
    collected_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    record = {
        "schemaVersion": "1.0",
        "recordType": "PRODUCT",
        "source": {
            "name": "Company website",
            "url": chosen_url,
        },
        "content": {
            "startupName": entity_name,
            "pricingModel": pricing_model,
            "pricingEvidence": pricing_evidence,
            "pricingMethod": pricing_method,
        },
        "collectedAt": collected_at,
    }

    return {"status": "saved", "reason": None, "record": record}


# Run process_single_startup under the concurrency semaphore with a 45 second timeout.
async def process_startup_with_timeout(
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    llm_lock: asyncio.Lock,
    llm_state: dict,
    startup: dict,
) -> dict:
    # Acquire the semaphore first so queue wait time does not count against the 45 second limit.
    async with semaphore:
        try:
            return await asyncio.wait_for(
                process_single_startup(session, llm_lock, llm_state, startup),
                timeout=STARTUP_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            entity_name = None
            content = startup.get("content")
            if isinstance(content, dict):
                entity_name = content.get("entityName")
            logger.warning(
                "Timed out after %d seconds while processing startup: %s",
                STARTUP_TIMEOUT_SECONDS,
                entity_name,
            )
            return {"status": "skipped", "reason": "timeout", "record": None}


# Save records atomically by writing output/products.tmp.json and replacing output/products.json.
def save_products_atomic(data):
    # Create the parent directory if it does not exist.
    parent_dir = os.path.dirname(OUTPUT_PRODUCTS_PATH)
    if parent_dir != "":
        os.makedirs(parent_dir, exist_ok=True)

    # Write formatted JSON to the temporary file first.
    with open(OUTPUT_PRODUCTS_TMP_PATH, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, ensure_ascii=False)

    # Atomically replace the final output file with the temporary file.
    os.replace(OUTPUT_PRODUCTS_TMP_PATH, OUTPUT_PRODUCTS_PATH)


# Load existing product records from output/products.json if the file exists and loads cleanly.
def load_existing_products(file_path: str) -> list:
    # Return an empty list if no previous output file exists.
    if not os.path.exists(file_path):
        return []

    # Try loading the existing JSON list; start fresh if unreadable or invalid.
    try:
        with open(file_path, "r", encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError) as error:
        logger.warning("Could not load existing %s (%s). Starting with empty list.", file_path, error)
        return []

    if not isinstance(data, list):
        logger.warning("Existing %s is not a list. Starting with empty list.", file_path)
        return []

    logger.info("Loaded %d existing product records from %s.", len(data), file_path)
    return data


# Load startup records from output/startups.json.
def load_startups(file_path: str) -> list:
    # Check that the input file exists before reading it.
    if not os.path.exists(file_path):
        logger.error("Input file not found: %s", file_path)
        return []

    # Parse and return the JSON list.
    with open(file_path, "r", encoding="utf-8") as file:
        data = json.load(file)

    if not isinstance(data, list):
        logger.error("Expected a list in %s", file_path)
        return []

    return data


# Scrape product pricing for startups in batches of 50 and save atomic checkpoints to disk.
async def scrape_products(start: int, limit, llm_limit: int) -> tuple:
    # Load all startups from output/startups.json.
    startups = load_startups(INPUT_STARTUPS_PATH)

    # Slice from --start index onward.
    if start > 0:
        startups = startups[start:]

    # Apply --limit if provided.
    if limit is not None and limit >= 0:
        startups = startups[:limit]

    # Keep any existing records from output/products.json and append new ones after them.
    all_records = load_existing_products(OUTPUT_PRODUCTS_PATH)

    # Initialize concurrency semaphore, LLM call tracker, and summary counters for this run.
    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
    llm_lock = asyncio.Lock()
    llm_state = {
        "calls_used": 0,
        "llm_limit": llm_limit,
    }

    startups_processed = 0
    records_saved_this_run = 0
    skipped_by_reason = {
        "no_website": 0,
        "no_page": 0,
        "timeout": 0,
    }
    counts_by_method = {
        "rule": 0,
        "llm": 0,
        "none": 0,
    }
    counts_by_model = {
        "FREE": 0,
        "FREEMIUM": 0,
        "PAID": 0,
        "ENTERPRISE": 0,
        "None": 0,
    }

    # Open a shared aiohttp session with the required User-Agent header.
    headers = {"User-Agent": USER_AGENT}
    async with aiohttp.ClientSession(headers=headers) as session:
        # Process startups in batches of 50 so we save progress every 50 startups.
        total_startups = len(startups)
        for batch_start in range(0, total_startups, SAVE_BATCH_SIZE):
            batch_end = batch_start + SAVE_BATCH_SIZE
            batch = startups[batch_start:batch_end]

            # Run all startups in this batch concurrently with a 45 second timeout per startup.
            tasks = []
            for startup in batch:
                tasks.append(
                    process_startup_with_timeout(
                        session,
                        semaphore,
                        llm_lock,
                        llm_state,
                        startup,
                    )
                )
            results = await asyncio.gather(*tasks)

            # Update counts and collect saved records from the batch.
            for result in results:
                startups_processed += 1

                if result["status"] == "skipped":
                    reason = result["reason"]
                    if reason not in skipped_by_reason:
                        skipped_by_reason[reason] = 0
                    skipped_by_reason[reason] += 1
                    continue

                record = result["record"]
                all_records.append(record)
                records_saved_this_run += 1

                # Count by pricingMethod.
                method = record["content"]["pricingMethod"]
                if method not in counts_by_method:
                    counts_by_method[method] = 0
                counts_by_method[method] += 1

                # Count by pricingModel.
                model = record["content"]["pricingModel"]
                model_key = "None"
                if model is not None:
                    model_key = model
                if model_key not in counts_by_model:
                    counts_by_model[model_key] = 0
                counts_by_model[model_key] += 1

            # Atomically save a checkpoint after every 50 startups.
            save_products_atomic(all_records)
            logger.info(
                "Checkpoint saved: processed %d/%d startups in this run, total product records=%d.",
                startups_processed,
                total_startups,
                len(all_records),
            )

    # Atomically save the final output file at the end.
    save_products_atomic(all_records)

    # Return all saved records and the summary statistics dictionary.
    summary = {
        "startups_processed": startups_processed,
        "records_saved": records_saved_this_run,
        "total_records": len(all_records),
        "skipped_by_reason": skipped_by_reason,
        "counts_by_method": counts_by_method,
        "counts_by_model": counts_by_model,
        "llm_calls_used": llm_state["calls_used"],
    }
    return all_records, summary


# Parse command-line arguments --start, --limit, and --llm-limit.
def parse_args():
    # Set up a basic argument parser.
    parser = argparse.ArgumentParser(
        description="Scrape startup websites and extract product pricing models."
    )
    parser.add_argument(
        "--start",
        type=int,
        default=0,
        help="Start processing startups from index N onward (default: 0).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only process the first N startups from the start index (default: all).",
    )
    parser.add_argument(
        "--llm-limit",
        type=int,
        default=300,
        help="Maximum number of LLM calls allowed (default: 300).",
    )
    return parser.parse_args()


# Main entry point to run the product scraper and log summary stats.
async def main():
    # Read CLI options.
    args = parse_args()

    # Run the scraper.
    records, summary = await scrape_products(args.start, args.limit, args.llm_limit)

    # Log all summary statistics, including total product records across all runs.
    logger.info("Startups processed this run: %d", summary["startups_processed"])
    logger.info("Records saved this run: %d", summary["records_saved"])
    logger.info("Total product records: %d", summary["total_records"])
    logger.info("Skipped by reason: %s", summary["skipped_by_reason"])
    logger.info("Counts by method: %s", summary["counts_by_method"])
    logger.info("Counts by pricing model: %s", summary["counts_by_model"])
    logger.info("LLM calls used: %d", summary["llm_calls_used"])

    # Log the first 2 records if available.
    if len(records) > 0:
        first_two = records[:2]
        logger.info("First 2 records:\n%s", json.dumps(first_two, indent=2))
    else:
        logger.info("No product records were saved.")


# Run the async main function when executed directly.
if __name__ == "__main__":
    asyncio.run(main())