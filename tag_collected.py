"""Tag collected editions (trade paperbacks, omnibuses, "Volume 3" files) with Comic Vine, by ID.

comictagger_runner.py handles numbered single issues in folders pinned to a series. A file like
"Thunderbolts Classic Vol. 03 (2012) (digital) (Minutemen-Slayer).cbr" has no issue number for ComicTagger to search with, so it never
matches. This tool does the matching itself: it reads the series name, volume number and year out of the file name, finds the Comic Vine
volume with exactly that name (nearest start year when several share it), picks the issue that is that volume number, and then has
ComicTagger write that issue's tags by ID. Anything it is not sure about (no match, several equally good volumes, no volume number
for a multi-issue volume) is listed and left alone.

Usage:  python tag_collected.py DIR [DIR ...] [--apply] [--max 20] [--pause 2] [--log tag_collected_log.jsonl]
Preview by default. Needs a Comic Vine key (COMICVINE_KEY, or ComicTagger's own settings) and ComicTagger on PATH (or --comictagger).
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path

import komga_title_fix
import tagcommon

EXTENSIONS = {".cbz", ".cbr"}
VOLUME = re.compile(r"(?<![A-Za-z])(?:volume|vol\.?|v)\s*0*(\d+)(?![A-Za-z])", re.I)
YEAR = re.compile(r"[\(\[]\s*((?:19|20)\d{2})(?:[^\)\]]*)[\)\]]")
YEAR_WINDOW = 3          # a collection's Comic Vine start year can be a year or two before the file's release year


def norm(text):
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower().replace("&", " and ")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def parse_name(filename):
    """File name -> {names: [candidate volume names, best first], volume: int | None, year: int | None}."""
    stem = Path(filename).stem
    year = YEAR.search(stem)
    year = int(year.group(1)) if year else None
    stem = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", stem)
    stem = " ".join(stem.split())
    m = VOLUME.search(stem)
    if not m:
        return {"names": [stem] if stem else [], "volume": None, "year": year}
    title = stem[:m.start()].strip(" -_.,")
    subtitle = stem[m.end():].strip(" -_.,")
    names = [f"{title} {subtitle}".strip()] if subtitle else []
    names.append(title)
    return {"names": [n for n in names if n], "volume": int(m.group(1)), "year": year}


def choose_volume(parsed, candidates):
    """candidates: [{id, name, start_year}] from a Comic Vine search. Returns (volume, why-not)."""
    for name in parsed["names"]:
        exact = [c for c in candidates if norm(c["name"]) == norm(name)]
        if not exact:
            continue
        if parsed["year"]:
            near = [c for c in exact if c.get("start_year") and abs(int(c["start_year"]) - parsed["year"]) <= YEAR_WINDOW]
            if len(near) == 1:
                return near[0], None
            if len(near) > 1:
                best = sorted(near, key=lambda c: abs(int(c["start_year"]) - parsed["year"]))
                gap = [abs(int(c["start_year"]) - parsed["year"]) for c in best]
                return (best[0], None) if gap[0] < gap[1] else (None, f"{len(near)} volumes named {name!r} are equally close to {parsed['year']}")
            return None, f"no volume named {name!r} starts within {YEAR_WINDOW} years of {parsed['year']}"
        return (exact[0], None) if len(exact) == 1 else (None, f"{len(exact)} volumes are named {name!r} and the file has no year")
    return None, "no Comic Vine volume has that name"


def choose_issue(parsed, issues):
    """issues: [{id, issue_number}] of the chosen volume."""
    if parsed["volume"] is not None:
        hit = [i for i in issues if str(i.get("issue_number")).strip() == str(parsed["volume"])]
        return (hit[0], None) if len(hit) == 1 else (None, f"the volume has no issue #{parsed['volume']}")
    return (issues[0], None) if len(issues) == 1 else (None, f"the volume has {len(issues)} issues and the file name gives no volume number")


def _get(cv_key, path, **params):
    q = urllib.parse.urlencode({"api_key": cv_key, "format": "json", **params})
    req = urllib.request.Request(f"https://comicvine.gamespot.com/api/{path}?{q}", headers={"User-Agent": "komga-comic-tools"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)["results"]


def search_volumes(cv_key, name):
    return _get(cv_key, "search/", query=name, resources="volume", limit=10, field_list="id,name,start_year")


def volume_issues(cv_key, volume_id):
    return _get(cv_key, "issues/", filter=f"volume:{volume_id}", limit=100, field_list="id,issue_number,name")


def find_files(dirs):
    for d in dirs:
        for p in sorted(Path(d).rglob("*")):
            if p.suffix.lower() in EXTENSIONS and not p.name.startswith("."):
                yield p


def already_tagged(path):
    try:
        return bool(tagcommon.read_tags(path).get("Count"))
    except Exception:                                                              # noqa: BLE001
        return False


def match(cv_key, path, pause=1.5, sleep=time.sleep):
    """-> dict(file, status 'match'|'skip', plus volume/issue/why)."""
    parsed = parse_name(path.name)
    out = {"file": str(path), "parsed": parsed}
    if not parsed["names"]:
        return {**out, "status": "skip", "why": "could not read a title from the file name"}
    seen, candidates = set(), []
    for name in parsed["names"]:
        for c in search_volumes(cv_key, name):
            if c["id"] not in seen:
                seen.add(c["id"])
                candidates.append(c)
        sleep(pause)
    volume, why = choose_volume(parsed, candidates)
    if not volume:
        return {**out, "status": "skip", "why": why}
    issues = volume_issues(cv_key, volume["id"])
    sleep(pause)
    issue, why = choose_issue(parsed, issues)
    if not issue:
        return {**out, "status": "skip", "why": why, "volume": volume}
    return {**out, "status": "match", "volume": volume, "issue": issue}


def tag(exe, path, issue_id, timeout=300):
    """Have ComicTagger write the Comic Vine tags of that issue. True when the file now carries them."""
    cmd = [exe, "-s", "-o", "--id", str(issue_id), "--tags-write", "CR", "--no-gui", "--no-prompt-on-save", "--no-abort", str(path)]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except subprocess.TimeoutExpired:
        return False
    return "Save complete" in out and already_tagged(path)


def run(dirs, cv_key, exe="comictagger", apply=False, max_files=20, pause=1.5, log=None, say=print, sleep=time.sleep):
    """Returns the number of files tagged (always 0 for a preview)."""
    todo = [p for p in find_files(dirs) if not already_tagged(p)]
    say(f"{len(todo)} file(s) without Comic Vine tags in {len(list(dirs))} folder(s)")
    tagged = 0
    for n, path in enumerate(todo):
        if n >= max_files:
            say(f"  stopped after {max_files}; run again for the rest")
            break
        try:
            r = match(cv_key, path, pause, sleep)
        except Exception as e:                                                     # noqa: BLE001
            r = {"file": str(path), "status": "skip", "why": f"Comic Vine lookup failed: {e}"}
        if r["status"] == "match":
            label = f"{r['volume']['name']} #{r['issue']['issue_number']}  (issue id {r['issue']['id']})"
            if apply:
                ok = tag(exe, path, r["issue"]["id"])
                r["tagged"] = ok
                tagged += ok
                say(f"  {'tagged' if ok else 'TAGGING FAILED'}: {path.name} -> {label}")
            else:
                say(f"  would tag: {path.name} -> {label}")
        else:
            say(f"  left alone: {path.name} ({r['why']})")
        if log:
            with open(log, "a", encoding="utf-8") as f:
                f.write(json.dumps({"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "file": path.name, "status": r["status"],
                                    "tagged": r.get("tagged"), "issue": (r.get("issue") or {}).get("id"), "why": r.get("why")}) + "\n")
    return tagged


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dirs", nargs="+")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--max", type=int, default=20)
    ap.add_argument("--pause", type=float, default=1.5)
    ap.add_argument("--comictagger", default="comictagger")
    ap.add_argument("--comictagger-settings")
    ap.add_argument("--log", default="tag_collected_log.jsonl")
    args = ap.parse_args()
    cv_key = komga_title_fix.comicvine_key(args.comictagger_settings)
    if not cv_key:
        sys.exit("Set COMICVINE_KEY (or --comictagger-settings).")
    run(args.dirs, cv_key, args.comictagger, args.apply, args.max, args.pause, args.log if args.apply else None)


if __name__ == "__main__":
    main()
