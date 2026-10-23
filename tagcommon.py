"""Shared helpers: read/verify/restore ComicInfo.xml tags in .cbz and .cbr archives.

RAR support needs WinRAR's `Rar.exe` / `UnRAR.exe` (or `rar` / `unrar` on PATH). Override with the
RAR_PATH and UNRAR_PATH environment variables.
"""
import os
import re
import shutil
import subprocess
import tempfile
import zipfile


def _find(env_name, names, extra=()):
    if os.environ.get(env_name):
        return os.environ[env_name]
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    for candidate in extra:
        if os.path.exists(candidate):
            return candidate
    return None


UNRAR = _find("UNRAR_PATH", ["unrar", "UnRAR.exe"], [r"C:\Program Files\WinRAR\UnRAR.exe"])
RAR = _find("RAR_PATH", ["rar", "Rar.exe"], [r"C:\Program Files\WinRAR\Rar.exe"])

FIELDS = r"<(Series|Number|Year|Month|Volume|Count|Title)>([^<]*)<"


def is_cbz(path):
    """True for ZIP containers. Sniffs the header, because plenty of .cbz files are really RARs (and vice versa)."""
    try:
        with open(str(path), "rb") as f:
            head = f.read(4)
        if head[:2] == b"PK":
            return True
        if head[:4] == b"Rar!":
            return False
    except OSError:
        pass
    return str(path).lower().endswith(".cbz")


def list_names(path):
    if is_cbz(path):
        return zipfile.ZipFile(path).namelist()
    out = subprocess.run([UNRAR, "lb", "-c-", str(path)], capture_output=True, text=True, errors="replace").stdout
    return out.splitlines()


def read_raw_root_info(path):
    """ComicInfo.xml from the archive ROOT only (that is the only place Komga reads it)."""
    path = str(path)
    if is_cbz(path):
        z = zipfile.ZipFile(path)
        names = [n for n in z.namelist() if n.lower() == "comicinfo.xml"]
        return z.read(names[0]).decode("utf-8", "replace") if names else ""
    return subprocess.run([UNRAR, "p", "-inul", path, "ComicInfo.xml"], capture_output=True).stdout.decode("utf-8", "replace")


def read_tags(path):
    return dict(re.findall(FIELDS, read_raw_root_info(path)))


def nested_info_name(path):
    """Name of a ComicInfo.xml that is NOT at the archive root (invisible to Komga), if any."""
    for n in list_names(path):
        norm = n.replace("\\", "/")
        if norm.lower().endswith("/comicinfo.xml"):
            return n
    return None


def verdict(path, expected_names, number):
    """ok | not-cv-tagged | wrong-series | wrong-number. A Comic Vine tag always carries <Count>."""
    tags = read_tags(path)
    if not tags.get("Count"):
        return "not-cv-tagged", tags
    if tags.get("Series", "").lower() not in expected_names:
        return "wrong-series", tags
    if tags.get("Number") != number:
        return "wrong-number", tags
    return "ok", tags


def minimal_xml(series, number, title="", year=""):
    from xml.sax.saxutils import escape
    parts = ["<?xml version='1.0' encoding='utf-8'?>",
             '<ComicInfo xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema">',
             f"  <Series>{escape(series)}</Series>", f"  <Number>{escape(number)}</Number>"]
    if title:
        parts.append(f"  <Title>{escape(title)}</Title>")
    if year:
        parts.append(f"  <Year>{year}</Year>")
    parts.append("</ComicInfo>")
    return "\n".join(parts) + "\n"


def write_root_info(path, xml):
    """Put `xml` at the archive root as ComicInfo.xml, replacing any root copy."""
    path = str(path)
    if is_cbz(path):
        tmp = path + ".tmp"
        with zipfile.ZipFile(path) as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as zout:
            for item in zin.infolist():
                if item.filename.lower() == "comicinfo.xml":
                    continue
                zout.writestr(item, zin.read(item.filename))
            zout.writestr("ComicInfo.xml", xml)
        os.replace(tmp, path)
    else:
        if RAR is None:
            raise RuntimeError("Rar.exe/rar not found; set RAR_PATH to tag .cbr files")
        subprocess.run([RAR, "d", "-idq", "-y", path, "ComicInfo.xml"], capture_output=True)
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "ComicInfo.xml")
            with open(p, "w", encoding="utf-8") as f:
                f.write(xml)
            run = subprocess.run([RAR, "a", "-ep", "-idq", "-y", "-tl-", path, p], capture_output=True, text=True, errors="replace")
            if run.returncode != 0:
                raise RuntimeError((run.stdout + run.stderr).strip()[-200:] or f"rar exit {run.returncode}")


def restore_minimal(path, series, number):
    write_root_info(path, minimal_xml(series, number))
