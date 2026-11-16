#!/usr/bin/env python3
"""
feature-ladder: run one command with secrets from Google Secret Manager in its
environment, without the values ever entering the transcript.

  with_secrets.py --secret NAME[=ENV_VAR] [--secret ...] [--project P] [--timeout S] -- <command...>
      Fetches each NAME (latest version) with the gcloud CLI, exports it as
      ENV_VAR (default: NAME with '-' -> '_', upper-cased), runs the command,
      and prints its output, line by line as it arrives, with every secret
      value replaced by ***. Exit code is the command's (127 if it cannot be
      started, 124 if --timeout stopped it). Nothing is written to disk.

  with_secrets.py check NAME [NAME ...] [--project P]
      Confirms each secret is readable (names and status only). Exit 3 if
      gcloud is missing or not signed in, 1 if any secret is unreadable.

The command after --
  Several words are a program and its arguments. The program is looked up on
  PATH the way a shell does it, so npm-installed .cmd shims (npm, npx,
  firebase, gcloud) run on Windows:
      with_secrets.py --secret STRIPE_KEY -- npm run sync
  The program reads the secret from its environment. To put a secret INTO an
  argument, let a shell inside the wrapper expand it; the shell you are typing
  in would expand $NAME before the secret exists:
      with_secrets.py --secret API_TOKEN -- bash -c 'curl -H "Authorization: Bearer $API_TOKEN" https://api.example.com'
  One single word (one quoted string) is shell text and runs the same way:
  through bash when there is one (Git Bash on Windows), else the system shell:
      with_secrets.py --secret API_TOKEN -- 'curl -H "Authorization: Bearer $API_TOKEN" https://api.example.com'

Masking replaces each secret value, each line of a multi-line value, and its
base64, URL-safe base64, hex and URL-encoded forms. It cannot see a value the
command cuts into pieces or transforms some other way: do not write commands
that print parts of a secret.

Secrets are fetched with the user's own gcloud sign-in (or
GOOGLE_APPLICATION_CREDENTIALS). The project comes from --project,
GOOGLE_CLOUD_PROJECT, the .firebaserc default, or gcloud's configured project.
gcloud gets no terminal to prompt on and FEATURE_LADDER_GCLOUD_TIMEOUT seconds
(default 60) to answer.
"""
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import ladder_state  # noqa: E402
from ladder_state import find_bash, kill_tree, run_tree  # noqa: E402

GCLOUD_TIMEOUT = int(os.environ.get("FEATURE_LADDER_GCLOUD_TIMEOUT", "60"))
# gcloud's own words for "sign in first". Matched on its message with the secret
# and project names removed: a secret called OAUTH_CLIENT_SECRET or DB_CREDENTIALS
# that is merely missing is unreadable, not a sign-in problem.
SIGNED_OUT = re.compile(
    r"reauthentication|gcloud auth (application-default )?login|not logged in|unauthenticated|"
    r"no credentialed accounts|do not currently have an active account|invalid_grant|"
    r"could not automatically determine credentials|application default credentials", re.I)


def gcloud():
    return shutil.which("gcloud") or shutil.which("gcloud.cmd")


def _gcloud(args):
    """(exit code, stdout, stderr); the exit code is None when gcloud did not answer in time."""
    try:
        return run_tree(args, timeout=GCLOUD_TIMEOUT)  # no input: its stdin is the null device
    except OSError as e:
        return 127, "", str(e)


def project(explicit):
    if explicit:
        return explicit
    for var in ("GOOGLE_CLOUD_PROJECT", "GCLOUD_PROJECT", "CLOUDSDK_CORE_PROJECT"):
        if os.environ.get(var):
            return os.environ[var]
    p = Path.cwd()
    for d in [p, *p.parents]:
        f = d / ".firebaserc"
        if f.is_file():
            try:
                return json.loads(f.read_text(encoding="utf-8-sig")).get("projects", {}).get("default")
            except (OSError, ValueError):
                break
        if (d / ".git").exists():
            break
    exe = gcloud()
    if exe:
        code, out, _ = _gcloud([exe, "config", "get-value", "project"])
        if code == 0 and out.strip() and out.strip() != "(unset)":
            return out.strip()
    return None


class Reason(list):
    """Why a secret could not be read: the one line worth showing, plus gcloud's
    whole message in .full (the sign-in hint is often on a later line)."""
    full = ""


def fetch(exe, name, proj):
    """(value, None), or (None, [reason]) when the secret cannot be read."""
    args = [exe, "secrets", "versions", "access", "latest", "--secret", name]
    if proj:
        args += ["--project", proj]
    code, out, err = run_tree(args, timeout=GCLOUD_TIMEOUT)
    if code is None:
        return None, Reason([f"gcloud did not answer within {GCLOUD_TIMEOUT}s"])
    if code != 0:
        lines = [l.strip() for l in (err or out).splitlines() if l.strip()]
        reason = Reason([next((l for l in lines if l.upper().startswith("ERROR")), lines[-1] if lines else "unknown error")])
        reason.full = "\n".join(lines)
        return None, reason
    return out.rstrip("\r\n"), None


