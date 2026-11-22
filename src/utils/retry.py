import asyncio
import json
import random
import aiohttp

from src.config import MAX_RETRIES, MAX_CONCURRENCY
from src.utils.logger import get_logger

logger = get_logger("retry")

# Define the HTTP status codes that should trigger a retry.
RETRY_STATUS_CODES = {429, 500, 502, 503, 504}

# Create a single global semaphore to limit concurrent requests.
SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENCY)


# Helper function to calculate how many seconds to wait before the next retry.
def _get_wait_time(attempt, retry_after_header):
    # Check if the server provided a valid Retry-After header.
    if retry_after_header is not None:
        try:
            wait_seconds = float(retry_after_header)
            if wait_seconds >= 0:
                return wait_seconds
        except ValueError:
            pass

    # Calculate exponential backoff starting at 2 seconds, plus random jitter.
    base_delay = 2 * (2 ** attempt)
    jitter = random.uniform(0.0, 1.0)
    return base_delay + jitter


# Helper function to parse the response body as JSON if possible, otherwise text.
async def _parse_response(response):
    # Read the raw text body from the response.
    text_body = await response.text()
    content_type = response.headers.get("Content-Type", "").lower()

    # Check if the header indicates JSON or try parsing the text directly.
    if "application/json" in content_type:
        try:
            return json.loads(text_body)
        except json.JSONDecodeError:
            return text_body

    # Return plain text for non-JSON responses.
    return text_body


# Fetch a URL with concurrency limits, exponential backoff retries, and logging.
async def fetch_with_retry(session, url, headers=None, params=None):
    # Acquire the semaphore so only MAX_CONCURRENCY requests run at once.
    async with SEMAPHORE:
        # Loop through the initial attempt plus up to MAX_RETRIES retries.
        for attempt in range(MAX_RETRIES + 1):
            status_code = None
            retry_after = None

            # Perform the HTTP GET request and handle timeouts or client errors.
            try:
                async with session.get(url, headers=headers, params=params) as response:
                    status_code = response.status

                    # If the request succeeded, parse and return the response body.
                    if status_code == 200:
                        return await _parse_response(response)

                    # Check if the status code is in the retryable list.
                    if status_code in RETRY_STATUS_CODES:
                        retry_after = response.headers.get("Retry-After")
                    else:
                        # Log non-retryable HTTP errors and stop immediately.
                        logger.error(
                            "Request failed with non-retryable status %s for url: %s",
                            status_code,
                            url,
                        )
                        return None

            except (asyncio.TimeoutError, aiohttp.ClientError) as exc:
                # Record that a timeout or network error occurred.
                status_code = type(exc).__name__

            # Check if we still have retry attempts remaining.
            if attempt < MAX_RETRIES:
                wait_time = _get_wait_time(attempt, retry_after)
                logger.warning(
                    "Retrying url: %s | status: %s | wait: %.2fs (attempt %s of %s)",
                    url,
                    status_code,
                    wait_time,
                    attempt + 1,
                    MAX_RETRIES,
                )
                await asyncio.sleep(wait_time)
            else:
                # Log the final failure after all retries are exhausted.
                logger.error(
                    "All %s retries exhausted for url: %s | last status: %s",
                    MAX_RETRIES,
                    url,
                    status_code,
                )

        # Return None if all attempts failed.
        return None