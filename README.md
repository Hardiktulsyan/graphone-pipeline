# graphone-pipeline

A trial data pipeline for the GraphOne AI Engineer demo task. It is written in Python 3.12 using `asyncio` and `aiohttp`, and everything runs on free tiers.

## Architecture Overview

### Data Sources

- **Research papers**: Uses the Hugging Face daily papers API to find papers with GitHub links, the arXiv API for title, authors, and date, and the GitHub API for current star counts.
- **Startups**: Uses Y Combinator open data from `yc-oss.github.io/api`, filtered by AI-related tags, `Active` status, and a website.
- **Products**: Fetches each startup's homepage and `/pricing` page. Pricing is decided by keyword rules with matching evidence text. The LLM is called only when the page is unclear, limited to 300 calls, and its answer is kept only if the quoted evidence is found in the page text and supports the label. Otherwise pricing stays `null`.
- **News**: Reads 5 RSS feeds (TechCrunch AI, Ars Technica AI, MIT Technology Review AI, The Verge AI, and Hugging Face blog) and fetches full article text. Articles are kept only if published in the last 24 hours with a real time of day.
- **Jobs**: Collects postings from RemoteOK, Remotive, We Work Remotely, Greenhouse boards (`anthropic`, `databricks`, `scaleai`, `togetherai`, `figureai`, `stabilityai`, `labelbox`, `assemblyai`), and Lever (`palantir`). Postings are kept only if posted in the last 24 hours.

### Components in `src/`

- `src/utils/retry.py`: HTTP GET requests with exponential backoff and jitter, `Retry-After` support, and no retries on 4xx responses.
- `src/utils/chunking.py`: Text truncation to stay under payload limits.
- `src/utils/dates.py`: Relative and absolute date parsing.
- `src/llm/orchestrator.py`: Fallback chain in order: `gemini-3.5-flash-lite`, `gemini-3.1-flash-lite`, Groq, and DeepSeek. Retries 429 and 5xx responses with backoff and jitter. On 413 or context errors, cuts the text in half and retries once. Skips any tier with an empty API key. Every extracted field must have evidence found in the source text.
- `src/resolver/entity_resolver.py`: Uses 50 seed startups and resolves names in order: normalize, exact match, no-space match, and fuzzy match at 0.92. Logs every call for the Entity Mapping Log.
- `src/storage/seen_store.py`: Stores a SQLite hash of each normalized URL; used by the news scraper.
- `src/validate.py`: Runs schema, duplicate, and minimum-count checks.

### Anti-Bot Position

The pipeline never tries to bypass blocks. When a site returns 403 or 429, the response is logged and the page is skipped. Handling heavy JavaScript and Cloudflare sites is covered in `architecture.pdf` as a design only and is not implemented.

## Setup

1. Create a virtual environment:

```bash
python -m venv venv