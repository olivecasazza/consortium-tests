#!/usr/bin/env python3
"""Tests for pr_review_items.build_items / flatten_file_pages (pure logic)."""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.fspath(Path(__file__).resolve().parents[1]))

from pr_review_items import build_items, flatten_file_pages  # noqa: E402


def gh_file(path, status="modified", additions=3, deletions=1, patch="@@ -1 +1 @@\n-a\n+b"):
    return {
        "filename": path,
        "status": status,
        "additions": additions,
        "deletions": deletions,
        **({"patch": patch} if patch is not None else {}),
    }


PR_META = {
    "number": 17,
    "title": "Fanout identity",
    "author": "olivecasazza",
    "base": "master",
    "head": "feat/x",
    "additions": 100,
    "deletions": 40,
    "changed_files": 2,
    "commit_count": 7,
    "body": "A body",
}


class BuildItemsTest(unittest.TestCase):
    def test_basic_shape_preserves_order_and_counts(self):
        files = [gh_file("a.py"), gh_file("b.yml")]
        doc = build_items(files, PR_META)

        self.assertEqual([i["path"] for i in doc["items"]], ["a.py", "b.yml"])
        self.assertEqual(doc["items"][0]["status"], "modified")
        self.assertEqual(doc["items"][0]["additions"], 3)
        self.assertEqual(doc["items"][0]["deletions"], 1)
        self.assertEqual(doc["items"][0]["patch"], gh_file("a.py")["patch"])
        self.assertIsNone(doc["items"][0]["previous_path"])
        self.assertEqual(doc["pr"]["number"], 17)
        self.assertEqual(doc["pr"]["author"], "olivecasazza")
        self.assertEqual(doc["pr"]["base"], "master")
        self.assertEqual(doc["pr"]["head"], "feat/x")
        self.assertEqual(doc["pr"]["changed_files"], 2)
        self.assertEqual(doc["pr"]["commit_count"], 7)
        self.assertEqual(doc["patch_truncated_paths"], [])
        self.assertEqual(doc["patch_missing_paths"], [])

    def test_patch_bytes_total_is_encoded_length_sum(self):
        patch = "α" * 10  # multi-byte: 20 UTF-8 bytes per 10 chars
        doc = build_items([gh_file("a.py", patch=patch)], PR_META)
        self.assertEqual(doc["patch_bytes_total"], len(patch.encode("utf-8")))

    def test_budget_drops_later_patch_and_records_it(self):
        big = "x" * 100
        files = [gh_file("big.py", patch=big), gh_file("small.py", patch="y")]
        doc = build_items(files, PR_META, max_total_patch_bytes=100)

        self.assertIsNone(doc["items"][1]["patch"])
        self.assertIn("small.py", doc["patch_truncated_paths"])
        self.assertNotIn("big.py", doc["patch_truncated_paths"])
        self.assertEqual(doc["items"][1]["additions"], 3)  # counts survive
        self.assertEqual(doc["items"][0]["patch"], big)

    def test_exact_fit_budget_truncates_nothing(self):
        patch = "z" * 30
        doc = build_items([gh_file("a.py", patch=patch)],
                          PR_META, max_total_patch_bytes=30)
        self.assertEqual(doc["items"][0]["patch"], patch)
        self.assertEqual(doc["patch_truncated_paths"], [])

    def test_api_absent_patch_recorded_as_missing_not_truncated(self):
        files = [gh_file("binary.bin", patch=None)]
        doc = build_items(files, PR_META)
        self.assertIsNone(doc["items"][0]["patch"])
        self.assertEqual(doc["patch_missing_paths"], ["binary.bin"])
        self.assertEqual(doc["patch_truncated_paths"], [])

    def test_rename_keeps_previous_path(self):
        files = [gh_file("new.py", status="renamed")]
        files[0]["previous_filename"] = "old.py"
        doc = build_items(files, PR_META)
        self.assertEqual(doc["items"][0]["previous_path"], "old.py")

    def test_body_none_becomes_empty_and_long_body_truncated(self):
        doc = build_items([], {**PR_META, "body": None})
        self.assertEqual(doc["pr"]["body"], "")
        doc = build_items([], {**PR_META, "body": "b" * 3000})
        self.assertEqual(len(doc["pr"]["body"]), 2000)

    def test_missing_filename_fails_loud(self):
        with self.assertRaises(ValueError):
            build_items([{"status": "modified"}], PR_META)

    def test_unknown_status_fails_loud(self):
        with self.assertRaises(ValueError):
            build_items([gh_file("a.py", status="moved")], PR_META)

    def test_non_integer_additions_fails_loud(self):
        with self.assertRaises(ValueError):
            build_items([gh_file("a.py", additions="3")], PR_META)


class FlattenFilePagesTest(unittest.TestCase):
    def test_flattens_pages_in_order(self):
        a, b, c = gh_file("a.py"), gh_file("b.py"), gh_file("c.py")
        self.assertEqual(flatten_file_pages([[a], [b, c]]), [a, b, c])

    def test_accepts_single_page_of_dicts(self):
        a = gh_file("a.py")
        self.assertEqual(flatten_file_pages([a]), [a])

    def test_empty_pages_give_empty_list(self):
        self.assertEqual(flatten_file_pages([]), [])

    def test_mixed_shape_fails_loud(self):
        with self.assertRaises(ValueError):
            flatten_file_pages([[gh_file("a.py")], {"filename": "b.py"}])


if __name__ == "__main__":
    unittest.main()
