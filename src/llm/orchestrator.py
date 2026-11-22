# Standard library imports for async tasks, JSON, jitter, regex, and timing.
import asyncio
import json
import random
import re
import time

# Third-party async HTTP library.
import aiohttp

# Project imports for API keys, retry limit, and logging.
from src.config import DEEPSEEK_API_KEY, GEMINI_API_KEY, GROQ_API_KEY
from src.utils.logger import get_logger

# Create a module-level logger.
logger = get_logger(__name__)

# Model constants for each tier.
GEMINI_MODEL_1 = "gemini-3.5-flash-lite"
GEMINI_MODEL_2 = "gemini-3.1-flash-lite"
GROQ_MODEL = "llama-3.3-70b-versatile"
DEEPSEEK_MODEL = "deepseek-chat"
# Retries per tier for LLM calls, kept low so a busy model does not stall the pipeline.
MAX_RETRIES = 2

# API endpoint URLs for each provider.
GEMINI_URL_TEMPLATE = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"

# HTTP timeout per request in seconds.
REQUEST_TIMEOUT_SECONDS = 30

# HTTP status codes that should be retried vs not retried.
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
NON_RETRYABLE_STATUS_CODES = {400, 401, 402, 403, 404, 413}

# Minimum wait in seconds between calls for each tier.
TIER_MIN_INTERVALS = {
    "gemini_model_1": 5.0,
    "gemini_model_2": 4.0,
    "groq": 2.0,
    "deepseek": 1.0,
}

# Per-tier asyncio locks and last-call timestamps for spacing out requests.
_tier_locks = {}
_tier_last_call = {}


# Return the asyncio.Lock for a given tier, creating it on first use.
def get_tier_lock(tier_name: str) -> asyncio.Lock:
    # Create a new lock if this tier does not have one yet.
    if tier_name not in _tier_locks:
        _tier_locks[tier_name] = asyncio.Lock()
    return _tier_locks[tier_name]


# Space out calls on the same tier so we respect the per-tier minimum delay.
async def wait_for_tier_slot(tier_name: str):
    # Look up the minimum delay and lock for this tier.
    min_interval = TIER_MIN_INTERVALS.get(tier_name, 1.0)
    lock = get_tier_lock(tier_name)

    # Hold the lock while checking elapsed time and waiting if needed.
    async with lock:
        now = time.monotonic()
        last_time = _tier_last_call.get(tier_name)
        if last_time is not None:
            elapsed = now - last_time
            if elapsed < min_interval:
                wait_seconds = min_interval - elapsed
                await asyncio.sleep(wait_seconds)

        # Record the timestamp when this call is allowed to start.
        _tier_last_call[tier_name] = time.monotonic()


# Collapse whitespace and lowercase text for evidence verification.
def normalize_for_match(text: str) -> str:
    # Return an empty string if the value is not a string.
    if not isinstance(text, str):
        return ""

    # Lowercase and replace any run of whitespace with a single space.
    return " ".join(text.lower().split())


# Cut source text roughly in half without splitting the middle of a sentence.
def cut_text_in_half_at_sentence(text: str) -> str:
    # Return empty string if text is empty.
    if not isinstance(text, str) or text.strip() == "":
        return ""

    # Take the first half of the text as a candidate slice.
    half_length = len(text) // 2
    if half_length <= 0:
        return text

    first_half = text[:half_length]

    # Search backward for the last sentence-ending punctuation mark.
    last_period = first_half.rfind(".")
    last_exclamation = first_half.rfind("!")
    last_question = first_half.rfind("?")
    cut_index = max(last_period, last_exclamation, last_question)

    # Cut cleanly at the sentence boundary when one is found.
    if cut_index > 0:
        return first_half[: cut_index + 1].strip()

    # Fall back to the last space so we do not cut inside a word.
    last_space = first_half.rfind(" ")
    if last_space > 0:
        return first_half[:last_space].strip()

    return first_half.strip()


