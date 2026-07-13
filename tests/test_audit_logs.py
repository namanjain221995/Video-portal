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
