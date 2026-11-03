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
        self.assertEqual(tc.parse_name("Thunderbolts Classic Vol. 03 (2012) (digital) (Minutemen-Slayer).cbr"), {'names': ['Thunderbolts Classic'], 'volume': 3, 'year': 2012})
        self.assertEqual(tc.parse_name("Hawkeye And The Thunderbolts v02.cbz"), {'names': ['Hawkeye And The Thunderbolts'], 'volume': 2, 'year': None})
        self.assertEqual(tc.parse_name("Thunderbolts - Worldstrike (2024).cbz"), {'names': ['Thunderbolts - Worldstrike'], 'volume': None, 'issue_num': None, 'year': 2024})

    def test_subtitle_is_tried_first(self):
        p = tc.parse_name("Thunderbolts Epic Collection v01 - Justice, Like Lightning (2023) (digital-SD) (Dakota Scanning).cbz")
        self.assertEqual(p["names"], ["Thunderbolts Epic Collection Justice, Like Lightning", "Thunderbolts Epic Collection"])
        self.assertEqual((p["volume"], p["year"]), (1, 2023))

    def test_words_that_merely_contain_a_v_are_not_volumes(self):
        self.assertIsNone(tc.parse_name("Venom 5 Revelations.cbz")["volume"])
        self.assertIsNone(tc.parse_name("Marvel Vault.cbz")["volume"])

    def test_bare_issue_numbers_are_removed_from_series_search_name(self):
        p = tc.parse_name("Amazing Spider-Man 001 (1963) (Digital) (Shadowcat-Empire).cbz")
        self.assertEqual((p["names"], p["issue_num"], p["year"]), (["Amazing Spider-Man"], "001", 1963))

    def test_pack_sequence_prefix_is_not_mistaken_for_issue_number(self):
        p = tc.parse_name("001 Shadowland 01 (of 05) (2010) (Digital).cbr")
        self.assertEqual((p["names"], p["issue_num"], p["year"]), (["Shadowland"], "01", 2010))

    def test_negative_issue_and_decimal_issue_numbers_are_preserved(self):
        negative = tc.parse_name("Amazing Spider-Man -001 (1997) (FB-DCP)(C2C).cbz")
        decimal = tc.parse_name("Amazing Spider-Man 700.1 (2014).cbz")
        self.assertEqual((negative["names"], negative["issue_num"]), (["Amazing Spider-Man"], "-001"))
        self.assertEqual((decimal["names"], decimal["issue_num"]), (["Amazing Spider-Man"], "700.1"))


class TagOutcomeTests(unittest.TestCase):
    """ComicTagger failures that pass (rate limit, network) must not be mistaken for a bad file."""

    def run_tag(self, stdout, stderr="", tagged=False, timeout=False):
        import subprocess
        from unittest import mock

        def fake_run(*a, **k):
            if timeout:
                raise subprocess.TimeoutExpired("ct", 1)
            return subprocess.CompletedProcess(a, 3, stdout, stderr)
        with mock.patch.object(tc.subprocess, "run", fake_run), mock.patch.object(tc, "already_tagged", lambda p: tagged):
            return tc.tag_outcome("comictagger", "x.cbz", 1)

    def test_saved_and_tagged_is_ok(self):
        self.assertEqual(self.run_tag("Save complete", tagged=True), "ok")

    def test_a_comic_vine_rate_limit_is_transient(self):
        self.assertEqual(self.run_tag("Network Data Fetch Failures", "Comic Vine rate limit error. Exceeded 3 retires."), "transient")
        self.assertEqual(self.run_tag("", timeout=True), "transient")

    def test_anything_else_is_a_real_failure(self):
        self.assertEqual(self.run_tag("could not write archive", "permission denied"), "failed")
        self.assertEqual(self.run_tag("Save complete", tagged=False), "failed")        # said it saved, but the tags are not in the file


