import tagcommon
import re
def _safe_int(x, base=10):
    try:
        if isinstance(x, bool): return int(x)
        if isinstance(x, (int, float)): return int(x)
        if isinstance(x, str): x = re.sub(r"[^\d-]", "", x)
        if not x or x == "-": return 0
        return int(x, base) if isinstance(x, str) else int(x)
    except Exception: return 0

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
from functools import lru_cache
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

import komga_title_fix
import comicvine_store
import tagcommon

EXTENSIONS = {".cbz", ".cbr"}
VOLUME = re.compile(r"(?<![A-Za-z])(?:volume|vol\.?|v)\s*0*(\d+)(?![A-Za-z])", re.I)
YEAR = re.compile(r"[\(\[]\s*((?:19|20)\d{2})(?:[^\)\]]*)[\)\]]")
YEAR_WINDOW = 3          # a collection's Comic Vine start year can be a year or two before the file's release year
ARCHIVE_SERIES_ALIASES = {
    "iron man - director of s.h.i.e.l.d": "Iron Man",
    "lotdk": "Batman: Legends of the Dark Knight",
    "legends of the dark knight": "Batman: Legends of the Dark Knight",
    "batman sotb": "Batman: Shadow of the Bat",
    "sotb": "Batman: Shadow of the Bat",
    "shadow of the bat": "Batman: Shadow of the Bat",
    "ldetective": "Detective Comics",
    "azrael": "Azrael: Agent of the Bat",
    "azarel": "Azrael: Agent of the Bat",
    "detective": "Detective Comics",
    "batman chronicles": "The Batman Chronicles",
}


def norm(text):
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower().replace("&", " and ")
    words = re.sub(r"[^a-z0-9]+", " ", text).split()
    return " ".join(words[1:] if words[:1] == ["the"] and len(words) > 1 else words)


def parse_name(filename, aliases=None):
    """File name -> {names: [candidate volume names, best first], volume: int | None, year: int | None}.

    aliases maps a normalised file title to the Comic Vine volume name it really is ('incredible hulk masterworks' -> 'Marvel Masterworks: The Incredible Hulk')."""
    parsed = _parse_name(filename)
    alias = {norm(k): v for k, v in (aliases or {}).items()}
    extra = [alias[norm(n)] for n in parsed["names"] if norm(n) in alias]
    return {**parsed, "names": extra + parsed["names"]}