# Parse a Retry-After header value into seconds, or return None if unusable.
def parse_retry_after(header_value):
    # Return None if the header is missing.
    if header_value is None:
        return None

    # Try converting numeric Retry-After seconds into a float.
    try:
        seconds = float(str(header_value).strip())
        if seconds > 0:
            return seconds
    except ValueError:
        return None

    return None


# Send a JSON POST request with retries on 429, 500-504, and timeouts.
async def post_json_with_retry(
    session: aiohttp.ClientSession,
    url: str,
    headers: dict,
    body: dict,
) -> tuple:
    # Configure the timeout for aiohttp requests.
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)

    # Track the last status and response body across retry attempts.
    last_status = 0
    last_payload = "timeout"

    # Attempt the initial call plus up to MAX_RETRIES retries.
    for attempt in range(MAX_RETRIES + 1):
        try:
            async with session.post(
                url,
                headers=headers,
                json=body,
                timeout=timeout,
            ) as response:
                status_code = response.status
                raw_text = await response.text()

                # Try parsing the response text as JSON; keep raw text if not JSON.
                try:
                    parsed_payload = json.loads(raw_text)
                except json.JSONDecodeError:
                    parsed_payload = raw_text

                last_status = status_code
                last_payload = parsed_payload

                # Return immediately on success.
                if 200 <= status_code < 300:
                    return status_code, parsed_payload

                # Do not retry client errors such as 400, 401, 402, 403, 404, or 413.
                if status_code in NON_RETRYABLE_STATUS_CODES:
                    return status_code, parsed_payload

                # Retry on 429 and 500-504 while retries remain.
                if status_code in RETRYABLE_STATUS_CODES:
                    if attempt < MAX_RETRIES:
                        retry_after = parse_retry_after(response.headers.get("Retry-After"))
                        if retry_after is not None:
                            base_delay = retry_after
                        else:
                            base_delay = 2 * (2 ** attempt)
                        jitter = random.uniform(0.0, 1.0)
                        wait_seconds = base_delay + jitter

                        logger.warning(
                            "HTTP %d on %s. Retrying (%d/%d) in %.2f seconds.",
                            status_code,
                            url,
                            attempt + 1,
                            MAX_RETRIES,
                            wait_seconds,
                        )
                        await asyncio.sleep(wait_seconds)
                        continue
                    return status_code, parsed_payload

                # Return any other unexpected status code without retrying.
                return status_code, parsed_payload

        except (asyncio.TimeoutError, aiohttp.ClientError) as error:
            last_status = 0
            last_payload = "timeout: " + str(error)

            # Retry timeouts and transient network errors while retries remain.
            if attempt < MAX_RETRIES:
                base_delay = 2 * (2 ** attempt)
                jitter = random.uniform(0.0, 1.0)
                wait_seconds = base_delay + jitter

                logger.warning(
                    "Request error on %s (%s). Retrying (%d/%d) in %.2f seconds.",
                    url,
                    type(error).__name__,
                    attempt + 1,
                    MAX_RETRIES,
                    wait_seconds,
                )
                await asyncio.sleep(wait_seconds)
                continue

            return last_status, last_payload

    # Return the last observed status and payload.
    return last_status, last_payload


# Build the extraction prompt instructing the model to return strict JSON with evidence.
def build_extraction_prompt(source_text: str, fields: dict) -> str:
    # Format each field name and description on its own line.
    field_lines = []
    for field_name, description in fields.items():
        field_lines.append("- " + str(field_name) + ": " + str(description))
    fields_block = "\n".join(field_lines)

    # Assemble the full prompt instructions and source text.
    prompt = (
        "Return only valid JSON and nothing else.\n"
        "For every field listed below, return an object with the shape "
        '{"value": ..., "evidence": ...}.\n'
        "The evidence must be an exact phrase copied verbatim from the page text.\n"
        "If the page text does not clearly state the value, return "
        '{"value": null, "evidence": null}.\n'
        "Never guess or invent any information.\n\n"
        "Fields to extract:\n"
        + fields_block
        + "\n\nPage text:\n"
        + source_text
    )
    return prompt


