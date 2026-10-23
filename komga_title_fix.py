"""Give collected editions a title that says what they are.

Comic Vine names most trade paperbacks and omnibuses just "Volume 3" or "TPB", so a card (or a Komga book list) shows
"Volume Three" with nothing to say which series it belongs to. This tool finds books whose title is that generic, looks up the
Comic Vine volume (series) the book is linked to, and sets the title to "<Series> Vol. <n>" (or just "<Series>" for a one-off
like "TPB"). Single issues with real titles ("Distant Rumblings!") are never touched.

The new title is locked in Komga, so a rescan or re-tag does not undo it. Default is a preview (CSV); add --apply to write.

Authentication: KOMGA_API_KEY (Komga > My Account > API Keys) and a Comic Vine key (COMICVINE_KEY, or --comictagger-settings
pointing at ComicTagger's settings.json). Neither key is written anywhere. Comic Vine allows about 200 requests an hour per key:
the tool pauses between lookups and --max limits how many books are looked up in one run.

Usage:
  python komga_title_fix.py [--library-id ID ...] [--max N] [--preview out.csv] [--apply] [--base-url http://localhost:25600]
"""
import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
         "eleven": 11, "twelve": 12}
# "Volume 3", "Vol. 3", "Volume Three". "Part Three" and "Book Four" are titles of single issues (Dark Knight III), not collections,
# and "Volume 3: Subtitle" already says something, so none of those count.
GENERIC = re.compile(r"^(?:volume|vol\.?|tome)\s*(?:\d+|[ivxlc]+|" + "|".join(WORDS) + r")\.?$"
                     r"|^(?:tpb|hc|hardcover|softcover|omnibus|graphic novel|trade paperback|collection|complete collection)\.?$", re.I)
CV_ISSUE = re.compile(r"comicvine\.gamespot\.com/.*?/4000-(\d+)")


def is_generic(title):
    return bool(GENERIC.match((title or "").strip()))


def new_title(old, volume_name, issue_number):
    """Title for a generic-titled collected edition, or None when there is nothing sensible to say."""
    old, volume_name = (old or "").strip(), (volume_name or "").strip()
    if not volume_name or not is_generic(old):
        return None
    if re.match(r"^(?:volume|vol\.?|tome)\b", old, re.I):
        try:
            n = int(float(issue_number))
        except (TypeError, ValueError):
            return None
        return f"{volume_name} Vol. {n}"
    return volume_name          # "TPB", "Omnibus", "Hardcover"...: the series name says it all


class Komga:
    """The little part of Komga's API this tool needs."""
    def __init__(self, base_url, key):
        self.base, self.key = base_url.rstrip("/") + "/api/v1", key

    def call(self, path, method="GET", body=None):
        req = urllib.request.Request(self.base + path, method=method, headers={"X-API-Key": self.key, "Content-Type": "application/json"},
                                     data=json.dumps(body).encode() if body is not None else None)
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = r.read()
        return json.loads(raw) if raw else None

    def paged(self, path):
        out, page = [], 0
        while True:
            d = self.call(f"{path}{'&' if '?' in path else '?'}size=500&page={page}")
            out += d["content"]
            page += 1
            if page >= d.get("totalPages", 1):
                return out

    def library_ids(self):
        return [l["id"] for l in self.call("/libraries")]

    def scan(self, library_id):
        self.call(f"/libraries/{library_id}/scan", "POST")


def comicvine_issue(cv_key, issue_id):
    q = urllib.parse.urlencode({"api_key": cv_key, "format": "json", "field_list": "name,issue_number,volume"})
    req = urllib.request.Request(f"https://comicvine.gamespot.com/api/issue/4000-{issue_id}/?{q}", headers={"User-Agent": "komga-comic-tools"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)["results"]


def comicvine_key(settings_path=None):
    """COMICVINE_KEY, else the key in ComicTagger's settings.json (the path is given, or the usual per-user location)."""
    key = os.environ.get("COMICVINE_KEY")
    if key:
        return key
    for path in [settings_path, os.path.expandvars(r"%LOCALAPPDATA%\ComicTagger\ComicTagger\settings.json"),
                 os.path.expanduser("~/.local/share/comictagger/settings.json")]:
        if path and os.path.exists(path):
            try:
                return json.load(open(path, encoding="utf-8"))["Source comicvine"]["comicvine_key"]
            except (KeyError, ValueError, OSError):
                continue
    return None


def fix_titles(komga, cv_key, library_ids=None, max_lookups=150, pause=1.5, apply=False, preview=None, say=print):
    """Find generic-titled collected editions and (with apply) retitle and lock them. Returns the list of (book, new title)."""
    libs = library_ids or komga.library_ids()
    books = [b for lib in libs for b in komga.paged(f"/books?library_id={lib}")]
    candidates = []
    for b in books:
        m = b.get("metadata") or {}
        link = next((l["url"] for l in m.get("links", []) if CV_ISSUE.search(l.get("url", ""))), None)
        if link and is_generic(m.get("title")):
            candidates.append((b, CV_ISSUE.search(link).group(1)))
    say(f"{len(books)} books | {len(candidates)} with a generic title and a Comic Vine link")

    rows, changes = [], []
    for n, (b, issue_id) in enumerate(candidates):
        m = b["metadata"]
        if n >= max_lookups:
            rows.append([b["seriesTitle"], b["name"], m.get("title"), "", "skipped (--max reached; run again)"])
            continue
        try:
            cv = comicvine_issue(cv_key, issue_id)
        except Exception as e:                                                    # noqa: BLE001
            rows.append([b["seriesTitle"], b["name"], m.get("title"), "", f"Comic Vine lookup failed: {e}"])
            continue
        time.sleep(pause)
        t = new_title(m.get("title"), (cv.get("volume") or {}).get("name"), cv.get("issue_number"))
        rows.append([b["seriesTitle"], b["name"], m.get("title"), t or "", "" if t else "nothing to change"])
        if t and t != m.get("title"):
            changes.append((b, t))
    if preview:
        with open(preview, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["series", "book", "old title", "new title", "note"])
            w.writerows(rows)
    say(f"{len(changes)} title(s) would change" + (f". Preview: {preview}" if preview else ""))
    if not apply:
        say("preview only - re-run with --apply to write them")
        return changes
    for b, t in changes:
        komga.call(f"/books/{b['id']}/metadata", "PATCH", {"title": t, "titleLock": True})
        check = komga.call(f"/books/{b['id']}")["metadata"]
        assert check["title"] == t and check["titleLock"], f"did not update: {b['name']}"
    say(f"updated {len(changes)} title(s)")
    return changes


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", default="http://localhost:25600")
    ap.add_argument("--library-id", action="append", help="limit to a library (repeatable); default is every library")
    ap.add_argument("--comictagger-settings", help="ComicTagger settings.json to read the Comic Vine key from")
    ap.add_argument("--max", type=int, default=150, help="most Comic Vine lookups in one run (default 150)")
    ap.add_argument("--pause", type=float, default=1.5, help="seconds between Comic Vine lookups")
    ap.add_argument("--preview", default="title_fix_preview.csv")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    key, cv_key = os.environ.get("KOMGA_API_KEY"), comicvine_key(args.comictagger_settings)
    if not key or not cv_key:
        sys.exit("Set KOMGA_API_KEY and COMICVINE_KEY (or --comictagger-settings).")
    fix_titles(Komga(args.base_url, key), cv_key, args.library_id, args.max, args.pause, args.apply, args.preview)


if __name__ == "__main__":
    main()
