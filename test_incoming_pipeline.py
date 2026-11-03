"""Tests for incoming_pipeline.py. Run: python test_incoming_pipeline.py"""
import io, json, os, sys, tempfile, time, unittest, urllib.parse, zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import incoming_pipeline as ip
from PIL import Image

CFG_ROUTES = [{"match": "hulk", "collection": "Hulk Collection", "lists": ["! Marvel Universe - Suggested Reading Order"],
               "anchors": [{"match": "masterworks", "anchor": "1962-05"}, {"match": "loeb", "anchor": "2008-01"}]}]


def pic(seed):
    im = Image.new("L", (40, 60))
    im.putdata([(x * seed + y * 3 * (seed % 5 + 1)) % 256 for y in range(60) for x in range(40)])
    return im


class BadDownloadTests(unittest.TestCase):
    def test_a_web_page_saved_as_a_comic_is_recognised_and_moved_aside(self):
        d = Path(tempfile.mkdtemp())
        page = d / "Chronology, Volume 001.cbz"
        page.write_bytes(b"<!DOCTYPE html>\n<html lang='en'><head><title>ViKiNG FiLE</title></head></html>" + b" " * 600)
        real = d / "Real 001.cbz"
        with zipfile.ZipFile(real, "w") as z:
            z.writestr("1.jpg", b"x")
        rar = d / "Old 001.cbr"
        rar.write_bytes(b"Rar!\x1a\x07\x00" + b"x" * 40)
        self.assertEqual(ip.bad_download_kind(page), "a web page")
        self.assertIsNone(ip.bad_download_kind(real))
        self.assertIsNone(ip.bad_download_kind(rar))
        empty = d / "Empty.cbz"
        empty.write_bytes(b"")
        self.assertEqual(ip.bad_download_kind(empty), "empty")
        dest = ip.quarantine_bad_download(page, d)
        self.assertEqual(dest.parent.name, ip.BAD_DOWNLOADS)
        self.assertTrue(dest.exists() and not page.exists())
        again = d / "Chronology, Volume 001.cbz"
        again.write_bytes(b"<html></html>")
        self.assertEqual(ip.quarantine_bad_download(again, d).name, "Chronology, Volume 001 (2).cbz")      # no overwrite

    def test_a_dry_run_leaves_the_fake_download_where_it_is(self):
        d = Path(tempfile.mkdtemp())
        page = d / "x.cbz"
        page.write_bytes(b"<html></html>")
        ip.quarantine_bad_download(page, d, dry_run=True)
        self.assertTrue(page.exists())


class CoverRobustnessTests(unittest.TestCase):
    def test_a_scan_with_a_credit_line_along_the_bottom_still_matches_the_clean_cover(self):
        from PIL import ImageDraw
        cover = pic(11).resize((200, 300))
        scan = cover.copy()
        ImageDraw.Draw(scan).rectangle((0, 285, 200, 300), fill=0)               # the (c) line a digital scan adds
        self.assertEqual(ip.cover_verdict([scan], cover), "ok")

    def test_a_scan_that_opens_with_variant_covers_checks_more_pages(self):
        self.assertEqual(ip.cover_pages_to_check(Path("Invincible Iron Man 006 (2016) (5 covers) (digital).cbz")), 7)
        self.assertEqual(ip.cover_pages_to_check(Path("Thor 001 (2020).cbz")), 4)
        self.assertEqual(ip.cover_pages_to_check(Path("X 001 (1 cover).cbz")), 4)


class HelperTests(unittest.TestCase):
    def test_sanitize(self):
        self.assertEqual(ip.sanitize("Marvel Masterworks: The Incredible Hulk"), "Marvel Masterworks - The Incredible Hulk")
        self.assertEqual(ip.sanitize('What If?: "Why" <or> not*'), "What If - Why or not")

    def test_comicvine_issue_id_reads_komga_issue_link_or_tag(self):
        self.assertEqual(ip.comicvine_issue_id({"metadata": {"links": [{"url": "https://comicvine.gamespot.com/foo/4000-12345/"}]}}), 12345)
        self.assertEqual(ip.comicvine_issue_id({"metadata": {"tags": ["comicvine", "cv:4000-67890"]}}), 67890)

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
        self.assertEqual(ip.route_for(cfg, "Batman"), (None, None, None))

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


