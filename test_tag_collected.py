"""Tests for tag_collected.py (name parsing, volume and issue choice, the tagging flow). Run: python test_tag_collected.py"""
import os, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tag_collected as tc

CLASSIC = {"id": 40490, "name": "Thunderbolts Classic", "start_year": "2011"}
HAWKEYE = {"id": 89784, "name": "Hawkeye & The Thunderbolts", "start_year": "2016"}
EPIC = {"id": 152562, "name": "Thunderbolts Epic Collection: Justice, Like Lightning", "start_year": "2023"}
WORLD = {"id": 158669, "name": "Thunderbolts: Worldstrike", "start_year": "2024"}
TB97, TB06 = {"id": 6020, "name": "Thunderbolts", "start_year": "1997"}, {"id": 18128, "name": "Thunderbolts", "start_year": "2006"}


class ParseTests(unittest.TestCase):
    def test_volume_year_and_names(self):
        self.assertEqual(tc.parse_name("Thunderbolts Classic Vol. 03 (2012) (digital) (Minutemen-Slayer).cbr"),
                         {"names": ["Thunderbolts Classic"], "volume": 3, "year": 2012})
        self.assertEqual(tc.parse_name("Hawkeye And The Thunderbolts v02.cbz"),
                         {"names": ["Hawkeye And The Thunderbolts"], "volume": 2, "year": None})
        self.assertEqual(tc.parse_name("Thunderbolts - Worldstrike (2024).cbz"),
                         {"names": ["Thunderbolts - Worldstrike"], "volume": None, "year": 2024})

    def test_subtitle_is_tried_first(self):
        p = tc.parse_name("Thunderbolts Epic Collection v01 - Justice, Like Lightning (2023) (digital-SD) (Dakota Scanning).cbz")
        self.assertEqual(p["names"], ["Thunderbolts Epic Collection Justice, Like Lightning", "Thunderbolts Epic Collection"])
        self.assertEqual((p["volume"], p["year"]), (1, 2023))

    def test_words_that_merely_contain_a_v_are_not_volumes(self):
        self.assertIsNone(tc.parse_name("Venom 5 Revelations.cbz")["volume"])
        self.assertIsNone(tc.parse_name("Marvel Vault.cbz")["volume"])


