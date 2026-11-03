"""Break a collected edition (trade, omnibus, "Volume N" book) into the single issues it collects, so the reading lists can hold
single issues only and still contain every story in the right order.

For one collected edition the pipeline asks, in this order:

  1. contents   what does it collect?  Comic Vine's description says ("Collects Thor (2007) #1-6"); parse_collects reads that and
                resolve_contents turns it into the list of Comic Vine issues, in order.
  2. coverage   are those issues already in Komga as singles?  Then nothing more is needed: the singles are already in the lists.
  3. break out  when some are missing, split the book: the pages are read with OCR (breakout_ocr.py, run with the interpreter that
                has RapidOCR installed) and find_starts looks for each issue's own title page.  The split is accepted ONLY when every
                expected issue has a title page, in order, with plausible lengths (verified).  The singles are written, tagged with
                their Comic Vine issue id, into Incoming\\_breakout, where the normal pipeline routes, uploads and lists them by cover date.
  4. otherwise  the book stays in its library, its missing issues are written to _state\\acquisition_wanted.json (nothing is guessed
                into a list) and the next download of those singles fills the gap automatically.
"""
import html
import json
import re
import subprocess
import time
import unicodedata
import zipfile
from difflib import SequenceMatcher
from pathlib import Path

MIN_ISSUE_PAGES = 10          # an issue shorter than this is not an issue
MAX_ISSUE_PAGES = 90          # ... and nothing longer than a double-sized special
TITLE_PAGE_EXTRA = 45         # a title page may carry this many more characters than the title itself (chapter number, credits line)
TITLE_MIN_RATIO = 0.82        # how closely an OCR page must match an issue title to count as that issue's title page
GENERIC_TITLES = re.compile(r"^(?:part|chapter|book|issue|volume|vol|no|number)?\s*(?:one|two|three|four|five|six|seven|eight|nine|ten|\d+)?$", re.I)

SENTENCE = re.compile(r"(?:collect(?:s|ing|ed)|reprint(?:s|ing)|including|features)\b[^.]*?((?:[A-Z][\w'&:.\- ]*?)\(?\d{0,4}\)?\s*#\s*\d[^.]*)", re.I)
SEGMENT = re.compile(r"(?P<name>[A-Z][\w'&:\- ]*?)\s*(?:\((?:vol\.?\s*\d+\s*)?(?P<year>\d{4})\))?\s*#\s*(?P<first>\d+)(?:\s*[-–—]\s*#?\s*(?P<last>\d+))?")


def plain(text):
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", text or "")).split())


def parse_collects(description):
    """Comic Vine description -> [(series name, year or None, first issue, last issue)] in the order they are listed.

    "Collects Captain America: Sam Wilson (2015) #7-12" -> [("Captain America: Sam Wilson", 2015, 7, 12)].  Annuals, specials and
    anything without an explicit issue range are left out (they cannot be placed safely)."""
    text = plain(description)
    out = []
    for sentence in re.finditer(r"(?:collect(?:s|ing|ed)|reprint(?:s|ing))\b(?P<rest>[^\n]*)", text, re.I):
        rest = sentence.group("rest")
        rest = re.split(r"\.\s+[A-Z]", rest, maxsplit=1)[0]
        for seg in SEGMENT.finditer(rest):
            name = seg.group("name").strip(" ,;:-&")
            name = re.sub(r"^(?:and|plus|&)\s+", "", name, flags=re.I).strip()
            if not name or re.search(r"\b(?:annual|special|giant|king-size)\b", name, re.I):
                continue
            first = int(seg.group("first"))
            last = int(seg.group("last") or first)
            if last < first or last - first > 200:
                continue
            year = int(seg.group("year")) if seg.group("year") else None
            out.append((name, year, first, last))
    return out


def norm(text):
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower().replace("&", " and ")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def resolve_contents(cv_key, segments, search_volumes, volume_issues, publisher_hint=None):
    """segments from parse_collects -> list of Comic Vine issue dicts (id, issue_number, name, cover_date, volume_name), in order.
    Returns None when any segment cannot be resolved to exactly one volume with every listed issue (nothing is guessed)."""
    contents = []
    for name, year, first, last in segments:
        candidates = [c for c in search_volumes(cv_key, name) if norm(c.get("name")) == norm(name)]
        if year:
            candidates = [c for c in candidates if abs(int(re.sub(r"\D", "", str(c.get("start_year") or "")) or 0) - year) <= 1] or []
        def numbered(vol):
            by_num = {}
            for it in volume_issues(cv_key, vol["id"]):
                m = re.fullmatch(r"0*(\d+)", str(it.get("issue_number") or "").strip())
                if m:
                    by_num[int(m.group(1))] = it
            return by_num
        fits = []
        for vol in candidates[:6]:                      # several same-named volumes: keep the one(s) that really have these issues
            by_num = numbered(vol)
            if all(n in by_num for n in range(first, last + 1)):
                fits.append((vol, by_num))
        if len(fits) != 1:
            return None
        vol, by_num = fits[0]
        picked = [by_num[n] for n in range(first, last + 1)]
        contents += [{**p, "volume_name": vol.get("name"), "volume_id": vol["id"]} for p in picked]
    return contents or None


