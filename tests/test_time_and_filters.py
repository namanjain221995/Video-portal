"""Meeting start times, the date-range filter, multi-select file types and the
caption-sibling lookup.

The Time-*-IST folder was previously only recognised well enough to keep it OUT of
the company/round fields; now it is the source of the Time column and of every
timezone conversion in the browser, so its parsing is pinned here. A silently
mis-read time is invisible in the UI — it just shows the wrong hour.
"""

import os
import unittest

os.environ.setdefault("DEMO_MODE", "true")

import s3_service  # noqa: E402  (DEMO_MODE must be set before import)


class TimeFolderParsingTests(unittest.TestCase):
    def test_every_shape_the_bucket_actually_writes(self):
        cases = {
            "Time-8-30-PM-IST": ("20:30", "IST"),
            "Time-11-00-AM-IST": ("11:00", "IST"),
            "Time-1-27-AM-IST": ("01:27", "IST"),
            "Time-4-00-PM-IST": ("16:00", "IST"),
            "Time-6-IST": ("06:00", "IST"),          # hour only, no meridiem
            "time-9-05-am-ist": ("09:05", "IST"),    # lower case
        }
        for folder, expected in cases.items():
            self.assertEqual(s3_service._parse_time_folder(folder), expected, folder)

    def test_midnight_and_noon_cross_over_correctly(self):
        """The classic 12-hour clock trap: 12 AM is 00:xx and 12 PM is 12:xx."""
        self.assertEqual(s3_service._parse_time_folder("Time-12-00-AM-IST")[0], "00:00")
        self.assertEqual(s3_service._parse_time_folder("Time-12-30-PM-IST")[0], "12:30")

    def test_unreadable_folders_yield_no_time_rather_than_a_wrong_one(self):
        for folder in ("", "MP4", "Introduction_Call", "Time--IST", "Time-25-00-IST",
                       "Time-13-00-PM-IST", "Time-9-99-AM-IST", "Time-abc-IST"):
            self.assertEqual(s3_service._parse_time_folder(folder), ("", ""), folder)


class RecordTimeTests(unittest.TestCase):
    def test_layout_c_record_carries_the_meeting_time(self):
        record = s3_service._parse_key(
            "HR/Abhishek_Jain/2026/June/Sanjana_Gupta"
            "/2026-06-15/Time-4-00-PM-IST/96355120099/M4A/audio.m4a", 1)
        self.assertEqual(record["time"], "16:00")
        self.assertEqual(record["time_zone"], "IST")

    def test_layout_b_time_folder_sits_after_the_round(self):
        record = s3_service._parse_key(
            "Interview-Success/Vivek_Parmar/2026/June/Aditya_Walker/96355119001"
            "/Amazon/2026-06-12/Technical_Round_1/Time-3-00-PM-IST/MP4/rec.mp4", 1)
        self.assertEqual(record["time"], "15:00")
        self.assertEqual(record["round"], "Technical_Round_1")   # still not the time

    def test_layout_a_has_no_time_folder_so_time_stays_empty(self):
        """Interview-Success' 10-segment layout carries no clock reading at all.
        An empty Time cell is correct; a fabricated 00:00 would convert into the
        wrong DAY for any timezone behind IST."""
        record = s3_service._parse_key(
            "Interview-Success/Vivek_Parmar/2026/June/Akhilendra_NA_Sirikonda"
            "/Gartner/2026-06-10/Introduction_Call/96355112813/MP4/rec.mp4", 1)
        self.assertEqual(record["time"], "")
        self.assertEqual(record["time_zone"], "")

    def test_a_pre_upgrade_indexed_record_backfills_its_time(self):
        """A disk index written before times existed must not show a blank column."""
        old = {
            "department": "HR", "host": "H", "year": "2026", "month": "June",
            "candidate": "C", "company": "", "date": "2026-06-15", "round": "",
            "file_type": "M4A", "category": "audio", "filename": "a.m4a", "ext": "m4a",
            "key": "HR/H/2026/June/C/2026-06-15/Time-4-00-PM-IST/963/M4A/a.m4a",
            "size": 1,
        }
        self.assertEqual(s3_service._intern_rec(old)["time"], "16:00")


