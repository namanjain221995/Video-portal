"""End-to-end audit-log checks using demo recordings and an isolated database."""

import json
import os
import tempfile
import unittest
from unittest import mock


_TEMP_DIR = tempfile.TemporaryDirectory()
_ADMIN_USER = "audit-test-admin"
_ADMIN_PASSWORD = "StrongTestPassword-123"

# Set every stateful/runtime value before importing app.py. load_dotenv() does not
# override existing environment variables, so these tests never touch real AWS or
# the developer's users/audit files.
os.environ.update({
    "DEMO_MODE": "true",
    "SECRET_KEY": "audit-test-secret-that-is-not-used-outside-tests",
    "ADMIN_USERS": f"{_ADMIN_USER}:{_ADMIN_PASSWORD}",
    "USERS_FILE": os.path.join(_TEMP_DIR.name, "users.json"),
    "AUDIT_DB": os.path.join(_TEMP_DIR.name, "audit.db"),
    "AUDIT_RETENTION_DAYS": "0",
})

import app as portal  # noqa: E402  (environment must be configured first)
import audit_service  # noqa: E402
import s3_service  # noqa: E402


class AuditLogIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        portal.app.config.update(TESTING=True)

    @classmethod
    def tearDownClass(cls):
        _TEMP_DIR.cleanup()

    def setUp(self):
        connection = audit_service._connect()
        try:
            connection.execute("DELETE FROM audit_events")
            connection.commit()
        finally:
            connection.close()

    def _login_admin(self, client):
        response = client.post("/api/login", json={
            "username": _ADMIN_USER,
            "password": _ADMIN_PASSWORD,
        })
        self.assertEqual(response.status_code, 200)

    def test_login_search_and_preview_are_recorded_with_server_metadata(self):
        with portal.app.test_client() as client:
            self._login_admin(client)
            search = client.get("/api/search", query_string={
                "candidate": "Akhilendra",
                "page": 1,
                "per_page": 5,
            })
            self.assertEqual(search.status_code, 200)
            result = search.get_json()["results"][0]

            preview = client.get("/api/view", query_string={
                "key": result["key"],
                # These are deliberately ignored; metadata must come from the index.
                "host": "forged-host",
                "meeting_id": "forged-meeting",
            })
            self.assertEqual(preview.status_code, 200)

            logs_response = client.get("/api/admin/logs")
            self.assertEqual(logs_response.status_code, 200)
            self.assertEqual(logs_response.headers.get("Cache-Control"), "no-store")
            events = logs_response.get_json()["events"]

            filtered = client.get("/api/admin/logs", query_string={
                "action": "view",
                "q": result["meeting_id"],
            })
            self.assertEqual(filtered.status_code, 200)
            self.assertEqual(filtered.get_json()["total"], 1)

        actions = {event["action"] for event in events}
        self.assertTrue({"login", "search", "view"}.issubset(actions))

        view_event = next(event for event in events if event["action"] == "view")
        self.assertEqual(view_event["username"], _ADMIN_USER)
        self.assertEqual(view_event["host"], result["host"])
        self.assertEqual(view_event["meeting_id"], result["meeting_id"])
        self.assertEqual(view_event["recording_date"], result["date"])
        self.assertNotEqual(view_event["host"], "forged-host")
        self.assertTrue(view_event["occurred_at"].endswith("Z"))

        search_event = next(event for event in events if event["action"] == "search")
        self.assertEqual(search_event["candidate"], "Akhilendra")
        self.assertGreater(search_event["details"]["total_results"], 0)
        self.assertNotIn(_ADMIN_PASSWORD, json.dumps(events))

    def test_failed_login_does_not_write_untrusted_public_input(self):
        attempted_password = "never-store-this-password"
        with portal.app.test_client() as client:
            response = client.post("/api/login", json={
                "username": "not-a-real-user",
                "password": attempted_password,
            })
        self.assertEqual(response.status_code, 401)

        events = audit_service.list_events()["events"]
        self.assertEqual(events, [])
        self.assertNotIn(attempted_password, json.dumps(events))

    def test_logs_are_admin_only_and_invalid_dates_return_400(self):
        with portal.app.test_client() as client:
            self.assertEqual(client.get("/api/admin/logs").status_code, 401)

            with client.session_transaction() as current_session:
                current_session["user"] = "ordinary-user"
                current_session["role"] = "user"
            self.assertEqual(client.get("/api/admin/logs").status_code, 403)

            with client.session_transaction() as current_session:
                current_session["user"] = _ADMIN_USER
                current_session["role"] = "admin"
            bad_date = client.get("/api/admin/logs", query_string={
                "date_from": "not-a-date",
            })
            self.assertEqual(bad_date.status_code, 400)

            page = client.get("/logs")
            self.assertEqual(page.status_code, 200)
            self.assertEqual(page.headers.get("Cache-Control"), "no-store")

    def test_clear_all_logs_is_admin_only_and_self_audited(self):
        with portal.app.test_client() as client:
            # Anonymous and non-admin callers may not clear the trail.
            self.assertEqual(client.delete("/api/admin/logs").status_code, 401)
            with client.session_transaction() as current_session:
                current_session["user"] = "ordinary-user"
                current_session["role"] = "user"
            self.assertEqual(client.delete("/api/admin/logs").status_code, 403)

        # Seed a handful of events, then clear them as an admin.
        for number in range(6):
            self.assertTrue(audit_service.record_event("search", username=f"user-{number}"))
        self.assertEqual(audit_service.list_events()["total"], 6)

        with portal.app.test_client() as client:
            self._login_admin(client)  # adds a 'login' event -> at least 7 rows
            seeded_total = client.get("/api/admin/logs").get_json()["total"]
            self.assertGreaterEqual(seeded_total, 7)

            response = client.delete("/api/admin/logs")
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["deleted"], seeded_total)

            after = client.get("/api/admin/logs").get_json()

        # The clear wipes everything, then records exactly one self-audit entry.
        self.assertEqual(after["total"], 1)
        cleared = after["events"][0]
        self.assertEqual(cleared["action"], "logs_cleared")
        self.assertEqual(cleared["username"], _ADMIN_USER)
        self.assertEqual(cleared["role"], "admin")
        self.assertEqual(cleared["details"]["deleted_events"], seeded_total)

    def test_delete_single_log_entry_is_admin_only_and_leaves_no_marker(self):
        for number in range(4):
            self.assertTrue(audit_service.record_event("search", username=f"user-{number}"))
        target_id = audit_service.list_events()["events"][0]["id"]

        with portal.app.test_client() as client:
            # Anonymous / non-admin cannot delete a single entry.
            self.assertEqual(client.delete(f"/api/admin/logs/{target_id}").status_code, 401)
            with client.session_transaction() as current_session:
                current_session["user"] = "ordinary-user"
                current_session["role"] = "user"
            self.assertEqual(client.delete(f"/api/admin/logs/{target_id}").status_code, 403)

            self._login_admin(client)
            before = client.get("/api/admin/logs").get_json()["total"]

            # A non-integer path never matches the route; a missing id is a 404.
            self.assertEqual(client.delete("/api/admin/logs/not-a-number").status_code, 404)
            self.assertEqual(client.delete("/api/admin/logs/99999999").status_code, 404)

            self.assertEqual(client.delete(f"/api/admin/logs/{target_id}").status_code, 200)
            self.assertEqual(client.delete(f"/api/admin/logs/{target_id}").status_code, 404)

            after = client.get("/api/admin/logs").get_json()

        # Exactly one row removed, and NO self-audit marker was written.
        self.assertEqual(after["total"], before - 1)
        remaining_ids = {event["id"] for event in after["events"]}
        self.assertNotIn(target_id, remaining_ids)
        self.assertFalse(any(
            event["action"] in ("log_deleted", "logs_cleared") for event in after["events"]
        ))

    def test_screen_capture_signals_are_logged_for_users_and_skipped_for_admins(self):
        with portal.app.test_client() as client:
            # Unauthenticated callers cannot report capture signals.
            self.assertEqual(
                client.post("/api/log/capture", json={"kind": "screenshot"}).status_code, 401
            )

        # A normal user's PrintScreen signal is recorded with the SERVER identity,
        # never client-supplied values.
        with portal.app.test_client() as client:
            self._login_admin(client)
            client.post("/api/admin/users", json={
                "username": "capture-user",
                "password": "CaptureUserPassword-1",
                "departments": ["Interview-Success"],
                "can_download": True,
            })

        with portal.app.test_client() as client:
            client.post("/api/login", json={
                "username": "capture-user", "password": "CaptureUserPassword-1",
            })
            self.assertEqual(client.post("/api/log/capture", json={
                "kind": "bogus",
            }).status_code, 400)
            self.assertEqual(client.post("/api/log/capture", json={
                "kind": "screenshot", "method": "printscreen",
                "username": "forged-admin",  # must be ignored
            }).status_code, 200)

        events = audit_service.list_events()["events"]
        shot = next(event for event in events if event["action"] == "screenshot")
        self.assertEqual(shot["username"], "capture-user")
        self.assertEqual(shot["role"], "user")
        self.assertEqual(shot["details"]["method"], "printscreen")
        self.assertTrue(shot["details"]["client_reported"])

        # Admins are exempt: their capture reports are skipped and never logged.
        before = audit_service.list_events()["total"]
        with portal.app.test_client() as client:
            self._login_admin(client)
            response = client.post("/api/log/capture", json={
                "kind": "screenshot", "method": "printscreen",
            })
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json().get("skipped"), "admin")
        admin_shots = [
            event for event in audit_service.list_events(per_page=200)["events"]
            if event["action"] == "screenshot" and event["role"] == "admin"
        ]
        self.assertEqual(admin_shots, [])
        # Only the admin's own login was added on top of the pre-existing events.
        self.assertEqual(audit_service.list_events()["total"], before + 1)

    def test_webcam_capture_gates_recordings_and_photos_are_admin_only(self):
        png = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwC"
               "AAAAC0lEQVR42mNk+M8AAAMBAQAY3Y2wAAAAAElFTkSuQmCC")
        captures = os.path.join(_TEMP_DIR.name, "test-captures")
        with mock.patch.object(portal, "WEBCAM_CAPTURE", True), \
                mock.patch.object(portal.capture_store, "CAPTURE_DIR", captures):
            with portal.app.test_client() as admin:
                self._login_admin(admin)
                admin.post("/api/admin/users", json={
                    "username": "camera-gate-user",
                    "password": "CameraGateUser-1",
                    "departments": ["Interview-Success"],
                    "can_download": True,
                })
                key = admin.get("/api/search", query_string={
                    "meeting_id": "96355112813",
                }).get_json()["results"][0]["key"]

            with portal.app.test_client() as user:
                user.post("/api/login", json={
                    "username": "camera-gate-user", "password": "CameraGateUser-1",
                })
                # Recordings are blocked until the camera is enrolled.
                self.assertEqual(user.get("/api/view", query_string={"key": key}).status_code, 403)
                self.assertEqual(user.get("/api/download", query_string={"key": key}).status_code, 403)
                self.assertEqual(user.post("/api/download/bulk", json={"keys": [key]}).status_code, 403)

                # A denial is logged and keeps recordings blocked.
                denied = user.post("/api/camera/enroll", json={"denied": True, "reason": "blocked"})
                self.assertEqual(denied.status_code, 200)
                self.assertFalse(denied.get_json()["ok"])
                self.assertEqual(user.get("/api/view", query_string={"key": key}).status_code, 403)

                # Enrolment needs a real photo; then recordings unlock.
                self.assertEqual(user.post("/api/camera/enroll", json={}).status_code, 400)
                self.assertTrue(user.post("/api/camera/enroll", json={"photo": png}).get_json()["ok"])
                self.assertEqual(user.get("/api/view", query_string={"key": key}).status_code, 200)

                # A capture report stores a webcam photo.
                self.assertEqual(user.post("/api/log/capture", json={
                    "kind": "screenshot", "method": "printscreen", "key": key, "photo": png,
                }).status_code, 200)

                photo_name = next(
                    event["details"]["capture_photo"]
                    for event in audit_service.list_events(per_page=100)["events"]
                    if event["action"] == "camera_enrolled"
                )
                # A normal user cannot read capture photos.
                self.assertEqual(user.get("/api/admin/capture/" + photo_name).status_code, 403)

            with portal.app.test_client() as admin:
                self._login_admin(admin)
                photo_response = admin.get("/api/admin/capture/" + photo_name)
                self.assertEqual(photo_response.status_code, 200)
                photo_response.close()   # release the streamed file handle
                self.assertEqual(admin.get("/api/admin/capture/missing00.jpg").status_code, 404)
                # Admins are exempt from the camera gate.
                self.assertEqual(admin.get("/api/view", query_string={"key": key}).status_code, 200)

        actions = {e["action"] for e in audit_service.list_events(per_page=100)["events"]}
        self.assertTrue({"camera_enrolled", "camera_unavailable", "screenshot"}.issubset(actions))

    def test_download_refresh_user_changes_and_logout_are_recorded(self):
        child_password = "ChildPasswordMustNotBeLogged"
        with portal.app.test_client() as client:
            self._login_admin(client)
            result = client.get("/api/search", query_string={
                "meeting_id": "96355112813",
            }).get_json()["results"][0]

            self.assertEqual(client.get("/api/download", query_string={
                "key": result["key"],
            }).status_code, 200)
            self.assertEqual(client.post("/api/refresh").status_code, 200)
            self.assertEqual(client.post("/api/admin/users", json={
                "username": "audit-child-user",
                "password": child_password,
                "departments": ["Interview-Success"],
                "hosts": {},
                "can_download": False,
            }).status_code, 200)
            self.assertEqual(client.patch("/api/admin/users/audit-child-user", json={
                "can_download": True,
            }).status_code, 200)
            self.assertEqual(client.delete("/api/admin/users/audit-child-user").status_code, 200)
            self.assertEqual(client.delete("/api/admin/users/audit-child-user").status_code, 404)
            self.assertEqual(client.get("/logout").status_code, 302)

        events = audit_service.list_events(per_page=100)["events"]
        actions = {event["action"] for event in events}
        self.assertTrue({
            "download", "refresh", "user_create", "user_update", "user_delete", "logout",
        }.issubset(actions))
        download_event = next(event for event in events if event["action"] == "download")
        self.assertEqual(download_event["meeting_id"], result["meeting_id"])
        self.assertEqual(sum(event["action"] == "user_delete" for event in events), 1)
        self.assertNotIn(child_password, json.dumps(events))

    def test_hard_row_cap_keeps_only_the_newest_events(self):
        original_limit = audit_service.AUDIT_MAX_ROWS
        audit_service.AUDIT_MAX_ROWS = 3
        try:
            for number in range(5):
                self.assertTrue(audit_service.record_event(
                    "cap_test", username=f"user-{number}",
                ))
            result = audit_service.list_events(per_page=20)
            self.assertEqual(result["total"], 3)
            self.assertEqual(
                {event["username"] for event in result["events"]},
                {"user-2", "user-3", "user-4"},
            )
        finally:
            audit_service.AUDIT_MAX_ROWS = original_limit

    def test_json_endpoints_reject_non_object_bodies(self):
        with portal.app.test_client() as client:
            self.assertEqual(client.post("/api/login", json=[1]).status_code, 400)
            self.assertEqual(client.post(
                "/api/login", data="null", content_type="application/json",
            ).status_code, 400)
            self.assertEqual(client.post(
                "/api/login", data="{", content_type="application/json",
            ).status_code, 400)
            self._login_admin(client)
            self.assertEqual(client.post("/api/download/bulk", json=[1]).status_code, 400)
            self.assertEqual(client.post("/api/admin/users", json=[1]).status_code, 400)
            self.assertEqual(
                client.patch("/api/admin/users/any-user", json=[1]).status_code,
                400,
            )

    def test_bulk_zip_file_limit_is_enforced_before_building(self):
        with portal.app.test_client() as client:
            self._login_admin(client)
            result = client.get("/api/search", query_string={
                "meeting_id": "96355112813",
            }).get_json()["results"][0]
            original_limit = s3_service.BULK_ZIP_MAX_FILES
            s3_service.BULK_ZIP_MAX_FILES = 0
            try:
                response = client.post("/api/download/bulk", json={
                    "keys": [result["key"]],
                })
            finally:
                s3_service.BULK_ZIP_MAX_FILES = original_limit
        self.assertEqual(response.status_code, 413)
        self.assertIn("at most", response.get_json()["error"])

    def test_bulk_zip_deduplicates_keys_and_records_item_metadata(self):
        zip_paths = []
        real_build_zip = s3_service.build_zip

        def capture_zip(keys):
            path = real_build_zip(keys)
            zip_paths.append(path)
            return path

        try:
            with portal.app.test_client() as client:
                self._login_admin(client)
                result = client.get("/api/search", query_string={
                    "meeting_id": "96355112813",
                }).get_json()["results"][0]
                with mock.patch.object(s3_service, "build_zip", side_effect=capture_zip):
                    response = client.post("/api/download/bulk", json={
                        "keys": [result["key"], result["key"], "not/authorized"],
                    })
                    self.assertEqual(response.status_code, 200)
                    self.assertTrue(response.get_data().startswith(b"PK"))
                    response.close()
            self.assertTrue(zip_paths)
            self.assertTrue(all(not os.path.exists(path) for path in zip_paths))
        finally:
            for path in zip_paths:
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    pass

        event = next(
            item for item in audit_service.list_events(per_page=100)["events"]
            if item["action"] == "bulk_download"
        )
        self.assertEqual(event["details"]["file_count"], 1)
        self.assertEqual(len(event["details"]["items"]), 1)
        self.assertEqual(event["details"]["items"][0]["meeting_id"], result["meeting_id"])


if __name__ == "__main__":
    unittest.main()
