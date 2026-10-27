"""Check every todo/blurbs_NN.json and merge them into data/blurbs.json.
Run from the repo root: python3 tools/blurbs/merge.py"""
import glob, json, re, sys
facts = {}
for f in glob.glob("tools/blurbs/todo/facts_*.json"):
    for e in json.load(open(f)):
        facts[e["t"]] = e
new = {}
for f in sorted(glob.glob("tools/blurbs/todo/blurbs_*.json")):
    new.update(json.load(open(f)))
bad = []
for t, s in new.items():
    words = len(s.split())
    if t not in facts or "—" in s or "–" in s or not 28 <= words <= 58 or re.search(r"tracks? the", s, re.I):
        bad.append((t, words, s))
for b in bad:
    print("FIX:", *b)
missing = sorted(set(facts) - set(new))
print(f"{len(new)} written, {len(missing)} still missing, {len(bad)} to fix")
if bad:
    sys.exit(1)
cur = json.load(open("data/blurbs.json"))
cur.update(new)
json.dump(cur, open("data/blurbs.json", "w"), indent=1, ensure_ascii=False, sort_keys=True)
print("merged into data/blurbs.json")