class TieInTests(unittest.TestCase):
    """Crossover chapters named after another series: 'Thor 444b (Captain America #398)', 'Thor 468a - Silver Surfer 086'."""

    def run_lookup(self, name, volumes, issues, parent_date=None):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / name
            f.write_bytes(b"x")
            if parent_date:
                (Path(d) / "Thor 444 (1992).cbz").write_bytes(b"x")
            with mock.patch.object(ip.tag_collected, "search_volumes", lambda key, n: volumes.get(n, [])), \
                    mock.patch.object(ip.tag_collected, "volume_issues", lambda key, vid, num=None: [i for i in issues.get(vid, []) if int(i["n"]) == int(num)]), \
                    mock.patch.object(ip.tag_collected, "_get", lambda *a, **k: {"cover_date": parent_date}), \
                    mock.patch.object(ip.tagcommon, "comicvine_issue_id", lambda p: 5 if "Thor 444 " in str(p) else None):
                return ip.tiein_issue_id("key", f)

    def test_one_series_one_number(self):
        vols = {"Captain America": [{"id": 1, "name": "Captain America"}, {"id": 2, "name": "Captain America"}]}
        issues = {1: [{"id": 100, "n": "398", "cover_date": "1992-03-01"}], 2: [{"id": 200, "n": "7", "cover_date": "1998-01-01"}]}
        self.assertEqual(self.run_lookup("Thor 444b (Captain America #398).cbz", vols, issues), 100)
        self.assertEqual(self.run_lookup("Thor 444b - Captain America 398.cbz", vols, issues), 100)

    def test_ambiguous_without_a_date_is_refused_and_the_date_decides_with_one(self):
        vols = {"Silver Surfer": [{"id": 1, "name": "Silver Surfer"}, {"id": 2, "name": "Silver Surfer"}]}
        issues = {1: [{"id": 100, "n": "79", "cover_date": "1993-04-01"}], 2: [{"id": 200, "n": "79", "cover_date": "2012-04-01"}]}
        self.assertIsNone(self.run_lookup("Thor 461b (Silver Surfer #79).cbz", vols, issues))
        self.assertEqual(self.run_lookup("Thor 444b (Silver Surfer #79).cbz", vols, issues, parent_date="1993-05-01"), 100)

    def test_a_date_far_from_the_parent_issue_is_refused(self):
        vols = {"Marvel Spotlight": [{"id": 1, "name": "Marvel Spotlight"}]}
        issues = {1: [{"id": 100, "n": "30", "cover_date": "1976-10-01"}]}
        self.assertIsNone(self.run_lookup("Thor 444b (Marvel Spotlight #30).cbz", vols, issues, parent_date="1992-02-01"))

    def test_ordinary_names_are_not_tie_ins(self):
        self.assertIsNone(self.run_lookup("Thor 444 (1992) (Digital).cbz", {}, {}))
        self.assertIsNone(self.run_lookup("Captain America 398 (1992).cbz", {}, {}))