# Build the URL, headers, and JSON body for a specific provider tier.
def build_tier_request(tier: dict, prompt: str) -> tuple:
    # Read tier settings.
    provider_type = tier["type"]
    model = tier["model"]
    api_key = tier["api_key"]

    # Build a Gemini generateContent request with x-goog-api-key header.
    if provider_type == "gemini":
        url = GEMINI_URL_TEMPLATE.format(model=model)
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        }
        body = {
            "contents": [
                {
                    "parts": [
                        {"text": prompt},
                    ]
                }
            ],
            "generationConfig": {
                "temperature": 0,
                "responseMimeType": "application/json",
            },
        }
        return url, headers, body

    # Build an OpenAI-compatible chat completions request for Groq or DeepSeek.
    url = tier["url"]
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + api_key,
    }
    body = {
        "model": model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": "You extract structured facts from text and return only valid JSON.",
            },
            {
                "role": "user",
                "content": prompt,
            },
        ],
    }
    return url, headers, body


# Extract the raw text reply from a Gemini, Groq, or DeepSeek JSON response.
def extract_model_reply_text(provider_type: str, payload):
    # Ensure the API returned a JSON dictionary.
    if not isinstance(payload, dict):
        return None

    # Parse Gemini candidate text.
    if provider_type == "gemini":
        candidates = payload.get("candidates")
        if not isinstance(candidates, list) or len(candidates) == 0:
            return None
        first_candidate = candidates[0]
        if not isinstance(first_candidate, dict):
            return None
        content = first_candidate.get("content")
        if not isinstance(content, dict):
            return None
        parts = content.get("parts")
        if not isinstance(parts, list) or len(parts) == 0:
            return None
        first_part = parts[0]
        if not isinstance(first_part, dict):
            return None
        return first_part.get("text")

    # Parse OpenAI-compatible choices text for Groq and DeepSeek.
    choices = payload.get("choices")
    if not isinstance(choices, list) or len(choices) == 0:
        return None
    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        return None
    message = first_choice.get("message")
    if not isinstance(message, dict):
        return None
    return message.get("content")


# Parse the model's text reply into a JSON dictionary, or return None if invalid.
def parse_model_json(reply_text):
    # Return None if no string reply was provided.
    if not isinstance(reply_text, str) or reply_text.strip() == "":
        return None

    # Strip optional markdown code fences if present.
    cleaned = reply_text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
        cleaned = cleaned.strip()

    # Parse JSON and verify the top-level object is a dictionary.
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        return None

        # Some models wrap the answer in a list, so use the first item.
    if isinstance(parsed, list) and len(parsed) > 0:
        parsed = parsed[0]

    if not isinstance(parsed, dict):
        return None
    return parsed


# Check whether an HTTP 400 error payload mentions context or token length limits.
def is_context_length_error(status_code: int, payload) -> bool:
    # Always treat HTTP 413 as a payload-too-large error.
    if status_code == 413:
        return True

    # Only inspect body text for HTTP 400 errors.
    if status_code != 400:
        return False

    # Convert payload to lowercase text and look for context or token keywords.
    if isinstance(payload, str):
        text = payload.lower()
    else:
        text = json.dumps(payload).lower()

    if "context" in text or "token" in text:
        return True
    return False


