"""Merge newly added books into an EXISTING Komga read list without disturbing it.

* The existing entries keep their relative order, the read list keeps its id/name, and reading progress is untouched.
* Each new book is placed after the last existing entry with an earlier-or-equal cover month.
* A new book's cover month is interpolated from books of the same folder that already have a release date
  (one issue per month); books without a "#number" in the name, or from a folder with no dated books, go into a
  block at the end of the list.
* Default is a preview (writes a CSV); add --apply to PATCH the read list.

Authentication: set the KOMGA_API_KEY environment variable (Komga > My Account > API Keys). The key is never
written anywhere by this script.

Usage:
  KOMGA_API_KEY=... python komga_readlist_merge.py --library-id ID --readlist-id ID [--base-url http://localhost:25600]
                                                   [--preview preview.csv] [--apply] [--cbl out.cbl]
"""
import argparse
import csv
import json
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

NUMBER = re.compile(r"#(-?\d+(?:\.\d+)?)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", default="http://localhost:25600")
    ap.add_argument("--library-id", required=True)
    ap.add_argument("--readlist-id", required=True)
    ap.add_argument("--preview", default="readlist_merge_preview.csv")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--cbl", help="also write the merged list as a .cbl reading list")
    args = ap.parse_args()
    key = os.environ.get("KOMGA_API_KEY")
    if not key:
        sys.exit("Set KOMGA_API_KEY (Komga > My Account > API Keys).")
    base = args.base_url.rstrip("/") + "/api/v1"

    def api(path, method="GET", body=None):
        req = urllib.request.Request(base + path, method=method, headers={"X-API-Key": key, "Content-Type": "application/json"},
                                     data=json.dumps(body).encode() if body is not None else None)
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = r.read()
        return json.loads(raw) if raw else None

    def month_index(date):
        return int(date[:4]) * 12 + int(date[5:7]) - 1

    def number_of(book):
        m = NUMBER.search(book["name"])
        return float(m.group(1)) if m else None

    def folder_of(book):
        return os.path.basename(os.path.dirname(book["url"].replace("\\", "/")))

    def fetch_all(path):
        """Every item of a paged Komga endpoint (the server caps the page size, so walk totalPages)."""
        out, page = [], 0
        while True:
            d = api(f"{path}{'&' if '?' in path else '?'}size=500&page={page}")
            out += d["content"]
            page += 1
            if page >= d.get("totalPages", 1):
                return out

    existing = fetch_all(f"/readlists/{args.readlist_id}/books")
    library = fetch_all(f"/books?library_id={args.library_id}")
    # A read list usually spans several libraries: keep EVERY existing entry (in its order), not just the ones in --library-id.
    by_id = {b["id"]: b for b in existing}
    by_id.update({b["id"]: b for b in library})
    order = [b["id"] for b in existing]
    in_list = set(order)
    # Dates that are far from their neighbours (an omnibus printed in 2017 sitting among 1976 issues) must not drag the
    # running position forward, so such outliers count as undated.
    months = [month_index(by_id[bid]["metadata"]["releaseDate"]) if by_id[bid]["metadata"].get("releaseDate") else None for bid in order]
    dated = {}
    last = 0
    for i, bid in enumerate(order):
        near = sorted(m for m in months[max(0, i - 6):i + 7] if m is not None)
        outlier = months[i] is not None and near and abs(months[i] - near[len(near) // 2]) > 36
        if months[i] is not None and not outlier:
            last = months[i]
        dated[bid] = last
    new_books = [b for b in library if b["id"] not in in_list]
    print(f"existing list: {len(order)} ({len([b for b in library if b['id'] in in_list])} in this library) | library: {len(library)} | new to place: {len(new_books)}")
    if not order:
        sys.exit("The read list came back empty; refusing to continue (applying would replace it with only the new books).")

    anchors = defaultdict(list)
    for i, bid in enumerate(order):
        b = by_id[bid]
        d, n = b["metadata"].get("releaseDate"), number_of(b)
        if d and n is not None and abs(months[i] - dated[bid]) <= 36:
            anchors[folder_of(b)].append((n, month_index(d)))
    for pts in anchors.values():
        pts.sort()

    def estimate(book):
        n, pts = number_of(book), anchors.get(folder_of(book))
        if n is None or n <= 0 or not pts:
            return None
        for i, (pn, pd) in enumerate(pts):
            if pn == n:
                return pd
            if pn > n:
                if i == 0:
                    return pd - round(pn - n)
                p0n, p0d = pts[i - 1]
                return round(p0d + (pd - p0d) * (n - p0n) / (pn - p0n))
        pn, pd = pts[-1]
        return pd + round(n - pn)

    placed, trailing = [], []
    for b in new_books:
        est = estimate(b)
        if est is None:
            trailing.append(b)
        else:
            placed.append((est, folder_of(b), number_of(b), b))

    envelope, m = [], 0
    for bid in order:
        m = max(m, dated[bid])
        envelope.append(m)
    buckets = defaultdict(list)
    for est, folder, n, b in placed:
        pos = next((i for i, e in enumerate(envelope) if e > est), len(order))
        buckets[pos].append((est, folder, n, b))

    final = []
    for i in range(len(order) + 1):
        for est, folder, n, b in sorted(buckets.get(i, []), key=lambda t: t[:3]):
            final.append(b["id"])
        if i < len(order):
            final.append(order[i])
    final += [b["id"] for b in sorted(trailing, key=lambda b: (folder_of(b), number_of(b) or 0, b["name"]))]
    assert len(final) == len(set(final)) == len(order) + len(new_books)
    assert [x for x in final if x in in_list] == order, "existing relative order changed"
    assert in_list <= set(final) and len(final) >= len(order), "an existing entry would be lost"

    with open(args.preview, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["position", "new", "series", "name"])
        for i, bid in enumerate(final, 1):
            w.writerow([i, "" if bid in in_list else "NEW", by_id[bid]["seriesTitle"], by_id[bid]["name"]])
    print(f"merged list: {len(final)} books ({len(placed)} interleaved, {len(trailing)} at the end). Preview: {args.preview}")

    if not args.apply:
        print("preview only - re-run with --apply to update the read list")
        return
    api(f"/readlists/{args.readlist_id}", "PATCH", {"bookIds": final})
    check = api(f"/readlists/{args.readlist_id}")
    assert check["bookIds"] == final, "read list did not update as expected"
    print("read list updated:", check["name"], len(check["bookIds"]), "books")
    if args.cbl:
        root = ET.Element("ReadingList")
        ET.SubElement(root, "Name").text = check["name"].lstrip("! ").strip()
        books = ET.SubElement(root, "Books")
        for bid in final:
            b = by_id[bid]
            attrs = {"Series": b["seriesTitle"], "Number": str(b["metadata"].get("number") or b["number"])}
            year = (b["metadata"].get("releaseDate") or "")[:4]
            if year:
                attrs["Year"] = year
            ET.SubElement(books, "Book", attrs)
        ET.indent(root, space="  ")
        ET.ElementTree(root).write(args.cbl, encoding="utf-8", xml_declaration=True)
        print("wrote", args.cbl)


if __name__ == "__main__":
    main()