class CoverTests(unittest.TestCase):
    def test_verdicts(self):
        cover, other = pic(7), pic(23)
        self.assertEqual(ip.cover_verdict([cover.copy(), other], cover), "ok")
        self.assertEqual(ip.cover_verdict([other, cover.copy()], cover), "later-page")
        self.assertEqual(ip.cover_verdict([other, pic(41)], cover), "mismatch")
        self.assertEqual(ip.cover_verdict([], cover), "unreadable")
        self.assertEqual(ip.cover_verdict([None, cover], cover), "unreadable")

    def test_reframed_scan_is_recognised_by_features_not_by_hash(self):
        import random
        from PIL import ImageDraw

        def art(seed, w=320, h=480):
            r = random.Random(seed)
            im = Image.new("RGB", (w, h), (r.randrange(256), r.randrange(256), r.randrange(256)))
            d = ImageDraw.Draw(im)
            for _ in range(140):
                x, y, size = r.randrange(w), r.randrange(h), r.randrange(8, 60)
                c = (r.randrange(256), r.randrange(256), r.randrange(256))
                (d.ellipse if r.random() < .5 else d.rectangle)((x, y, x + size, y + int(size * r.uniform(.4, 2))), fill=c)
            return im
        cover, other = art(1), art(2)
        scan = cover.crop((0, 50, 250, 480)).resize((250, 380))                  # another crop and size of the same cover
        self.assertGreater(ip.distance(ip.dhash(cover), ip.dhash(scan)), ip.COVER_OK)      # the quick hash rejects it
        self.assertEqual(ip.cover_verdict([scan, other], cover), "ok")                        # the feature check accepts it
        self.assertEqual(ip.cover_verdict([other, scan], cover), "later-page")
        self.assertEqual(ip.cover_verdict([other, art(3)], cover), "mismatch")              # unrelated art is still refused


def make_cfg():
    root = Path(tempfile.mkdtemp())
    (root / "Incoming").mkdir()
    return {"incoming": str(root / "Incoming"), "ready": str(root / "Incoming" / "_ready"), "routes": CFG_ROUTES, "min_age_minutes": 10,
            "publishers": {"Marvel": "Marvel Universe", "DC Comics": "DC Universe"},
            "include_file": str(root / "readlist_include.txt")}


def old_file(cfg, name):
    p = Path(cfg["incoming"]) / name
    p.write_bytes(b"data")
    t = time.time() - 7200
    os.utime(p, (t, t))
    return p


ISSUE = {"name": "Volume One", "issue_number": "1", "cover_date": "1962-05-01", "volume": {"id": 35210, "name": "The Incredible Hulk"}}
VOL = {"publisher": {"name": "Marvel"}, "start_year": "1962"}


