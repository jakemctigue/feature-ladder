#!/usr/bin/env python3
"""
Generate the repo's security evidence, deterministically and with the stdlib only:

  evidence/sbom.cdx.json    CycloneDX 1.5 SBOM: the plugin, each of its Python
                            scripts with a SHA-256, and the count of third-party
                            dependencies (expected 0).
  evidence/findings.sarif   SARIF 2.1.0 results of a small ast scan of those
                            scripts (shell=True, eval/exec, os.system, literals
                            that look like secrets).

  python3 generate_evidence.py            write both files
  python3 generate_evidence.py --check    exit 1 if the checked-in files are stale

Byte-deterministic: no timestamps, serial numbers or absolute paths; keys and
entries are sorted; files are hashed after \\r\\n -> \\n so Windows and Linux
agree. Scope is every non-test .py under plugins/. Nothing here runs during a
feature run and nothing touches the network.
"""
import ast
import hashlib
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
PLUGIN = REPO / "plugins" / "feature-ladder"
OUT = REPO / "evidence"
SBOM = "sbom.cdx.json"
SARIF = "findings.sarif"
TOOL = "feature-ladder-evidence"

SECRET_NAME = re.compile(r"(secret|token|passwd|password|api_?key|private_?key)", re.I)
SECRET_VALUE = re.compile(r"^(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|"
                          r"xox[baprs]-[A-Za-z0-9-]{10,}|[A-Za-z0-9+/_-]{32,}={0,2})$")

RULES = [
    ("FL001", "shell-true", "subprocess call with shell=True", "warning"),
    ("FL002", "eval-exec", "eval() or exec() call", "warning"),
    ("FL003", "os-system", "os.system() call", "warning"),
    ("FL004", "hardcoded-secret", "string literal that looks like a hardcoded secret", "error"),
]


def read_text(path):
    return Path(path).read_bytes().decode("utf-8", errors="replace").replace("\r\n", "\n")


def sources():
    """Non-test Python files under plugins/, as sorted repo-relative posix paths."""
    found = []
    for p in (REPO / "plugins").rglob("*.py"):
        rel = p.relative_to(REPO)
        if "__pycache__" in rel.parts or "tests" in rel.parts:
            continue
        found.append(rel.as_posix())
    return sorted(found)


def sha256(rel):
    return hashlib.sha256(read_text(REPO / rel).encode("utf-8")).hexdigest()


def third_party(files):
    local = {Path(f).stem for f in files}
    names = set()
    for rel in files:
        try:
            tree = ast.parse(read_text(REPO / rel))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    return sorted(n for n in names if n not in sys.stdlib_module_names and n not in local)


def build_sbom():
    files = sources()
    plugin = json.loads(read_text(PLUGIN / ".claude-plugin" / "plugin.json"))
    third = third_party(files)
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "metadata": {
            "component": {"type": "application", "bom-ref": "feature-ladder", "name": plugin["name"],
                          "version": plugin["version"], "licenses": [{"license": {"id": plugin["license"]}}]},
            "tools": {"components": [{"type": "application", "name": TOOL}]},
        },
        "components": [{"type": "file", "bom-ref": "file:" + rel, "name": rel,
                        "hashes": [{"alg": "SHA-256", "content": sha256(rel)}]} for rel in files],
        "dependencies": [{"ref": "feature-ladder", "dependsOn": []}],
        "properties": [{"name": "feature-ladder:third-party-dependencies", "value": str(len(third))},
                       {"name": "feature-ladder:third-party-names", "value": ",".join(third)}],
    }


def scan_file(rel):
    out = []
    try:
        tree = ast.parse(read_text(REPO / rel))
    except SyntaxError as e:
        return [("FL000", "error", f"syntax error: {e.msg}", e.lineno or 1)]
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
            owner = f.value.id if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) else ""
            if any(k.arg == "shell" and isinstance(k.value, ast.Constant) and k.value.value is True
                   for k in node.keywords):
                out.append(("FL001", node.lineno))
            if name in ("eval", "exec") and owner in ("", "builtins"):
                out.append(("FL002", node.lineno))
            if name == "system" and owner == "os":
                out.append(("FL003", node.lineno))
        targets, value = [], None
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            names = [t.id for t in targets if isinstance(t, ast.Name)]
            if any(SECRET_NAME.search(n) for n in names) and SECRET_VALUE.match(value.value):
                out.append(("FL004", node.lineno))
    return out


def build_sarif():
    meta = {r[0]: r for r in RULES}
    results = []
    for rel in sources():
        for item in scan_file(rel):
            rid, line = item[0], item[-1]
            level, text = (meta[rid][3], meta[rid][2]) if rid in meta else (item[1], item[2])
            results.append({"ruleId": rid, "level": level, "message": {"text": text},
                            "locations": [{"physicalLocation": {
                                "artifactLocation": {"uri": rel, "uriBaseId": "%SRCROOT%"},
                                "region": {"startLine": line}}}]})
    results.sort(key=lambda r: (r["locations"][0]["physicalLocation"]["artifactLocation"]["uri"],
                                r["locations"][0]["physicalLocation"]["region"]["startLine"], r["ruleId"]))
    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {"name": TOOL, "informationUri": "https://github.com/jakemctigue/feature-ladder",
                                "rules": [{"id": i, "name": n, "shortDescription": {"text": d},
                                           "defaultConfiguration": {"level": lv}} for i, n, d, lv in RULES]}},
            "originalUriBaseIds": {"%SRCROOT%": {"uri": "file:///"}},
            "results": results,
        }],
    }


def dumps(obj):
    return json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def build():
    """filename -> exact text that belongs in evidence/."""
    return {SBOM: dumps(build_sbom()), SARIF: dumps(build_sarif())}


def main(argv):
    if argv not in ([], ["--check"]):
        print(__doc__)
        return 1
    want = build()
    if argv == ["--check"]:
        stale = [n for n, text in want.items()
                 if not (OUT / n).is_file() or read_text(OUT / n) != text]
        if stale:
            print("stale evidence: " + ", ".join(stale) +
                  " - run python plugins/feature-ladder/scripts/evidence/generate_evidence.py and commit")
            return 1
        print("evidence is current")
        return 0
    OUT.mkdir(exist_ok=True)
    for n, text in want.items():
        (OUT / n).write_bytes(text.encode("utf-8"))
        print("wrote evidence/" + n)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
