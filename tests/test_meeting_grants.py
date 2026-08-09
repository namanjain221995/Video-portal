"""Sharing ONE meeting with a user, independently of their department grant.

This is access-control code, so the tests are written around what must NOT
happen: a meeting grant must reach exactly that meeting and nothing else, must
not widen the department grant it sits beside, and its view/download choice must
hold even for an account that downloads freely elsewhere.
"""

import os
import tempfile
import unittest
from unittest import mock

_TEMP_DIR = tempfile.TemporaryDirectory()
_USERS_PATH = os.path.join(_TEMP_DIR.name, "meeting-grant-users.json")

# setdefault only: other modules in the same run configure these process-wide
# before importing app.py, and overwriting them would break their logins. The
# users file is redirected per-test instead of globally, for the same reason.
os.environ.setdefault("DEMO_MODE", "true")
os.environ.setdefault("SECRET_KEY", "meeting-grant-secret-not-used-outside-tests")

import app as portal    # noqa: E402
import auth            # noqa: E402
import s3_service      # noqa: E402

# Two demo meetings in DIFFERENT departments, so "reaches across departments"
# is actually being tested rather than assumed.
GROUP_MEETING = "97609808470"        # Training/Advanced, 4 files
QMS_MEETING = "96355125555"          # QMS, 2 files
HR_MEETING = "96355120099"           # HR, 3 files


def files_for(meeting_id):
    return [r for r in s3_service.DEMO_RECORDS if r["meeting_id"] == meeting_id]


class MeetingVisibilityTests(unittest.TestCase):
    """s3_service.search with a meeting grant and no department access."""

    def search(self, **kwargs):
        rows, total, _ = s3_service.search(limit=1000, **kwargs)
        return rows, total

    def test_a_shared_meeting_is_reachable_with_no_department_at_all(self):
        rows, total = self.search(allowed_departments=[],
                                  allowed_meetings={GROUP_MEETING: False},
                                  meeting_id=GROUP_MEETING)
        self.assertEqual(total, len(files_for(GROUP_MEETING)))
        self.assertTrue(total > 0)
        for row in rows:
            self.assertEqual(row["meeting_id"], GROUP_MEETING)
            self.assertEqual(row["department"], "Training/Advanced")

    def test_every_file_of_the_meeting_comes_with_it(self):
        """Sharing a meeting means the recording AND its transcript/chat/audio."""
        rows, _ = self.search(allowed_departments=[],
                              allowed_meetings={GROUP_MEETING: False},
                              meeting_id=GROUP_MEETING)
        self.assertEqual({r["category"] for r in rows},
                         {"video", "audio", "transcript", "chat"})

    def test_the_grant_reaches_that_meeting_and_nothing_else(self):
        """The property that matters: no sibling recording leaks in with it."""
        rows, _ = self.search(allowed_departments=[],
                              allowed_meetings={GROUP_MEETING: False},
                              date="2026")
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(row["meeting_id"], GROUP_MEETING)

    def test_a_meeting_grant_does_not_widen_the_department_grant(self):
        """Granting a Training/Advanced meeting must not hand over the rest of
        Training/Advanced — the department has other meetings in the demo set."""
        rows, _ = self.search(allowed_departments=[],
                              allowed_meetings={GROUP_MEETING: False},
                              department="Training/Advanced", date="2026")
        other = [r for r in s3_service.DEMO_RECORDS
                 if r["department"] == "Training/Advanced"
                 and r["meeting_id"] != GROUP_MEETING]
        self.assertTrue(other, "fixture must contain another meeting in that department")
        self.assertEqual({r["meeting_id"] for r in rows}, {GROUP_MEETING})

    def test_department_and_meeting_grants_add_up(self):
        rows, _ = self.search(allowed_departments=["QMS"],
                              allowed_meetings={GROUP_MEETING: False},
                              date="2026")
        self.assertEqual({r["department"] for r in rows},
                         {"QMS", "Training/Advanced"})

    def test_a_host_restriction_still_binds_the_department_grant(self):
        """A meeting grant must not become a hole in the host mask: the extra
        records it adds are its own, not the blocked host's other meetings."""
        rows, _ = self.search(allowed_departments=["Training/Advanced"],
                              allowed_hosts={"Training/Advanced": ["Nobody"]},
                              allowed_meetings={GROUP_MEETING: False},
                              date="2026")
        self.assertEqual({r["meeting_id"] for r in rows}, {GROUP_MEETING})

    def test_no_grants_means_no_records(self):
        rows, total = self.search(allowed_departments=[], allowed_meetings={},
                                  date="2026")
        self.assertEqual((rows, total), ([], 0))

    def test_an_unknown_meeting_id_grants_nothing(self):
        rows, total = self.search(allowed_departments=[],
                                  allowed_meetings={"00000000000": True}, date="2026")
        self.assertEqual((rows, total), ([], 0))


