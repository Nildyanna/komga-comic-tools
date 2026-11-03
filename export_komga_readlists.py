"""Keep the ARMGDDN site's reading orders in step with George's Komga.

Komga is the source of truth. Every run (hourly, ArmgddnReadlistExport):

 1. Collections: a missing list is created only when every item is a locked, Comic Vine-linked single issue with a
    verified date. Existing lists are never rebuilt. To stop a
    collection ever getting a list, put its library name on a line in C:\\Users\\paris\\ArmgddnUploader\\readlists\\readlist_ignore.txt.
 2. Export: every "! <name> - Suggested Reading Order" list with more than 40 books is read in its current order and
    written to G:\\Comics\\<name> - Suggested Reading Order.cbl (only when the content changed; the old copy goes to
    G:\\Comics\\cbl_history). Smaller lists (one-off events) and lists named any other way are never exported.
    The one exception: a list named on a line in C:\\Users\\paris\\ArmgddnUploader\\readlists\\readlist_include.txt is exported at any size. The comics
    pipeline (incoming_pipeline.py) adds the lists it fills from the Incoming folder there, so staged content shows up on
    the site while collections added to Komga by hand stay off it.
    A list whose books cannot be found at all (nothing to write) keeps its previous file.
 3. Clean up: .cbl files whose list was deleted, renamed or no longer qualifies are moved to cbl_history, and
    C:\\Users\\paris\\ArmgddnUploader\\readlists\\readlists-manifest.txt (the names that SHOULD be on the site) is rewritten. The site hides anything
    not in that manifest and its server tidy job removes the stale file from the mirror.
 4. Upload filter: the include list in the uploader's comics-filter.txt is rebuilt from the qualifying collections
    (plus every "<Name> Collection" folder, big or small), so a new collection's comics and its list both reach the site.

Komga membership and progress are never changed by this exporter. Curated ! reading lists allow singles plus collected editions with explicit, list-scoped content-date anchors in komga_readlist_backups/content_order_anchors.json. For a changed list, an existing CBL may be extended with verified single issues only when every old entry remains in the same relative order; dropping, moving, or adding collected editions still requires verified anchors. Any verified single-issue chronology reversal blocks the export, and the previous .cbl file is kept. --dry-run changes nothing at all.
"""
import json
import ntpath
import os
import re
import shutil
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

try:
    import sync_universe_readlists as k      # shared local Komga connection and the ordering helper `sk`
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import sync_universe_readlists as k

DEST = r"C:\Users\paris\ArmgddnUploader\readlists"
MIRROR_DRIVE = "R:\\"          # the read-only Comics mirror mount; Komga records the real path for folders that are junctions to it
LIBRARY_PARENT = "R:\\"   # collection folders must sit directly under this
HISTORY = os.path.join(DEST, "cbl_history")
MANIFEST = os.path.join(DEST, "readlists-manifest.txt")
METADATA = os.path.join(DEST, "comics-metadata.txt")   # JSON, .txt so the uploader filter's "- *.json" still applies to everything else
IGNORE = os.path.join(DEST, "readlist_ignore.txt")
INCLUDE = os.path.join(DEST, "readlist_include.txt")   # lists the comics pipeline fills from Incoming: exported whatever their size
CONTENT_ANCHORS = os.path.join(DEST, "content_order_anchors.json")
LOCK = os.path.join(DEST, "readlist_export.lock")
UPLOADER_FILTER = r"C:\Users\paris\ArmgddnUploader\comics-filter.txt"
KEEP = 10
MIN_BOOKS = 41            # "over 40": a real main-run collection, not a one-off event (those run 1 to ~20 books)
MAX_CREATE = 3            # new lists per run
NAME_RE = re.compile(r"^! (.+) - Suggested Reading Order$", re.I)
FILE_SUFFIX = " - Suggested Reading Order.cbl"
BAD_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
SKIP_WORDS = {"the", "collection", "universe", "complete", "run", "and"}
FILTER_HEAD = ["- *.json", "- desktop.ini", "- Thumbs.db", "- .tmp.*/**", "- *before_repair*", "- *(backup*"]
COLLECTED_RE = re.compile(r"\b(?:masterworks?|omnibus|compendium|archives?|collected edition|graphic novel|complete collection|complete series|epic collection|library edition|deluxe edition|haunted knight)\b", re.I)
NAMED_VOLUME_RE = re.compile(r"\b(?:v|vol(?:ume)?)[ ._-]*0*\d{1,3}\s*[-–:]\s*\S", re.I)
BY_VOLUME_RE = re.compile(r"\bby\b.*\b(?:v|vol(?:ume)?)[ ._-]*0*\d{1,3}\b", re.I)
VERIFIED_DATE_RE = re.compile(r"^(19\d{2}|20\d{2})-(0[1-9]|1[0-2])(?:-(0[1-9]|[12]\d|3[01]))?$")
DRY = "--dry-run" in sys.argv


