"""Tests for komga_readlist_merge.py against a fake Komga (no network). Run: python test_readlist_merge.py"""
import io, json, os, sys, tempfile, unittest, urllib.parse
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import komga_readlist_merge as m


def book(i, series, number, date, lib):
    return {"id": f"b{i}", "name": f"{series} #{number:03d}", "number": number, "seriesTitle": series, "libraryId": lib,
            "url": f"/x/{series}/{series} #{number:03d}.cbz", "metadata": {"releaseDate": date, "number": str(number)}}


class FakeKomga:
    """Serves one read list that spans two libraries and one library of new books, 5 items per page."""
    def __init__(self, listed, library):
        self.listed, self.library, self.patched = listed, library, None

    def __call__(self, req, timeout=None):
        url = urllib.parse.urlparse(req.full_url)
        q = urllib.parse.parse_qs(url.query)
        size, page = int(q.get("size", ["5"])[0]), int(q.get("page", ["0"])[0])
        size = min(size, 5)                                            # the server caps the page size
        if req.get_method() == "PATCH":
            self.patched = json.loads(req.data)["bookIds"]
            body = {"id": "L", "name": "! Test", "bookIds": self.patched}
        elif url.path.endswith("/readlists/L/books"):
            body = self._page(self.listed, size, page)
        elif url.path.endswith("/readlists/L"):
            body = {"id": "L", "name": "! Test", "bookIds": self.patched or [b["id"] for b in self.listed]}
        else:
            body = self._page(self.library, size, page)
        return io.BytesIO(json.dumps(body).encode())

    @staticmethod
    def _page(items, size, page):
        return {"content": items[page * size:(page + 1) * size], "totalPages": max(1, -(-len(items) // size))}


def run(listed, library, apply=False):
    fake = FakeKomga(listed, library)
    out = os.path.join(tempfile.mkdtemp(), "p.csv")
    argv = ["x", "--library-id", "LIB", "--readlist-id", "L", "--preview", out] + (["--apply"] if apply else [])
    resp = lambda req, timeout=None: _Ctx(fake(req, timeout))
    with mock.patch.dict(os.environ, {"KOMGA_API_KEY": "k"}), mock.patch.object(sys, "argv", argv), \
            mock.patch("urllib.request.urlopen", resp):
        m.main()
    return fake


class _Ctx:
    def __init__(self, b): self.b = b
    def __enter__(self): return self.b
    def __exit__(self, *a): return False


def listed_books():
    # other libraries, in order, with an omnibus dated 2017 among the 1976 issues (an outlier)
    items = [book(i, "Old", i, f"1976-{(i % 12) + 1:02d}-01", "OTHER") for i in range(1, 9)]
    items[3]["metadata"]["releaseDate"] = "2017-01-01"
    items += [book(20 + i, "Mid", i, f"1997-{i:02d}-01", "OTHER") for i in (1, 2, 3, 5, 6, 7)]   # issue 4 is missing
    items += [book(40 + i, "Late", i, f"2005-{i:02d}-01", "OTHER") for i in range(1, 6)]
    return items


class MergeTests(unittest.TestCase):
    def test_keeps_entries_from_other_libraries_and_places_by_date(self):
        listed = listed_books()
        new = [book(90, "Mid", 4, None, "LIB")]   # same folder, so its date is interpolated to April 1997
        fake = run(listed, new, apply=True)
        ids = [b["id"] for b in listed]
        self.assertEqual([i for i in fake.patched if i != "b90"], ids, "existing entries lost or reordered")
        pos = fake.patched.index("b90")
        self.assertTrue(ids.index("b23") < pos <= ids.index("b25"), f"placed at {pos}, not between issues 3 and 5")

    def test_unplaceable_books_go_to_the_end(self):
        listed = listed_books()
        new = [{**book(91, "Undated", 0, None, "LIB"), "name": "Undated Omnibus"}]
        new[0]["metadata"] = {}
        fake = run(listed, new, apply=True)
        self.assertEqual(fake.patched[-1], "b91")
        self.assertEqual(len(fake.patched), len(listed) + 1)

    def test_empty_read_list_refuses(self):
        with self.assertRaises(SystemExit):
            run([], [book(92, "New", 1, "1997-04-15", "LIB")], apply=True)


if __name__ == "__main__":
    unittest.main()