def _parse_name(filename):
    stem = Path(filename).stem
    # A few already-converted downloads retain the source extension in the
    # basename (for example, "Issue 001.cbr.cbz").
    stem = re.sub(r"\.cbr$", "", stem, flags=re.I)
    year = YEAR.search(stem)
    year = _safe_int(year.group(1)) if year else None
    stem = re.sub(r"\bGetComics\.INFO\b", " ", stem, flags=re.I)        # site tag left outside the parentheses
    stem = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", stem)
    stem = re.sub(r"\s+\([^)]*$", " ", stem)  # tolerate a truncated release-group parenthesis
    stem = " ".join(stem.split())
    stem = re.sub(r"(?<=\d)\.NOW$", "", stem, flags=re.I)         # "Thor God of Thunder 019.NOW": a Marvel NOW! issue, number 19
    # Some annual scans encode the one-shot's publication year as the trailing
    # number. Preserve it as the year and leave the series name intact.
    annual = re.search(r"\bAnnual\s+((?:19|20)\d{2})$", stem, re.I)
    if annual and not year:
        year = _safe_int(annual.group(1))
        stem = stem[:annual.start(1)].strip(" -_.,")
    # Packs often prefix an ordering number before the actual series/issue. Accept
    # both bare three-digit ordinals ("001 Shadowland 01") and shorter or lettered
    # ordinals separated by a dash ("8 - Batman 502", "00a - Batman 489").
    # Only remove the prefix when the remainder ends in an issue number, so normal
    # titles beginning with a number are not rewritten.
    has_trailing_issue = re.search(r"(?:#|\bno\.?\s*)?-?0*\d{1,4}(?:\.\d+)?(?:\s+[A-F]\s+story)?$", stem, re.I)
    ordinal = re.match(r"^(?:\d{1,3}[A-Za-z]?\s*[-–—]\s*|\d{3}\s+)", stem)
    if ordinal and has_trailing_issue:
        stem = stem[ordinal.end():].strip()
    # In filenames such as "Catwoman v1 007", v1 identifies the series run;
    # the final number is the issue, not a collected-edition volume number.
    series_run = re.search(r"(?<![A-Za-z])v1(?=\s+[-#]?\s*0*\d{1,4}(?:\.\d+)?$)", stem, re.I)
    if series_run:
        stem = (stem[:series_run.start()] + " " + stem[series_run.end():]).strip()
    m = VOLUME.search(stem)
    # Also parse normal comic pack names with a bare trailing issue number. Keep numbers embedded in
    # series names (e.g. Spider-Man 2099) and strip malformed release-group tails above.
    story_m = re.search(r"(?P<num>-?0*\d+(?:\.\d+)?)\s+[A-F]\s+story$", stem, re.I)
    explicit_m = re.search(r"(?:#|\bno\.?\s*|\bissue\s*)(-?0*\d+(?:\.\d+)?)", stem, re.I)
    negative_m = re.search(r"(?P<title>.+?)\s+(?P<num>-0*\d+(?:\.\d+)?)$", stem)
    trailing_m = re.search(r"(?P<title>.+?)[\s_-]+(?P<num>-0*\d+(?:\.\d+)?|0*\d+(?:\.\d+|\.[A-Za-z]{2,4})?)$", stem)
    issue_m = story_m or explicit_m or negative_m or trailing_m
    issue_num = (story_m.group("num") if story_m else explicit_m.group(1) if explicit_m else negative_m.group("num") if negative_m else trailing_m.group("num") if trailing_m else None)
    if not m:
        if negative_m:
            clean_name = negative_m.group("title").strip(" -_.,")
        elif trailing_m:
            clean_name = trailing_m.group("title").strip(" -_.,")
        elif story_m:
            clean_name = stem[:story_m.start("num")].strip(" -_.,")
        elif explicit_m:
            clean_name = re.sub(r"(?:#|\bno\.?\s*|\bissue\s*)-?0*\d+(?:\.\d+)?", "", stem, flags=re.I).strip(" -_.,")
        else:
            clean_name = stem.strip(" -_.,")
        names = [clean_name] if clean_name else []
        return {"names": names, "volume": None, "issue_num": issue_num, "year": year}
    title = stem[:m.start()].strip(" -_.,")
    subtitle = stem[m.end():].strip(" -_.,")
    names = [f"{title} {subtitle}".strip()] if subtitle else []
    names.append(title)
    return {"names": [n for n in names if n], "volume": _safe_int(m.group(1)), "year": year}


