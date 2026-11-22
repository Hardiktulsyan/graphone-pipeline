# Standard library import for reading environment variables.
import os

# Third-party import for loading variables from a .env file.
from dotenv import load_dotenv

# Load environment variables from the .env file in the project root.
load_dotenv()


# Read an environment variable and return None if it is missing or empty.
def get_env_value(name: str):
    # Fetch the raw value from the environment.
    value = os.getenv(name)

    # Return None when the variable is not set or only contains spaces.
    if value is None:
        return None
    if value.strip() == "":
        return None

    # Return the cleaned string value.
    return value.strip()


# API keys and tokens loaded from the .env file.
GEMINI_API_KEY = get_env_value("GEMINI_API_KEY")
GROQ_API_KEY = get_env_value("GROQ_API_KEY")
DEEPSEEK_API_KEY = get_env_value("DEEPSEEK_API_KEY")
GITHUB_TOKEN = get_env_value("GITHUB_TOKEN")

# Pipeline concurrency, retry, and timeout constants.
MAX_CONCURRENCY = 10
MAX_RETRIES = 5
REQUEST_TIMEOUT = 30