class PrepareTests(unittest.TestCase):
    def patches(self, verdict="ok", tagged=False, match_status="match"):
        m = {"status": match_status, "why": "no Comic Vine volume has that name", "issue": {"id": 55}, "volume": {"name": "x"}}
        self.calls = []
        return [mock.patch.object(ip.tag_collected, "already_tagged", lambda p: tagged),
                mock.patch.object(ip.tag_collected, "match", lambda k, p, aliases=None, **kw: m),
                mock.patch.object(ip.tag_collected, "_get", lambda k, path, **kw: ISSUE if path.startswith("issue") else VOL),
                mock.patch.object(ip, "first_pages", lambda p, count=4: []),
                mock.patch.object(ip, "cv_cover", lambda k, i, *a: None),
                mock.patch.object(ip, "cover_verdict", lambda pages, cv, *a: verdict),
                mock.patch.object(ip.tag_collected, "tag_outcome", lambda exe, p, i: self.calls.append(("tag", i)) or "ok"),
                mock.patch.object(ip, "set_title", lambda p, t: self.calls.append(("title", t)) or True),
                mock.patch.object(ip, "issue_id_of", lambda p: 55)]

    def run_one(self, publishers=True, **kw):
        cfg = make_cfg()
        if not publishers:
            cfg.pop("publishers")
        p = old_file(cfg, "The Incredible Hulk v01 (1962).cbz")
        from contextlib import ExitStack
        with ExitStack() as st:
            for pt in self.patches(**kw):
                st.enter_context(pt)
            return ip.prepare_file(p, cfg, "cv", say=lambda *a: None), cfg

    def test_ready_file_is_tagged_retitled_and_routed(self):
        r, _ = self.run_one()
        self.assertEqual(r["status"], "ready")
        self.assertEqual(r["dest"], os.path.join("Hulk Collection", "The Incredible Hulk", "The Incredible Hulk v01 (1962).cbz"))
        self.assertEqual(r["title"], "The Incredible Hulk Vol. 1")
        self.assertEqual((r["anchor"], r["lists"]), ("1962-05", ["! Marvel Universe - Suggested Reading Order"]))         # the route's list and the publisher's master list are the same one
        self.assertEqual(self.calls, [("tag", 55), ("title", "The Incredible Hulk Vol. 1")])

    def test_a_publisher_without_a_configured_master_list_gets_one_named_after_it(self):
        r, _ = self.run_one(publishers=False)
        self.assertEqual(r["lists"], ["! Marvel - Suggested Reading Order", "! Marvel Universe - Suggested Reading Order"])     # still merged into the publisher's master list, as well as the route's

    def run_with_tag_outcome(self, outcome):
        from contextlib import ExitStack
        cfg = make_cfg()
        p = old_file(cfg, "The Incredible Hulk v01 (1962).cbz")
        with ExitStack() as st:
            for pt in self.patches()[:6] + self.patches()[7:]:
                st.enter_context(pt)
            st.enter_context(mock.patch.object(ip.tag_collected, "tag_outcome", lambda exe, p_, i: outcome))
            return ip.prepare_file(p, cfg, "cv", say=lambda *a: None)

    def test_a_rate_limited_tag_is_transient_and_a_real_tag_failure_is_not(self):
        transient = self.run_with_tag_outcome("transient")
        self.assertEqual((transient["status"], transient.get("transient")), ("held", True))
        failed = self.run_with_tag_outcome("failed")
        self.assertEqual((failed["status"], failed.get("transient")), ("held", None))
        self.assertEqual(failed["reason"], "ComicTagger could not write the tags")

    def test_bad_cover_holds_the_file_untouched(self):
        r, _ = self.run_one(verdict="later-page")
        self.assertEqual(r["status"], "held")
        self.assertIn("cover", r["reason"])
        self.assertEqual(self.calls, [])                               # not tagged, not retitled

    def prepare_collected(self, **cfg_extra):
        cfg = make_cfg(); cfg.update(cfg_extra)
        p = old_file(cfg, "Marvel Masterworks - The Incredible Hulk v01 (2009).cbz")
        from contextlib import ExitStack
        with ExitStack() as st:
            for pt in self.patches():
                st.enter_context(pt)
            return ip.prepare_file(p, cfg, "cv", say=lambda *a: None)

    def test_collected_editions_are_filed_automatically_without_a_list_anchor(self):
        r = self.prepare_collected()
        self.assertEqual(r["status"], "ready")
        self.assertTrue(r["collected"])
        self.assertIsNone(r["anchor"])
        self.assertTrue(ip.komga_ready({"collected": True}))
        self.assertFalse(ip.komga_ready({"anchor": "2009-01", "anchor_source": "guess"}))
        self.assertTrue(ip.komga_ready({"anchor": "2009-01", "anchor_source": "comicvine.cover_date"}))

    def test_collected_editions_can_still_be_held_when_the_config_asks(self):
        r = self.prepare_collected(hold_collected_editions=True)
        self.assertEqual(r["status"], "held")
        self.assertIn("collected edition", r["reason"])

    def test_named_trade_volume_is_recognized_even_when_cv_omits_format(self):
        self.assertTrue(ip.collected_edition("Daredevil by Chip Zdarsky v06 - Doing Time.cbz", {},
                                             "Daredevil By Chip Zdarsky: Doing Time", {"name": "Volume 6"}))

    def test_approved_cover_passes_and_damaged_archive_is_called_that(self):
        cfg = make_cfg()
        p = old_file(cfg, "The Incredible Hulk v01 (1962).cbz")
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

    def test_issue_already_in_komga_is_skipped_before_mutating_file(self):
        cfg = make_cfg(); p = old_file(cfg, "duplicate.cbz")
        from contextlib import ExitStack
        with ExitStack() as st:
            for pt in self.patches():
                st.enter_context(pt)
            r = ip.prepare_file(p, cfg, "cv", say=lambda *a: None, existing_ids={55})
        self.assertEqual(r["status"], "duplicate")
        self.assertTrue(p.exists())
        self.assertEqual(self.calls, [])

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
    def test_package_comics_are_deferred_when_komga_duplicate_check_fails(self):
        cfg = make_cfg()
        p = Path(cfg["incoming"]) / "_packages" / "pack" / "issue.cbz"
        p.parent.mkdir(parents=True); p.write_bytes(b"comic")
        old = time.time() - 900; os.utime(p, (old, old))
        with mock.patch.object(ip, "existing_issue_ids", side_effect=RuntimeError("Komga unavailable")):
            rep = ip.run_prepare(cfg, "cv", say=lambda *a: None)
        self.assertEqual(rep["held"], [])
        self.assertEqual(rep["deferred"], [p.name])
        self.assertTrue(p.exists())

    def test_unexpected_processing_errors_are_retried_then_deferred_not_held(self):
        cfg = make_cfg(); f = old_file(cfg, "temporary-error.cbz")
        with mock.patch.object(ip, "existing_issue_ids", return_value=set()), \
                mock.patch.object(ip, "prepare_file", side_effect=RuntimeError("temporary tool failure")) as process, \
                mock.patch("time.sleep"):
            rep = ip.run_prepare(cfg, "cv", say=lambda *a: None)
        self.assertEqual(process.call_count, 3)
        self.assertEqual(rep["held"], [])
        self.assertEqual(rep["deferred"], [f.name])
        self.assertTrue(f.exists())

    def test_loose_comics_are_deferred_if_komga_duplicate_check_fails(self):
        cfg = make_cfg(); f = old_file(cfg, "loose.cbz")
        with mock.patch.object(ip, "existing_issue_ids", side_effect=RuntimeError("Komga unavailable")), \
                mock.patch.object(ip, "prepare_file") as process:
            rep = ip.run_prepare(cfg, "cv", say=lambda *a: None)
        self.assertEqual(rep["held"], [])
        self.assertEqual(rep["deferred"], [f.name])
        self.assertTrue(f.exists())
        process.assert_not_called()

    def test_comicvine_rate_limit_defers_remainder_and_persists_cooldown(self):
        cfg = make_cfg()
        first, second = old_file(cfg, "first.cbz"), old_file(cfg, "second.cbz")
        def limited(*args, **kwargs):
            raise ip.urllib.error.HTTPError("https://comicvine.example", 420, "rate limited", {}, None)
        with mock.patch.object(ip, "existing_issue_ids", return_value=set()), mock.patch.object(ip, "prepare_file", limited):
            rep = ip.run_prepare(cfg, "cv", say=lambda *a: None)
        self.assertEqual(rep["deferred"], [first.name, second.name])
        self.assertEqual(rep["held"], [])
        self.assertGreater(ip.load_state(cfg)["cv_retry_after"], time.time())
        with mock.patch.object(ip, "existing_issue_ids", return_value=set()), mock.patch.object(ip, "prepare_file", side_effect=AssertionError("should honor cooldown")):
            rep = ip.run_prepare(cfg, "cv", say=lambda *a: None)
        self.assertEqual(rep["deferred"], [first.name, second.name])

    def test_moves_ready_holds_the_rest_and_skips_downloads(self):
        cfg = make_cfg()
        good, bad = old_file(cfg, "good.cbz"), old_file(cfg, "bad.cbz")
        busy = old_file(cfg, "busy.cbz"); (busy.parent / "busy.cbz.aria2").write_bytes(b"")
        fresh = Path(cfg["incoming"]) / "fresh.cbz"; fresh.write_bytes(b"x")
        def fake(path, cfg_, key, exe="comictagger", dry_run=False, say=print, existing_ids=None):
            if path.name == "good.cbz":
                return {"file": str(path), "status": "ready", "dest": os.path.join("Hulk Collection", "S", "good.cbz"), "title": "T", "volume": "S",
                        "number": "1", "issue": 5, "collection": "Hulk Collection", "library": "Hulk Collection", "anchor": "1962-05", "lists": []}
            return {"file": str(path), "status": "held", "reason": "no match"}
        with mock.patch.object(ip, "existing_issue_ids", return_value=set()), mock.patch.object(ip, "prepare_file", fake):
            rep = ip.run_prepare(cfg, "cv", say=lambda *a: None)
        self.assertTrue((Path(cfg["ready"]) / "Hulk Collection" / "S" / "good.cbz").exists())
        self.assertTrue(bad.exists() and busy.exists() and fresh.exists())
        self.assertEqual([h["file"] for h in rep["held"]], ["bad.cbz"])
        self.assertEqual(sorted(rep["waiting"]), ["busy.cbz", "fresh.cbz"])
        self.assertIn(os.path.join("Hulk Collection", "S", "good.cbz"), ip.load_state(cfg)["files"])

    def test_converted_cbr_source_is_moved_using_new_cbz_path(self):
        cfg = make_cfg()
        source = old_file(cfg, "good.cbr")
        converted = source.with_suffix(".cbz")
        def fake(path, cfg_, key, exe="comictagger", dry_run=False, say=print, existing_ids=None):
            path.rename(converted)
            return {"file": str(converted), "status": "ready", "dest": os.path.join("Hulk Collection", "S", "good.cbz"),
                    "title": "T", "volume": "S", "number": "1", "issue": 5, "collection": "Hulk Collection",
                    "library": "Hulk Collection", "anchor": "1962-05", "lists": []}
        with mock.patch.object(ip, "existing_issue_ids", return_value=set()), mock.patch.object(ip, "prepare_file", fake):
            ip.run_prepare(cfg, "cv", say=lambda *a: None)
        self.assertFalse(source.exists())
        self.assertTrue((Path(cfg["ready"]) / "Hulk Collection" / "S" / "good.cbz").exists())

    def test_unpacks_only_comic_members_and_moves_package_to_done(self):
        cfg = make_cfg()
        import zipfile
        package = Path(cfg["incoming"]) / "pack.zip"
        with zipfile.ZipFile(package, "w") as zf:
            zf.writestr("pack/issue.cbz", b"comic")
            zf.writestr("pack/readme.txt", b"notes")
        n = ip.unpack_packages(cfg["incoming"], Path(cfg["incoming"]) / "_done", say=lambda *a: None)
        self.assertEqual(n, 1)
        self.assertEqual((Path(cfg["incoming"]) / "_packages" / "pack" / "pack" / "issue.cbz").read_bytes(), b"comic")
        self.assertFalse(package.exists())
        self.assertTrue((Path(cfg["incoming"]) / "_done" / "_packages" / "pack.zip").exists())

    def test_rejects_unsafe_package_paths(self):
        cfg = make_cfg()
        import zipfile
        package = Path(cfg["incoming"]) / "unsafe.zip"
        with zipfile.ZipFile(package, "w") as zf:
            zf.writestr("../escape.cbz", b"comic")
        ip.unpack_packages(cfg["incoming"], Path(cfg["incoming"]) / "_done", say=lambda *a: None)
        self.assertTrue(package.exists())
        self.assertFalse((Path(cfg["incoming"]).parent / "escape.cbz").exists())

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
                                                                    "anchor": "1962-05", "anchor_source": "comicvine.cover_date", "publisher": "Marvel",
                                                                    "lists": ["! Marvel Universe - Suggested Reading Order"], "uploaded_at": 1000.0}
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
        mu = self.komga.lists["MU"]["bookIds"]
        self.assertTrue({"b10", "b11"} <= set(mu))                                                   # and the new collection is merged into the publisher's master list
        self.assertEqual(Path(self.cfg["include_file"]).read_text().splitlines()[1:], ["Hulk Collection", "Marvel Universe"])
        self.assertEqual(ip.load_state(self.cfg)["touched_lists"], ["Hulk Collection", "Marvel Universe"])

    def test_a_book_with_no_recorded_publisher_goes_to_the_other_publishers_master_list(self):
        state = ip.load_state(self.cfg)
        for v in state["files"].values():
            v.pop("publisher")
        ip.save_state(self.cfg, state)
        self.go(1000.0 + ip.KOMGA_DELAY + 1)
        self.assertIn("! Other Publishers - Suggested Reading Order", self.komga.created)
        self.assertIn("Other Publishers", Path(self.cfg["include_file"]).read_text().splitlines())

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

    def test_missing_library_is_reconciled_by_exact_comicvine_id_and_explicit_list(self):
        state = ip.load_state(self.cfg)
        key = os.path.join("Hulk Collection", "S", "v01.cbz")
        v = state["files"][key]
        v.update({"issue": 55, "library": "Stale Library", "anchor": "1962-05", "anchor_source": "comicvine.cover_date"})
        state["files"][os.path.join("Hulk Collection", "S", "v02.cbz")].update({"komga_done": True, "lists_done": True})
        ip.save_state(self.cfg, state)
        self.komga.books["b10"]["metadata"]["links"] = [{"url": "https://comicvine.gamespot.com/issue/4000-55/"}]
        messages = []
        with mock.patch("time.sleep"):
            self.assertEqual(ip.run_komga(self.cfg, self.komga, now=1000.0 + ip.KOMGA_DELAY + 1,
                                          say=messages.append), 1, messages)
        repaired = ip.load_state(self.cfg)["files"][key]
        self.assertTrue(repaired["komga_done"] and repaired["lists_done"])
        self.assertEqual(repaired["duplicate_of"], "b10")
        self.assertEqual(repaired["matched_library"], "Hulk Collection")
        self.assertEqual(self.komga.books["b10"]["metadata"]["releaseDate"], "1962-05-01")
        self.assertIn("b10", self.komga.lists["MU"]["bookIds"])
        self.assertTrue(any("already integrated" in message for message in messages))
        self.assertFalse(any("HELD" in message for message in messages))

    def test_legacy_guessed_anchor_is_repaired_from_comicvine_before_list_insertion(self):
        state = ip.load_state(self.cfg)
        legacy = state["files"][os.path.join("Hulk Collection", "S", "v01.cbz")]
        legacy.update({"issue": 47836, "anchor": "1988-01", "anchor_guess": True})
        legacy.pop("anchor_source", None)
        finished = state["files"][os.path.join("Hulk Collection", "S", "v02.cbz")]
        finished.update({"komga_done": True, "lists_done": True})
        ip.save_state(self.cfg, state)
        with mock.patch.object(ip.tag_collected, "_get", return_value={"id": 47836, "issue_number": "5", "cover_date": "1992-09-01"}), \
                mock.patch("time.sleep"):
            self.assertEqual(ip.run_komga(self.cfg, self.komga, now=1000.0 + ip.KOMGA_DELAY + 1,
                                          say=lambda *a: None, cv_key="test-key"), 1)
        repaired = ip.load_state(self.cfg)["files"][os.path.join("Hulk Collection", "S", "v01.cbz")]
        self.assertEqual((repaired["anchor"], repaired["anchor_source"]), ("1992-09", "comicvine.cover_date"))
        self.assertEqual(repaired["anchor_previous_guess"], "1988-01")
        self.assertNotIn("anchor_guess", repaired)

    def test_temporary_date_lookup_failure_is_deferred_instead_of_held(self):
        state = ip.load_state(self.cfg)
        legacy = state["files"][os.path.join("Hulk Collection", "S", "v01.cbz")]
        legacy.update({"issue": 47836, "anchor": "1988-01", "anchor_guess": True})
        legacy.pop("anchor_source", None)
        finished = state["files"][os.path.join("Hulk Collection", "S", "v02.cbz")]
        finished.update({"komga_done": True, "lists_done": True})
        ip.save_state(self.cfg, state)
        messages = []
        with mock.patch.object(ip.tag_collected, "_get", side_effect=ip.urllib.error.URLError("offline")), \
                mock.patch("time.sleep"):
            self.assertEqual(ip.run_komga(self.cfg, self.komga, now=1000.0 + ip.KOMGA_DELAY + 1,
                                          say=messages.append, cv_key="test-key"), 0)
        saved = ip.load_state(self.cfg)["files"][os.path.join("Hulk Collection", "S", "v01.cbz")]
        self.assertGreater(saved["date_repair_retry_after"], 1000.0 + ip.KOMGA_DELAY + 1)
        self.assertTrue(any("DEFERRED date repair" in m for m in messages))
        self.assertFalse(any("HELD" in m for m in messages))


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


