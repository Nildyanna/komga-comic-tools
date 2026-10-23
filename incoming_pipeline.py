"""Everything that happens to a comic between "I downloaded it" and "it is on the site and in Komga", in one script.

Drop finished or still-downloading comics into the Incoming folder. Every run (incoming-pipeline.ps1 runs this every 15 minutes) does:

  prepare   for each file that has stopped changing (no .aria2/.part marker, 10+ minutes old):
              1. tag it with Comic Vine (tag_collected: name -> volume -> issue id), unless it already is
              2. fix its title inside the file ("Volume 3" -> "<Series> Vol. 3")
              3. check the cover: the first page must look like Comic Vine's cover for that issue (an ad or credits page
                 as page one would become the site's cover, so such files are HELD, not sent)
              4. work out which collection it belongs to (routes in the config) and move it to _ready\\<Collection>\\<Series>\\
            Anything it is not sure about stays in Incoming and is listed in incoming_report.json (held, with the reason).
  upload    (the .ps1) copies _ready to the Comics mirror, then moves the uploaded files to _done
  komga     once uploaded files are old enough for the mirror mount to show them: scan the Komga library, lock title / number /
            date on the new books, then update the read lists so the site and Komga show the same lists in the same order:
              - "! <Library> - Suggested Reading Order" for the collection (created if missing)
              - the publisher's master list: "! Marvel Universe ...", "! DC Universe ...", or a new "! <Publisher> ..." list
                for any other publisher (created if missing)
            New books go in at the route's anchor date; existing entries never move. Every change is re-read and checked
            (nothing lost, nothing reordered, block in one place, dates around it plausible) and undone if the check fails.
  verify-export  after the hourly exporter has written the .cbl files: each touched list's file must hold exactly the books of the
            Komga list in the same order, otherwise the previous good file is put back before anything is uploaded
  Before any stage touches anything, all the test suites (this script, the tagger, the title fix, the merge tool, the exporter) run;
  if one fails the pipeline stops and says so.
  The site's feed tracker and Tulip's hourly digest announce the new issues on the usual schedule.

Usage: python incoming_pipeline.py prepare|komga|verify-export|mark-uploaded [--config incoming_pipeline.json] [--dry-run]
"""
import argparse
import io
import json
import os
import re
import shutil
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path

import komga_title_fix
import tag_collected
import tagcommon

PARTIAL_SUFFIXES = (".aria2", ".aria2__temp", ".part", ".crdownload", ".tmp", ".torrent")
COMIC_SUFFIXES = (".cbz", ".cbr")
IMAGE = re.compile(r"\.(jpe?g|png|webp)$", re.I)
COVER_OK = 14            # dHash distance (of 64 bits) up to which a page counts as "the Comic Vine cover"
LIST_SUFFIX = " - Suggested Reading Order"
PLAUSIBLE_MONTHS = 36    # dated entries next to an inserted block may be at most this far (in months) from its anchor date
KOMGA_DELAY = 12 * 60    # seconds after an upload before Komga's view of the mirror is trusted (the mount caches directories for 10 minutes)


# ---------------------------------------------------------------- small pure helpers
def sanitize(name):
    """A Comic Vine volume name as a folder name: 'Marvel Masterworks: The Incredible Hulk' -> 'Marvel Masterworks - The Incredible Hulk'."""
    name = re.sub(r"\s*:\s*", " - ", name or "")
    name = re.sub(r'[<>"/\\|?*]', "", name)
    return " ".join(name.split()).strip(" .")


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


def route_for(cfg, volume_name):
    """-> (route, anchor 'YYYY-MM' or None) for a Comic Vine volume name, or (None, None)."""
    for r in cfg.get("routes", []):
        if re.search(r["match"], volume_name or "", re.I):
            for a in r.get("anchors", []):
                if re.search(a["match"], volume_name, re.I):
                    return r, a["anchor"]
            return r, r.get("anchor")
    return None, None


def month_index(date):
    return int(date[:4]) * 12 + int(date[5:7]) - 1


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


def verify_insert(books, new_ids, block, at, anchor):
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
    months, a = list_months(books), month_index(anchor + "-01")
    before = next((m for m in reversed(months[:at]) if m is not None), None)
    after = next((m for m in months[at:] if m is not None), None)
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