def key_of(text):
    """'Spider Man Collection', 'Spider-Man Collection' and 'Spider-Man' all give 'spiderman'."""
    words = [w for w in re.findall(r"[a-z0-9]+", str(text or "").lower()) if w not in SKIP_WORDS]
    return "".join(words)


def read_ignore(path=None):
    try:
        lines = open(path or IGNORE, encoding="utf-8").read().splitlines()
    except OSError:
        return set()
    return {key_of(l) for l in lines if l.strip() and not l.strip().startswith("#")}


def read_include(path=None):
    """Keys (see key_of) of the lists that are exported whatever their size."""
    return read_ignore(path or INCLUDE)


def is_collected_book(book):
    """True for editions whose story dates differ from their publication date; never infer their chronology from the edition date."""
    m = book.get("metadata") or {}
    name = str(book.get("name") or "")
    series = str(book.get("seriesTitle") or "")
    title = str(m.get("title") or "")
    fmt = str(m.get("format") or "")
    text = " ".join((name, series, title, fmt))
    if COLLECTED_RE.search(text):
        return True
    # Comic Vine often represents named trade volumes as issue #1 and leaves the format out of its labels.
    # Match explicit named-volume subtitles even without a "by <author>" phrase (for example,
    # "Ultimate Spider-Man Vol. 02 - The Paper"). The separator-plus-subtitle avoids treating
    # ordinary numbered issues in a series volume as collected editions.
    return bool(BY_VOLUME_RE.search(name) or NAMED_VOLUME_RE.search(name + " " + title))


def verified_issue_date(book):
    """Return a locked, Comic Vine-linked issue date. Inferred dates are never used as chronology evidence."""
    m = book.get("metadata") or {}
    date = str(m.get("releaseDate") or "")
    linked = "comicvine" in {str(t).lower() for t in (m.get("tags") or [])} or any(
        "comicvine" in str(link.get("url") or "").lower() for link in (m.get("links") or []) if isinstance(link, dict))
    if not m.get("releaseDateLock") or not linked or not VERIFIED_DATE_RE.fullmatch(date):
        return None
    return date[:7]


def chronology_inversions(book_ids, books):
    """Out-of-order verified singles; collected editions are left for contents-level review."""
    problems, previous = [], None
    for pos, bid in enumerate(book_ids, 1):
        b = books.get(bid)
        if not b or is_collected_book(b):
            continue
        date = verified_issue_date(b)
        if not date:
            continue
        if previous and date < previous[0]:
            problems.append((previous[1], pos, previous[0], date, b.get("name") or ""))
        previous = (date, pos)
    return problems


def read_content_anchors():
    """Explicit list-scoped story dates for collected editions and issue-level chronology exceptions."""
    try:
        with open(CONTENT_ANCHORS, encoding="utf-8") as f:
            rows = json.load(f).get("books", {})
        return {str(bid): {"anchor": str(row.get("anchor") or "")[:7],
                           "lists": set(row.get("lists") or [])} for bid, row in rows.items()
                if isinstance(row, dict) and VERIFIED_DATE_RE.fullmatch(str(row.get("anchor") or ""))}
    except (OSError, ValueError, AttributeError):
        return {}


