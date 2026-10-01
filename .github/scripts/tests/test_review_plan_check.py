#!/usr/bin/env python3
"""Tests for review_plan_check.validate_plan / check_plan_file (pure + CLI)."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.fspath(Path(__file__).resolve().parents[1]))

from review_plan_check import validate_plan, check_plan_file  # noqa: E402

SCRIPT = Path(__file__).resolve().parents[1] / "review_plan_check.py"


def plan(**over):
    p = {
        "verdict": "approve",
        "summary": "Looks fine",
        "findings": [{"severity": "minor", "path": "a.py", "note": "ty op"}],
    }
    p.update(over)
    return p


class ValidatePlanTest(unittest.TestCase):
    def test_valid_plan_has_no_errors(self):
        self.assertEqual(validate_plan(plan(), allowed_paths={"a.py"}), [])

    def test_empty_findings_is_valid(self):
        self.assertEqual(validate_plan(plan(findings=[])), [])

    def test_empty_path_allowed_and_skips_membership(self):
        p = plan(findings=[{"severity": "nit", "path": "", "note": "n"}])
        self.assertEqual(validate_plan(p), [])

    def test_bad_verdict_rejected(self):
        errs = validate_plan(plan(verdict=" LGTM "))
        self.assertTrue(any("verdict" in e for e in errs))

    def test_missing_top_key_rejected(self):
        errs = validate_plan({"verdict": "approve", "summary": "s"})
        self.assertTrue(any("findings" in e for e in errs))

    def test_extra_top_key_rejected(self):
        errs = validate_plan({**plan(), "issues": []})
        self.assertTrue(any("issues" in e for e in errs))

    def test_findings_not_a_list_rejected(self):
        errs = validate_plan(plan(findings={}))
        self.assertTrue(any("findings" in e for e in errs))

    def test_bad_severity_rejected(self):
        p = plan(findings=[{"severity": "fatal", "path": "a", "note": "n"}])
        self.assertTrue(any("severity" in e for e in validate_plan(p)))

    def test_finding_missing_note_rejected(self):
        p = plan(findings=[{"severity": "nit", "path": "a"}])
        self.assertTrue(any("note" in e for e in validate_plan(p)))

    def test_finding_extra_key_rejected(self):
        p = plan(findings=[{"severity": "nit", "path": "a", "note": "n",
                            "line": 3}])
        self.assertTrue(any("line" in e for e in validate_plan(p)))

    def test_empty_note_rejected(self):
        p = plan(findings=[{"severity": "nit", "path": "a", "note": "  "}])
        self.assertTrue(any("note" in e for e in validate_plan(p)))

    def test_note_over_500_chars_rejected(self):
        p = plan(findings=[{"severity": "nit", "path": "a", "note": "n" * 501}])
        self.assertTrue(any("note" in e for e in validate_plan(p)))

    def test_empty_summary_rejected(self):
        self.assertTrue(any("summary" in e for e in validate_plan(plan(summary=""))))

    def test_summary_over_500_chars_rejected(self):
        errs = validate_plan(plan(summary="s" * 501))
        self.assertTrue(any("summary" in e for e in errs))

    def test_non_member_path_rejected_when_items_given(self):
        errs = validate_plan(plan(), allowed_paths={"other.py"})
        self.assertTrue(any("path" in e and "'a.py'" in e for e in errs))
        errs = validate_plan(plan(), allowed_paths={"a.py"})
        self.assertEqual(errs, [])

    def test_plan_not_an_object_rejected(self):
        self.assertTrue(validate_plan([1, 2]))


class CheckPlanFileTest(unittest.TestCase):
    def _run_cli(self, plan_obj, text=None):
        with tempfile.TemporaryDirectory() as td:
            pp = Path(td) / "plan.json"
            pp.write_text(text if text is not None else json.dumps(plan_obj))
            return check_plan_file(pp)

    def test_valid_file_returns_empty(self):
        self.assertEqual(self._run_cli(plan()), [])

    def test_unparseable_file_returns_json_error(self):
        errs = self._run_cli(None, text="not json {")
        self.assertEqual(len(errs), 1)
        self.assertIn("JSON", errs[0])

    def test_missing_file_returns_error(self):
        errs = check_plan_file(Path("/nonexistent/plan.json"))
        self.assertEqual(len(errs), 1)

    def test_cli_exit_codes(self):
        with tempfile.TemporaryDirectory() as td:
            good = Path(td) / "good.json"
            good.write_text(json.dumps(plan()))
            bad = Path(td) / "bad.json"
            bad.write_text('{"verdict": "nope"}')
            ok = subprocess.run(
                [sys.executable, str(SCRIPT), "--plan", str(good)],
                capture_output=True, text=True)
            ko = subprocess.run(
                [sys.executable, str(SCRIPT), "--plan", str(bad)],
                capture_output=True, text=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(ko.returncode, 1)
        self.assertIn("verdict", ko.stdout + ko.stderr)


if __name__ == "__main__":
    unittest.main()
