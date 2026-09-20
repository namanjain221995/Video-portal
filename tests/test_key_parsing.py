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


class MixedParentDepartmentTests(unittest.TestCase):
    """Interview-Success holds host folders and category folders side by side.

    The bucket looks like this:

        Interview-Success/Agrima_Agarwal/2026/09/…     <- a HOST (a person)
        Interview-Success/Nitin_Singh/2026/09/…        <- a HOST
        Interview-Success/Internal-Interview/{Host}/…  <- a SUB-DEPARTMENT
        Interview-Success/Interview/{Host}/…           <- a SUB-DEPARTMENT

    Before this, every child was read as a host, so a recording inside
    Internal-Interview/ was filed with host='Internal-Interview', year set to the
    real host's name and candidate set to the month — the person who ran the
    session and the person who sat it both became unsearchable.
    """

    IS_SEGMENTS = "2026/09/Rohan_Mehta/Gartner/2026-09-15/Round-1/96355112813/MP4/a.mp4"

    def parse(self, key):
        record = s3_service._parse_key(key, 1234)
        self.assertIsNotNone(record, f"key was dropped by the parser: {key}")
        return record

    def test_a_person_folder_is_a_host_of_the_parent_department(self):
        record = self.parse("Interview-Success/Agrima_Agarwal/" + self.IS_SEGMENTS)
        self.assertEqual(record["department"], "Interview-Success")
        self.assertEqual(record["host"], "Agrima_Agarwal")
        self.assertEqual(record["year"], "2026")
        self.assertEqual(record["candidate"], "Rohan_Mehta")

    def test_a_category_folder_becomes_its_own_department(self):
        record = self.parse(
            "Interview-Success/Internal-Interview/Agrima_Agarwal/" + self.IS_SEGMENTS)
        self.assertEqual(record["department"], "Interview-Success/Internal-Interview")
        # The fields that used to be shifted by one — this is the actual bug.
        self.assertEqual(record["host"], "Agrima_Agarwal")
        self.assertEqual(record["year"], "2026")
        self.assertEqual(record["month"], "09")
        self.assertEqual(record["candidate"], "Rohan_Mehta")

    def test_a_category_folder_nobody_configured_is_still_recognised(self):
        """Structural, not a name list: a category folder added to the bucket
        later is classified without a config change or a redeploy."""
        record = self.parse(
            "Interview-Success/Some-New-Category/Nitin_Singh/" + self.IS_SEGMENTS)
        self.assertEqual(record["department"], "Interview-Success/Some-New-Category")
        self.assertEqual(record["host"], "Nitin_Singh")

    def test_it_needs_no_entry_in_DEPARTMENTS_to_work(self):
        """The property that decides whether this fix reaches production at all.

        DEPARTMENTS is set explicitly in the deployed .env, so anything that had
        to be listed there would need a server-side edit to take effect — and
        would silently do nothing until someone made it. Interview-Success is
        configured as a plain entry with no "/*", and the split still happens."""
        self.assertIn("Interview-Success", s3_service.DEPARTMENTS)
        self.assertNotIn("Interview-Success", s3_service.AUTO_PARENTS)
        self.assertEqual(
            s3_service._department_of(
                ["Interview-Success", "Internal-Interview", "Nitin_Singh", "2026", "09"]),
            ("Interview-Success/Internal-Interview", 2))

    def test_stacked_category_folders_do_not_mis_file_the_host(self):
        record = self.parse(
            "Interview-Success/Training/Advanced/Nitin_Singh/" + self.IS_SEGMENTS)
        self.assertEqual(record["department"], "Interview-Success/Training/Advanced")
        self.assertEqual(record["host"], "Nitin_Singh")
        self.assertEqual(record["year"], "2026")

    def test_the_two_kinds_of_child_are_told_apart_by_what_is_inside_them(self):
        """A host folder holds {Year}; a sub-department holds another host."""
        self.assertTrue(s3_service._looks_like_host(
            ["Interview-Success", "Agrima_Agarwal", "2026", "09"], 1))
        self.assertFalse(s3_service._looks_like_host(
            ["Interview-Success", "Internal-Interview", "Agrima_Agarwal", "2026"], 1))
        # A year-shaped folder is only a year in the plausible range.
        self.assertFalse(s3_service._looks_like_host(
            ["Interview-Success", "Somebody", "1899", "09"], 1))
        # Nothing below it at all -> cannot be a host.
        self.assertFalse(s3_service._looks_like_host(["Interview-Success", "X"], 1))

    def test_training_children_are_unaffected(self):
        """Training/* has no host folders directly under it, so the mixed rule
        must not disturb it."""
        record = self.parse(
            "Training/Resume-Based/Vivek_Parmar/2026/April/Khushali_Prasad"
            "/2026-04-01/Time-11-00-AM-IST/8898177914/M4A/a.m4a")
        self.assertEqual(record["department"], "Training/Resume-Based")
        self.assertEqual(record["host"], "Vivek_Parmar")

    def test_a_flat_department_is_unaffected(self):
        record = self.parse(
            "HR/Priya_Nair/2026/09/Rohan_Mehta/2026-09-15/Time-10-00-IST/963/MP4/a.mp4")
        self.assertEqual(record["department"], "HR")
        self.assertEqual(record["host"], "Priya_Nair")

    def test_a_key_outside_every_department_is_still_dropped(self):
        self.assertIsNone(s3_service._parse_key(
            "Random-Folder/Someone/2026/09/c/2026-09-15/T/1/MP4/a.mp4", 1))

    def test_the_parent_is_still_listed_once_for_scanning(self):
        """Listing "Interview-Success" and "Interview-Success/*" together must not
        make the indexer walk those objects twice."""
        prefixes = s3_service._scan_prefixes()
        self.assertEqual(prefixes.count("Interview-Success"), 1)
        for name in prefixes:
            self.assertFalse(name.startswith("Interview-Success/"),
                             f"{name!r} is inside a prefix already being scanned")


if __name__ == "__main__":
    unittest.main()
