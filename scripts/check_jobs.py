import json
import re
import urllib.request

HEADERS = {"User-Agent": "graphone-pipeline-trial/1.0"}


# Download a URL and return its text, or None if it fails
def get(url):
    request = urllib.request.Request(url, headers=HEADERS)
    try:
        return urllib.request.urlopen(request, timeout=30).read().decode("utf-8")
    except Exception:
        return None


# Part 1: show the 3 newest raw dates from each non-board source
print("PART 1: newest raw dates")

text = get("https://remoteok.com/api")
if text:
    jobs = json.loads(text)[1:4]
    for job in jobs:
        print("RemoteOK:", job.get("date"), job.get("epoch"))

text = get("https://remotive.com/api/remote-jobs?search=AI")
if text:
    jobs = json.loads(text)["jobs"][:3]
    for job in jobs:
        print("Remotive:", job.get("publication_date"))

text = get("https://weworkremotely.com/categories/remote-programming-jobs.rss")
if text:
    for date in re.findall(r"<pubDate>(.*?)</pubDate>", text)[:3]:
        print("We Work Remotely:", date)

# Part 2: test Greenhouse board names and print how many jobs each has
print("PART 2: Greenhouse boards")
greenhouse = ["runwayml", "cerebrassystems", "figureai", "deepmind", "stabilityai",
              "cohere", "perplexityai", "openai", "pinecone", "anyscale",
              "labelbox", "datarobot", "wayve", "assemblyai", "deepgram",
              "sambanova", "groq", "samsara", "scaleai", "databricks"]
for name in greenhouse:
    text = get("https://boards-api.greenhouse.io/v1/boards/" + name + "/jobs")
    if text:
        print(name, len(json.loads(text)["jobs"]))

# Part 3: test Lever board names
print("PART 3: Lever boards")
lever = ["mistral", "palantir", "cohere", "weaviate", "anyscale", "sakana",
         "wayve", "jasper", "spotify", "binance"]
for name in lever:
    text = get("https://api.lever.co/v0/postings/" + name + "?mode=json")
    if text:
        print(name, len(json.loads(text)))