def choose_volume(parsed, candidates):
    tgt = parsed.get("volume") if parsed.get("volume") is not None else parsed.get("issue_num")
    try:
        tgt_int = int(float(str(tgt).strip().split(".", 1)[0])) if tgt is not None else 0
    except (TypeError, ValueError):
        tgt_int = 0
    # a first issue goes on sale in the year before its cover date, so its file year may precede its volume's start year by one
    first_issue = parsed.get("volume") is None and parsed.get("issue_num") is not None and 0 <= tgt_int <= 1

    def pub_ok(c):
        pn = ((c.get("publisher") or {}).get("name") or "").lower()
        return 1 if any(x in pn for x in ("marvel", "dc", "epic", "max")) else 0
    for name in parsed["names"]:
        exact = [c for c in candidates if norm(c["name"]) == norm(name)]
        if not exact:
            continue
        # A file's publication year cannot belong to a volume that starts later.
        # Comic Vine often returns count_of_issues=0 from volume search, so a future
        # relaunch could otherwise win the nearest-year tie and reject valid old issues.
        if parsed.get("year"):
            # Exception: a one-issue Comic Vine entry for a named collected volume ("Vol. 3 - Civil War II") is often dated
            # a year after the file's own year (the trade came out later than the scan's label), so give those one year of slack.
            def started(c):
                slack = 1 if parsed.get("volume") is not None and _safe_int(c.get("count_of_issues")) == 1 else 0
                if first_issue:
                    slack = 1               # "Thor God of Thunder 001 (2012)" is the January 2013 issue
                return not c.get("start_year") or _safe_int(c["start_year"]) <= parsed["year"] + slack
            compatible = [c for c in exact if started(c)]
            if not compatible:
                continue
            exact = compatible
        if tgt_int < 0:
            older = [c for c in exact if c.get("start_year") and parsed.get("year") and _safe_int(c["start_year"]) <= parsed["year"]]
            if len(older) == 1:
                return older[0], None
            if older:
                older.sort(key=lambda c: (-_safe_int(c.get("count_of_issues")), _safe_int(c.get("start_year") or 9999)))
                return older[0], None
        if tgt_int > 1:
            # Treat a missing/zero count as unknown; it is not evidence that the
            # volume lacks the requested legacy-numbered issue.
            known_counts = [c for c in exact if _safe_int(c.get("count_of_issues")) > 0]
            enough = [c for c in known_counts if _safe_int(c.get("count_of_issues")) >= tgt_int]
            if enough:
                exact = enough
        if len(exact) == 1:
            return exact[0], None
        if not parsed.get("year"):
            return None, "several same-named Comic Vine volumes and no usable year"
        if parsed["year"]:
            near = [c for c in exact if c.get("start_year") and 0 <= parsed["year"] - _safe_int(c["start_year"]) <= YEAR_WINDOW]
            if len(near) == 1:
                return near[0], None
            if len(near) > 1:
                best = sorted(near, key=lambda c: (-pub_ok(c), abs(_safe_int(c["start_year"]) - parsed["year"]), -_safe_int(c.get("count_of_issues"))))
                return best[0], None
            older = [c for c in exact if c.get("start_year") and _safe_int(c["start_year"]) <= parsed["year"]]
            if older:
                best = sorted(older, key=lambda c: (-pub_ok(c), -_safe_int(c.get("count_of_issues")), -_safe_int(c["start_year"])))
                return best[0], None
            if first_issue:
                later = [c for c in exact if c.get("start_year") and _safe_int(c["start_year"]) == parsed["year"] + 1]
                if later:
                    return sorted(later, key=lambda c: (-pub_ok(c), -_safe_int(c.get("count_of_issues"))))[0], None
        return None, "no same-named Comic Vine volume fits the file year"
    return None, "no Comic Vine volume has that name"


def choose_issue(parsed, issues):
    """issues: [{id, issue_number}] of the chosen volume."""
    target_num = parsed.get("volume") if parsed.get("volume") is not None else parsed.get("issue_num")
    if target_num is not None:
        hit = [i for i in issues if _issue_key(i.get("issue_number")) == _issue_key(target_num)]
        if not hit and parsed.get("volume") is not None and len(issues) == 1:
            # "Vol. 4 - #TAKEBACKTHESHIELD": Comic Vine lists that trade as its own one-issue volume (issue #1), so the file's
            # volume number is not an issue number there.
            return (issues[0], None)
        return (hit[0], None) if len(hit) >= 1 else (None, f"the volume has no issue #{target_num}")
    if parsed.get("year"):
        year = _safe_int(parsed["year"])
        # Annual series volumes contain decades of one-shots. A filename such
        # as "Iron Man Annual 2001" identifies its issue by publication year
        # even without a trailing issue number. Require one dated issue only.
        dated = [issue for issue in issues
                 if str(issue.get("cover_date") or issue.get("store_date") or "")[:4] == str(year)]
        if len(dated) == 1:
            return (dated[0], None)
        if len(dated) > 1:
            return (None, f"the volume has multiple issues dated {year} and the file name gives no issue number")
    if len(issues) == 1:
        return (issues[0], None)
    return (issues[0], None) if len(issues) == 1 else (None, f"the volume has {len(issues)} issues and the file name gives no volume number")


