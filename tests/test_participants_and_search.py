"""Attendee rosters (participants.json) and candidate-name matching.

Group sessions carry no names in the S3 path — the folder is literally "Group" and
the roster lives in a participants.json beside the media folders. These tests cover
reading that file, attaching it to the right meeting, and the name matching that
has to cope with real-world spellings (id prefixes, honorifics, reordered names,
underscores vs hyphens vs spaces).
"""

import os
import unittest
from unittest import mock

os.environ.setdefault("DEMO_MODE", "true")

import s3_service  # noqa: E402  (DEMO_MODE must be set before import)


MEETING = ("Training/Advanced/Sneha_Chaudhary/2026/July/Group"
           "/2026-07-09/Time-1-27-AM-IST/97609808470/")
ROSTER_KEY = MEETING + "participants.json"
MEDIA_KEYS = [MEETING + "MP4/adbc738d-efe4-4fc8-8993-76bb38751025.mp4",
              MEETING + "M4A/audio.m4a",
              MEETING + "TRANSCRIPT/transcript.vtt",
              MEETING + "CHAT/chat.txt"]

# The exact payload shape the Zoom automation writes.
ROSTER_PAYLOAD = {
    "meeting_id": "97609808470",
    "topic": "Advance Training ",
    "department": "Advanced-Training",     # deliberately NOT the path department
    "host": "Someone_Else",                # deliberately NOT the path host
    "host_email": "",
    "start_time": "2026-07-01T19:25:00Z",
    "candidate_count": 8,
    "candidates": [
        {"name": "563-bhanu_Varshini", "email": ""},
        {"name": "Khaja_Faizan", "email": ""},
        {"name": "Mani", "email": ""},
        {"name": "Mohammed_Farhan_Wajid", "email": ""},
        {"name": "Mohammed_Obaid_Ahmed", "email": ""},
        {"name": "Pavithran_Gnanasekaran", "email": ""},
        {"name": "Ruthura_Meedimale", "email": ""},
        {"name": "Vidya_Nomula", "email": ""},
    ],
}
ROSTER = [c["name"] for c in ROSTER_PAYLOAD["candidates"]]


class RosterParsingTests(unittest.TestCase):
    def test_real_payload_yields_every_attendee_in_order(self):
        self.assertEqual(s3_service._roster_names(ROSTER_PAYLOAD), ROSTER)

    def test_roster_and_media_keys_resolve_to_the_same_meeting(self):
        self.assertEqual(s3_service._meeting_prefix(ROSTER_KEY), MEETING)
        for key in MEDIA_KEYS:
            self.assertEqual(s3_service._meeting_prefix(key), MEETING)

    def test_malformed_rosters_degrade_to_no_names(self):
        for payload in (None, [], "nope", {}, {"candidates": "nope"},
                        {"candidates": [None, 5, {}, {"name": ""}, {"name": "12345"}]}):
            self.assertEqual(s3_service._roster_names(payload), [],
                             f"payload {payload!r} should yield no names")

    def test_plain_string_entries_and_duplicates_are_handled(self):
        names = s3_service._roster_names({"candidates": [
            "Naman_Jain", {"name": "naman jain"}, {"name": "Naman-Jain"},
            "Priya_Nair", {"name": " "},
        ]})
        # All three spellings of one person collapse to a single entry.
        self.assertEqual(names, ["Naman_Jain", "Priya_Nair"])

    def test_read_roster_never_raises(self):
        """A roster we cannot read must not abort the whole bucket scan."""
        with mock.patch.object(s3_service, "_client", side_effect=RuntimeError("boom")):
            prefix, names = s3_service._read_roster(ROSTER_KEY)
        self.assertEqual(prefix, MEETING)
        self.assertEqual(names, [])


