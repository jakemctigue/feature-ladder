"""hooks/guard-rs: the Rust build of the PreToolUse(Bash) audit hook must behave
exactly like guard_irreversible.py + irreversible.py, which stay the reference.

  - classify() and the stored (redacted) command agree with Python on every
    command string in the classifier tests, on edge cases, and on a seeded fuzz
  - the hook writes the same run.json bytes and prints the same denial
  - it shares the lock with Python hooks (no lost rows when both log at once)
  - a stale binary (Python reference edited since the build) and input Python
    would read differently are handed to the Python guard, never decided in Rust
  - guard.sh runs the binary when it exists and the Python guard when it does not

Skipped when the binary has not been built (bash hooks/scripts/build_guard.sh)
or is stale; CI builds it first.

Run from the repo root:  python3 -m unittest discover -s plugins/feature-ladder/tests
"""
import ast
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
SCRIPTS = PLUGIN / "skills" / "orchestration-ladder" / "scripts"
PY_GUARD = PLUGIN / "hooks" / "scripts" / "guard_irreversible.py"
GUARD_SH = PLUGIN / "hooks" / "scripts" / "guard.sh"
sys.path.insert(0, str(SCRIPTS))

import irreversible  # noqa: E402


def _binary():
    for name in ("guard_irreversible.exe", "guard_irreversible"):
        p = PLUGIN / "hooks" / "bin" / name
        if p.is_file():
            return p
    return None


BIN = _binary()
CURRENT = bool(BIN) and subprocess.run([str(BIN), "--check"], capture_output=True).returncode == 0
BASH = shutil.which("bash")
SKIP = "the Rust guard is not built or is stale (bash plugins/feature-ladder/hooks/scripts/build_guard.sh)"

RUNS = [
    {"active": True, "base": "main"},
    {"active": True, "base": "develop", "environments": {
        "production": ["acme-prod"], "nonproduction": ["acme-dev", "acme-staging"],
        "aliases": {"prod": "acme-prod", "dev": "acme-dev"}, "default": "prod"}},
    {"active": True, "base": "release/2.0", "environments": {"production": ["p"], "default": "demo-x"}},
]

