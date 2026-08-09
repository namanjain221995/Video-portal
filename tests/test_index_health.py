"""What the portal says when the bucket index cannot be built.

A failed index and a slow index look identical from the search page: hosts are
empty and `ready` is false either way. That ambiguity is the bug these tests pin
— an operator staring at "indexing bucket…" with an empty Host dropdown has no
way to learn that the AWS credentials expired twenty minutes ago.
"""

import os
import unittest
from unittest import mock

# setdefault, never update: another test module in the same run configures the
# same process-wide values (ADMIN_USERS, USERS_FILE, AUDIT_DB) before importing
# app.py, and overwriting them here would break ITS logins. These tests need no
# credentials of their own — they seed the session directly.
os.environ.setdefault("DEMO_MODE", "true")
os.environ.setdefault("SECRET_KEY", "index-health-secret-not-used-outside-tests")

import app as portal      # noqa: E402  (environment must be configured first)
import s3_service         # noqa: E402

# The exact shape boto3 raises once a temporary STS token ages out — the failure
# that produced an empty Host dropdown in production.
EXPIRED = ("An error occurred (ExpiredToken) when calling the ListObjectsV2 "
           "operation: The provided token has expired.")

RECORD_KEY = ("HR/Abhishek_Jain/2026/June/Sanjana_Gupta/2026-06-15"
              "/Time-4-00-PM-IST/96355120099/M4A/audio.m4a")


class IndexFailureTests(unittest.TestCase):
    """These exercise the real (non-demo) code path, so DEMO_MODE is lifted for
    the duration and every scrap of cached state is restored afterwards."""

    def setUp(self):
        self._demo = s3_service.DEMO_MODE
        s3_service.DEMO_MODE = False
        self._reset()

    def tearDown(self):
        s3_service.DEMO_MODE = self._demo
        self._reset()

    def _reset(self):
        s3_service._cache.update({"records": None, "by_key": None,
                                  "by_meeting": None, "options": None, "ts": 0.0})
        s3_service._index_error = None

    def test_a_failed_build_records_why_instead_of_staying_silent(self):
        with mock.patch.object(s3_service, "_scan_s3", side_effect=RuntimeError(EXPIRED)):
            with self.assertRaises(RuntimeError):
                s3_service.get_records(force=True)

        info = s3_service.cache_info()
        self.assertFalse(info["ready"])
        self.assertIn("ExpiredToken", info["error"])

    def test_a_later_successful_build_clears_the_error(self):
        with mock.patch.object(s3_service, "_scan_s3", side_effect=RuntimeError(EXPIRED)):
            with self.assertRaises(RuntimeError):
                s3_service.get_records(force=True)

        record = s3_service._parse_key(RECORD_KEY, 1000)
        with mock.patch.object(s3_service, "_scan_s3", return_value=[record]):
            s3_service.get_records(force=True)

        info = s3_service.cache_info()
        self.assertTrue(info["ready"])
        self.assertIsNone(info["error"])
        self.assertEqual(info["count"], 1)

    def test_a_healthy_index_reports_no_error(self):
        record = s3_service._parse_key(RECORD_KEY, 1000)
        with mock.patch.object(s3_service, "_scan_s3", return_value=[record]):
            s3_service.get_records(force=True)
        self.assertIsNone(s3_service.cache_info()["error"])


class FiltersEndpointTests(unittest.TestCase):
    def setUp(self):
        self.client = portal.app.test_client()
        # Seed the session rather than logging in: /api/filters only reads
        # session user/role, and this keeps the test independent of whatever
        # ADMIN_USERS the rest of the suite configured.
        with self.client.session_transaction() as session:
            session["user"] = "index-health-admin"
            session["role"] = "admin"

    def test_the_search_page_is_told_the_index_failed_and_why(self):
        """The whole point: hosts are empty AND the page can explain itself."""
        broken = {"demo": False, "count": 0, "rosters": 0, "age_sec": None,
                  "ready": False, "error": EXPIRED}
        with mock.patch.object(s3_service, "cache_info", return_value=broken), \
             mock.patch.object(s3_service, "filter_options",
                               return_value={"hosts": [], "departments": []}):
            data = self.client.get("/api/filters").get_json()

        self.assertEqual(data["hosts"], [])
        self.assertFalse(data["cache"]["ready"])
        # Translated into the actionable sentence, not the raw boto traceback text.
        self.assertIn("credentials have expired", data["cache"]["error"])
        self.assertNotIn("ListObjectsV2", data["cache"]["error"])

    def test_a_warming_index_is_not_reported_as_an_error(self):
        """A cold boot must still look like progress, or the page would cry wolf
        on every restart and stop retrying."""
        warming = {"demo": False, "count": 0, "rosters": 0, "age_sec": None,
                   "ready": False, "error": None}
        with mock.patch.object(s3_service, "cache_info", return_value=warming), \
             mock.patch.object(s3_service, "filter_options",
                               return_value={"hosts": [], "departments": []}):
            data = self.client.get("/api/filters").get_json()

        self.assertFalse(data["cache"]["ready"])
        self.assertIsNone(data["cache"]["error"])

    def test_a_healthy_index_still_returns_its_hosts(self):
        healthy = {"demo": True, "count": 30, "rosters": 1, "age_sec": 0,
                   "ready": True, "error": None}
        with mock.patch.object(s3_service, "cache_info", return_value=healthy):
            data = self.client.get("/api/filters").get_json()

        self.assertTrue(data["cache"]["ready"])
        self.assertIsNone(data["cache"]["error"])
        self.assertIn("Vivek_Parmar", data["hosts"])


if __name__ == "__main__":
    unittest.main()