# Verify every extracted field against source_text using normalized substring matching.
def verify_extraction(parsed_json: dict, fields: dict, source_text: str) -> tuple:
    # Normalize the source text once for substring checks.
    normalized_source = normalize_for_match(source_text)

    # Dictionaries for verified values and verified evidence phrases.
    values = {}
    evidence = {}

    # Check each requested field one by one.
    for field_name in fields:
        field_entry = parsed_json.get(field_name)

        # Default to None if the field entry is missing or not a dictionary.
        if not isinstance(field_entry, dict):
            values[field_name] = None
            continue

        raw_value = field_entry.get("value")
        raw_evidence = field_entry.get("evidence")

        # Keep None when the model reported null.
        if raw_value is None:
            values[field_name] = None
            continue

        # Require a non-empty evidence string that exists inside source_text.
        if not isinstance(raw_evidence, str) or raw_evidence.strip() == "":
            values[field_name] = None
            continue

        normalized_evidence = normalize_for_match(raw_evidence)
        if normalized_evidence == "" or normalized_evidence not in normalized_source:
            values[field_name] = None
            continue

        # Store the verified value and its supporting phrase.
        values[field_name] = raw_value
        evidence[field_name] = raw_evidence.strip()

    # Return verified values and verified evidence.
    return values, evidence


# Build a default values dictionary with every requested field set to None.
def build_empty_values(fields: dict) -> dict:
    # Map every field name to None.
    empty = {}
    for field_name in fields:
        empty[field_name] = None
    return empty


# Extract structured JSON fields from source_text using tiered LLM fallback and verification.
async def extract_json(
    session: aiohttp.ClientSession,
    source_text: str,
    fields: dict,
    fake_first_key: bool = False,
) -> tuple:
    # Track a trace message for each tier attempt.
    trace = []

    # Return all None immediately if source_text is empty.
    if not isinstance(source_text, str) or source_text.strip() == "":
        trace.append("input: empty source_text")
        return build_empty_values(fields), {}, "none", trace

    # Choose the key for tier 1 (fake key when testing fallback).
    tier_1_key = GEMINI_API_KEY
    if fake_first_key:
        tier_1_key = "fake_invalid_key_for_testing"

    # Define the 4 tiers in exact fallback order.
    tiers = [
        {
            "name": "gemini_model_1",
            "type": "gemini",
            "model": GEMINI_MODEL_1,
            "url": None,
            "api_key": tier_1_key,
        },
        {
            "name": "gemini_model_2",
            "type": "gemini",
            "model": GEMINI_MODEL_2,
            "url": None,
            "api_key": GEMINI_API_KEY,
        },
        {
            "name": "groq",
            "type": "openai",
            "model": GROQ_MODEL,
            "url": GROQ_URL,
            "api_key": GROQ_API_KEY,
        },
        {
            "name": "deepseek",
            "type": "openai",
            "model": DEEPSEEK_MODEL,
            "url": DEEPSEEK_URL,
            "api_key": DEEPSEEK_API_KEY,
        },
    ]

    # Try each tier in order until one returns valid JSON.
    for tier in tiers:
        tier_name = tier["name"]
        api_key = tier["api_key"]

        # Skip tiers with an empty or missing API key and log a warning.
        if not isinstance(api_key, str) or api_key.strip() == "":
            logger.warning("Skipping tier %s because its API key is empty.", tier_name)
            trace.append(tier_name + ": skipped (missing key)")
            continue

        # Allow one retry on the same tier with halved text if a 413 or context 400 occurs.
        current_text = source_text
        retried_with_half = False

        while True:
            # Build the prompt and provider request payload.
            prompt = build_extraction_prompt(current_text, fields)
            url, headers, body = build_tier_request(tier, prompt)

            # Respect per-tier call spacing before sending the HTTP request.
            await wait_for_tier_slot(tier_name)

            # Send the request with built-in retry handling.
            status_code, payload = await post_json_with_retry(
                session,
                url,
                headers,
                body,
            )

            # Handle timeouts and retryable errors that still failed after retries.
            if status_code == 0:
                logger.warning("Tier %s timed out after retries.", tier_name)
                trace.append(tier_name + ": timeout after retries")
                break

            if status_code in RETRYABLE_STATUS_CODES:
                logger.warning(
                    "Tier %s failed with HTTP %d after retries.",
                    tier_name,
                    status_code,
                )
                trace.append(tier_name + ": " + str(status_code) + " after retries")
                break

            # Handle 413 or 400 mentioning context or token length by halving the text once.
            if is_context_length_error(status_code, payload):
                if not retried_with_half:
                    logger.warning(
                        "Tier %s hit context/size limit (HTTP %d). Cutting text in half and retrying once.",
                        tier_name,
                        status_code,
                    )
                    trace.append(
                        tier_name + ": " + str(status_code) + " context limit, retrying half text"
                    )
                    current_text = cut_text_in_half_at_sentence(current_text)
                    retried_with_half = True
                    continue

                logger.warning(
                    "Tier %s still hit context/size limit (HTTP %d) after halving text.",
                    tier_name,
                    status_code,
                )
                trace.append(tier_name + ": " + str(status_code) + " after half text")
                break

                        # Handle 401, 402, 403, 404, or any other non-200 status by moving to the next tier.
            if status_code < 200 or status_code >= 300:
                logger.warning("Error body: %s", str(payload)[:300])
                logger.warning(
                    "Tier %s returned HTTP %d. Moving to next tier.",
                    tier_name,
                    status_code,
                )
                trace.append(tier_name + ": HTTP " + str(status_code))
                break
            # Extract the model reply text and parse it as JSON.
            reply_text = extract_model_reply_text(tier["type"], payload)
            parsed_json = parse_model_json(reply_text)
            if parsed_json is None:
                logger.warning("Reply started with: %s", str(reply_text)[:300])
                logger.warning(
                    "Tier %s returned invalid JSON. Moving to next tier.",
                    tier_name,
                )

            
                trace.append(tier_name + ": invalid JSON")
                break

            # Verify every field's evidence against the original source_text.
            values, evidence = verify_extraction(parsed_json, fields, source_text)
            trace.append(tier_name + ": success")
            return values, evidence, tier_name, trace

    # Return all values as None and provider "none" when every tier fails.
    logger.error("All LLM tiers failed to extract JSON.")
    return build_empty_values(fields), {}, "none", trace


