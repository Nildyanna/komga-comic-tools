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
* Comic Vine allows roughly 200 requests per hour per key. Pausing ~25 s between files was reliable; 10 s was not.
  A personal API key (free) gives you your own quota.
* Always take a backup copy of the library first.

## License
MIT
