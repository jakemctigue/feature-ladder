"""Tests for the outside-repo command classifier and the audit hook.

Since v1.11 the classifier is an audit log, not a gate: the former never-list
and production overwrites still get their tier (so the report lists them
first, review: pending) but they run. The one denial left is a command that
would print a secret value into the transcript.

Run from the repo root:  python3 -m unittest discover -s plugins/feature-ladder/tests
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN / "skills" / "orchestration-ladder" / "scripts"))

from irreversible import classify  # noqa: E402

RUN = {"active": True, "base": "main"}


def tier(cmd, run=RUN):
    return classify(cmd, run)["tier"]


class NeverList(unittest.TestCase):
    def test_plain_commands(self):
        for cmd in ["git push -f origin main", "git push --force origin master", "gh pr merge 3",
                    "gh repo delete me/x --yes", "terraform destroy", "gcloud projects delete p"]:
            self.assertEqual(tier(cmd), "never", cmd)

    def test_wrapped_in_a_shell(self):
        for cmd in ["bash -c 'git push --force origin main'", 'sh -c "git push --force origin main"',
                    "bash -lc 'gh pr merge 3'", "zsh -c 'terraform destroy'", "eval 'gh pr merge 3'",
                    'eval "gh repo delete me/x --yes"']:
            self.assertEqual(tier(cmd), "never", cmd)

    def test_command_substitution(self):
        for cmd in ['echo "$(gh repo delete me/x --yes)"', "echo `gh pr merge 1`",
                    'x="$(bash -c \'gh pr merge 1\')"']:
            self.assertEqual(tier(cmd), "never", cmd)

    def test_heredoc_fed_to_a_shell(self):
        self.assertEqual(tier("bash <<EOF\ngit push -f origin main\nEOF"), "never")
        self.assertEqual(tier("sh -s <<'EOF'\ngh pr merge 1\nEOF"), "never")

    def test_after_one_of_our_own_scripts(self):
        self.assertEqual(tier("python3 route.py '{}'; git push -f origin main"), "never")
        self.assertEqual(tier("python3 intake.py scan x && gh pr merge 2"), "never")

    def test_inside_with_secrets(self):
        self.assertEqual(tier("python3 with_secrets.py --secret K -- git push -f origin main"), "never")
        c = classify("python3 with_secrets.py --secret K -- firebase deploy --project prod", RUN)
        self.assertEqual((c["tier"], c["decision"]), ("mundane", "allow"))

    def test_recursive_delete_of_root_or_home(self):
        for cmd in ["rm -rf /", "rm -rf ~", "rm -fr ~/", "sudo rm -r -f $HOME", "rm --recursive /*",
                    "rm -rf ${HOME}"]:
            self.assertEqual(tier(cmd), "never", cmd)


class StillAllowed(unittest.TestCase):
    def test_ordinary_development(self):
        for cmd in ["rm -rf build/", "rm -rf ./node_modules /tmp/x", "rm ~/notes.txt", "npm test",
                    "git push -u origin feature-ladder/x", "bash -c 'npm test'", "echo $(date)"]:
            self.assertEqual(classify(cmd, RUN)["decision"], "allow", cmd)

    def test_text_that_only_mentions_a_command(self):
        for cmd in ['git commit -m "bash -c \'gh pr merge\'"', "echo '$(gh pr merge 1)'",
                    'git commit -m "mention \\`gh pr merge\\` and \\$(gh repo delete)"',
                    "grep 'DELETE FROM' x.sql", "cat > f.sql <<EOF\nDROP DATABASE x;\nEOF",
                    "cat <<EOF > f.sql\nDROP DATABASE x;\nEOF"]:
            self.assertEqual(tier(cmd), "none", cmd)

    def test_our_own_scripts_take_commands_as_arguments(self):
        cmd = "python3 irreversible.py classify \"bash -c 'gh pr merge 1'\""
        self.assertEqual(tier(cmd), "none")


class NotAGate(unittest.TestCase):
    """The former never-list and production overwrites run; they are labelled for review."""

    def test_never_list_runs_and_is_pending_review(self):
        for cmd in ["gh pr merge 3", "git push -f origin main", "bash -c 'gh pr merge 3'",
                    "terraform destroy", "python3 with_secrets.py --secret K -- git push -f origin main"]:
            c = classify(cmd, RUN)
            self.assertEqual((c["tier"], c["decision"], c["review"]), ("never", "allow", "pending"), cmd)

    def test_push_to_the_run_base_is_allowed_and_logged(self):
        c = classify("git push origin map-implementation", {"active": True, "base": "map-implementation"})
        self.assertEqual((c["tier"], c["decision"]), ("never", "allow"))

    def test_no_approve_step_remains(self):
        import irreversible
        self.assertFalse(hasattr(irreversible, "approve"))
        self.assertNotIn("approve --cmd", irreversible.__doc__)
        self.assertNotIn("VERDICT", irreversible.__doc__)
        self.assertNotIn("needs_verdict", irreversible.RANK)

    def test_policy_in_run_json_no_longer_denies(self):
        for policy in ("none", "nonprod", "all"):
            c = classify("firebase deploy --project prod", {"active": True, "base": "main", "outside_policy": policy})
            self.assertEqual(c["decision"], "allow", policy)


class RiskyAndSecrets(unittest.TestCase):
    def test_wrapped_production_delete_runs_and_is_pending_review(self):
        c = classify("bash -c 'firebase firestore:delete /users --project prod'", RUN)
        self.assertEqual((c["tier"], c["decision"], c["review"]), ("risky", "allow", "pending"))

    def test_mundane_deploy_is_logged_without_review(self):
        c = classify("firebase deploy --only hosting --project dev", RUN)
        self.assertEqual((c["tier"], c["decision"], c["review"]), ("mundane", "allow", "n/a"))

    def test_emulator_context_spans_segments(self):
        c = classify("export FIRESTORE_EMULATOR_HOST=localhost:8080 && firebase firestore:delete /users", RUN)
        self.assertEqual(c["environment"], "emulator")
        self.assertEqual(c["decision"], "allow")

    def test_wrapped_secret_read_is_denied(self):
        c = classify("sh -c 'gcloud secrets versions access latest --secret=X'", RUN)
        self.assertEqual((c["tier"], c["decision"]), ("secret", "deny"))


class GuardHook(unittest.TestCase):
    def run_hook(self, cmd):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / ".git").mkdir()
            (Path(d) / ".feature-ladder").mkdir()
            run_file = Path(d) / ".feature-ladder" / "run.json"
            run_file.write_text(json.dumps(RUN))
            payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": d})
            r = subprocess.run([sys.executable, str(PLUGIN / "hooks" / "scripts" / "guard_irreversible.py")],
                               input=payload, capture_output=True, text=True, timeout=30)
            self.assertEqual(r.returncode, 0, r.stderr)
            return r.stdout, json.loads(run_file.read_text(encoding="utf-8"))

    def test_logs_wrapped_force_push_and_lets_it_run(self):
        out, run = self.run_hook("bash -c 'git push --force origin main'")
        self.assertEqual(out.strip(), "")  # no deny: it runs
        actions = run["outside_actions"]
        self.assertEqual((actions[0]["tier"], actions[0]["review"]), ("never", "pending"))
        self.assertIn("review: pending", actions[0]["status"])

    def test_denies_only_a_secret_exposure(self):
        out, run = self.run_hook("cat .env")
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(run["outside_actions"][0]["tier"], "secret")

    def test_passes_ordinary_command(self):
        out, run = self.run_hook("npm test")
        self.assertEqual(out.strip(), "")
        self.assertNotIn("outside_actions", run)

    def test_pending_subcommand_lists_what_to_review(self):
        import irreversible
        run = dict(RUN)
        irreversible.record(run, "gh pr merge 3", irreversible.classify("gh pr merge 3", run), "ran")
        irreversible.record(run, "firebase deploy --project dev", irreversible.classify("firebase deploy --project dev", run), "ran")
        lines = irreversible.pending_lines(run)
        self.assertEqual(len(lines), 1)
        self.assertIn("gh pr merge 3", lines[0])


SECRET = "sk-proj-abcdefghijklmnopqrstuvwxyz0123"
GH_TOKEN = "ghp_abcdefghijklmnopqrstuvwxyz0123"
SCRIPT = PLUGIN / "skills" / "orchestration-ladder" / "scripts" / "irreversible.py"


class SecretsNeverStored(unittest.TestCase):
    """The audit log reaches FEATURE-REPORT.md and the PR body: no secret value may enter it."""

    def test_a_secret_on_the_command_line_is_denied_and_not_recorded(self):
        import irreversible
        for cmd in (f"OPENAI_API_KEY={SECRET} node scripts/migrate.js", f"GITHUB_TOKEN={GH_TOKEN} gh api user",
                    f"TOKEN={GH_TOKEN} gh api user", f'export STRIPE_SECRET_KEY="{SECRET}"',
                    f"docker run -e OPENROUTER_API_KEY={SECRET} img", f"gh auth login --with-token={GH_TOKEN}"):
            run = dict(RUN)
            ok, reason, changed = irreversible.gate(cmd, run)
            self.assertFalse(ok, cmd)
            self.assertTrue(changed)
            stored = json.dumps(run)
            self.assertNotIn(SECRET, stored, cmd)
            self.assertNotIn(GH_TOKEN, stored, cmd)
            self.assertNotIn(SECRET, reason)
            self.assertIn("<redacted>", run["outside_actions"][0]["cmd"])

    def test_record_redacts_whatever_classification_it_is_given(self):
        import irreversible
        import pr_checkpoint
        run = dict(RUN)
        c = {"tier": "risky", "label": "data script", "environment": "prod", "decision": "allow", "review": "pending"}
        irreversible.record(run, f"OPENAI_API_KEY={SECRET} node scripts/migrate.js --token {GH_TOKEN}", c, "ran")
        irreversible.record(run, f'curl -H "Authorization: Bearer {GH_TOKEN}" https://user:{SECRET}@example.com/x', c, "ran")
        for text in (json.dumps(run), "\n".join(irreversible.pending_lines(run)), pr_checkpoint.pending_note(run)):
            self.assertNotIn(SECRET, text)
            self.assertNotIn(GH_TOKEN, text)
        self.assertIn("node scripts/migrate.js", run["outside_actions"][0]["cmd"])  # the rest stays readable

    def test_rows_written_before_the_fix_are_redacted_when_rendered(self):
        import irreversible
        import pr_checkpoint
        run = dict(RUN, outside_actions=[{"at": "2026-01-01T00:00:00+00:00", "label": "data script", "environment": "prod",
                                          "tier": "risky", "status": "ran", "review": "pending",
                                          "cmd": f"OPENAI_API_KEY={SECRET} node scripts/migrate.js"}])
        for text in ("\n".join(irreversible.pending_lines(run)), pr_checkpoint.pending_note(run),
                     "\n".join(irreversible.report_lines(run))):
            self.assertNotIn(SECRET, text)
            self.assertIn("node scripts/migrate.js", text)

    def test_a_secret_flag_behind_another_flag_is_redacted(self):
        import irreversible
        for cmd, gone in (("node x.js --verbose --token abcdefgh12345678", "abcdefgh12345678"),
                          ("deploy --force --api-key=Zq9fP2mK7xLw4bT8 --region us", "Zq9fP2mK7xLw4bT8"),
                          ("mysql -u root --password hunter2hunter2 db", "hunter2hunter2"),
                          ("export db_password=correcthorsebattery", "correcthorsebattery"),
                          ("gh secret set API_KEY --body plainvalue123", "plainvalue123"),
                          ("curl -u admin:letmein123 https://example.com", "letmein123")):
            out = irreversible.redact(cmd)
            self.assertNotIn(gone, out, cmd)
            self.assertIn("<redacted>", out, cmd)
        self.assertEqual(irreversible.redact("node x.js --verbose --region us"), "node x.js --verbose --region us")

    def test_ordinary_values_are_left_readable(self):
        import irreversible
        for cmd in ("firebase deploy --only hosting --project my-app-staging", "NODE_ENV=production npm run migrate",
                    "KEYCLOAK_URL=http://localhost:8080/auth npm run seed"):
            self.assertEqual(irreversible.redact(cmd), cmd)


class SecretBypasses(unittest.TestCase):
    def test_quoted_and_qualified_spellings_are_denied(self):
        for cmd in ['echo "$OPENROUTER_API_KEY"', "echo ${GITHUB_TOKEN}", 'printf "%s" "$DB_PASSWORD"',
                    'cat ".env"', "cat '.env'", "cat ./config/.env.production", "cat < .env", "type .env",
                    "printenv OPENROUTER_API_KEY", "printenv", "env > /tmp/e.txt", "env", "env | grep KEY", "set",
                    "grep . .env", "grep -n API .env.local", "sed -n 1,5p .env", "sudo cat .env",
                    "for f in a b; do cat .env; done", "cat .env ledger.py",
                    "gcloud --project p secrets versions access latest --secret X",
                    'bash -c "echo \\"$STRIPE_SECRET\\""', 'x=$(cat ".env")']:
            c = classify(cmd, RUN)
            self.assertEqual((c["tier"], c["decision"]), ("secret", "deny"), cmd)

    def test_a_here_string_of_a_secret_variable_is_denied(self):
        for cmd in ('cat <<< "$API_KEY"', "base64 <<< $DB_PASSWORD"):
            c = classify(cmd, RUN)
            self.assertEqual((c["tier"], c["decision"]), ("secret", "deny"), cmd)
        self.assertEqual(tier('cat <<< "$HOME"'), "none")
        self.assertEqual(tier("cat > .env.example <<EOF\nAPI_KEY=\nEOF"), "none")  # writing a template prints nothing

    def test_only_a_call_to_one_of_our_scripts_is_skipped(self):
        self.assertEqual(tier("cat .env ledger.py"), "secret")
        self.assertEqual(tier("grep -r password verify.py .env"), "secret")
        self.assertEqual(tier("python3 irreversible.py classify \"cat .env\""), "none")
        self.assertEqual(tier("python ${CLAUDE_PLUGIN_ROOT}/skills/orchestration-ladder/scripts/verify.py run "
                              "--id T1 --check \"cat .env\""), "none")
        self.assertEqual(tier("git add verify.py && gh pr merge 3"), "never")
        self.assertEqual(tier("ls verify.py; gh pr merge 3"), "never")
        self.assertEqual(tier("cat verify.py | xargs gh pr merge"), "never")

    def test_with_secrets_is_the_way_through(self):
        c = classify('python3 with_secrets.py --secret MY_KEY -- node -e "console.log(1)"', RUN)
        self.assertEqual((c["tier"], c["decision"]), ("none", "allow"))


class SecretFalsePositives(unittest.TestCase):
    def test_harmless_commands_are_not_denied(self):
        for cmd in ["for key in a b c; do echo $key; done", "echo $token_count", "echo $TOKEN_COUNT",
                    "cat .env.example", "head -3 .env.sample", "cat config/.env.template",
                    "head -3 docs/setup.env.md", "KEYCLOAK_URL=http://localhost:8080/auth npm test",
                    "gcloud run deploy api --set-secrets=API_KEY=API_KEY:latest --project dev",
                    "pytest --keyfile=/home/me/certs/dev.pem", "TOKEN_FILE=/home/user/.config/tok.json npm test",
                    "API_KEY=$(python3 with_secrets.py get) npm test", "env NODE_ENV=test npm test", "set -euo pipefail",
                    "printenv PATH", "grep -q API_KEY .env", "grep -c . .env", "sed -i 's/a/b/' .env",
                    "cp .env.example .env", "test -f .env", "git commit -m \"cat .env\"", "grep printenv notes.md",
                    "echo 'echo $API_KEY'",
                    "git commit -m 'set GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz0123 in CI'"]:
            c = classify(cmd, RUN)
            self.assertEqual(c["decision"], "allow", cmd)
            self.assertNotEqual(c["tier"], "secret", cmd)


class Classifier(unittest.TestCase):
    def test_vercel_prod_is_a_deploy(self):
        for cmd in ("vercel --prod", "vercel deploy --prod", "netlify deploy --prod", "railway up"):
            c = classify(cmd, RUN)
            self.assertEqual((c["tier"], c.get("label")), ("mundane", "deploy"), cmd)

    def test_refspec_onto_main_is_a_push_to_main(self):
        for cmd in ("git push origin feature-ladder/x:main", "git push origin feature-ladder/x:master",
                    "git push origin feature-ladder/x main"):
            self.assertEqual(tier(cmd), "never", cmd)
        self.assertEqual(tier("git push -u origin feature-ladder/x"), "none")
        self.assertEqual(tier("git push origin feature-ladder/main-nav"), "none")


class Report(unittest.TestCase):
    def test_a_pipe_in_the_command_does_not_break_the_table(self):
        import irreversible
        run = dict(RUN)
        cmd = "gcloud run services list --project prod | grep api | xargs gcloud run services delete"
        irreversible.record(run, cmd, irreversible.classify(cmd, run), "ran")
        rows = irreversible.report_lines(run)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[2].replace("\\|", "").count("|"), rows[0].count("|"))
        self.assertIn("\\| grep api \\|", rows[2])

    def cli(self, root, *args, env=None):
        return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=str(root), capture_output=True, env=env, timeout=60)

    def repo(self, tmp, run_text):
        root = Path(tmp)
        subprocess.run(["git", "init", "-q"], cwd=str(root), check=True)
        (root / ".feature-ladder").mkdir()
        (root / ".feature-ladder" / "run.json").write_text(run_text, encoding="utf-8")
        return root

    def test_a_corrupt_run_json_is_one_line_not_a_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.repo(tmp, '{"active": true, "outside_actions": [{"cmd": "x')
            for sub in ("pending", "report"):
                r = self.cli(root, sub)
                text = (r.stdout + r.stderr).decode("utf-8", "replace")
                self.assertEqual(r.returncode, 1, sub)
                self.assertNotIn("Traceback", text)
                self.assertIn("run.json", text)
            r = self.cli(root, "classify", "gh pr merge 3")  # classification needs no run
            self.assertEqual(r.returncode, 0)
            self.assertEqual(json.loads(r.stdout)["tier"], "never")

    def test_non_cp1252_text_prints(self):
        import os
        with tempfile.TemporaryDirectory() as tmp:
            run = dict(RUN, outside_actions=[{"at": "2026-01-01T00:00:00+00:00", "cmd": "firebase deploy → ✔",
                                              "label": "firebase deploy", "environment": "prod", "tier": "mundane",
                                              "status": "ran", "review": "pending"}])
            root = self.repo(tmp, json.dumps(run))
            env = dict(os.environ, PYTHONIOENCODING="cp1252", PYTHONUTF8="0")
            for sub in ("pending", "report"):
                r = self.cli(root, sub, env=env)
                self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace"))
                self.assertIn("firebase deploy", r.stdout.decode("utf-8", "replace"))


if __name__ == "__main__":
    unittest.main()
