import re
def _safe_int(x, base=10):
    try:
        if isinstance(x, str): x = re.sub(r"[^\d-]", "", x)
        if not x or x == "-": return 0
        return int(x, base)
    except Exception: return 0

"""Everything that happens to a comic between "I downloaded it" and "it is on the site and in Komga", in one script.

Drop finished or still-downloading comics into the Incoming folder. Every run (incoming-pipeline.ps1 runs this every 15 minutes) does:

  prepare   for each file that has stopped changing (no .aria2/.part marker, 10+ minutes old):
              1. tag it with Comic Vine (tag_collected: name -> volume -> issue id), unless it already is
              2. fix its title inside the file ("Volume 3" -> "<Series> Vol. 3")
              3. check the cover: the first page must look like Comic Vine's cover for that issue (an ad or credits page
                 as page one would become the site's cover, so such files are HELD, not sent)
              4. work out which collection it belongs to (routes in the config) and move it to _ready\\<Collection>\\<Series>\\
            Anything it is not sure about stays in Incoming and is listed in incoming_report.json (held, with the reason).
            A series with no route gets one created automatically (auto_create_routes, publishers in the config only): see auto_create_route.
  upload    (the .ps1) copies _ready to the Comics mirror, then moves the uploaded files to _done
  komga     once uploaded files are old enough for the mirror mount to show them: scan the Komga library, lock title / number /
            date on the new books, then update the read lists so the site and Komga show the same lists in the same order:
              - "! <Library> - Suggested Reading Order" for the collection (created if missing)
              - the publisher's master list: "! Marvel Universe ...", "! DC Universe ...", or a new "! <Publisher> ..." list
                for any other publisher (created if missing)
            New single issues go in at their verified Comic Vine cover date; entries without a reliable issue date and collected
            editions are tagged and filed in their library but kept out of the lists (hold_collected_editions: true holds them for review). Existing entries never move. Every change is re-read and checked
            (nothing lost, nothing reordered, block in one place, dates around it plausible) and undone if the check fails.
  verify-export  after the hourly exporter has written the .cbl files: each touched list's file must hold exactly the books of the
            Komga list in the same order, otherwise the previous good file is put back before anything is uploaded
  No test suite runs as part of the scheduled pipeline. Uncertain matches stay held for review rather than being guessed into a list.
  The site's feed tracker and Tulip's hourly digest announce the new issues on the usual schedule.

Usage: python incoming_pipeline.py prepare|komga|verify-export|mark-uploaded|acquire [--config incoming_pipeline.json] [--dry-run]
"""
import argparse
import copy
from difflib import SequenceMatcher
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import zipfile
from datetime import datetime
from pathlib import Path

import komga_title_fix
import comicvine_store
import acquire
import breakout
import tag_collected
import tagcommon

PARTIAL_SUFFIXES = (".aria2", ".aria2__temp", ".part", ".crdownload", ".tmp", ".torrent")
COMIC_SUFFIXES = (".cbz", ".cbr")
PACKAGE_SUFFIXES = (".zip",)
IMAGE = re.compile(r"\.(jpe?g|png|webp)$", re.I)
COVER_FEATURES_OK = 30   # matching image features (RANSAC inliers) with the Comic Vine cover that count as the same cover; unrelated images score under ~15
COVER_OK = 14            # dHash distance (of 64 bits) up to which a page counts as "the Comic Vine cover"
LIST_SUFFIX = " - Suggested Reading Order"
PLAUSIBLE_MONTHS = 36    # dated entries next to an inserted block may be at most this far (in months) from its anchor date
KOMGA_DELAY = 12 * 60    # seconds after an upload before Komga's view of the mirror is trusted (the mount caches directories for 10 minutes)
CV_RATE_LIMIT_COOLDOWN = 60 * 60  # stop retrying Comic Vine after HTTP 420; preserve remaining files for the next scheduled run
MAX_ITEMS_PER_RUN = 500  # safety ceiling; the time budget normally ends the batch first
PREPARE_TIME_BUDGET_SECONDS = 10 * 60  # leave a few minutes in the 15-minute task interval for upload and Komga work
PIPELINE_ARTIFACT_RETENTION_DAYS = 30  # recoverable source/list backups remain available for a month
HELD_RETRY_SECONDS = 7 * 24 * 60 * 60  # retry unchanged holds weekly unless the file, policy, or code changes
DATE_REPAIR_RETRY_SECONDS = 15 * 60
LIST_RETRY_SECONDS = 24 * 60 * 60  # do not re-read a chronologically blocked master list for every pending file
CONTENT_ORDER_ANCHORS = Path(r"C:\Users\paris\ArmgddnUploader\readlists\content_order_anchors.json")


ARCHIVE_MAGIC = (b"PK\x03\x04", b"PK\x05\x06", b"Rar!\x1a\x07", b"7z\xbc\xaf\x27\x1c")
BAD_DOWNLOADS = "_bad_downloads"


class ArchiveConversionError(RuntimeError):
    """A CBR cannot be converted without losing or corrupting pages."""


def clean_tool_output(value, limit=400):
    """Keep subprocess diagnostics readable when console tools emit backspaces."""
    value = re.sub(r"[\x00-\x1f\x7f-\x9f]+", " ", value or "")
    total_errors = re.search(r"Total errors:\s*\d+", value, re.IGNORECASE)
    if total_errors:
        return total_errors.group(0)
    return " ".join(value.split())[-limit:]


def pipeline_say(message):
    """Print progress and append it to the live pipeline log when run by the scheduled wrapper."""
    message = str(message)
    print(message, flush=True)
    log_path = os.environ.get("KOMGA_PIPELINE_LOG")
    if log_path:
        try:
            with open(log_path, "a", encoding="utf-8") as log:
                log.write(f"{datetime.now().isoformat(timespec='seconds')} {message}\n")
        except OSError:
            pass


# ---------------------------------------------------------------- small pure helpers
def sanitize(name):
    """A Comic Vine volume name as a folder name: 'Marvel Masterworks: The Incredible Hulk' -> 'Marvel Masterworks - The Incredible Hulk'."""
    name = re.sub(r"\s*:\s*", " - ", name or "")
    name = re.sub(r'[<>"/\\|?*]', "", name)
    return " ".join(name.split()).strip(" .")


def bad_download_kind(path):
    """None for a real archive; 'a web page' or 'empty' for a download that is not a comic at all. A file host's landing page saved under the
    comic's name (vikingfile.com links do this: aria2 fetched the HTML page, 650 KB, instead of the 350 MB file) is not a damaged comic and
    no re-run will fix it, so it must never sit in the held list or be sent to Comic Vine."""
    try:
        with open(path, "rb") as f:
            head = f.read(512)
    except OSError:
        return None
    if not head:
        return "empty"
    if head.startswith(ARCHIVE_MAGIC):
        return None
    probe = head.lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if probe.startswith((b"<!doctype html", b"<html", b"<head", b"<?xml")) or b"<html" in probe[:300]:
        return "a web page"
    return None


def quarantine_bad_download(path, incoming, say=print, dry_run=False):
    """Move a fake download out of the inbox into _bad_downloads, so the next run does not see it again. Returns the new path."""
    dest_dir = Path(incoming) / BAD_DOWNLOADS
    dest = dest_dir / Path(path).name
    n = 1
    while dest.exists():
        n += 1
        dest = dest_dir / f"{Path(path).stem} ({n}){Path(path).suffix}"
    if not dry_run:
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(dest))
    return dest


def is_partial_marker(name):
    return name.lower().endswith(PARTIAL_SUFFIXES)


def is_finished(path, now, min_age_minutes=10):
    """Old enough, and no download marker beside it (aria2 writes <file>.aria2 and <file>.aria2__temp while it works)."""
    try:
        if now - path.stat().st_mtime < min_age_minutes * 60:
            return False
    except OSError:
        return False
    return not any((path.parent / (path.name + s)).exists() for s in PARTIAL_SUFFIXES)


COLLECTED_FORMATS = re.compile(r"(?:trade\s+paper\s*back|paperback|hardcover|collected edition|graphic novel|omnibus|compendium|complete series|complete collection|masterworks|archives?|epic collection|library edition|deluxe edition|\btpb\b|\bhc\b)", re.I)
NAMED_VOLUME = re.compile(r"\b(?:v|vol(?:ume)?)[ ._-]*0*\d{1,3}\s*[-–:]\s*\S", re.I)
BY_VOLUME = re.compile(r"\bby\b.*\b(?:v|vol(?:ume)?)[ ._-]*0*\d{1,3}\b", re.I)


FRANCHISE_PREFIX = re.compile(r"^(?:the|all[- ]new|mighty)\s+", re.I)
FRANCHISE_BLOCKED = {"annual", "annuals", "special", "one-shot", "marvel", "dc", "comics", "giant-size", "unknown"}


def franchise_name(volume_name):
    """'Captain America: Sam Wilson' -> 'Captain America'; 'All-New Captain America' -> 'Captain America'; 'The Mighty Thor' -> 'Thor'."""
    name = re.split(r"\s*(?::|\s-\s)\s*", (volume_name or "").strip(), maxsplit=1)[0]
    for _ in range(2):
        name = FRANCHISE_PREFIX.sub("", name)
    name = re.sub(r"\s*\(\d{4}\)\s*$", "", name).strip(" .")
    return name if len(name) >= 3 and name.lower() not in FRANCHISE_BLOCKED else ""