class NowAndFirstIssueTests(unittest.TestCase):
    def test_marvel_now_suffix_is_part_of_the_issue_number(self):
        p = tc.parse_name("Thor God of Thunder 019.NOW (2014) (Digital) (Zone-Empire).cbz")
        self.assertEqual(p["names"], ["Thor God of Thunder"])
        self.assertEqual(int(p["issue_num"]), 19)

    def test_a_first_issue_may_belong_to_a_volume_that_starts_the_next_year(self):
        got37 = {"id": 7, "name": "Thor: God of Thunder", "start_year": "2013", "count_of_issues": 25}
        p = {"names": ["Thor God of Thunder"], "volume": None, "issue_num": "001", "year": 2012}
        self.assertEqual(tc.choose_volume(p, [got37])[0], got37)                       # on sale in 2012, cover-dated January 2013
        turkish = {"id": 8, "name": "Thor: God of Thunder", "start_year": None, "count_of_issues": 2, "publisher": {"name": "Marmara Cizgi"}}
        marvel = {**got37, "publisher": {"name": "Marvel"}}
        self.assertEqual(tc.choose_volume(p, [turkish, marvel])[0], marvel)            # a one-year-later Marvel volume beats an undated foreign one
        later = {**p, "issue_num": "007"}
        self.assertIsNone(tc.choose_volume(later, [got37])[0])                        # but issue 7 of a 2012 file is not that volume
        trade = {"names": ["Thor God of Thunder"], "volume": 1, "issue_num": None, "year": 2012}
        self.assertIsNone(tc.choose_volume(trade, [got37])[0])                        # and a trade volume gets no such slack


class ChooseTests(unittest.TestCase):
    def test_exact_name_ignoring_punctuation_and_ampersand(self):
        for name, vol in [("Hawkeye And The Thunderbolts", HAWKEYE), ("Thunderbolts - Worldstrike", WORLD),
                          ("Thunderbolts Epic Collection Justice, Like Lightning", EPIC)]:
            got, why = tc.choose_volume({"names": [name], "volume": 1, "issue_num": None, "year": None}, [TB97, vol])
            self.assertEqual(got, vol, why)

    def test_nearest_year_wins_among_same_named_volumes(self):
        got, _ = tc.choose_volume({"names": ["Thunderbolts"], "volume": 1, "issue_num": None, "year": 2007}, [TB97, TB06])
        self.assertEqual(got, TB06)

    def test_negative_flashback_issue_uses_the_older_run(self):
        old = {"id": 10, "name": "The Amazing Spider-Man", "start_year": "1963", "count_of_issues": "700"}
        modern = {"id": 11, "name": "The Amazing Spider-Man", "start_year": "1999", "count_of_issues": "58"}
        parsed = {"names": ["Amazing Spider-Man"], "volume": None, "issue_num": "-001", "year": 1997}
        self.assertEqual(tc.choose_volume(parsed, [old, modern])[0], old)

    def test_only_volume_with_that_name_is_accepted_whatever_the_year(self):
        got, why = tc.choose_volume({"names": ["Marvel Masterworks The Incredible Hulk"], "volume": 1, "issue_num": None, "year": 2009},
                                    [{"id": 35210, "name": "Marvel Masterworks: The Incredible Hulk", "start_year": "2003"}])
        self.assertEqual(got["id"], 35210, why)

    def test_leading_the_is_ignored_and_aliases_apply(self):
        self.assertEqual(tc.norm("The Immortal Hulk Omnibus"), tc.norm("Immortal Hulk Omnibus"))
        p = tc.parse_name("Incredible Hulk Masterworks v15.cbz", {"Incredible Hulk Masterworks": "Marvel Masterworks: The Incredible Hulk"})
        self.assertEqual(p["names"][0], "Marvel Masterworks: The Incredible Hulk")
        self.assertEqual(p["volume"], 15)

    def test_ambiguous_or_missing_is_left_alone(self):
        self.assertIsNone(tc.choose_volume({"names": ["Thunderbolts"], "volume": None, "issue_num": None, "year": None}, [TB97, TB06])[0])      # no year
        self.assertIsNone(tc.choose_volume({"names": ["Thunderbolts"], "volume": None, "issue_num": None, "year": 1980}, [TB97, TB06])[0])      # too far
        self.assertIsNone(tc.choose_volume({"names": ["Nothing Like It"], "volume": 1, "issue_num": None, "year": 2000}, [TB97])[0])

    def test_issue_by_volume_number_or_the_only_issue(self):
        issues = [{"id": 1, "issue_number": "1"}, {"id": 2, "issue_number": "2"}, {"id": 3, "issue_number": "3"}]
        self.assertEqual(tc.choose_issue({"volume": 3}, issues)[0]["id"], 3)
        self.assertIsNone(tc.choose_issue({"volume": 9}, issues)[0])
        self.assertIsNone(tc.choose_issue({"volume": None}, issues)[0])          # several issues, no number
        self.assertEqual(tc.choose_issue({"volume": None}, issues[:1])[0]["id"], 1)

    def test_issue_number_comparison_handles_zero_padding_and_decimals(self):
        issues = [{"id": 1, "issue_number": "1"}, {"id": 2, "issue_number": "700.1"}]
        self.assertEqual(tc.choose_issue({"issue_num": "001"}, issues)[0]["id"], 1)
        self.assertEqual(tc.choose_issue({"issue_num": "0700.1"}, issues)[0]["id"], 2)


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
                mock.patch.object(tc, "volume_issues", lambda k, v, n=None: self.issues[v]), \
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


