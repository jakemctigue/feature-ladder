# CI troubleshooting

The `tests` workflow (`.github/workflows/tests.yml`) runs on ubuntu and windows
with Python 3.11 and 3.13. Each section below is one failure mode the repo's
log classifier knows. The sample logs in
`plugins/feature-ladder/tests/fixtures/ci_logs/` are hand-written
representatives, not captures; `scripts/evidence/ci_log_classifier.py` maps a
log to a mode and `tests/test_ci_logs.py` pins that mapping. Nothing here runs
during a feature run.

Classify a log you downloaded:

```
python plugins/feature-ladder/scripts/evidence/ci_log_classifier.py path/to/job.log
```

## unittest-failure

Sample: `unittest_failure.log`. The "Run tests" step ends with
`FAILED (failures=N)` or `FAILED (errors=N)` and a `FAIL:` / `ERROR:` block.

Fix: reproduce locally with the exact command,
`python -m unittest discover -v -s plugins/feature-ladder/tests`, then read the
first `FAIL:` block. If a golden case in `tests/fixtures/` is the failing
assertion, decide whether the script's behaviour changed on purpose (update the
fixture and review the diff) or by accident (fix the script).

## compileall-syntax-error

Sample: `compileall_syntax_error.log`. The "Compile every script" step prints
`*** Error compiling '<file>'...` and a `SyntaxError`.

Fix: `python -m compileall -q plugins` locally; the file and line are in the
log. Python 3.11 is the oldest version in the matrix, so syntax added in 3.12+
(for example nested same-quote f-strings) fails there first.

## python-version-missing

Sample: `setup_python_version_missing.log`. `actions/setup-python` reports
`The version 'X' with architecture 'x64' was not found`.

Fix: the `python` matrix entry in `tests.yml` names a version the runner image
does not have (a typo, or a pre-release). Use a released minor such as `"3.11"`
or `"3.13"`, quoted so YAML does not read `3.10` as `3.1`.

## import-error

Sample: `import_error.log`. `ModuleNotFoundError` or
`ImportError: Failed to import test module`.

Fix: the scripts have no third-party dependencies, so this is almost always a
path problem. Tests add the script directory to `sys.path` themselves (see the
top of `test_codex_triage.py`); a new test that imports a script needs the same
`sys.path.insert`. Check the case of the file name too, because Windows will
not catch a case mismatch that ubuntu does.

## windows-path-or-crlf

Sample: `windows_path_crlf.log`. Only the windows job fails, with
`UnicodeDecodeError: 'charmap' codec`, `[WinError N]`, or an assertion that
shows `\r\n` against `\n`.

Fix: open text files with `encoding="utf-8"`, compare text after normalising
`\r\n` to `\n`, and build paths with `pathlib` rather than string joins. For
hashes of checked-in files, normalise line endings before hashing (the evidence
generator does).

## timeout-or-cancelled

Sample: `timeout_cancelled.log`. `The operation was canceled` or
`has exceeded the maximum execution time`.

Fix: find the last test that printed before the gap in timestamps; a test that
waits on stdin, a subprocess or the network is the usual cause. Every
subprocess in the tests should pass `timeout=`. A run cancelled by a newer push
to the same PR looks identical and needs no fix.

## unclassified

Sample: `unclassified_checkout_network.log`. The classifier found no known
pattern, for example a transient `could not resolve host` while checking out.

Fix: re-run the job. If it repeats, read the first `##[error]` line, fix the
cause and, if it is likely to recur, add a rule and a sample log in the same
change.
