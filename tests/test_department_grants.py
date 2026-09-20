"""How a stored department grant follows a folder that was split into parts.

This is access-control code, and both directions are dangerous. Expanding too
little silently REVOKES recordings someone could see yesterday; expanding too
much hands over a department nobody granted. The cases below pin each shape.

Two shapes exist in this bucket:

* FULLY split — "Training" stopped being a department when Training/Advanced,
  Training/Coding and the rest became ones. A stored "Training" grant must
  become exactly those children.
* MIXED — "Interview-Success" is still a department (two dozen host folders sit
  directly under it) AND holds category folders (Internal-Interview/, Interview/)
  that are granted separately. A stored grant must keep the parent and gain the
  children.
"""

import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("DEMO_MODE", "true")
os.environ.setdefault("SECRET_KEY", "department-grant-secret-not-used-outside-tests")

import auth  # noqa: E402

_TEMP_DIR = tempfile.TemporaryDirectory()
_USERS_PATH = os.path.join(_TEMP_DIR.name, "department-grant-users.json")

# What the bucket actually holds, as the live index would report it. Note
# Training/Coding and Training/Retraining: they exist in S3 but were never added
# to auth.SPLIT_DEPARTMENTS, which is exactly the drift these tests cover.
KNOWN = [
    "CEO", "COO", "Customer-Success", "Executive-Assistant", "HR",
    "Interview-Success",
    "Interview-Success/Internal-Interview",
    "Interview-Success/Interview",
    "Marketing", "Other", "QMS", "Techsphere",
    "Training/Advanced", "Training/Coding", "Training/Interview-Readiness",
    "Training/Interview-Rejection-Training", "Training/Non-Technical-1-1-Training",
    "Training/Other", "Training/Resume-Based", "Training/Retraining",
]


class MixedParentGrantTests(unittest.TestCase):
    """Interview-Success: the parent survives the split."""

    def expand(self, departments, hosts=None, known=KNOWN):
        return auth._expand_split(departments, hosts or {}, known)

    def test_the_parent_grant_keeps_the_parent_and_gains_its_children(self):
        depts, _ = self.expand(["Interview-Success"])
        self.assertEqual(set(depts), {
            "Interview-Success",
            "Interview-Success/Internal-Interview",
            "Interview-Success/Interview",
        })

    def test_nobody_loses_the_recordings_they_could_already_see(self):
        """The whole point of expanding: before the split a grant for the parent
        reached everything underneath it, and it still must."""
        before = {"Interview-Success"}
        after = set(self.expand(["Interview-Success"])[0])
        reachable = {d for d in KNOWN
                     if d in after or any(d == p or d.startswith(p + "/") for p in after)}
        self.assertTrue({d for d in KNOWN if d.startswith("Interview-Success")} <= reachable)
        self.assertTrue(before <= after)

    def test_a_host_restriction_follows_the_grant_into_every_child(self):
        """Left behind on the parent alone, the mask would key off a department
        the grant no longer names and restrict nothing — WIDENING access."""
        depts, hosts = self.expand(["Interview-Success"],
                                   {"Interview-Success": ["Agrima_Agarwal"]})
        for dept in depts:
            self.assertEqual(hosts.get(dept), ["Agrima_Agarwal"], dept)

    def test_granting_one_child_grants_only_that_child(self):
        depts, _ = self.expand(["Interview-Success/Internal-Interview"])
        self.assertEqual(depts, ["Interview-Success/Internal-Interview"])

    def test_an_unrelated_department_is_untouched(self):
        depts, hosts = self.expand(["QMS", "HR"], {"QMS": ["Priya_Nair"]})
        self.assertEqual(depts, ["QMS", "HR"])
        self.assertEqual(hosts, {"QMS": ["Priya_Nair"]})

    def test_a_prefix_that_is_not_a_folder_boundary_does_not_match(self):
        """"Interview-Success" must not swallow a department that merely starts
        with the same letters."""
        depts, _ = self.expand(["Interview"], known=["Interview", "Interview-Success"])
        self.assertEqual(depts, ["Interview"])


class FullySplitParentGrantTests(unittest.TestCase):
    """Training: the parent is gone and its children cover it."""

    def expand(self, departments, hosts=None, known=KNOWN):
        return auth._expand_split(departments, hosts or {}, known)

    def test_a_legacy_parent_grant_becomes_its_children(self):
        depts, _ = self.expand(["Training"])
        self.assertNotIn("Training", depts)
        self.assertIn("Training/Advanced", depts)

    def test_children_missing_from_the_static_map_are_still_picked_up(self):
        """Training/Coding and Training/Retraining are in the bucket but were
        never added to SPLIT_DEPARTMENTS, so a legacy grant silently missed
        them. The live vocabulary is what decides, not the hard-coded list."""
        self.assertNotIn("Training/Coding", auth.SPLIT_DEPARTMENTS["Training"])
        depts, _ = self.expand(["Training"])
        self.assertEqual({d for d in KNOWN if d.startswith("Training/")}, set(depts))

    def test_the_static_map_is_the_floor_while_the_index_is_warming(self):
        """all_departments() returns only the configured names until the first
        scan finishes. The map has to keep a legacy grant alive until then, or
        every Training user goes dark on each cold start."""
        depts, _ = self.expand(["Training"], known=[])
        self.assertEqual(set(depts), set(auth.SPLIT_DEPARTMENTS["Training"]))

    def test_the_host_mask_moves_to_the_children(self):
        depts, hosts = self.expand(["Training"], {"Training": ["Vivek_Parmar"]})
        self.assertNotIn("Training", hosts)
        for dept in depts:
            self.assertEqual(hosts.get(dept), ["Vivek_Parmar"], dept)


class StoredGrantRoundTripTests(unittest.TestCase):
    """The same expansion through the real storage functions."""

    def setUp(self):
        patcher = mock.patch.object(auth, "USERS_FILE", _USERS_PATH)
        patcher.start()
        self.addCleanup(patcher.stop)
        if os.path.exists(_USERS_PATH):
            os.unlink(_USERS_PATH)

    def test_user_access_expands_a_stored_parent_grant(self):
        auth.create_user("legacy", "pw", departments=["Interview-Success"])
        access = auth.user_access("legacy", KNOWN)
        self.assertIn("Interview-Success/Internal-Interview", access["departments"])
        self.assertIn("Interview-Success", access["departments"])

    def test_the_admin_list_shows_what_is_actually_enforced(self):
        """The ticked boxes must match user_access(), or an admin saves a row and
        silently narrows a grant they were only looking at."""
        auth.create_user("shown", "pw", departments=["Interview-Success"])
        listed = auth.list_users(KNOWN)[0]["departments"]
        self.assertEqual(set(listed), set(auth.user_access("shown", KNOWN)["departments"]))

    def test_omitting_the_vocabulary_falls_back_to_the_static_map(self):
        """Callers that have no live department list must still behave as before
        rather than quietly dropping the expansion."""
        auth.create_user("nolist", "pw", departments=["Training"])
        self.assertEqual(set(auth.user_access("nolist")["departments"]),
                         set(auth.SPLIT_DEPARTMENTS["Training"]))

    def test_an_account_with_no_department_field_keeps_the_legacy_default(self):
        auth.create_user("old", "pw", departments=["Interview-Success"])
        raw = auth._load_users()
        del raw["old"]["departments"]
        auth._save_users(raw)
        access = auth.user_access("old", KNOWN)
        self.assertIn("Interview-Success", access["departments"])


if __name__ == "__main__":
    unittest.main()
