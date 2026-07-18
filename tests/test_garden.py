"""garden.py is plain JSON read/write, mirroring merge_allowlist.py exactly
— real files in a tmp dir per test, same test shape as
test_merge_allowlist.py so the two stay obviously in sync."""
import json
import tempfile
import unittest
from pathlib import Path

from gardener import garden


class TestGarden(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.path = Path(self._tmpdir.name) / "garden.json"

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_missing_file_is_empty_garden_not_an_error(self):
        self.assertEqual(garden.list_garden(path=self.path), [])
        self.assertFalse(garden.is_in_garden("owner/repo", path=self.path))

    def test_add_then_is_in_garden(self):
        garden.add("owner/repo", path=self.path)
        self.assertTrue(garden.is_in_garden("owner/repo", path=self.path))
        self.assertFalse(garden.is_in_garden("owner/other", path=self.path))

    def test_add_returns_true_when_new_false_when_already_present(self):
        self.assertTrue(garden.add("owner/repo", path=self.path))
        self.assertFalse(garden.add("owner/repo", path=self.path))

    def test_remove_returns_true_when_present_false_when_not(self):
        garden.add("owner/repo", path=self.path)
        self.assertTrue(garden.remove("owner/repo", path=self.path))
        self.assertFalse(garden.remove("owner/repo", path=self.path))
        self.assertFalse(garden.is_in_garden("owner/repo", path=self.path))

    def test_list_garden_is_sorted(self):
        garden.add("owner/zeta", path=self.path)
        garden.add("owner/alpha", path=self.path)
        self.assertEqual(garden.list_garden(path=self.path), ["owner/alpha", "owner/zeta"])

    def test_file_persists_as_a_plain_json_array(self):
        garden.add("owner/repo", path=self.path)
        raw = json.loads(self.path.read_text())
        self.assertEqual(raw, ["owner/repo"])

    def test_creates_parent_directories(self):
        nested = Path(self._tmpdir.name) / "nested" / "dir" / "garden.json"
        garden.add("owner/repo", path=nested)
        self.assertTrue(nested.exists())

    def test_malformed_json_raises_valueerror(self):
        self.path.write_text("not json{{{")
        with self.assertRaises(ValueError):
            garden.list_garden(path=self.path)

    def test_non_array_json_raises_valueerror(self):
        self.path.write_text(json.dumps({"not": "a list"}))
        with self.assertRaises(ValueError):
            garden.list_garden(path=self.path)

    def test_garden_and_merge_allowlist_are_independent_files(self):
        # Adding to the garden must never touch the separate merge
        # allow-list file, and vice versa — see garden.py's module
        # docstring on these being two independent opt-in gates.
        from gardener import merge_allowlist

        allowlist_path = Path(self._tmpdir.name) / "merge_allowlist.json"
        garden.add("owner/repo", path=self.path)
        self.assertFalse(merge_allowlist.is_allowed("owner/repo", path=allowlist_path))


if __name__ == "__main__":
    unittest.main()
