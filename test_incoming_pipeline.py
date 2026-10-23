"""Tests for incoming_pipeline.py. Run: python test_incoming_pipeline.py"""
import io, json, os, sys, tempfile, time, unittest, urllib.parse
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import incoming_pipeline as ip
from PIL import Image

CFG_ROUTES = [{"match": "hulk", "collection": "Hulk Collection", "lists": ["! Marvel Universe"],
               "anchors": [{"match": "masterworks", "anchor": "1962-05"}, {"match": "loeb", "anchor": "2008-01"}]}]


def pic(seed):
    im = Image.new("L", (40, 60))
    im.putdata([(x * seed + y * 3 * (seed % 5 + 1)) % 256 for y in range(60) for x in range(40)])
    return im


class HelperTests(unittest.TestCase):
    def test_sanitize(self):
        self.assertEqual(ip.sanitize("Marvel Masterworks: The Incredible Hulk"), "Marvel Masterworks - The Incredible Hulk")
        self.assertEqual(ip.sanitize('What If?: "Why" <or> not*'), "What If - Why or not")

    def test_finished_means_old_and_no_marker(self):
        d = Path(tempfile.mkdtemp())
        f = d / "a.cbz"; f.write_bytes(b"x")
        now = time.time()
        os.utime(f, (now - 60, now - 60))
        self.assertFalse(ip.is_finished(f, now))                       # too new
        os.utime(f, (now - 3600, now - 3600))
        self.assertTrue(ip.is_finished(f, now))
        (d / "a.cbz.aria2").write_bytes(b"")
        self.assertFalse(ip.is_finished(f, now))                       # aria2 still working on it

    def test_routes_and_anchors(self):
        cfg = {"routes": CFG_ROUTES}
        self.assertEqual(ip.route_for(cfg, "Marvel Masterworks: The Incredible Hulk")[1], "1962-05")
        self.assertEqual(ip.route_for(cfg, "Hulk by Jeph Loeb: The Complete Collection")[1], "2008-01")
        self.assertEqual(ip.route_for(cfg, "The Immortal Hulk Omnibus")[1], None)         # route without a default anchor
        self.assertEqual(ip.route_for(cfg, "Batman"), (None, None))

    def test_placement_after_the_anchor_and_ignores_outliers(self):
        era1 = [f"1976-{m:02d}-01" for m in range(1, 10)]
        era1[4] = "2017-01-01"                                           # an omnibus dated 2017 among the 1976 issues
        era2 = ["1997-06-01", "1997-07-01", "1997-07-01", "1997-09-01", "1997-10-01", "1998-01-01", "1998-02-01"]
        dates = era1 + era2
        self.assertEqual(ip.placement_index(dates, "1997-08"), len(era1) + 3)       # before 1997-09, even though an omnibus is dated 2017
        self.assertEqual(ip.placement_index(dates, "2030-01"), len(dates))
        self.assertEqual(ip.placement_index(dates, "1970-01"), 0)

    def test_insert_block_keeps_existing_order(self):
        self.assertEqual(ip.insert_block(["a", "b", "c"], ["x", "y"], 1), ["a", "x", "y", "b", "c"])
        self.assertEqual(ip.insert_block(["a", "b"], ["a", "z"], 2), ["a", "b", "z"])        # already present: not duplicated


class CoverTests(unittest.TestCase):
    def test_verdicts(self):
        cover, other = pic(7), pic(23)
        self.assertEqual(ip.cover_verdict([cover.copy(), other], cover), "ok")
        self.assertEqual(ip.cover_verdict([other, cover.copy()], cover), "later-page")
        self.assertEqual(ip.cover_verdict([other, pic(41)], cover), "mismatch")
        self.assertEqual(ip.cover_verdict([], cover), "unreadable")
        self.assertEqual(ip.cover_verdict([None, cover], cover), "unreadable")


def make_cfg():
    root = Path(tempfile.mkdtemp())
    (root / "Incoming").mkdir()
    return {"incoming": str(root / "Incoming"), "ready": str(root / "Incoming" / "_ready"), "routes": CFG_ROUTES, "min_age_minutes": 10,
            "include_file": str(root / "readlist_include.txt")}