SECRET = "sk-proj-abcdefghijklmnopqrstuvwxyz0123"
GH = "ghp_abcdefghijklmnopqrstuvwxyz0123"
EDGE = [
    "", " ", "\n", "#", "# gh pr merge 1", "echo hi # gh pr merge 1", "echo hi#gh pr merge 1",
    "git push origin feature-ladder/x:main", "git push origin HEAD:refs/heads/develop", "git push origin develop",
    "git push origin release/2.0", "git push -f origin +main", "git push origin :master", "git push --delete origin main",
    "rm -rf $HOME/", "rm -r -f ~/*", "rm -rf /tmp/x", "RM -RF /", "sudo -E env -i rm -rf /",
    "cat < .env", "cat .env.example", "cat config/.env.production", "grep -q KEY .env", "grep -c x .env",
    "sed -i s/a/b/ .env", "sed s/a/b/ .env", "rg --count TOKEN .env", "printenv", "printenv HOME", "printenv OPENAI_API_KEY",
    "env", "env > out.txt", "env FOO=1 node x.js", "export -p", "export FOO=1", "declare -x", "set", "set -e",
    "echo $OPENAI_API_KEY", "echo '$OPENAI_API_KEY'", 'echo "$OPENAI_API_KEY"', "echo \\$OPENAI_API_KEY",
    "echo ${DB_PASSWORD}", "echo $TOKEN_FILE", "echo $token", "printf '%s' \"$X_SECRET\"", "cat <<< \"$X_KEY\"",
    f"OPENAI_API_KEY={SECRET} node scripts/migrate.js", f"export STRIPE_SECRET_KEY=\"{SECRET}\"",
    f"gh auth login --with-token={GH}", f"curl --token {GH} x", f"curl --api-key {SECRET}", "curl --api-key short",
    f"curl -H \"Authorization: Bearer {GH}\" https://user:{SECRET}@example.com/x", f"curl -u admin:{SECRET} x",
    f"gh secret set X --body {SECRET}", f"gh secret set X -b={SECRET}", f"x=AbCdEfGhIjKlMnOpQrStUvWx12 y",
    "TOKEN=/path/to/file.txt run", "API_KEY=$(cat k) x", "API_KEY=C:\\keys\\k.pem x", "API_KEY=https://x.y/abcdefghijkl",
    "firebase deploy", "firebase deploy --project prod", "firebase deploy -P dev", "firebase deploy --project=acme-staging",
    "firebase deploy --only firestore:indexes", "firebase deploy --only firestore:indexes --force",
    "firebase hosting:channel:deploy pr-1", "firebase firestore:delete -r /users --project acme-prod",
    "firebase firestore:delete --project demo-test /x", "FIRESTORE_EMULATOR_HOST=localhost:8080 node scripts/seed.js",
    "node scripts/migrate-users.js", "node tests/migrate.test.js", "python3 scripts/cleanup_old.py --dry-run",
    "npx tsx scripts/backfill.ts", "npm run db:reset", "pnpm run seed", "yarn migrate:prod", "npm run build",
    "psql -c 'DELETE FROM users'", "psql -c DELETE FROM users", "mysql -e \"drop table x\"", "UPDATE users SET a=1",
    "terraform apply", "terraform destroy -auto-approve", "pulumi up --stack prod", "kubectl delete pod x",
    "kubectl apply -f k.yaml --context staging", "helm upgrade x", "docker push img", "cargo publish", "twine upload dist/*",
    "vercel --prod", "vercel deploy", "netlify deploy --prod", "fly deploy", "railway up", "wrangler deploy",
    "gh release create v1", "aws s3 rm s3://b/k", "aws ec2 terminate-instances --instance-ids i-1", "az group delete -n x",
    "gcloud run services delete svc --project acme-dev", "gsutil -m rm gs://b/backup/x", "gcloud storage rm gs://x",
    "gcloud secrets versions access latest --secret=X", "gcloud secrets create X", "firebase functions:secrets:access X",
    "firebase functions:config:get", "gcloud projects delete p", "gcloud firestore databases update --no-enable-pitr",
    "bash -c 'gh pr merge 1'", "bash -lc \"git push -f origin main\"", "eval \"gh repo delete x/y --yes\"",
    "echo $(gh pr merge 1)", "echo '$(gh pr merge 1)'", "echo \\$(gh pr merge 1)", "echo `terraform destroy`",
    "bash <<EOF\ngh pr merge 1\nEOF", "cat <<'EOF'\ngh pr merge 1\nEOF", "sh -s <<-X\n\tgit push -f origin main\n\tX",
    "python3 route.py '{}'; git push -f origin main", "python3 intake.py scan && gh pr merge 2",
    "bash hooks/scripts/py.sh skills/x/irreversible.py classify 'gh pr merge 1'", "py -3 verify.py",
    "python3 with_secrets.py --secret K -- firebase deploy --project prod", "python with_secrets.py -- cat .env",
    "C:\\Python315\\python.exe C:\\x\\route.py --describe y", "/usr/bin/env python3 brief.py", "pypy3.10 ledger.py",
    "git commit -m \"gh pr merge 1\"", "git commit -m 'cat .env'", "grep \"DELETE FROM\" -r .", "echo 'drop table x'",
    "echo \"unterminated", "echo 'unterminated", "echo \\", "a\\\nb", "x='a'\"b\"c", "(cd x && gh pr merge 1)",
    "{ gh pr merge 1; }", "! gh pr merge 1", "time gh pr merge 1", "nohup terraform destroy &", "a | b || c && d ; e",
    "2>&1 cat .env", "cat 2> err .env", "cat > .env", ">.env cat", "cat .env 2>/dev/null | head",
    "Get-Content .env", "type .env", "TYPE .ENV", "cat .ENV", "cat ./config/prod.env", "cat app.env.md",
    "echo café $ÉTÉ_TOKEN", "echo $ÉTÉ_TOKEN", "gh pr merge ١", "git push -f origin main\u00a0", "echo \u3000$X_KEY",
    "echo \x1c gh pr merge 1", "cat\x1d.env", "πython3 route.py x; gh pr merge 1", "ﬁrebase deploy",
    "echo 😀 && firebase deploy --project prod", "x=\"$(bash -c \\\"gh pr merge 1\\\")\"",
    "deploy() { firebase deploy; }; deploy", "git push origin main\n", "export API_KEY\nprintenv API_KEY",
    # Python's `$` also matches before a final newline (a quoted one survives into the token):
    "API_KEY='keys/abcdefghijklmn.pem\n' node x.js", "x='AbCdEfGhIjKlMnOpQrStUvWx12\n' y",
    # re.IGNORECASE also matches İ ı to i, ſ to s, the Kelvin sign to k; \s is str.isspace():
    "gİt push -f origin main", "gıt push -f origin main", "Kubectl delete pod x",
    "gſutil rm gs://b/backup/x", "FIREBAſE DEPLOY --project acme-prod", "gh\x1cpr\x1fmerge 1",
    "rm -ırf /", "terraform\x1edestroy", "echo $X_KEY", "printenv API_İD",
]


