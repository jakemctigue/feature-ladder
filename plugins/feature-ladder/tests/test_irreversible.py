"""Tests for the irreversible-command classifier and the guard hook.

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
        cmd = "python3 irreversible.py approve --cmd \"bash -c 'gh pr merge 1'\" --verdict-file v"
        self.assertEqual(tier(cmd), "none")


class RiskyAndSecrets(unittest.TestCase):
    def test_wrapped_production_delete_needs_a_verdict(self):
        c = classify("bash -c 'firebase firestore:delete /users --project prod'", RUN)
        self.assertEqual((c["tier"], c["decision"]), ("risky", "needs_verdict"))

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
            (Path(d) / ".feature-ladder" / "run.json").write_text(json.dumps(RUN))
            payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": d})
            r = subprocess.run([sys.executable, str(PLUGIN / "hooks" / "scripts" / "guard_irreversible.py")],
                               input=payload, capture_output=True, text=True, timeout=30)
            self.assertEqual(r.returncode, 0, r.stderr)
            return r.stdout

    def test_denies_wrapped_force_push(self):
        out = json.loads(self.run_hook("bash -c 'git push --force origin main'"))
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_passes_ordinary_command(self):
        self.assertEqual(self.run_hook("npm test").strip(), "")


if __name__ == "__main__":
    unittest.main()
