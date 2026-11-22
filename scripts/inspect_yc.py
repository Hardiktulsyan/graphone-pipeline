import json
import urllib.request

# Download the full YC company list
url = "https://yc-oss.github.io/api/companies/all.json"
data = json.load(urllib.request.urlopen(url))

# Print the total and the field names of the first company
print("Total companies:", len(data))
print("Fields:", list(data[0].keys()))

# Count how many companies have each tag that looks AI related
words = ["AI", "Machine Learning", "Artificial", "LLM"]
counts = {}
for company in data:
    tags = company.get("tags", [])
    for tag in tags:
        for word in words:
            if word in tag:
                counts[tag] = counts.get(tag, 0) + 1

for tag in counts:
    print(tag, counts[tag])

# Count companies that have at least one of those tags
ai_total = 0
for company in data:
    found = False
    tags = company.get("tags", [])
    for tag in tags:
        if tag in counts:
            found = True
    if found:
        ai_total += 1
print("Companies with an AI related tag:", ai_total)