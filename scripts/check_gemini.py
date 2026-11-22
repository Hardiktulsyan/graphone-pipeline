import json
import os
import sys
import urllib.error
import urllib.request

# Let Python find the src folder
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import GEMINI_API_KEY

BASE = "https://generativelanguage.googleapis.com/v1beta/"

# Models to test with a real request
to_test = ["gemini-3.5-flash", "gemini-3.1-flash-lite", "gemini-flash-lite-latest", "gemini-3.6-flash", "gemini-2.5-flash"]


# Send one request and return the status and the first 300 characters of the reply
def call(url, body=None):
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    headers = {"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"}
    request = urllib.request.Request(url, data=data, headers=headers)
    try:
        response = urllib.request.urlopen(request, timeout=40)
        return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8")[:300]
    except Exception as error:
        return "error", str(error)


# Part 1: list the models this key can use
status, text = call(BASE + "models?pageSize=100")
print("List status:", status)
if status == 200:
    print("Flash models you can use:")
    for model in json.loads(text)["models"]:
        if "generateContent" in model.get("supportedGenerationMethods", []):
            name = model["name"].replace("models/", "")
            if "flash" in name:
                print("  ", name)

# Part 2: send one small request to each model in the test list
body = {
    "contents": [{"parts": [{"text": 'Return only this JSON: {"ok": true}'}]}],
    "generationConfig": {"responseMimeType": "application/json", "temperature": 0},
}
for name in to_test:
    status, text = call(BASE + "models/" + name + ":generateContent", body)
    print("---", name, "status:", status)
    print(text[:300])