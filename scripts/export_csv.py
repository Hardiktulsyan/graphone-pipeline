import csv
import json
import os

OUTPUT_FOLDER = "output"
SHEETS_FOLDER = os.path.join("output", "sheets")

# Google Sheets allows at most 50,000 characters in one cell
MAX_CELL = 45000

# Each tab: the JSON file, the tab name, and its columns as (header, path of keys)
TABS = [
    ("startups.json", "Startups", [
        ("schemaVersion", ["schemaVersion"]),
        ("recordType", ["recordType"]),
        ("source_name", ["source", "name"]),
        ("source_url", ["source", "url"]),
        ("entityName", ["content", "entityName"]),
        ("employeeCount", ["content", "data", "employeeCount"]),
        ("website", ["content", "data", "website"]),
        ("collectedAt", ["collectedAt"]),
    ]),
    ("products.json", "Products", [
        ("schemaVersion", ["schemaVersion"]),
        ("recordType", ["recordType"]),
        ("source_name", ["source", "name"]),
        ("source_url", ["source", "url"]),
        ("startupName", ["content", "startupName"]),
        ("pricingModel", ["content", "pricingModel"]),
        ("pricingEvidence", ["content", "pricingEvidence"]),
        ("pricingMethod", ["content", "pricingMethod"]),
        ("collectedAt", ["collectedAt"]),
    ]),
    ("papers.json", "Research Papers", [
        ("schemaVersion", ["schemaVersion"]),
        ("recordType", ["recordType"]),
        ("source_name", ["source", "name"]),
        ("source_url", ["source", "url"]),
        ("title", ["content", "title"]),
        ("authors", ["content", "authors"]),
        ("paper_url", ["content", "paper_url"]),
        ("github_url", ["content", "github_url"]),
        ("github_stars", ["content", "github_stars"]),
        ("published_date", ["content", "published_date"]),
        ("collectedAt", ["collectedAt"]),
    ]),
    ("jobs.json", "Jobs", [
        ("schemaVersion", ["schemaVersion"]),
        ("recordType", ["recordType"]),
        ("source_name", ["source", "name"]),
        ("source_url", ["source", "url"]),
        ("title", ["content", "title"]),
        ("company", ["content", "company"]),
        ("date", ["content", "date"]),
        ("is_remote", ["content", "is_remote"]),
        ("role_family", ["content", "role_family"]),
        ("collectedAt", ["collectedAt"]),
    ]),
    ("news.json", "News", [
        ("schemaVersion", ["schemaVersion"]),
        ("recordType", ["recordType"]),
        ("source_name", ["source", "name"]),
        ("source_url", ["source", "url"]),
        ("title", ["content", "title"]),
        ("published_date", ["content", "published_date"]),
        ("full_text", ["content", "full_text"]),
        ("collectedAt", ["collectedAt"]),
    ]),
]

# The entity mapping log is built from these files
LOG_FILES = ["entity_log_startups.json", "entity_log_jobs.json"]


# Read a JSON file and return its list, or an empty list if it is missing
def load_list(file_name):
    path = os.path.join(OUTPUT_FOLDER, file_name)
    if not os.path.exists(path):
        print("Missing file:", file_name)
        return []
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# Follow a path of keys into a record and return a clean text value
def get_value(record, keys):
    value = record
    for key in keys:
        if isinstance(value, dict) and key in value:
            value = value[key]
        else:
            return ""
    if value is None:
        return ""
    if isinstance(value, list):
        return "; ".join(str(item) for item in value)
    text = str(value).replace("\n", " ").replace("\r", " ")
    return text[:MAX_CELL]


# Write one CSV file
def write_csv(tab_name, headers, rows):
    path = os.path.join(SHEETS_FOLDER, tab_name + ".csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        for row in rows:
            writer.writerow(row)
    print(tab_name + ".csv:", len(rows), "rows")


# Build the CSV for a normal tab from its JSON records
def export_tab(file_name, tab_name, columns):
    records = load_list(file_name)
    headers = [column[0] for column in columns]
    rows = []
    for record in records:
        row = [get_value(record, column[1]) for column in columns]
        rows.append(row)
    write_csv(tab_name, headers, rows)


# Build the Entity Mapping Log CSV from the resolver logs
def export_entity_log():
    headers = ["raw", "canonical", "matched", "method", "score", "from_file"]
    rows = []
    for file_name in LOG_FILES:
        for entry in load_list(file_name):
            row = [
                get_value(entry, ["raw"]),
                get_value(entry, ["canonical"]),
                get_value(entry, ["matched"]),
                get_value(entry, ["method"]),
                get_value(entry, ["score"]),
                file_name,
            ]
            rows.append(row)
    write_csv("Entity Mapping Log", headers, rows)


# Create the output folder, then export every tab
def main():
    os.makedirs(SHEETS_FOLDER, exist_ok=True)
    for file_name, tab_name, columns in TABS:
        export_tab(file_name, tab_name, columns)
    export_entity_log()
    print("Done. Files are in", SHEETS_FOLDER)


if __name__ == "__main__":
    main()