class ChooseTests(unittest.TestCase):
    def test_exact_name_ignoring_punctuation_and_ampersand(self):
        for name, vol in [("Hawkeye And The Thunderbolts", HAWKEYE), ("Thunderbolts - Worldstrike", WORLD),
                          ("Thunderbolts Epic Collection Justice, Like Lightning", EPIC)]:
            got, why = tc.choose_volume({"names": [name], "volume": 1, "year": None}, [TB97, vol])
            self.assertEqual(got, vol, why)

    def test_nearest_year_wins_among_same_named_volumes(self):
        got, _ = tc.choose_volume({"names": ["Thunderbolts"], "volume": 1, "year": 2007}, [TB97, TB06])
        self.assertEqual(got, TB06)

    def test_only_volume_with_that_name_is_accepted_whatever_the_year(self):
        got, why = tc.choose_volume({"names": ["Marvel Masterworks The Incredible Hulk"], "volume": 1, "year": 2009},
                                    [{"id": 35210, "name": "Marvel Masterworks: The Incredible Hulk", "start_year": "2003"}])
        self.assertEqual(got["id"], 35210, why)

    def test_leading_the_is_ignored_and_aliases_apply(self):
        self.assertEqual(tc.norm("The Immortal Hulk Omnibus"), tc.norm("Immortal Hulk Omnibus"))
        p = tc.parse_name("Incredible Hulk Masterworks v15.cbz", {"Incredible Hulk Masterworks": "Marvel Masterworks: The Incredible Hulk"})
        self.assertEqual(p["names"][0], "Marvel Masterworks: The Incredible Hulk")
        self.assertEqual(p["volume"], 15)

    def test_ambiguous_or_missing_is_left_alone(self):
        self.assertIsNone(tc.choose_volume({"names": ["Thunderbolts"], "volume": None, "year": None}, [TB97, TB06])[0])      # no year
        self.assertIsNone(tc.choose_volume({"names": ["Thunderbolts"], "volume": None, "year": 1980}, [TB97, TB06])[0])      # too far
        self.assertIsNone(tc.choose_volume({"names": ["Nothing Like It"], "volume": 1, "year": 2000}, [TB97])[0])

    def test_issue_by_volume_number_or_the_only_issue(self):
        issues = [{"id": 1, "issue_number": "1"}, {"id": 2, "issue_number": "2"}, {"id": 3, "issue_number": "3"}]
        self.assertEqual(tc.choose_issue({"volume": 3}, issues)[0]["id"], 3)
        self.assertIsNone(tc.choose_issue({"volume": 9}, issues)[0])
        self.assertIsNone(tc.choose_issue({"volume": None}, issues)[0])          # several issues, no number
        self.assertEqual(tc.choose_issue({"volume": None}, issues[:1])[0]["id"], 1)


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        for n in ["Thunderbolts Classic Vol. 03 (2012) (digital).cbz", "Thunderbolts - Worldstrike (2024).cbz", "Unknown Thing v01.cbz", "Already Done v01.cbz"]:
            (self.dir / n).write_bytes(b"PK")
        self.searches = {"Thunderbolts Classic": [CLASSIC], "Thunderbolts - Worldstrike": [TB97, WORLD], "Unknown Thing": []}
        self.issues = {40490: [{"id": 272868, "issue_number": "1"}, {"id": 317032, "issue_number": "2"}, {"id": 351477, "issue_number": "3"}],
                       158669: [{"id": 1061646, "issue_number": "1"}]}
        self.tagged_ids = []

    def go(self, apply):
        said = []
        done = lambda p: p.name == "Already Done v01.cbz"
        def fake_tag(exe, path, issue_id, timeout=300):
            self.tagged_ids.append(issue_id)
            return True
        with mock.patch.object(tc, "search_volumes", lambda k, n: self.searches.get(n, [])), \
                mock.patch.object(tc, "volume_issues", lambda k, v: self.issues[v]), \
                mock.patch.object(tc, "already_tagged", done), mock.patch.object(tc, "tag", fake_tag):
            n = tc.run([self.dir], "cv", apply=apply, say=said.append, sleep=lambda s: None)
        return n, said

    def test_preview_tags_nothing(self):
        n, said = self.go(False)
        self.assertEqual((n, self.tagged_ids), (0, []))
        self.assertTrue(any("would tag: Thunderbolts Classic Vol. 03" in s and "351477" in s for s in said))

    def test_apply_tags_the_matches_only(self):
        n, said = self.go(True)
        self.assertEqual(sorted(self.tagged_ids), [351477, 1061646])
        self.assertEqual(n, 2)
        self.assertTrue(any("left alone: Unknown Thing" in s for s in said))
        self.assertFalse(any("Already Done" in s for s in said))


class RunnerHookTests(unittest.TestCase):
    def args(self):
        from types import SimpleNamespace
        return SimpleNamespace(max=0, state_dir=Path(tempfile.mkdtemp()))

    def test_runs_on_the_configured_folders_under_the_library_root(self):
        import comictagger_runner as runner
        seen = {}
        def fake_run(dirs, key, exe, apply=False, max_files=20, log=None, say=print):
            seen.update(dirs=[str(d) for d in dirs], key=key, apply=apply, max=max_files)
            return 3
        cfg = {"library_root": "/lib", "collected_folders": ["TPB", "Omnibus"]}
        with mock.patch.object(tc, "run", fake_run), mock.patch.object(runner.komga_title_fix, "comicvine_key", lambda *a: "cv"):
            n = runner.tag_collected_folders(cfg, self.args(), apply=True)
        self.assertEqual(n, 3)
        self.assertEqual(seen["dirs"], [str(Path("/lib") / "TPB"), str(Path("/lib") / "Omnibus")])
        self.assertTrue(seen["apply"])

    def test_skips_without_folders_or_key(self):
        import comictagger_runner as runner
        said = []
        with mock.patch.object(runner.komga_title_fix, "comicvine_key", lambda *a: "cv"):
            self.assertEqual(runner.tag_collected_folders({"library_root": "/lib"}, self.args(), apply=True, say=said.append), 0)
        self.assertIn("skipped", said[0])


if __name__ == "__main__":
    unittest.main()