class AuthorizedRecordTests(unittest.TestCase):
    """The gate every download/view/caption route runs through."""

    def test_a_shared_meetings_file_is_authorized_without_its_department(self):
        key = files_for(GROUP_MEETING)[0]["key"]
        self.assertIsNotNone(
            s3_service.authorized_record(key, [], None, {GROUP_MEETING: False}))

    def test_a_file_outside_the_grant_stays_forbidden(self):
        key = files_for(QMS_MEETING)[0]["key"]
        self.assertIsNone(
            s3_service.authorized_record(key, [], None, {GROUP_MEETING: False}))

    def test_no_meeting_grants_behaves_exactly_as_before(self):
        key = files_for(QMS_MEETING)[0]["key"]
        self.assertIsNotNone(s3_service.authorized_record(key, ["QMS"]))
        self.assertIsNone(s3_service.authorized_record(key, ["HR"]))
        self.assertIsNone(s3_service.authorized_record(key, []))


class PerRecordDownloadTests(unittest.TestCase):
    """Download is decided per record once meetings can be shared view-only."""

    def rec(self, meeting_id):
        return files_for(meeting_id)[0]

    def test_a_view_only_share_cannot_be_downloaded(self):
        access = {"departments": [], "hosts": {}, "can_download": True,
                  "meetings": {GROUP_MEETING: False}}
        self.assertFalse(s3_service.record_downloadable(self.rec(GROUP_MEETING), access))

    def test_a_download_share_can_be_downloaded_by_a_view_only_account(self):
        """The inverse case: the account is view-only everywhere, but this one
        meeting was deliberately shared WITH download."""
        access = {"departments": ["QMS"], "hosts": {}, "can_download": False,
                  "meetings": {GROUP_MEETING: True}}
        self.assertTrue(s3_service.record_downloadable(self.rec(GROUP_MEETING), access))
        self.assertFalse(s3_service.record_downloadable(self.rec(QMS_MEETING), access))

    def test_the_more_permissive_route_wins(self):
        """Reachable through BOTH a downloadable department and a view-only
        share: refusing the download would take away access they already had."""
        access = {"departments": ["Training/Advanced"], "hosts": {}, "can_download": True,
                  "meetings": {GROUP_MEETING: False}}
        self.assertTrue(s3_service.record_downloadable(self.rec(GROUP_MEETING), access))

    def test_department_download_still_governs_department_files(self):
        allowed = {"departments": ["QMS"], "hosts": {}, "can_download": True, "meetings": {}}
        denied = {"departments": ["QMS"], "hosts": {}, "can_download": False, "meetings": {}}
        self.assertTrue(s3_service.record_downloadable(self.rec(QMS_MEETING), allowed))
        self.assertFalse(s3_service.record_downloadable(self.rec(QMS_MEETING), denied))


