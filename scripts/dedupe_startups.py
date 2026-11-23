import json

# Load the startups file
with open("output/startups.json", encoding="utf-8") as f:
    records = json.load(f)

# Keep the first record of each name and drop later ones, ignoring capital letters
seen = set()
kept = []
for record in records:
    key = record["content"]["entityName"].strip().lower()
    if key in seen:
        print("Removed duplicate:", record["content"]["entityName"], record["source"]["url"])
    else:
        seen.add(key)
        kept.append(record)

# Save the file back
with open("output/startups.json", "w", encoding="utf-8") as f:
    json.dump(kept, f, indent=2, ensure_ascii=False)

print("Before:", len(records), "After:", len(kept))