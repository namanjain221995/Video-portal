"""The bulk ZIP download — streamed, and started through a preflight.

It used to copy every selected recording from S3 into a temp file and only then
start sending. A 99-file / 6.6 GB selection left the connection silent for
minutes, Cloudflare (which fronts the portal) gave up on the idle origin with a
502, and the user saw "Could not build the zip." — for an account that was fully
allowed to download. These tests pin the replacement: bytes flow immediately,
nothing is staged on disk, the archive is valid (including ZIP64), and the form
post the page now uses cannot be forged from another site.
"""

import io
import json
import os
import tempfile
import unittest
import zipfile
from unittest import mock

os.environ.setdefault("DEMO_MODE", "true")
os.environ.setdefault("SECRET_KEY", "bulk-zip-secret-not-used-outside-tests")

import app as portal   # noqa: E402
import s3_service      # noqa: E402

GROUP_MEETING = "97609808470"
QMS_MEETING = "96355125555"


def keys_for(meeting_id):
    return [r["key"] for r in s3_service.DEMO_RECORDS if r["meeting_id"] == meeting_id]


class _FakeBody:
    """Stands in for an S3 StreamingBody."""

    def __init__(self, size):
        self.size = size
        self.closed = False

    def iter_chunks(self, chunk_size):
        left = self.size
        while left > 0:
            n = min(chunk_size, left)
            left -= n
            yield b"r" * n

    def close(self):
        self.closed = True


