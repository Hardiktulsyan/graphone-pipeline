import urllib.request
import xml.etree.ElementTree as ET

# Candidate AI news feeds to test
feeds = {
    "Ars Technica AI": "https://arstechnica.com/ai/feed/",
    "Wired AI": "https://www.wired.com/feed/tag/ai/latest/rss",
    "MarkTechPost": "https://www.marktechpost.com/feed/",
    "Guardian AI": "https://www.theguardian.com/technology/artificialintelligenceai/rss",
    "OpenAI news": "https://openai.com/news/rss.xml",
}

# Try each feed and print how many items it has
for name in feeds:
    url = feeds[name]
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "graphone-pipeline-trial/1.0"})
        text = urllib.request.urlopen(request, timeout=20).read()
        root = ET.fromstring(text)
        items = root.findall(".//item")
        if len(items) == 0:
            items = root.findall(".//{http://www.w3.org/2005/Atom}entry")
        print(name, "OK, items:", len(items))
    except Exception as error:
        print(name, "FAILED:", error)