def old_file(cfg, name):
    p = Path(cfg["incoming"]) / name
    p.write_bytes(b"data")
    t = time.time() - 7200
    os.utime(p, (t, t))
    return p


ISSUE = {"name": "Volume One", "issue_number": "1", "volume": {"id": 35210, "name": "Marvel Masterworks: The Incredible Hulk"}}
VOL = {"publisher": {"name": "Marvel"}, "start_year": "2003"}


class PrepareTests(unittest.TestCase):
    def patches(self, verdict="ok", tagged=False, match_status="match"):
        m = {"status": match_status, "why": "no Comic Vine volume has that name", "issue": {"id": 55}, "volume": {"name": "x"}}
        self.calls = []
        return [mock.patch.object(ip.tag_collected, "already_tagged", lambda p: tagged),
                mock.patch.object(ip.tag_collected, "match", lambda k, p, aliases=None: m),
                mock.patch.object(ip.tag_collected, "_get", lambda k, path, **kw: ISSUE if path.startswith("issue") else VOL),
                mock.patch.object(ip, "first_pages", lambda p: []),
                mock.patch.object(ip, "cv_cover", lambda k, i: None),
                mock.patch.object(ip, "cover_verdict", lambda pages, cv: verdict),
                mock.patch.object(ip.tag_collected, "tag", lambda exe, p, i: self.calls.append(("tag", i)) or True),
                mock.patch.object(ip, "set_title", lambda p, t: self.calls.append(("title", t)) or True),
                mock.patch.object(ip, "issue_id_of", lambda p: 55)]

    def run_one(self, **kw):
        cfg = make_cfg()
        p = old_file(cfg, "Marvel Masterworks - The Incredible Hulk v01 (2009).cbz")
        from contextlib import ExitStack
        with ExitStack() as st:
            for pt in self.patches(**kw):
                st.enter_context(pt)
            return ip.prepare_file(p, cfg, "cv", say=lambda *a: None), cfg

    def test_ready_file_is_tagged_retitled_and_routed(self):
        r, _ = self.run_one()
        self.assertEqual(r["status"], "ready")
        self.assertEqual(r["dest"], os.path.join("Hulk Collection", "Marvel Masterworks - The Incredible Hulk", "Marvel Masterworks - The Incredible Hulk v01 (2009).cbz"))
        self.assertEqual(r["title"], "Marvel Masterworks: The Incredible Hulk Vol. 1")
        self.assertEqual((r["anchor"], r["lists"]), ("1962-05", ["! Marvel Universe"]))
        self.assertEqual(self.calls, [("tag", 55), ("title", "Marvel Masterworks: The Incredible Hulk Vol. 1")])

    def test_bad_cover_holds_the_file_untouched(self):
        r, _ = self.run_one(verdict="later-page")
        self.assertEqual(r["status"], "held")
        self.assertIn("cover", r["reason"])
        self.assertEqual(self.calls, [])                               # not tagged, not retitled

    def test_approved_cover_passes_and_damaged_archive_is_called_that(self):
        cfg = make_cfg()
        p = old_file(cfg, "Marvel Masterworks - The Incredible Hulk v01 (2009).cbz")
        st = Path(cfg["incoming"]) / "_state"; st.mkdir()
        (st / "approved_covers.txt").write_text("# looked at\n" + p.name + "\n")
        from contextlib import ExitStack
        with ExitStack() as s:
            for pt in self.patches(verdict="mismatch"):
                s.enter_context(pt)
            r = ip.prepare_file(p, cfg, "cv", say=lambda *a: None)
        self.assertEqual(r["status"], "ready")                          # approved by name
        import zipfile
        with ExitStack() as s:
            for pt in self.patches():
                s.enter_context(pt)
            s.enter_context(mock.patch.object(ip, "first_pages", mock.Mock(side_effect=zipfile.BadZipFile("File is not a zip file"))))
            r = ip.prepare_file(old_file(cfg, "broken.cbz"), cfg, "cv", say=lambda *a: None)
        self.assertEqual(r["status"], "held")
        self.assertIn("damaged or incomplete", r["reason"])

    def test_unmatched_file_is_held(self):
        r, _ = self.run_one(match_status="skip")
        self.assertEqual(r["status"], "held")
        self.assertIn("not tagged", r["reason"])

    def test_already_tagged_file_is_not_tagged_again(self):
        r, _ = self.run_one(tagged=True)
        self.assertEqual(r["status"], "ready")
        self.assertEqual([c[0] for c in self.calls], ["title"])

    def test_unrouted_series_is_held(self):
        cfg = make_cfg(); cfg["routes"] = []
        p = old_file(cfg, "x.cbz")
        from contextlib import ExitStack
        with ExitStack() as st:
            for pt in self.patches():
                st.enter_context(pt)
            r = ip.prepare_file(p, cfg, "cv", say=lambda *a: None)
        self.assertEqual(r["status"], "held")
        self.assertIn("route", r["reason"])