class DateRangeSearchTests(unittest.TestCase):
    def dates(self, **kwargs):
        rows, _, _ = s3_service.search(limit=1000, **kwargs)
        return sorted({r["date"] for r in rows})

    def test_a_range_returns_only_the_days_inside_it(self):
        found = self.dates(date_from="2026-06-12", date_to="2026-06-18")
        self.assertTrue(found)
        for date in found:
            self.assertGreaterEqual(date, "2026-06-12")
            self.assertLessEqual(date, "2026-06-18")

    def test_both_bounds_on_one_day_is_a_single_day_filter(self):
        self.assertEqual(self.dates(date_from="2026-06-15", date_to="2026-06-15"),
                         ["2026-06-15"])

    def test_each_bound_alone_is_open_ended(self):
        for date in self.dates(date_from="2026-06-20"):
            self.assertGreaterEqual(date, "2026-06-20")
        for date in self.dates(date_to="2026-04-03"):
            self.assertLessEqual(date, "2026-04-03")

    def test_a_reversed_range_is_read_as_the_range_the_user_meant(self):
        self.assertEqual(self.dates(date_from="2026-06-18", date_to="2026-06-12"),
                         self.dates(date_from="2026-06-12", date_to="2026-06-18"))

    def test_the_range_narrows_rather_than_replaces_the_free_text_date(self):
        rows, _, _ = s3_service.search(date="2026-06", date_from="2026-06-15",
                                       date_to="2026-06-30", limit=1000)
        self.assertTrue(rows)
        for row in rows:
            self.assertTrue(row["date"].startswith("2026-06"))
            self.assertGreaterEqual(row["date"], "2026-06-15")

    def test_a_partial_date_is_not_a_bound_and_is_ignored(self):
        """'2026-06' belongs in the free-text filter; as a bound it would silently
        compare as a string and drop every June record."""
        self.assertEqual(s3_service._clean_iso_date("2026-06"), "")
        self.assertEqual(s3_service._clean_iso_date("  2026-06-15 "), "2026-06-15")
        self.assertEqual(s3_service.search(date_from="2026-06", limit=1000), ([], 0, 0))

    def test_a_range_alone_counts_as_a_real_query(self):
        _, total, _ = s3_service.search(date_from="2026-06-01", date_to="2026-06-30")
        self.assertGreater(total, 0)


class FileTypeMultiSelectTests(unittest.TestCase):
    def categories(self, file_type):
        rows, _, _ = s3_service.search(file_type=file_type, date="2026", limit=1000)
        return sorted({r["category"] for r in rows})

    def test_a_single_category_still_behaves_exactly_as_before(self):
        self.assertEqual(self.categories("video"), ["video"])

    def test_several_categories_are_matched_in_one_search(self):
        self.assertEqual(self.categories("video,audio"), ["audio", "video"])
        self.assertEqual(self.categories(["video", "transcript"]), ["transcript", "video"])

    def test_blank_and_padded_values_are_tolerated(self):
        self.assertEqual(self.categories(" video , , audio "), ["audio", "video"])
        self.assertEqual(s3_service._category_filter(""), [])
        self.assertEqual(s3_service._category_filter(",, "), [])

    def test_an_unknown_category_matches_nothing_instead_of_everything(self):
        """Dropping an unrecognised value would turn a typo into "no filter" and
        return the caller's whole allowed corpus."""
        self.assertEqual(s3_service.search(file_type="bogus", limit=1000), ([], 0, 0))
        self.assertEqual(self.categories("bogus,video"), ["video"])

    def test_a_type_selection_alone_counts_as_a_real_query(self):
        _, total, _ = s3_service.search(file_type="video,audio")
        self.assertGreater(total, 0)


class SortingTests(unittest.TestCase):
    def test_date_sort_breaks_ties_on_the_meeting_time(self):
        rows, _, _ = s3_service.search(date="2026", sort="date_asc", limit=1000)
        stamps = [(r["date"], r["time"]) for r in rows]
        self.assertEqual(stamps, sorted(stamps))


class CaptionLookupTests(unittest.TestCase):
    GROUP = ("Training/Advanced/Sneha_Chaudhary/2026/June/Group"
             "/2026-07-09/Time-1-27-AM-IST/97609808470/")

    def test_a_video_finds_the_transcript_of_its_own_meeting(self):
        video = self.GROUP + "MP4/adbc738d-efe4-4fc8-8993-76bb38751025.mp4"
        captions = s3_service.caption_records(video)
        self.assertEqual([r["key"] for r in captions],
                         [self.GROUP + "TRANSCRIPT/transcript_97609808470.vtt"])

    def test_only_subtitle_files_are_offered_as_captions(self):
        video = self.GROUP + "MP4/adbc738d-efe4-4fc8-8993-76bb38751025.mp4"
        for record in s3_service.caption_records(video):
            self.assertEqual(record["ext"], "vtt")

    def test_a_meeting_with_no_transcript_offers_none(self):
        video = ("Interview-Success/Abhishek_Jain/2026/June/Chaitanya_Nenavath"
                 "/Google/2026-06-15/HR_Round/96355120044/MP4/rec_96355120044.mp4")
        self.assertEqual(s3_service.caption_records(video), [])

    def test_a_caption_never_reaches_outside_its_own_meeting(self):
        """Every candidate track must live under the same meeting prefix — that is
        what makes them the same department, and therefore the same access grant."""
        video = self.GROUP + "MP4/adbc738d-efe4-4fc8-8993-76bb38751025.mp4"
        for record in s3_service.caption_records(video):
            self.assertTrue(record["key"].startswith(self.GROUP))

    def test_an_unknown_key_yields_no_captions(self):
        self.assertEqual(s3_service.caption_records(""), [])
        self.assertEqual(s3_service.caption_records("nope.mp4"), [])


if __name__ == "__main__":
    unittest.main()
