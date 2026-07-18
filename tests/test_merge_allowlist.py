"""merge_allowlist.py is plain JSON read/write — no subprocess, real files
in a tmp dir per test."""
import json
import tempfile
import unittest
from pathlib import Path

from gardener import merge_allowlist


class TestMergeAllowlist(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.path = Path(self._tmpdir.name) / "merge_allowlist.json"

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_missing_file_is_empty_allowlist_not_an_error(self):
        self.assertEqual(merge_allowlist.list_allowed(path=self.path), [])
        self.assertFalse(merge_allowlist.is_allowed("owner/repo", path=self.path))

    def test_add_then_is_allowed(self):
        merge_allowlist.add("owner/repo", path=self.path)
        self.assertTrue(merge_allowlist.is_allowed("owner/repo", path=self.path))
        self.assertFalse(merge_allowlist.is_allowed("owner/other", path=self.path))

    def test_add_returns_true_when_new_false_when_already_present(self):
        self.assertTrue(merge_allowlist.add("owner/repo", path=self.path))
        self.assertFalse(merge_allowlist.add("owner/repo", path=self.path))

    def test_remove_returns_true_when_present_false_when_not(self):
        merge_allowlist.add("owner/repo", path=self.path)
        self.assertTrue(merge_allowlist.remove("owner/repo", path=self.path))
        self.assertFalse(merge_allowlist.remove("owner/repo", path=self.path))
        self.assertFalse(merge_allowlist.is_allowed("owner/repo", path=self.path))

    def test_list_allowed_is_sorted(self):
        merge_allowlist.add("owner/zeta", path=self.path)
        merge_allowlist.add("owner/alpha", path=self.path)
        self.assertEqual(merge_allowlist.list_allowed(path=self.path), ["owner/alpha", "owner/zeta"])

    def test_file_persists_as_a_plain_json_array(self):
        merge_allowlist.add("owner/repo", path=self.path)
        raw = json.loads(self.path.read_text())
        self.assertEqual(raw, ["owner/repo"])

    def test_creates_parent_directories(self):
        nested = Path(self._tmpdir.name) / "nested" / "dir" / "allowlist.json"
        merge_allowlist.add("owner/repo", path=nested)
        self.assertTrue(nested.exists())

    def test_malformed_json_raises_valueerror(self):
        self.path.write_text("not json{{{")
        with self.assertRaises(ValueError):
            merge_allowlist.list_allowed(path=self.path)

    def test_non_array_json_raises_valueerror(self):
        self.path.write_text(json.dumps({"not": "a list"}))
        with self.assertRaises(ValueError):
            merge_allowlist.list_allowed(path=self.path)

    def test_save_leaves_no_leftover_tmp_file(self):
        # _save writes via a sibling .tmp file + os.replace for atomicity
        # (a kill mid-write must never leave a torn allow-list file) —
        # confirm the temp file doesn't linger after a normal save.
        merge_allowlist.add("owner/repo", path=self.path)
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        self.assertFalse(tmp_path.exists())


if __name__ == "__main__":
    unittest.main()