class RunPrepareTests(unittest.TestCase):
    def test_moves_ready_holds_the_rest_and_skips_downloads(self):
        cfg = make_cfg()
        good, bad = old_file(cfg, "good.cbz"), old_file(cfg, "bad.cbz")
        busy = old_file(cfg, "busy.cbz"); (busy.parent / "busy.cbz.aria2").write_bytes(b"")
        fresh = Path(cfg["incoming"]) / "fresh.cbz"; fresh.write_bytes(b"x")
        def fake(path, cfg_, key, exe="comictagger", dry_run=False, say=print):
            if path.name == "good.cbz":
                return {"file": str(path), "status": "ready", "dest": os.path.join("Hulk Collection", "S", "good.cbz"), "title": "T", "volume": "S",
                        "number": "1", "issue": 5, "collection": "Hulk Collection", "library": "Hulk Collection", "anchor": "1962-05", "lists": []}
            return {"file": str(path), "status": "held", "reason": "no match"}
        with mock.patch.object(ip, "prepare_file", fake):
            rep = ip.run_prepare(cfg, "cv", say=lambda *a: None)
        self.assertTrue((Path(cfg["ready"]) / "Hulk Collection" / "S" / "good.cbz").exists())
        self.assertTrue(bad.exists() and busy.exists() and fresh.exists())
        self.assertEqual([h["file"] for h in rep["held"]], ["bad.cbz"])
        self.assertEqual(sorted(rep["waiting"]), ["busy.cbz", "fresh.cbz"])
        self.assertIn(os.path.join("Hulk Collection", "S", "good.cbz"), ip.load_state(cfg)["files"])

    def test_dry_run_changes_nothing(self):
        cfg = make_cfg()
        f = old_file(cfg, "good.cbz")
        ok = {"file": str(f), "status": "ready", "dest": "Hulk Collection/S/good.cbz", "title": "T", "volume": "S", "number": "1", "issue": 5,
              "collection": "Hulk Collection", "library": "Hulk Collection", "anchor": None, "lists": []}
        with mock.patch.object(ip, "prepare_file", lambda *a, **k: ok):
            ip.run_prepare(cfg, "cv", dry_run=True, say=lambda *a: None)
        self.assertTrue(f.exists())
        self.assertEqual(ip.load_state(cfg)["files"], {})


class FakeKomga:
    def __init__(self, books, lists):
        self.books = {b["id"]: b for b in books}
        self.lists = lists                       # id -> {"name", "bookIds"}
        self.scans, self.patches, self.created = [], [], []

    def call(self, path, method="GET", body=None):
        if path == "/libraries":
            return [{"id": "LIBH", "name": "Hulk Collection"}]
        if method == "PATCH" and "/metadata" in path:
            bid = path.split("/")[2]
            self.books[bid]["metadata"].update(body); self.patches.append((bid, body))
            return None
        if method == "POST" and path == "/readlists":
            rid = "NEW%d" % len(self.created)
            self.lists[rid] = {"name": body["name"], "bookIds": list(body["bookIds"])}
            self.created.append(body["name"])
            return {"id": rid, "name": body["name"]}
        if method == "PATCH":
            rid = path.split("/")[-1]; self.lists[rid]["bookIds"] = body["bookIds"]; self.patches.append((rid, body))
            return None
        return {"bookIds": self.lists[path.split("/")[-1]]["bookIds"]}

    def scan(self, lib): self.scans.append(lib)

    def paged(self, path):
        if path.startswith("/books?library_id"):
            return [b for b in self.books.values() if b["library"] == "LIBH"]
        if path == "/readlists":
            return [{"id": k, "name": v["name"]} for k, v in self.lists.items()]
        rid = path.split("/")[2]
        return [self.books[i] for i in self.lists[rid]["bookIds"]]