class RosterAttachmentTests(unittest.TestCase):
    def _records(self):
        return [s3_service._parse_key(k, 1000) for k in MEDIA_KEYS]

    def test_every_file_of_the_meeting_gets_the_roster(self):
        records = self._records()
        for record in records:
            self.assertEqual(record["candidates"], ["Group"])   # path has no names

        with mock.patch.object(s3_service, "_read_roster",
                              return_value=(MEETING, ROSTER)):
            resolved = s3_service._attach_rosters(records, [ROSTER_KEY])

        self.assertEqual(resolved, 1)
        for record in records:
            self.assertEqual(record["candidates"], ROSTER)

    def test_roster_never_overrides_department_or_host(self):
        """The roster JSON carries its own 'department' and 'host'. Access control
        reads the KEY, so those fields must be ignored entirely."""
        records = self._records()
        with mock.patch.object(s3_service, "_read_roster",
                              return_value=(MEETING, ROSTER)):
            s3_service._attach_rosters(records, [ROSTER_KEY])
        for record in records:
            self.assertEqual(record["department"], "Training/Advanced")
            self.assertEqual(record["host"], "Sneha_Chaudhary")
            self.assertNotEqual(record["department"], ROSTER_PAYLOAD["department"])
            self.assertNotEqual(record["host"], ROSTER_PAYLOAD["host"])

    def test_a_meeting_without_a_roster_keeps_its_path_derived_names(self):
        records = self._records()
        with mock.patch.object(s3_service, "_read_roster",
                              return_value=("some/other/meeting/", ROSTER)):
            s3_service._attach_rosters(records, [ROSTER_KEY])
        for record in records:
            self.assertEqual(record["candidates"], ["Group"])

    def test_participants_json_is_not_itself_a_searchable_record(self):
        self.assertIsNone(s3_service._parse_key(ROSTER_KEY, 616))


class NameMatchingTests(unittest.TestCase):
    GROUP = {"candidate": "Group", "candidates": ROSTER}

    def assert_hits(self, record, queries):
        for query in queries:
            self.assertTrue(
                s3_service._match_candidate(s3_service._cand_tokens(query), record),
                f"{query!r} should have matched",
            )

    def assert_misses(self, record, queries):
        for query in queries:
            self.assertFalse(
                s3_service._match_candidate(s3_service._cand_tokens(query), record),
                f"{query!r} should NOT have matched",
            )

    def test_separator_and_case_variants_all_match(self):
        self.assert_hits(self.GROUP, [
            "Khaja_Faizan", "khaja-faizan", "khaja faizan", "KHAJA FAIZAN",
            "khajafaizan", "Khaja.Faizan",
        ])

    def test_word_order_does_not_matter(self):
        self.assert_hits(self.GROUP, [
            "faizan khaja", "varshini bhanu", "nomula vidya",
            "wajid mohammed", "gnanasekaran pavithran",
        ])

    def test_partial_names_match(self):
        self.assert_hits(self.GROUP, ["bhanu", "obaid", "meedimale", "mani", "faizan"])

    def test_honorifics_are_ignored_on_either_side(self):
        self.assert_hits(self.GROUP, [
            "vidya sir", "Ruthura Meedimale ma'am", "mr khaja faizan", "mani madam",
        ])
        self.assert_hits(
            {"candidate": "Group", "candidates": ["Naman_Sir", "Priya_Nair"]},
            ["naman", "naman sir"],
        )

    def test_id_prefix_is_searchable_and_ignorable(self):
        """A roster entry like "563-bhanu_Varshini" must be findable by the pasted
        entry, by the name alone, and by the id alone."""
        self.assert_hits(self.GROUP, ["563-bhanu_Varshini", "bhanu varshini", "563"])

    def test_tokens_must_all_land_inside_one_person(self):
        """The property that makes group search trustworthy: a query naming two
        different attendees must not match."""
        self.assert_misses(self.GROUP, [
            "mohammed nomula", "khaja varshini", "bhanu faizan", "mani gnanasekaran",
        ])

    def test_absent_people_do_not_match(self):
        self.assert_misses(self.GROUP, ["naman jain", "sirikonda", "zzz"])

    def test_single_person_records_keep_working(self):
        record = s3_service._parse_key(
            "Interview-Success/V_P/2026/June/152026_Akhilendra_NA_Sirikonda"
            "/Gartner/2026-06-10/Intro/963/MP4/x.mp4", 1)
        self.assert_hits(record, ["sirikonda", "akhilendra sirikonda",
                                  "sirikonda akhilendra", "152026"])
        self.assert_misses(record, ["nandini"])

    def test_empty_or_separator_only_query_is_not_a_filter(self):
        for query in ("", "   ", "-", "_", "-_-"):
            self.assertEqual(s3_service._cand_tokens(query), [],
                             f"{query!r} must produce no tokens")

    def test_matched_candidates_names_the_right_person(self):
        rows, total, _ = s3_service.search(candidate="obaid")
        self.assertGreater(total, 0)
        for row in rows:
            self.assertEqual(row["matched_candidates"], ["Mohammed_Obaid_Ahmed"])

    def test_searching_a_participant_does_not_mutate_the_cached_record(self):
        """search() shallow-copies rows to add matched_candidates; the shared index
        must be left alone or every later search sees stale annotations."""
        rows, _, _ = s3_service.search(candidate="bhanu")
        self.assertTrue(rows)
        for source in s3_service.DEMO_RECORDS:
            self.assertNotIn("matched_candidates", source)


if __name__ == "__main__":
    unittest.main()
