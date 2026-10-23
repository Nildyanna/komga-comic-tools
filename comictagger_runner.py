"""Resumable ComicTagger (Comic Vine) pass that picks the right volume and verifies every result.

Why this exists: with a bare "Spider-Man #7" ComicTagger can match any of a dozen volumes. This runner pins the
series name and a year hint per folder, follows the last real cover date (so the hint stays accurate), accepts
low-confidence matches only when the series name/number then verify, and puts the basic tags back on a bad match.
It is also gentle on Comic Vine's rate limit (default 25 s pause) and can be run in chunks.

Config (JSON, see config.example.json):
{
  "library_root": "/path/to/library",
  "comictagger": "comictagger",
  "folders": { "<folder name>": {"series": "<Comic Vine series name>", "start_year": 1990,
                                 "expected": ["<accepted series names, lower-case>"]} }
}

Optional "collected_folders": ["<folder under library_root>", ...]. With --collected the runner also tags the trade paperbacks and
omnibuses in those folders (see tag_collected.py: it matches the Comic Vine volume from the file name and tags by issue id), which
the numbered-issue pass cannot do.

Optional "komga" section: {"base_url": "http://localhost:25600", "library_ids": ["<Komga library id>", ...]}. With --title-fix
preview|apply (and KOMGA_API_KEY set) the runner finishes by asking Komga to rescan those libraries, waiting --scan-wait seconds, and
running komga_title_fix.py on them, so collected editions get "<Series> Vol. N" titles right after they are tagged.

Usage:  python comictagger_runner.py config.json --state-dir ./state [--max 150] [--pause 25] [--dry-run]
                                      [--collected] [--title-fix off|preview|apply] [--scan-wait 90]
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import komga_title_fix
import tag_collected
import tagcommon

NUMBER = re.compile(r"#(-?\d+(?:\.\d+)?)\.cb[rz]$", re.I)


def issue_number(text):
    value = float(text)
    return str(int(value)) if value.is_integer() else text


def real_date_index(path):
    tags = tagcommon.read_tags(path)
    if tags.get("Year") and tags.get("Month"):
        return int(tags["Year"]) * 12 + int(tags["Month"]) - 1
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("config", type=Path)
    ap.add_argument("--state-dir", type=Path, default=Path("state"))
    ap.add_argument("--max", type=int, default=0, help="stop after this many files (run in chunks)")
    ap.add_argument("--pause", type=int, default=25, help="seconds between files (Comic Vine rate limit)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--collected", action="store_true", help="also tag the collected editions in the config's collected_folders")
    ap.add_argument("--title-fix", choices=["off", "preview", "apply"], default="off",
                    help="after tagging: rescan the config's Komga libraries and retitle generic collected editions")
    ap.add_argument("--scan-wait", type=int, default=90, help="seconds to let Komga read the tagged files before the title fix")
    args = ap.parse_args()

    cfg = json.loads(args.config.read_text(encoding="utf-8"))
    root = Path(cfg["library_root"])
    exe = cfg.get("comictagger", "comictagger")
    folders = cfg["folders"]
    args.state_dir.mkdir(parents=True, exist_ok=True)
    log_path = args.state_dir / "runner_log.jsonl"

    files = []
    for folder, meta in folders.items():
        for path in sorted((root / folder).glob("*")):
            m = NUMBER.search(path.name)
            if m and float(m.group(1)) > 0:
                files.append((path, folder, issue_number(m.group(1))))

    todo, state = [], {}
    for path, folder, number in files:
        expected = {n.lower() for n in folders[folder]["expected"]}
        verdict, _ = tagcommon.verdict(path, expected, number)
        key = (folders[folder]["series"], folders[folder]["start_year"])
        if verdict == "ok":
            idx = real_date_index(path)
            if idx and (key not in state or float(number) > state[key][0]):
                state[key] = (float(number), idx)
        else:
            if verdict in ("wrong-series", "wrong-number"):
                tagcommon.restore_minimal(path, folder, number)
                print("reset wrong tags:", path.name)
            todo.append((path, folder, number))
    todo.sort(key=lambda f: (folders[f[1]]["series"], folders[f[1]]["start_year"], float(f[2])))
    if args.max:
        todo = todo[:args.max]
    print(f"{len(files)} eligible, {len(todo)} to tag this run")
    if args.dry_run:
        if args.collected:
            tag_collected_folders(cfg, args, apply=False)
        return 0

    pause, ok, bad = args.pause, 0, 0
    with log_path.open("a", encoding="utf-8") as log:
        for path, folder, number in todo:
            meta = folders[folder]
            key = (meta["series"], meta["start_year"])
            if key in state:  # follow the last real cover date (monthly)
                base = (state[key][1] + round(float(number) - state[key][0])) // 12
            else:
                base = meta["start_year"] + max(0, int(float(number)) - 1) // 12
            expected = {n.lower() for n in meta["expected"]}
            outcome = "no-match"
            for year in (base, base + 1, base - 1):  # the estimate can be a month or two off
                hint = f"series: '{meta['series']}', issue: '{number}', year: {year}"
                cmd = [exe, "-s", "-o", "--tags-write", "CR", "--no-gui", "--no-prompt-on-save", "--no-abort", "-m", hint, str(path)]
                try:
                    out = subprocess.run(cmd, capture_output=True, text=True, timeout=240).stdout
                except subprocess.TimeoutExpired:
                    outcome = "timeout"
                    break
                if "Save complete" in out:
                    check, tags = tagcommon.verdict(path, expected, number)
                    if check == "ok":
                        outcome = "tagged"
                        idx = real_date_index(path)
                        if idx:
                            state[key] = (float(number), idx)
                    else:
                        outcome = check
                        if check in ("wrong-series", "wrong-number"):
                            tagcommon.restore_minimal(path, folder, number)
                    break
                time.sleep(12)
            ok += outcome == "tagged"
            bad += outcome != "tagged"
            log.write(json.dumps({"file": path.name, "folder": folder, "number": number, "outcome": outcome,
                                  "time": time.strftime("%Y-%m-%dT%H:%M:%S")}) + "\n")
            log.flush()
            if outcome in ("no-match", "timeout") and pause < 25:
                pause = 25  # back off to the safe pause
            time.sleep(pause)
    print(f"tagged {ok}, not tagged {bad}; log: {log_path}")
    if args.collected:
        ok += tag_collected_folders(cfg, args, apply=True, exe=exe)
    if ok and args.title_fix != "off":
        title_fix(cfg.get("komga") or {}, args)
    return 0


def tag_collected_folders(cfg, args, apply, exe="comictagger", say=print):
    """Tag the collected editions in the config's collected_folders; returns how many files were tagged."""
    key = komga_title_fix.comicvine_key()
    dirs = [Path(cfg["library_root"]) / f for f in cfg.get("collected_folders") or []]
    if not key or not dirs:
        say("collected editions skipped: needs a Comic Vine key and collected_folders in the config")
        return 0
    return tag_collected.run(dirs, key, exe, apply=apply, max_files=args.max or 20, log=str(args.state_dir / "tag_collected_log.jsonl"), say=say)


def title_fix(komga_cfg, args, say=print, sleep=time.sleep):
    """Rescan the Komga libraries, give Komga time to read the new tags, then fix generic collected-edition titles."""
    key, cv_key = os.environ.get("KOMGA_API_KEY"), komga_title_fix.comicvine_key()
    libs = komga_cfg.get("library_ids") or []
    if not key or not cv_key or not libs:
        say("title fix skipped: needs KOMGA_API_KEY, a Comic Vine key and komga.library_ids in the config")
        return None
    komga = komga_title_fix.Komga(komga_cfg.get("base_url", "http://localhost:25600"), key)
    for lib in libs:
        komga.scan(lib)
    say(f"asked Komga to rescan {len(libs)} librar{'y' if len(libs) == 1 else 'ies'}; waiting {args.scan_wait}s")
    sleep(args.scan_wait)
    return komga_title_fix.fix_titles(komga, cv_key, libs, apply=args.title_fix == "apply",
                                      preview=str(args.state_dir / "title_fix_preview.csv"), say=say)


if __name__ == "__main__":
    sys.exit(main())