def mkbook(i, name, date, library="OTHER"):
    return {"id": f"b{i}", "url": f"/mirror/x/{name}", "library": library, "metadata": {"releaseDate": date, "title": name}}


class KomgaStageTests(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg()
        old = [mkbook(i, f"Old {i}", d) for i, d in enumerate(["1961-01-01", "1962-03-01", "1962-09-01", "1963-01-01"], 1)]
        new = [mkbook(10, "v01.cbz", "2009-03-01", "LIBH"), mkbook(11, "v02.cbz", "2012-03-01", "LIBH")]
        lists = {"MU": {"name": "! Marvel Universe - Suggested Reading Order", "bookIds": [b["id"] for b in old]}, "HC": {"name": "Hulk Collection", "bookIds": ["b10"]}}
        self.komga = FakeKomga(old + new, lists)
        state = {"files": {os.path.join("Hulk Collection", "S", n): {"title": t, "volume": "S", "number": str(i), "issue": i, "collection": "Hulk Collection",
                                                                    "anchor": "1962-05", "lists": ["! Marvel Universe - Suggested Reading Order"], "uploaded_at": 1000.0}
                           for i, (n, t) in enumerate([("v01.cbz", "S Vol. 1"), ("v02.cbz", "S Vol. 2")], 1)}}
        ip.save_state(self.cfg, state)

    def go(self, now):
        with mock.patch("time.sleep"):
            return ip.run_komga(self.cfg, self.komga, now=now, say=lambda *a: None)

    def test_waits_for_the_mirror_mount(self):
        self.assertEqual(self.go(1000.0 + 60), 0)
        self.assertEqual(self.komga.scans, [])

    def test_locks_adds_to_lists_and_keeps_existing_order(self):
        self.assertEqual(self.go(1000.0 + ip.KOMGA_DELAY + 1), 2)
        self.assertEqual(self.komga.scans, ["LIBH"])
        self.assertEqual(self.komga.books["b10"]["metadata"]["title"], "S Vol. 1")
        self.assertTrue(self.komga.books["b10"]["metadata"]["titleLock"])
        self.assertEqual(self.komga.lists["HC"]["bookIds"], ["b10", "b11"])                         # own list: appended, no duplicate
        mu = self.komga.lists["MU"]["bookIds"]
        self.assertEqual([i for i in mu if i not in ("b10", "b11")], ["b1", "b2", "b3", "b4"])      # existing order intact
        self.assertEqual(mu[:3], ["b1", "b2", "b10"])                                               # block after the last entry before May 1962
        self.assertTrue(all(v.get("komga_done") for v in ip.load_state(self.cfg)["files"].values()))
        self.assertEqual(self.go(1000.0 + ip.KOMGA_DELAY + 99), 0)                                   # finished files are not redone

    def test_missing_collection_list_is_created_from_the_staged_books_only_and_registered_for_the_site(self):
        self.komga.books["b99"] = mkbook(99, "added-by-hand.cbz", "2001-01-01", "LIBH")               # in the library, never staged
        self.go(1000.0 + ip.KOMGA_DELAY + 1)
        name = "! Hulk Collection - Suggested Reading Order"
        self.assertIn(name, self.komga.created)
        rid = next(k for k, v in self.komga.lists.items() if v["name"] == name)
        self.assertEqual(self.komga.lists[rid]["bookIds"], ["b10", "b11"])                          # the hand-added book is not in it
        self.assertEqual(Path(self.cfg["include_file"]).read_text().splitlines()[1:], ["Hulk Collection", "Marvel Universe"])
        self.assertEqual(ip.load_state(self.cfg)["touched_lists"], ["Hulk Collection", "Marvel Universe"])

    def test_files_finished_by_an_older_version_get_their_lists_checked_once(self):
        state = ip.load_state(self.cfg)
        for v in state["files"].values():
            v["komga_done"] = True                       # done by the old script: metadata locked, lists never made
        ip.save_state(self.cfg, state)
        self.assertEqual(self.go(1000.0 + ip.KOMGA_DELAY + 1), 2)
        self.assertIn("! Hulk Collection - Suggested Reading Order", self.komga.created)
        self.assertEqual(self.go(1000.0 + ip.KOMGA_DELAY + 99), 0)

    def test_a_new_publisher_gets_its_own_master_list(self):
        state = ip.load_state(self.cfg)
        for v in state["files"].values():
            v["lists"] = [ip.site_list_name(ip.master_base({"publishers": {"Marvel": "Marvel Universe"}}, "Image Comics"))]
            v["anchor"] = "1992-04"
        ip.save_state(self.cfg, state)
        self.go(1000.0 + ip.KOMGA_DELAY + 1)
        self.assertIn("! Image Comics - Suggested Reading Order", self.komga.created)
        self.assertIn("Image Comics", Path(self.cfg["include_file"]).read_text().splitlines())

    def test_existing_site_list_is_updated_in_place_not_recreated(self):
        self.komga.lists["SL"] = {"name": "! Hulk Collection - Suggested Reading Order", "bookIds": ["b1", "b2", "b3"]}
        self.go(1000.0 + ip.KOMGA_DELAY + 1)
        self.assertNotIn("! Hulk Collection - Suggested Reading Order", self.komga.created)
        self.assertEqual([i for i in self.komga.lists["SL"]["bookIds"] if i not in ("b10", "b11")], ["b1", "b2", "b3"])
        self.assertTrue({"b10", "b11"} <= set(self.komga.lists["SL"]["bookIds"]))

    def test_book_komga_has_not_seen_yet_is_retried_later(self):
        del self.komga.books["b11"]
        self.assertEqual(self.go(1000.0 + ip.KOMGA_DELAY + 1), 1)
        st = ip.load_state(self.cfg)["files"]
        self.assertFalse(st[os.path.join("Hulk Collection", "S", "v02.cbz")].get("komga_done"))


class ListChecksTests(unittest.TestCase):
    BOOKS = [mkbook(i, f"B{i}", f"{1960 + i // 3}-0{i % 3 + 1}-01") for i in range(1, 10)]

    def test_publisher_master_list_names(self):
        cfg = {"publishers": {"Marvel": "Marvel Universe", "DC Comics": "DC Universe"}}
        self.assertEqual(ip.site_list_name(ip.master_base(cfg, "Marvel")), "! Marvel Universe - Suggested Reading Order")
        self.assertEqual(ip.master_base(cfg, "DC Comics"), "DC Universe")
        self.assertEqual(ip.master_base(cfg, "Image Comics"), "Image Comics")                  # any other publisher: a master list of its own
        self.assertEqual(ip.master_base(cfg, "Dark Horse: Comics"), "Dark Horse - Comics")

    def test_a_good_insert_passes(self):
        ids = [b["id"] for b in self.BOOKS]
        new = ip.insert_block(ids, ["x1", "x2"], 4)
        anchor = "1961-06"
        self.assertEqual(ip.verify_insert(self.BOOKS, new, ["x1", "x2"], 4, anchor), [])

    def test_bad_inserts_are_caught(self):
        ids = [b["id"] for b in self.BOOKS]
        reordered = list(reversed(ids[:3])) + ids[3:] + ["x1"]
        self.assertIn("existing entries were lost or reordered", ip.verify_insert(self.BOOKS, reordered, ["x1"], 9, "1961-06"))
        split = ids[:2] + ["x1"] + ids[2:5] + ["x2"] + ids[5:]
        self.assertIn("the new books are not together", ip.verify_insert(self.BOOKS, split, ["x1", "x2"], 2, "1961-06"))
        self.assertIn("duplicate entries", ip.verify_insert(self.BOOKS, ids + ["x1", "x1"], ["x1"], 9, "1963-01"))
        early = ip.insert_block(ids, ["x1"], 0)                                                 # forced to the very start although its anchor is 1990
        self.assertTrue(any("after the block is dated" in p and "months before its anchor" in p for p in ip.verify_insert(self.BOOKS, early, ["x1"], 0, "1990-01")))
        late = ip.insert_block(ids, ["x1"], 9)                                                  # forced to the very end although its anchor is 1950
        self.assertTrue(any("before the block is dated" in p and "months after its anchor" in p for p in ip.verify_insert(self.BOOKS, late, ["x1"], 9, "1950-01")))

    def test_export_must_match_komga_and_a_bad_file_is_replaced(self):
        books = [dict(mkbook(i, f"B{i}", "2000-01-01"), seriesTitle=f"Series {i}", number=i) for i in range(1, 5)]
        for b in books:
            b["metadata"]["number"] = str(b["number"])
        komga = FakeKomga(books, {"L": {"name": "! Hulk Collection - Suggested Reading Order", "bookIds": [b["id"] for b in books]}})
        d = Path(tempfile.mkdtemp())
        good = "\n".join(f'    <Book Series="Series {i}" Number="{i}" Year="2000" />' for i in range(1, 5))
        bad = "\n".join(f'    <Book Series="Series {i}" Number="{i}" Year="2000" />' for i in (3, 1, 2, 4))
        (d / "cbl_history").mkdir()
        (d / "cbl_history" / "Hulk Collection - Suggested Reading Order_20260101_000000.cbl").write_text(good)
        target = d / "Hulk Collection - Suggested Reading Order.cbl"
        target.write_text(good)
        self.assertEqual(ip.verify_export({}, komga, ["Hulk Collection"], dest=str(d), say=lambda *a: None), [])
        target.write_text(bad)
        self.assertEqual(ip.verify_export({}, komga, ["Hulk Collection"], dest=str(d), say=lambda *a: None), ["Hulk Collection"])
        self.assertEqual(target.read_text(), good)                                              # the previous good copy is back


class SelfTestGateTests(unittest.TestCase):
    def test_failing_suite_stops_the_pipeline_and_passing_suite_lets_it_run(self):
        d = Path(tempfile.mkdtemp())
        (d / "test_green_gate.py").write_text("import unittest\nclass T(unittest.TestCase):\n    def test_a(self): self.assertTrue(True)\n")
        (d / "test_red_gate.py").write_text("import unittest\nclass T(unittest.TestCase):\n    def test_a(self): self.assertTrue(False)\n")
        said = []
        self.assertTrue(ip.run_selftests(said.append, modules=["test_green_gate"], extra_paths=[str(d)]))
        self.assertFalse(ip.run_selftests(said.append, modules=["test_red_gate"], extra_paths=[str(d)]))
        self.assertTrue(any("SELF-TESTS FAILED" in s for s in said))
        self.assertFalse(ip.run_selftests(said.append, modules=["test_does_not_exist_gate"], extra_paths=[str(d)]))
        self.assertTrue(any("missing" in s for s in said))


class MarkUploadedTests(unittest.TestCase):
    def test_stamps_and_moves_to_done(self):
        cfg = make_cfg()
        f = Path(cfg["ready"]) / "Hulk Collection" / "S" / "a.cbz"
        f.parent.mkdir(parents=True); f.write_bytes(b"x")
        ip.save_state(cfg, {"files": {os.path.join("Hulk Collection", "S", "a.cbz"): {"collection": "Hulk Collection"}}})
        self.assertEqual(ip.mark_uploaded(cfg, now=5000.0), 1)
        self.assertFalse(f.exists())
        self.assertTrue((Path(cfg["incoming"]) / "_done" / "Hulk Collection" / "S" / "a.cbz").exists())
        self.assertEqual(ip.load_state(cfg)["files"][os.path.join("Hulk Collection", "S", "a.cbz")]["uploaded_at"], 5000.0)


if __name__ == "__main__":
    unittest.main()