# Run 3 tests when executing this module directly.
async def main():
    # Define the fields used across all 3 tests.
    test_fields = {
        "product_name": "Name of the product",
        "pricing_model": "Pricing model: FREE, FREEMIUM, PAID, ENTERPRISE, or null",
    }

    # Sample texts for tests 1, 2, and 3.
    pricing_text = (
        "Acme Notes is a note taking app. "
        "It is free to start, with paid plans from 20 dollars a month."
    )
    no_pricing_text = (
        "Acme Notes is a note taking app built for teams to organize meeting notes "
        "and share project updates in one place."
    )

    # Open a shared aiohttp session for all 3 tests.
    async with aiohttp.ClientSession() as session:
        # Test 1: Normal extraction on text with pricing info.
        values_1, evidence_1, provider_1, trace_1 = await extract_json(
            session,
            pricing_text,
            test_fields,
            fake_first_key=False,
        )
        logger.info(
            "Test 1 | provider=%s | values=%s | trace=%s",
            provider_1,
            values_1,
            trace_1,
        )

        # Test 2: Same text with fake_first_key=True to verify tier fallback.
        values_2, evidence_2, provider_2, trace_2 = await extract_json(
            session,
            pricing_text,
            test_fields,
            fake_first_key=True,
        )
        logger.info(
            "Test 2 | provider=%s | values=%s | trace=%s",
            provider_2,
            values_2,
            trace_2,
        )

        # Test 3: Text with no pricing information (pricing_model must be None).
        values_3, evidence_3, provider_3, trace_3 = await extract_json(
            session,
            no_pricing_text,
            test_fields,
            fake_first_key=False,
        )
        logger.info(
            "Test 3 | provider=%s | values=%s | trace=%s",
            provider_3,
            values_3,
            trace_3,
        )


# Run the async test suite when executed directly.
if __name__ == "__main__":
    asyncio.run(main())