class LibraryRootTests(unittest.TestCase):
    CFG = {"library_roots": {"Thor Collection": "R:/Thor Collection"}}

    def test_a_collection_without_a_configured_root_gets_one_beside_the_others(self):
        self.assertEqual(Path(ip.library_root_for(self.CFG, "Silver Surfer Collection")), Path("R:/Silver Surfer Collection"))
        self.assertEqual(ip.library_root_for(self.CFG, "Thor Collection"), "R:/Thor Collection")
        cfg = {"library_roots": {"Spider Man Collection": "R:/The Amazing Spider-Man"}}
        self.assertEqual(ip.library_root_for(cfg, "Spider Man Collection"), "R:/The Amazing Spider-Man")      # an explicit root always wins
        self.assertIsNone(ip.library_root_for({}, "Anything"))

    def test_a_missing_library_is_provisioned_instead_of_silently_skipped(self):
        said = []
        got = ip.ensure_configured_libraries(self.CFG, None, ["The Punisher Collection", "Thor Collection"], {"Thor Collection": "id"},
                                             say=said.append, dry_run=True)
        self.assertEqual(got, {"The Punisher Collection"})
        self.assertTrue(any("would create Komga library 'The Punisher Collection'" in m for m in said))
        said.clear()
        self.assertEqual(ip.ensure_configured_libraries({}, None, ["X"], {}, say=said.append, dry_run=True), set())
        self.assertTrue(any("no root known" in m for m in said))