class StoredGrantTests(unittest.TestCase):
    """How the grant survives a round-trip through users.json."""

    def setUp(self):
        # Patched, not assigned: mutating auth.USERS_FILE for the whole process
        # would silently redirect every OTHER test module's user storage too.
        patcher = mock.patch.object(auth, "USERS_FILE", _USERS_PATH)
        patcher.start()
        self.addCleanup(patcher.stop)
        if os.path.exists(_USERS_PATH):
            os.unlink(_USERS_PATH)

    def test_a_grant_round_trips_with_its_download_flag(self):
        auth.create_user("sharee", "pw", departments=["QMS"], can_download=False,
                         meetings=[{"meeting_id": GROUP_MEETING, "can_download": True},
                                   {"meeting_id": HR_MEETING, "can_download": False}])
        access = auth.user_access("sharee")
        self.assertEqual(access["meetings"],
                         {GROUP_MEETING: True, HR_MEETING: False})

    def test_a_bare_id_is_accepted_and_defaults_to_view_only(self):
        auth.create_user("bare", "pw", departments=[], meetings=[QMS_MEETING])
        self.assertEqual(auth.user_access("bare")["meetings"], {QMS_MEETING: False})

    def test_duplicate_ids_collapse_to_one_grant(self):
        auth.create_user("dupe", "pw", departments=[], meetings=[
            {"meeting_id": QMS_MEETING, "can_download": True},
            {"meeting_id": QMS_MEETING, "can_download": True},
        ])
        self.assertEqual(len(auth.list_users()[0]["meetings"]), 1)

    def test_an_account_with_no_meetings_field_is_unaffected(self):
        """Every pre-existing user record predates this feature."""
        auth.create_user("legacy", "pw", departments=["HR"])
        self.assertEqual(auth.user_access("legacy")["meetings"], {})

    def test_removing_a_department_does_not_revoke_a_shared_meeting(self):
        """A shared meeting stands on its own — pruning it against the department
        list would silently undo the very thing it was created for."""
        auth.create_user("keeps", "pw", departments=["Training/Advanced"],
                         meetings=[{"meeting_id": GROUP_MEETING, "can_download": True}])
        auth.update_user_access("keeps", departments=[])
        access = auth.user_access("keeps")
        self.assertEqual(access["departments"], [])
        self.assertEqual(access["meetings"], {GROUP_MEETING: True})

    def test_meetings_are_only_replaced_when_supplied(self):
        auth.create_user("stable", "pw", departments=["HR"],
                         meetings=[{"meeting_id": HR_MEETING, "can_download": False}])
        auth.update_user_access("stable", can_download=True)          # untouched
        self.assertEqual(auth.user_access("stable")["meetings"], {HR_MEETING: False})
        auth.update_user_access("stable", meetings=[])                # explicitly cleared
        self.assertEqual(auth.user_access("stable")["meetings"], {})


class MeetingLookupTests(unittest.TestCase):
    """The admin picker's search — it spans every department by design."""

    def test_an_id_search_finds_the_meeting_with_useful_context(self):
        found = s3_service.meeting_summaries(GROUP_MEETING)
        self.assertEqual(len(found), 1)
        meeting = found[0]
        self.assertEqual(meeting["meeting_id"], GROUP_MEETING)
        self.assertEqual(meeting["files"], len(files_for(GROUP_MEETING)))
        self.assertEqual(meeting["departments"], ["Training/Advanced"])
        self.assertIn("Khaja_Faizan", meeting["candidates"])
        self.assertEqual(meeting["dates"], ["2026-07-09"])
        self.assertEqual(meeting["occurrences"], 1)

    def test_a_candidate_search_finds_their_meeting(self):
        found = s3_service.meeting_summaries("obaid")
        self.assertEqual([m["meeting_id"] for m in found], [GROUP_MEETING])

    def test_the_picker_is_not_scoped_to_one_department(self):
        ids = {m["meeting_id"] for m in s3_service.meeting_summaries("2026")}
        depts = {d for m in s3_service.meeting_summaries("2026") for d in m["departments"]}
        self.assertGreater(len(ids), 1)
        self.assertGreater(len(depts), 1)

    def test_an_empty_query_returns_nothing_rather_than_everything(self):
        self.assertEqual(s3_service.meeting_summaries(""), [])
        self.assertEqual(s3_service.meeting_summaries("   "), [])

    def test_details_resolve_an_already_granted_id(self):
        details = s3_service.meeting_details([GROUP_MEETING, "00000000000"])
        self.assertIn(GROUP_MEETING, details)
        self.assertNotIn("00000000000", details)      # unknown id simply absent
        self.assertEqual(details[GROUP_MEETING]["files"], len(files_for(GROUP_MEETING)))

    def test_details_of_nothing_is_cheap_and_empty(self):
        self.assertEqual(s3_service.meeting_details([]), {})
        self.assertEqual(s3_service.meeting_details(None), {})