def anchored_chronology_inversions(book_ids, books, anchors, list_name=None):
    """Check publication dates, with explicit list-scoped dates for story-order exceptions and collections."""
    problems, previous = [], None
    for pos, bid in enumerate(book_ids, 1):
        b = books.get(bid)
        if not b:
            continue
        row = anchors.get(bid, {})
        explicit = row.get("anchor") if list_name and list_name in row.get("lists", set()) else None
        date = explicit or (None if is_collected_book(b) else verified_issue_date(b))
        if not date:
            continue
        if previous and date < previous[0]:
            problems.append((previous[1], pos, previous[0], date, b.get("name") or ""))
        previous = (date, pos)
    return problems


def verified_creation_order(items):
    """Only make a new list when every item is a dated, linked single issue. Existing lists are never rebuilt."""
    rows = []
    for pos, b in enumerate(items):
        if is_collected_book(b):
            return None
        date = verified_issue_date(b)
        if not date:
            return None
        rows.append((date, pos, b))
    return [b for _, _, b in sorted(rows, key=lambda row: (row[0], row[1]))]


def fetch_all(path_fmt):
    """Every item of a paged Komga list. Komga may cap the page size, so go by its own page count."""
    out, page = [], 0
    while True:
        r = k.req(path_fmt % page)
        if not isinstance(r, dict):
            return None
        content = r.get("content", [])
        out.extend(content)
        if r.get("last", True) or not content or page + 1 >= int(r.get("totalPages", 1) or 1):
            return out
        page += 1


def load_libraries_and_books():
    libs = k.req("/libraries")
    if not isinstance(libs, list) or not libs:
        return None, None
    by_lib, books = {}, {}
    for lib in libs:
        items = fetch_all("/books?library_id=" + str(lib["id"]) + "&page=%d&size=5000")
        if items is None:
            return None, None
        by_lib[lib["id"]] = items
        for b in items:
            books[b["id"]] = b
    return [(l, by_lib[l["id"]]) for l in libs], books


def build_metadata(books):
    """Komga fields George has locked (title, number, release date, tags), keyed by the file's path under G:\\Comics with
    forward slashes, which is also its path in the Comics mirror. Fields that were never locked are left to the file name."""
    root = LIBRARY_PARENT.lower().rstrip("\\") + "\\"
    items = {}
    for b in books.values():
        m = b.get("metadata") or {}
        url = str(b.get("url") or "")
        if url.lower().startswith(MIRROR_DRIVE.lower()):       # a folder linked to the Comics mirror (R:) is the same path under G:\\Comics
            url = root + url[len(MIRROR_DRIVE):]
        if not url.lower().startswith(root):
            continue
        row = {}
        if m.get("titleLock") and m.get("title"):
            row["t"] = m["title"]
        if m.get("numberLock") and m.get("number") not in (None, ""):
            row["n"] = str(m["number"])
        if m.get("releaseDateLock") and m.get("releaseDate"):
            row["d"] = m["releaseDate"]
        if m.get("tagsLock") and m.get("tags"):
            row["g"] = sorted(m["tags"])
        cv = next((l.get("url") for l in (m.get("links") or []) if "comicvine" in str(l.get("url") or "")), None)
        if cv or "comicvine" in (m.get("tags") or []):
            row["c"] = cv or ""                  # scanned against ComicVine; the link when Komga has one
        if row:
            items[url[len(root):].replace("\\", "/")] = row
    return items


def build_cbl(name, ids, books):
    lines = ['<?xml version="1.0" encoding="utf-8"?>', "<ReadingList>",
             "  <Name>%s - Suggested Reading Order</Name>" % k.esc(name), "  <Books>"]
    n = 0
    for bid in ids:
        b = books.get(bid)
        if not b:
            continue
        m = b.get("metadata") or {}
        num = str(m.get("number") or b.get("number") or 1)
        lines.append('    <Book Series="%s" Number="%s" Year="%s" />' % (k.esc(b.get("seriesTitle")), k.esc(num), k.sk(b)[0]))
        n += 1
    lines += ["  </Books>", "</ReadingList>"]
    return "\n".join(lines), n


