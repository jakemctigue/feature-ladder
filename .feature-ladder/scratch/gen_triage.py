import json
import sys

sys.path.insert(0, "skills/cross-model-review/scripts")
from codex_review import triage

F = lambda sev, file, line, title: {"severity": sev, "file": file, "line": line, "title": title}
cases = [
    ("no-findings", []),
    ("only-low-findings-are-informational", [F("low", "a.py", 1, "naming"), F("low", "b.py", 2, "comment typo")]),
    ("ordered-by-severity-then-file-then-line", [F("medium", "b.py", 9, "m-b9"), F("critical", "z.py", 1, "crit"), F("high", "a.py", 20, "h-a20"), F("high", "a.py", 3, "h-a3"), F("medium", "a.py", 50, "m-a50")]),
    ("same-file-same-title-modulo-case-and-spacing-is-answered", [F("high", "a.py", 10, "Unchecked None"), F("medium", "a.py", 42, "unchecked   NONE")]),
    ("same-file-same-line-different-title-is-answered", [F("medium", "a.py", 7, "off by one"), F("medium", "a.py", 7, "loop bound wrong")]),
    ("same-title-in-different-files-are-distinct", [F("medium", "a.py", 1, "missing timeout"), F("medium", "b.py", 1, "missing timeout")]),
    ("line-zero-never-dedupes-on-line", [F("medium", "a.py", 0, "first"), F("medium", "a.py", 0, "second")]),
    ("low-duplicate-of-a-higher-finding-is-answered-not-informational", [F("high", "a.py", 5, "race"), F("low", "a.py", 5, "minor race note")]),
    ("duplicate-low-findings-count-once-as-answered", [F("low", "a.py", 4, "style"), F("low", "a.py", 9, "style")]),
    ("three-way-duplicate-keeps-the-most-severe", [F("medium", "a.py", 3, "leak"), F("critical", "a.py", 8, "Leak"), F("high", "a.py", 12, "leak ")]),
    ("unknown-severity-sorts-last-but-stays-actionable", [F("info", "a.py", 1, "fyi"), F("medium", "a.py", 2, "real")]),
    ("missing-file-and-line-fields-do-not-crash", [{"severity": "high", "title": "no location"}, {"severity": "low", "title": "no location either"}]),
]
out = {
    "_doc": (
        "Golden cases for codex_review.triage(). Class mapping onto the classes the code really has: "
        "'actionable' = needs a response (critical/high/medium, and any unknown severity); "
        "'low' = informational (listed in the record, never acted on); "
        "'duplicates' = answered (a finding that repeats an earlier one is collapsed into it and only counted). "
        "There is NO 'no-response' class in the code: every non-duplicate, non-low finding is actionable, so no case asserts one. "
        "Findings are [severity, file, line, title] objects; expected values are the CURRENT classifier output."
    ),
    "cases": [],
}
for name, findings in cases:
    t = triage(findings)
    out["cases"].append({
        "name": name,
        "findings": findings,
        "expect": {
            "actionable": [x["title"] for x in t["actionable"]],
            "low": [x["title"] for x in t["low"]],
            "duplicates": t["duplicates"],
        },
    })
    print(name, out["cases"][-1]["expect"])
with open("tests/fixtures/codex_triage/cases.json", "w", encoding="utf-8", newline="\n") as fh:
    json.dump(out, fh, indent=2, sort_keys=True)
    fh.write("\n")