def auto_create_route(cfg, volume_name, publisher, say=print, dry_run=False):
    """Create (and remember) a collection route for a series nothing matches yet, when the config allows it.

    Needs "auto_create_routes": true and a publisher listed in the config's "publishers" (Marvel, DC Comics, ...), so an
    unknown publisher's file is still held for review. The route matches the series' franchise ('Captain America' covers
    Captain America, All-New Captain America, Captain America: Sam Wilson ...) and sends it to '<Franchise> Collection'.
    Lists default to the publisher's master list. A library root is added beside the other configured roots, so the Komga
    stage can create the library once its mirror folder is visible. The route is written to the config file (backup first)
    so later runs and the policy fingerprint see it; with dry_run it is only added in memory and nothing is written."""
    if not cfg.get("auto_create_routes") or publisher not in (cfg.get("publishers") or {}):
        return None
    franchise = franchise_name(volume_name)
    if not franchise:
        return None
    collection = f"{sanitize(franchise)} Collection"
    route = {"match": rf"\b{re.escape(franchise)}\b", "collection": collection, "library": collection,
             "auto_created": time.strftime("%Y-%m-%d")}
    for r in cfg.get("routes", []):
        if r.get("collection") == collection or r.get("match") == route["match"]:
            return r
    roots = cfg.setdefault("library_roots", {})
    other = next((Path(v) for v in roots.values()), None)
    if collection not in roots and other is not None:
        roots[collection] = str(other.parent / collection)
    cfg.setdefault("routes", []).append(route)
    say(f"  {'would create' if dry_run else 'created'} route: {route['match']!r} -> {collection!r} (publisher {publisher}, series {volume_name!r})")
    path = cfg.get("_config_path")
    if dry_run or not path:
        return route
    try:
        cfg_path = Path(path)
        disk = json.loads(cfg_path.read_text(encoding="utf-8"))
        if not any(r.get("collection") == collection or r.get("match") == route["match"] for r in disk.get("routes", [])):
            backup = cfg_path.with_name(cfg_path.name + ".bak-autoroute-" + time.strftime("%Y%m%d"))
            if not backup.exists():
                shutil.copy2(cfg_path, backup)
            disk.setdefault("routes", []).append(route)
            if collection in roots:
                disk.setdefault("library_roots", {})[collection] = roots[collection]
            tmp = cfg_path.with_name(cfg_path.name + ".tmp")
            tmp.write_text(json.dumps(disk, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            os.replace(tmp, cfg_path)
    except Exception as e:                                                         # noqa: BLE001
        say(f"  could not save the new route to the config ({e}); it applies to this run only")
    return route


def route_for(cfg, volume_name):
    """-> (route, optional explicitly configured anchor, anchor source) for a Comic Vine volume name."""
    for r in cfg.get("routes", []):
        if re.search(r["match"], volume_name or "", re.I):
            for a in r.get("anchors", []):
                if re.search(a["match"], volume_name, re.I):
                    return r, a["anchor"], "config.override"
            if r.get("anchor"):
                return r, r["anchor"], "config.override"
            return r, None, None
    return None, None, None


def collected_edition(path, tags, volume_name="", issue=None):
    """Conservative collected-edition guard; editions need contents-level chronology, not their release date.

    Comic Vine models many paperbacks as issue #1 in a volume, and its volume name may omit the format
    (for example, "Daredevil By Chip Zdarsky: Doing Time"). Check the incoming filename and both CV labels.
    A named "Vol. N - Subtitle" file is a collection; it must not be inserted using the paperback's release date.
    """
    fmt = str((tags or {}).get("Format") or "")
    name = Path(path).name
    cv_name = str((issue or {}).get("name") or "")
    text = " ".join((fmt, name, volume_name or "", cv_name))
    return bool(COLLECTED_FORMATS.search(text) or NAMED_VOLUME.search(name) or BY_VOLUME.search(name))


def month_from_cover_date(value):
    """Accept only an actual Comic Vine issue cover date, returning YYYY-MM."""
    m = re.match(r"^(19\d{2}|20\d{2})-(0[1-9]|1[0-2])(?:-\d{2})?$", str(value or "").strip())
    return f"{m.group(1)}-{m.group(2)}" if m else None


def month_index(date):
    return _safe_int(date[:4]) * 12 + _safe_int(date[5:7]) - 1


def placement_index(dates, anchor):
    """Where a block dated `anchor` ('YYYY-MM') goes in a read list whose entries have release dates `dates` ('' when unknown).

    Same rule as komga_readlist_merge: after the last entry that is not later than the anchor, judged by a running maximum.
    Dates far (36+ months) from their neighbours (an omnibus among 1970s issues) are ignored, and the end of the list is the
    answer when nothing in it is later than the anchor."""
    months = [month_index(d) if d else None for d in dates]
    envelope, run = [], 0
    for i, m in enumerate(months):
        near = sorted(x for x in months[max(0, i - 6):i + 7] if x is not None)
        if m is not None and not (near and abs(m - near[len(near) // 2]) > 36):
            run = max(run, m)
        envelope.append(run)
    a = month_index(anchor + "-01")
    return next((i for i, e in enumerate(envelope) if e > a), len(dates))


def insert_block(ids, block, at):
    """New list with `block` inserted at `at`; every existing id keeps its relative order."""
    new = ids[:at] + [b for b in block if b not in set(ids)] + ids[at:]
    assert [i for i in new if i in set(ids)] == ids, "existing order changed"
    return new


def site_list_name(base):
    """The Komga list name the site mirrors: '! Hulk Collection - Suggested Reading Order'."""
    return f"! {base}{LIST_SUFFIX}"


def master_base(cfg, publisher):
    """Name of a publisher's master list: 'Marvel Universe', 'DC Universe', or the publisher's own name for anyone else."""
    return (cfg.get("publishers") or {}).get(publisher) or sanitize(publisher) or "Other Publishers"


def list_months(books):
    """Release month index per entry ('' / unknown -> None), dropping dates far from their neighbours (omnibus outliers)."""
    months = [month_index((b.get("metadata") or {}).get("releaseDate")) if (b.get("metadata") or {}).get("releaseDate") else None for b in books]
    out = []
    for i, m in enumerate(months):
        near = sorted(x for x in months[max(0, i - 6):i + 7] if x is not None)
        out.append(None if m is None or (near and abs(m - near[len(near) // 2]) > 36) else m)
    return out


def readlist_order_months(books, list_name):
    """Use locked issue dates and list-scoped contents anchors for safe insertion into a curated list."""
    try:
        anchor_rows = json.loads(CONTENT_ORDER_ANCHORS.read_text(encoding="utf-8")).get("books", {})
    except (OSError, ValueError, AttributeError):
        anchor_rows = {}
    months = []
    for book in books:
        metadata = book.get("metadata") or {}
        tags = {"Format": metadata.get("format") or ""}
        issue = {"name": metadata.get("title") or ""}
        import export_komga_readlists as exporter
        if collected_edition(book.get("url") or book.get("name") or "", tags,
                             book.get("seriesTitle") or "", issue) or exporter.is_collected_book(book):   # same rule as the export check
            row = anchor_rows.get(str(book.get("id") or ""), {})
            date = row.get("anchor") if list_name in (row.get("lists") or []) else ""
        else:
            date = metadata.get("releaseDate") or ""
        months.append(month_from_cover_date(date) or "")
    return months


def date_inversions(months):
    """Return the first few backward date steps, ignoring entries with no usable date."""
    out, previous = [], None
    for pos, date in enumerate(months, 1):
        if not date:
            continue
        current = month_index(date + "-01")
        if previous and current < previous[0]:
            out.append((previous[1], pos, previous[2], date))
            if len(out) == 5:
                break
        previous = (current, pos, date)
    return out


def verify_insert(books, new_ids, block, at, anchor, dates=None, strict=False):
    """Problems (empty list = fine) with a read list after a block was inserted. books: the entries before the change."""
    problems, old = [], [b["id"] for b in books]
    if len(set(new_ids)) != len(new_ids):
        problems.append("duplicate entries")
    if [i for i in new_ids if i in set(old)] != old:
        problems.append("existing entries were lost or reordered")
    if not set(block) <= set(new_ids):
        problems.append("new books missing from the list")
    pos = [new_ids.index(b) for b in block if b in new_ids]
    if pos and sorted(pos) != list(range(min(pos), min(pos) + len(pos))):
        problems.append("the new books are not together")
    months = ([month_index(d + "-01") if d else None for d in dates]
              if dates is not None else list_months(books))
    a = month_index(anchor + "-01")
    before = next((m for m in reversed(months[:at]) if m is not None), None)
    after = next((m for m in months[at:] if m is not None), None)
    if strict:
        if before is not None and before > a:
            problems.append("a later-dated entry would remain before the new block")
        if after is not None and after < a:
            problems.append("an earlier-dated entry would remain after the new block")
    else:
        if before is not None and before - a > PLAUSIBLE_MONTHS:
            problems.append(f"the entry before the block is dated {before - a} months after its anchor")
        if after is not None and a - after > PLAUSIBLE_MONTHS:
            problems.append(f"the entry after the block is dated {a - after} months before its anchor")
    return problems


CBL_BOOK = re.compile(r'<Book Series="([^"]*)" Number="([^"]*)"')


def cbl_sequence(text):
    import html as _h
    return [(_h.unescape(a), _h.unescape(b)) for a, b in CBL_BOOK.findall(text or "")]


def komga_sequence(books):
    """(Series, Number) per book exactly as the exporter writes them to the .cbl."""
    return [(b.get("seriesTitle") or "", str((b.get("metadata") or {}).get("number") or b.get("number") or 1)) for b in books]


def verify_export(cfg, komga, names, dest=r"C:\Users\paris\ArmgddnUploader\readlists", say=print):
    """The site shows the .cbl file, so it must equal the Komga list: same books, same order. A bad file is replaced by the newest
    earlier copy from cbl_history. Returns the names that were wrong."""
    lists = {l["name"]: l["id"] for l in komga.paged("/readlists")}
    bad = []
    for base in names:
        rid = lists.get(site_list_name(base))
        path = Path(dest) / f"{base}{LIST_SUFFIX}.cbl"
        if not rid:
            bad.append(base)
            say(f"  EXPORT MISMATCH for {base}: Komga read list is missing; no file published")
            continue
        if not path.exists():
            bad.append(base)
            say(f"  EXPORT MISMATCH for {base}: exported .cbl is missing; no file published")
            continue
        want = komga_sequence(komga.paged(f"/readlists/{rid}/books"))
        got = cbl_sequence(path.read_text(encoding="utf-8"))
        # The .cbl is the site's visible reading order; require a one-for-one exact sequence match.
        if got == want:
            continue
        bad.append(base)
        hist = sorted(Path(dest, "cbl_history").glob(f"{base}{LIST_SUFFIX}_*.cbl")) if Path(dest, "cbl_history").exists() else []
        if hist:
            shutil.copy2(hist[-1], path)
        say(f"  EXPORT MISMATCH for {base}: file has {len(got)} books, Komga {len(want)}; " + ("previous copy put back" if hist else "no earlier copy to put back"))
    return bad


def run_selftests(say=print, modules=None, extra_paths=None):
    """Run every test suite this pipeline depends on. Returns True when all pass.

    The pipeline's own suites must exist (a missing one counts as a failure); the exporter's suite is run too when it is installed."""
    import importlib.util
    import unittest
    here = Path(__file__).resolve().parent
    required = modules if modules is not None else ["test_incoming_pipeline", "test_tag_collected", "test_title_fix", "test_readlist_merge", "test_tagcommon"]
    optional = [] if modules is not None else ["test_export_komga_readlists"]
    for extra in (extra_paths if extra_paths is not None else (str(here),)):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    suite, loader = unittest.TestSuite(), unittest.TestLoader()
    for m in required + optional:
        if importlib.util.find_spec(m) is None:
            if m in required:
                say(f"self-test module {m} is missing: pipeline stopped")
                return False
            continue
        suite.addTests(loader.loadTestsFromName(m))
    result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
    if not result.wasSuccessful():
        say(f"SELF-TESTS FAILED ({len(result.failures)} failures, {len(result.errors)} errors): pipeline stopped")
        for t, tb in (result.failures + result.errors)[:3]:
            say(f"  {t}: {tb.strip().splitlines()[-1]}")
    return result.wasSuccessful()


# ---------------------------------------------------------------- cover check
def dhash(image, n=8):
    small = image.convert("L").resize((n + 1, n))
    px = list(getattr(small, "get_flattened_data", small.getdata)())
    bits = 0
    for r in range(n):
        for c in range(n):
            bits = (bits << 1) | (px[r * (n + 1) + c] > px[r * (n + 1) + c + 1])
    return bits


def distance(a, b):
    return bin(a ^ b).count("1")


COVERS_IN_NAME = re.compile(r"\((\d+)\s+covers?\)", re.I)


def cover_pages_to_check(path):
    """How many opening pages may hold the cover: four, or two more than the covers a '(5 covers)' scan says it carries up front
    (the main cover is then often not the first of the variants)."""
    m = COVERS_IN_NAME.search(Path(path).name)
    return max(4, int(m.group(1)) + 2) if m else 4


def first_pages(path, count=4):
    """Images of the first few pages of a .cbz/.cbr, natural order."""
    from PIL import Image
    names = sorted((n for n in tagcommon.list_names(path) if IMAGE.search(n) and "__macosx" not in n.lower()),
                   key=lambda n: [_safe_int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", n)])[:count]
    out = []
    for n in names:
        if tagcommon.is_cbz(path):
            data = zipfile.ZipFile(path).read(n)
        else:
            import subprocess
            data = subprocess.run([tagcommon.UNRAR, "p", "-inul", str(path), n], capture_output=True).stdout
        try:
            out.append(Image.open(io.BytesIO(data)))
        except Exception:                                                          # noqa: BLE001
            out.append(None)
    return out


_WARNED_NO_OPENCV = False


def feature_inliers(a, b):
    """How many image features of page `a` and the Comic Vine cover `b` agree geometrically (ORB + RANSAC). Far sturdier than the
    8x8 dHash for scans that are cropped, recoloured or framed differently; 0 when OpenCV is not installed."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        global _WARNED_NO_OPENCV
        if not _WARNED_NO_OPENCV:
            _WARNED_NO_OPENCV = True
            print("WARNING: OpenCV/NumPy are not installed in this Python, so the feature cover check is off and covers the quick "
                  "hash rejects will be held (pip install opencv-python-headless numpy)")
        return 0

    def prep(im):
        g = np.array(im.convert("L"))
        f = 600 / max(g.shape[0], 1)
        return cv2.resize(g, None, fx=f, fy=f, interpolation=cv2.INTER_AREA if f < 1 else cv2.INTER_CUBIC)
    A, B = prep(a), prep(b)
    orb = cv2.ORB_create(1500)
    ka, da = orb.detectAndCompute(A, None)
    kb, db = orb.detectAndCompute(B, None)
    if da is None or db is None or len(ka) < 8 or len(kb) < 8:
        return 0
    pairs = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(da, db, k=2)
    good = [m[0] for m in pairs if len(m) == 2 and m[0].distance < 0.8 * m[1].distance]
    if len(good) < 8:
        return 0
    src = np.float32([ka[g.queryIdx].pt for g in good]).reshape(-1, 1, 2)
    dst = np.float32([kb[g.trainIdx].pt for g in good]).reshape(-1, 1, 2)
    _, mask = cv2.findHomography(src, dst, cv2.RANSAC, 6.0)
    return int(mask.sum()) if mask is not None else 0


def _without_footer(image, keep=0.9):
    """The image without its bottom tenth."""
    w, h = image.size
    return image.crop((0, 0, w, max(1, int(h * keep))))


def cover_match_index(pages, cv_image, tolerance=COVER_OK):
    """Index of a page that matches the Comic Vine cover; recognize front/back wraparound scans.
    The quick dHash decides first; a page it rejects gets the feature comparison before it counts as a mismatch."""
    if not pages or pages[0] is None:
        return None
    ref = dhash(cv_image)
    ref_trim = dhash(_without_footer(cv_image))
    scored = []
    for i, page in enumerate(pages):
        if page is None:
            continue
        score = distance(dhash(page), ref)
        # a digital scan often carries a copyright line along the bottom edge that Comic Vine's cover does not: compare without the footer too
        score = min(score, distance(dhash(_without_footer(page)), ref_trim))
        # Some complete-cover scans are landscape front/back spreads. The front cover is on the right.
        if i == 0 and page.width / max(page.height, 1) >= 1.2:
            half = page.width // 2
            score = min(score,
                        distance(dhash(page.crop((0, 0, half, page.height))), ref),
                        distance(dhash(page.crop((half, 0, page.width, page.height))), ref))
            # Some scanned covers are wide pages whose original art was rotated
            # in Comic Vine's thumbnail. Compare both orientations too.
            for angle in (90, 270):
                score = min(score, distance(dhash(page.rotate(angle, expand=True)), ref))
        scored.append((score, i))
    hits = [(score, i) for score, i in scored if score <= tolerance]
    if hits:
        return min(hits)[1]
    best = max(((feature_inliers(page, cv_image), i) for i, page in enumerate(pages) if page is not None), default=(0, None))
    return best[1] if best[0] >= COVER_FEATURES_OK else None


def cover_verdict(pages, cv_image, tolerance=COVER_OK):
    """'ok' | 'later-page' | 'mismatch' | 'unreadable'."""
    if not pages or pages[0] is None:
        return "unreadable"
    index = cover_match_index(pages, cv_image, tolerance)
    if index == 0:
        return "ok"
    if index is not None:
        return "later-page"
    return "mismatch"


def promote_cover_page(path, cover_index):
    """Move an exact Comic Vine-matched later image to the front of a CBZ without changing image bytes."""
    path = Path(path)
    if cover_index <= 0 or not tagcommon.is_cbz(path):
        return cover_index == 0
    names = sorted((n for n in tagcommon.list_names(path) if IMAGE.search(n) and "__macosx" not in n.lower()),
                   key=lambda n: [_safe_int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", n)])
    if cover_index >= len(names):
        return False
    source = names[cover_index]
    parent = source.rpartition("/")[0]
    ext = Path(source).suffix.lower()
    prefix = f"{parent + '/' if parent else ''}!000000-front-cover"
    new_name = f"{prefix}{ext}"
    used = set(tagcommon.list_names(path))
    n = 1
    while new_name in used:
        new_name = f"{prefix}-{n}{ext}"
        n += 1

    def fix_page_indexes(xml):
        pattern = re.compile(r"(<Page\b[^>]*\bImage=[\"'])(\d+)([\"'][^>]*>)", re.I)
        def replace(match):
            old_index = int(match.group(2))
            new_index = 0 if old_index == cover_index else old_index + 1 if old_index < cover_index else old_index
            return f"{match.group(1)}{new_index}{match.group(3)}"
        return pattern.sub(replace, xml)

    tmp = path.with_name(path.name + ".coverfix.tmp")
    try:
        with zipfile.ZipFile(path, "r") as zin, zipfile.ZipFile(tmp, "w") as zout:
            for item in zin.infolist():
                info = copy.copy(item)
                if item.filename == source:
                    info.filename = new_name
                data = zin.read(item)
                if item.filename.lower() == "comicinfo.xml":
                    data = fix_page_indexes(data.decode("utf-8", "replace")).encode("utf-8")
                zout.writestr(info, data, compress_type=info.compress_type)
        with zipfile.ZipFile(tmp) as check:
            bad = check.testzip()
            if bad:
                raise zipfile.BadZipFile(f"archive integrity check failed at {bad}")
        os.replace(tmp, path)
        return True
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False

def cv_cover(cv_key, issue_id, issue=None):
    import urllib.request
    from PIL import Image
    info = (issue or tag_collected._get(cv_key, f"issue/4000-{issue_id}/", field_list="image"))["image"]
    req = urllib.request.Request(info["medium_url"], headers={"User-Agent": "komga-comic-tools"})
    return Image.open(io.BytesIO(urllib.request.urlopen(req, timeout=30).read()))


def chronology_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def chronology_content_override(cfg, path, series, title):
    """Return a curated chronology map only for the exact, verified source archive."""
    expected_series = tag_collected.norm(series)
    expected_title = tag_collected.norm(title)
    for override in cfg.get("chronology_content_overrides", []):
        if (tag_collected.norm(override.get("series")) != expected_series
                or tag_collected.norm(override.get("title")) != expected_title):
            continue
        if (int(override.get("size") or -1) != Path(path).stat().st_size
                or str(override.get("sha256") or "").casefold() != chronology_sha256(path).casefold()):
            continue
        return override
    return None


def resolve_chronology_override(cv_key, override):
    """Load the configured exact issue IDs and recheck each against Comic Vine metadata."""
    issues = []
    for expected in override.get("issues", []):
        issue_id = str(expected.get("id") or "")
        if not issue_id.isdigit():
            return None
        issue = tag_collected._get(
            cv_key, f"issue/4000-{issue_id}/",
            field_list="id,name,issue_number,cover_date,volume,image") or {}
        volume = issue.get("volume") or {}
        if (str(issue.get("id") or "") != issue_id
                or str(issue.get("issue_number") or "") != str(expected.get("number") or "")
                or str(issue.get("cover_date") or "") != str(expected.get("cover_date") or "")
                or (volume.get("id") and str(volume["id"]) != str(expected.get("volume_id")))
                or (volume.get("name") and tag_collected.norm(volume["name"]) != tag_collected.norm(expected.get("volume_name")))
                or (expected.get("cv_title") and tag_collected.norm(issue.get("name") or "")
                    != tag_collected.norm(expected["cv_title"]))):
            return None
        issues.append({**issue,
                       "volume_name": expected["volume_name"],
                       "volume_id": expected["volume_id"],
                       "toc_title": expected.get("page_marker") or issue.get("name") or ""})
    return issues if len(issues) == len(override.get("issues", [])) and len(issues) >= 2 else None


def verify_chronology_override_pages(pages, contents, override, first_start):
    """Check the curated page map, story-title markers, rear covers, and segment lengths."""
    if len(pages) != int(override.get("page_count") or -1):
        return None, None, None, f"page count changed from curated {override.get('page_count')}"
    start_pages = [int(value) - 1 for value in override.get("start_pages", [])]
    end = int(override.get("end_before_page") or 0) - 1
    markers = list(override.get("page_markers") or [])
    if len(start_pages) != len(contents) or len(markers) != len(contents):
        return None, None, None, "curated issue/page map is incomplete"
    if not start_pages or start_pages[0] != first_start or start_pages != sorted(set(start_pages)):
        return None, None, None, "curated first issue cover or page order changed"
    if end <= start_pages[-1] or end > len(pages):
        return None, None, None, "curated content end is outside the archive"
    bounds = list(zip(start_pages, start_pages[1:] + [end]))
    lengths = [stop - start for start, stop in bounds]
    if lengths != [int(value) for value in override.get("segment_lengths", [])]:
        return None, None, None, f"curated section lengths changed: {lengths}"
    evidence = []
    for (start, stop), issue, marker in zip(bounds, contents, markers):
        expected = tag_collected.norm(marker)
        section_text = tag_collected.norm(" ".join(" ".join(page.get("text") or []) for page in pages[start:stop]))
        if not expected or expected not in section_text:
            return None, None, None, f"curated OCR marker {marker!r} was not found for {issue.get('volume_name')} #{issue.get('issue_number')}"
        evidence.append({"issue": issue["id"], "page": pages[start]["i"],
                         "signals": ["exact archive SHA-256", "curated page map", "OCR title marker"],
                         "title_marker": marker})
    cover_pages = list(override.get("cover_pages") or [])
    for item in cover_pages:
        page_index = int(item.get("page") or 0) - 1
        issue_number = str(item.get("number") or "")
        if (page_index < end or page_index >= len(pages)
                or issue_number not in tag_collected.norm(" ".join(pages[page_index].get("text") or [])).split()):
            return None, None, None, f"rear cover issue number {issue_number} was not verified on page {item.get('page')}"
    return start_pages, evidence, end, ""


# ---------------------------------------------------------------- in-file title and tags
def issue_id_of(path):
    return tagcommon.comicvine_issue_id(path)


def is_partial_story_excerpt(path):
    """Story-letter Spider-Verse CBZs and the extras bundle are excerpts, not whole issues."""
    name = Path(path).name
    if not re.search(r"spider[- ]verse", name, re.I):
        return False
    return bool(re.search(r"\b[A-F]\s+story\b", name, re.I) or re.search(r"\bextras?\b", name, re.I))


def archive_page_signature(path):
    """Hash page images while ignoring ComicInfo and other archive metadata."""
    with zipfile.ZipFile(path) as zf:
        images = [item for item in zf.infolist()
                  if not item.is_dir() and IMAGE.search(item.filename)]
        if not images:
            return None
        images.sort(key=lambda item: [_safe_int(token) if token.isdigit() else token.casefold()
                                      for token in re.split(r"(\d+)", item.filename)])
        return tuple((Path(item.filename).suffix.lower(), hashlib.sha256(zf.read(item)).hexdigest())
                     for item in images)


def intentional_extract_duplicate(path, cfg):
    """Recognize the specifically curated Spider-Verse extracts already present in their reading-order folder."""
    entries = cfg.get("intentional_extract_duplicates", {})
    try:
        relative = Path(path).resolve().relative_to(Path(cfg["incoming"]).resolve()).as_posix().casefold()
    except (KeyError, OSError, ValueError):
        return None
    item = next((value for name, value in entries.items()
                 if str(name).replace("\\", "/").casefold() == relative), None)
    if item is None:
        return None
    target = Path(item["destination"])
    if not target.is_file():
        return {"status": "held", "reason": f"intentional reading-order extract has no existing destination at {target}; preserve for manual review"}
    try:
        if archive_page_signature(path) == archive_page_signature(target):
            return {"status": "duplicate", "reason": f"same page images as the curated Spider-Verse reading-order copy at {target}; existing Komga item is preserved"}
    except (OSError, zipfile.BadZipFile):
        pass
    return {"status": "held", "reason": f"intentional reading-order extract does not match the curated Spider-Verse copy at {target}; preserve for manual review"}


def path_override(path, cfg, section):
    """Look up exact Incoming-relative policy by normalized path."""
    try:
        relative = Path(path).resolve().relative_to(Path(cfg["incoming"]).resolve()).as_posix().casefold()
    except (KeyError, OSError, ValueError):
        return None
    for configured_path, value in cfg.get(section, {}).items():
        if str(configured_path).replace("\\", "/").casefold() == relative:
            return value
    # Event-pack folders sometimes differ only by a missing or curly apostrophe
    # between the downloaded directory and a manually entered policy key. Permit
    # that harmless typography drift only when it resolves to one policy entry;
    # ambiguous normalized paths remain unmatched and therefore fail safely.
    def apostrophe_insensitive(value):
        return str(value).replace("\\", "/").replace("’", "").replace("'", "").casefold()

    normalized = apostrophe_insensitive(relative)
    candidates = [value for configured_path, value in cfg.get(section, {}).items()
                  if apostrophe_insensitive(configured_path) == normalized]
    if len(candidates) == 1:
        return candidates[0]
    return None


def path_year_hint(path, cfg):
    """Return an explicitly configured year for an incoming-relative path."""
    try:
        relative = Path(path).resolve().relative_to(Path(cfg["incoming"]).resolve()).as_posix()
    except (KeyError, OSError, ValueError):
        return None
    for item in cfg.get("path_year_hints", []):
        if re.search(item.get("match", r"$^"), relative, re.I):
            year = _safe_int(str(item.get("year") or ""))
            return year if 1900 <= year <= 2100 else None
    return None


def path_route_hint(path, cfg):
    """Return an explicit route for a staging subpath, such as an event-pack folder."""
    try:
        relative = Path(path).resolve().relative_to(Path(cfg["incoming"]).resolve()).as_posix()
    except (KeyError, OSError, ValueError):
        return None
    # A chronology breakout folder names the source chronology, not the issue's
    # series. Never inherit its collection; the caller routes by the verified
    # Comic Vine volume (then the configured auto-series router).
    for item in cfg.get("path_route_patterns", []):
        if re.search(item.get("match", r"$^"), relative, re.I):
            return {k: item[k] for k in ("collection", "library", "lists", "collection_list") if k in item}
    return None


def path_story_arc_hint(path, cfg):
    """Return a Comic Vine story arc ID configured for this staging subpath."""
    try:
        relative = Path(path).resolve().relative_to(Path(cfg["incoming"]).resolve()).as_posix()
    except (KeyError, OSError, ValueError):
        return None
    for item in cfg.get("path_story_arc_hints", []):
        if re.search(item.get("match", r"$^"), relative, re.I):
            return _safe_int(str(item.get("story_arc_id") or "")) or None
    return None


def override_issue_id(path, cfg):
    """Look up a verified issue ID by exact Incoming-relative path; never guess from pack ordinals."""
    value = path_override(path, cfg, "issue_overrides")
    return int(value) if value is not None else None

def comicvine_issue_id(book):
    """Get the Comic Vine issue ID from a Komga book's canonical issue link or tags."""
    metadata = book.get("metadata") or {}
    for link in metadata.get("links") or []:
        match = re.search(r"comicvine\.gamespot\.com/.*?/4000-(\d+)", str(link.get("url") or ""), re.I)
        if match:
            return int(match.group(1))
    for tag in metadata.get("tags") or []:
        match = re.fullmatch(r"cv:4000-(\d+)", str(tag).strip(), re.I)
        if match:
            return int(match.group(1))
    return None


class IssueInventory(set):
    """Comic Vine issue IDs plus a conservative series/number/year fallback for unlinked Komga books."""
    def __init__(self):
        super().__init__()
        self.issue_keys = set()


def issue_identity_key(series, number, date):
    series = re.sub(r"\s*\((?:19|20)\d{2}\)\s*$", "", str(series or "")).strip()
    num = str(number or "").strip().lstrip("#")
    m = re.fullmatch(r"(-?)0*(\d+)(\.\d+)?", num)
    if not series or not m or not re.match(r"^(?:19|20)\d{2}", str(date or "")):
        return None
    number_key = f"{m.group(1)}{int(m.group(2))}{m.group(3) or ''}"
    return tag_collected.norm(series), number_key, str(date)[:4]


def komga_issue_identity(book):
    metadata = book.get("metadata") or {}
    return issue_identity_key(book.get("seriesTitle") or metadata.get("series") or "",
                              metadata.get("number") or metadata.get("numberSort"),
                              metadata.get("releaseDate"))


def existing_issue_ids(cfg):
    """Snapshot Comic Vine issue IDs already indexed by Komga. None means the duplicate check could not run."""
    key = os.environ.get("KOMGA_API_KEY")
    if not key:
        return None
    komga = komga_title_fix.Komga(cfg.get("komga_url", "http://localhost:25600"), key)
    ids = IssueInventory()
    for lib in komga.call("/libraries"):
        for book in komga.paged(f"/books?library_id={lib['id']}"):
            if not book.get("deleted"):
                issue_id = comicvine_issue_id(book)
                if issue_id:
                    ids.add(issue_id)
                key = komga_issue_identity(book)
                if key:
                    ids.issue_keys.add(key)
    return ids


def unpack_packages(incoming, done, say=print):
    """Expand outer .zip download packs, extracting only .cbz/.cbr comic files into isolated folders."""
    incoming, done = Path(incoming), Path(done)
    skip = {(incoming / "_ready").resolve(), done.resolve(), (incoming / "_state").resolve()}
    packages = [p for p in sorted(incoming.rglob("*")) if p.is_file() and p.suffix.lower() in PACKAGE_SUFFIXES
                and not any(parent.resolve() in skip for parent in p.parents)]
    extracted = 0
    for package in packages:
        try:
            with zipfile.ZipFile(package) as zf:
                members = [m for m in zf.infolist() if not m.is_dir() and Path(m.filename.replace("\\", "/")).suffix.lower() in COMIC_SUFFIXES]
                if not members:
                    say(f"  HELD package: {package.name} (no .cbz/.cbr comics found)")
                    continue
                root = (incoming / "_packages" / package.stem).resolve()
                root.mkdir(parents=True, exist_ok=True)
                for member in members:
                    rel = Path(member.filename.replace("\\", "/"))
                    if rel.is_absolute() or any(part in ("", ".", "..") for part in rel.parts):
                        raise ValueError(f"unsafe archive path: {member.filename}")
                    target = (root / rel).resolve()
                    if root != target and root not in target.parents:
                        raise ValueError(f"archive path escapes staging folder: {member.filename}")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    tmp = target.with_name(target.name + ".extracting")
                    with zf.open(member) as src, tmp.open("wb") as dst:
                        shutil.copyfileobj(src, dst, length=1024 * 1024)
                    if tmp.stat().st_size != member.file_size:
                        tmp.unlink(missing_ok=True)
                        raise IOError(f"size check failed for {member.filename}")
                    os.replace(tmp, target)
                    extracted += 1
            dest = done / "_packages" / package.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(package), str(dest))
            say(f"  unpacked: {package.name} ({len(members)} comic file(s))")
        except Exception as e:  # noqa: BLE001
            say(f"  HELD package: {package.name} (unpack failed: {e})")
    return extracted


def set_title(path, title):
    """Write <Title> into the archive's root ComicInfo.xml (everything else in it stays)."""
    from xml.sax.saxutils import escape
    raw = tagcommon.read_raw_root_info(path)
    if not raw:
        return False
    if re.search(r"<Title>[^<]*</Title>", raw):
        new = re.sub(r"<Title>[^<]*</Title>", lambda m: f"<Title>{escape(title)}</Title>", raw, count=1)
    else:
        new = raw.replace("</ComicInfo>", f"  <Title>{escape(title)}</Title>\n</ComicInfo>")
    if new == raw:
        return True
    tagcommon.write_root_info(path, new)
    return tagcommon.read_tags(path).get("Title") == title


# ---------------------------------------------------------------- stage 1: prepare
def approved_names(cfg):
    """Approved cover basenames or exact Incoming-relative paths, one per line."""
    try:
        text = (Path(cfg["incoming"]) / "_state" / "approved_covers.txt").read_text(encoding="utf-8")
    except OSError:
        return set()
    return {line.strip().replace("\\", "/").casefold() for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")}


def cover_is_approved(path, cfg):
    """Match a reviewed cover by basename or exact Incoming-relative path."""
    approved = approved_names(cfg)
    candidates = {Path(path).name.casefold()}
    try:
        candidates.add(Path(path).resolve().relative_to(Path(cfg["incoming"]).resolve()).as_posix().casefold())
    except (KeyError, OSError, ValueError):
        pass
    return bool(candidates & approved)



def archive_converted_source(path):
    """Move an original CBR into the recoverable done archive after a verified CBZ conversion."""
    path = Path(path)
    incoming = next((parent for parent in path.parents if (parent / "_state").is_dir()), path.parent)
    try:
        relative = path.relative_to(incoming)
    except ValueError:
        relative = Path(path.name)
    archive = incoming / "_done" / "converted_sources" / relative
    archive.parent.mkdir(parents=True, exist_ok=True)
    if archive.exists():
        archive = archive.with_name(f"{archive.stem}-{time.strftime('%Y%m%d%H%M%S')}{archive.suffix}")
    shutil.move(str(path), str(archive))
    return archive


def undecodable_pages(folder, cut_off_ok=False):
    """Names of the pages under `folder` that cannot be opened as images. With cut_off_ok, a page whose data stops early still counts as
    readable (it shows what was saved of it): that is what a failed checksum usually leaves."""
    from PIL import Image, ImageFile
    bad = []
    before = ImageFile.LOAD_TRUNCATED_IMAGES
    ImageFile.LOAD_TRUNCATED_IMAGES = bool(cut_off_ok)
    try:
        for f in sorted(Path(folder).rglob("*")):
            if f.is_file() and IMAGE.search(f.name):
                try:
                    with Image.open(f) as im:
                        im.load()
                except Exception:                                                    # noqa: BLE001
                    bad.append(f.name)
    finally:
        ImageFile.LOAD_TRUNCATED_IMAGES = before
    return bad


def checksum_errors(output):
    """The page names UnRAR reports as 'checksum error' (the rest of the archive was fine)."""
    return re.findall(r"^(.+?) - checksum error\s*$", output or "", re.M)


def ensure_cbz(path, say=print):
    path = Path(path)
    if path.suffix.lower() != ".cbr":
        return path
    cbz = path.with_suffix(".cbz")
    import tempfile

    # A previous interrupted conversion may leave a partial extraction tree.
    # Never reuse a predictable path: on Windows, a locked/stale tree can make
    # rmtree silently fail and the next mkdir then turns into a false hold.
    legacy_tmp = path.parent / f".tmp_cbr_{path.stem}"
    try:
        if legacy_tmp.exists():
            shutil.rmtree(legacy_tmp)
    except OSError:
        pass  # the unique scratch path below remains safe even if cleanup is blocked
    legacy_cbz_tmp = cbz.with_name(f".{cbz.name}.tmp")
    try:
        legacy_cbz_tmp.unlink(missing_ok=True)
    except OSError:
        pass
    tmp = Path(tempfile.mkdtemp(prefix=".tmp_cbr_", dir=path.parent))
    fd, cbz_tmp_name = tempfile.mkstemp(prefix=f".{cbz.name}.", suffix=".tmp", dir=cbz.parent)
    os.close(fd)
    cbz_tmp = Path(cbz_tmp_name)
    seven_zip = r"C:\Program Files\7-Zip\7z.exe"
    import subprocess

    def extract(command):
        return subprocess.run(command, capture_output=True, text=True, errors="replace",
                              creationflags=subprocess.CREATE_NO_WINDOW)

    try:
        attempts = []
        if Path(seven_zip).exists():
            result = extract([seven_zip, "x", str(path), f"-o{tmp}", "-y"])
            attempts.append(("7-Zip", result))
        else:
            attempts.append(("7-Zip", None))

        # Some RAR variants are readable by WinRAR's UnRAR but not 7-Zip.
        # Retry with the installed RAR extractor before treating the source as bad.
        # Its overwrite switch allows reuse of the unique scratch tree after a
        # partial 7-Zip extraction without deleting/recreating a Windows path.
        if not attempts[0][1] or attempts[0][1].returncode != 0:
            if tagcommon.UNRAR and Path(tagcommon.UNRAR).exists():
                result = extract([tagcommon.UNRAR, "x", "-y", "-o+", str(path), str(tmp) + os.sep])
                attempts.append(("UnRAR", result))

        successful = next(((name, result) for name, result in attempts
                           if result is not None and result.returncode == 0), None)
        if not successful and tagcommon.UNRAR and Path(tagcommon.UNRAR).exists():
            # A RAR whose only fault is a failed checksum on a page or two: keep the broken pages (-kb) and use them when they still decode.
            # Without this the whole issue stayed held although 20 of 21 pages were fine and the 21st opens.
            unrar_text = " ".join((r.stdout or "") + "\n" + (r.stderr or "") for _, r in attempts if r is not None)
            tested = extract([tagcommon.UNRAR, "t", "-y", str(path)])
            faults = checksum_errors((tested.stdout or "") + "\n" + (tested.stderr or ""))
            if faults:
                shutil.rmtree(tmp, ignore_errors=True)
                tmp.mkdir(parents=True, exist_ok=True)
                kept = extract([tagcommon.UNRAR, "x", "-kb", "-y", "-o+", str(path), str(tmp) + os.sep])
                attempts.append(("UnRAR keep-broken", kept))
                images = sorted(f.name for f in tmp.rglob("*") if f.is_file() and IMAGE.search(f.name))
                unreadable = undecodable_pages(tmp, cut_off_ok=True)
                cut_off = undecodable_pages(tmp)                 # pages that open only partly
                # accepted: every page opens at least partly, at most two are cut off, and none of them is the first page (the cover)
                if images and not unreadable and len(cut_off) <= 2 and images[0] not in cut_off:
                    say(f"  salvaged {path.name}: kept {len(faults)} page(s) with a failed checksum"
                        + (f"; cut off: {', '.join(cut_off)}" if cut_off else "; all of them open"))
                    successful = ("UnRAR keep-broken", kept)
        if not successful:
            diagnostics = []
            for name, result in attempts:
                if result is None:
                    diagnostics.append(f"{name} is unavailable")
                    continue
                detail = clean_tool_output(result.stdout + " " + result.stderr)
                diagnostics.append(f"{name} exit {result.returncode}" + (f": {detail[-400:]}" if detail else ""))
            raise ArchiveConversionError("CBR extraction failed; " + "; ".join(diagnostics))

        extracted = sorted(f for f in tmp.rglob("*") if f.is_file())
        if not extracted or not any(IMAGE.search(f.name) for f in extracted):
            raise ArchiveConversionError("CBR extraction produced no readable comic pages")
        with zipfile.ZipFile(cbz_tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for f in extracted:
                zf.write(f, arcname=f.relative_to(tmp))
        with zipfile.ZipFile(cbz_tmp) as check:
            bad_member = check.testzip()
            if bad_member:
                raise ArchiveConversionError(f"converted CBZ failed its ZIP integrity check at {bad_member}")
        os.replace(cbz_tmp, cbz)
        shutil.rmtree(tmp, ignore_errors=True)
        archive_converted_source(path)
    except ArchiveConversionError:
        shutil.rmtree(tmp, ignore_errors=True)
        cbz_tmp.unlink(missing_ok=True)
        raise
    except Exception as e:  # noqa: BLE001
        shutil.rmtree(tmp, ignore_errors=True)
        cbz_tmp.unlink(missing_ok=True)
        raise ArchiveConversionError(f"CBR conversion failed: {e}") from e
    return cbz


# Crossover chapters in event packs are named after the issue they are read with:
# "Thor 444b (Captain America #398).cbz" or "Thor 468a - Silver Surfer 086.cbz". Neither names a series Comic Vine can search by
# the file's own title, so they are found by the series and number in the name.
TIEIN_NAME = (re.compile(r"^(?P<parent>.+?) (?P<pn>\d{2,4})[a-z] \((?P<series>.+?) #?0*(?P<num>\d+)\)$", re.I),
              re.compile(r"^(?P<parent>.+?) (?P<pn>\d{2,4})[a-z] - (?P<series>.+?) 0*(?P<num>\d+)$", re.I))
TIEIN_MONTHS = 4          # a tie-in's cover date may differ this much from the issue it is read with


def _month_gap(a, b):
    return abs((int(a[:4]) - int(b[:4])) * 12 + int(a[5:7]) - int(b[5:7]))


def tiein_issue_id(cv_key, path):
    """Comic Vine issue ID of a crossover chapter named after another series, or None when it is not certain.
    The series is looked up by name and the issue by number. When the issue it is read with (its sibling file in the same folder)
    is tagged, only a candidate dated within TIEIN_MONTHS of it counts; without that, only an unambiguous candidate does."""
    stem = Path(path).stem
    m = next((x.match(stem) for x in TIEIN_NAME if x.match(stem)), None)
    if not m:
        return None
    series, number = m.group("series").strip(), int(m.group("num"))
    when = None
    for sibling in sorted(Path(path).parent.glob(f"{m.group('parent')} {m.group('pn')} *")):
        sibling_id = tagcommon.comicvine_issue_id(sibling) if sibling.is_file() else None
        if sibling_id:
            when = month_from_cover_date(tag_collected._get(cv_key, f"issue/4000-{sibling_id}/", field_list="cover_date").get("cover_date"))
            break
    base = re.sub(r"^the\s+", "", series.replace("&amp;", "&"), flags=re.I)
    wanted = {tag_collected.norm(x) for x in (series, base, "The " + base, base.replace("&", "and"), "The " + base.replace("&", "and"))}
    hits = {}
    for name in dict.fromkeys((series, base, base.replace("&", "and"))):
        for volume in tag_collected.search_volumes(cv_key, name):
            if tag_collected.norm(volume.get("name")) not in wanted:
                continue
            for issue in tag_collected.volume_issues(cv_key, volume["id"], number):
                date = month_from_cover_date(issue.get("cover_date"))
                if date and (when is None or _month_gap(date, when) <= TIEIN_MONTHS):
                    hits[issue["id"]] = date
    return int(next(iter(hits))) if len(hits) == 1 else None


def cbr_integrity_problem(path):
    """Read-only archive test for CBR files, used by previews before conversion."""
    import subprocess

    path = Path(path)
    seven_zip = r"C:\Program Files\7-Zip\7z.exe"
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    attempts = []

    def test(name, command):
        try:
            result = subprocess.run(command, capture_output=True, text=True,
                                    errors="replace", creationflags=no_window)
            attempts.append((name, result.returncode, result.stdout + " " + result.stderr))
        except OSError as exc:
            attempts.append((name, None, str(exc)))

    if Path(seven_zip).exists():
        test("7-Zip", [seven_zip, "t", str(path)])
    else:
        attempts.append(("7-Zip", None, "not installed"))
    if attempts[-1][1] != 0 and tagcommon.UNRAR and Path(tagcommon.UNRAR).exists():
        test("UnRAR", [tagcommon.UNRAR, "t", str(path)])

    if any(code == 0 for _, code, _ in attempts):
        return None
    diagnostics = []
    for name, code, output in attempts:
        detail = clean_tool_output(output, limit=300)
        diagnostics.append(f"{name} " + (f"exit {code}" if code is not None else "unavailable")
                           + (f": {detail[-300:]}" if detail else ""))
    return "CBR archive integrity check failed; " + "; ".join(diagnostics)

def match_from_comicinfo(cv_key, path, tags, cfg, policy_path=None):
    """Retry Comic Vine matching with embedded Series metadata; only trust issue/year when the file is a single issue."""
    series = str((tags or {}).get("Series") or "").strip()
    if not series:
        return None
    filename = Path(path).name
    looks_collected = bool(COLLECTED_FORMATS.search(" ".join((filename, str((tags or {}).get("Format") or "")))) or NAMED_VOLUME.search(filename) or BY_VOLUME.search(filename))
    year = path_year_hint(policy_path or path, cfg)
    if not year:
        year = _safe_int(str((tags or {}).get("Year") or ""))
    # A collected book's embedded Number is its edition number, never an issue number.
    if looks_collected:
        return None
    issue_num = str((tags or {}).get("Number") or "").strip().lstrip("#")
    return tag_collected.match(cv_key, path, aliases=cfg.get("aliases"), year_hint=year,
                               story_arc_id=path_story_arc_hint(policy_path or path, cfg),
                               series_hint=series, issue_hint=issue_num or None)


def chronology_part_info(name, base):
    """Return (part, total) for a titled story part, including cross-series `N of M` names."""
    words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
             "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
    wanted = tag_collected.norm(base).split()
    actual = tag_collected.norm(name).split()
    if not wanted or len(actual) < len(wanted):
        return None
    for at in range(len(actual) - len(wanted) + 1):
        if actual[at:at + len(wanted)] != wanted:
            continue
        tail = actual[at + len(wanted):]
        if tail and tail[0] == "part":
            tail = tail[1:]
        if not tail:
            continue
        part = words.get(tail[0], _safe_int(tail[0]))
        if not part:
            continue
        total = None
        if len(tail) >= 3 and tail[1] == "of":
            total = words.get(tail[2], _safe_int(tail[2])) or None
        if total and (part > total or total > 30):
            continue
        return part, total
    return None


def chronology_part_number(name, base):
    """Backward-compatible part-number accessor used by older local workflows."""
    info = chronology_part_info(name, base)
    return info[0] if info else None


def chronology_base_part_title(title):
    """Remove a trailing `Part N` from a chronology label so issue-title searches can find the full run."""
    return re.sub(r"\s*[,;:–—-]?\s+part\s+(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)(?:\s+of\s+\d+)?\s*$",
                  "", str(title or ""), flags=re.I).strip(" .,:;-")


def resolve_chronology_parts(cv_key, title):
    """Resolve a named event from Comic Vine issue names, including parts spread across series."""
    if not title:
        return None
    base_title = chronology_base_part_title(title)
    search_titles = list(dict.fromkeys([title, base_title]))
    results_by_id = {}
    for search_title in search_titles:
        for issue in (tag_collected._get(cv_key, "search/", query=search_title, resources="issue", limit=100,
                                         field_list="id,name,issue_number,volume,cover_date,image") or []):
            if issue.get("id"):
                results_by_id[str(issue["id"])] = issue
        found_parts = {info[0] for it in results_by_id.values()
                       if (info := chronology_part_info(it.get("name") or "", base_title or title))}
        if len(found_parts) >= 2:
            break
    results = list(results_by_id.values())
    groups = {}
    cross_parts = {}
    for issue in results:
        info = chronology_part_info(issue.get("name") or "", base_title or title)
        volume = issue.get("volume") or {}
        if not info or not volume.get("id") or not issue.get("id"):
            continue
        part, total = info
        if total:
            cross_parts.setdefault(total, {}).setdefault(part, []).append(issue)
        groups.setdefault(str(volume["id"]), {"volume": volume, "parts": {}})["parts"].setdefault(part, []).append(issue)
    matches = []
    for group in groups.values():
        parts = group["parts"]
        if len(parts) < 2 or sorted(parts) != list(range(1, max(parts) + 1)):
            continue
        chosen = []
        for part in sorted(parts):
            candidates = {int(it["id"]): it for it in parts[part]}
            if len(candidates) != 1:
                chosen = []
                break
            chosen.append(next(iter(candidates.values())))
        nums = [_safe_int(str(it.get("issue_number") or "")) for it in chosen]
        if not chosen or any(not n for n in nums) or nums != list(range(nums[0], nums[0] + len(nums))):
            continue
        if any(not month_from_cover_date(it.get("cover_date")) for it in chosen):
            continue
        if [it.get("cover_date") for it in chosen] != sorted(it.get("cover_date") for it in chosen):
            continue
        matches.append([{**it, "volume_name": group["volume"].get("name") or "",
                         "volume_id": group["volume"]["id"]} for it in chosen])
    # Event parts often alternate between Superman, Adventures, Steel, Action, etc.  In
    # that case Comic Vine's issue numbers are not consecutive in any one volume. Accept
    # only one distinct issue for every explicitly declared part, with cover dates in order.
    for total, parts in cross_parts.items():
        if sorted(parts) != list(range(1, total + 1)):
            continue
        chosen, ambiguous = [], False
        for part in range(1, total + 1):
            by_id = {int(it["id"]): it for it in parts[part]}
            if len(by_id) != 1:
                ambiguous = True
                break
            issue = next(iter(by_id.values()))
            volume = issue.get("volume") or {}
            issue["volume_name"] = volume.get("name") or ""
            issue["volume_id"] = volume.get("id")
            chosen.append(issue)
        if ambiguous or not chosen or any(not month_from_cover_date(it.get("cover_date")) for it in chosen):
            continue
        dates = [it["cover_date"] for it in chosen]
        if dates != sorted(dates):
            continue
        ids = [int(it["id"]) for it in chosen]
        if len(ids) == len(set(ids)):
            matches.append(chosen)
    unique_matches = {tuple(int(issue["id"]) for issue in group): group for group in matches}
    if len(unique_matches) == 1:
        return next(iter(unique_matches.values()))
    if len(unique_matches) > 1:
        matches = list(unique_matches.values())

    # Many collected story arcs are catalogued as a Comic Vine trade issue
    # (for example, "Volume 2: The Villain's Journey") rather than issues named
    # "Part 1", "Part 2". Use that record's explicit Collects range and resolve
    # it through the canonical volume issue index; never infer a range from the
    # chronology's own ordinal or publication year.
    targets = {tag_collected.norm(title)}
    collected_records = []
    for candidate in results:
        name = tag_collected.norm(candidate.get("name") or "")
        name = tag_collected.norm(re.sub(r"^volume\s+\d+\s+", "", name))
        if name not in targets:
            continue
        try:
            full = tag_collected._get(cv_key, f"issue/4000-{candidate['id']}/",
                                      field_list="id,name,description,volume,issue_number,cover_date") or {}
            segments = breakout.parse_collects(full.get("description") or "")
            if len(segments) != 1:
                continue
            series_name, year, first, last = segments[0]
            volume_ids = {str((item.get("volume") or {}).get("id")) for item in results
                          if target in tag_collected.norm(item.get("name") or "")
                          and (item.get("volume") or {}).get("id")}
            volumes = [volume for volume in tag_collected.search_volumes(cv_key, series_name)
                       if tag_collected.norm(volume.get("name") or "") == tag_collected.norm(series_name)]
            if year:
                volumes = [volume for volume in volumes
                           if _safe_int(str(volume.get("start_year") or "")) == year]
            if volume_ids:
                volumes = [volume for volume in volumes if str(volume.get("id")) in volume_ids]
            resolved = []
            for volume in volumes:
                by_number = {}
                for issue in tag_collected.volume_issues(cv_key, volume["id"]):
                    number = str(issue.get("issue_number") or "").strip()
                    if re.fullmatch(r"0*\d+", number):
                        by_number.setdefault(int(number), issue)
                if all(number in by_number for number in range(first, last + 1)):
                    issues = [by_number[number] for number in range(first, last + 1)]
                    if all(issue.get("id") and month_from_cover_date(issue.get("cover_date")) for issue in issues):
                        resolved.append([{**issue, "volume_name": volume.get("name") or series_name,
                                          "volume_id": volume["id"]} for issue in issues])
            if len(resolved) != 1 and not volume_ids:
                fallback = breakout.resolve_contents(cv_key, segments, tag_collected.search_volumes,
                                                     tag_collected.volume_issues)
                if fallback:
                    resolved.append(fallback)
            issues = resolved[0] if len(resolved) == 1 else None
            if not issues or len(issues) < 2:
                continue
            collected_records.append(issues)
        except Exception:  # noqa: BLE001
            continue
    unique = {tuple(int(issue["id"]) for issue in issues): issues for issues in collected_records}
    return next(iter(unique.values())) if len(unique) == 1 else None


CHRONOLOGY_MONTHS = {"jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
                     "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
                     "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
                     "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12}


def chronology_toc_references(pages, max_pages=16):
    """Extract explicit `first appeared in <series> issue #N, released Month YYYY` TOC entries."""
    month_re = "|".join(sorted(CHRONOLOGY_MONTHS, key=len, reverse=True))
    ref_re = re.compile(
        rf"first\s*appeared\s*in\s*(?P<series>[A-Za-z0-9&’'./ \-]{{1,100}}?)\s*"
        rf"issue\s*#?\s*(?P<number>\d{{1,4}}(?:\.\d+)?)\s*[,.:]?\s*released\s*"
        rf"(?P<month>{month_re})\s*[,.:]?\s*(?P<year>19\d{{2}}|20\d{{2}})", re.I)
    text = " ".join(" ".join(page.get("text") or []) for page in (pages or [])[:max_pages])
    refs, seen = [], set()
    for match in ref_re.finditer(text):
        series = re.sub(r"\s+", " ", match.group("series")).strip(" .,:;-/")
        number = match.group("number").strip()
        month = CHRONOLOGY_MONTHS[match.group("month").lower()]
        year = int(match.group("year"))
        if not series or not 1 <= _safe_int(number.split(".")[0]) <= 9999:
            continue
        key = (tag_collected.norm(series), number, year, month)
        if key in seen:
            continue
        seen.add(key)
        prefix = text[max(0, match.start() - 240):match.start()]
        quotes = re.findall(r"[\"“]([^\"”]{3,160})[\"”]", prefix)
        toc_title = quotes[-1].strip() if quotes else ""
        # The nearest unmatched quote can span the previous entry and its
        # creator credits. Such a fragment is not a usable story-title hint.
        if (len(toc_title) > 100
                or re.search(r"first\s*appeared|issue\s*#?|released|\bwriter\b|penciller|inker", toc_title, re.I)):
            toc_title = ""
        refs.append({"series": series, "number": number, "release_year": year,
                     "release_month": month, "toc_title": toc_title})
    title_counts = {}
    for ref in refs:
        title = tag_collected.norm(ref.get("toc_title") or "")
        if title:
            title_counts[title] = title_counts.get(title, 0) + 1
    for ref in refs:
        title = tag_collected.norm(ref.get("toc_title") or "")
        if title and title_counts.get(title, 0) > 1:
            # A repeated nearest-quote is usually an OCR association with the
            # wrong neighboring TOC entry; keep issue/date matching and omit
            # that weak title hint.
            ref["toc_title"] = ""
    return refs if len(refs) >= 2 else None


def chronology_series_query(series):
    """Repair common OCR token concatenation before searching Comic Vine."""
    value = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", str(series or ""))
    value = re.sub(r"\b(Adventures|Chronicles|Legends|Tales|World)of\b", r"\1 of", value, flags=re.I)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def chronology_series_candidates(series):
    """Return narrowly scoped search spellings for common TOC OCR artifacts."""
    primary = chronology_series_query(series)
    variants = [primary]
    without_article = re.sub(r"^The\s+", "", primary, flags=re.I)
    if without_article != primary:
        variants.append(without_article)
    # Comic Vine omits the leading article for several long-running series.
    if re.match(r"^Adventures of Superman$", primary, re.I):
        variants.append("Adventures of Superman")
    return list(dict.fromkeys(v for v in variants if v))


def chronology_issue_number_key(value):
    """Compare issue numbers independent of leading zeroes and OCR fractions."""
    value = str(value or "").strip().lstrip("#")
    value = value.replace("½", ".5").replace("¼", ".25").replace("¾", ".75")
    match = re.fullmatch(r"0*(\d+)(\.\d+)?", value)
    if not match:
        return tag_collected.norm(value)
    whole = str(int(match.group(1)))
    fraction = (match.group(2) or "").rstrip("0").rstrip(".")
    return whole + fraction


def issue_number_sort_key(value):
    """Sort ordinary and fractional issue numbers without crashing on malformed metadata."""
    text = str(value or "").strip().replace("½", ".5").replace("¼", ".25").replace("¾", ".75")
    match = re.match(r"^\s*#?\s*(\d+(?:\.\d+)?)(.*)$", text)
    if match:
        try:
            return (0, float(match.group(1)), tag_collected.norm(match.group(2)))
        except (TypeError, ValueError, OverflowError):
            pass
    return (1, tag_collected.norm(text))


def chronology_pack_title(path):
    """Read the exact named series hint from a numbered chronology-volume filename."""
    stem = re.sub(r"\s*\(v\d{4}-\d{2}\)\s*$", "", Path(path).stem, flags=re.I)
    match = re.search(r"\bVolume\s+\d+\s*[-–—]\s*(.+)$", stem, re.I)
    return match.group(1).strip(" .-_") if match else ""


def chronology_arc_key(value):
    """Normalize a chronology label and common Comic Vine story-arc prefixes/suffixes."""
    value = re.sub(r"\s*\((?:19|20)\d{2}\)\s*$", "", str(value or "")).strip()
    value = re.sub(r"^(?:superman|adventures of superman|action comics)\s*:\s*", "", value, flags=re.I)
    return tag_collected.norm(value)


def resolve_chronology_story_arc(cv_key, title):
    """Resolve a uniquely named Comic Vine story arc to dated issue records.

    The source collection names many packs after the arc, not after an issue
    named "Part 1". Comic Vine's story_arc record supplies the candidate issue
    set. Search article/punctuation variants and accept a fuzzy name only when
    it is strongly separated from every other result. Archive page-boundary
    verification remains mandatory before a split.
    """
    if not title:
        return None
    query_variants = [title]
    without_article = re.sub(r"^(?:the|a|an)\s+", "", title, flags=re.I).strip()
    if without_article and without_article.casefold() != title.casefold():
        query_variants.append(without_article)
    results_by_id = {}
    for query in dict.fromkeys(query_variants):
        for item in (tag_collected._get(cv_key, "search/", query=query, resources="story_arc", limit=100,
                                        field_list="id,name") or []):
            if item.get("id"):
                results_by_id[str(item["id"])] = item
        if any(chronology_arc_key(item.get("name")) == chronology_arc_key(title)
               for item in results_by_id.values()):
            break
    if not results_by_id:
        return None
    target = chronology_arc_key(title)
    ranked = sorted(((1.0 if chronology_arc_key(item.get("name")) == target else
                      SequenceMatcher(None, target, chronology_arc_key(item.get("name"))).ratio(), arc_id, item)
                     for arc_id, item in results_by_id.items()), key=lambda row: row[0], reverse=True)
    if not ranked or ranked[0][0] < 0.90:
        return None
    exact = [row for row in ranked if row[0] == 1.0]
    if len(exact) == 1:
        best = exact[0]
    elif exact:
        return None
    elif (len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 0.12):
        return None
    else:
        # Short generic names such as "Doomsday" need an exact catalog match.
        if len(target.split()) < 3 or len(target) < 15:
            return None
        best = ranked[0]
    arc_id = best[1]
    issue_ids = tag_collected._story_arc_issue_ids(cv_key, arc_id)
    if not issue_ids or len(issue_ids) < 2 or len(issue_ids) > 40:
        return None
    issues = []
    for issue_id in sorted(issue_ids):
        issue = tag_collected._get(cv_key, f"issue/4000-{issue_id}/",
                                   field_list="id,name,issue_number,cover_date,volume") or {}
        volume = issue.get("volume") or {}
        if (not issue.get("id") or not volume.get("id") or not volume.get("name")
                or not issue.get("issue_number") or not month_from_cover_date(issue.get("cover_date"))):
            return None
        issues.append({**issue, "volume_name": volume["name"], "volume_id": volume["id"]})
    ids = [int(issue["id"]) for issue in issues]
    if len(ids) != len(set(ids)):
        return None
    # Pack splitting follows the user's issue-date chronology. Keep a stable
    # tie-break for same-month releases; the boundary check verifies that order.
    issues.sort(key=lambda issue: (issue.get("cover_date") or "", tag_collected.norm(issue.get("volume_name") or ""),
                                   chronology_issue_number_key(issue.get("issue_number")), int(issue["id"])))
    return issues


def chronology_issue_summary(issue):
    """Keep the exact issue identity needed by the reacquisition report."""
    return {"id": issue.get("id"), "series": issue.get("volume_name") or (issue.get("volume") or {}).get("name"),
            "number": issue.get("issue_number"), "cover_date": issue.get("cover_date"),
            "title": issue.get("toc_title") or issue.get("name")}


def resolve_chronology_named_volume(cv_key, title):
    """Resolve a chronology title only when it uniquely names a dated Comic Vine issue volume."""
    if not title:
        return None
    valid = []
    seen = set()
    for volume in tag_collected.search_volumes(cv_key, title) or []:
        volume_id = str(volume.get("id") or "")
        if (not volume_id or volume_id in seen
                or tag_collected.norm(volume.get("name") or "") != tag_collected.norm(title)):
            continue
        seen.add(volume_id)
        issues = tag_collected.volume_issues(cv_key, volume_id)
        numbers = [chronology_issue_number_key(issue.get("issue_number")) for issue in issues]
        dates = [issue.get("cover_date") or "" for issue in issues]
        if (len(issues) < 2 or any(not issue.get("id") for issue in issues)
                or len(set(numbers)) != len(numbers)
                or any(not re.fullmatch(r"\d+(?:\.\d+)?", number) for number in numbers)
                or any(not month_from_cover_date(date) for date in dates)
                or dates != sorted(dates)):
            continue
        valid.append([{**issue, "volume_name": volume.get("name") or title,
                       "volume_id": volume_id} for issue in issues])
    return valid[0] if len(valid) == 1 else None


def resolve_chronology_toc(cv_key, references):
    """Resolve OCR TOC references to exact Comic Vine issues using series, number and near-release date."""
    resolved = []
    volume_cache = {}
    issue_cache = {}
    for ref in references or []:
        series_variants = chronology_series_candidates(ref["series"])
        norm_series = tag_collected.norm(series_variants[0])
        if norm_series not in volume_cache:
            exact = []
            searched = {}
            for variant in series_variants:
                for volume in (tag_collected.search_volumes(cv_key, variant) or []):
                    searched[str(volume.get("id"))] = volume
            volumes = list(searched.values())
            normalized_variants = {tag_collected.norm(v) for v in series_variants}
            exact = [v for v in volumes if tag_collected.norm(v.get("name") or "") in normalized_variants]
            if not exact:
                ranked = sorted(((max(SequenceMatcher(None, tag_collected.norm(variant), tag_collected.norm(v.get("name") or "")).ratio()
                                        for variant in series_variants), v) for v in volumes),
                                key=lambda x: x[0], reverse=True)
                # OCR may concatenate words or drop a leading article. Accept a
                # unique strong spelling only; issue number and publication date
                # still have to identify exactly one issue below.
                exact = [ranked[0][1]] if ranked and ranked[0][0] >= 0.84 and (len(ranked) == 1 or ranked[0][0] - ranked[1][0] >= 0.05) else []
            volume_cache[norm_series] = exact
        release_month = ref["release_year"] * 12 + ref["release_month"]
        possible = []
        for volume in volume_cache[norm_series]:
            vid = str(volume.get("id") or "")
            if not vid:
                continue
            if vid not in issue_cache:
                issue_cache[vid] = tag_collected.volume_issues(cv_key, vid)
            wanted = chronology_issue_number_key(ref["number"])
            for issue in issue_cache[vid]:
                if chronology_issue_number_key(issue.get("issue_number")) != wanted:
                    continue
                cover_month = month_from_cover_date(issue.get("cover_date"))
                if not cover_month:
                    continue
                y, m = map(int, cover_month.split("-"))
                delta = y * 12 + m - release_month
                # TOCs generally record on-sale month; Comic Vine records the cover month.
                if -1 <= delta <= 3:
                    possible.append(({**issue, "volume_name": volume.get("name") or series,
                                      "volume_id": volume["id"], "toc_title": ref.get("toc_title") or "",
                                      "toc_release_date": f"{ref['release_year']}-{ref['release_month']:02d}"}, delta))
        if not possible:
            return None
        # If two similarly titled volumes overlap the same issue number/date, require a decisive TOC title match.
        if len(possible) > 1 and ref.get("toc_title"):
            scored = sorted(((SequenceMatcher(None, tag_collected.norm(ref["toc_title"]),
                                                tag_collected.norm(item.get("name") or "")).ratio(), item, delta)
                             for item, delta in possible), key=lambda x: (x[0], -abs(x[2])), reverse=True)
            if scored[0][0] >= 0.82 and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.08):
                possible = [(scored[0][1], scored[0][2])]
        ids = {int(item["id"]) for item, _ in possible if item.get("id")}
        if len(ids) != 1:
            return None
        resolved.append(possible[0][0])
    ids = [int(item["id"]) for item in resolved]
    if len(ids) < 2 or len(ids) != len(set(ids)):
        return None
    dates = [item.get("cover_date") or "" for item in resolved]
    if any(not month_from_cover_date(x) for x in dates):
        return None
    # TOC OCR follows the visual column layout, which is frequently not reading
    # order. The list's contract is chronological issue dates; order by CV cover
    # date, preserving TOC order for same-date issues. Page-boundary verification
    # below independently rejects any sequence that does not match the archive.
    return [item for _, item in sorted(enumerate(resolved), key=lambda pair: (pair[1].get("cover_date") or "", pair[0]))]


def chronology_title_candidate(pages, cursor, issue_name):
    """Match a named story title even when Comic Vine lists multiple stories in one issue."""
    words = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
             "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10"}
    variants = []
    for title in re.split(r"\s*;\s*", issue_name or ""):
        clauses = [title] + [clause.strip() for clause in title.split(":") if clause.strip()]
        for clause in clauses:
            normalized_clause = tag_collected.norm(clause)
            # OCR frequently drops the space in TOC labels such as
            # "Bleeding Kansas Part1"; normalize that before part-number
            # comparison so the title page can still verify the boundary.
            normalized_clause = re.sub(r"\bpart(\d+)\b", r"part \1", normalized_clause)
            expected = normalized_clause.split()
            if "part" in expected:
                at = expected.index("part")
                if at + 1 < len(expected):
                    expected[at + 1] = words.get(expected[at + 1], expected[at + 1])
            at = expected.index("part") if "part" in expected else -1
            if at >= 0:
                distinctive = [token for token in expected[at + 2:] if len(token) >= 4]
                if not distinctive:
                    distinctive = [token for token in expected[:at] if len(token) >= 4
                                   and token not in {"with", "from", "into", "over", "under", "journey"}]
            else:
                distinctive = [token for token in expected if len(token) >= 4
                               and token not in {"with", "from", "into", "over", "under", "journey"}]
            if len(distinctive) >= 2:
                variants.append((expected, distinctive))
    for n in range(max(0, cursor), len(pages)):
        text = tag_collected.norm(" ".join(pages[n].get("text") or ""))
        text = re.sub(r"\bpt\b", "part", text)
        actual = text.split()
        filename = tag_collected.norm(Path(pages[n].get("file") or "").stem)
        if "part" in actual:
            at = actual.index("part")
            if at + 1 < len(actual):
                actual[at + 1] = words.get(actual[at + 1], actual[at + 1])
        for expected, distinctive in variants:
            matched = sum(1 for token in expected if token in actual or (len(token) >= 5 and token in text))
            score = matched / max(1, len(expected))
            part_at = expected.index("part") if "part" in expected else -1
            missing_part_ordinal = (part_at >= 0 and part_at + 1 < len(expected)
                                    and expected[part_at + 1].isdigit()
                                    and expected[part_at + 1] not in actual and "part" in actual)
            threshold = 0.74 if missing_part_ordinal else 0.82
            if score >= threshold and all(token in actual or token in text for token in distinctive):
                return n, score
            file_score = sum(1 for token in expected if token in filename.split()) / max(1, len(expected))
            if (len(distinctive) >= 3 and file_score >= 0.82
                    and all(token in filename.split() for token in distinctive)):
                return n, file_score
    return None


def chronology_ocr_title_candidate(pages, cursor, issue_name):
    """Find sparse title pages when OCR has a minor spelling error."""
    expected = [word for word in tag_collected.norm(issue_name).split()
                if len(word) >= 4 and word not in {"this", "that", "with", "from", "into", "over", "under", "like", "part", "issue"}]
    if not expected:
        return None
    best = None
    for n in range(max(0, cursor), len(pages)):
        words = tag_collected.norm(" ".join(pages[n].get("text") or [])).split()
        # Keep this OCR-tolerant fallback restricted to sparse title/credits
        # pages so ordinary dialogue cannot identify a boundary by coincidence.
        if not words or len(words) > max(18, len(expected) * 5):
            continue
        scores = [max((1.0 if token in word or word in token else SequenceMatcher(None, token, word).ratio()
                       for word in words), default=0.0) for token in expected]
        score = sum(scores) / len(scores)
        if score >= 0.86 and (best is None or score > best[1]):
            best = (n, score)
    return best


def expand_chronology_pack(path, cfg, cv_key, tags, existing_ids, say=print, dry_run=False):
    """Split a custom chronology pack only when its named multi-issue story is fully identified and page boundaries verify."""
    series = str((tags or {}).get("Series") or "").strip()
    title = str((tags or {}).get("Title") or "").strip()
    if "chronology" not in series.casefold():
        return None
    if existing_ids is None:
        return {"status": "held", "reason": "Komga duplicate inventory is unavailable; chronology contents were not split"}
    from PIL import Image
    manual_override = chronology_content_override(cfg, path, series, chronology_pack_title(path))
    contents = resolve_chronology_override(cv_key, manual_override) if manual_override else None
    if manual_override and not contents:
        return {"status": "held", "reason": "curated chronology issue IDs no longer agree with Comic Vine metadata",
                "reacquire_kind": "source_review", "reacquire_query": chronology_pack_title(path)}
    if contents:
        say(f"  chronology exact-source map resolved {len(contents)} dated issue(s) for {Path(path).name}")
    else:
        contents = resolve_chronology_parts(cv_key, title)
    # Verify the archive's own boundaries even when every issue ID is already present;
    # a title alone never authorizes retiring the source pack as a confirmed overlap.
    python = cfg.get("ocr_python")
    if not python or not Path(python).exists():
        return {"status": "held", "reason": "OCR interpreter is unavailable for chronology issue-boundary verification"}
    parent_route, _, _ = route_for(cfg, series)
    if not parent_route:
        return {"status": "held", "reason": f"no chronology collection route for {series!r}"}
    parent_collection = parent_route.get("collection")
    if not parent_collection:
        return {"status": "held", "reason": f"chronology route for {series!r} has no collection"}
    incoming, state_dir = Path(cfg["incoming"]), Path(cfg["incoming"]) / "_state"
    work = Path(tempfile.mkdtemp(prefix="komga-chronology-preview-")) if dry_run else state_dir / "breakout_work"
    work.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1((str(Path(path).resolve()) + str(Path(path).stat().st_mtime_ns)).encode()).hexdigest()[:12]
    data = None
    out_dir = incoming / "_breakout" / f"Chronology - {sanitize(parent_collection)}" / sanitize(Path(path).stem)
    created = []
    try:
        ocr_json = work / f"chronology-{key}.json"
        if ocr_json.exists():
            data = json.loads(ocr_json.read_text(encoding="utf-8"))
        # Older cache files have no `complete` field but contain OCR text on
        # every page. Recognize those as complete to avoid repeating their work.
        if data and data.get("pages") and not any(page.get("ocr_skipped") for page in data["pages"]):
            data["complete"] = True
        # Start with the TOC pages and hashes for every page. If the Comic Vine
        # covers prove all issue boundaries, this is enough to split the pack;
        # only missing cover evidence triggers full-book OCR.
        if not data or not Path(data.get("tmp") or "").is_dir() or (not data.get("complete") and not data.get("toc_only")):
            data = breakout.run_ocr(path, python, Path(__file__).with_name("breakout_ocr.py"), ocr_json, toc_only=True)
        pages = data["pages"]
        unreadable = [page.get("i") for page in pages if page.get("decode_error")]
        if unreadable:
            return {"status": "held", "reason": f"OCR could not decode source pages {unreadable[:12]}",
                    "reacquire_kind": "unreadable_source", "reacquire_query": chronology_pack_title(path)}
        # A title search can find only the named crossover arc inside a larger
        # chronology volume. When its own TOC yields a complete dated issue map,
        # use that explicit contents list instead of splitting only the subset
        # that happened to match the cover title.
        refs = chronology_toc_references(pages)
        toc_contents = resolve_chronology_toc(cv_key, refs) if refs and not manual_override else None
        if toc_contents and (not contents or len(toc_contents) >= len(contents)):
            contents = toc_contents
            say(f"  chronology TOC resolved {len(contents)} dated issue reference(s) for {Path(path).name}")
        if not contents and not manual_override:
            arc_contents = resolve_chronology_story_arc(cv_key, chronology_pack_title(path))
            if arc_contents:
                contents = arc_contents
                say(f"  chronology story arc resolved {len(contents)} dated issue(s) for {Path(path).name}")
        if not contents and not manual_override:
            if not toc_contents:
                named_contents = resolve_chronology_named_volume(cv_key, chronology_pack_title(path))
                if named_contents:
                    contents = named_contents
                    say(f"  chronology exact title resolved {len(contents)} dated issue(s) for {Path(path).name}")
            if not contents and not toc_contents:
                return {"status": "held", "reason": f"Comic Vine did not resolve {title!r}; no unambiguous dated issue list could be recovered from the chronology contents page",
                        "reacquire_kind": "contents_unmatched", "reacquire_query": chronology_pack_title(path),
                        "unmatched_toc_references": refs or []}
        existing_keys = getattr(existing_ids, "issue_keys", set())
        missing = [issue for issue in contents
                   if int(issue["id"]) not in existing_ids
                   and issue_identity_key(issue.get("volume_name"), issue.get("issue_number"), issue.get("cover_date")) not in existing_keys]
        def held_with_acquisition(reason):
            # The exact contents list can safely identify gaps even when page
            # boundaries cannot prove a split. Keep the source pack held, but
            # let the normal acquisition stage fill only those missing singles.
            if missing and not dry_run:
                queued = breakout.record_wanted(
                    state_dir, Path(path).name, missing,
                    f"chronology contents identified; source retained because split was not verified: {reason}")
                say(f"  chronology split held; queued {queued} exact missing issue(s) for acquisition")
            return {"status": "held", "reason": reason,
                    "reacquire_kind": "issue_boundaries_unverified" if missing else "source_review",
                    "reacquire_query": chronology_pack_title(path),
                    "reacquire_issues": [chronology_issue_summary(issue) for issue in missing]}
        cover_images, cover_hashes, cover_positions = {}, {}, {}
        manual_cover_page_by_id = {}
        if manual_override:
            manual_cover_page_by_id = {
                int(item["id"]): int(item["page"]) - 1
                for item in manual_override.get("cover_pages", [])
            }
        for issue in contents:
            issue_id = int(issue["id"])
            if manual_override and issue_id in manual_cover_page_by_id:
                page_index = manual_cover_page_by_id[issue_id]
                if page_index < len(pages):
                    cover_positions[issue_id] = page_index
                    cover_images[issue_id] = Image.open(Path(data["tmp"]) / pages[page_index]["file"]).convert("RGB")
                continue
            if manual_override and issue_id not in {int(item["id"]) for item in missing}:
                continue
            try:
                image = cv_cover(cv_key, issue["id"], issue)
                cover_images[issue_id] = image
                cover_hashes[issue_id] = dhash(image)
                scores = sorted((breakout.hamming(page["dh"], cover_hashes[issue_id]), n)
                                for n, page in enumerate(pages) if page.get("dh") is not None)
                if scores and scores[0][0] <= breakout.COVER_TOLERANCE:
                    rivals = [d for d, n in scores[1:] if abs(n - scores[0][1]) >= 3]
                    if not rivals or rivals[0] >= scores[0][0] + breakout.COVER_MARGIN:
                        cover_positions[issue_id] = scores[0][1]
            except Exception:  # noqa: BLE001
                continue
        if data.get("toc_only") and any(int(issue["id"]) not in cover_positions for issue in contents):
            data = breakout.run_ocr(path, python, Path(__file__).with_name("breakout_ocr.py"), ocr_json, toc_only=False)
            pages = data["pages"]
            unreadable = [page.get("i") for page in pages if page.get("decode_error")]
            if unreadable:
                return held_with_acquisition(f"OCR could not decode source pages {unreadable[:12]}")
            say(f"  chronology cover match incomplete; ran full title-page OCR for {Path(path).name}")
        # ComicInfo's FrontCover marker anchors the first reprint. For each later issue,
        # accept a verified Comic Vine cover or a distinctive OCR title page (or both).
        try:
            raw = tagcommon.read_raw_root_info(path)
            front = re.search(r'<Page\b(?=[^>]*\bImage=["\'](\d+)["\'])(?=[^>]*\bType=["\']FrontCover["\'])[^>]*/?>', raw or "", re.I)
            first_start = int(front.group(1)) + 1 if front else 0
        except Exception:  # noqa: BLE001
            first_start = 0
        if not pages or first_start < 0 or first_start >= len(pages):
            return held_with_acquisition("archive has no readable first-issue boundary")
        if manual_override:
            starts, evidence, end, boundary_error = verify_chronology_override_pages(
                pages, contents, manual_override, first_start)
            if boundary_error:
                return held_with_acquisition(boundary_error)
        else:
            starts = [first_start]
            evidence = [{"issue": contents[0]["id"], "page": pages[first_start]["i"], "signals": ["ComicInfo front cover boundary"]}]
            cursor = first_start + 1
        for issue in (contents[1:] if not manual_override else []):
            issue_id = int(issue["id"])
            cover_n = cover_positions.get(issue_id)
            if cover_n is not None and cover_n < cursor:
                cover_n = None
            title_hits = []
            title_names = list(dict.fromkeys([issue.get("toc_title") or "", issue.get("name") or ""]))
            for title_priority, candidate_title in enumerate(filter(None, title_names)):
                strict = chronology_title_candidate(pages, cursor, candidate_title)
                if strict:
                    title_hits.append((strict[1], strict[0], "OCR title page", title_priority))
                else:
                    # OCR can split words or drop punctuation. This scorer also rejects
                    # dialogue pages and only accepts a high-confidence title match.
                    fuzzy = breakout.title_candidate(pages, cursor, candidate_title)
                    if fuzzy and fuzzy[1] >= 0.90:
                        title_hits.append((fuzzy[1], fuzzy[0], "OCR title page (fuzzy)", title_priority))
                    elif not fuzzy:
                        noisy = chronology_ocr_title_candidate(pages, cursor, candidate_title)
                        if noisy:
                            title_hits.append((noisy[1], noisy[0], "OCR title page (OCR-tolerant)", title_priority))
            # An explicit title in the archive's own TOC identifies the story
            # more specifically than Comic Vine's broader issue title. Prefer
            # that signal when both land on different pages; ties among hits
            # from the same source still hold for review.
            title_hits.sort(key=lambda hit: (hit[3], -hit[0], hit[1]))
            title_n = None
            if title_hits:
                best_score, best_n, _, best_priority = title_hits[0]
                conflicting = [hit for hit in title_hits[1:] if hit[3] == best_priority
                               and hit[1] != best_n and best_score - hit[0] < 0.08]
                if conflicting and (cover_n is None or all(abs(hit[1] - cover_n) > breakout.AGREE_PAGES for hit in conflicting)):
                    return held_with_acquisition(f"ambiguous OCR title-page matches for {issue.get('volume_name')} #{issue.get('issue_number')}")
                title_n = best_n
            if cover_n is None and title_n is None:
                return held_with_acquisition(f"no Comic Vine cover or OCR title page found for {issue.get('volume_name')} #{issue.get('issue_number')} ({issue.get('name')})")
            if cover_n is not None and title_n is not None and abs(cover_n - title_n) > breakout.AGREE_PAGES:
                return held_with_acquisition(f"Comic Vine cover and OCR title page disagree for {issue.get('volume_name')} #{issue.get('issue_number')}")
            start_n = min(n for n in (cover_n, title_n) if n is not None)
            signals = []
            if cover_n is not None:
                signals.append("Comic Vine cover")
            if title_n is not None:
                hit = next((entry for entry in title_hits if entry[1] == title_n), None)
                signals.append(hit[2] if hit else "OCR title page")
            starts.append(pages[start_n]["i"])
            evidence.append({"issue": issue["id"], "page": pages[start_n]["i"], "signals": signals,
                             "cover_distance": next((breakout.hamming(pages[cover_n]["dh"], cover_hashes[issue_id])
                                                     for cover_n in [cover_n] if cover_n is not None), None),
                             "title_score": next((round(hit[0], 2) for hit in title_hits if hit[1] == title_n), None)})
            cursor = start_n + 1
        if not manual_override:
            for issue in contents:
                if int(issue["id"]) not in cover_positions and not any(e["issue"] == issue["id"] and any("title page" in s for s in e["signals"]) for e in evidence):
                    return held_with_acquisition(f"no independent cover/title evidence for {issue.get('volume_name')} #{issue.get('issue_number')}")
            if starts != sorted(set(starts)):
                return held_with_acquisition("chronology issue title pages are not in strictly increasing order")
            trailing_covers = [n for n in cover_positions.values() if n >= starts[-1]]
            end = min(trailing_covers) if trailing_covers else len(pages)
        bounds = list(zip(starts, starts[1:] + [end]))
        lengths = [b - a for a, b in bounds]
        if end <= starts[-1] or any(not (breakout.MIN_ISSUE_PAGES <= size <= breakout.MAX_ISSUE_PAGES) for size in lengths):
            return held_with_acquisition(f"verified section boundaries produce implausible issue lengths {lengths}")
        if dry_run:
            return {"status": "verified", "issues": len(missing), "of": len(contents), "lengths": lengths,
                    "evidence": evidence, "why": "dry run; no single issues were written"}
        cover_page_indices = set(cover_positions.values())
        missing_ids = {int(issue["id"]) for issue in missing}
        out_dir.mkdir(parents=True, exist_ok=True)
        for (start, stop), issue in zip(bounds, contents):
            if int(issue["id"]) not in missing_ids:
                continue
            stem = f"{sanitize(issue['volume_name'])} {int(issue['issue_number']):03d} ({issue['cover_date'][:4]})"
            single = out_dir / (stem + ".cbz")
            cover = io.BytesIO()
            issue_id = int(issue["id"])
            cover_image = cover_images.get(issue_id)
            if cover_image is None and issue_id in cover_positions:
                cover_file = Path(data["tmp"]) / pages[cover_positions[issue_id]]["file"]
                cover_image = Image.open(cover_file).convert("RGB")
            if cover_image is None:
                raise ValueError(f"no verified cover image available for {issue['volume_name']} #{issue['issue_number']}")
            cover_image.convert("RGB").save(cover, format="JPEG", quality=95)
            with zipfile.ZipFile(single, "w", zipfile.ZIP_STORED) as archive:
                archive.writestr("000-cover.jpg", cover.getvalue())
                page_no = 1
                for page in pages:
                    if start <= page["i"] < stop and page["i"] not in cover_page_indices:
                        archive.write(Path(data["tmp"]) / page["file"], f"{page_no:03d}{Path(page['file']).suffix.lower()}")
                        page_no += 1
            if page_no - 1 < breakout.MIN_ISSUE_PAGES:
                raise ValueError(f"split for {issue['volume_name']} #{issue['issue_number']} has too few story pages")
            created.append(single)
            if not tag_collected.tag(cfg.get("comictagger", "comictagger"), single, issue["id"]):
                raise ValueError(f"ComicTagger could not tag {issue['volume_name']} #{issue['issue_number']}")
            os.utime(single, (time.time() - 3600, time.time() - 3600))
        say(f"  chronology content match: {title} -> {len(contents)} Comic Vine issues; {len(created)} missing single(s) split and tagged")
        return {"status": "expanded", "issues": len(created), "of": len(contents),
                "issue_ids": [int(issue["id"]) for issue in contents], "evidence": evidence}
    except Exception as exc:  # noqa: BLE001
        for single in created:
            single.unlink(missing_ok=True)
        try:
            if out_dir.exists() and not any(out_dir.iterdir()):
                out_dir.rmdir()
        except OSError:
            pass
        transient = (isinstance(exc, (comicvine_store.ComicVineBudgetExceeded, urllib.error.URLError,
                                      TimeoutError, ConnectionError))
                     or isinstance(exc, urllib.error.HTTPError) and exc.code in (420, 429, 500, 502, 503, 504))
        # Do not turn a temporary Comic Vine/network failure into a permanent
        # boundary hold just because the contents list was already resolved.
        # Transient failures must be retried automatically and excluded from
        # the reacquisition report.
        if transient:
            return {"status": "held", "transient": True, "reason": f"chronology split verification failed: {exc}",
                    "reacquire_kind": "retry_later", "reacquire_query": chronology_pack_title(path)}
        if "missing" in locals() and "held_with_acquisition" in locals():
            return held_with_acquisition(f"chronology split verification failed: {exc}")
        return {"status": "held", "transient": transient,
                "reason": f"chronology split verification failed: {exc}",
                "reacquire_kind": "retry_later" if transient else "source_review",
                "reacquire_query": chronology_pack_title(path)}
    finally:
        if dry_run and data and data.get("tmp"):
            shutil.rmtree(data["tmp"], ignore_errors=True)
        if dry_run:
            shutil.rmtree(work, ignore_errors=True)


def metadata_collected_fallback(path, cfg, tags, say=print, existing_ids=None, cv_key=None, dry_run=False):
    """Route a clearly identified collected edition using its embedded ComicInfo metadata, without inventing a Comic Vine issue."""
    path = Path(path)
    series = str((tags or {}).get("Series") or "").strip()
    title = str((tags or {}).get("Title") or "").strip()
    number = str((tags or {}).get("Number") or "").strip().lstrip("#")
    publisher = str((tags or {}).get("Publisher") or "").strip()
    fmt = str((tags or {}).get("Format") or "").strip()
    filename = path.name
    if not (series and title and number and publisher and (fmt or COLLECTED_FORMATS.search(filename) or NAMED_VOLUME.search(filename))):
        return None
    # Only use a strong series agreement between the filename and ComicInfo. A subtitle can be a real, unrelated series.
    stem = re.sub(r"\s*\([^)]*\)\s*$", "", path.stem).strip()
    m = re.match(r"^(?P<series>.+?),?\s+(?:v|vol(?:ume)?)[ ._-]*0*(?P<num>\d{1,4})\b", stem, re.I)
    if m:
        file_series, file_number = m.group("series").rstrip(" ,"), m.group("num")
        if tag_collected.norm(file_series) != tag_collected.norm(series):
            return None
        # Custom chronology packs use ComicInfo Number for the sequence inside the chronology;
        # their filename's Volume N is a different counter and must not block content matching.
        if int(file_number) != _safe_int(number) and "chronology" not in series.casefold():
            return None
    elif tag_collected.norm(series) not in tag_collected.norm(stem):
        return None
    if publisher not in (cfg.get("publishers") or {}):
        return {"file": str(path), "status": "held", "reason": f"metadata-only collected edition has unsupported publisher {publisher!r}"}
    if "chronology" in series.casefold() and cv_key:
        expanded = expand_chronology_pack(path, cfg, cv_key, tags, existing_ids, say=say, dry_run=dry_run)
        if expanded and expanded.get("status") == "expanded":
            return {"file": str(path), **expanded}
        if expanded and expanded.get("status") == "verified":
            return {"file": str(path), "status": "expanded", **expanded}
        if expanded and expanded.get("status") == "held":
            return {"file": str(path), **expanded}
    # A custom collected-edition series/number is not a Comic Vine issue identity.
    # Do not route it until a contents manifest is verified against the existing single-issue IDs.
    # This prevents a fan chronology volume from duplicating issues already in another format.
    allowed = cfg.get("verified_collected_contents") or {}
    manifest_key = str(path.relative_to(Path(cfg["incoming"]))).replace("\\", "/")
    manifest = allowed.get(manifest_key)
    if not isinstance(manifest, dict) or not manifest.get("verified_issue_ids") or manifest.get("verified_no_overlap") is not True:
        return {"file": str(path), "status": "held",
                "reason": "collected edition has no Comic Vine match and its contents are not verified as non-overlapping; held to prevent duplicate stories"}
    manifest_ids = {_safe_int(str(issue_id)) for issue_id in manifest["verified_issue_ids"]}
    manifest_ids.discard(0)
    if not manifest_ids or existing_ids is None:
        return {"file": str(path), "status": "held",
                "reason": "collected-edition contents cannot be compared with the current Comic Vine issue inventory; held to prevent duplicates"}
    overlap = manifest_ids & {int(issue_id) for issue_id in existing_ids}
    if overlap:
        return {"file": str(path), "status": "held",
                "reason": f"collected edition overlaps {len(overlap)} issue(s) already in the library; split or review missing singles before routing"}
    route = path_route_hint(path, cfg)
    if not route:
        route, _, _ = route_for(cfg, series)
    if not route:
        route = auto_create_route(cfg, series, publisher, say=say, dry_run=bool(cfg.get("_dry_run")))
    if not route:
        return {"file": str(path), "status": "held", "reason": f"no collection route for metadata series {series!r} ({publisher}); auto route unavailable"}
    collection = route["collection"]
    target = Path(collection) / sanitize(series) / path.name
    library_root = Path((cfg.get("library_roots") or {}).get(route.get("library", collection), collection))
    destination = library_root / sanitize(series) / path.name
    if destination.exists():
        try:
            if destination.stat().st_size == path.stat().st_size and hashlib.sha256(destination.read_bytes()).digest() == hashlib.sha256(path.read_bytes()).digest():
                return {"file": str(path), "status": "duplicate", "reason": "identical metadata-only edition already exists at destination", "issue": None}
        except OSError:
            pass
        return {"file": str(path), "status": "held", "reason": f"destination filename collision for metadata-only edition: {destination}"}
    say(f"  metadata fallback: {series} #{number} ({publisher}); no Comic Vine issue ID, no chronology list anchor")
    return {"file": str(path), "status": "ready", "dest": str(target), "title": title,
            "volume": series, "number": number, "issue": None, "collection": collection,
            "library": route.get("library", collection), "anchor": None,
            "anchor_source": "collected-edition-metadata", "collected": True,
            "publisher": publisher, "cover_reordered": False, "metadata_fallback": True,
            "lists": sorted(set(route.get("lists") or []) | {site_list_name(master_base(cfg, publisher))})}


def prepare_file(path, cfg, cv_key, exe="comictagger", dry_run=False, say=print, existing_ids=None):
    path = Path(path)
    policy_path = path  # preserve exact Incoming-relative overrides across .cbr -> .cbz conversion
    # Exact issue overrides let us recognize already-present CBR scans before
    # attempting to unpack them; some duplicate archives have damaged pages.
    if path.suffix.lower() == ".cbr" and existing_ids is not None:
        early_issue = override_issue_id(path, cfg)
        if early_issue is None and tag_collected.already_tagged(path):
            early_issue = issue_id_of(path)
        if early_issue is not None and int(early_issue) in existing_ids:
            return {"file": str(path), "status": "duplicate",
                    "reason": "this Comic Vine issue is already present in Komga or in the completed pipeline state",
                    "issue": int(early_issue)}
    # A preview must never rewrite the incoming tree. Test CBR integrity in
    # place so the preview can still expose a broken archive before a live run
    # reaches its conversion/hold branch.
    if dry_run and path.suffix.lower() == ".cbr":
        problem = cbr_integrity_problem(path)
        if problem:
            return {"file": str(path), "status": "held", "reason": problem}
    else:
        path = ensure_cbz(path, say=say)
    # -> dict(status 'ready'|'held', reason, dest, title, volume, number, issue).
    # Every check happens before moving the file; held files stay in Incoming.
    out = {"file": str(path)}
    # Custom chronology books are collected editions, not Comic Vine single
    # issues. Resolve and verify their contents before normal issue matching;
    # otherwise Comic Vine searches the chronology title as though it were an
    # issue/series name and holds the book before the splitter is ever reached.
    chronology_tags = tagcommon.read_tags(path)
    chronology_series = str((chronology_tags or {}).get("Series") or "").strip()
    if "chronology" in chronology_series.casefold() and cv_key:
        expanded = expand_chronology_pack(path, cfg, cv_key, chronology_tags,
                                          existing_ids, say=say, dry_run=dry_run)
        if expanded and expanded.get("status") == "expanded":
            return {"file": str(path), **expanded}
        if expanded and expanded.get("status") == "verified":
            return {"file": str(path), "status": "expanded", **expanded}
        if expanded and expanded.get("status") == "held":
            return {"file": str(path), "status": "held", "reason": expanded["reason"]}
    curated_duplicate = intentional_extract_duplicate(path, cfg)
    if curated_duplicate:
        return {**out, **curated_duplicate}
    if is_partial_story_excerpt(path):
        return {**out, "status": "held", "reason": "partial Spider-Verse story excerpt/extras bundle; not a complete standalone issue"}
    override_id = override_issue_id(policy_path, cfg)
    tagged = tag_collected.already_tagged(path)
    # Exact path overrides cover sequence-numbered event packs and relaunch annuals.
    # Existing tags count only if they point to the same verified issue.
    if override_id is not None:
        issue_id = override_id
        tagged = issue_id_of(path) == issue_id
    elif tagged:
        issue_id = issue_id_of(path)
        if not issue_id:
            return {**out, "status": "held", "reason": "Comic Vine issue ID could not be read from the file"}
    else:
        m = tag_collected.match(cv_key, path, aliases=cfg.get("aliases"),
                                year_hint=path_year_hint(policy_path, cfg),
                                story_arc_id=path_story_arc_hint(policy_path, cfg))
        if m["status"] == "match":
            issue_id = m["issue"]["id"]
        else:
            issue_id = tiein_issue_id(cv_key, path)
            if not issue_id:
                tags = tagcommon.read_tags(path)
                corrected = match_from_comicinfo(cv_key, path, tags, cfg, policy_path)
                if corrected and corrected["status"] == "match":
                    issue_id = corrected["issue"]["id"]
                else:
                    fallback = metadata_collected_fallback(path, cfg, tags, say, existing_ids=existing_ids,
                                                           cv_key=cv_key, dry_run=dry_run)
                    if fallback:
                        return fallback
                    reason = corrected.get("why") if corrected else m["why"]
                    return {**out, "status": "held", "reason": f"not tagged: {reason}"}
    if existing_ids is not None and int(issue_id) in existing_ids:
        return {**out, "status": "duplicate", "reason": "this Comic Vine issue is already present in Komga", "issue": issue_id}
    issue = tag_collected._get(cv_key, f"issue/4000-{issue_id}/", field_list="name,issue_number,volume,cover_date,store_date,image")
    volume_name = (issue.get("volume") or {}).get("name") or ""
    issue_target = chronology_issue_summary({**issue, "volume_name": volume_name})
    vol = {}
    try:
        vol = tag_collected._get(cv_key, f"volume/4050-{(issue.get('volume') or {}).get('id')}/", field_list="publisher,start_year") or {}
    except Exception:                                                              # noqa: BLE001
        pass
    publisher = (vol.get("publisher") or {}).get("name") or "Unknown"
    # route
    route = path_override(policy_path, cfg, "route_overrides")
    anchor, anchor_source = None, None
    if not route:
        try:
            relative_path = Path(policy_path).resolve().relative_to(Path(cfg["incoming"]).resolve()).as_posix()
        except (KeyError, OSError, ValueError):
            relative_path = ""
        # Chronology paths are only a source hint; route their verified Comic Vine series.
        if re.match(r"^_breakout/chronology - [^/]+/", relative_path, re.I):
            route, anchor, anchor_source = route_for(cfg, volume_name)
        if not route:
            route = path_route_hint(policy_path, cfg)
    if not route:
        route, anchor, anchor_source = route_for(cfg, volume_name)
    if not route:
        route = auto_create_route(cfg, volume_name, publisher, say=print, dry_run=bool(cfg.get("_dry_run")))
    if not route and cfg.get("default_route"):
        coll = cfg["default_route"]["collection"].format(publisher=sanitize(publisher))
        route = {"collection": coll}
    if not route:
        return {**out, "status": "held", "reason": f"no collection route for the series {volume_name!r} ({publisher}); add one to the config", "issue": issue_id}
    date_override = path_override(policy_path, cfg, "date_overrides")
    if date_override is not None:
        anchor = month_from_cover_date(date_override)
        if not anchor:
            return {**out, "status": "held", "reason": f"invalid verified date override {date_override!r}; expected YYYY-MM", "issue": issue_id}
        anchor_source = "config.override"
    # Do not infer an issue's story date from a volume start year or from the collected book's publication date.
    # A collected edition is held even when Comic Vine has a cover_date: that date is the edition's release, not its contents.
    tags = tagcommon.read_tags(path)
    is_collected = collected_edition(path, tags, volume_name, issue)
    if is_collected and cfg.get("hold_collected_editions"):
        return {**out, "status": "held", "reason": "collected edition detected; split or review its contents before adding it to chronological issue lists", "issue": issue_id}
    if is_collected:
        # Trades, omnibuses and "Volume N" books are tagged, routed and uploaded like any comic. They carry no list anchor: their
        # release date is the edition's, not their contents', and the "!" reading lists hold single issues only (see update_lists).
        anchor, anchor_source = None, "collected-edition"
    elif not anchor:
        anchor = month_from_cover_date(issue.get("cover_date"))
        anchor_source = "comicvine.cover_date" if anchor else None
        if not anchor:
            # no cover date on Comic Vine: modern issues are cover-dated about two months after they go on sale
            sale = month_from_cover_date(issue.get("store_date"))
            if sale and sale >= "1990-01":
                year, month = int(sale[:4]), int(sale[5:7]) + 2
                anchor = f"{year + (month - 1) // 12:04d}-{(month - 1) % 12 + 1:02d}"
                anchor_source = "comicvine.store_date+2m"
    if not anchor and not is_collected:
        return {**out, "status": "held", "reason": "Comic Vine has no usable cover date; add a verified per-volume anchor to incoming_pipeline.json for manual review", "issue": issue_id}
    # cover
    cover_reordered = False
    cover_tolerance = max(0, min(64, int((cfg.get("cover_match_tolerances") or {}).get(route["collection"], COVER_OK))))
    if cover_is_approved(path, cfg) or (policy_path != path and cover_is_approved(policy_path, cfg)):
        # Exact paths in approved_covers.txt have already been visually reviewed.
        # Do not require another Comic Vine image request for a reviewed scan.
        say(f"  approved cover review: {path.relative_to(Path(cfg['incoming']))}")
        verdict = "ok"
    else:
        try:
            cv_image = cv_cover(cv_key, issue_id, issue)
            pages = first_pages(path, cover_pages_to_check(path))
            verdict = cover_verdict(pages, cv_image, cover_tolerance)
            if verdict == "later-page":
                matched_page = cover_match_index(pages, cv_image, cover_tolerance)
                if dry_run:
                    cover_reordered = True  # report the safe repair without writing in preview mode
                    verdict = "ok"  # the exact cover match will become page one when applied
                elif promote_cover_page(path, matched_page):
                    cover_reordered = True
                    verdict = cover_verdict(first_pages(path), cv_image, cover_tolerance)
                else:
                    verdict = "could not safely promote the verified cover page"
        except zipfile.BadZipFile:
            return {**out, "status": "held", "reason": "the archive is damaged or incomplete (it cannot be opened as a zip); download it again",
                    "issue": issue_id, "reacquire_kind": "unreadable_source", "issue_target": issue_target}
        except Exception as e:                                                              # noqa: BLE001
            verdict = f"unreadable ({e})"
    if verdict != "ok":
        why = {"later-page": "page one is not the cover (an ad or credits page?); the cover is a later page",
               "mismatch": "page one does not look like the Comic Vine cover"}
        return {**out, "status": "held", "reason": why.get(verdict, f"cover check: {verdict}"),
                "issue": issue_id, "reacquire_kind": "issue_match_found_cover_unverified", "issue_target": issue_target}
    issue_name = issue.get("name")
    if not issue_name:
        # Comic Vine has a few valid issue records with an empty name. Never
        # write or report the literal null value; retain a useful series/number
        # title so Komga and exported lists still identify the issue.
        issue_name = f"{volume_name} #{issue.get('issue_number')}"
    title = komga_title_fix.new_title(issue_name, volume_name, issue.get("issue_number")) or issue_name
    # all checks passed: now change the file
    if not dry_run:
        outcome = "ok" if tagged else tag_collected.tag_outcome(exe, path, issue_id)
        if outcome == "transient":
            # a Comic Vine rate limit or network error says nothing about the file: no hold is recorded, so the next run tries it again
            return {**out, "status": "held", "transient": True, "reason": "ComicTagger hit a Comic Vine rate limit or network error; retried on the next run", "issue": issue_id}
        if outcome != "ok":
            return {**out, "status": "held", "reason": "ComicTagger could not write the tags", "issue": issue_id}
        if title and not set_title(path, title):
            return {**out, "status": "held", "reason": "could not write the fixed title into the file", "issue": issue_id}
    dest = Path(route["collection"]) / sanitize(volume_name) / path.name
    return {**out, "status": "ready", "dest": str(dest), "title": title, "volume": volume_name, "number": issue.get("issue_number"),
            "issue": issue_id, "collection": route["collection"], "library": route.get("library", route["collection"]),
            "anchor": anchor, "anchor_source": anchor_source, "collected": bool(is_collected), "publisher": publisher, "cover_reordered": cover_reordered,
            "collection_list": route.get("collection_list"),
            "lists": sorted(set(route.get("lists") or []) | {site_list_name(master_base(cfg, publisher))})}


def _file_signature(path):
    stat = Path(path).stat()
    return [int(stat.st_size), int(stat.st_mtime_ns)]


def prepare_policy_fingerprint(cfg):
    """Invalidate cached holds when matching/routing policy or relevant code changes."""
    keys = ("aliases", "routes", "route_overrides", "issue_overrides", "date_overrides", "path_route_patterns",
            "path_year_hints", "path_story_arc_hints", "library_roots", "cover_match_tolerances", "metadata_collected_fallback", "verified_collected_contents",
            "chronology_content_overrides")
    policy = {key: cfg.get(key) for key in keys}
    approved = Path(cfg["incoming"]) / "_state" / "approved_covers.txt"
    try:
        policy["approved_covers"] = approved.read_text(encoding="utf-8")
    except OSError:
        policy["approved_covers"] = ""
    code = {}
    # A cached hold must be retried when any module involved in matching or
    # collected-edition verification changes. Omitting breakout.py meant a
    # splitter fix could remain dormant for the full weekly hold interval.
    for module_path in (__file__, tag_collected.__file__, tagcommon.__file__,
                        komga_title_fix.__file__, breakout.__file__, Path(__file__).with_name("breakout_ocr.py")):
        try:
            st = Path(module_path).stat()
            code[str(module_path)] = [st.st_size, st.st_mtime_ns]
        except OSError:
            code[str(module_path)] = None
    body = json.dumps([policy, code], sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def breakout_collected(cfg, cv_key, ready_path, r, existing_ids, say=print, dry_run=False):
    """A collected edition was just filed. Make sure the single issues it collects reach the single-issue reading lists in order.

    -> {"status": "covered" | "broken-out" | "wanted" | "unresolved", ...}.  See breakout.py for the rules; nothing is guessed:
    the split is written only when every issue's own title page was found, otherwise the missing issues are recorded in
    _state/acquisition_wanted.json (and, for a numbered "Vol. N" book, the book itself may stand in its list at its first issue)."""
    incoming = Path(cfg["incoming"])
    state_dir = incoming / "_state"
    issue = tag_collected._get(cv_key, f"issue/4000-{r['issue']}/", field_list="description,name,volume,cover_date")
    segments = breakout.parse_collects(issue.get("description"))
    if not segments:
        return {"status": "unresolved", "why": "Comic Vine does not say what this edition collects"}
    contents = breakout.resolve_contents(cv_key, segments, tag_collected.search_volumes, tag_collected.volume_issues)
    if not contents:
        return {"status": "unresolved", "why": "could not resolve what it collects to exact Comic Vine issues: " + "; ".join(f"{n} #{a}-{b}" for n, _, a, b in segments)}
    first_month = month_from_cover_date(contents[0].get("cover_date"))
    missing = breakout.missing_issues(contents, existing_ids)
    if not missing:
        return {"status": "covered", "issues": len(contents), "first_month": first_month}
    if dry_run:
        return {"status": "wanted", "why": f"would try to split out {len(missing)} of {len(contents)} issues", "first_month": first_month}
    python, helper = cfg.get("ocr_python"), Path(__file__).with_name("breakout_ocr.py")
    why = "no OCR interpreter configured (ocr_python)"
    if python and Path(python).exists():
        out_dir = incoming / "_breakout" / sanitize(Path(ready_path).stem)
        tmp = None
        try:
            work = state_dir / "breakout_work"
            work.mkdir(parents=True, exist_ok=True)
            data = breakout.run_ocr(ready_path, python, helper, work / (hashlib.sha1(str(ready_path).encode()).hexdigest()[:12] + ".json"))
            tmp = data["tmp"]
            hashes = {}
            for c in contents:                                  # Comic Vine's cover for each issue, to find where the book reprints it
                try:
                    hashes[c["id"]] = dhash(cv_cover(cv_key, c["id"]))
                except Exception:                              # noqa: BLE001
                    hashes[c["id"]] = None
            starts, evidence = breakout.find_starts_multi(data["pages"], contents, hashes)
            if starts:
                lengths = [b - a for a, b in zip(starts, starts[1:])]
                end = breakout.end_of_last_issue(data["pages"], starts[-1], contents[-1].get("name"), typical=(sorted(lengths)[len(lengths) // 2] if lengths else None))
                miss_ids = {int(m["id"]) for m in missing}
                bounds = list(zip(starts, starts[1:] + [end]))
                written = []
                for (a, b), c in zip(bounds, contents):
                    if int(c["id"]) not in miss_ids:
                        continue
                    stem = f"{sanitize(c['volume_name'])} {int(c['issue_number']):03d} ({(c.get('cover_date') or '')[:4]})"
                    single = out_dir / (stem + ".cbz")
                    out_dir.mkdir(parents=True, exist_ok=True)
                    with zipfile.ZipFile(single, "w", zipfile.ZIP_STORED) as z:
                        for n, page in enumerate([pg for pg in data["pages"] if a <= pg["i"] < b], 1):
                            z.write(Path(data["tmp"]) / page["file"], f"{n:03d}{Path(page['file']).suffix.lower()}")
                    if not tag_collected.tag(cfg.get("comictagger", "comictagger"), single, c["id"]):
                        why = f"ComicTagger could not tag the split-out issue #{c['issue_number']}"
                        written = None
                        break
                    old = time.time() - 3600
                    os.utime(single, (old, old))                      # already verified: no need to wait for the settling time
                    written.append(single)
                if written is not None:
                    approved = state_dir / "approved_covers.txt"
                    with open(approved, "a", encoding="utf-8") as f:
                        for single in written:
                            f.write(single.relative_to(incoming).as_posix() + "\n")
                    return {"status": "broken-out", "issues": len(written), "of": len(contents), "evidence": evidence, "first_month": first_month}
                for single in out_dir.glob("*.cbz"):
                    failed_dir = Path(cfg["incoming"]) / "_done" / "breakout_failed" / out_dir.name
                    failed_dir.mkdir(parents=True, exist_ok=True)
                    target = failed_dir / single.name
                    if target.exists():
                        target = target.with_name(f"{target.stem}-{time.strftime('%Y%m%d%H%M%S')}{target.suffix}")
                    shutil.move(str(single), str(target))
            else:
                why = f"not verified: {evidence}"
        except Exception as e:                                                      # noqa: BLE001
            why = f"split failed: {e}"
        finally:
            if tmp:
                shutil.rmtree(tmp, ignore_errors=True)
    breakout.record_wanted(state_dir, Path(ready_path).name, missing, why)
    return {"status": "wanted", "why": why, "missing": len(missing), "of": len(contents), "first_month": first_month}


def cleanup_expired_pipeline_artifacts(cfg, now=None, say=print):
    """Delete only pipeline-owned duplicate/quarantine/backup artifacts after the 30-day recovery window."""
    now = time.time() if now is None else now
    cutoff = now - PIPELINE_ARTIFACT_RETENTION_DAYS * 24 * 3600
    incoming = Path(cfg["incoming"]).resolve()
    state = incoming / "_state"
    done = incoming / "_done"
    roots = [state / "list_backups", state / "breakout_work", done / "duplicates", done / "converted_sources",
             done / "breakout_failed", done / "collected_sources"]
    roots.extend(p for p in state.glob("pipeline_metadata_fallback_*") if p.is_dir())
    candidates = []
    for root in roots:
        if root.exists():
            candidates.extend(p for p in root.rglob("*") if p.is_file())
    config_path = Path(cfg.get("_config_path") or Path(__file__).with_name("incoming_pipeline.json"))
    candidates.extend(p for p in config_path.parent.glob(config_path.name + ".bak-autoroute-*") if p.is_file())
    removed = 0
    protected = [incoming, config_path.parent.resolve()]
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
            allowed = any(root == resolved or root in resolved.parents for root in protected)
            if not allowed or candidate.stat().st_mtime >= cutoff:
                continue
            candidate.unlink()
            removed += 1
        except OSError as exc:
            say(f"  retention cleanup skipped {candidate.name}: {exc}")
    for root in roots:
        if not root.exists():
            continue
        for folder in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
            try:
                folder.rmdir()  # only empty directories; never recursively delete user content
            except OSError:
                pass
    if removed:
        say(f"retention cleanup: deleted {removed} pipeline artifact(s) older than {PIPELINE_ARTIFACT_RETENTION_DAYS} days")
    return removed


def run_prepare(cfg, cv_key, dry_run=False, say=print, now=None):
    incoming, ready = Path(cfg["incoming"]), Path(cfg["ready"])
    now = now or time.time()
    prepare_started = time.monotonic()
    state = load_state(cfg)
    if not dry_run:
        cleanup_expired_pipeline_artifacts(cfg, now=now, say=say)
    retry_after = float(state.get("cv_retry_after", 0) or 0)
    # A scheduled pass can start just before a rolling Comic Vine cooldown
    # expires. Deferring the entire queue for another 15-minute interval when
    # the cooldown has only seconds left needlessly strands every pending file.
    # Spend at most 45 seconds of this run waiting out that edge, then re-check.
    cooldown_remaining = retry_after - now
    if 0 < cooldown_remaining <= 45 and not dry_run:
        say(f"Comic Vine cooldown has {cooldown_remaining:.1f}s left; waiting for it to expire in this run")
        time.sleep(cooldown_remaining + 0.5)
        now = time.time()
        retry_after = float(state.get("cv_retry_after", 0) or 0)
    # Do not spend time expanding very large packages while Comic Vine is
    # rate-limited. Leave the ZIP untouched; the next run after cooldown will
    # unpack it before building the comic inventory.
    package_count = unpack_packages(incoming, incoming / "_done", say=say) if not dry_run and retry_after <= now else 0
    skip = {ready.resolve(), (incoming / "_done").resolve(), (incoming / "_state").resolve(), (incoming / BAD_DOWNLOADS).resolve()}
    files = [p for p in sorted(incoming.rglob("*")) if p.is_file() and p.suffix.lower() in COMIC_SUFFIXES
             and not any(parent.resolve() in skip for parent in p.parents)]
    cursor = state.get("prepare_cursor")
    if cursor and files:
        relative_paths = [str(p.relative_to(incoming)) for p in files]
        try:
            start = relative_paths.index(cursor) + 1
        except ValueError:
            start = next((i for i, relative in enumerate(relative_paths) if relative > cursor), 0)
        files = files[start:] + files[:start]
    holds = state.setdefault("prepare_holds", {})
    current_relatives = {str(p.relative_to(incoming)) for p in files}
    for held_path in list(holds):
        if held_path not in current_relatives:
            holds.pop(held_path, None)
    policy_fingerprint = prepare_policy_fingerprint(cfg)
    active_holds = {}
    released_holds = set()
    hold_retry_seconds = max(60, int(cfg.get("held_retry_seconds", HELD_RETRY_SECONDS)))
    for p in files:
        relative = str(p.relative_to(incoming))
        hold = holds.get(relative)
        if not hold:
            continue
        try:
            unchanged = (hold.get("signature") == _file_signature(p)
                         and hold.get("policy") == policy_fingerprint
                         and now - float(hold.get("attempted_at", 0)) < hold_retry_seconds)
        except (OSError, TypeError, ValueError):
            unchanged = False
        if unchanged:
            active_holds[relative] = hold
        else:
            # Keep the stale hold record until this file is actually attempted.
            # That lets repaired files stay at the front of later batches if a
            # time budget expires before all of them are retried.
            released_holds.add(relative)
    target_override_relatives = set()
    for override in cfg.get("chronology_content_overrides", []):
        for p in files:
            if (tag_collected.norm((tagcommon.read_tags(p) or {}).get("Series") or "")
                    == tag_collected.norm(override.get("series"))
                    and tag_collected.norm(chronology_pack_title(p))
                    == tag_collected.norm(override.get("title"))
                    and int(override.get("size") or -1) == p.stat().st_size
                    and str(override.get("sha256") or "").casefold()
                    == chronology_sha256(p).casefold()):
                target_override_relatives.add(str(p.relative_to(incoming)))
    if released_holds:
        # Alternate the retry queue across file types. Previously the sorted
        # chronology volumes occupied the front of the queue on every retry,
        # starving ordinary single issues until all very large books completed.
        released = [p for p in files if str(p.relative_to(incoming)) in released_holds]
        ordinary = [p for p in released if "chronology" not in str(p.relative_to(incoming)).casefold()]
        chronology = [p for p in released if "chronology" in str(p.relative_to(incoming)).casefold()]
        prioritized = ordinary + chronology
        files = prioritized + [p for p in files if str(p.relative_to(incoming)) not in released_holds]
        say(f"retrying {len(released_holds)} changed or reconfigured held file(s) ahead of the regular queue")
    if target_override_relatives:
        prioritized = [p for p in files if str(p.relative_to(incoming)) in target_override_relatives]
        files = prioritized + [p for p in files if str(p.relative_to(incoming)) not in target_override_relatives]
        say(f"prioritizing {len(prioritized)} exact-source chronology override(s) for verified processing")
    breakout_ready = [p for p in files
                      if p.relative_to(incoming).parts[0].casefold() == "_breakout"
                      and str(p.relative_to(incoming)) not in active_holds]
    if breakout_ready:
        prioritized = set(breakout_ready)
        files = breakout_ready + [p for p in files if p not in prioritized]
        say(f"prioritizing {len(breakout_ready)} verified breakout issue(s) for routing")
    finished_files = [p for p in files if is_finished(p, now, cfg.get("min_age_minutes", 10))
                      and str(p.relative_to(incoming)) not in active_holds]
    report, moved = {"time": datetime.now().isoformat(timespec="seconds"), "held": [], "waiting": [], "ready": [], "duplicates": [], "deferred": [], "bad_downloads": []}, 0
    min_age = cfg.get("min_age_minutes", 10)
    pause_flag = incoming / "_state" / "pause_tagging.flag"      # set while list reconcile needs the Comic Vine quota; reconcile removes it when done
    if pause_flag.exists() and retry_after <= now:
        retry_after = now + 60
        say("tagging paused while list reconcile uses the Comic Vine quota (pause_tagging.flag in _state)")
    if retry_after > now:
        for p in files:
            relative = str(p.relative_to(incoming))
            if relative in active_holds:
                hold = active_holds[relative]
                report["held"].append({"file": p.name, "reason": hold.get("reason", "unchanged file is held for review")})
            else:
                report["deferred" if is_finished(p, now, min_age) else "waiting"].append(p.name)
        say(f"Comic Vine rate limit cooldown active until {datetime.fromtimestamp(retry_after).isoformat(timespec='seconds')}; deferred {len(report['deferred'])} finished comic(s); skipped Komga duplicate scan")
        if not dry_run:
            save_state(cfg, state)
            write_report(cfg, report)
        return report
    state.pop("cv_retry_after", None)
    # Only make the whole-library Komga snapshot when matching can continue.
    # During Comic Vine cooldown all finished files are deferred, so this scan
    # cannot affect the outcome and may take several minutes on a large library.
    duplicate_check_error = None
    existing_ids = None
    if finished_files:
        say(f"checking Komga issue IDs before processing {len(finished_files)} finished comic(s)")
        try:
            existing_ids = existing_issue_ids(cfg)
            if existing_ids is not None:
                # The mirror can already contain a successfully uploaded item
                # before Komga's mounted library notices it. Count the
                # pipeline's verified uploaded/ready issue IDs too, so another
                # copy can be discarded before a damaged CBR is unpacked.
                for known in state.get("files", {}).values():
                    issue_id = _safe_int(str(known.get("issue") or ""))
                    if issue_id:
                        existing_ids.add(issue_id)
                    key = issue_identity_key(known.get("volume"), known.get("number"), known.get("anchor"))
                    if key and hasattr(existing_ids, "issue_keys"):
                        existing_ids.issue_keys.add(key)
        except Exception as e:  # noqa: BLE001
            duplicate_check_error = str(e)
    if finished_files and existing_ids is None:
        # Duplicate inventory is a safety prerequisite. Do not spend the
        # per-run item/time budget attempting files that cannot possibly be
        # prepared safely; leave them in place for the next healthy run.
        for p in files:
            relative = str(p.relative_to(incoming))
            if relative in active_holds:
                hold = active_holds[relative]
                report["held"].append({"file": p.name,
                                       "reason": hold.get("reason", "unchanged file is held for review")})
            elif is_finished(p, now, cfg.get("min_age_minutes", 10)):
                report["deferred"].append(p.name)
            else:
                report["waiting"].append(p.name)
        reason = f"Komga duplicate inventory unavailable: {duplicate_check_error or 'no inventory returned'}"
        say(f"{reason}; skipped prepare matching for {len(report['deferred'])} finished file(s) to preserve the batch for a healthy run")
        if not dry_run:
            save_state(cfg, state)
            write_report(cfg, report)
        return report
    max_items = max(1, int(cfg.get("max_items_per_run", MAX_ITEMS_PER_RUN)))
    time_budget = max(60, int(cfg.get("prepare_time_budget_seconds", PREPARE_TIME_BUDGET_SECONDS)))
    processed = 0
    skipped_holds = 0
    say(f"prepare inventory: {len(files)} comic file(s); processing for up to {time_budget}s with a {max_items}-file safety ceiling")
    for index, p in enumerate(files):
        relative = str(p.relative_to(incoming))
        if is_finished(p, now, min_age) and bad_download_kind(p):
            # a web page saved under the comic's name: it costs no Comic Vine request, needs no hold, and is cleared even if it was held before
            quarantine_bad_download(p, incoming, say=say, dry_run=dry_run)
            report["bad_downloads"].append(p.name)
            holds.pop(relative, None)
            active_holds.pop(relative, None)
            continue
        if relative in active_holds:
            hold = active_holds[relative]
            details = {key: hold[key] for key in ("reacquire_kind", "reacquire_query", "reacquire_issues",
                                                   "unmatched_toc_references", "issue_target", "transient")
                       if key in hold}
            report["held"].append({"file": p.name, "reason": hold.get("reason", "unchanged file is held for review"), **details})
            skipped_holds += 1
            continue
        if not is_finished(p, now, min_age):
            report["waiting"].append(p.name)
            continue
        time_exhausted = time.monotonic() - prepare_started >= time_budget
        if processed >= max_items or time_exhausted:
            report["deferred"].append(p.name)
            continue
        processed += 1
        if processed == 1 or processed % 10 == 0:
            say(f"prepare progress: {processed} files started; {len(files) - index - 1} remain in inventory")
        if existing_ids is None:
            reason = "Komga duplicate check unavailable (set KOMGA_API_KEY); comic was left untouched"
            if duplicate_check_error:
                reason = f"Komga duplicate check failed ({duplicate_check_error}); comic was left untouched"
            say(f"  DEFERRED: {p.name} ({reason}; retrying on a later run)")
            report["deferred"].append(p.name)
            state["prepare_cursor"] = relative
            continue
        holds.pop(relative, None)
        r, error = None, None
        for attempt in range(3):
            try:
                r = prepare_file(p, cfg, cv_key, cfg.get("comictagger", "comictagger"), dry_run, say, existing_ids=existing_ids)
                error = None
                break
            except Exception as e:                                                   # noqa: BLE001
                error = e
                if isinstance(e, ArchiveConversionError):
                    break
                if isinstance(e, urllib.error.HTTPError) and e.code == 420:
                    retry = time.time() + CV_RATE_LIMIT_COOLDOWN
                    state["cv_retry_after"] = retry
                    report["deferred"].extend([p.name] + [later.name for later in files[index + 1:] if is_finished(later, now, cfg.get("min_age_minutes", 10))])
                    report["waiting"].extend(later.name for later in files[index + 1:] if not is_finished(later, now, cfg.get("min_age_minutes", 10)))
                    say(f"Comic Vine returned HTTP 420; pausing matching for {CV_RATE_LIMIT_COOLDOWN // 60} minutes and deferring {len(report['deferred'])} comic(s)")
                    break
                if isinstance(e, comicvine_store.ComicVineBudgetExceeded):
                    retry = max(time.time() + 30, e.retry_at)
                    state["cv_retry_after"] = retry
                    remaining = retry - time.time()
                    if 0 < remaining <= 45 and attempt < 2:
                        say(f"Comic Vine's rolling budget needs {remaining:.1f}s to reset; waiting and retrying {p.name} in this run")
                        time.sleep(remaining + 0.5)
                        state.pop("cv_retry_after", None)
                        error = None
                        continue
                    report["deferred"].extend([p.name] + [later.name for later in files[index + 1:] if is_finished(later, now, cfg.get("min_age_minutes", 10))])
                    report["waiting"].extend(later.name for later in files[index + 1:] if not is_finished(later, now, cfg.get("min_age_minutes", 10)))
                    say(f"Comic Vine's rolling request budget is exhausted; deferring remaining files until {datetime.fromtimestamp(retry).isoformat(timespec='seconds')}")
                    break
                if attempt < 2:
                    say(f"  retrying {p.name} after processing error ({attempt + 1}/2): {e}")
                    time.sleep(2 ** attempt)
                    if not p.exists() and p.suffix.lower() == ".cbr" and p.with_suffix(".cbz").exists():
                        p = p.with_suffix(".cbz")
                    continue
                say(f"  DEFERRED: {p.name} (processing error after 3 attempts: {e}; left untouched for retry)")
        if ((isinstance(error, urllib.error.HTTPError) and error.code == 420)
                or isinstance(error, comicvine_store.ComicVineBudgetExceeded)):
            break
        if r is None:
            state["prepare_cursor"] = relative
            holds.pop(relative, None)
            if isinstance(error, ArchiveConversionError):
                reason = str(error)
                details = {"reacquire_kind": "unreadable_source", "reacquire_query": p.stem}
                if not dry_run:
                    holds[relative] = {"signature": _file_signature(p), "policy": policy_fingerprint,
                                       "reason": reason, "attempted_at": time.time(), **details}
                say(f"  HELD: {p.name} ({reason})")
                report["held"].append({"file": p.name, "reason": reason, **details})
            else:
                report["deferred"].append(p.name)
            continue
        # ensure_cbz may convert an incoming .cbr and remove its source after creating the .cbz.
        p = Path(r.get("file") or p)
        relative = str(p.relative_to(incoming))
        state["prepare_cursor"] = relative
        if r["status"] == "duplicate":
            holds.pop(relative, None)
            say(f"  duplicate: {p.name} ({r['reason']})")
            report["duplicates"].append(p.name)
            if not dry_run:
                try:
                    p.unlink()
                    say(f"  deleted confirmed duplicate: {p.name}")
                except OSError as exc:
                    say(f"  could not delete confirmed duplicate {p.name}; left in place: {exc}")
        elif r["status"] == "expanded":
            holds.pop(relative, None)
            report.setdefault("expanded", []).append({"file": p.name, "issues": r.get("issues", 0), "of": r.get("of", 0)})
            if existing_ids is not None:
                existing_ids.update(int(issue_id) for issue_id in r.get("issue_ids", []))
            if not dry_run:
                archive = incoming / "_done" / "collected_sources"
                archive.mkdir(parents=True, exist_ok=True)
                target = archive / p.name
                if target.exists():
                    target = target.with_name(f"{target.stem}-{time.strftime('%Y%m%d%H%M%S')}{target.suffix}")
                shutil.move(str(p), str(target))
                os.utime(target, None)  # start the 30-day recovery window when the contents are confirmed
            say(f"  expanded verified chronology pack: {p.name} ({r.get('issues', 0)} missing issue(s) staged; source retained for {PIPELINE_ARTIFACT_RETENTION_DAYS} days)")
        elif r["status"] == "ready":
            holds.pop(relative, None)
            say(f"  ready: {p.name} -> {r['dest']}  [{r['title']}]")
            report["ready"].append(p.name)
            if not dry_run:
                target = ready / r["dest"]
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(p), str(target))
                state["files"][r["dest"]] = {k: r.get(k) for k in ("title", "volume", "number", "issue", "collection", "library", "anchor", "anchor_source", "collected", "publisher", "lists", "collection_list", "metadata_fallback")}
                moved += 1
                if existing_ids is not None and r.get("issue"):
                    existing_ids.add(int(r["issue"]))
                if r.get("collected") and r.get("issue") and cfg.get("break_out_collected_editions", True):
                    try:
                        b = breakout_collected(cfg, cv_key, target, r, existing_ids, say=say, dry_run=dry_run)
                    except Exception as e:                                          # noqa: BLE001
                        b = {"status": "unresolved", "why": f"breakout failed: {e}"}
                    say(f"  breakout: {b['status']} {b.get('why') or ''}".rstrip())
                    report.setdefault("breakout", []).append({"file": p.name, **{k: v for k, v in b.items() if k != "evidence"}})
                    entry = state["files"][r["dest"]]
                    entry["breakout"] = b["status"]
                    # a numbered "Vol. N" book whose issues are not (yet) in Komga stands in the lists at its first issue's month
                    # (a multi-volume set is already in reading order); once the singles arrive it can be taken out of the lists
                    if b["status"] in ("wanted", "unresolved") and b.get("first_month") and cfg.get("trade_list_fallback", True) and re.search(r"(?:v|vol(?:ume)?)[ ._-]*0*[1-9]", p.name, re.I):
                        entry["list_fallback"] = True
                        entry["content_anchor"] = b["first_month"]
        else:
            if not dry_run:
                try:
                    if r.get("transient"):
                        holds.pop(relative, None)
                    else:
                        details = {key: r[key] for key in ("reacquire_kind", "reacquire_query", "reacquire_issues",
                                                           "unmatched_toc_references", "issue_target", "transient")
                                   if key in r}
                        holds[relative] = {"signature": _file_signature(p), "policy": policy_fingerprint,
                                           "reason": r.get("reason", "held for review"), "attempted_at": time.time(), **details}
                except OSError:
                    holds.pop(relative, None)
            say(f"  HELD: {p.name} ({r['reason']})")
            details = {key: r[key] for key in ("reacquire_kind", "reacquire_query", "reacquire_issues",
                                               "unmatched_toc_references", "issue_target", "transient")
                       if key in r}
            report["held"].append({"file": p.name, "reason": r["reason"], **details})
    if not dry_run:
        save_state(cfg, state)
        write_report(cfg, report)
    elapsed = int(time.monotonic() - prepare_started)
    say(f"{len(files)} comic file(s): processed {processed} in {elapsed}s, {moved} moved to _ready, {len(report['duplicates'])} duplicates skipped, {len(report['held'])} held ({skipped_holds} unchanged holds skipped), {len(report['deferred'])} deferred, {len(report['waiting'])} still downloading ({package_count} unpacked)")
    return report


# ---------------------------------------------------------------- state and report
def state_path(cfg):
    return Path(cfg["incoming"]) / "_state" / "incoming_state.json"


def load_state(cfg):
    try:
        s = json.loads(state_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        s = {}
    s.setdefault("files", {})
    return s


def save_state(cfg, state):
    p = state_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def write_report(cfg, report):
    p = Path(cfg["incoming"]) / "_state" / "incoming_report.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(report, indent=1), encoding="utf-8")
    write_reacquire_report(cfg, report)


def write_reacquire_report(cfg, report=None):
    """Write a human-readable action list for held items and their known replacement issues."""
    state_dir = Path(cfg["incoming"]) / "_state"
    if report is None:
        try:
            report = json.loads((state_dir / "incoming_report.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            report = {"held": []}
    try:
        wanted = json.loads((state_dir / "acquisition_wanted.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        wanted = {}
    try:
        sent = json.loads((state_dir / "acquisition_sent.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        sent = {}

    items = []
    for held in report.get("held", []):
        if isinstance(held, str):
            held = {"file": held, "reason": "held; see incoming report"}
        source = str(held.get("file") or "")
        reason = str(held.get("reason") or "held; see incoming report")
        queued = wanted.get(source) or {}
        targets = list(held.get("reacquire_issues") or queued.get("issues") or [])
        references = list(held.get("unmatched_toc_references") or [])
        kind = str(held.get("reacquire_kind") or queued.get("kind") or "")
        if not targets and held.get("issue_target"):
            targets = [held["issue_target"]]

        transient = bool(held.get("transient") or kind == "retry_later"
                         or "request budget exhausted" in reason.casefold()
                         or "rate limit" in reason.casefold())
        # This is a reacquisition report, not a report of every held source.
        # Transient service limits are retryable and must not be presented as
        # items the user needs to reacquire.
        if transient:
            continue
        unreadable = (kind == "unreadable_source"
                      or any(word in reason.casefold() for word in
                             ("crc failed", "data error", "cannot open as archive", "archive is damaged",
                              "cbr extraction failed", "ocr could not decode", "unreadable")))
        if targets:
            category = "known_issue_replacements"
            action = "Reacquire these exact single issues; the pipeline will skip any it later finds in Komga or staging."
        elif references:
            category = "unmatched_contents_references"
            action = "Reacquire these listed issues after identifying their exact Comic Vine records."
        elif unreadable:
            category = "unreadable_source"
            action = "Reacquire a clean copy of this source file."
        elif kind == "contents_unmatched" or "did not resolve" in reason.casefold():
            category = "contents_unmatched"
            action = "No reliable issue set was found; identify the volume contents, then reacquire its missing single issues."
        elif "cover" in reason.casefold() or "title page" in reason.casefold():
            category = "source_match_unverified"
            action = "Reacquire a clean scan only if the source cannot be matched after review; do not split or route it yet."
        else:
            category = "match_unresolved"
            action = "Resolve the metadata/content match; reacquire the named source if no exact issue match is available."

        target_rows = []
        for target in targets:
            number = target.get("number") or target.get("issue_number")
            series_name = target.get("series") or target.get("volume_name") or "Unknown series"
            cover_date = target.get("cover_date") or ""
            issue_id = str(target.get("id") or "")
            last = sent.get(issue_id, {}) if issue_id else {}
            last_status = last.get("status")
            status_text = {"not found": "not found at configured source", "sent": "sent to JDownloader"}.get(
                last_status, "pending acquisition" if source in wanted else "not yet queued")
            label = f"{series_name} #{number}" if number not in (None, "") else str(series_name)
            if cover_date:
                label += f" ({cover_date})"
            target_rows.append({"issue_id": issue_id, "issue": label, "status": status_text})
        for ref in references:
            month = ref.get("release_month")
            year = ref.get("release_year")
            label = f"{ref.get('series') or 'Unknown series'} #{ref.get('number') or '?'}"
            if month and year:
                label += f" (TOC release {year}-{int(month):02d})"
            target_rows.append({"issue_id": "", "issue": label, "status": "Comic Vine match unresolved"})
        if not target_rows:
            if transient:
                target_rows.append({"issue_id": "", "issue": "", "status": "automatic retry; no reacquisition needed"})
            else:
                target_rows.append({"issue_id": "", "issue": held.get("reacquire_query") or Path(source).stem or source,
                                    "status": "source file / contents need identification"})
        items.append({"source_file": source, "category": category, "reason": reason,
                      "action": action, "targets": target_rows})

    summary = {"held_sources": len(items),
               "known_issue_replacements": sum(len(x["targets"]) for x in items if x["category"] == "known_issue_replacements"),
               "unreadable_sources": sum(x["category"] == "unreadable_source" for x in items),
               "unmatched_contents": sum(x["category"] in ("contents_unmatched", "unmatched_contents_references") for x in items),
               "temporary_retries": sum(x["category"] == "retry_later" for x in items)}
    document = {"generated_at": datetime.now().isoformat(timespec="seconds"), "summary": summary, "items": items}
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "reacquire_report.json").write_text(json.dumps(document, indent=1, ensure_ascii=False), encoding="utf-8")

    def cell(value):
        return str(value or "").replace("|", "\\|").replace("\r", " ").replace("\n", " ")

    lines = ["# Comic pipeline: reacquisition and unresolved matching", "",
             f"Generated: {document['generated_at']}", "",
             f"Held sources: {summary['held_sources']} · exact issue replacement targets: {summary['known_issue_replacements']} · "
             f"unreadable sources: {summary['unreadable_sources']} · unmatched contents: {summary['unmatched_contents']} · "
             f"temporary retries: {summary['temporary_retries']}", "",
            "Temporary Comic Vine/network failures are omitted; the pipeline retries them automatically. Exact issue targets include the last configured-source result.", "",
             "| Source file | Status | Action / target | Reason |", "|---|---|---|---|"]
    for item in items:
        targets = "; ".join(f"{t['issue']} — {t['status']}" for t in item["targets"])
        lines.append(f"| {cell(item['source_file'])} | {cell(item['category'])} | {cell(targets)} | {cell(item['reason'])} |")
    if not items:
        lines.append("| — | — | No currently held sources | — |")
    (state_dir / "reacquire_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- stage 3: Komga
def books_of(komga, path):
    return komga.paged(path)


def komga_ready(v):
    """True when the Komga stage may finish this file: a verified list date, or a collected edition (no list date needed)."""
    return bool(v.get("collected")) or bool(v.get("anchor") and not v.get("anchor_guess")
                                           and v.get("anchor_source") in ("comicvine.cover_date", "config.override"))


def repair_unverified_anchors(due, state, cv_key, now, say=print):
    """Repair old guessed/missing date anchors from the linked Comic Vine issue before list placement."""
    candidates = [(k, v) for k, v in due.items()
                  if not komga_ready(v) and not v.get("date_review_reason")]
    retryable = set()
    if not candidates:
        return retryable
    if not cv_key:
        for k, v in candidates:
            retryable.add(k)
            v["date_repair_retry_after"] = now + DATE_REPAIR_RETRY_SECONDS
            say(f"  DEFERRED date repair: {Path(k).name} (Comic Vine key unavailable; will retry)")
        return retryable

    global_retry = float(state.get("cv_retry_after", 0) or 0)
    if global_retry > now:
        for k, v in candidates:
            retryable.add(k)
            v["date_repair_retry_after"] = global_retry
            say(f"  DEFERRED date repair: {Path(k).name} (Comic Vine cooldown active until {datetime.fromtimestamp(global_retry).isoformat(timespec='seconds')})")
        return retryable

    for index, (k, v) in enumerate(candidates):
        retry_after = float(v.get("date_repair_retry_after", 0) or 0)
        if retry_after > now:
            retryable.add(k)
            say(f"  DEFERRED date repair: {Path(k).name} (retry is due at {datetime.fromtimestamp(retry_after).isoformat(timespec='seconds')})")
            continue
        issue_id = v.get("issue")
        if not issue_id:
            v["date_review_reason"] = "no Comic Vine issue ID is recorded; cannot verify a date safely"
            say(f"  HELD for date review: {Path(k).name} ({v['date_review_reason']})")
            continue

        issue, error = None, None
        for attempt in range(3):
            try:
                issue = tag_collected._get(cv_key, f"issue/4000-{issue_id}/", field_list="id,issue_number,cover_date")
                error = None
                break
            except urllib.error.HTTPError as e:                                  # noqa: BLE001
                error = e
                if e.code == 420:
                    retry = now + CV_RATE_LIMIT_COOLDOWN
                    state["cv_retry_after"] = retry
                    for rest_k, rest_v in candidates[index:]:
                        retryable.add(rest_k)
                        rest_v["date_repair_retry_after"] = retry
                    say(f"Comic Vine returned HTTP 420 during date repair; deferring remaining lookups for {CV_RATE_LIMIT_COOLDOWN // 60} minutes")
                    return retryable
            except comicvine_store.ComicVineBudgetExceeded as e:
                retry = max(now + 30, e.retry_at)
                state["cv_retry_after"] = retry
                for rest_k, rest_v in candidates[index:]:
                    retryable.add(rest_k)
                    rest_v["date_repair_retry_after"] = retry
                say(f"Comic Vine's rolling request budget is exhausted; deferring date repairs until {datetime.fromtimestamp(retry).isoformat(timespec='seconds')}")
                return retryable
            except Exception as e:                                                   # noqa: BLE001
                error = e
            if attempt < 2:
                time.sleep(2 ** attempt)

        if not isinstance(issue, dict):
            retryable.add(k)
            v["date_repair_retry_after"] = now + DATE_REPAIR_RETRY_SECONDS
            v["date_repair_error"] = f"{type(error).__name__}: {error}"[:240] if error else "Comic Vine returned no issue record"
            say(f"  DEFERRED date repair: {Path(k).name} (lookup failed after 3 attempts: {v['date_repair_error']}; will retry)")
            continue
        if str(issue.get("id")) != str(issue_id):
            retryable.add(k)
            v["date_repair_retry_after"] = now + DATE_REPAIR_RETRY_SECONDS
            v["date_repair_error"] = "Comic Vine returned an issue ID that did not match the recorded ID"
            say(f"  DEFERRED date repair: {Path(k).name} ({v['date_repair_error']})")
            continue

        anchor = month_from_cover_date(issue.get("cover_date"))
        if not anchor:
            v["date_review_reason"] = "Comic Vine confirms the issue but has no usable cover date; add a verified override"
            say(f"  HELD for date review: {Path(k).name} ({v['date_review_reason']})")
            continue

        if v.get("anchor") and v.get("anchor") != anchor:
            v["anchor_previous_guess"] = v["anchor"]
        v["anchor"] = anchor
        v["anchor_source"] = "comicvine.cover_date"
        v.pop("anchor_guess", None)
        v.pop("date_review_reason", None)
        v.pop("date_repair_retry_after", None)
        v.pop("date_repair_error", None)
        say(f"  repaired verified date: {Path(k).name} -> {anchor} (Comic Vine cover date)")
        if index < len(candidates) - 1:
            time.sleep(1.5)
    return retryable


def library_root_for(cfg, name):
    """Where the Komga library `name` lives on the mirror: its configured root, else a folder of that name beside the other
    configured roots (the rule auto_create_route uses). None when no root is configured at all."""
    roots = cfg.get("library_roots") or {}
    if roots.get(name):
        return str(roots[name])
    other = next((Path(v) for v in roots.values()), None)
    return str(other.parent / name) if other is not None else None


def ensure_configured_libraries(cfg, komga, library_names, existing, say=print, dry_run=False):
    """Create a Komga library only after its uploaded mirror root is visible to Komga.

    The uploader creates the remote destination; `root` must be the mounted mirror path
    Komga can read. Creating a same-named local folder here can silently point Komga at
    an empty directory while the comics are already on the mirror.
    """
    provisionable = set()
    for name in sorted(set(library_names)):
        if name in existing:
            continue
        configured = library_root_for(cfg, name)
        if not configured:
            say(f"  no root known for the Komga library {name!r}; add it to library_roots in the config")
            continue
        provisionable.add(name)
        root = str(Path(configured))
        if dry_run:
            say(f"  would create Komga library {name!r} at {root}")
            continue
        if not Path(root).is_dir():
            provisionable.discard(name)
            say(f"  DEFERRED library creation for {name!r}: mirror destination is not visible at {root}; upload/refresh the mirror mount first")
            continue
        body = {
            "name": name, "root": root,
            "importComicInfoBook": True, "importComicInfoSeries": True,
            "importComicInfoCollection": True, "importComicInfoReadList": True,
            "importComicInfoSeriesAppendVolume": True, "importEpubBook": True,
            "importEpubSeries": True, "importMylarSeries": True,
            "importLocalArtwork": True, "importBarcodeIsbn": True,
            "scanForceModifiedTime": True, "scanInterval": "EVERY_6H",
            "scanOnStartup": False, "scanCbx": True, "scanPdf": True,
            "scanEpub": True, "scanDirectoryExclusions": [],
            "repairExtensions": False, "convertToCbz": False,
            "emptyTrashAfterScan": False, "seriesCover": "FIRST",
            "hashFiles": True, "hashPages": False, "hashKoreader": False,
            "analyzeDimensions": True,
        }
        try:
            created = komga.call("/libraries", "POST", body)
            existing[name] = created["id"]
            say(f"  created Komga library {name!r} at {root}")
        except Exception as e:  # noqa: BLE001
            say(f"  DEFERRED library creation for {name!r} at {root}: {e}")
    return provisionable


def run_komga(cfg, komga, now=None, say=print, dry_run=False, cv_key=None):
    """Finish the files uploaded at least KOMGA_DELAY ago: lock metadata, update read lists. Returns the number completed."""
    now = now or time.time()
    state = load_state(cfg)
    # Older pipeline versions could save an empty Comic Vine issue name. Keep
    # those pending records useful instead of sending `title: null` to Komga.
    for item in state["files"].values():
        if (not item.get("title") and item.get("volume")
                and item.get("number") not in (None, "")):
            item["title"] = f"{item['volume']} #{item['number']}"
    # a file is due until both its metadata is locked and its read lists are brought up to date (lists_done): files finished by an
    # older version of this script get their lists checked once more
    due = {k: v for k, v in state["files"].items()
           if v.get("uploaded_at") and not (v.get("komga_done") and v.get("lists_done"))
           and now - v["uploaded_at"] >= KOMGA_DELAY
           and now - float(v.get("lists_retry_after") or 0) >= 0}
    if not due:
        say("nothing waiting for Komga")
        return 0
    cv_key = cv_key or komga_title_fix.comicvine_key(cfg.get("comictagger_settings"))
    retryable_dates = repair_unverified_anchors(due, state, cv_key, now, say=say)
    libs = {l["name"]: l["id"] for l in komga.call("/libraries")}
    configured_library_names = {
        v.get("library", v.get("collection")) for k, v in due.items()
        if k not in retryable_dates and komga_ready(v)
    }
    provisionable = ensure_configured_libraries(cfg, komga, configured_library_names, libs, say, dry_run)
    reconciled = set()
    done = 0
    missing_library = [(k, v) for k, v in due.items()
                       if k not in retryable_dates and v.get("library", v.get("collection")) not in libs
                       and v.get("library", v.get("collection")) not in provisionable
                       and komga_ready(v) and not v.get("collected")]
    # State from older runs can point to a library that does not exist. Reconcile
    # these by the stable Comic Vine issue ID instead of title or folder guesses.
    if missing_library:
        all_books = []
        book_libraries = {}
        for lib_id in libs.values():
            lib_name = next((name for name, candidate in libs.items() if candidate == lib_id), None)
            for book in books_of(komga, f"/books?library_id={lib_id}"):
                if not book.get("deleted"):
                    all_books.append(book)
                    book_libraries[book["id"]] = lib_name
        list_ids = {l["name"]: l["id"] for l in komga.paged("/readlists")}
        for k, v in missing_library:
            issue_id = v.get("issue")
            matches = [b for b in all_books if issue_id and str(comicvine_issue_id(b)) == str(issue_id)]
            explicit_lists = list(v.get("lists") or [])
            if len(matches) != 1:
                say(f"  DEFERRED missing-library reconciliation: {Path(k).name} (found {len(matches)} exact Comic Vine issue matches; need exactly one)")
                continue
            book = matches[0]
            if not dry_run:
                komga.call(f"/books/{book['id']}/metadata", "PATCH", {
                    "releaseDate": f"{v['anchor']}-01", "releaseDateLock": True})
            say(f"  locked: verified release date {v['anchor']}-01 for {Path(k).name}")
            absent = []
            for name in explicit_lists:
                rid = list_ids.get(name)
                if not rid:
                    absent.append(name)
                    continue
                current_ids = [b["id"] for b in komga.paged(f"/readlists/{rid}/books")]
                if book["id"] not in current_ids:
                    absent.append(name)
            if absent and not dry_run:
                backup_dir = Path(cfg["incoming"]) / "_state" / "list_backups"
                backup_dir.mkdir(parents=True, exist_ok=True)
                problems = []
                for name in absent:
                    problems.extend(put_block(komga, list_ids, name, [book["id"]], v["anchor"], backup_dir, say))
                if problems:
                    say(f"  DEFERRED missing-library reconciliation: {Path(k).name} ({'; '.join(problems)})")
                    continue
                add_to_include(cfg, [n[2:-len(LIST_SUFFIX)] for n in absent if n.startswith("! ") and n.endswith(LIST_SUFFIX)])
                state.setdefault("touched_lists", [])
                state["touched_lists"] = sorted(set(state["touched_lists"]) | {n[2:-len(LIST_SUFFIX)] for n in absent if n.startswith("! ") and n.endswith(LIST_SUFFIX)})
            elif absent:
                say(f"  would add {Path(k).name} to explicit list(s): {', '.join(absent)}")
            if not dry_run:
                state["files"][k]["komga_done"] = True
                state["files"][k]["lists_done"] = True
                state["files"][k]["duplicate_of"] = book["id"]
                state["files"][k]["matched_library"] = book_libraries.get(book["id"])
                reconciled.add(k)
            reconciled.add(k)
            say(f"  already integrated: {Path(k).name} -> Komga book {book['id']} (Comic Vine issue {issue_id})")
            done += 1
    by_library = {}
    for k, v in due.items():
        if k in retryable_dates or k in reconciled or (not dry_run and state["files"].get(k, {}).get("komga_done") and state["files"].get(k, {}).get("lists_done")):
            continue
        if not komga_ready(v):
            reason = v.get("date_review_reason") or "missing verified date source; add a verified override before list insertion"
            say(f"  HELD for date review: {Path(k).name} ({reason})")
            continue
        by_library.setdefault(v.get("library", v["collection"]), []).append((k, v))
    for library, items in by_library.items():
        lib = libs.get(library)
        if not lib:
            say(f"  no Komga library named {library!r}; leaving {len(items)} file(s) for later")
            continue
        if not dry_run:
            komga.scan(lib)
            time.sleep(cfg.get("scan_wait", 60))
        books = books_of(komga, f"/books?library_id={lib}")
        found = []
        for k, v in sorted(items, key=lambda kv: (kv[1]["volume"], issue_number_sort_key(kv[1].get("number")))):
            b = next((b for b in books if re.split(r"[\\/]", b["url"])[-1] == Path(k).name and not b.get("deleted")), None)
            if b is None:
                say(f"  Komga has not picked up {Path(k).name} yet")
                continue
            found.append((k, v, b))
        for k, v, b in found:
            m = b["metadata"]
            # The list anchor is the verified Comic Vine cover month (or an explicitly reviewed override).
            # Lock that same date in Komga so its metadata and chronological list placement cannot diverge.
            body = {"title": v["title"], "titleLock": True, "numberLock": True, "numberSortLock": True}
            if not v.get("collected") and v.get("anchor"):
                body.update({"releaseDate": f"{v['anchor']}-01", "releaseDateLock": True})
            if not dry_run:
                komga.call(f"/books/{b['id']}/metadata", "PATCH", body)
            say(f"  locked: {v['title']}")
        ids = [b["id"] for _, _, b in found]
        lists_ok = False
        if ids and not dry_run:
            touched, lists_ok = update_lists(cfg, komga, library, found, say)
            add_to_include(cfg, touched)
            state.setdefault("touched_lists", [])
            state["touched_lists"] = sorted(set(state["touched_lists"]) | set(touched))
        for k, v, b in found:
            if not dry_run:
                state["files"][k]["komga_done"] = True
                state["files"][k]["lists_done"] = lists_ok
                if lists_ok:
                    state["files"][k].pop("lists_retry_after", None)
                else:
                    state["files"][k]["lists_retry_after"] = now + max(
                        60, int(cfg.get("list_retry_seconds", LIST_RETRY_SECONDS)))
            done += 1
    if not dry_run:
        save_state(cfg, state)
    return done


def add_to_include(cfg, names):
    """Tell the exporter these lists belong on the site whatever their size (readlist_include.txt). Only lists filled from Incoming
    are ever added, so collections put into Komga by hand stay off the site."""
    path = Path(cfg.get("include_file", r"C:\Users\paris\ArmgddnUploader\readlists\readlist_include.txt"))
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = ["# lists the comics pipeline fills from Incoming: exported to the site at any size"]
    have = {l.strip().lower() for l in lines}
    new = [n for n in names if n.lower() not in have]
    if new:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines + new) + "\n", encoding="utf-8")
    return new


def approve_content_anchor(book_id, anchor, source, list_names):
    """Record the content-order anchor of a collected edition standing in a reading list (its first collected issue's cover month).
    The exporter refuses to publish a list holding a collected edition without such an approved date."""
    path = CONTENT_ORDER_ANCHORS
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {"description": "Content-order anchors for collected editions in curated lists.", "books": {}}
    books = data.setdefault("books", {})
    row = books.setdefault(str(book_id), {"anchor": anchor, "source": source, "lists": []})
    row["anchor"], row["source"] = anchor, source
    row["lists"] = sorted(set(row.get("lists") or []) | set(list_names))
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def put_block(komga, lists, name, block, anchor, backup_dir, say):
    """Make sure the Komga list `name` exists and holds `block` (book ids, in order) at `anchor`. Created when missing; otherwise the
    block is inserted without moving anything. Re-read and checked afterwards; undone if the check fails. Returns problems."""
    rid = lists.get(name)
    if not rid:
        ids = list(block)                     # only what came through Incoming: the rest of a library may be there on purpose
        made = komga.call("/readlists", "POST", {"name": name, "bookIds": ids, "ordered": True})
        if isinstance(made, dict) and made.get("id"):
            lists[name] = made["id"]
            say(f"  {name}: created with {len(ids)} book(s)")
            return []
        return [f"could not create {name}"]
    books = komga.paged(f"/readlists/{rid}/books")
    ids = [b["id"] for b in books]
    if len(ids) != len(set(ids)):
        say(f"  {name}: NOT changed (the existing list already contains duplicate book IDs)")
        return ["existing list contains duplicate book IDs"]
    dates = readlist_order_months(books, name)
    inversions = date_inversions(dates)
    if inversions:
        first = inversions[0]
        say(f"  {name}: preserving existing date reversal at positions {first[0]}-{first[1]}, {first[2]} to {first[3]}; checking this insertion locally")
    inversion_signature = tuple((row[2], row[3]) for row in inversions)
    at = placement_index(dates, anchor)
    new = insert_block(ids, block, at)
    if len(new) == len(ids):
        return []
    problems = verify_insert(books, new, block, at, anchor, dates=dates, strict=True)
    if problems:
        say(f"  {name}: NOT changed ({'; '.join(problems)})")
        return problems
    (backup_dir / f"{rid}-{time.time_ns()}.json").write_text(json.dumps(ids))
    komga.call(f"/readlists/{rid}", "PATCH", {"bookIds": new, "ordered": True})
    check_books = komga.paged(f"/readlists/{rid}/books")
    check = [b["id"] for b in check_books]
    detail = komga.call(f"/readlists/{rid}")
    check_inversions = date_inversions(readlist_order_months(check_books, name))
    check_inversion_signature = tuple((row[2], row[3]) for row in check_inversions)
    if (check != new or check_inversion_signature != inversion_signature
            or (isinstance(detail, dict) and detail.get("ordered") is False)):
        komga.call(f"/readlists/{rid}", "PATCH", {"bookIds": ids, "ordered": True})
        reason = "the insertion changed date-order anomalies" if check_inversion_signature != inversion_signature else "Komga did not keep the change"
        say(f"  {name}: {reason}; restored the previous order")
        return [reason]
    say(f"  {name}: {len(new) - len(ids)} book(s) inserted at position {at} (anchor {anchor})")
    return []


def update_lists(cfg, komga, library, found, say):
    """Bring the read lists in step with the new books so the site (which mirrors the "! ... - Suggested Reading Order" lists) and
    Komga look the same. Returns (base names of the site lists that changed, for the export check; whether every list went fine)."""
    lists = {l["name"]: l["id"] for l in komga.paged("/readlists")}
    backup_dir = Path(cfg["incoming"]) / "_state" / "list_backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    touched = []
    # the library's own plain list (same name as the library), when one exists: new books appended
    own = lists.get(library)
    if own:
        cur = [b["id"] for b in komga.paged(f"/readlists/{own}/books")]
        new = cur + [b["id"] for _, _, b in found if b["id"] not in cur]
        if new != cur:
            (backup_dir / f"{own}-{time.time_ns()}.json").write_text(json.dumps(cur))
            komga.call(f"/readlists/{own}", "PATCH", {"bookIds": new})
            say(f"  {library}: {len(new) - len(cur)} book(s) added to its read list")
    # the collection's site list, then the publisher's master list(s)
    wanted, ok = {}, True
    fallback_ids = {b["id"] for k, v, b in found if v.get("collected") and v.get("list_fallback") and v.get("content_anchor")}
    for k, v, b in found:
        if v.get("collected") and not (v.get("list_fallback") and v.get("content_anchor")):
            say(f"  {Path(k).name}: not added to a ! reading list (collected edition; its issues are in the lists as singles)")
            continue
        master = site_list_name(master_base(cfg, v.get("publisher")))
        names = list(dict.fromkeys([v.get("collection_list") or site_list_name(library), *(v.get("lists") or []), master]))
        # Curated "!" lists are singles-only. A content-anchor repair once put
        # Masterworks/omnibus volumes at the dates of the stories they collect,
        # making old collected editions appear among original 1960s issues.
        # Enforce this again at the final Komga mutation boundary.
        if any(name.startswith("! ") and name.endswith(LIST_SUFFIX) for name in names):
            tags = {"Format": (b.get("metadata") or {}).get("format") or ""}
            issue = {"name": (b.get("metadata") or {}).get("title") or ""}
            if collected_edition(b.get("url") or k, tags, v.get("volume", ""), issue) and not (v.get("list_fallback") and v.get("content_anchor")):
                say(f"  {Path(k).name}: not added to a ! reading list (collected edition; its issues are in the lists as singles)")
                continue
        for name in names:
            wanted.setdefault((name, v.get("anchor") or v.get("content_anchor")), []).append(b["id"])
    for (name, anchor), block in wanted.items():
        if not anchor:
            say(f"  {name}: no anchor date, not changed")
            continue
        problems = put_block(komga, lists, name, block, anchor, backup_dir, say)
        if problems:
            ok = False                                    # tried again on the next run
        else:
            for book_id in block:
                if book_id in fallback_ids:
                    approve_content_anchor(book_id, anchor, "Comic Vine: cover month of the first issue this edition collects", [name])
            if name.startswith("! ") and name.endswith(LIST_SUFFIX):
                touched.append(name[2:-len(LIST_SUFFIX)])
    return sorted(set(touched)), ok


def edition_contents(cv_key, issue_id):
    """The exact Comic Vine issues a collected edition collects (oldest first), or None when Comic Vine does not say."""
    issue = tag_collected._get(cv_key, f"issue/4000-{issue_id}/", field_list="description,name,volume,cover_date")
    segments = breakout.parse_collects(issue.get("description"))
    if not segments:
        return None
    return breakout.resolve_contents(cv_key, segments, tag_collected.search_volumes, tag_collected.volume_issues) or None


def volume_number(name):
    m = re.search(r"(?:\bv|\bvol(?:ume)?\.?)[ ._-]*0*(\d+)", str(name or ""), re.I)
    return int(m.group(1)) if m else None


def sort_volumes(books):
    """Book ids with the numbered volumes of each series folder put in volume order, every volume taking a slot the series already
    held (so the unnumbered books and the series' place in the list do not move)."""
    ids = [b["id"] for b in books]
    groups = {}
    for i, b in enumerate(books):
        n = volume_number(b.get("name"))
        if n is not None:
            groups.setdefault(str(Path(str(b.get("url") or "")).parent), []).append((i, n, b["id"]))
    for rows in groups.values():
        slots = sorted(r[0] for r in rows)
        for slot, row in zip(slots, sorted(rows, key=lambda r: (r[1], r[0]))):
            ids[slot] = row[2]
    return ids


def fallback_anchor(cfg, book, books, anchors, list_name, editions_only=True):
    """A month for a collected edition Comic Vine cannot describe: a hand-set month from the config
    (reconcile_anchor_overrides: {"name text": "YYYY-MM"}), else the anchor of the nearest numbered volume of the same series
    (same folder) that already has one: the previous volume, otherwise the next."""
    name = str(book.get("name") or "")
    for text, month in (cfg.get("reconcile_anchor_overrides") or {}).items():
        if text.lower() in name.lower() and re.fullmatch(r"\d{4}-\d{2}", str(month)):
            return month
    if not editions_only:
        return None

    number = volume_number
    here, folder = number(name), Path(str(book.get("url") or "")).parent
    near = []
    for o in books:
        row = anchors.get(o["id"])
        if o["id"] != book["id"] and row and list_name in row["lists"] and Path(str(o.get("url") or "")).parent == folder:
            n = number(str(o.get("name") or ""))
            if n is not None and here is not None:
                near.append((n, row["anchor"]))
    before = [x for x in near if x[0] < here]
    after = [x for x in near if x[0] > here]
    if before:
        return max(before)[1]
    if after:
        return min(after)[1]
    own = str((book.get("metadata") or {}).get("releaseDate") or "")[:7]     # last resort in a list of editions: its own month
    return own if re.fullmatch(r"\d{4}-\d{2}", own) else None


def reconcile_listed_editions(cfg, komga, cv_key, say=print, dry_run=False):
    """Collected editions that already stand in a "! ... - Suggested Reading Order" list (from before the lists were singles-only)
    must not block the export.  In a list of single issues every unanchored edition is first parked (taken out, remembered in
    _state/list_reconcile.json) so the lists export at once; then, as Comic Vine answers, each parked edition is put back at the month
    of the first issue it collects, or dropped when all of its issues are in the list as singles (or it cannot be dated).  A list that
    is itself a collection of editions (a Masterworks run) keeps its books where they are and only gets approved anchors, kept in
    volume order.  Once per two weeks a split into singles is attempted (breakout).  Returns the base names of the lists changed."""
    import export_komga_readlists as exporter
    state_file = Path(cfg["incoming"]) / "_state" / "list_reconcile.json"
    try:
        tried = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        tried = {}

    def save():
        if not dry_run:
            state_file.write_text(json.dumps(tried, indent=1), encoding="utf-8")
    anchors = exporter.read_content_anchors()
    have = existing_issue_ids(cfg)
    if have is None:
        return []
    lists = {l["name"]: l["id"] for l in komga.paged("/readlists")}
    backup_dir = Path(cfg["incoming"]) / "_state" / "list_backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    budget, touched, now = int(cfg.get("reconcile_max_per_run", 8)), set(), time.time()
    splits = int(cfg.get("reconcile_splits_per_run", 1))

    def set_list(rid, ids, old):
        (backup_dir / f"{rid}-{time.time_ns()}.json").write_text(json.dumps(old))
        komga.call(f"/readlists/{rid}", "PATCH", {"bookIds": ids})

    for name, rid in sorted(lists.items()):
        if not (name.startswith("! ") and name.endswith(LIST_SUFFIX)):
            continue
        base = name[2:-len(LIST_SUFFIX)]
        books = list(komga.paged(f"/readlists/{rid}/books"))
        ids = [b["id"] for b in books]
        singles = {comicvine_issue_id(b) for b in books if not exporter.is_collected_book(b)} - {None}
        editions_only = sum(1 for x in books if exporter.is_collected_book(x)) * 2 >= len(books)
        if editions_only and not dry_run:
            in_order = sort_volumes(books)              # a list of volumes must run v01, v02, ... or later volumes inherit early months
            if in_order != ids:
                set_list(rid, in_order, ids)
                by_id = {x["id"]: x for x in books}
                books, ids = [by_id[i] for i in in_order], in_order
                touched.add(base)
                say(f"  reconcile: {name}: numbered volumes put in volume order")

        def is_anchored(book):
            row = anchors.get(book["id"])
            return bool(row and name in row["lists"])
        work = [(b, False) for b in books if exporter.is_collected_book(b)]
        if not editions_only:
            parked = [b for b in books if exporter.is_collected_book(b) and not is_anchored(b)]
            if parked and not dry_run:
                gone = {b["id"] for b in parked}
                set_list(rid, [x for x in ids if x not in gone], ids)
                ids = [x for x in ids if x not in gone]
                for b in parked:
                    tried.setdefault(b["id"], {}).setdefault("parked_from", [])
                    if name not in tried[b["id"]]["parked_from"]:
                        tried[b["id"]]["parked_from"].append(name)
                save()
                touched.add(base)
                say(f"  reconcile: {name}: {len(parked)} unanchored collected edition(s) parked so the list exports; they are put back as they are dated")
            work = [(b, b["id"] in tried and name in tried[b["id"]].get("parked_from", [])) for b in books if exporter.is_collected_book(b) and is_anchored(b)]
            work += [(b, True) for b in parked]
            for bid, note in tried.items():                        # parked by an earlier run
                if name in note.get("parked_from", []) and bid not in ids and all(w[0]["id"] != bid for w in work):
                    try:
                        work.append((komga.call(f"/books/{bid}"), True))
                    except Exception:                                                # noqa: BLE001
                        pass
        prev_anchor = ""
        if not editions_only:
            work.sort(key=lambda w: is_anchored(w[0]) and not w[1])      # books still to be dated first; re-checks of placed ones last
        for b, is_parked in work:
            row = anchors.get(b["id"])
            anchored = is_anchored(b)
            note = tried.setdefault(b["id"], {})
            cid = comicvine_issue_id(b) or note.get("cv_issue")
            if budget <= 0 and not anchored:
                continue
            if not cid:
                # never matched to Comic Vine (no cv tag): find the issue by name the way tag_collected does; a miss is retried weekly
                if dry_run or now - note.get("match_tried", 0) < 7 * 24 * 3600:
                    continue
                budget -= 1
                note["match_tried"] = now
                try:
                    import tag_collected
                    found = tag_collected.match(cv_key, Path(str(b.get("url") or "")), pause=1.0)
                except Exception as e:                                              # noqa: BLE001
                    say(f"  reconcile: {b['name'][:50]}: Comic Vine search failed ({e}); stopping until the next run")
                    note.pop("match_tried", None)
                    save()
                    return sorted(touched)
                if found.get("status") != "match":
                    say(f"  reconcile: {b['name'][:50]}: not matched to a Comic Vine issue ({found.get('why')})")
                    continue
                cid = note["cv_issue"] = int(found["issue"]["id"])
                say(f"  reconcile: {b['name'][:50]}: matched to Comic Vine issue {cid}")
            if (anchored or note.get("status") == "unresolved") and now - note.get("checked", 0) < 24 * 3600 and not is_parked:
                if anchored and row:
                    prev_anchor = max(prev_anchor, row["anchor"])
                continue                                              # already placed; re-check for retirement once a day

            def unpark():
                lst = note.get("parked_from", [])
                if name in lst:
                    lst.remove(name)
            budget -= 1
            try:
                contents = edition_contents(cv_key, cid)
            except Exception as e:                                                  # noqa: BLE001
                if anchored and not is_parked:
                    note["checked"] = now                                   # only a retirement re-check of a placed book: try again tomorrow
                    continue
                say(f"  reconcile: {b['name'][:50]}: Comic Vine lookup failed ({e}); stopping until the next run")
                save()
                return sorted(touched)
            note["checked"] = now
            fallback = None
            if not contents:
                # A month guessed from a neighbouring volume (or the edition's own date) is only good enough in a list of editions
                # (a collection of Masterworks); among single issues it would misplace the book, so it is left out there.
                fallback = fallback_anchor(cfg, b, books, anchors, name, editions_only)
                if not fallback:
                    if not dry_run and not is_parked and b["id"] in ids:
                        cur = [x for x in ids if x != b["id"]]
                        set_list(rid, cur, ids)
                        ids = cur
                    unpark()
                    note["status"] = "removed"
                    touched.add(base)
                    say(f"  reconcile: {b['name'][:50]}: Comic Vine does not say what it collects, so it cannot be dated among single issues; left out of {name}")
                    continue
            pages = (b.get("media") or {}).get("pagesCount") or 0
            plausible = not pages or not contents or pages / len(contents) <= 60          # the issues found must account for the book (an omnibus resolved to 4 issues does not)
            if contents and plausible and all(int(c["id"]) in singles for c in contents):
                if not dry_run and not is_parked and b["id"] in ids:
                    cur = [x for x in ids if x != b["id"]]
                    set_list(rid, cur, ids)
                    ids = cur
                unpark()
                note["status"] = "retired"
                touched.add(base)
                say(f"  reconcile: {b['name'][:50]}: all {len(contents)} issues are in {name} as singles; edition left out of the list")
                continue
            if not anchored:
                anchor = fallback or month_from_cover_date(contents[0].get("cover_date"))
                if not anchor:
                    continue
                if editions_only:
                    anchor = max(anchor, prev_anchor)               # keep the volumes in order: anchors never go backwards along the list
                if not dry_run:
                    if editions_only:
                        problems = []
                    else:
                        before = list(ids)
                        if b["id"] in ids:
                            ids = [x for x in ids if x != b["id"]]
                            set_list(rid, ids, before)
                        problems = put_block(komga, lists, name, [b["id"]], anchor, backup_dir, say)
                        ids = [x["id"] for x in komga.paged(f"/readlists/{rid}/books")]
                        if problems or b["id"] not in ids:
                            if before != ids:
                                set_list(rid, [x for x in before if x != b["id"]] if is_parked else before, ids)
                                ids = [x for x in before if x != b["id"]] if is_parked else before
                            say(f"  reconcile: {b['name'][:50]}: could not place it at {anchor} yet; list left as it was ({problems or 'book missing after the move'})")
                            continue
                    approve_content_anchor(b["id"], anchor, "fallback: neighbouring volume or hand-set month" if fallback else "Comic Vine: cover month of the first issue this edition collects", [name])
                    anchors[b["id"]] = {"anchor": anchor, "lists": {name}}
                unpark()
                note["status"] = "anchored"
                touched.add(base)
                say(f"  reconcile: {b['name'][:50]}: placed at {anchor} ({'fallback month' if fallback else 'first collected issue'})")
            prev_anchor = max(prev_anchor, (anchors.get(b["id"]) or {}).get("anchor", ""))
            missing = breakout.missing_issues(contents, have) if contents else []
            if missing and splits > 0 and not dry_run and now - note.get("split_tried", 0) > 14 * 24 * 3600:
                splits -= 1
                note["split_tried"] = now
                try:
                    res = breakout_collected(cfg, cv_key, b["url"], {"issue": cid}, have, say=say)
                except Exception as e:                                              # noqa: BLE001
                    res = {"status": "unresolved", "why": f"breakout failed: {e}"}
                note["split"] = res["status"]
                say(f"  reconcile: {b['name'][:50]}: split {res['status']} {res.get('why') or ''}".rstrip())
        save()
    pending = [bid for bid, n in tried.items() if n.get("parked_from") and (n.get("cv_issue") or now - n.get("match_tried", 0) >= 7 * 24 * 3600)]
    flag = Path(cfg["incoming"]) / "_state" / "pause_tagging.flag"
    if not pending and flag.exists() and not dry_run:
        flag.unlink()
        say("  reconcile: nothing left to match or date; tagging resumes")
    return sorted(touched)


def mark_uploaded(cfg, now=None):
    """After the .ps1 has copied _ready to the mirror: stamp those files and move them to _done."""
    now = now or time.time()
    ready, done = Path(cfg["ready"]), Path(cfg["incoming"]) / "_done"
    state, n = load_state(cfg), 0
    for p in sorted(ready.rglob("*")):
        if p.is_file() and p.suffix.lower() in COMIC_SUFFIXES:
            rel = str(p.relative_to(ready))
            if rel in state["files"]:
                state["files"][rel]["uploaded_at"] = now
            target = done / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(p), str(target))
            n += 1
    save_state(cfg, state)
    return n


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stage", choices=["prepare", "komga", "mark-uploaded", "verify-export", "acquire", "reconcile"])
    ap.add_argument("--config", default=str(Path(__file__).with_name("incoming_pipeline.json")))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    cfg["_config_path"] = str(Path(args.config).resolve())
    cfg["_dry_run"] = bool(args.dry_run)
    cache_db = cfg.get("comicvine_cache_db") or str(Path(cfg["incoming"]) / "_state" / "comicvine_cache.sqlite")
    comicvine_store.configure(cache_db, cfg.get("comicvine_requests_per_hour", comicvine_store.DEFAULT_REQUESTS_PER_HOUR))
    if args.stage == "mark-uploaded":
        print(f"{mark_uploaded(cfg)} uploaded file(s) moved to _done")
        return
    if False:
        sys.exit(2)
    if args.stage == "prepare":
        cv_key = komga_title_fix.comicvine_key(cfg.get("comictagger_settings"))
        if not cv_key:
            sys.exit("No Comic Vine key (COMICVINE_KEY or ComicTagger settings).")
        run_prepare(cfg, cv_key, args.dry_run, say=pipeline_say)
        return
    key = os.environ.get("KOMGA_API_KEY")
    if not key:
        sys.exit("Set KOMGA_API_KEY.")
    komga = komga_title_fix.Komga(cfg.get("komga_url", "http://localhost:25600"), key)
    if args.stage == "reconcile":
        cv_key = komga_title_fix.comicvine_key(cfg.get("comictagger_settings"))
        if not cv_key:
            sys.exit("No Comic Vine key (COMICVINE_KEY or ComicTagger settings).")
        changed = reconcile_listed_editions(cfg, komga, cv_key, say=pipeline_say, dry_run=args.dry_run)
        if changed and not args.dry_run:
            st = load_state(cfg)
            st["touched_lists"] = sorted(set(st.get("touched_lists", [])) | set(changed))
            save_state(cfg, st)
        print("reconcile: lists changed: " + (", ".join(changed) or "none"))
        return
    if args.stage == "acquire":
        have = existing_issue_ids(cfg)
        if have is None:
            sys.exit("Set KOMGA_API_KEY.")
        have |= {int(v["issue"]) for v in load_state(cfg).get("files", {}).values() if str(v.get("issue") or "").isdigit()}
        acquire.run_acquire(cfg, have, say=pipeline_say, dry_run=args.dry_run)
        if not args.dry_run:
            write_reacquire_report(cfg)
        return
    if args.stage == "verify-export":
        st = load_state(cfg)
        bad = verify_export(cfg, komga, st.get("touched_lists", []), dest=cfg.get("readlist_dir", r"C:\Users\paris\ArmgddnUploader\readlists"))
        print("export check: " + ("all lists match Komga" if not bad else "MISMATCH in " + ", ".join(bad)))
        if bad:
            # Keep the retry marker so the next pipeline pass exports and verifies again.
            save_state(cfg, st)
            sys.exit(2)
        st["touched_lists"] = []
        save_state(cfg, st)
        return
    run_komga(cfg, komga, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
