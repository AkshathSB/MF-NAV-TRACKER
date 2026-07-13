import requests

resp = requests.get("https://www.amfiindia.com/spages/NAVAll.txt", timeout=30)

categories = set()
for line in resp.text.splitlines():
    line = line.strip()
    if "Schemes(" in line and line.endswith(")"):
        start = line.index("(") + 1
        categories.add(line[start:-1].strip())

for cat in sorted(categories):
    print(cat)