def signed_out(message, *names):
    """True when gcloud's message says the user must sign in. `names` (the secret,
    the project) are removed first so their spelling cannot decide it."""
    text = getattr(message, "full", "") or (" ".join(message) if isinstance(message, (list, tuple)) else str(message or ""))
    for n in sorted((n for n in names if n), key=len, reverse=True):
        text = re.sub(re.escape(str(n)), " ", text, flags=re.I)
    return bool(SIGNED_OUT.search(text))


def env_name(name):
    return re.sub(r"[^A-Za-z0-9_]", "_", name).upper()


def mask_values(values):
    """Everything to hide for these secret values: the value, each line of a
    multi-line value, and the encodings a command most often prints."""
    out = set()
    for v in values:
        if not v:
            continue
        parts = {v} | {line.strip() for line in v.splitlines()}
        for part in parts:
            if len(part) >= 4:
                out.add(part)
        raw = v.encode("utf-8")
        for enc in (base64.b64encode(raw).decode(), base64.urlsafe_b64encode(raw).decode(), raw.hex(),
                    urllib.parse.quote(v, safe="")):
            if len(enc) >= 8:
                out.add(enc)
                out.add(enc.rstrip("="))
    return sorted(out, key=len, reverse=True)


def mask(text, values):
    for v in sorted(values, key=len, reverse=True):
        if len(v) >= 4:
            text = text.replace(v, "***")
    return text


def command_argv(cmd):
    """How the words after -- are started: (argv or shell text, shell flag)."""
    if len(cmd) == 1:  # one string is shell text; bash expands $NAME the way the caller wrote it
        bash = find_bash()
        return ([bash, "-c", cmd[0]], False) if bash else (cmd[0], True)
    exe = cmd[0]
    if sys.platform == "win32" and exe.lower() in ("bash", "bash.exe", "sh", "sh.exe"):
        exe = find_bash() or exe  # Git Bash, not WSL's bash in System32
    return [shutil.which(exe) or exe, *cmd[1:]], False


def _pump(src, dst, values):
    for line in iter(src.readline, ""):
        dst.write(mask(line, values))
        dst.flush()
    src.close()


def run_masked(cmd, env, values, timeout=None):
    """Run the command, streaming its output through the mask. Returns its exit
    code; 127 when it cannot be started, 124 when the timeout stopped it."""
    argv, shell = command_argv(cmd)
    kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32" and timeout \
        else ({"start_new_session": True} if timeout else {})
    try:
        proc = subprocess.Popen(argv, shell=shell, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace", bufsize=1, **kw)
    except OSError as e:
        print(f"with_secrets: cannot run {cmd[0]!r}: {e}", file=sys.stderr)
        return 127
    hidden = mask_values(values)
    pumps = [threading.Thread(target=_pump, args=(proc.stdout, sys.stdout, hidden), daemon=True),
             threading.Thread(target=_pump, args=(proc.stderr, sys.stderr, hidden), daemon=True)]
    for t in pumps:
        t.start()
    try:
        code = proc.wait(timeout=timeout or None)
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        code = 124
        print(f"with_secrets: the command was stopped after {timeout}s", file=sys.stderr)
    for t in pumps:
        t.join(timeout=5)
    return code


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    exe = gcloud()
    if not exe:
        print("with_secrets: gcloud CLI not found on PATH", file=sys.stderr)
        return 3

    if argv[0] == "check":
        names, proj = [], None
        it = iter(argv[1:])
        for a in it:
            if a == "--project":
                proj = next(it, None)
            else:
                names.append(a)
        proj = project(proj)
        bad = 0
        for n in names:
            val, err = fetch(exe, n, proj)
            if val is None:
                bad += 1
                msg = " ".join(err)
                if signed_out(err, n, proj):
                    print(f"{n}: NOT SIGNED IN ({msg})")
                    return 3
                print(f"{n}: UNREADABLE ({msg})")
            else:
                print(f"{n}: ok ({len(val)} chars)")
        print(f"project: {proj or '(none)'}")
        return 1 if bad else 0

    secrets, proj, cmd, timeout = [], None, None, None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--secret" and i + 1 < len(argv):
            secrets.append(argv[i + 1])
            i += 2
        elif a == "--project" and i + 1 < len(argv):
            proj = argv[i + 1]
            i += 2
        elif a == "--timeout" and i + 1 < len(argv):
            if not argv[i + 1].isdigit() or int(argv[i + 1]) < 1:
                print("with_secrets: --timeout needs a whole number of seconds", file=sys.stderr)
                return 1
            timeout = int(argv[i + 1])
            i += 2
        elif a == "--":
            cmd = argv[i + 1:]
            break
        else:
            print(f"with_secrets: unexpected argument {a!r} (put the command after --)", file=sys.stderr)
            return 1
    if not cmd or not secrets:
        print("with_secrets: need at least one --secret and a command after --", file=sys.stderr)
        return 1
    proj = project(proj)
    env = dict(os.environ)
    values = []
    for spec in secrets:
        name, _, var = spec.partition("=")
        val, err = fetch(exe, name, proj)
        if val is None:
            print(f"with_secrets: cannot read secret {name}: {' '.join(err)}", file=sys.stderr)
            return 3 if signed_out(err, name, proj) else 1
        env[var or env_name(name)] = val
        values.append(val)
    return run_masked(cmd, env, values, timeout)


if __name__ == "__main__":
    ladder_state.utf8_stdio()
    sys.exit(main(sys.argv[1:]))
