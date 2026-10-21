"""Run tests in a disposable workspace, including Windows restricted-token hosts."""
import os
import shutil
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / '.test-work'


def workspace_temp(suffix=None, prefix=None, dir=None):
    # Windows Python's mode=0700 tempfile ACL omits sandbox restricted SIDs.
    # Inherit the authorized workspace ACL when creating these test-only dirs.
    parent = SCRATCH if dir is None else Path(dir)
    parent.mkdir(parents=True, exist_ok=True)
    path = parent / ((prefix or 'tmp') + uuid.uuid4().hex + (suffix or ''))
    path.mkdir()
    return str(path)


if __name__ == '__main__':
    if os.name == 'nt':
        tempfile.mkdtemp = workspace_temp
    directory = sys.argv[1] if len(sys.argv) > 1 else str(ROOT / 'plugins/feature-ladder-gpt/tests')
    suite = unittest.defaultTestLoader.discover(directory)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