def _archive_issue_hint(path):
    """Return an unambiguous series/issue hint from CBZ page filenames, if available.

    Event packs often rename every book by reading-order position (for example,
    "No Man's Land 018.cbz") while the page names retain the original series and
    issue ("Azrael_No.052_p00.jpg"). Never use the pack ordinal as the issue.
    """
    if Path(path).suffix.lower() != ".cbz":
        return None
    try:
        with zipfile.ZipFile(path) as archive:
            members = [n for n in archive.namelist()
                       if Path(n).suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}]
    except (OSError, zipfile.BadZipFile):
        return None

    hints = {}
    for member in members:
        stem = Path(member).stem.strip()
        stem = re.sub(r"~[^~]+$", "", stem).strip()
        stem = re.sub(r"[_\s-]+rougher$", "", stem, flags=re.I)
        # Some No Man's Land/Aftershock packs use event-order filenames while
        # the page scans retain the real issue name in fused forms such as
        # "ShadowOfTheBat77p01" or "Batman = Shadow079 ...". Keep the series
        # and issue from the scan names instead of interpreting the pack
        # ordinal as the comic issue.
        shadow_issue = re.match(
            r"^(?:batman[\s=_-]+)?shadow(?:of[\s_-]*the[\s_-]*bat)?[\s=_-]*0*(\d{1,3})(?=$|p\d|[^0-9])",
            stem, re.I,
        )
        if shadow_issue:
            stem = f"Shadow of the Bat {int(shadow_issue.group(1))}"
        # Some scans fuse the series and issue, and optionally append the page
        # number: Robin67, Robin67p01, Nightwing038. Normalize these before
        # the generic title parser, which intentionally avoids guessing from
        # arbitrary trailing digits.
        fused_page = re.fullmatch(r"(robin|nightwing)[\s_-]*0*(\d{1,3})(?:p\d{1,4})?", stem, re.I)
        if fused_page:
            stem = f"{fused_page.group(1)} {int(fused_page.group(2))}"
        # Common scan conventions append the page ordinal to the series/issue.
        stem = re.sub(r"(?:[\s_-]+(?:pg|page|p)[\s_-]*\d{1,4}[a-z]?|[\s_-]+#?\d{2,4}[_\s-]+(?:FC|LP))$", "", stem, flags=re.I)
        # Robin's page names encode both volume and issue: robin_v1_068_00.
        vol_issue = re.fullmatch(r"(.+?)_v\d+[_-]0*(\d{1,3})", stem, re.I)
        if vol_issue and "nml" not in vol_issue.group(1).lower():
            stem = f"{vol_issue.group(1)} {vol_issue.group(2)}"
        # Scan exports may append a page number after the issue number (e.g.
        # Detective-736-01 or Batman_Chronicles-16-01). Strip it only when
        # there are at least two numeric tokens, so a bare issue number stays.
        elif len(re.findall(r"\d+", stem)) >= 2:
            stem = re.sub(r"[\s_-]+#?\d{1,3}[a-z]?$", "", stem, flags=re.I)
        # Recheck volume/issue scan names after removing their page ordinal,
        # e.g. robin_v1_068_00_rougher -> Robin #68.
        if not vol_issue:
            vol_issue = re.fullmatch(r"(.+?)_v\d+[_-]0*(\d{1,3})", stem, re.I)
            if vol_issue and "nml" not in vol_issue.group(1).lower():
                stem = f"{vol_issue.group(1)} {int(vol_issue.group(2))}"
        # Keep the issue digit in fused series names used by this pack.
        special = re.fullmatch(r"(robin|nightwing)(\d{1,4})", stem, re.I)
        if special:
            stem = f"{special.group(1)} {special.group(2)}"
        # The pack uses a few event-specific names that do not include '#'.
        if re.fullmatch(r"Nightwing[-_ ]SecretFiles1", stem, re.I):
            stem = "Nightwing Secret Files 1"
        if re.fullmatch(r"__hr_yjnml0001", Path(member).stem, re.I):
            stem = "Young Justice in No Man's Land 1"
        stem = re.sub(r"^[-_\s]*ocd[-_\s]*$", "", stem, flags=re.I).strip(" -_.")
        if not stem or re.fullmatch(r"(?:cover|page|\d{1,4}|__hr.*)", stem, re.I):
            continue
        fused = re.fullmatch(r"(robin|nightwing)(\d{1,4})", stem, re.I)
        if fused:
            stem = f"{fused.group(1)} {fused.group(2)}"
        # Remove known publication-year tokens, not issue numbers.
        stem = re.sub(r"(?<!\d)(?:19|20)\d{2}(?!\d)", " ", stem)
        stem = " ".join(stem.replace("_", " ").replace("-", " ").split())
        # Add an explicit extension: Path('Azrael No.050').stem would otherwise
        # mistake the issue suffix for a file extension and drop it.
        parsed = parse_name(stem + ".jpg", aliases=ARCHIVE_SERIES_ALIASES)
        issue_no = parsed.get("issue_num")
        series = next((n for n in parsed.get("names", []) if n), "")
        if not series or issue_no is None:
            continue
        # Require a plausible comic-series title and a real issue marker/number.
        if not re.search(r"[A-Za-z]{3}", series) or not re.fullmatch(r"-?\d+(?:\.\d+)?", str(issue_no)):
            continue
        key = (norm(series), str(issue_no).lstrip("0") or "0")
        value = {"names": [ARCHIVE_SERIES_ALIASES.get(norm(series), series)],
                 "volume": None, "issue_num": str(issue_no), "year": None}
        count, _ = hints.get(key, (0, value))
        hints[key] = (count + 1, value)
    # Require a clear consensus across the archive's internal page names.
    # This tolerates a small number of stray scan labels without trusting one
    # incidental number or the event pack's outer ordinal.
    total = sum(count for count, _ in hints.values())
    if not hints or total == 0:
        return None
    key, (count, value) = max(hints.items(), key=lambda item: item[1][0])
    return value if count >= 2 and count / total >= 0.8 else None


