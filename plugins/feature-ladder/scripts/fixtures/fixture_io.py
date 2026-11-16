"""Shared by the gen_*.py fixture generators: where the plugin lives, and one
way to write (or check) a golden fixture.

Fixtures are JSON with sorted keys, two-space indent and LF endings, so a
regeneration is byte-identical on every platform. Never hand-edit a fixture:
change the generator's case list and run it, or the edit is lost on the next
regeneration (`--check` in the test suite catches the drift).
"""
import json
import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[2]          # .../plugins/feature-ladder
FIXTURES = PLUGIN / "tests" / "fixtures"


def use(*skills):
    """Put the scripts of the named skills on sys.path (works from any cwd)."""
    for name in skills:
        sys.path.insert(0, str(PLUGIN / "skills" / name / "scripts"))


def emit(path, data, check=False):
    """Write the fixture, or with check=True compare it with what is on disk.
    Returns the exit code: 0 written or current, 1 missing or out of date."""
    path = Path(path)
    text = json.dumps(data, indent=2, sort_keys=True) + "\n"
    if check:
        try:
            current = path.read_bytes().decode("utf-8").replace("\r\n", "\n")
        except OSError:
            current = None
        if current != text:
            print(f"{path} is out of date: run {Path(sys.argv[0]).name} (without --check) and review the diff")
            return 1
        print(f"{path.name} is current")
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    print(f"wrote {path}")
    return 0
