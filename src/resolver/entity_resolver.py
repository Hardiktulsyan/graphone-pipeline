# Standard library imports for fuzzy matching, JSON loading, file paths, and punctuation.
import difflib
import json
import os
import string

# Project logger helper.
from src.utils.logger import get_logger

# Create a module-level logger.
logger = get_logger(__name__)

# Common legal suffix words removed during name normalization.
SUFFIX_WORDS = {
    "inc",
    "ltd",
    "llc",
    "corp",
    "corporation",
    "co",
    "limited",
}

# Minimum similarity score required for a fuzzy match.
FUZZY_THRESHOLD = 0.92


# Resolves raw startup names to canonical names using a local seed list.
class EntityResolver:
    # Initialize the resolver, load the seed file, and build lookup tables.
    def __init__(self, seed_filename: str = "seed_startups.json"):
        # Build a path to the seed file relative to this Python file.
        current_dir = os.path.dirname(os.path.abspath(__file__))
        self.seed_path = os.path.join(current_dir, seed_filename)

        # Lookup dictionaries for exact matches and space-removed matches.
        self.exact_map = {}
        self.nospace_map = {}

        # History list storing a record for every resolve() call.
        self.log = []

        # Load the seed startups into the lookup tables.
        self._load_seed_data()

    # Read the seed JSON file and populate exact and no-space lookup maps.
    def _load_seed_data(self):
        # Open and parse the JSON seed file.
        with open(self.seed_path, "r", encoding="utf-8") as file:
            entries = json.load(file)

        # Register each canonical name and all of its aliases.
        for entry in entries:
            canonical = entry.get("canonical")
            if canonical is None:
                continue

            # Add the canonical name itself to both lookup maps.
            self._register_name(canonical, canonical)

            # Add every alias listed for this startup to both lookup maps.
            aliases = entry.get("aliases")
            if aliases is not None:
                for alias in aliases:
                    self._register_name(alias, canonical)

        # Log how many startup entries and lookup keys were loaded.
        logger.info(
            "Loaded %d seed startups (%d normalized keys).",
            len(entries),
            len(self.exact_map),
        )

    # Store one raw or alias variant in both the exact and no-space lookup maps.
    def _register_name(self, variant: str, canonical: str):
        # Normalize the variant before storing it.
        normalized = self.normalize(variant)
        if normalized == "":
            return

        # Store the normalized form in the exact lookup map.
        if normalized not in self.exact_map:
            self.exact_map[normalized] = canonical

        # Store the space-free form in the no-space lookup map.
        no_space = normalized.replace(" ", "")
        if no_space != "" and no_space not in self.nospace_map:
            self.nospace_map[no_space] = canonical

    # Clean whitespace on an original name without changing its casing.
    def _clean_original(self, raw_name: str) -> str:
        # Split on any whitespace and rejoin with single spaces.
        parts = raw_name.strip().split()
        return " ".join(parts)

    # Step 1: Lowercase a name, remove punctuation, and remove legal suffix words.
    def normalize(self, raw_name: str) -> str:
        # Return an empty string if the input is None.
        if raw_name is None:
            return ""

        # Convert to lowercase.
        text = raw_name.lower()

        # Replace every punctuation character with a space.
        cleaned_chars = []
        for ch in text:
            if ch in string.punctuation:
                cleaned_chars.append(" ")
            else:
                cleaned_chars.append(ch)
        without_punctuation = "".join(cleaned_chars)

        # Split into words and drop common legal suffix words.
        words = without_punctuation.split()
        filtered_words = []
        for word in words:
            if word not in SUFFIX_WORDS:
                filtered_words.append(word)

        # Join the remaining words with single spaces.
        return " ".join(filtered_words)

    # Record a resolution attempt in self.log.
    def _record_log(self, raw, canonical, matched: bool, method: str, score: float):
        # Create a dictionary entry for this resolution call.
        row = {
            "raw": raw,
            "canonical": canonical,
            "matched": matched,
            "method": method,
            "score": score,
        }

        # Append the entry to the internal log list.
        self.log.append(row)

    # Resolve a raw startup name to its canonical name using the 5-step pipeline.
    def resolve(self, raw_name):
        # Return None if the input is missing or blank.
        if raw_name is None:
            self._record_log(None, None, False, "empty", 0.0)
            return None

        cleaned_original = self._clean_original(raw_name)
        if cleaned_original == "":
            self._record_log(raw_name, None, False, "empty", 0.0)
            return None

        # Step 1: Normalize the incoming name.
        normalized = self.normalize(cleaned_original)

        # Step 2: Check for an exact match against normalized canonical names and aliases.
        if normalized in self.exact_map:
            canonical = self.exact_map[normalized]
            self._record_log(raw_name, canonical, True, "exact", 1.0)
            return canonical

        # Step 3: Check for a match with all spaces removed.
        no_space = normalized.replace(" ", "")
        if no_space in self.nospace_map:
            canonical = self.nospace_map[no_space]
            self._record_log(raw_name, canonical, True, "no_space", 1.0)
            return canonical

        # Step 4: Check for a fuzzy match using difflib at 0.92 or higher.
        best_key = None
        best_score = 0.0
        for candidate_key in self.exact_map:
            matcher = difflib.SequenceMatcher(None, normalized, candidate_key)
            score = matcher.ratio()
            if score > best_score:
                best_score = score
                best_key = candidate_key

        if best_key is not None and best_score >= FUZZY_THRESHOLD:
            canonical = self.exact_map[best_key]
            self._record_log(raw_name, canonical, True, "fuzzy", round(best_score, 4))
            return canonical

        # Step 5: Return the cleaned original name when nothing matches.
        self._record_log(
            raw_name,
            cleaned_original,
            False,
            "unmatched",
            round(best_score, 4),
        )
        return cleaned_original

    # Return the full list of resolution log rows.
    def get_log(self) -> list:
        return self.log


# Run 20 test cases when executing this module directly.
if __name__ == "__main__":
    # Create an EntityResolver instance.
    resolver = EntityResolver()

    # Define 20 test cases covering exact, suffix, no-space, fuzzy, and unmatched names.
    test_names = [
        "OpenAI",
        "OpenAI Inc",
        "Open AI",
        "OpenAI, LLC",
        "Anthropic PBC",
        "Mistral AI",
        "HuggingFace",
        "Eleven Labs",
        "PerplexityAI",
        "Scale AI, Inc.",
        "Databrick",
        "Cerebras System",
        "Safe Superinteligence",
        "Weights & Biases",
        "Character.AI",
        "DeepL Limited",
        "Together AI Corp",
        "Acme Robotics",
        "Northstar Biotech",
        "Bluebell Bakery",
    ]

    # Resolve each test name and log raw, canonical, and method.
    for name in test_names:
        canonical_name = resolver.resolve(name)
        last_entry = resolver.get_log()[-1]
        logger.info(
            "raw=%s | canonical=%s | method=%s",
            last_entry["raw"],
            canonical_name,
            last_entry["method"],
        )