#!/usr/bin/env python3
"""
Classify a GitHub Actions failure log into one of the failure modes documented
in docs/ci-troubleshooting.md. Pure stdlib, no network, no model: the same log
always gives the same answer. Used by tests/test_ci_logs.py against the
hand-written logs in tests/fixtures/ci_logs/; never runs during a feature run.

  python3 ci_log_classifier.py LOG_FILE      prints "<mode> <anchor>"

Rules are checked in order, first match wins. The order matters: a Windows
encoding error and an import error both end in "FAILED (errors=1)", so the
specific causes come before the generic unittest summary.
"""
import re
import sys
from pathlib import Path

# (mode, docs anchor in docs/ci-troubleshooting.md, patterns). The anchor is the
# GitHub slug of the section heading, and a test checks the heading exists.
RULES = [
    ("timeout-or-cancelled", "timeout-or-cancelled",
     [r"has exceeded the maximum execution time", r"The operation was canceled", r"##\[error\]The run was canceled"]),
    ("python-version-missing", "python-version-missing",
     [r"The version '[^']+' with architecture '[^']+' was not found", r"Version [\d.]+ with arch \w+ not found"]),
    ("import-error", "import-error",
     [r"ModuleNotFoundError: No module named", r"ImportError: Failed to import test module",
      r"ImportError: cannot import name"]),
    ("compileall-syntax-error", "compileall-syntax-error",
     [r"\*\*\* Error compiling"]),
    ("windows-path-or-crlf", "windows-path-or-crlf",
     [r"UnicodeDecodeError: 'charmap' codec", r"UnicodeEncodeError: 'charmap' codec", r"\[WinError \d+\]",
      r"AssertionError: .*\\r\\n", r"FileNotFoundError: .*[A-Za-z]:\\\\"]),
    ("unittest-failure", "unittest-failure",
     [r"^FAILED \((?:failures|errors)=\d+", r"^(?:FAIL|ERROR): test"]),
]
UNCLASSIFIED = ("unclassified", "unclassified")


def classify(text):
    """Return {"mode", "anchor"} for a log's text (CRLF or LF)."""
    text = text.replace("\r\n", "\n")
    # drop the "2026-03-02T10:14:01.1021110Z " prefix so ^ anchors work
    lines = [re.sub(r"^\d{4}-\d\d-\d\dT[\d:.]+Z ", "", ln) for ln in text.split("\n")]
    body = "\n".join(lines)
    for mode, anchor, patterns in RULES:
        if any(re.search(p, body, re.M) for p in patterns):
            return {"mode": mode, "anchor": anchor}
    return {"mode": UNCLASSIFIED[0], "anchor": UNCLASSIFIED[1]}


def main(argv):
    if len(argv) != 1 or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if argv else 1
    result = classify(Path(argv[0]).read_text(encoding="utf-8", errors="replace"))
    print(f"{result['mode']} {result['anchor']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
