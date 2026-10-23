"""Tests for komga_title_fix.py (the title rule). Run: python test_title_fix.py"""
import io, json, os, sys, tempfile, unittest, urllib.parse
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import komga_title_fix
from komga_title_fix import is_generic, new_title


class RuleTests(unittest.TestCase):
    def test_generic_titles(self):
        for t in ["Volume 1", "Volume Three", "volume 12", "Vol. 3", "Vol 3", "TPB", "Omnibus", "Hardcover", "Tome 2"]:
            self.assertTrue(is_generic(t), t)

    def test_real_titles_are_left_alone(self):
        for t in ["Distant Rumblings!", "Volume 3: Death of a Goblin", "The Final Chapter", "", None, "Part of the Problem", "Booked", "Part Three", "Part IV", "Book Four", "Book 2"]:
            self.assertFalse(is_generic(t), t)

    def test_numbered_volumes(self):
        self.assertEqual(new_title("Volume Three", "Thunderbolts Classic", "3"), "Thunderbolts Classic Vol. 3")
        self.assertEqual(new_title("Volume 1", "Hawkeye & The Thunderbolts", "1"), "Hawkeye & The Thunderbolts Vol. 1")
        self.assertEqual(new_title("Vol. 2", "Hawkeye & The Thunderbolts", 2), "Hawkeye & The Thunderbolts Vol. 2")

    def test_one_offs_take_the_series_name(self):
        self.assertEqual(new_title("TPB", "Thunderbolts: Worldstrike", "1"), "Thunderbolts: Worldstrike")
        self.assertEqual(new_title("Omnibus", "Daredevil By Chip Zdarsky Omnibus", None), "Daredevil By Chip Zdarsky Omnibus")

    def test_nothing_sensible_means_no_change(self):
        self.assertIsNone(new_title("Distant Rumblings!", "Thunderbolts", "-1"))
        self.assertIsNone(new_title("Volume 1", "", "1"))
        self.assertIsNone(new_title("Volume 1", "Series", None))      # a numbered volume needs its number


def book(i, title, series="Thunderbolts", link=True):
    links = [{"label": "Comic Vine", "url": "https://comicvine.gamespot.com/x/4000-%d/" % (100 + i)}] if link else []
    return {"id": f"b{i}", "name": f"Book {i}", "seriesTitle": series, "metadata": {"title": title, "links": links, "titleLock": False}}


class FakeServers:
    """Komga (one library, 3 books) and Comic Vine (volume name by issue id) in one fake urlopen."""
    def __init__(self):
        self.books = {b["id"]: b for b in [book(1, "Volume Three"), book(2, "Part Three"), book(3, "Volume 2", link=False), book(4, "TPB")]}
        self.scans, self.patches = [], []

    def __call__(self, req, timeout=None):
        url = urllib.parse.urlparse(req.full_url)
        if "comicvine" in url.netloc:
            iid = int(url.path.split("4000-")[1].strip("/"))
            vol = {101: ("Thunderbolts Classic", "3"), 104: ("Thunderbolts: Worldstrike", "1")}[iid]
            body = {"results": {"name": "x", "issue_number": vol[1], "volume": {"name": vol[0]}}}
        elif req.get_method() == "POST":
            self.scans.append(url.path)
            return io.BytesIO(b"")
        elif req.get_method() == "PATCH":
            bid = url.path.split("/")[-2]
            patch = json.loads(req.data)
            self.books[bid]["metadata"].update(patch)
            self.patches.append((bid, patch))
            return io.BytesIO(b"")
        elif url.path.endswith("/libraries"):
            body = [{"id": "LIB"}]
        elif url.path.endswith("/books"):
            body = {"content": list(self.books.values()), "totalPages": 1}
        else:
            body = self.books[url.path.split("/")[-1]]
        return io.BytesIO(json.dumps(body).encode())


class _Ctx:
    def __init__(self, b): self.b = b
    def __enter__(self): return self.b
    def __exit__(self, *a): return False


class FlowTests(unittest.TestCase):
    def run_fix(self, **kw):
        fake = FakeServers()
        with mock.patch("urllib.request.urlopen", lambda r, timeout=None: _Ctx(fake(r, timeout))), mock.patch("time.sleep"):
            changes = komga_title_fix.fix_titles(komga_title_fix.Komga("http://k", "key"), "cv", pause=0, say=lambda *a: None, **kw)
        return fake, changes

    def test_preview_changes_nothing(self):
        fake, changes = self.run_fix()
        self.assertEqual(sorted(t for _, t in changes), ["Thunderbolts Classic Vol. 3", "Thunderbolts: Worldstrike"])
        self.assertEqual(fake.patches, [])

    def test_apply_retitles_and_locks_only_collected_editions(self):
        fake, _ = self.run_fix(apply=True)
        self.assertEqual(sorted(fake.patches), [("b1", {"title": "Thunderbolts Classic Vol. 3", "titleLock": True}),
                                                ("b4", {"title": "Thunderbolts: Worldstrike", "titleLock": True})])
        self.assertEqual(fake.books["b2"]["metadata"]["title"], "Part Three")   # a single issue
        self.assertEqual(fake.books["b3"]["metadata"]["title"], "Volume 2")     # no Comic Vine link: nothing to look up


class RunnerHookTests(unittest.TestCase):
    def args(self, mode):
        return SimpleNamespace(title_fix=mode, scan_wait=7, state_dir=Path(tempfile.mkdtemp()))

    def test_rescans_waits_then_fixes(self):
        import comictagger_runner as runner
        fake, waits = FakeServers(), []
        env = {"KOMGA_API_KEY": "k", "COMICVINE_KEY": "cv"}
        with mock.patch.dict(os.environ, env), mock.patch("urllib.request.urlopen", lambda r, timeout=None: _Ctx(fake(r, timeout))), mock.patch("time.sleep"):
            changes = runner.title_fix({"library_ids": ["LIB"]}, self.args("apply"), say=lambda *a: None, sleep=waits.append)
        self.assertEqual(fake.scans, ["/api/v1/libraries/LIB/scan"])
        self.assertEqual(waits, [7])
        self.assertEqual(len(changes), 2)
        self.assertEqual(len(fake.patches), 2)

    def test_skips_quietly_without_keys_or_libraries(self):
        import comictagger_runner as runner
        said = []
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(komga_title_fix, "comicvine_key", lambda *a: None):
            self.assertIsNone(runner.title_fix({"library_ids": ["LIB"]}, self.args("apply"), say=said.append))
        self.assertIn("skipped", said[0])


if __name__ == "__main__":
    unittest.main()