def literals(*names):
    """Every string constant in the given test modules: each is a command to try."""
    out = set()
    for name in names:
        tree = ast.parse((TESTS / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and len(node.value) < 2000:
                out.add(node.value)
    return sorted(out)


PIECES = [
    "git", "push", "-f", "--force", "origin", "main", "master", "develop", "release/2.0", "HEAD:main", "+main",
    "gh", "pr", "merge", "repo", "delete", "rm", "-rf", "-r", "/", "~", "$HOME", "cat", "grep", "-q", ".env",
    ".env.local", ".env.example", "echo", "printf", "printenv", "env", "export", "-p", "$X_KEY", "${DB_PASSWORD}",
    "$TOKEN_FILE", "OPENAI_API_KEY=" + SECRET, "--token", GH, "--api-key=" + SECRET, "Bearer", GH,
    "firebase", "deploy", "--project", "prod", "dev", "acme-prod", "acme-dev", "--dry-run", "firestore:delete",
    "gcloud", "secrets", "versions", "access", "node", "scripts/migrate.js", "tests/seed.js", "npm", "run", "seed",
    "terraform", "destroy", "apply", "kubectl", "delete", "DELETE", "FROM", "users", "drop", "table", "update", "set",
    "bash", "-c", "sh", "-lc", "eval", "python3", "route.py", "with_secrets.py", "--", "py.sh", "irreversible.py",
    "vercel", "--prod", "aws", "s3", "rm", "localhost", "demo-x", "staging", "x", "y", "é", "😀", "1", "2>", "<",
    ">", ">>", "<<<", "<<EOF", "EOF", "|", "||", "&&", ";", "&", "\n", "#", "(", ")", "{", "}", "!", "$(", "`",
    "'", '"', "\\", "'gh pr merge 1'", '"git push -f origin main"', "'cat .env'", "\"$X_SECRET\"", "a=b", "c='d e'",
]


def fuzz(n, seed=20261123):
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        k = rng.randint(1, 14)
        words = [rng.choice(PIECES) for _ in range(k)]
        glue = rng.choice([" ", " ", " ", "", "  "])
        out.append(glue.join(words))
    return out


WRAPS = ["bash -c '{}'", 'sh -c "{}"', "eval '{}'", "echo $({})", "echo `{}`", "x=\"$({})\"", "bash <<EOF\n{}\nEOF",
         "cat <<'EOF'\n{}\nEOF", "python3 with_secrets.py --secret K -- {}", "python3 route.py x; {}", "sudo {}", "({})",
         "{} # trailing", "{} 2>&1 | tee log", "FOO=1 {}", "time {}", "'{}'", '"{}"', "{} && {}"]


def mutate(corpus, n, seed=1123):
    """Real commands, recombined: joined, wrapped, re-cased, spliced with pieces."""
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        cmd = rng.choice(corpus)
        for _ in range(rng.randint(1, 3)):
            op = rng.randrange(6)
            if op == 0:
                cmd = cmd + rng.choice([" ; ", " && ", " | ", "\n", " & ", " || "]) + rng.choice(corpus)
            elif op == 1:
                cmd = rng.choice(WRAPS).replace("{}", cmd)
            elif op == 2 and cmd:
                i = rng.randrange(len(cmd))
                cmd = cmd[:i] + rng.choice([" ", ""]) + rng.choice(PIECES) + " " + cmd[i:]
            elif op == 3:
                cmd = cmd.upper() if rng.random() < 0.3 else cmd.swapcase() if rng.random() < 0.5 else cmd
            elif op == 4 and len(cmd) > 2:
                i = rng.randrange(len(cmd))
                cmd = cmd[:i] + cmd[i + 1:]
            else:
                cmd = cmd.replace(" ", rng.choice(["  ", "\t", " "]))
        out.append(cmd)
    return out


def python_line(cmd, run):
    return json.dumps({"classify": irreversible.classify(cmd, run), "cmd": irreversible.redact(irreversible.norm(cmd))[:300]})


def rust_lines(items):
    data = "".join(json.dumps({"cmd": c, "run": r}) + "\n" for c, r in items)
    r = subprocess.run([str(BIN), "--classify-batch"], input=data.encode("utf-8"), capture_output=True, timeout=300)
    if r.returncode != 0:
        raise AssertionError(r.stderr.decode("utf-8", "replace"))
    return r.stdout.decode("utf-8").splitlines()


@unittest.skipUnless(BIN and CURRENT, SKIP)
class ClassifierParity(unittest.TestCase):
    def check(self, cmds):
        items = [(c, run) for c in cmds for run in RUNS]
        got = rust_lines(items)
        self.assertEqual(len(got), len(items))
        diffs = []
        for (cmd, run), line in zip(items, got):
            want = python_line(cmd, run)
            if line != want:
                diffs.append(f"{cmd!r} base={run.get('base')}\n  python: {want}\n  rust:   {line}")
        self.assertFalse(diffs, f"{len(diffs)} of {len(items)} differ:\n" + "\n".join(diffs[:15]))

    def test_every_command_in_the_classifier_tests(self):
        self.check(literals("test_irreversible.py", "test_guard_irreversible.py", "test_with_secrets.py",
                            "test_pr_checkpoint.py", "test_no_bs_silk.py"))

    def test_edge_cases(self):
        self.check(EDGE)

    def test_seeded_fuzz(self):
        self.check(fuzz(3000))

    def test_recombined_real_commands(self):
        corpus = EDGE + literals("test_irreversible.py", "test_with_secrets.py")
        self.check(mutate(corpus, 3000))


def make_repo(base):
    root = Path(base) / "main"
    root.mkdir()
    (root / ".git").mkdir()
    return root


def hook_payload(cwd, command, **extra):
    p = {"tool_name": "Bash", "cwd": str(cwd), "tool_input": {"command": command}}
    p.update(extra)
    return json.dumps(p)


def run_hook(argv, payload, env=None):
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    if env is None:
        # The Python hook always runs through py.sh, which sets PYTHONUTF8=1; run it
        # the same way so stdin is UTF-8 on Windows too (not the console code page).
        env = {k: v for k, v in os.environ.items() if k != "PYTHONIOENCODING"}
        env["PYTHONUTF8"] = "1"
    r = subprocess.run(argv, input=payload, capture_output=True, timeout=120, env=env)
    return r.returncode, r.stdout.decode("utf-8").replace("\r\n", "\n"), r.stderr.decode("utf-8", "replace")


def normalised(text):
    run = json.loads(text)
    for a in run.get("outside_actions", []) if isinstance(run, dict) else []:
        if isinstance(a, dict) and "at" in a:
            a["at"] = "T"
    return json.dumps(run, indent=2)


@unittest.skipUnless(BIN and CURRENT, SKIP)
class HookParity(unittest.TestCase):
    """The same payload through the Python hook and the Rust hook, in twin repos."""

    def twin(self, run_text, command, **extra):
        outs = []
        for argv in ([sys.executable, str(PY_GUARD)], [str(BIN)]):
            with tempfile.TemporaryDirectory() as d:
                root = make_repo(d)
                (root / ".feature-ladder").mkdir()
                f = root / ".feature-ladder" / "run.json"
                f.write_bytes(run_text.encode("utf-8"))
                (root / "src").mkdir()
                code, out, err = run_hook(argv, hook_payload(root / "src", command, **extra))
                text = f.read_text(encoding="utf-8")
                left = sorted(p.name for p in (root / ".feature-ladder").iterdir())
                outs.append((code, out, text if text == run_text else normalised(text), left, err))
        return outs

    def assert_same(self, run_text, command, **extra):
        (pc, pout, prun, pleft, perr), (rc, rout, rrun, rleft, rerr) = self.twin(run_text, command, **extra)
        self.assertEqual((rc, rout, rleft), (pc, pout, pleft), f"{command!r}\n{rerr}")
        self.assertEqual(rrun, prun, command)
        self.assertEqual(rerr, "", command)

    def test_same_output_and_run_file(self):
        run_texts = [json.dumps(RUNS[0]), json.dumps(RUNS[1], indent=2),
                     '\ufeff{"active": 1, "base": "", "x": [1.5, 1e16, -0.0, 12345678901234567890, null, "\\u00e9\\ud83d\\ude00"]}',
                     json.dumps({"active": True, "outside_actions": [{"cmd": "old", "at": "2026-01-01T00:00:00+00:00"}]})]
        cmds = ["gh pr merge 3", "cat .env", "npm test", f"OPENAI_API_KEY={SECRET} node scripts/migrate.js",
                "firebase firestore:delete -r /x --project acme-prod", "bash -c 'git push --force origin main'",
                "echo café 😀 && firebase deploy --project prod"]
        for t in run_texts:
            for c in cmds:
                with self.subTest(run=t[:40], cmd=c):
                    self.assert_same(t, c)

    def test_inactive_corrupt_and_odd_run_files_are_left_alone(self):
        for t in ['{"active": false}', '{"active": true, "slug": "x', "[]", "", '{"active": ""}', "null"]:
            with self.subTest(run=t):
                self.assert_same(t, "gh pr merge 1")

    def test_odd_payloads(self):
        with tempfile.TemporaryDirectory() as d:
            root = make_repo(d)
            (root / ".feature-ladder").mkdir()
            (root / ".feature-ladder" / "run.json").write_text(json.dumps(RUNS[0]), encoding="utf-8")
            for payload in ["", "not json", "[]", '{"tool_name": "Read"}', '{"tool_name": "Bash"}',
                            json.dumps({"tool_name": "Bash", "tool_input": "x", "cwd": str(root)}),
                            json.dumps({"tool_name": "Bash", "tool_input": {"command": ""}, "cwd": str(root)}),
                            json.dumps({"tool_name": "Bash", "tool_input": {"command": "cat .env"}, "cwd": 5}),
                            "\ufeff\ufeff" + hook_payload(root, "printenv")]:
                with self.subTest(payload=payload[:60]):
                    py = run_hook([sys.executable, str(PY_GUARD)], payload)
                    rs = run_hook([str(BIN)], payload)
                    self.assertEqual(rs[:2], py[:2])

    def test_unsupported_input_is_handed_to_python(self):
        """A lone surrogate is something Rust strings cannot hold: Python decides."""
        if not BASH:
            self.skipTest("no bash")
        run_text = json.dumps(RUNS[0])
        (pc, pout, prun, _, _), (rc, rout, rrun, _, rerr) = self.twin(run_text, "echo \ud800 && cat .env")
        self.assertEqual((rc, rout, rrun), (pc, pout, prun))
        self.assertIn('"permissionDecision": "deny"', rout)


@unittest.skipUnless(BIN and CURRENT, SKIP)
class SharedState(unittest.TestCase):
    def test_a_linked_worktree_logs_into_the_main_checkout(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve() / "main"
            root.mkdir()
            git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]
            subprocess.run(git + ["init", "-q"], cwd=root, check=True)
            subprocess.run(git + ["commit", "-q", "--allow-empty", "-m", "i"], cwd=root, check=True)
            subprocess.run(git + ["worktree", "add", "-q", str(Path(d) / "wt")], cwd=root, check=True)
            (root / ".feature-ladder").mkdir()
            f = root / ".feature-ladder" / "run.json"
            f.write_text(json.dumps(RUNS[0]), encoding="utf-8")
            run_hook([str(BIN)], hook_payload(Path(d) / "wt", "gh pr merge 9"))
            self.assertEqual(json.loads(f.read_text(encoding="utf-8"))["outside_actions"][0]["cmd"], "gh pr merge 9")

    def test_python_and_rust_hooks_logging_at_once_lose_no_row(self):
        with tempfile.TemporaryDirectory() as d:
            root = make_repo(d)
            (root / ".feature-ladder").mkdir()
            f = root / ".feature-ladder" / "run.json"
            f.write_text(json.dumps(RUNS[0]), encoding="utf-8")
            jobs = []
            for i in range(12):
                argv = [str(BIN)] if i % 2 else [sys.executable, str(PY_GUARD)]
                jobs.append((argv, hook_payload(root, f"firebase deploy --only functions:f{i} --project prod-app")))
            with ThreadPoolExecutor(max_workers=12) as pool:
                list(pool.map(lambda j: run_hook(*j), jobs))
            logged = {a["cmd"] for a in json.loads(f.read_text(encoding="utf-8"))["outside_actions"]}
            self.assertEqual(len(logged), 12, logged)
            self.assertFalse((root / ".feature-ladder" / "state.lock").exists())

    def test_a_stale_lock_is_taken_over(self):
        with tempfile.TemporaryDirectory() as d:
            root = make_repo(d)
            (root / ".feature-ladder").mkdir()
            f = root / ".feature-ladder" / "run.json"
            f.write_text(json.dumps(RUNS[0]), encoding="utf-8")
            lock = root / ".feature-ladder" / "state.lock"
            lock.write_text("99999")
            old = lock.stat().st_mtime - 120
            os.utime(lock, (old, old))
            run_hook([str(BIN)], hook_payload(root, "gh pr merge 1"))
            self.assertIn("outside_actions", json.loads(f.read_text(encoding="utf-8")))
            self.assertFalse(lock.exists())


def plugin_copy(dest, with_binary=True):
    """A minimal copy of the plugin: the hook scripts, the reference, the binary."""
    for rel in ("hooks/scripts", "skills/orchestration-ladder/scripts"):
        shutil.copytree(PLUGIN / rel, Path(dest) / rel, ignore=shutil.ignore_patterns("__pycache__"))
    if with_binary:
        (Path(dest) / "hooks" / "bin").mkdir(parents=True)
        shutil.copy2(BIN, Path(dest) / "hooks" / "bin" / BIN.name)
    return Path(dest)


@unittest.skipUnless(BIN and CURRENT and BASH, SKIP + " (and bash on PATH)")
class Launcher(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = make_repo(Path(self.tmp.name))
        (self.repo / ".feature-ladder").mkdir()
        self.run_file = self.repo / ".feature-ladder" / "run.json"
        self.run_file.write_text(json.dumps(RUNS[0]), encoding="utf-8")
        self.env = dict(os.environ, FEATURE_LADDER_GUARD_TRACE="1")

    def tearDown(self):
        self.tmp.cleanup()

    def guard_sh(self, plugin, command):
        sh = (plugin / "hooks" / "scripts" / "guard.sh").as_posix()
        return run_hook([BASH, sh], hook_payload(self.repo, command), env=self.env)

    def test_guard_sh_runs_the_binary_and_denies(self):
        with tempfile.TemporaryDirectory() as d:
            code, out, err = self.guard_sh(plugin_copy(d), "cat .env")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(err, "")  # decided in Rust: nothing handed to Python

    def test_without_the_binary_guard_sh_runs_the_python_guard(self):
        with tempfile.TemporaryDirectory() as d:
            code, out, err = self.guard_sh(plugin_copy(d, with_binary=False), "cat .env")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertNotIn("guard_irreversible:", err)

    def test_a_stale_binary_hands_every_call_to_python(self):
        with tempfile.TemporaryDirectory() as d:
            plugin = plugin_copy(d)
            ref = plugin / "skills" / "orchestration-ladder" / "scripts" / "irreversible.py"
            ref.write_text(ref.read_text(encoding="utf-8") + "\n# edited after the build\n", encoding="utf-8")
            self.assertEqual(subprocess.run([str(plugin / "hooks" / "bin" / BIN.name), "--check"],
                                            capture_output=True).returncode, 1)
            code, out, err = self.guard_sh(plugin, "gh pr merge 4")
        self.assertEqual((code, out), (0, ""))
        self.assertIn("reference changed", err)
        rows = json.loads(self.run_file.read_text(encoding="utf-8"))["outside_actions"]
        self.assertEqual([(r["cmd"], r["tier"]) for r in rows], [("gh pr merge 4", "never")])


if __name__ == "__main__":
    unittest.main()
