"""Key-parsing checks, including the nested Training/* sub-departments.

_parse_key is the single point where an S3 key becomes searchable metadata, so a
regression here is invisible: it does not raise, it just files the recording under
the wrong host/candidate. These tests pin each real bucket layout to its expected
record. No AWS access and no DEMO_MODE dependency — _parse_key is pure.
"""

import os
import unittest

os.environ.setdefault("DEMO_MODE", "true")

import s3_service  # noqa: E402  (DEMO_MODE must be set before import)


class KeyParsingTests(unittest.TestCase):
    def assert_fields(self, key, **expected):
        record = s3_service._parse_key(key, 1234)
        self.assertIsNotNone(record, f"key was dropped by the parser: {key}")
        for field, value in expected.items():
            self.assertEqual(record[field], value, f"field {field!r} for {key}")
        return record

    # ── Nested Training sub-departments (Training/{Sub}/{Trainer}/…) ──────────
    def test_training_subdepartment_is_the_department_not_the_host(self):
        """The regression this file exists for: before nested departments were
        supported, seg[0] was the department and everything below shifted by one
        — host became 'Resume-Based', candidate became the month, and the real
        candidate was reported as the company."""
        self.assert_fields(
            "Training/Resume-Based/Vivek_Parmar/2026/April/Khushali_Prasad"
            "/2026-04-01/Time-11-00-AM-IST/8898177914/M4A/a1ac24ce-7bad.m4a",
            department="Training/Resume-Based",
            host="Vivek_Parmar",
            year="2026",
            month="April",
            candidate="Khushali_Prasad",
            company="",            # layout C has no Company folder
            round="",              # ...and no Round folder
            date="2026-04-01",
            meeting_id="8898177914",
            file_type="M4A",
            category="audio",
        )

    def test_training_advanced_group_roster_splits_into_attendees(self):
        record = self.assert_fields(
            "Training/Advanced/Rahul_Verma/2026/April"
            "/700758300_Nandini_K-Ram_Reddy-Syed_Faraaz"
            "/2026-04-03/Time-6-30-PM-IST/700758300/MP4/rec.mp4",
            department="Training/Advanced",
            host="Rahul_Verma",
            candidate="700758300_Nandini_K-Ram_Reddy-Syed_Faraaz",
            meeting_id="700758300",
            category="video",
        )
        self.assertEqual(record["candidates"], ["Nandini_K", "Ram_Reddy", "Syed_Faraaz"])

    def test_training_subdepartments_are_auto_discovered_not_configured(self):
        """The shipped config lists Training/* rather than the four children, so a
        FIFTH sub-department created in S3 later is indexed with no config change.
        A bare "Training" would swallow them all into one department."""
        self.assertIn("Training", s3_service.AUTO_PARENTS)
        self.assertNotIn("Training", s3_service.DEPARTMENTS)
        for sub in ("Resume-Based", "Advanced", "Interview-Readiness", "Other"):
            self.assertNotIn(f"Training/{sub}", s3_service.DEPARTMENTS)

    def test_a_brand_new_subdepartment_needs_no_configuration(self):
        self.assert_fields(
            "Training/Placement-Prep/New_Trainer/2026/May/Some_Trainee"
            "/2026-05-04/Time-9-00-AM-IST/8898100001/MP4/rec.mp4",
            department="Training/Placement-Prep",
            host="New_Trainer",
            candidate="Some_Trainee",
            meeting_id="8898100001",
        )

    def test_a_stray_file_directly_under_the_wildcard_parent_is_dropped(self):
        """Auto-discovery must not turn a loose object sitting in Training/ into a
        department named after the file itself."""
        self.assertIsNone(s3_service._parse_key("Training/notes.txt", 1))

    # ── The three pre-existing layouts must be unaffected ─────────────────────
    def test_layout_a_interview_success_keeps_company_and_round(self):
        self.assert_fields(
            "Interview-Success/Vivek_Parmar/2026/June/Akhilendra_NA_Sirikonda"
            "/Gartner/2026-06-10/Introduction_Call/96355112813/MP4/rec.mp4",
            department="Interview-Success",
            host="Vivek_Parmar",
            candidate="Akhilendra_NA_Sirikonda",
            company="Gartner",
            round="Introduction_Call",
            meeting_id="96355112813",
        )

    def test_layout_b_extra_meeting_id_and_time_folder(self):
        self.assert_fields(
            "Interview-Success/Vivek_Parmar/2026/June/Aditya_Walker/96355119001"
            "/Amazon/2026-06-12/Technical_Round_1/Time-3-00-PM-IST/MP4/rec.mp4",
            department="Interview-Success",
            candidate="Aditya_Walker",
            company="Amazon",
            round="Technical_Round_1",
            meeting_id="96355119001",
        )

    def test_layout_c_flat_department_has_no_company_or_round(self):
        self.assert_fields(
            "HR/Abhishek_Jain/2026/June/Sanjana_Gupta"
            "/2026-06-15/Time-4-00-PM-IST/96355120099/M4A/audio.m4a",
            department="HR",
            host="Abhishek_Jain",
            candidate="Sanjana_Gupta",
            company="",
            round="",
            meeting_id="96355120099",
        )

    # ── Department resolution rules ───────────────────────────────────────────
    def test_longest_configured_department_wins_and_parent_scans_once(self):
        """A parent and its child may both be configured: keys resolve to the
        most specific match, and only the parent prefix is listed from S3 so no
        object is indexed twice."""
        original = s3_service.DEPARTMENTS
        original_parents = s3_service.AUTO_PARENTS
        try:
            # Deliberately overlapping: an explicit parent, an explicit child, AND
            # a wildcard on the same parent must still list "Training" exactly once.
            s3_service.DEPARTMENTS = ["Training", "Training/Advanced"]
            s3_service.AUTO_PARENTS = ["Training"]
            s3_service._DEPT_SET = set(s3_service.DEPARTMENTS)
            s3_service._DEPT_MAX_DEPTH = 2

            self.assertEqual(s3_service._scan_prefixes(), ["Training"])
            self.assertEqual(
                s3_service._parse_key(
                    "Training/Advanced/R_V/2026/April/C/2026-04-03"
                    "/Time-6-IST/700/MP4/x.mp4", 1)["department"],
                "Training/Advanced",
            )
            self.assertEqual(
                s3_service._parse_key(
                    "Training/Legacy_Host/2026/April/C/2026-04-03"
                    "/Time-6-IST/700/MP4/x.mp4", 1)["department"],
                "Training",
            )
        finally:
            s3_service.DEPARTMENTS = original
            s3_service.AUTO_PARENTS = original_parents
            s3_service._DEPT_SET = set(original)
            s3_service._DEPT_MAX_DEPTH = max(
                (len(d.split("/")) for d in original), default=1)

    def test_keys_outside_every_department_and_short_keys_are_dropped(self):
        self.assertIsNone(s3_service._parse_key(
            "Finance/H/2026/April/C/2026-04-01/Time-1-IST/9/MP4/x.mp4", 1))
        self.assertIsNone(s3_service._parse_key("HR/too/short/key.mp4", 1))
        self.assertIsNone(s3_service._parse_key(
            "HR/Host/2026/June/Cand/2026-06-15/Time-4-IST/963/M4A/", 1))

    def test_scan_prefixes_never_double_lists_the_shipped_configuration(self):
        prefixes = s3_service._scan_prefixes()
        for a in prefixes:
            for b in prefixes:
                if a is not b:
                    self.assertFalse(
                        a.startswith(b + "/"),
                        f"{a!r} would be listed twice: it sits inside {b!r}",
                    )


if __name__ == "__main__":
    unittest.main()
