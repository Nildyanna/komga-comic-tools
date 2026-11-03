# komga-comic-tools

Small Python tools for keeping a large comic library tidy in [Komga](https://komga.org): getting issue numbers and
series right, tagging with [ComicTagger](https://github.com/comictagger/comictagger) / Comic Vine, and merging newly
added books into an **existing** Komga read list without disturbing it.

These tools do not download, host or link to any comics. Use them only with files you are entitled to have.

## What problems they solve

| Problem | Tool |
|---|---|
| Komga only reads `ComicInfo.xml` from the **root** of an archive. Packs that bury it in a subfolder (or have none) get numbered by position, so "Annual #1" shows as 2 and "#-1" as 99. | `inject_comicinfo.py` |
| ComicTagger matches "Spider-Man #7" against any of a dozen volumes, rejects issues more than ~1 year from the year hint, and prints "Files tagged" even when it aborts a low-confidence save. | `comictagger_runner.py` |
| You tagged a lot of files and want to know which ones really got the right Comic Vine series. | `verify_tags.py` |
| You added hundreds of issues and want them in your existing read list, in order, with your progress and the old order untouched. | `komga_readlist_merge.py` |
| Comic Vine names trade paperbacks and omnibuses just "Volume 3" or "TPB", so a list shows "Volume Three" with no series. | `komga_title_fix.py` |
| Trade paperbacks and omnibuses have no issue number, so ComicTagger cannot match them by itself. | `tag_collected.py` |

## Requirements
Python 3.9+, no third-party packages. ComicTagger on PATH (or set `comictagger` in the config) for the tagging tools.
For `.cbr` files you also need WinRAR's `Rar.exe`/`UnRAR.exe` (or `rar`/`unrar`); set `RAR_PATH` / `UNRAR_PATH` if they are not found.

## Usage

```bash
# 1. make sure every file has a root ComicInfo.xml (series = folder name, number = "#NNN" in the file name)
python inject_comicinfo.py /path/to/library --dry-run
python inject_comicinfo.py /path/to/library

# 2. optional: full Comic Vine tags, in chunks (copy config.example.json to config.json and edit it)
python comictagger_runner.py config.json --state-dir ./state --max 150
# add --collected to also tag the trade paperbacks/omnibuses in the config's "collected_folders" (name -> Comic Vine volume -> issue id)
# (or run it on its own: python tag_collected.py DIR [--apply]; preview first)
# add --title-fix apply (or preview) to also rescan the config's Komga libraries when the run finishes and retitle
# collected editions (needs KOMGA_API_KEY and a "komga" section in the config; see config.example.json)
python verify_tags.py config.json

# 3. give collected editions a title that names the series ("Volume 3" -> "Thunderbolts Classic Vol. 3"); preview first
export KOMGA_API_KEY=...        # and COMICVINE_KEY=... (or --comictagger-settings path\to\settings.json)
python komga_title_fix.py --preview titles.csv
python komga_title_fix.py --apply

# 4. merge new books into an existing Komga read list (preview first)
export KOMGA_API_KEY=...        # Komga > My Account > API Keys; never stored by these tools
python komga_readlist_merge.py --library-id LIB_ID --readlist-id LIST_ID
python komga_readlist_merge.py --library-id LIB_ID --readlist-id LIST_ID --apply --cbl merged.cbl
```

The title fix only touches books whose title is exactly "Volume N" / "Vol. N" / "TPB" / "Omnibus" and that carry a Comic Vine link; single
issues with real titles (including "Part Three" or "Book Four") are left alone. Titles it writes are locked in Komga. The tagging runner can run it for you
(`--title-fix`); otherwise run it after a Komga scan of newly tagged books. Tests: `python test_title_fix.py`.

The read-list merge keeps every existing entry (including ones from other libraries) in the same relative order, keeps the list's id and name, and places
each new book after the last entry with an earlier-or-equal cover month (interpolated from books of the same folder
that already have dates). Books it cannot date go in a block at the end. Read the preview CSV before using `--apply`. Dates far from their neighbours (an omnibus among 1970s issues) are ignored when
working out where a book goes, and the tool refuses to apply anything that would drop an existing entry. Tests: `python test_readlist_merge.py`.

## Lessons baked in
* Filename conventions matter: use `<Series> #NNN.cbz` (3-digit issue numbers) inside one folder per series/volume.
* Give ComicTagger the series name **and** a year hint close to the issue's cover year; the runner follows the last real
  cover date so the hint stays accurate, then verifies the result and restores basic tags if the series is wrong.
* Event-pack numbers may be reading-order positions, not comic issue numbers. Prefer consensus hints from internal page
  filenames while preserving a publication year from the outer filename, constrain ambiguous series matches with the configured
  Comic Vine story-arc ID, and use exact Incoming-relative `issue_overrides` only for files whose scans have no usable title/number.
  Never treat the outer pack ordinal as an issue number.
* Routes can declare a `library_roots` entry. After a verified issue is uploaded, the Komga stage creates that destination
  library on demand if it does not already exist; it does not create empty libraries during intake.
* The incoming pipeline keeps a persistent SQLite cache and a rolling per-key request ledger. Its default budget is 160
  Comic Vine calls per hour, leaving headroom under the service's approximate 200-request limit; tune it with
  `comicvine_requests_per_hour` only when the key's other usage is understood.
* Routes are created automatically for a series nothing matches yet (`auto_create_routes`, Marvel/DC and other configured
  publishers only): see `auto_create_route`.
* Collected editions (trades, omnibuses, "Volume N" books) are filed in their library like any comic and then broken out
  (`breakout.py`): Comic Vine's description says what the book collects; issues already in Komga need nothing; for the rest the book is
  OCR'd (`ocr_python` must point at an interpreter with `rapidocr_onnxruntime`, Pillow and numpy) and split ONLY when every issue's
  own title page is found in order with plausible lengths. Those singles are tagged and flow through the normal pipeline, so they land
  in the single-issue reading lists by cover date. A book that cannot be verified is never guessed: its missing issues go to
  `_state/acquisition_wanted.json`, and a numbered "Vol. N" book stands in the lists at its first issue's month
  (`trade_list_fallback`) until the singles arrive. `hold_collected_editions: true` restores the old hold-for-review behaviour and
  `break_out_collected_editions: false` turns the split off.
* Preparation uses a 600-second time budget with a 500-file safety ceiling. Unchanged held files are skipped until their
  file, routing/matching policy, relevant code, or weekly retry window changes.
* Cover exceptions in `Incoming\_state\approved_covers.txt` should be visually checked against the selected Comic Vine issue.
  Prefer exact Incoming-relative paths; basename entries remain supported for existing approvals.
  `cover_match_tolerances` can set a route-specific dHash limit for a verified scan set; the default remains 14.
* Always take a backup copy of the library first.

## License
MIT