class SingleIssueTradeTests(unittest.TestCase):
    def test_site_tag_outside_parentheses_is_dropped_from_the_title(self):
        names = tc.parse_name("Captain America - Sam Wilson Vol. 05 - End of the Line (2017) GetComics.INFO.cbz")["names"]
        self.assertEqual(names[0], "Captain America - Sam Wilson End of the Line")

    def test_named_volume_uses_the_only_issue_of_a_one_issue_comic_vine_volume(self):
        parsed = {"names": ["x"], "volume": 4, "year": 2017}
        issue, why = tc.choose_issue(parsed, [{"id": 7, "issue_number": "1"}])
        self.assertEqual(issue["id"], 7)
        issue, why = tc.choose_issue(parsed, [{"id": 7, "issue_number": "1"}, {"id": 8, "issue_number": "2"}])
        self.assertIsNone(issue)

    def test_one_issue_trade_dated_a_year_after_the_file_is_still_a_candidate(self):
        parsed = {"names": ["Captain America - Sam Wilson Civil War II"], "volume": 3, "year": 2016}
        trade = {"id": 1, "name": "Captain America: Sam Wilson - Civil War II", "start_year": "2017", "count_of_issues": 1}
        series = {"id": 2, "name": "Captain America: Sam Wilson", "start_year": "2015", "count_of_issues": 24}
        chosen, why = tc.choose_volume({**parsed, "names": parsed["names"] + ["Captain America - Sam Wilson"]}, [trade, series])
        self.assertEqual(chosen["id"], 1)


