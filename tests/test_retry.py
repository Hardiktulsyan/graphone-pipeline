import asyncio
import os
import sys

# Let Python find the src folder
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aiohttp
from src.utils.retry import fetch_with_retry


# Run three tests and print what each one returns
async def main():
    async with aiohttp.ClientSession() as session:

        # Test 1: a working URL, should return data
        print("TEST 1: working URL")
        result = await fetch_with_retry(session, "https://httpbin.org/json")
        print("Result type:", type(result))

        # Test 2: a URL that always answers 429, should retry then return None
        print("TEST 2: always 429")
        result = await fetch_with_retry(session, "https://httpbin.org/status/429")
        print("Result:", result)

        # Test 3: a website that does not exist, should return None
        print("TEST 3: bad domain")
        result = await fetch_with_retry(session, "https://this-site-does-not-exist-12345.com")
        print("Result:", result)


asyncio.run(main())