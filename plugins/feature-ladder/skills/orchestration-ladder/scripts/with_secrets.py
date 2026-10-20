#!/usr/bin/env python3
"""
feature-ladder: run one command with secrets from Google Secret Manager in its
environment, without the values ever entering the transcript.

  with_secrets.py --secret NAME[=ENV_VAR] [--secret ...] [--project P] -- <command...>
      Fetches each NAME (latest version) with the gcloud CLI, exports it as
      ENV_VAR (default: NAME with '-' -> '_', upper-cased), runs the command,
      and prints its output with every secret value replaced by ***.
      Exit code is the command's. Nothing is written to disk.

  with_secrets.py check NAME [NAME ...] [--project P]
      Confirms each secret is readable (names and status only). Exit 3 if
      gcloud is missing or not signed in, 1 if any secret is unreadable.

Secrets are fetched with the user's own gcloud sign-in (or
GOOGLE_APPLICATION_CREDENTIALS). The project comes from --project,
GOOGLE_CLOUD_PROJECT, the .firebaserc default, or gcloud's configured project.
"""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


def gcloud():
    return shutil.which("gcloud") or shutil.which("gcloud.cmd")


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
        r = subprocess.run([exe, "config", "get-value", "project"], capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip() and r.stdout.strip() != "(unset)":
            return r.stdout.strip()
    return None


def fetch(exe, name, proj):
    args = [exe, "secrets", "versions", "access", "latest", "--secret", name]
    if proj:
        args += ["--project", proj]
    r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        return None, (r.stderr or r.stdout).strip().splitlines()[-1:] or ["unknown error"]
    return r.stdout.rstrip("\r\n"), None


def env_name(name):
    return re.sub(r"[^A-Za-z0-9_]", "_", name).upper()


def mask(text, values):
    for v in sorted(values, key=len, reverse=True):
        if len(v) >= 4:
            text = text.replace(v, "***")
    return text


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
                if "not logged in" in msg.lower() or "credential" in msg.lower() or "auth" in msg.lower():
                    print(f"{n}: NOT SIGNED IN ({msg})")
                    return 3
                print(f"{n}: UNREADABLE ({msg})")
            else:
                print(f"{n}: ok ({len(val)} chars)")
        print(f"project: {proj or '(none)'}")
        return 1 if bad else 0

    secrets, proj, cmd = [], None, None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--secret" and i + 1 < len(argv):
            secrets.append(argv[i + 1])
            i += 2
        elif a == "--project" and i + 1 < len(argv):
            proj = argv[i + 1]
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
            return 3 if any(w in " ".join(err).lower() for w in ("logged in", "credential", "auth")) else 1
        env[var or env_name(name)] = val
        values.append(val)
    r = subprocess.run(cmd if len(cmd) > 1 else cmd[0], shell=len(cmd) == 1, env=env,
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    sys.stdout.write(mask(r.stdout, values))
    sys.stderr.write(mask(r.stderr, values))
    return r.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