def verify_export(cfg, komga, names, dest=r"G:\Comics", say=print):
    """The site shows the .cbl file, so it must equal the Komga list: same books, same order. A bad file is replaced by the newest
    earlier copy from cbl_history. Returns the names that were wrong."""
    lists = {l["name"]: l["id"] for l in komga.paged("/readlists")}
    bad = []
    for base in names:
        rid = lists.get(site_list_name(base))
        path = Path(dest) / f"{base}{LIST_SUFFIX}.cbl"
        if not rid or not path.exists():
            continue
        want = komga_sequence(komga.paged(f"/readlists/{rid}/books"))
        got = cbl_sequence(path.read_text(encoding="utf-8"))
        # the exporter drops books it cannot find, so the file may be shorter, but what it holds must be in Komga's order
        it = iter(want)
        in_order = all(any(g == w for w in it) for g in got)
        if got and in_order and len(got) >= 0.98 * len(want):
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
    for extra in (extra_paths if extra_paths is not None else (str(here), r"G:\Comics")):
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


def first_pages(path, count=4):
    """Images of the first few pages of a .cbz/.cbr, natural order."""
    from PIL import Image
    names = sorted((n for n in tagcommon.list_names(path) if IMAGE.search(n) and "__macosx" not in n.lower()),
                   key=lambda n: [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", n)])[:count]
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


def cover_verdict(pages, cv_image):
    """'ok' | 'later-page' | 'mismatch' | 'unreadable'. pages: PIL images (page 1 first); cv_image: Comic Vine's cover."""
    if not pages or pages[0] is None:
        return "unreadable"
    ref = dhash(cv_image)
    if distance(dhash(pages[0]), ref) <= COVER_OK:
        return "ok"
    if any(p is not None and distance(dhash(p), ref) <= COVER_OK for p in pages[1:]):
        return "later-page"
    return "mismatch"


def cv_cover(cv_key, issue_id):
    import urllib.request
    from PIL import Image
    info = tag_collected._get(cv_key, f"issue/4000-{issue_id}/", field_list="image")["image"]
    req = urllib.request.Request(info["medium_url"], headers={"User-Agent": "komga-comic-tools"})
    return Image.open(io.BytesIO(urllib.request.urlopen(req, timeout=30).read()))


# ---------------------------------------------------------------- in-file title and tags
def issue_id_of(path):
    raw = tagcommon.read_raw_root_info(path)
    m = re.search(r"Issue ID (\d+)", raw) or re.search(r"4000-(\d+)", raw)
    return int(m.group(1)) if m else None


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
    """File names whose cover was looked at and approved: one per line in Incoming\\_state\\approved_covers.txt."""
    try:
        text = (Path(cfg["incoming"]) / "_state" / "approved_covers.txt").read_text(encoding="utf-8")
    except OSError:
        return set()
    return {line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")}


def prepare_file(path, cfg, cv_key, exe="comictagger", dry_run=False, say=print):
    """-> dict(status 'ready'|'held', reason, dest, title, volume, number, issue). Moves nothing; the caller moves 'ready' files.

    Every check happens before the file is touched, so a held file stays exactly as it was downloaded."""
    out = {"file": str(path)}
    tagged = tag_collected.already_tagged(path)
    # which Comic Vine issue is this?
    if tagged:
        issue_id = issue_id_of(path)
        if not issue_id:
            return {**out, "status": "held", "reason": "tagged, but the Comic Vine issue could not be read from the file"}
    else:
        m = tag_collected.match(cv_key, path, aliases=cfg.get("aliases"))
        if m["status"] != "match":
            return {**out, "status": "held", "reason": f"not tagged: {m['why']}"}
        issue_id = m["issue"]["id"]
    issue = tag_collected._get(cv_key, f"issue/4000-{issue_id}/", field_list="name,issue_number,volume")
    volume_name = (issue.get("volume") or {}).get("name") or ""
    vol = {}
    try:
        vol = tag_collected._get(cv_key, f"volume/4050-{(issue.get('volume') or {}).get('id')}/", field_list="publisher,start_year") or {}
    except Exception:                                                              # noqa: BLE001
        pass
    publisher = (vol.get("publisher") or {}).get("name") or "Unknown"
    # route
    route, anchor = route_for(cfg, volume_name)
    if not route and cfg.get("default_route"):
        coll = cfg["default_route"]["collection"].format(publisher=sanitize(publisher))
        route = {"collection": coll}
    if not route:
        return {**out, "status": "held", "reason": f"no collection route for the series {volume_name!r} ({publisher}); add one to the config", "issue": issue_id}
    anchor_guess = False
    if not anchor and str(vol.get("start_year") or "").isdigit():
        anchor, anchor_guess = f"{int(vol['start_year']):04d}-01", True
    # cover
    try:
        verdict = cover_verdict(first_pages(path), cv_cover(cv_key, issue_id))
    except zipfile.BadZipFile:
        return {**out, "status": "held", "reason": "the archive is damaged or incomplete (it cannot be opened as a zip); download it again", "issue": issue_id}
    except Exception as e:                                                         # noqa: BLE001
        verdict = f"unreadable ({e})"
    if verdict in ("mismatch", "later-page") and path.name in approved_names(cfg):
        verdict = "ok"                  # looked at and approved: a digital edition often has its own cover art
    if verdict != "ok":
        why = {"later-page": "page one is not the cover (an ad or credits page?); the cover is a later page",
               "mismatch": "page one does not look like the Comic Vine cover (a different edition's cover art is fine: approve it by adding the file name to _state\\approved_covers.txt)"}.get(verdict, f"cover check: {verdict}")
        return {**out, "status": "held", "reason": why, "issue": issue_id}
    title = komga_title_fix.new_title(issue.get("name"), volume_name, issue.get("issue_number")) or issue.get("name")
    # all checks passed: now change the file
    if not dry_run:
        if not tagged and not tag_collected.tag(exe, path, issue_id):
            return {**out, "status": "held", "reason": "ComicTagger could not write the tags", "issue": issue_id}
        if title and not set_title(path, title):
            return {**out, "status": "held", "reason": "could not write the fixed title into the file", "issue": issue_id}
    dest = Path(route["collection"]) / sanitize(volume_name) / path.name
    return {**out, "status": "ready", "dest": str(dest), "title": title, "volume": volume_name, "number": issue.get("issue_number"),
            "issue": issue_id, "collection": route["collection"], "library": route.get("library", route["collection"]),
            "anchor": anchor, "anchor_guess": anchor_guess, "publisher": publisher,
            "lists": route.get("lists") or [site_list_name(master_base(cfg, publisher))]}


def run_prepare(cfg, cv_key, dry_run=False, say=print, now=None):
    incoming, ready = Path(cfg["incoming"]), Path(cfg["ready"])
    now = now or time.time()
    skip = {ready.resolve(), (incoming / "_done").resolve(), (incoming / "_state").resolve()}
    files = [p for p in sorted(incoming.rglob("*")) if p.is_file() and p.suffix.lower() in COMIC_SUFFIXES
             and not any(parent.resolve() in skip for parent in p.parents)]
    state = load_state(cfg)
    report, moved = {"time": datetime.now().isoformat(timespec="seconds"), "held": [], "waiting": [], "ready": []}, 0
    for p in files:
        if not is_finished(p, now, cfg.get("min_age_minutes", 10)):
            report["waiting"].append(p.name)
            continue
        try:
            r = prepare_file(p, cfg, cv_key, cfg.get("comictagger", "comictagger"), dry_run, say)
        except Exception as e:                                                     # noqa: BLE001
            r = {"file": str(p), "status": "held", "reason": f"error: {e}"}
        if r["status"] == "ready":
            say(f"  ready: {p.name} -> {r['dest']}  [{r['title']}]")
            report["ready"].append(p.name)
            if not dry_run:
                target = ready / r["dest"]
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(p), str(target))
                state["files"][r["dest"]] = {k: r.get(k) for k in ("title", "volume", "number", "issue", "collection", "library", "anchor", "anchor_guess", "publisher", "lists")}
                moved += 1
        else:
            say(f"  HELD: {p.name} ({r['reason']})")
            report["held"].append({"file": p.name, "reason": r["reason"]})
    if not dry_run:
        save_state(cfg, state)
        write_report(cfg, report)
    say(f"{len(files)} comic file(s): {moved} moved to _ready, {len(report['held'])} held, {len(report['waiting'])} still downloading")
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


# ---------------------------------------------------------------- stage 3: Komga
def books_of(komga, path):
    return komga.paged(path)


def run_komga(cfg, komga, now=None, say=print, dry_run=False):
    """Finish the files uploaded at least KOMGA_DELAY ago: lock metadata, update read lists. Returns the number completed."""
    now = now or time.time()
    state = load_state(cfg)
    # a file is due until both its metadata is locked and its read lists are brought up to date (lists_done): files finished by an
    # older version of this script get their lists checked once more
    due = {k: v for k, v in state["files"].items()
           if v.get("uploaded_at") and not (v.get("komga_done") and v.get("lists_done")) and now - v["uploaded_at"] >= KOMGA_DELAY}
    if not due:
        say("nothing waiting for Komga")
        return 0
    libs = {l["name"]: l["id"] for l in komga.call("/libraries")}
    by_library = {}
    for k, v in due.items():
        by_library.setdefault(v.get("library", v["collection"]), []).append((k, v))
    done = 0
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
        for k, v in sorted(items, key=lambda kv: (kv[1]["volume"], float(kv[1]["number"] or 0))):
            b = next((b for b in books if re.split(r"[\\/]", b["url"])[-1] == Path(k).name and not b.get("deleted")), None)
            if b is None:
                say(f"  Komga has not picked up {Path(k).name} yet")
                continue
            found.append((k, v, b))
        for k, v, b in found:
            m = b["metadata"]
            body = {"title": v["title"], "titleLock": True, "numberLock": True, "numberSortLock": True}
            if m.get("releaseDate"):
                body["releaseDateLock"] = True
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
            done += 1
    if not dry_run:
        save_state(cfg, state)
    return done


def add_to_include(cfg, names):
    """Tell the exporter these lists belong on the site whatever their size (readlist_include.txt). Only lists filled from Incoming
    are ever added, so collections put into Komga by hand stay off the site."""
    path = Path(cfg.get("include_file", r"G:\Comics\readlist_include.txt"))
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
    at = placement_index([(b.get("metadata") or {}).get("releaseDate") or "" for b in books], anchor)
    new = insert_block(ids, block, at)
    if len(new) == len(ids):
        return []
    problems = verify_insert(books, new, block, at, anchor)
    if problems:
        say(f"  {name}: NOT changed ({'; '.join(problems)})")
        return problems
    (backup_dir / f"{rid}-{int(time.time())}.json").write_text(json.dumps(ids))
    komga.call(f"/readlists/{rid}", "PATCH", {"bookIds": new})
    check = [b["id"] for b in komga.paged(f"/readlists/{rid}/books")]
    if check != new:
        komga.call(f"/readlists/{rid}", "PATCH", {"bookIds": ids})
        say(f"  {name}: Komga did not keep the change; restored the previous order")
        return ["Komga did not keep the change"]
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
            (backup_dir / f"{own}-{int(time.time())}.json").write_text(json.dumps(cur))
            komga.call(f"/readlists/{own}", "PATCH", {"bookIds": new})
            say(f"  {library}: {len(new) - len(cur)} book(s) added to its read list")
    # the collection's site list, then the publisher's master list(s)
    wanted, ok = {}, True
    for k, v, b in found:
        names = [site_list_name(library)] + list(v.get("lists") or [])
        for name in names:
            wanted.setdefault((name, v.get("anchor")), []).append(b["id"])
    for (name, anchor), block in wanted.items():
        if not anchor:
            say(f"  {name}: no anchor date, not changed")
            continue
        problems = put_block(komga, lists, name, block, anchor, backup_dir, say)
        if problems:
            ok = False                                    # tried again on the next run
        elif name.startswith("! ") and name.endswith(LIST_SUFFIX):
            touched.append(name[2:-len(LIST_SUFFIX)])
    return sorted(set(touched)), ok


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
    ap.add_argument("stage", choices=["prepare", "komga", "mark-uploaded", "verify-export"])
    ap.add_argument("--config", default=str(Path(__file__).with_name("incoming_pipeline.json")))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    if args.stage == "mark-uploaded":
        print(f"{mark_uploaded(cfg)} uploaded file(s) moved to _done")
        return
    if not run_selftests():
        sys.exit(2)
    if args.stage == "prepare":
        cv_key = komga_title_fix.comicvine_key(cfg.get("comictagger_settings"))
        if not cv_key:
            sys.exit("No Comic Vine key (COMICVINE_KEY or ComicTagger settings).")
        run_prepare(cfg, cv_key, args.dry_run)
        return
    key = os.environ.get("KOMGA_API_KEY")
    if not key:
        sys.exit("Set KOMGA_API_KEY.")
    komga = komga_title_fix.Komga(cfg.get("komga_url", "http://localhost:25600"), key)
    if args.stage == "verify-export":
        st = load_state(cfg)
        bad = verify_export(cfg, komga, st.get("touched_lists", []))
        print("export check: " + ("all lists match Komga" if not bad else "MISMATCH in " + ", ".join(bad)))
        st["touched_lists"] = []
        save_state(cfg, st)
        return
    run_komga(cfg, komga, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