class HeldComicsFixTests(unittest.TestCase):
    def test_marvel_tie_in_labels_half_issues_and_year_named_annuals_are_parsed(self):
        p = tc.parse_name("Iron Man 020.INH (2014) (digital) (Minutemen-PhD).cbz")
        self.assertEqual((p["names"], p["issue_num"], p["year"]), (["Iron Man"], "020.INH", 2014))
        h = tc.parse_name("Iron Man 000.5 (1999) (Digital) (Shadowcat-Empire).cbz")
        self.assertEqual((h["names"], h["issue_num"]), (["Iron Man"], "000.5"))
        a = tc.parse_name("Iron Man Annual 2001 (BrentNW-HaCsA).cbz")
        self.assertEqual((a["names"], a["year"]), (["Iron Man Annual"], 2001))                                 # the year is the issue's date
        self.assertEqual(tc.parse_name("Iron Man Annual 02 (2015).cbz")["names"], ["Iron Man Annual"])                 # a numbered annual is unchanged

    def test_issue_numbers_compare_half_issues_and_labels(self):
        issues = [{"id": 1, "issue_number": "20"}, {"id": 2, "issue_number": "20.INH"}, {"id": 3, "issue_number": "\u00bd"}]
        self.assertEqual(tc.choose_issue({"issue_num": "020.INH"}, issues)[0]["id"], 2)
        self.assertEqual(tc.choose_issue({"issue_num": "020"}, issues)[0]["id"], 1)
        self.assertEqual(tc.choose_issue({"issue_num": "000.5"}, issues)[0]["id"], 3)
        self.assertEqual(tc.choose_issue({"issue_num": "0.5"}, [{"id": 4, "issue_number": "1/2"}])[0]["id"], 4)
        self.assertIsNone(tc.choose_issue({"issue_num": "021.INH"}, issues)[0])

    def run_match(self, name, searches, issues):
        d = Path(tempfile.mkdtemp())
        f = d / name
        f.write_bytes(b"PK")
        with mock.patch.object(tc, "search_volumes", lambda k, n: searches.get(n, [])), \
                mock.patch.object(tc, "volume_issues", lambda k, v, n=None: [i for i in issues[v] if n is None or tc._issue_key(i["issue_number"]) == tc._issue_key(n)]):
            return tc.match("cv", f, sleep=lambda s: None)

    def test_a_subtitled_run_is_found_under_its_parent_series_by_number_and_year(self):
        dos = {"id": 36926, "name": "Iron Man: Director of S.H.I.E.L.D.", "start_year": "2007", "count_of_issues": 1}
        im68 = {"id": 2407, "name": "Iron Man", "start_year": "1968", "count_of_issues": 336}
        im98 = {"id": 6504, "name": "Iron Man", "start_year": "1998", "count_of_issues": 90}
        im05 = {"id": 18220, "name": "Iron Man", "start_year": "2005", "count_of_issues": 35}
        searches = {"Iron Man - Director of S.H.I.E.L.D": [dos], "Iron Man": [im68, im98, im05]}
        issues = {36926: [{"id": 1, "issue_number": "1", "cover_date": "2007-12-01"}],
                  2407: [{"id": 100 + n, "issue_number": str(n), "cover_date": f"{1968 + n // 12}-01-01"} for n in range(1, 40)],
                  6504: [{"id": 200 + n, "issue_number": str(n), "cover_date": "1999-05-01"} for n in range(1, 40)],
                  18220: [{"id": 300 + n, "issue_number": str(n), "cover_date": "2007-06-01"} for n in range(1, 36)]}
        m = self.run_match("Iron Man - Director of S.H.I.E.L.D. 015 (2007) (digital-hd) (Kleio-Empire).cbz", searches, issues)
        self.assertEqual((m["status"], m["volume"]["id"], m["issue"]["id"]), ("match", 18220, 315))
        wrong_year = self.run_match("Iron Man - Director of S.H.I.E.L.D. 015 (2012) (digital).cbz", searches, issues)
        self.assertEqual(wrong_year["status"], "skip")                                  # no volume has an issue 15 from 2012: left alone

    def test_a_mini_series_file_with_no_number_is_its_collected_edition_when_comic_vine_has_one(self):
        series = {"id": 88934, "name": "International Iron Man", "start_year": "2016", "count_of_issues": 7}
        tpb = {"id": 95226, "name": "International Iron Man", "start_year": "2016", "count_of_issues": 1}
        issues = {88934: [{"id": 520000 + n, "issue_number": str(n), "name": ""} for n in range(1, 8)],
                  95226: [{"id": 555527, "issue_number": "1", "name": "Volume 1"}]}
        m = self.run_match("International Iron Man (2016) (Digital) (F) (Zone-Empire).cbz", {"International Iron Man": [series, tpb]}, issues)
        self.assertEqual((m["status"], m["volume"]["id"], m["issue"]["id"]), ("match", 95226, 555527))
        no_tpb = self.run_match("International Iron Man (2016) (Digital).cbz", {"International Iron Man": [series]}, issues)
        self.assertEqual(no_tpb["status"], "skip")

    def test_the_half_issue_and_the_year_named_annual_and_the_tie_in_reach_their_issues(self):
        im98 = {"id": 6504, "name": "Iron Man", "start_year": "1998", "count_of_issues": 90, "publisher": {"name": "Marvel"}}
        half = self.run_match("Iron Man 000.5 (1999) (Digital) (Shadowcat-Empire).cbz", {"Iron Man": [im98]}, {6504: [{"id": 171042, "issue_number": "\u00bd"}, {"id": 5, "issue_number": "1"}]})
        self.assertEqual((half["status"], half["issue"]["id"]), ("match", 171042))
        ann = {"id": 2905, "name": "Iron Man Annual", "start_year": "1970", "count_of_issues": 15, "publisher": {"name": "Marvel"}}
        got = self.run_match("Iron Man Annual 2001 (BrentNW-HaCsA).cbz", {"Iron Man Annual": [ann]},
                             {2905: [{"id": 8, "issue_number": "1", "cover_date": "1970-01-01"}, {"id": 9, "issue_number": "25", "cover_date": "2001-10-01"}]})
        self.assertEqual((got["status"], got["issue"]["id"]), ("match", 9))
        im12 = {"id": 53725, "name": "Iron Man", "start_year": "2012", "count_of_issues": 29, "publisher": {"name": "Marvel"}}
        inh = self.run_match("Iron Man 020.INH (2014) (digital) (Minutemen-PhD).cbz", {"Iron Man": [im12]},
                             {53725: [{"id": 20, "issue_number": "20"}, {"id": 2020, "issue_number": "20.INH"}]})
        self.assertEqual((inh["status"], inh["issue"]["id"]), ("match", 2020))


if __name__ == "__main__":
    unittest.main()
