"""Read-only check: does every numbered comic carry Comic Vine tags for the expected series?

Uses the same JSON config as comictagger_runner.py.  Usage:  python verify_tags.py config.json [--report report.csv]
Statuses: ok | not-cv-tagged | wrong-series | wrong-number
"""
import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

import tagcommon

NUMBER = re.compile(r"#(-?\d+(?:\.\d+)?)\.cb[rz]$", re.I)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("config", type=Path)
    ap.add_argument("--report", type=Path, default=Path("verify_tags_report.csv"))
    args = ap.parse_args()
    cfg = json.loads(args.config.read_text(encoding="utf-8"))
    root = Path(cfg["library_root"])
    rows, by_folder = [], defaultdict(Counter)
    for folder, meta in cfg["folders"].items():
        expected = {n.lower() for n in meta["expected"]}
        for path in sorted((root / folder).glob("*")):
            m = NUMBER.search(path.name)
            if not m or float(m.group(1)) <= 0:
                continue
            value = float(m.group(1))
            number = str(int(value)) if value.is_integer() else m.group(1)
            status, tags = tagcommon.verdict(path, expected, number)
            rows.append((status, folder, number, tags.get("Series", ""), tags.get("Year", ""), str(path)))
            by_folder[folder][status] += 1
    with args.report.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["status", "folder", "number", "cv_series", "year", "path"])
        w.writerows(rows)
    print(len(rows), "files checked:", dict(Counter(r[0] for r in rows)))
    for folder, counts in by_folder.items():
        print(f"  {folder:40} {dict(counts)}")


if __name__ == "__main__":
    main()