def missing_issues(contents, existing_ids):
    """The contents whose Comic Vine issue id is not already in Komga / the pipeline state."""
    have = {int(i) for i in (existing_ids or ())}
    return [c for c in contents if int(c["id"]) not in have]


def title_score(issue_name, page_text):
    """0..1: how well an OCR page's text matches an issue's Comic Vine title (words in the title that appear on the page)."""
    name = norm(issue_name)
    if not name or GENERIC_TITLES.match(name):
        return 0.0
    page = norm(" ".join(page_text) if isinstance(page_text, (list, tuple)) else page_text)
    if not page:
        return 0.0
    squashed = page.replace(" ", "")               # OCR often runs words together ("CHAPTERONECRIME")
    if len(squashed) > len(name.replace(" ", "")) + TITLE_PAGE_EXTRA:
        return 0.0                                 # a title page carries almost nothing but the title; dialogue pages do not count
    if name.replace(" ", "") in squashed:
        return 1.0
    words = [w for w in name.split() if len(w) >= 3]
    if not words:
        return 0.0
    best = 0.0
    for w in words:
        if w in page.split() or w in squashed:
            best += 1
        else:
            best += max((SequenceMatcher(None, w, tok).ratio() for tok in page.split()), default=0) ** 3
    return best / len(words)


def titles_verifiable(contents):
    """-> None when each issue has its own distinctive title (so a title-page search can work), else the reason it cannot.
    Checked before the slow OCR so books like "Vol. 3" trades with 'Part One'-style titles are not read page by page for nothing."""
    names = [c.get("name") or "" for c in contents]
    if any(not norm(x) or GENERIC_TITLES.match(norm(x)) for x in names):
        return "some issues have no distinctive title to look for"
    if len({norm(x) for x in names}) != len(names):
        return "issue titles repeat, so a title page cannot identify its issue"
    return None


def find_starts(pages, contents, min_pages=MIN_ISSUE_PAGES, max_pages=MAX_ISSUE_PAGES):
    """pages: [{"i": n, "text": [...]}], contents: Comic Vine issues in order.  -> (starts, evidence) or (None, why).

    Verified only when every issue has its own title page (score >= TITLE_MIN_RATIO), the pages are strictly increasing, and each issue
    is between min_pages and max_pages long (the last one is measured to the end of its title-page run + max_pages)."""
    n = len(contents)
    if n == 0:
        return None, "no contents"
    names = [c.get("name") or "" for c in contents]
    if any(not norm(x) or GENERIC_TITLES.match(norm(x)) for x in names):
        return None, "some issues have no distinctive title to look for"
    if len({norm(x) for x in names}) != n:
        return None, "issue titles repeat, so a title page cannot identify its issue"
    starts, evidence, cursor = [], [], 0
    for k, name in enumerate(names):
        best = None
        for p in pages[cursor:]:
            s = title_score(name, p.get("text") or [])
            if s >= TITLE_MIN_RATIO and (best is None or s > best[0] + 1e-9):
                best = (s, p["i"])
                if s >= 0.999:
                    break
        if best is None:
            return None, f"no title page found for issue {contents[k].get('issue_number')} ({name!r})"
        starts.append(best[1])
        evidence.append({"issue": contents[k]["id"], "page": best[1], "score": round(best[0], 2)})
        cursor = next(i for i, p in enumerate(pages) if p["i"] == best[1]) + 1
    if starts != sorted(set(starts)):
        return None, "title pages are not in order"
    lengths = [b - a for a, b in zip(starts, starts[1:])]
    if any(not (min_pages <= L <= max_pages) for L in lengths):
        return None, f"implausible issue lengths {lengths}"
    return starts, evidence


ROLE_WORDS = re.compile(r"\b(writer|artist|penciler|penciller|inker|colorist|colors|letterer|letters|editor|script|cover)\b", re.I)
COVER_TOLERANCE = 16          # dHash distance (of 64 bits) up to which a page counts as a reproduction of an issue's Comic Vine cover
COVER_MARGIN = 4              # the best cover match must beat every other page (3+ pages away) by this much, or it is ambiguous
AGREE_PAGES = 4               # a cover match and a title page for the same issue must lie this close together


def hamming(a, b):
    return bin(int(a) ^ int(b)).count("1")


def credits_page(text):
    """A first story page carries a credits line: three or more different role words (writer, artist, colorist, letterer ...)."""
    return len({m.group(1).lower() for m in ROLE_WORDS.finditer(" ".join(text or []))}) >= 3


def cover_candidate(pages, cursor, cover_hash, tolerance=COVER_TOLERANCE, margin=COVER_MARGIN):
    """-> (page index into `pages`, distance) of the page that reproduces an issue's cover, or None when absent or ambiguous."""
    if cover_hash is None:
        return None
    scored = sorted((hamming(p["dh"], cover_hash), n) for n, p in enumerate(pages) if n >= cursor and p.get("dh") is not None)
    if not scored or scored[0][0] > tolerance:
        return None
    best_d, best_n = scored[0]
    rivals = [d for d, n in scored[1:] if abs(n - best_n) >= 3]
    if rivals and rivals[0] < best_d + margin:
        return None
    return best_n, best_d


