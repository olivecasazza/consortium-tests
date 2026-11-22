"""Run the vendored exec-only gather cases against an explicitly selected claw."""

import argparse
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

# The cases and their Python fixture imports must come from the same checkout.
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "lib")]
from tests import CLIClushTest


def run_case(method: str, expected_calls: int, claw: Path, workdir: str) -> None:
    """Keep the upstream case and fixtures; replace only its CLI invocation."""
    case = CLIClushTest.CLIClushTest_A(method)

    def check_claw(args: list[str], stdin: bytes | None, expected_stdout: bytes,
                   expected_rc: int = 0, expected_stderr: bytes | None = None) -> None:
        """Check the unmodified upstream argv, return code and output bytes."""
        options = args[:-1]
        if "--worker=exec" not in options and ("-R", "exec") not in zip(options, options[1:]):
            case.fail(f"refusing vector without explicit exec worker: {args!r}")
        result = subprocess.run(
            [claw, *args], cwd=workdir, env={"HOME": workdir, "LC_ALL": "C", "PATH": os.environ["PATH"]},
            input=b"" if stdin is None else stdin, capture_output=True, timeout=30, check=False,
        )
        case.assertEqual(result.returncode, expected_rc, f"{args!r}: {result.stderr!r}")
        case.assertEqual(result.stdout, expected_stdout, f"stdout: {args!r}")
        if expected_stderr is not None:
            case.assertEqual(result.stderr, expected_stderr, f"stderr: {args!r}")

    with patch.object(case, "_clush_t", side_effect=check_claw) as calls:
        case.debug()  # Run setUp, this method only, and tearDown; skips are errors here.
        case.assertEqual(calls.call_count, expected_calls, case.id())
    print(f"PASS {case.id()} ({expected_calls} calls to {claw})")


def main() -> None:
    """Run only cases whose complete argv and expectations work unchanged."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claw", required=True, type=Path, help="path to the built native claw")
    claw = parser.parse_args().claw.resolve()
    if not claw.is_file() or not os.access(claw, os.X_OK):
        parser.error(f"not an executable file: {claw}")

    with TemporaryDirectory() as workdir:
        run_case("test_036_sorted_gather", 2, claw, workdir)
        run_case("test_046_axis", 5, claw, workdir)


if __name__ == "__main__":
    main()
