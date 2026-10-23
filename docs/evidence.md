# Evidence

Checked-in, deterministic evidence about this repo. Nothing here calls a model
or the network, and nothing runs during a feature run.

| File | What it is |
|---|---|
| `evidence/sbom.cdx.json` | CycloneDX 1.5 SBOM: the plugin (name, version, license), every non-test Python file under `plugins/` with a SHA-256, and the third-party dependency count (0) |
| `evidence/findings.sarif` | SARIF 2.1.0 from a stdlib `ast` scan of the same files: `shell=True` (FL001), `eval`/`exec` (FL002), `os.system` (FL003), literals that look like secrets (FL004) |

A finding is a lead to read, not a verdict. The scan is deliberately small and
is not a substitute for a security review or a tool such as AgentShield.

## Regenerate and check

```
python plugins/feature-ladder/scripts/evidence/generate_evidence.py          # write both files
python plugins/feature-ladder/scripts/evidence/generate_evidence.py --check  # exit 1 if stale
```

CI runs `--check` after the tests, and `tests/test_evidence.py` asserts the same.
The output is byte-for-byte reproducible: no timestamps, serial numbers or
absolute paths, sorted keys and entries, and files are hashed after `\r\n` is
normalised to `\n`, so Windows and Linux agree.

**Regenerate after any change to a script under `plugins/` and after every
version bump**: the SBOM records the plugin version and each script's hash, so
`--check` fails until the files are rewritten and committed. `.gitattributes`
keeps `evidence/` and the test fixtures LF.

## Other corpora (all under `plugins/feature-ladder/tests/`)

- `fixtures/routing/` router and budget goldens; `fixtures/skill_ranking/`
  selector ordering; `fixtures/pr_checkpoint/` branch/PR decision cases;
  `fixtures/codex_triage/` triage classes (informational = low, answered =
  duplicates, actionable = needs a response); `fixtures/ci_logs/` sample CI
  failures (see `docs/ci-troubleshooting.md`).
- `docs/harness-compat.md` lists which surfaces are tested (Claude Code, Claude
  app) and which are `untested`.
