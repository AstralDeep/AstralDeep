import urllib.request, json, sys

def fetch_contents(path=""):
    url = f"https://api.github.com/repos/AstralDeep/AstralDeep/contents{path}"
    req = urllib.request.Request(url, headers={"User-Agent": "AstralDeep-Dev/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())
    result = []
    for item in data:
        full_path = f"{path}/{item['name']}" if path else item['name']
        if item['type'] == 'dir':
            result.extend(fetch_contents(full_path))
        else:
            result.append(full_path)
    return result

for f in sorted(fetch_contents()):
    print(f)