class StreamZipTests(unittest.TestCase):
    """s3_service.stream_zip on its own."""

    def real_s3(self, size):
        bodies = []

        def get_object(Bucket, Key):
            body = _FakeBody(size)
            bodies.append(body)
            return {"Body": body}

        client = mock.Mock(get_object=get_object)
        patches = [mock.patch.object(s3_service, "DEMO_MODE", False),
                   mock.patch.object(s3_service, "_client", return_value=client)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return bodies

    def records(self, n, size):
        return [dict(r, size=size) for r in s3_service.DEMO_RECORDS[:n]]

    def test_the_archive_is_valid_and_complete(self):
        self.real_s3(2 * 1024 * 1024 + 5)
        recs = self.records(3, 2 * 1024 * 1024 + 5)
        archive = zipfile.ZipFile(io.BytesIO(b"".join(s3_service.stream_zip(recs))))
        self.assertIsNone(archive.testzip())
        self.assertEqual(len(archive.namelist()), 3)
        for info in archive.infolist():
            self.assertEqual(info.file_size, 2 * 1024 * 1024 + 5)

    def test_bytes_leave_before_the_whole_archive_exists(self):
        """The fix itself: the first chunk arrives after ONE read from S3, not
        after every recording has been fetched."""
        bodies = self.real_s3(5 * s3_service.ZIP_STREAM_CHUNK)
        stream = s3_service.stream_zip(self.records(4, 5 * s3_service.ZIP_STREAM_CHUNK))
        first = next(stream)
        self.assertTrue(first.startswith(b"PK"))
        self.assertEqual(len(bodies), 1, "only the first recording may have been opened")
        stream.close()

    def test_memory_is_one_chunk_whatever_the_archive_size(self):
        self.real_s3(6 * s3_service.ZIP_STREAM_CHUNK)
        biggest = max(len(c) for c in
                      s3_service.stream_zip(self.records(3, 6 * s3_service.ZIP_STREAM_CHUNK)))
        self.assertLessEqual(biggest, s3_service.ZIP_STREAM_CHUNK + 1024)

    def test_nothing_is_written_to_disk(self):
        """The old version wanted the whole selection's size free on the box."""
        self.real_s3(1024)
        with mock.patch.object(tempfile, "NamedTemporaryFile",
                               side_effect=AssertionError("no temp files")):
            b"".join(s3_service.stream_zip(self.records(2, 1024)))

    def test_every_s3_body_is_closed(self):
        bodies = self.real_s3(3000)
        b"".join(s3_service.stream_zip(self.records(3, 3000)))
        self.assertTrue(bodies and all(b.closed for b in bodies))

    def test_a_download_abandoned_midway_still_closes_the_s3_body(self):
        bodies = self.real_s3(4 * s3_service.ZIP_STREAM_CHUNK)
        stream = s3_service.stream_zip(self.records(3, 4 * s3_service.ZIP_STREAM_CHUNK))
        next(stream)
        stream.close()               # the browser went away
        self.assertTrue(all(b.closed for b in bodies))

    def test_zip64_holds_up(self):
        """A 6.6 GB archive is past the 4 GB classic-ZIP limit. Shrinking the limit
        sends small entries down that exact code path."""
        self.real_s3(5000)
        with mock.patch.object(zipfile, "ZIP64_LIMIT", 1024):
            blob = b"".join(s3_service.stream_zip(self.records(3, 5000)))
        self.assertIsNone(zipfile.ZipFile(io.BytesIO(blob)).testzip())

    def test_an_unknown_size_still_produces_a_valid_entry(self):
        self.real_s3(4000)
        blob = b"".join(s3_service.stream_zip(self.records(1, 0)))
        archive = zipfile.ZipFile(io.BytesIO(blob))
        self.assertIsNone(archive.testzip())
        self.assertEqual(archive.infolist()[0].file_size, 4000)

    def test_entries_carry_the_recording_date(self):
        rec = s3_service.DEMO_RECORDS[0]
        blob = b"".join(s3_service.stream_zip([rec]))
        stamp = zipfile.ZipFile(io.BytesIO(blob)).infolist()[0].date_time
        self.assertEqual("%04d-%02d-%02d" % stamp[:3], rec["date"])


class BulkDownloadRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = portal.app.test_client()
        with self.client.session_transaction() as session:
            session["user"] = "zip-admin"
            session["role"] = "admin"

    def preflight(self, keys, client=None):
        return (client or self.client).post("/api/download/bulk/check", json={"keys": keys})

    def form_download(self, keys, token, client=None):
        return (client or self.client).post("/api/download/bulk", data={
            "keys": json.dumps(keys), "token": token})

    # ── the flow the Search page uses ────────────────────────────────────────
    def test_preflight_then_form_post_streams_a_valid_zip(self):
        keys = keys_for(GROUP_MEETING)
        check = self.preflight(keys).get_json()
        self.assertTrue(check["ok"])
        self.assertEqual(check["files"], len(keys))
        resp = self.form_download(keys, check["token"])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "application/zip")
        self.assertIn("attachment", resp.headers["Content-Disposition"])
        self.assertEqual(resp.headers.get("X-Accel-Buffering"), "no")
        archive = zipfile.ZipFile(io.BytesIO(resp.get_data()))
        self.assertIsNone(archive.testzip())
        self.assertEqual(len(archive.namelist()), len(keys))

    def test_it_is_a_stream_not_a_prebuilt_file(self):
        resp = self.client.post("/api/download/bulk", json={"keys": keys_for(GROUP_MEETING)})
        self.assertTrue(resp.is_streamed)
        self.assertIsNone(resp.headers.get("Content-Length"))
        resp.close()

    def test_refusals_come_back_from_the_preflight_as_readable_json(self):
        with mock.patch.object(s3_service, "BULK_ZIP_MAX_FILES", 1):
            resp = self.preflight(keys_for(GROUP_MEETING))
        self.assertEqual(resp.status_code, 413)
        self.assertIn("at most", resp.get_json()["error"])
        self.assertEqual(self.preflight([]).status_code, 400)
        self.assertEqual(self.client.post("/api/download/bulk/check", json=[1]).status_code, 400)

    def test_the_preflight_refuses_a_view_only_account(self):
        client = portal.app.test_client()
        with client.session_transaction() as session:
            session["user"] = "viewer"
            session["role"] = "user"
        with mock.patch.object(portal.auth, "user_access", return_value={
                "departments": ["QMS"], "hosts": {}, "meetings": {}, "can_download": False}):
            resp = self.preflight(keys_for(QMS_MEETING), client)
        self.assertEqual(resp.status_code, 403)

    # ── the form path cannot be forged ───────────────────────────────────────
    def test_a_form_post_without_a_token_is_refused(self):
        resp = self.client.post("/api/download/bulk",
                                data={"keys": json.dumps(keys_for(GROUP_MEETING))})
        self.assertEqual(resp.status_code, 403)
        self.assertIn("expired", resp.get_json()["error"])

    def test_a_token_is_bound_to_the_exact_selection(self):
        token = self.preflight(keys_for(GROUP_MEETING)).get_json()["token"]
        resp = self.form_download(keys_for(GROUP_MEETING) + keys_for(QMS_MEETING), token)
        self.assertEqual(resp.status_code, 403)

    def test_a_token_is_bound_to_the_user_who_asked_for_it(self):
        token = self.preflight(keys_for(GROUP_MEETING)).get_json()["token"]
        other = portal.app.test_client()
        with other.session_transaction() as session:
            session["user"] = "someone-else"
            session["role"] = "admin"
        self.assertEqual(self.form_download(keys_for(GROUP_MEETING), token, other).status_code,
                         403)

    def test_a_token_expires(self):
        keys = keys_for(GROUP_MEETING)
        token = self.preflight(keys).get_json()["token"]
        with mock.patch.object(portal, "_BULK_TOKEN_MAX_AGE", -1):
            self.assertEqual(self.form_download(keys, token).status_code, 403)

    def test_key_order_and_repeats_do_not_invalidate_a_token(self):
        keys = keys_for(GROUP_MEETING)
        token = self.preflight(keys).get_json()["token"]
        resp = self.form_download(list(reversed(keys)) + keys[:1], token)
        self.assertEqual(resp.status_code, 200)
        resp.close()

    def test_a_valid_token_does_not_bypass_authorization(self):
        """The download re-checks access itself; the token only proves origin."""
        client = portal.app.test_client()
        with client.session_transaction() as session:
            session["user"] = "limited"
            session["role"] = "user"
        grant = {"departments": ["QMS"], "hosts": {}, "meetings": {}, "can_download": True}
        with mock.patch.object(portal.auth, "user_access", return_value=grant):
            token = self.preflight(keys_for(QMS_MEETING), client).get_json()["token"]
            # Access is narrowed between the preflight and the download.
            grant["departments"] = []
            resp = self.form_download(keys_for(QMS_MEETING), token, client)
        self.assertEqual(resp.status_code, 400)      # nothing authorized -> nothing to zip

    # ── failures that can still be reported properly ─────────────────────────
    def test_an_s3_failure_on_the_first_file_is_json_not_a_broken_download(self):
        def boom(records):
            raise RuntimeError("ExpiredToken: The provided token has expired.")
            yield b""                                 # pragma: no cover
        with mock.patch.object(s3_service, "stream_zip", side_effect=boom):
            resp = self.client.post("/api/download/bulk", json={"keys": keys_for(GROUP_MEETING)})
        self.assertEqual(resp.status_code, 502)
        self.assertIn("error", resp.get_json())

    def test_a_failed_start_is_not_audited_as_a_download(self):
        def boom(records):
            raise RuntimeError("S3 unavailable")
            yield b""                                 # pragma: no cover
        with mock.patch.object(s3_service, "stream_zip", side_effect=boom), \
                mock.patch.object(portal, "_audit") as audited:
            self.client.post("/api/download/bulk", json={"keys": keys_for(GROUP_MEETING)})
        self.assertFalse(any(c.args and c.args[0] == "bulk_download"
                             for c in audited.call_args_list))


if __name__ == "__main__":
    unittest.main()
