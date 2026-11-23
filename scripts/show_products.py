import json

# Load the products file
with open("output/products.json", encoding="utf-8") as f:
    records = json.load(f)

# Print the key fields of each record on one line
for record in records:
    content = record["content"]
    print(content["startupName"], "|", content["pricingModel"], "|", content["pricingMethod"], "|", content["pricingEvidence"], "|", record["source"]["url"])