def book_count(text):
    return len(re.findall(r"<Book ", text))


def cbl_entries(text):
    """Read the identity sequence represented by a CBL file."""
    try:
        root = ET.fromstring(text)
        return [(b.get("Series", ""), b.get("Number", ""), b.get("Year", ""))
                for b in root.findall("./Books/Book")]
    except (ET.ParseError, TypeError):
        return None


def safe_verified_single_insertions(old_text, new_text, ids, books):
    """Permit pipeline-added verified single issues while preserving every existing CBL entry/order.

    This does not authorize dropping, moving, or adding collected editions.
    """
    old_entries, new_entries = cbl_entries(old_text), cbl_entries(new_text)
    if not old_entries or new_entries is None or len(new_entries) != len(ids):
        return False
    preserved, old_index = set(), 0
    for pos, entry in enumerate(new_entries):
        if old_index < len(old_entries) and entry == old_entries[old_index]:
            preserved.add(pos)
            old_index += 1
    if old_index != len(old_entries):
        return False
    additions = [pos for pos in range(len(new_entries)) if pos not in preserved]
    return bool(additions) and all(
        not is_collected_book(books[ids[pos]]) and verified_issue_date(books[ids[pos]])
        for pos in additions
    )


def komga_order_matches(ids, books):
    """A collection list must be fully resolvable before its current Komga order is published."""
    return bool(ids) and len(ids) == len(set(ids)) and all(bid in books for bid in ids)


def covered_by_content(lib_ids, items, lists_by_name):
    """A list made (almost) entirely of one collection's books covers that collection, whatever it is called.
    Universe lists (several collections mixed) do not: no single library reaches 90% of them."""
    ids = set(lib_ids)
    for ids_in_list in lists_by_name:
        if not ids_in_list:
            continue
        inside = sum(1 for b in ids_in_list if b in ids)
        if inside >= 0.9 * len(ids_in_list) and inside >= 0.5 * len(ids):
            return True
    return False


def collections_needing_lists(libs_with_books, existing_lists, ignored):
    """Libraries with enough books and no list covering them (by name, or by what the list holds), minus the ignored ones.
    existing_lists: [(base name, [book ids])] for every "! ... - Suggested Reading Order" list, of any size."""
    have = {key_of(base) for base, _ in existing_lists}
    contents = [ids for _, ids in existing_lists]
    out = []
    for lib, items in libs_with_books:
        name = str(lib.get("name") or "").strip()
        if len(items) < MIN_BOOKS or not name or BAD_FILENAME_CHARS.search(name):
            continue
        key = key_of(name)
        if not key or key in have or key in ignored:
            continue
        if covered_by_content([b["id"] for b in items], items, contents):
            continue
        out.append((name, items))
    return out


def create_list(name, items):
    unique = list({b["id"]: b for b in items}.values())
    ordered = verified_creation_order(unique)
    if ordered is None:
        return None
    payload = {"name": "! %s - Suggested Reading Order" % name, "bookIds": [b["id"] for b in ordered], "ordered": True}
    return k.req("/readlists", "POST", payload)


def build_filter(folders):
    lines = list(FILTER_HEAD) + ["+ /readlists-manifest.txt", "+ /comics-metadata.txt"]
    lines += ["+ /%s/**" % f for f in sorted(folders, key=str.lower)]
    lines += ["+ /* Collection/**"]            # every "<Name> Collection" folder goes up, even small ones (George, 2026-10-21)
    lines += ["+ /*Reading Order.cbl", "- *"]
    return "\n".join(lines) + "\n"


def qualifying_folders(libs_with_books):
    """Collection folders that sit directly under G:\\Comics and hold more than 40 books."""
    out = set()
    for lib, items in libs_with_books:
        root = str(lib.get("root") or "").replace("/", "\\").rstrip("\\")
        if len(items) >= MIN_BOOKS and ntpath.dirname(root).lower() == LIBRARY_PARENT.lower():
            out.add(ntpath.basename(root))
    return out


