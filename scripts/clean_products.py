import json

# Load the products file
with open("output/products.json", encoding="utf-8") as f:
    records = json.load(f)

# Drop records whose source link has a question mark, because the link was built wrongly
kept = []
removed = 0
for record in records:
    if "?" in record["source"]["url"]:
        removed = removed + 1
    else:
        kept.append(record)

# Save the file back
with open("output/products.json", "w", encoding="utf-8") as f:
    json.dump(kept, f, indent=2, ensure_ascii=False)

print("Removed:", removed, "Kept:", len(kept))