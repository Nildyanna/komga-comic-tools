"""Fetch the single issues the pipeline is missing (breakout.record_wanted writes them to _state/acquisition_wanted.json).

For each wanted issue that Komga still does not have: look the issue up on getcomics.org (WordPress REST search), accept only a post
that is exactly that series' single issue (or a range/pack that includes the issue and the right year), resolve its "Download Now"
button to a direct comicfiles link, and hand the link to JDownloader (Click'n'Load on 127.0.0.1:9666) with the staging folder as the
download folder.  The files then go through the normal pipeline (unpack, tag, route, upload, lists).

Careful by design: only direct comicfiles links are sent (mirrors that need a browser are skipped), each issue/link is sent at most
once per `acquire_resend_days`, an issue that no post matches is not searched again for that long, and at most
`acquire_max_per_run` links are sent per run.  Nothing is guessed: a post whose title does not match the series exactly is ignored.
"""
import html
import http.client
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124"
SEARCH = "https://getcomics.org/wp-json/wp/v2/posts?per_page=30&_fields=link,title&search="
JDOWNLOADER = "http://127.0.0.1:9666/flash/add"


def norm(text):
    text = html.unescape(text or "").lower().replace("&", "and")
    text = re.sub(r"[’'`]", "", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\b(the|a)\b", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def classify(title, series, number, year):
    """-> (score, kind): 100 single issue, 60 range pack including the issue, 0 no match.  Same strict rules as find_acquisition_links."""
    t = html.unescape(title or "")
    core = re.sub(r"\([^)]*\)", "", t)
    core = re.sub(r"#\s*\d+(?:\s*(?:–|-)\s*\d+)?", " ", core)
    core = re.sub(r"vol\.?\s*\d+", " ", core, flags=re.I)
    if norm(core) != norm(series):
        return 0, ""
    if re.search(r"\b(tpb|hc|omnibus|compendium|collection)\b", t, re.I):
        return 0, ""
    years = [int(y) for y in re.findall(r"\b(19\d\d|20\d\d)\b", t)]
    year_ok = (not years) or any(abs(y - year) <= 1 for y in years) or (len(years) >= 2 and years[0] - 1 <= year <= years[-1] + 1)
    if not year_ok:
        return 0, ""
    rng = re.search(r"#\s*(\d+)\s*(?:–|-|&#8211;)\s*(\d+)", t)
    if rng and int(rng.group(1)) <= number <= int(rng.group(2)):
        return 60, f"range #{rng.group(1)}-{rng.group(2)}"
    single = re.search(r"#\s*(\d+)\b(?!\s*(?:–|-))", t)
    if single and int(single.group(1)) == number:
        return 100, "single issue"
    return 0, ""


def http_get(url, timeout=40):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def search(query):
    try:
        data = json.loads(http_get(SEARCH + urllib.parse.quote(query)))
    except Exception:                                                              # noqa: BLE001
        return []
    time.sleep(0.8)
    return [(html.unescape(x["title"]["rendered"]), x["link"]) for x in data]


def direct_link(post_url):
    """The comicfiles.ru URL behind a getcomics post's Download Now button, or None."""
    try:
        page = http_get(post_url)
    except Exception:                                                              # noqa: BLE001
        return None
    for url, cls in re.findall(r'<a [^>]*?href="(https?://[^"]+)"[^>]*?class="(aio-[a-z]+)"', page):
        if "/dls/" not in url:
            if "comicfiles" in url:
                return url
            continue
        parts = urllib.parse.urlsplit(url)
        conn = http.client.HTTPSConnection(parts.netloc, timeout=30)
        conn.request("HEAD", parts.path + ("?" + parts.query if parts.query else ""), headers={"User-Agent": UA})
        loc = conn.getresponse().getheader("Location") or ""
        conn.close()
        time.sleep(0.4)
        if "comicfiles" in loc:
            return loc
    return None


def find_issue_link(series, number, year):
    """-> (comicfiles link, post title, kind) or None."""
    best = None
    for q in (f"{series} {number}", f"{series} {number} {year}", f"{series} {year}"):
        for title, link in search(q):
            score, kind = classify(title, series, number, year)
            if score and (best is None or score > best[0]):
                best = (score, kind, title, link)
        if best and best[0] == 100:
            break
    if not best:
        return None
    link = direct_link(best[3])
    return (link, best[2], best[1]) if link else None


def send_to_jdownloader(urls, package, folder, say=print):
    body = urllib.parse.urlencode({"urls": "\n".join(urls), "package": package, "dir": str(folder), "autostart": "1", "source": "komga-pipeline"}).encode()
    req = urllib.request.Request(JDOWNLOADER, data=body, headers={"User-Agent": UA})
    try:
        return urllib.request.urlopen(req, timeout=20).read().decode().strip().lower().startswith("success")
    except (urllib.error.URLError, OSError) as e:
        say(f"  JDownloader is not reachable ({e}); nothing sent")
        return False


def run_acquire(cfg, have_ids, say=print, dry_run=False, now=None):
    """Send the wanted single issues Komga lacks to JDownloader. Returns the number of links sent."""
    now = now or time.time()
    state = Path(cfg["incoming"]) / "_state"
    try:
        wanted = json.loads((state / "acquisition_wanted.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        say("nothing wanted")
        return 0
    sent_path = state / "acquisition_sent.json"
    try:
        sent = json.loads(sent_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        sent = {}
    resend = float(cfg.get("acquire_resend_days", 7)) * 86400
    limit = int(cfg.get("acquire_max_per_run", 8))
    folder = Path(cfg["incoming"]) / "_acquired"
    # files for the same series that are still waiting in staging (an unpacked pack, a download in progress) will be tagged and filed
    # by the normal pipeline; fetching those issues again would only create duplicates
    skip_dirs = {"_ready", "_done", "_state", "_breakout", "_acquired"}
    waiting = norm(" ".join(p.relative_to(cfg["incoming"]).as_posix() for p in Path(cfg["incoming"]).rglob("*")
                            if p.is_file() and p.suffix.lower() in (".cbz", ".cbr", ".zip", ".rar", ".7z")
                            and not (set(p.relative_to(cfg["incoming"]).parts) & skip_dirs)))
    todo, seen = [], set()
    for source, entry in wanted.items():
        for it in entry.get("issues", []):
            iid = int(it["id"])
            if iid in have_ids or iid in seen:
                continue
            seen.add(iid)
            if norm(it.get("series")) and norm(it.get("series")) in waiting:
                continue
            rec = sent.get(str(iid))
            if rec and now - rec.get("time", 0) < resend:
                continue
            todo.append(it)
    if not todo:
        say("nothing to fetch: every wanted issue is already in Komga, in staging, sent or recently not found")
        return 0
    say(f"{len(todo)} wanted issue(s) not in Komga; fetching up to {limit} link(s) this run")
    links = {}
    for it in sorted(todo, key=lambda i: (str(i.get("series")), float(i.get("number") or 0))):
        if len(links) >= limit:
            break
        series, number = it.get("series") or "", int(float(it.get("number") or 0))
        year = int((it.get("cover_date") or "0000")[:4]) or 0
        if any(number == n and series == s for (s, n, _t) in links.values()):
            continue
        hit = find_issue_link(series, number, year)
        if not hit:
            sent[str(it["id"])] = {"time": now, "status": "not found"}
            say(f"  not found on getcomics: {series} #{number} ({year})")
            continue
        link, title, kind = hit
        links.setdefault(link, (series, number, title))
        sent[str(it["id"])] = {"time": now, "status": "sent", "link": link, "post": title, "kind": kind}
        say(f"  {series} #{number} -> {title} [{kind}]")
    if links and not dry_run:
        if not send_to_jdownloader(list(links), "komga-pipeline acquisitions", folder, say):
            for it in todo:
                if sent.get(str(it["id"]), {}).get("status") == "sent":
                    sent.pop(str(it["id"]))
            links = {}
    if not dry_run:
        sent_path.write_text(json.dumps(sent, indent=1), encoding="utf-8")
    say((f"would send {len(links)} link(s) to JDownloader" if dry_run else f"sent {len(links)} link(s) to JDownloader") if links else "no links sent")
    return len(links)