def archive(path, name, move=False, tag=""):
    os.makedirs(HISTORY, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(HISTORY, "%s_%s%s.cbl" % (name, stamp, tag))
    (shutil.move if move else shutil.copy2)(path, dest)
    olds = sorted(f for f in os.listdir(HISTORY) if f.startswith(name + "_"))
    for f in olds[:-KEEP]:
        os.remove(os.path.join(HISTORY, f))


def atomic_write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def main():
    r = k.req("/readlists?unpaged=true")
    rows = r.get("content") if isinstance(r, dict) else None
    if not isinstance(rows, list):
        print("Komga read-list request failed; nothing changed")
        return 1
    libs_with_books, books = load_libraries_and_books()
    if not books:
        print("Komga library/book request failed; nothing changed")
        return 1

    # 1. create missing collection lists
    existing = [(m.group(1), x.get("bookIds") or []) for m, x in ((NAME_RE.match(str(x.get("name") or "").strip()), x) for x in rows) if m]
    created = 0
    for name, items in collections_needing_lists(libs_with_books, existing, read_ignore()):
        if created >= MAX_CREATE:
            print("%-26s would get a list; limit of %d new lists per run reached, next run" % (name, MAX_CREATE))
            continue
        if DRY:
            print("%-26s %d books, no list: WOULD CREATE (dry run)" % (name, len(items)))
            continue
        res = create_list(name, items)
        if isinstance(res, dict) and res.get("id"):
            print("%-26s %d books, no list: CREATED in Komga" % (name, len(items)))
            rows.append(res)
            created += 1
        else:
            print("%-26s could not be created this run; will retry" % name)

    # 2. export every qualifying list
    by_name = {str(x.get("name") or "").lower(): x for x in rows}
    wanted, exported_files, changed = [], set(), 0
    for key, rl in sorted(by_name.items()):
        m = NAME_RE.match(str(rl.get("name") or "").strip())
        if not m:
            continue
        name = m.group(1).strip()
        count = len(rl.get("bookIds") or [])
        if count < MIN_BOOKS and key_of(name) not in read_include():
            print("%-26s only %d books (needs more than 40); not a collection, skipped" % (name, count))
            continue
        if BAD_FILENAME_CHARS.search(name) or not name:
            print("%-26s name has characters not allowed in a file name; skipped" % name)
            continue
        wanted.append((name, rl))
    for name, rl in wanted:
        ids = rl.get("bookIds") or []
        fname = name + FILE_SUFFIX
        exported_files.add(fname)                         # still a valid list even if this run could not refresh it
        if not komga_order_matches(ids, books):
            print("%-26s Komga list has duplicate or unresolved book IDs; kept the previous file" % name)
            continue
        text, n = build_cbl(name, ids, books)
        if n != len(ids):
            print("%-26s only %d of %d books resolved; kept the previous file" % (name, n, len(ids)))
            continue
        path = os.path.join(DEST, fname)
        old = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
        # A list that already exactly matches Komga needs no content-date anchors: there is no
        # proposed reordering or changed export to validate. Still check verified single-issue
        # chronology before treating that no-op export as clean.
        anchors = read_content_anchors()
        inversions = anchored_chronology_inversions(ids, books, anchors, str(rl.get("name") or ""))
        same_export = old.replace("\r\n", "\n").strip() == text.strip()
        if same_export:
            if inversions:
                first = inversions[0]
                print("%-26s unchanged CBL, but verified single-issue chronology has %d reversal(s) (positions %d to %d, %s to %s); kept the existing file" %
                      (name, len(inversions), first[0], first[1], first[2], first[3]))
            else:
                print("%-26s unchanged (%d books)" % (name, n))
            continue
        collected = [books[bid] for bid in ids if is_collected_book(books[bid])]
        list_name = str(rl.get("name") or "")
        unanchored = [b for b in collected if b["id"] not in anchors or list_name not in anchors[b["id"]]["lists"]]
        safe_insertions = safe_verified_single_insertions(old, text, ids, books)
        if unanchored and safe_insertions:
            print("%-26s contains %d unanchored collected edition(s); preserved all existing entries and accepted only verified single-issue insertions" %
                  (name, len(unanchored)))
        elif unanchored:
            sample = ", ".join(str(b.get("name") or "")[:52] for b in unanchored[:3])
            print("%-26s contains %d collected edition(s) without approved content dates (%s); kept the previous file" %
                  (name, len(unanchored), sample))
            continue
        if inversions:
            first = inversions[0]
            print("%-26s verified single-issue chronology has %d reversal(s) (positions %d to %d, %s to %s); kept the previous file" %
                  (name, len(inversions), first[0], first[1], first[2], first[3]))
            continue
        print("%-26s CHANGED: %d -> %d books%s" % (name, book_count(old), n, "  (dry run, not written)" if DRY else ""))
        changed += 1
        if DRY:
            continue
        if old:
            archive(path, name)
        atomic_write(path, text)

    # 3. clean up stale files and publish the manifest (only when there is something sensible to publish)
    removed = 0
    if wanted:
        for f in sorted(os.listdir(DEST)):
            if f.endswith(FILE_SUFFIX) and f not in exported_files:
                print("%-26s list deleted, renamed or no longer a collection: %s" % (f[:-len(FILE_SUFFIX)], "would remove (dry run)" if DRY else "taken off the site list"))
                removed += 1
                if not DRY:
                    archive(os.path.join(DEST, f), f[:-len(FILE_SUFFIX)], move=True, tag="_removed")
        if not DRY:
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            atomic_write(MANIFEST, "# generated %s\n%s\n" % (stamp, "\n".join(sorted(exported_files))))

    # 3b. publish the locked titles, numbers, dates and tags (only when something changed, and never an empty or tiny set)
    meta = build_metadata(books)
    if len(meta) >= 10 and not DRY:
        text = json.dumps({"items": meta}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        try:
            old = open(METADATA, encoding="utf-8").read()
        except OSError:
            old = ""
        if old != text:
            atomic_write(METADATA, text)
            print("comics metadata: %d books with locked titles, numbers, dates or tags written" % len(meta))

    # 4. keep the upload filter in step with the qualifying collections
    folders = qualifying_folders(libs_with_books)
    if folders and os.path.isdir(os.path.dirname(UPLOADER_FILTER)):
        new_filter = build_filter(folders)
        try:
            cur = open(UPLOADER_FILTER, encoding="utf-8").read()
        except OSError:
            cur = ""
        if cur.replace("\r\n", "\n") != new_filter:
            print("upload filter: collections now %s%s" % (", ".join(sorted(folders, key=str.lower)), "  (dry run, not written)" if DRY else ""))
            if not DRY:
                atomic_write(UPLOADER_FILTER, new_filter)
    print("done: %d created, %d exported, %d removed%s" % (created, changed, removed, " (dry run)" if DRY else ""))
    return 0


def run_locked():
    """One run at a time. The OS lock is released on exit/crash, so an abandoned lock file cannot block later runs."""
    lock_file = None
    lock_backend = None
    if not DRY:
        os.makedirs(os.path.dirname(LOCK), exist_ok=True)
        lock_file = open(LOCK, "a+b")
        if os.name == "nt":
            import msvcrt
            lock_file.seek(0)
            if not lock_file.read(1):
                lock_file.seek(0)
                lock_file.write(b"\0")
                lock_file.flush()
            lock_file.seek(0)
            try:
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                lock_file.close()
                print("another exporter run is still active; skipped")
                return 0
            lock_backend = msvcrt
        else:
            import fcntl
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                lock_file.close()
                print("another exporter run is still active; skipped")
                return 0
            lock_backend = fcntl
    try:
        return main()
    finally:
        if lock_file:
            try:
                if os.name == "nt":
                    lock_file.seek(0)
                    lock_backend.locking(lock_file.fileno(), lock_backend.LK_UNLCK, 1)
                else:
                    lock_backend.flock(lock_file.fileno(), lock_backend.LOCK_UN)
            finally:
                lock_file.close()


if __name__ == "__main__":
    sys.exit(run_locked())