def title_candidate(pages, cursor, issue_name):
    best = None
    for n in range(cursor, len(pages)):
        s = title_score(issue_name, pages[n].get("text") or [])
        if s >= TITLE_MIN_RATIO and (best is None or s > best[1] + 1e-9):
            best = (n, s)
            if s >= 0.999:
                break
    return best


def find_starts_multi(pages, contents, cover_hashes=None, min_pages=MIN_ISSUE_PAGES, max_pages=MAX_ISSUE_PAGES):
    """Where each issue starts, from more than titles.  -> (starts, evidence) or (None, why).

    For each expected issue, in order, two independent signals are looked for: the page that reproduces the issue's Comic Vine cover
    (dHash distance, `cover_hashes` maps issue id -> hash) and its own title page (OCR).  An issue is placed when at least one is found
    and, if both are, they agree (within AGREE_PAGES); the issue starts at the earlier of the two.  A credits page right after the start
    is recorded as extra evidence.  The split is accepted only when EVERY issue was placed, the starts are strictly increasing and every
    issue is between min_pages and max_pages long.  Otherwise nothing is split."""
    cover_hashes = cover_hashes or {}
    starts, evidence, cursor = [], [], 0
    for k, c in enumerate(contents):
        cov = cover_candidate(pages, cursor, cover_hashes.get(c["id"]))
        tit = title_candidate(pages, cursor, c.get("name") or "")
        if cov is None and tit is None:
            return None, f"no cover or title page found for issue {c.get('issue_number')} ({c.get('name')!r})"
        if cov and tit and abs(cov[0] - tit[0]) > AGREE_PAGES:
            return None, f"cover (page {pages[cov[0]]['i']}) and title page (page {pages[tit[0]]['i']}) disagree for issue {c.get('issue_number')}"
        start_n = min(x[0] for x in (cov, tit) if x)
        if starts and pages[start_n]["i"] <= starts[-1]:
            return None, f"issue {c.get('issue_number')} would start at or before the previous issue"
        signals = [name for name, x in (("cover", cov), ("title", tit)) if x]
        after = pages[start_n:start_n + 4]
        if any(credits_page(p.get("text")) for p in after):
            signals.append("credits")
        starts.append(pages[start_n]["i"])
        evidence.append({"issue": c["id"], "page": pages[start_n]["i"], "signals": signals,
                         "cover_distance": cov[1] if cov else None, "title_score": round(tit[1], 2) if tit else None})
        cursor = start_n + 1
    lengths = [b - a for a, b in zip(starts, starts[1:])]
    if any(not (min_pages <= L <= max_pages) for L in lengths):
        return None, f"implausible issue lengths {lengths}"
    return starts, evidence


def end_of_last_issue(pages, last_start, name, max_pages=MAX_ISSUE_PAGES, typical=None):
    """Where the last issue ends: before trailing extras. Without a reliable marker, the typical length of the other issues
    (or 24 pages) after its title page, never past the book."""
    limit = min(len(pages), last_start + max_pages)
    if typical:
        limit = min(limit, last_start + int(typical * 1.5))
    return limit


def run_ocr(path, python, helper, out_json, timeout=3600, toc_only=False):
    """OCR the whole archive with the interpreter that has RapidOCR; returns the parsed JSON {"tmp": dir, "pages": [...]}."""
    command = [python, "-W", "ignore", str(helper), str(path), str(out_json)]
    if toc_only:
        command.append("--toc-only")
    proc = subprocess.run(command, check=False,
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if proc.returncode:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"OCR helper exited {proc.returncode}" + (f": {detail[-1600:]}" if detail else ""))
    return json.loads(Path(out_json).read_text(encoding="utf-8"))


def write_singles(pages, tmp, starts, end, contents, out_dir, stem_for):
    """Write each issue's pages as <stem>.cbz in out_dir (stored, no recompression). Returns the paths."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    bounds = list(zip(starts, starts[1:] + [end]))
    paths = []
    for (a, b), issue in zip(bounds, contents):
        path = out_dir / stem_for(issue)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as z:
            for n, page in enumerate([p for p in pages if a <= p["i"] < b], 1):
                z.write(Path(tmp) / page["file"], f"{n:03d}{Path(page['file']).suffix.lower()}")
        paths.append(path)
    return paths


def record_wanted(state_dir, source, missing, reason):
    """Remember the single issues a collected edition collects that Komga does not have (for the next download to fill)."""
    path = Path(state_dir) / "acquisition_wanted.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    key = str(source)
    prior = data.get(key) or {}
    issues = {str(item.get("id")): item for item in prior.get("issues", []) if item.get("id")}
    for item in missing:
        if item.get("id"):
            issues[str(item["id"])] = {"id": item["id"], "series": item.get("volume_name"),
                                       "number": item.get("issue_number"), "cover_date": item.get("cover_date")}
    data[key] = {"reason": reason, "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
                 "issues": list(issues.values())}
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    return len(missing)
