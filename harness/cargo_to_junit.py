#!/usr/bin/env python3
"""cargo_to_junit.py — convert `cargo test -- --format=terse` output to JUnit XML.

Reads from stdin, writes JUnit XML to stdout.

Usage:
    cargo test -p consortium-crate -- --format=terse 2>&1 | python harness/cargo_to_junit.py
"""
import sys
import xml.etree.ElementTree as ET

def main():
    text = sys.stdin.read()

    suite = ET.Element("testsuite", name="consortium", tests="0", failures="0", errors="0")
    tests = 0
    failures = 0

    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("test "):
            continue
        if " ... " not in line:
            continue

        parts = line.split(" ... ", 1)
        name = parts[0].removeprefix("test ").strip()
        status = parts[1].strip().lower()

        tc = ET.SubElement(suite, "testcase", name=name, classname="consortium")
        tests += 1

        if status == "ok":
            pass
        elif status == "failed":
            failures += 1
            ET.SubElement(tc, "failure", message="test failed")
        elif status == "ignored":
            ET.SubElement(tc, "skipped", message="ignored")
        else:
            # bench, unknown
            ET.SubElement(tc, "skipped", message=status)

    suite.set("tests", str(tests))
    suite.set("failures", str(failures))

    tree = ET.ElementTree(suite)
    ET.indent(tree, space="  ")
    tree.write(sys.stdout, xml_declaration=True, encoding="unicode")
    print()  # trailing newline


if __name__ == "__main__":
    main()