def _issue_key(value):
    """Comparable form of an issue number: zero padding and a leading # are ignored, a half issue is '0.5' whether Comic Vine writes it
    '1/2' or with the fraction sign, and a Marvel tie-in label such as '.INH' is kept ('20.INH' is not issue 20)."""
    value = str(value or "").strip().lstrip("#")
    if value in ("1/2",):
        value = "0.5"
    value = value.replace("½", "0.5").replace("¼", "0.25").replace("¾", "0.75")
    m = re.fullmatch(r"(-?)0*(\d+)(\.\d+|\.[A-Za-z]{2,4})?", value)
    return (m.group(1), str(int(m.group(2))), (m.group(3) or "").upper()) if m else value


def _get(cv_key, path, **params):
    def fetch():
        q = urllib.parse.urlencode({"api_key": cv_key, "format": "json", **params})
        req = urllib.request.Request(f"https://comicvine.gamespot.com/api/{path}?{q}", headers={"User-Agent": "komga-comic-tools"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)["results"]
    return comicvine_store.get_json(cv_key, path, params, fetch)


@lru_cache(maxsize=512)
def search_volumes(cv_key, name):
    return _get(cv_key, "search/", query=name, resources="volume", limit=50, field_list="id,name,start_year,count_of_issues,publisher")


@lru_cache(maxsize=128)
def _all_volume_issues(cv_key, volume_id):
    """Fetch one issue index per volume in pages, avoiding a Comic Vine request for every numbered issue."""
    all_issues, offset = [], 0
    while offset < 2000:
        batch = _get(cv_key, "issues/", filter=f"volume:{volume_id}", limit=100, offset=offset,
                     field_list="id,issue_number,name,cover_date")
        all_issues.extend(batch)
        if len(batch) < 100:
            break
        offset += len(batch)
    return all_issues


def volume_issues(cv_key, volume_id, issue_num=None):
    issues = _all_volume_issues(cv_key, str(volume_id))
    if issue_num is None:
        return issues
    return [issue for issue in issues if _issue_key(issue.get("issue_number")) == _issue_key(issue_num)]


def find_files(dirs):
    for d in dirs:
        for p in sorted(Path(d).rglob("*")):
            if p.suffix.lower() in EXTENSIONS and not p.name.startswith("."):
                yield p


def already_tagged(path):
    """True only when root ComicInfo.xml contains a parseable Comic Vine issue ID."""
    try:
        return tagcommon.comicvine_issue_id(path) is not None
    except Exception:                                                              # noqa: BLE001
        return False


@lru_cache(maxsize=32)
def _story_arc_issue_ids(cv_key, arc_id):
    result = _get(cv_key, f"story_arc/4045-{arc_id}/", field_list="id,issues")
    return {int(item["id"]) for item in (result.get("issues") or []) if item.get("id")}


def _parent_series_issue(cv_key, parsed, sleep, pause):
    """A subtitled run that Comic Vine files under its parent series: 'Iron Man - Director of S.H.I.E.L.D. 015 (2007)' is issue 15 of
    'Iron Man' (2005), because the run kept the parent's numbering. Accepted only when exactly one same-titled volume has that issue
    with a cover date within a year of the file's year."""
    if parsed.get("issue_num") is None or not parsed.get("year"):
        return None
    number = _issue_key(parsed["issue_num"])
    try:
        wanted = int(number[1])
    except (TypeError, ValueError):
        return None
    bases = []
    for n in parsed["names"]:
        base = re.split(r"\s*(?::|\s-\s)\s*", n, maxsplit=1)[0].strip()
        if base and norm(base) != norm(n) and base not in bases:
            bases.append(base)
    found = []
    for base in bases:
        cands = [c for c in search_volumes(cv_key, base) if norm(c.get("name")) == norm(base)
                 and _safe_int(c.get("start_year")) and _safe_int(c["start_year"]) <= parsed["year"]
                 and (_safe_int(c.get("count_of_issues")) == 0 or _safe_int(c.get("count_of_issues")) >= wanted)]
        sleep(pause)
        cands.sort(key=lambda c: _safe_int(c["start_year"]), reverse=True)
        for c in cands[:6]:
            for issue in volume_issues(cv_key, c["id"], parsed["issue_num"]):
                year = _safe_int(str(issue.get("cover_date") or "")[:4])
                if year and abs(year - parsed["year"]) <= 1:
                    found.append((c, issue))
            sleep(pause)
    return found[0] if len(found) == 1 else None


def _collected_sibling(cv_key, volume, candidates, why, sleep, pause):
    """A file named for a mini-series with no issue number is its collected edition when Comic Vine keeps a same-named one-issue volume
    for it whose issue is called 'Volume 1': 'International Iron Man (2016)' (7 issues) and its 'Volume 1' entry."""
    if not why or not ("no volume number" in why or "no issue number" in why):
        return None
    start = _safe_int(volume.get("start_year"))
    hits = []
    for c in candidates:
        if c["id"] == volume["id"] or norm(c.get("name")) != norm(volume.get("name")) or _safe_int(c.get("count_of_issues")) != 1:
            continue
        if start and _safe_int(c.get("start_year")) and abs(_safe_int(c["start_year"]) - start) > 1:
            continue
        issues = volume_issues(cv_key, c["id"])
        sleep(pause)
        if len(issues) == 1 and re.match(r"(?i)\s*(?:volume|vol\.?|v)\s*\d", str(issues[0].get("name") or "")):
            hits.append((c, issues[0]))
    return hits[0] if len(hits) == 1 else None


def match(cv_key, path, pause=1.5, sleep=time.sleep, aliases=None, year_hint=None, story_arc_id=None,
          series_hint=None, issue_hint=None):
    tags = tagcommon.read_tags(path)
    parsed = parse_name(path.name, aliases)
    # Only trust ComicInfo fields as a match when Comic Vine itself supplied an issue ID.
    # Generic embedded tags (often just Series/Number/Count) can describe a different pack entirely.
    if tagcommon.comicvine_issue_id(path) is not None:
        if tags.get("Series"):
            parsed["names"] = [tags["Series"]] + [n for n in parsed["names"] if n != tags["Series"]]
        if tags.get("Number"):
            parsed["issue_num"] = str(tags["Number"]).strip().lstrip("#")
            parsed["volume"] = None
        if tags.get("Volume") and str(tags["Volume"]).isdigit() and len(str(tags["Volume"])) == 4:
            parsed["year"] = _safe_int(tags["Volume"])
    else:
        archive_hint = _archive_issue_hint(path)
        if archive_hint:
            # The archive's internal page names can improve the series and issue
            # number, but they usually do not include a publication year. Keep a
            # year parsed from the outer filename so same-named Comic Vine
            # relaunches (for example, Fantastic Four #001 (1961)) remain
            # distinguishable after applying the archive hint.
            archive_hint["year"] = archive_hint.get("year") or parsed.get("year")
            # Applying an archive-derived hint must not discard the user's
            # configured series aliases. Reapply a matching alias to the
            # hinted name while retaining the source spelling as fallback.
            aliases_by_norm = {norm(k): v for k, v in (aliases or {}).items()}
            extra = [aliases_by_norm[norm(name)] for name in archive_hint.get("names", [])
                     if norm(name) in aliases_by_norm]
            archive_hint["names"] = list(dict.fromkeys(extra + archive_hint.get("names", [])))
            parsed = archive_hint
        if year_hint and not parsed.get("year"):
            parsed["year"] = _safe_int(year_hint)
        if series_hint:
            parsed["names"] = [str(series_hint).strip()] + [n for n in parsed.get("names", []) if norm(n) != norm(series_hint)]
        if issue_hint is not None:
            parsed["issue_num"] = str(issue_hint).strip().lstrip("#")
            parsed["volume"] = None
    out = {"file": str(path), "parsed": parsed}
    if not parsed["names"]:
        return {**out, "status": "skip", "why": "could not read a title from the file name"}
    seen, candidates = set(), []
    for name in dict.fromkeys(parsed["names"]):
        for c in search_volumes(cv_key, name):
            if c["id"] not in seen:
                seen.add(c["id"])
                candidates.append(c)
        sleep(pause)
    if story_arc_id and parsed.get("issue_num") is not None:
        arc_ids = _story_arc_issue_ids(cv_key, str(story_arc_id))
        arc_hits = []
        seen_ids = set()
        for name in parsed["names"]:
            for candidate in candidates:
                if norm(candidate.get("name")) != norm(name) or str(candidate.get("id")) in seen_ids:
                    continue
                seen_ids.add(str(candidate.get("id")))
                for issue in volume_issues(cv_key, candidate["id"], parsed["issue_num"]):
                    if int(issue.get("id") or 0) in arc_ids:
                        arc_hits.append((candidate, issue))
                sleep(pause)
        if len(arc_hits) == 1:
            volume, issue = arc_hits[0]
            return {**out, "status": "match", "volume": volume, "issue": issue}
        if len(arc_hits) > 1:
            return {**out, "status": "skip", "why": f"issue #{parsed['issue_num']} matches multiple issues in Comic Vine story arc {story_arc_id}"}
    volume, why = choose_volume(parsed, candidates)
    if not volume:
        # A filename can identify the series and issue but omit the year. When
        # several runs share a title, use issue existence to disambiguate only
        # if exactly one candidate contains that numbered issue. Never select
        # the first search result on title similarity alone.
        if parsed.get("issue_num") is not None:
            matching = []
            seen_ids = set()
            for name in parsed["names"]:
                for candidate in candidates:
                    if norm(candidate.get("name")) != norm(name) or str(candidate.get("id")) in seen_ids:
                        continue
                    seen_ids.add(str(candidate.get("id")))
                    hits = volume_issues(cv_key, candidate["id"], parsed["issue_num"])
                    sleep(pause)
                    if hits:
                        matching.append((candidate, hits[0]))
            if len(matching) == 1:
                volume = matching[0][0]
            elif len(matching) > 1:
                return {**out, "status": "skip", "why": f"issue #{parsed['issue_num']} exists in multiple same-named Comic Vine volumes; add a verified year or exact override"}
        if not volume:
            return {**out, "status": "skip", "why": why}
    tgt = parsed.get("volume") if parsed.get("volume") is not None else parsed.get("issue_num")
    volumes = [volume]
    if parsed.get("issue_num") is not None:
        # Search metadata sometimes reports a later relaunch as the closest year
        # even when that run has no such high issue number. Confirm the issue in
        # the preferred volume, then try same-title runs that had already started
        # by the issue year before holding an otherwise valid comic.
        seen_ids = {str(volume["id"])}
        alternatives = []
        for candidate in candidates:
            if str(candidate.get("id")) in seen_ids or not any(norm(candidate.get("name")) == norm(n) for n in parsed["names"]):
                continue
            start_year = _safe_int(candidate.get("start_year"))
            if parsed.get("year") and start_year and start_year > parsed["year"]:
                continue
            seen_ids.add(str(candidate["id"]))
            alternatives.append(candidate)
        alternatives.sort(key=lambda c: _safe_int(c.get("start_year")), reverse=True)
        volumes.extend(alternatives)
    issue, why = None, None
    for candidate in volumes:
        issues = volume_issues(cv_key, candidate["id"], tgt)
        if not issues and parsed.get("volume") is not None:
            # a named collected volume that Comic Vine keeps as a one-issue volume: take its only issue
            only = volume_issues(cv_key, candidate["id"])
            if len(only) == 1:
                issues = only
        sleep(pause)
        issue, why = choose_issue(parsed, issues)
        if issue:
            volume = candidate
            break
    if not issue:
        found = _parent_series_issue(cv_key, parsed, sleep, pause) or _collected_sibling(cv_key, volume, candidates, why, sleep, pause)
        if found:
            return {**out, "status": "match", "volume": found[0], "issue": found[1]}
        return {**out, "status": "skip", "why": why, "volume": volume}
    return {**out, "status": "match", "volume": volume, "issue": issue}


TRANSIENT_TAG_ERRORS = re.compile(r"rate limit|network error|network data fetch failure|timed? ?out|connection (?:error|reset|aborted)|temporarily unavailable", re.I)


def tag_outcome(exe, path, issue_id, timeout=300):
    """Have ComicTagger write the Comic Vine tags of that issue. 'ok' when the file now carries them, 'transient' when it failed
    for a reason that passes (Comic Vine rate limit, network error, timeout: worth retrying on the next run), else 'failed'."""
    cmd = [exe, "-s", "-o", "--id", str(issue_id), "--tags-write", "CR", "--no-gui", "--no-prompt-on-save", "--no-abort", str(path)]
    try:
        done = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return "transient"
    out = done.stdout or ""
    if "Save complete" in out and already_tagged(path):
        return "ok"
    return "transient" if TRANSIENT_TAG_ERRORS.search(out + "\n" + (done.stderr or "")) else "failed"


def tag(exe, path, issue_id, timeout=300):
    """Have ComicTagger write the Comic Vine tags of that issue. True when the file now carries them."""
    return tag_outcome(exe, path, issue_id, timeout) == "ok"


def run(dirs, cv_key, exe="comictagger", apply=False, max_files=20, pause=1.5, log=None, say=print, sleep=time.sleep, aliases=None):
    """Returns the number of files tagged (always 0 for a preview)."""
    todo = [p for p in find_files(dirs) if not already_tagged(p)]
    say(f"{len(todo)} file(s) without Comic Vine tags in {len(list(dirs))} folder(s)")
    tagged = 0
    for n, path in enumerate(todo):
        if n >= max_files:
            say(f"  stopped after {max_files}; run again for the rest")
            break
        try:
            r = match(cv_key, path, pause, sleep, aliases)
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
