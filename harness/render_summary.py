#!/usr/bin/env python3
from __future__ import annotations
"""render_summary.py — parse JUnit XML results and render a GitHub Actions
summary showing side-by-side migration progress.

Usage:
    python harness/render_summary.py [--results-dir=./results] [--mapping=TEST_MAPPING.toml]

Reads:
    results/python-original.xml   — Python tests against pure-Python ClusterShell
    results/python-rust.xml       — Python tests against Rust (PyO3) backend
    results/rust-unit.xml         — Pure Rust unit tests

Outputs markdown to stdout (pipe to $GITHUB_STEP_SUMMARY in CI).
"""
import argparse
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def parse_junit(path: Path) -> dict[str, str]:
    """Parse JUnit XML → {test_name: "pass"|"fail"|"skip"|"error"}."""
    results = {}
    if not path.exists():
        return results

    try:
        tree = ET.parse(path)
    except ET.ParseError:
        print(f"  WARN: Could not parse {path}", file=sys.stderr)
        return results

    root = tree.getroot()

    # Handle both <testsuites><testsuite>... and <testsuite>... layouts
    suites = root.findall(".//testsuite")
    if root.tag == "testsuite":
        suites = [root]

    for suite in suites:
        for tc in suite.findall("testcase"):
            classname = tc.get("classname", "")
            name = tc.get("name", "")
            full_name = f"{classname}.{name}" if classname else name

            if tc.find("failure") is not None:
                results[full_name] = "fail"
            elif tc.find("error") is not None:
                results[full_name] = "error"
            elif tc.find("skipped") is not None:
                results[full_name] = "skip"
            else:
                results[full_name] = "pass"

    return results


def status_icon(status: str | None) -> str:
    """Map status to emoji."""
    if status is None:
        return "⬜"  # not run / not mapped
    return {
        "pass": "✅",
        "fail": "❌",
        "error": "💥",
        "skip": "⏭️",
    }.get(status, "❓")


def group_by_module(results: dict[str, str]) -> dict[str, dict[str, str]]:
    """Group test results by their module/class prefix."""
    grouped = defaultdict(dict)
    for full_name, status in results.items():
        parts = full_name.rsplit(".", 1)
        if len(parts) == 2:
            module, method = parts
        else:
            module, method = "unknown", parts[0]

        # Normalize module name: tests.RangeSetTest.RangeSetTest -> RangeSetTest
        if "." in module:
            module = module.rsplit(".", 1)[-1]

        grouped[module][method] = status
    return dict(grouped)


def render_summary(results_dir: Path):
    """Render the full migration scorecard."""
    py_orig = parse_junit(results_dir / "python-original.xml")
    py_rust = parse_junit(results_dir / "python-rust.xml")
    rust_unit = parse_junit(results_dir / "rust-unit.xml")

    # Read upstream ref
    ref_file = REPO_ROOT / "UPSTREAM_REF"
    upstream_ref = ref_file.read_text().strip() if ref_file.exists() else "unknown"

    # Group by module
    py_orig_grouped = group_by_module(py_orig)
    py_rust_grouped = group_by_module(py_rust)

    # Rust tests are already grouped by module:: path
    rust_grouped = defaultdict(dict)
    for name, status in rust_unit.items():
        # name like "range_set::tests::test_simple" or "consortium.range_set::tests::test_simple"
        parts = name.split("::")
        if len(parts) >= 2:
            module = parts[0]
            method = "::".join(parts[1:])
        else:
            module, method = "unknown", name
        rust_grouped[module][method] = status

    # Collect all modules (Python class names)
    all_modules = sorted(set(list(py_orig_grouped.keys()) + list(py_rust_grouped.keys())))

    # ── Header ──
    lines = []
    lines.append("## 🦀 Migration Scorecard")
    lines.append("")
    lines.append(f"Upstream: `cea-hpc/clustershell` @ `{upstream_ref}`")
    lines.append("")

    # ── Overview table ──
    lines.append("### Overview")
    lines.append("")
    lines.append("| Module | Python Tests | Original ✅ | Rust-backed ✅ | Parity |")
    lines.append("|--------|:-----------:|:-----------:|:-------------:|:------:|")

    total_py = 0
    total_py_pass = 0
    total_rust_pass = 0

    module_details = {}

    for module in all_modules:
        orig_tests = py_orig_grouped.get(module, {})
        rust_tests = py_rust_grouped.get(module, {})

        n_tests = len(orig_tests)
        n_orig_pass = sum(1 for s in orig_tests.values() if s == "pass")
        n_rust_pass = sum(1 for s in rust_tests.values() if s == "pass")

        parity = f"{n_rust_pass}/{n_orig_pass}" if n_orig_pass > 0 else "—"
        parity_pct = f" ({100*n_rust_pass//n_orig_pass}%)" if n_orig_pass > 0 else ""

        lines.append(f"| {module} | {n_tests} | {n_orig_pass} | {n_rust_pass} | {parity}{parity_pct} |")

        total_py += n_tests
        total_py_pass += n_orig_pass
        total_rust_pass += n_rust_pass

        module_details[module] = (orig_tests, rust_tests)

    # Totals row
    total_parity = f"{total_rust_pass}/{total_py_pass}"
    total_pct = f" ({100*total_rust_pass//total_py_pass}%)" if total_py_pass > 0 else ""
    lines.append(f"| **Total** | **{total_py}** | **{total_py_pass}** | **{total_rust_pass}** | **{total_parity}{total_pct}** |")
    lines.append("")

    # ── Rust unit test summary ──
    n_rust_unit = len(rust_unit)
    n_rust_unit_pass = sum(1 for s in rust_unit.values() if s == "pass")
    lines.append(f"**Pure Rust unit tests:** {n_rust_unit_pass}/{n_rust_unit} passing")
    lines.append("")

    # ── Per-module details (collapsed) ──
    for module in all_modules:
        orig_tests, rust_tests = module_details[module]
        all_methods = sorted(set(list(orig_tests.keys()) + list(rust_tests.keys())))

        if not all_methods:
            continue

        n_orig_pass = sum(1 for s in orig_tests.values() if s == "pass")
        n_rust_pass = sum(1 for s in rust_tests.values() if s == "pass")

        lines.append(f"<details>")
        lines.append(f"<summary><b>{module}</b> — {n_rust_pass}/{n_orig_pass} ported</summary>")
        lines.append("")
        lines.append("| Test | Original | Rust-backed |")
        lines.append("|------|:--------:|:-----------:|")

        for method in all_methods:
            orig_s = status_icon(orig_tests.get(method))
            rust_s = status_icon(rust_tests.get(method))
            lines.append(f"| `{method}` | {orig_s} | {rust_s} |")

        lines.append("")
        lines.append("</details>")
        lines.append("")

    # ── Rust unit tests detail ──
    if rust_unit:
        lines.append("<details>")
        lines.append(f"<summary><b>Pure Rust Unit Tests</b> — {n_rust_unit_pass}/{n_rust_unit} passing</summary>")
        lines.append("")
        lines.append("| Test | Status |")
        lines.append("|------|:------:|")
        for name in sorted(rust_unit.keys()):
            lines.append(f"| `{name}` | {status_icon(rust_unit[name])} |")
        lines.append("")
        lines.append("</details>")
        lines.append("")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--results-dir", default="results")
    args = parser.parse_args()

    results_dir = REPO_ROOT / args.results_dir
    summary = render_summary(results_dir)
    print(summary)


if __name__ == "__main__":
    main()
