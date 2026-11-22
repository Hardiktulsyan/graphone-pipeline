import json

# Load the papers file
with open("output/papers.json", encoding="utf-8") as f:
    records = json.load(f)

# Rebuild source.url for each record from the arXiv id in paper_url
fixed = 0
for record in records:
    paper_url = record["content"]["paper_url"]
    arxiv_id = paper_url.split("/abs/")[-1]
    new_url = "https://huggingface.co/papers/" + arxiv_id
    if record["source"]["url"] != new_url:
        record["source"]["url"] = new_url
        fixed += 1

# Save the file back
with open("output/papers.json", "w", encoding="utf-8") as f:
    json.dump(records, f, indent=2, ensure_ascii=False)

print("Fixed:", fixed, "Total:", len(records))