class RouteEnforcementTests(unittest.TestCase):
    """The HTTP surface — what a signed-in user can actually pull down.

    The access mask is stubbed at auth.user_access so these test the ROUTES
    (which the browser talks to) rather than the storage format, which
    StoredGrantTests already covers.
    """

    def client_with(self, departments=(), can_download=False, meetings=None,
                    hosts=None):
        client = portal.app.test_client()
        with client.session_transaction() as session:
            session["user"] = "sharee"
            session["role"] = "user"       # NOT admin: the mask must be enforced
        patcher = mock.patch.object(auth, "user_access", return_value={
            "departments": list(departments), "hosts": dict(hosts or {}),
            "meetings": dict(meetings or {}), "can_download": can_download,
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        return client

    def test_search_returns_the_shared_meeting_and_marks_it_view_only(self):
        client = self.client_with(departments=[], meetings={GROUP_MEETING: False})
        data = client.get("/api/search?meeting_id=" + GROUP_MEETING).get_json()
        self.assertEqual(data["total"], len(files_for(GROUP_MEETING)))
        for row in data["results"]:
            self.assertFalse(row["can_download"])
        self.assertFalse(data["can_download"])

    def test_a_download_share_is_offered_to_a_view_only_account(self):
        client = self.client_with(departments=[], can_download=False,
                                  meetings={GROUP_MEETING: True})
        data = client.get("/api/search?meeting_id=" + GROUP_MEETING).get_json()
        self.assertTrue(data["can_download"])            # download UI is offered…
        for row in data["results"]:
            self.assertTrue(row["can_download"])         # …and this row earns it

    def test_viewing_a_shared_meeting_works_without_its_department(self):
        client = self.client_with(departments=[], meetings={GROUP_MEETING: False})
        key = files_for(GROUP_MEETING)[0]["key"]
        self.assertEqual(client.get("/api/view?key=" + key).status_code, 200)

    def test_downloading_a_view_only_share_is_refused(self):
        client = self.client_with(departments=[], can_download=True,
                                  meetings={GROUP_MEETING: False})
        key = files_for(GROUP_MEETING)[0]["key"]
        self.assertEqual(client.get("/api/download?key=" + key).status_code, 403)

    def test_downloading_a_shared_meeting_granted_download_succeeds(self):
        client = self.client_with(departments=[], can_download=False,
                                  meetings={GROUP_MEETING: True})
        key = files_for(GROUP_MEETING)[0]["key"]
        self.assertEqual(client.get("/api/download?key=" + key).status_code, 200)

    def test_a_file_outside_every_grant_is_a_404_on_every_route(self):
        client = self.client_with(departments=[], can_download=True,
                                  meetings={GROUP_MEETING: True})
        key = files_for(QMS_MEETING)[0]["key"]
        for path in ("/api/view?key=", "/api/download?key=", "/api/captions?key="):
            self.assertEqual(client.get(path + key).status_code, 404, path)

    def test_a_zip_mixing_view_only_files_is_refused_not_silently_trimmed(self):
        """A zip quietly missing what the user selected is worse than a refusal."""
        client = self.client_with(departments=["QMS"], can_download=True,
                                  meetings={GROUP_MEETING: False})
        keys = [files_for(QMS_MEETING)[0]["key"], files_for(GROUP_MEETING)[0]["key"]]
        resp = client.post("/api/download/bulk", json={"keys": keys})
        self.assertEqual(resp.status_code, 403)
        self.assertIn("view-only", resp.get_json()["error"])

    def test_a_zip_of_only_downloadable_shares_is_built(self):
        client = self.client_with(departments=[], can_download=False,
                                  meetings={GROUP_MEETING: True})
        keys = [r["key"] for r in files_for(GROUP_MEETING)]
        resp = client.post("/api/download/bulk", json={"keys": keys})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "application/zip")
        resp.close()      # release the temp zip so its cleanup hook can run

    def test_a_zip_cannot_smuggle_in_a_forbidden_key(self):
        client = self.client_with(departments=[], can_download=False,
                                  meetings={GROUP_MEETING: True})
        resp = client.post("/api/download/bulk",
                           json={"keys": [files_for(QMS_MEETING)[0]["key"]]})
        self.assertEqual(resp.status_code, 400)          # nothing authorized -> nothing to zip

    def test_the_admin_meeting_picker_is_closed_to_normal_users(self):
        client = self.client_with(departments=["QMS"], can_download=True)
        self.assertEqual(client.get("/api/admin/meetings?q=2026").status_code, 403)
        self.assertEqual(client.get("/api/admin/users").status_code, 403)

    def test_host_restriction_is_not_bypassed_by_a_meeting_grant(self):
        client = self.client_with(departments=["Training/Advanced"],
                                  hosts={"Training/Advanced": ["Nobody"]},
                                  meetings={GROUP_MEETING: False})
        data = client.get("/api/search?date=2026").get_json()
        self.assertEqual({r["meeting_id"] for r in data["results"]}, {GROUP_MEETING})


class AdminMeetingApiTests(unittest.TestCase):
    def setUp(self):
        self.client = portal.app.test_client()
        with self.client.session_transaction() as session:
            session["user"] = "meeting-grant-admin"
            session["role"] = "admin"

    def test_an_admin_can_look_up_a_meeting_to_share(self):
        data = self.client.get("/api/admin/meetings?q=" + GROUP_MEETING).get_json()
        self.assertEqual(len(data["meetings"]), 1)
        self.assertEqual(data["meetings"][0]["meeting_id"], GROUP_MEETING)
        self.assertEqual(data["meetings"][0]["departments"], ["Training/Advanced"])

    def test_an_empty_query_returns_nothing(self):
        self.assertEqual(self.client.get("/api/admin/meetings?q=").get_json()["meetings"], [])

    def test_non_numeric_and_oversized_ids_are_rejected_before_storage(self):
        """A meeting folder in S3 is always all-digit, so nothing else can match —
        accepting it would just put junk in users.json."""
        cleaned = portal._clean_meetings([
            {"meeting_id": GROUP_MEETING, "can_download": True},
            {"meeting_id": "not-a-number"},
            {"meeting_id": "9" * 64},
            {"meeting_id": ""},
            "12345",                              # bare string form
            {"meeting_id": GROUP_MEETING},        # duplicate: last wins
            12345,                                # not a string or dict
        ])
        self.assertEqual(cleaned, [
            {"meeting_id": GROUP_MEETING, "can_download": False},
            {"meeting_id": "12345", "can_download": False},
        ])

    def test_an_omitted_meetings_key_leaves_the_grant_alone(self):
        self.assertIsNone(portal._clean_meetings(None))
        self.assertEqual(portal._clean_meetings("nonsense"), [])


if __name__ == "__main__":
    unittest.main()
