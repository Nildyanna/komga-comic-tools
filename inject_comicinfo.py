"""Give every comic a ComicInfo.xml at the archive ROOT so Komga reads its series and issue number.

Komga ignores a ComicInfo.xml inside a subfolder of the archive and numbers untagged books by position
(so "Annual #1" can show as 2, "#-1" as 99). For each .cbz/.cbr under ROOT that has no root ComicInfo.xml:
  * if a nested ComicInfo.xml exists, copy it to the root (series/number corrected from the folder/file name);
  * otherwise write a minimal one: Series = parent folder name, Number = the "#NNN" in the file name,
    Year = the "(YYYY)" in the file name if present.
Files whose name has no "#number" are reported and skipped.

Usage:  python inject_comicinfo.py ROOT [--dry-run]
"""
import argparse
import os
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import tagcommon

NUMBER = re.compile(r"#(-?\d+(?:\.\d+)?)")
YEAR = re.compile(r"\((\d{4})\)")


def clean_number(text):
    value = float(text)
    return str(int(value)) if value.is_integer() else text


def build_xml(path, series, number):
    nested = tagcommon.nested_info_name(path)
    if nested:
        if tagcommon.is_cbz(path):
            raw = zipfile.ZipFile(path).read(nested)
        else:
            import subprocess
            raw = subprocess.run([tagcommon.UNRAR, "p", "-inul", str(path), nested], capture_output=True).stdout
        try:
            root = ET.fromstring(raw.lstrip(b"\xef\xbb\xbf"))
            for tag, value in (("Series", series), ("Number", number)):
                el = root.find(tag)
                if el is None:
                    el = ET.SubElement(root, tag)
                el.text = value
            return ET.tostring(root, encoding="utf-8", xml_declaration=True).decode("utf-8")
        except ET.ParseError:
            pass  # fall through to a minimal tag
    year = YEAR.search(path.name)
    return tagcommon.minimal_xml(series, number, year=year.group(1) if year else "")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", type=Path)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    todo, skipped, done, failed = [], [], 0, []
    for path in sorted(args.root.rglob("*")):
        if path.suffix.lower() not in (".cbz", ".cbr") or not path.is_file():
            continue
        if tagcommon.read_raw_root_info(path):
            continue
        m = NUMBER.search(path.stem)
        if not m:
            skipped.append(path)
            continue
        todo.append((path, path.parent.name, clean_number(m.group(1))))

    print(f"{len(todo)} files need a root ComicInfo.xml; {len(skipped)} skipped (no '#number' in the name)")
    for path in skipped[:20]:
        print("  skipped:", path)
    if args.dry_run:
        for path, series, number in todo[:20]:
            print(f"  would tag: {path.name} -> {series} #{number}")
        return 0
    for path, series, number in todo:
        try:
            tagcommon.write_root_info(path, build_xml(path, series, number))
            done += 1
        except Exception as error:  # keep going; report at the end
            failed.append((path, str(error)))
    print(f"tagged {done}; failed {len(failed)}")
    for path, err in failed:
        print("  FAILED:", path, err)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
