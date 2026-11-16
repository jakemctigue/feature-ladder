#!/usr/bin/env python3
"""Generate tests/fixtures/routing/golden_cases.json from the CURRENT router.

    python gen_routing.py            regenerate (review the diff before committing)
    python gen_routing.py --check    exit 1 if the committed fixture differs

Add or rename a case HERE, never in the JSON.
"""
import sys

import fixture_io

fixture_io.use("complexity-router", "orchestration-ladder")
import intake  # noqa: E402
import route  # noqa: E402

BIG = {"files": 8, "loc": 400, "modules": 3, "security": True, "concurrency": True, "novelty": 2}
X = dict(BIG, cross_system=True, irreversible=True, public_api=True, persistence=True)
CASES = [
    ("empty-input-defaults-to-code-score-0", {}),
    ("docs-kind-routes-by-kind", {"kind": "docs", "files": 1, "loc": 10}),
    ("fixture-kind-routes-by-kind", {"kind": "fixture", "files": 2, "loc": 40}),
    ("check-kind-routes-by-kind", {"kind": "check"}),
    ("research-kind-evidence-collector", {"kind": "research", "ambiguity": 1}),
    ("analysis-kind-domain-expert", {"kind": "analysis", "novelty": 2}),
    ("analysis-novelty-3-domain-expert-high", {"kind": "analysis", "novelty": 3}),
    ("code-band-0-3-trivial", {"kind": "code", "files": 1, "loc": 20}),
    ("code-band-4-6-small-glm-eligible", {"kind": "code", "files": 3, "loc": 100, "public_api": True, "no_tests": True}),
    ("code-band-4-6-upper-boundary-score-6", {"kind": "code", "files": 3, "loc": 100, "modules": 2, "security": True, "no_tests": True}),
    ("code-band-7-14-lower-boundary-leaves-glm", {"kind": "code", "files": 3, "loc": 100, "modules": 2, "security": True, "no_tests": True, "new_dependency": True}),
    ("code-band-7-14-ordinary-app-work", {"kind": "code", "files": 5, "loc": 200, "modules": 2, "persistence": True, "public_api": True}),
    ("code-band-15-19-sonnet-high", dict(BIG)),
    ("code-band-20-23-opus-medium-needs-design-step", dict(X)),
    ("code-band-20-23-opus-medium-design-agreed", dict(X, design_agreed=True)),
    ("code-band-24-plus-opus-high-needs-design-step", dict(X, blast_radius=2)),
    ("code-band-24-plus-opus-high-design-agreed", dict(X, blast_radius=2, design_agreed=True)),
    ("hard-flag-ambiguity-2-keeps-size-S", {"kind": "code", "files": 1, "loc": 20, "ambiguity": 2}),
    ("hard-flag-blast-radius-3", {"kind": "code", "files": 2, "loc": 50, "blast_radius": 3}),
    ("hard-flag-with-design-agreed-no-design-step", {"kind": "code", "files": 2, "loc": 50, "ambiguity": 3, "design_agreed": True}),
    ("irreversible-is-a-risk-weight-not-a-stop", {"kind": "code", "files": 1, "loc": 20, "irreversible": True}),
    ("string-and-null-inputs-are-tolerated", {"kind": "code", "files": "4", "loc": None, "security": "yes", "no_tests": "true", "concurrency": "no"}),
    ("size-M-score-11", {"kind": "code", "files": 8, "loc": 400, "modules": 3, "new_dependency": True, "public_api": True, "no_tests": True}),
    ("kind-is-case-insensitive", {"kind": "DOCS", "files": 1}),
]
DOC = ("Golden router cases. Each case records the CURRENT output of complexity-router/scripts/route.py (route.route) "
       "and intake.budget_for for the given task fields. If a deliberate router change breaks these, regenerate the "
       "expected values and review the diff; do not edit them to hide a regression.")


def build():
    out = {"_doc": DOC, "cases": []}
    for name, task in CASES:
        r = route.route(task)
        b = intake.budget_for(r, task)
        out["cases"].append({
            "name": name, "input": task,
            "expect": {k: r[k] for k in ("rung", "agent", "model", "effort", "provider", "score", "size",
                                         "design_first", "hard_flags", "breakdown")},
            "budget": {k: b[k] for k in ("size", "workers", "design", "coordinator_turns")}})
    return out


if __name__ == "__main__":
    sys.exit(fixture_io.emit(fixture_io.FIXTURES / "routing" / "golden_cases.json", build(), "--check" in sys.argv[1:]))
