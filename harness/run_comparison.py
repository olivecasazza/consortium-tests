#!/usr/bin/env python3
from __future__ import annotations
"""run_comparison.py — run Python tests against both backends + Rust tests,
collect JUnit XML results for the scorecard.

Usage:
    python harness/run_comparison.py [--results-dir=./results]

Produces:
    results/python-original.xml   — Python tests against pure-Python ClusterShell
    results/python-rust.xml       — Python tests against Rust (PyO3) backend
    results/rust-unit.xml         — Pure Rust unit tests (cargo-nextest or cargo test)

Exit code: always 0 (we want all results even if tests fail).
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def run_cmd(cmd: list[str], env: dict | None = None, cwd: str | None = None) -> int:
    """Run a command, printing output. Returns exit code."""
    full_env = {**os.environ, **(env or {})}
    print(f"\n{'='*60}")
    print(f"  Running: {' '.join(cmd)}")
    if env:
        for k, v in env.items():
            print(f"    {k}={v}")
    print(f"{'='*60}\n")

    result = subprocess.run(
        cmd, env=full_env, cwd=cwd or str(REPO_ROOT),
    )
    return result.returncode


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--results-dir", default="results", help="Directory for JUnit XML outputs")
    parser.add_argument("--skip-python-original", action="store_true", help="Skip Python original backend tests")
    parser.add_argument("--skip-python-rust", action="store_true", help="Skip Python rust-backed tests")
    parser.add_argument("--skip-rust-unit", action="store_true", help="Skip pure Rust unit tests")
    args = parser.parse_args()

    results_dir = REPO_ROOT / args.results_dir
    results_dir.mkdir(parents=True, exist_ok=True)

    # Common pytest args
    pytest_base = [
        sys.executable, "-m", "pytest",
        "tests/", "-v",
        "--timeout=30",
        "--tb=short",
    ]

    # 1. Python tests against original pure-Python backend
    if not args.skip_python_original:
        rc = run_cmd(
            pytest_base + [f"--junit-xml={results_dir}/python-original.xml"],
            env={
                "CONSORTIUM_BACKEND": "python",
                "PYTHONPATH": f"{REPO_ROOT}/lib:{REPO_ROOT}/crates/consortium-py",
            },
        )
        print(f"  → Python (original): exit code {rc}")

    # 2. Python tests against Rust (PyO3) backend
    if not args.skip_python_rust:
        rc = run_cmd(
            pytest_base + [f"--junit-xml={results_dir}/python-rust.xml"],
            env={
                "CONSORTIUM_BACKEND": "rust",
                "PYTHONPATH": f"{REPO_ROOT}/crates/consortium-py",
            },
        )
        print(f"  → Python (rust-backed): exit code {rc}")

    # 3. Pure Rust unit tests
    if not args.skip_rust_unit:
        # Try cargo-nextest first (better JUnit output), fall back to cargo test
        nextest_available = subprocess.run(
            ["cargo", "nextest", "--version"],
            capture_output=True, cwd=str(REPO_ROOT),
        ).returncode == 0

        if nextest_available:
            rc = run_cmd([
                "cargo", "nextest", "run",
                "-p", "consortium",
                "--profile", "ci",
                f"--junit-xml={results_dir}/rust-unit.xml",
            ])
        else:
            # cargo test doesn't natively output JUnit, so we parse its output
            proc = subprocess.run(
                ["cargo", "test", "-p", "consortium", "--", "--format=terse"],
                capture_output=True, text=True, cwd=str(REPO_ROOT),
            )
            rc = proc.returncode
            # Generate a simple JUnit XML from cargo test output
            _generate_junit_from_cargo(proc.stdout, proc.stderr, results_dir / "rust-unit.xml")

        print(f"  → Rust unit tests: exit code {rc}")

    print(f"\n{'='*60}")
    print(f"  Results written to {results_dir}/")
    print(f"{'='*60}")


def _generate_junit_from_cargo(stdout: str, stderr: str, outpath: Path):
    """Parse cargo test output and generate JUnit XML."""
    import xml.etree.ElementTree as ET

    # Combine stdout and stderr — cargo test puts results in stdout
    output = stdout + stderr

    suite = ET.Element("testsuite", name="consortium", tests="0", failures="0", errors="0")
    tests = 0
    failures = 0

    for line in output.splitlines():
        line = line.strip()
        if line.startswith("test ") and (" ... " in line):
            parts = line.split(" ... ", 1)
            name = parts[0].removeprefix("test ").strip()
            status = parts[1].strip()

            tc = ET.SubElement(suite, "testcase", name=name, classname="consortium")
            tests += 1

            if status == "ok":
                pass  # success
            elif status == "FAILED":
                failures += 1
                ET.SubElement(tc, "failure", message="test failed")
            elif status == "ignored":
                ET.SubElement(tc, "skipped", message="ignored")

    suite.set("tests", str(tests))
    suite.set("failures", str(failures))

    tree = ET.ElementTree(suite)
    ET.indent(tree, space="  ")
    tree.write(str(outpath), xml_declaration=True, encoding="utf-8")


if __name__ == "__main__":
    main()