class AutoRouteTests(unittest.TestCase):
    def cfg(self, **kw):
        base = {"auto_create_routes": True, "publishers": {"Marvel": "Marvel Universe"}, "routes": [],
                "library_roots": {"Fantastic Four Collection": "R:/Fantastic Four Collection"}}
        base.update(kw)
        return base

    def test_franchise_names(self):
        self.assertEqual(ip.franchise_name("Captain America: Sam Wilson"), "Captain America")
        self.assertEqual(ip.franchise_name("All-New Captain America"), "Captain America")
        self.assertEqual(ip.franchise_name("The Mighty Thor"), "Thor")
        self.assertEqual(ip.franchise_name("Thor: God of Thunder"), "Thor")
        self.assertEqual(ip.franchise_name("Annual"), "")

    def test_creates_one_route_for_the_franchise_and_saves_it(self):
        d = Path(tempfile.mkdtemp()); path = d / "cfg.json"
        path.write_text(json.dumps({"routes": [], "library_roots": {}}), encoding="utf-8")
        cfg = self.cfg(_config_path=str(path))
        route = ip.auto_create_route(cfg, "Captain America: Sam Wilson", "Marvel", say=lambda *_: None)
        self.assertEqual(route["collection"], "Captain America Collection")
        self.assertEqual(ip.route_for(cfg, "All-New Captain America")[0]["collection"], "Captain America Collection")
        self.assertEqual(len(cfg["routes"]), 1)
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual([r["collection"] for r in saved["routes"]], ["Captain America Collection"])
        self.assertIn("Captain America Collection", saved["library_roots"])
        self.assertEqual(ip.auto_create_route(cfg, "Captain America", "Marvel", say=lambda *_: None), route)
        self.assertEqual(len(json.loads(path.read_text(encoding="utf-8"))["routes"]), 1)

    def test_needs_the_switch_and_a_known_publisher_and_dry_run_writes_nothing(self):
        say = lambda *_: None
        self.assertIsNone(ip.auto_create_route(self.cfg(auto_create_routes=False), "Thor", "Marvel", say=say))
        self.assertIsNone(ip.auto_create_route(self.cfg(), "Thor", "Image Comics", say=say))
        d = Path(tempfile.mkdtemp()); path = d / "cfg.json"
        path.write_text(json.dumps({"routes": []}), encoding="utf-8")
        ip.auto_create_route(self.cfg(_config_path=str(path)), "Thor", "Marvel", say=say, dry_run=True)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["routes"], [])


if __name__ == "__main__":
    unittest.main()
