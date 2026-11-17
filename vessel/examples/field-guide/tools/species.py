# Prints every species page's frontmatter as JSON, optionally filtered by habitat; reads the request from stdin.
import json
import pathlib
import sys

request = json.load(sys.stdin)
wanted = (request.get("habitat") or "").lower()
rows = []
for page in sorted(pathlib.Path("species").glob("*.md")):
    head = page.read_text().split("---")[1]
    fields = dict(line.split(": ", 1) for line in head.strip().splitlines())
    habitats = [h.strip() for h in fields["habitat"].strip("[]").split(",")]
    if wanted and wanted not in habitats:
        continue
    rows.append({"page": page.name, "name": fields["name"], "family": fields["family"],
                 "habitat": habitats, "length_cm": int(fields["length_cm"])})
print(json.dumps(rows, indent=1))
