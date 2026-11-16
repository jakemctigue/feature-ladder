#!/usr/bin/env python3
"""
Shared state helpers for every feature-ladder script and hook. Costs no model
tokens and has no dependencies.

All run state lives in ONE place: <main checkout>/.feature-ladder/. A linked
worktree, a subfolder and the hooks all resolve to the same folder, so a run
started anywhere is seen by the spawn rules, the audit log and the ledger.

    state_root(cwd)            the main checkout's root (linked worktrees map to it);
                               outside git, the nearest ancestor holding .feature-ladder/,
                               else cwd itself
    state_dir(root)            <root>/.feature-ladder
    read_json(path, default)   parsed JSON, or `default` when the file is missing or
                               unreadable; strict=True raises CorruptState for a file
                               that exists but does not parse
    write_json(path, data)     atomic: temp file in the same folder, then os.replace
    locked(root)               context manager: one writer at a time across processes
                               (wrap load -> decide -> save in it)
    ensure_gitignore(root)     keep .feature-ladder/.gitignore listing every runtime file
    utf8_stdio()               make stdout/stderr UTF-8 so printing never fails on a
                               cp1252 Windows console
    run_tree(args, timeout=)   subprocess.run that really stops at the timeout: the
                               command and everything it started are killed
    kill_tree(proc)            stop a process started in its own group, with its children
    find_bash()                Git Bash on Windows (never WSL's System32 bash), else None

Scripts in other folders import it with
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "orchestration-ladder" / "scripts"))
    import ladder_state
(in the Claude app every script sits in one folder, so the plain import works).
"""
import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

DIR = ".feature-ladder"
# Every file or folder a run writes. One list, so no script's copy can fall behind.
GITIGNORE = ("intake/", "reviews/", "adversarial/", "checkpoints/", "cards/", "evidence/", "runs/",
             "run.json", "session.json", "session-memory*.md", "memory-gate.json", "memory-ledger.jsonl",
             "*.lock", "*.tmp")
LOCK_STALE_SECONDS = 30


class CorruptState(ValueError):
    """A state file exists but is not valid JSON (truncated or hand-edited)."""


def _git_common_dir(cwd):
    try:
        r = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                           cwd=str(cwd), capture_output=True, text=True, timeout=10)
        out = r.stdout.strip()
        return Path(out) if r.returncode == 0 and out else None
    except (OSError, subprocess.SubprocessError):
        return None


_ROOTS = {}


def state_root(cwd=None):
    p = Path(cwd or os.getcwd()).resolve()
    hit = _ROOTS.get(str(p))
    if hit is not None:
        return hit
    start = p if p.is_dir() else p.parent
    common = _git_common_dir(start)
    root = None
    if common and common.name == ".git":
        root = common.parent.resolve()
    else:
        for d in [p, *p.parents]:
            if (d / DIR).is_dir():
                root = d
                break
    root = root or p
    _ROOTS[str(p)] = root
    return root


def state_dir(root):
    return Path(root) / DIR


def read_json(path, default=None, strict=False):
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return default
    try:
        return json.loads(text)
    except ValueError as e:
        if strict:
            raise CorruptState(f"{path.name} is not valid JSON ({e})") from e
        return default


def _replace(tmp, path):
    # Windows refuses the swap while another process holds the target open; retry briefly.
    for attempt in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.02)


def write_text(path, text):
    """Atomic text write (temp file in the same folder, then replace)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{time.monotonic_ns()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        _replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def write_json(path, data, indent=2):
    write_text(path, json.dumps(data, indent=indent) + "\n")


@contextlib.contextmanager
def locked(root, name="state", timeout=15):
    """One process at a time. A lock older than LOCK_STALE_SECONDS belonged to a
    process that died and is taken over; on timeout the caller proceeds unlocked
    rather than hanging a hook."""
    lock = state_dir(root) / f"{name}.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    held = False
    while True:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            held = True
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > LOCK_STALE_SECONDS:
                    lock.unlink()
                    continue
            except OSError:
                continue
        except OSError:
            pass  # e.g. PermissionError while another process is deleting it
        if time.monotonic() >= deadline:
            break
        time.sleep(0.01)
    try:
        yield held
    finally:
        if held:
            try:
                lock.unlink()
            except OSError:
                pass


def ensure_gitignore(root):
    gi = state_dir(root) / ".gitignore"
    try:
        lines = gi.read_text(encoding="utf-8").splitlines() if gi.is_file() else []
        missing = [e for e in GITIGNORE if e not in lines]
        if missing:
            write_text(gi, "\n".join(lines + missing) + "\n")
    except OSError:
        pass


def utf8_stdio():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


# ------------------------------------------------------------ running commands --
_BASH = []


def find_bash():
    """Git Bash on Windows: the shell a worker's "Done when" command was written in.
    None on other platforms (the default shell is already POSIX), when cmd.exe is
    forced, or when no Git Bash is installed. WSL's System32 bash.exe is not used:
    it runs in another filesystem."""
    if sys.platform != "win32" or os.environ.get("FEATURE_LADDER_CHECK_SHELL", "").strip().lower() == "cmd":
        return None
    if _BASH:
        return _BASH[0]
    cands = [os.environ.get("FEATURE_LADDER_BASH")]
    git = shutil.which("git")
    if git:
        g = Path(git).resolve()
        for top in (g.parent.parent, g.parent.parent.parent):  # <Git>/cmd/git.exe, <Git>/mingw64/bin/git.exe
            cands += [top / "bin" / "bash.exe", top / "usr" / "bin" / "bash.exe"]
    on_path = shutil.which("bash")
    if on_path and not any(w in on_path.lower() for w in ("\\system32\\", "\\windowsapps\\")):
        cands.append(on_path)
    found = next((str(c) for c in cands if c and Path(c).is_file()), None)
    _BASH.append(found)
    return found


def kill_tree(proc):
    """Stop a process started by run_tree and everything it started."""
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=15)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def run_tree(args, cwd=None, timeout=None, shell=False, env=None, input=None):  # noqa: A002
    """subprocess.run that really stops at the timeout. The command runs in its own
    process group; at the timeout the whole tree is killed, so a grandchild cannot
    keep running or keep the output pipes open. Returns (exit code, stdout, stderr);
    the exit code is None after a timeout. Raises OSError if the command cannot start."""
    kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32" \
        else {"start_new_session": True}
    proc = subprocess.Popen(args, shell=shell, cwd=str(cwd) if cwd else None, env=env,
                            stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace", **kw)
    try:
        out, err = proc.communicate(input=input, timeout=timeout)
        return proc.returncode, out or "", err or ""
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        try:
            out, err = proc.communicate(timeout=5)
        except (subprocess.SubprocessError, OSError, ValueError):
            out, err = "", ""
            for pipe in (proc.stdout, proc.stderr, proc.stdin):
                try:
                    if pipe:
                        pipe.close()
                except OSError:
                    pass
        return None, out or "", err or ""
