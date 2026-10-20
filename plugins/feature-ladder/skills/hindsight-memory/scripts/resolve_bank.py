#!/usr/bin/env python3
"""
Deterministic per-repository Hindsight bank resolution for feature-ladder.

For the repository you are working in, find the Hindsight memory bank that
already belongs to it, or create one. The same repository always resolves to
the same bank.

Order
  1. Pinned:   <repo>/.feature-ladder/hindsight.json  {"bank_id": "..."}
  2. Cached:   ~/.claude/feature-ladder/banks.json  (result of an earlier resolve)
  3. Existing: first candidate that already exists on the server, in this order
                 coding-agent::<project>   (Hindsight coding-agents integration convention)
                 <owner>-<repo>            (from the git remote, normalized)
                 <project>
  4. Created:  coding-agent::<project>, with a mission describing the repo

<project> is the folder name of the main worktree root (linked worktrees map to
the same project). <owner>/<repo> come from remote "origin".

Usage
  resolve_bank.py [--cwd DIR] [--refresh]   resolve (network if not cached); prints the bank id
  resolve_bank.py --session-start           SessionStart hook: resolve, print one status line for context
  resolve_bank.py --pretooluse              PreToolUse hook: add bank_id to Hindsight MCP tool calls
  resolve_bank.py --candidates [--cwd DIR]  offline: show identity and candidate ids (JSON)
  resolve_bank.py --choose FILE [--cwd DIR] offline: decide from a saved list_banks result (Claude app)
  resolve_bank.py --list                    list banks on the server

Environment: HINDSIGHT_MCP_URL (default https://api.hindsight.vectorize.io/mcp/),
HINDSIGHT_API_KEY. The key is never printed.
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_URL = "https://api.hindsight.vectorize.io/mcp/"
CACHE = Path.home() / ".claude" / "feature-ladder" / "banks.json"
PREFIX = "coding-agent::"
SKIP_TOOLS = {"list_banks", "create_bank"}


# ------------------------------------------------------------ identity ----
def _git(args, cwd):
    try:
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def identity(cwd):
    cwd = str(Path(cwd).resolve())
    common = _git(["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd)
    if common:
        p = Path(common)
        root = p.parent if p.name == ".git" else Path(_git(["rev-parse", "--show-toplevel"], cwd) or cwd)
    else:
        top = _git(["rev-parse", "--show-toplevel"], cwd)
        root = Path(top) if top else Path(cwd)
    root = root.resolve()
    remote = _git(["config", "--get", "remote.origin.url"], str(root))
    owner = repo = ""
    m = re.search(r"[:/]([^/:]+)/([^/]+?)(?:\.git)?/?$", remote)
    if m:
        owner, repo = m.group(1), m.group(2)
    return {"root": str(root), "project": root.name or "unknown", "owner": owner, "repo": repo,
            "is_git": bool(common or remote)}


def _slug(s):
    return re.sub(r"[^a-z0-9._-]+", "-", s.lower()).strip("-")


def candidates(ident):
    """Search order for existing banks. With a remote, the owner/repo form comes
    first so two clones that share a folder name never share a bank; the
    folder-name forms stay in the list so banks made by earlier versions are
    still found."""
    out = []
    if ident["owner"] and ident["repo"]:
        out.append(PREFIX + _slug(f"{ident['owner']}-{ident['repo']}"))
        out.append(_slug(f"{ident['owner']}-{ident['repo']}"))
    out.append(PREFIX + ident["project"])
    out.append(ident["project"])
    seen, uniq = set(), []
    for c in out:
        if c and c.lower() not in seen:
            seen.add(c.lower())
            uniq.append(c)
    return uniq


def pinned(ident):
    f = Path(ident["root"]) / ".feature-ladder" / "hindsight.json"
    if f.is_file():
        try:
            b = json.loads(f.read_text(encoding="utf-8-sig")).get("bank_id")
            return b or None
        except (ValueError, OSError):
            return None
    return None


def choose(ident, banks):
    """Pure decision. banks: list of {"bank_id"/"id", "name"}. Returns (bank_id, action)."""
    p = pinned(ident)
    if p:
        return p, "pinned"
    ids = {}
    for b in banks:
        for key in ("bank_id", "id", "name"):
            v = b.get(key) if isinstance(b, dict) else None
            if v:
                ids.setdefault(str(v).lower(), b.get("bank_id") or b.get("id") or v)
    for c in candidates(ident):
        if c.lower() in ids:
            return ids[c.lower()], "existing"
    return candidates(ident)[0], "create"


# ----------------------------------------------------------------- MCP ----
class Mcp:
    def __init__(self):
        self.url = os.environ.get("HINDSIGHT_MCP_URL") or DEFAULT_URL
        self.key = os.environ.get("HINDSIGHT_API_KEY", "")
        self.sid = None
        self.n = 0

    def _post(self, method, params=None, notify=False):
        body = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        if not notify:
            self.n += 1
            body["id"] = self.n
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.key:
            h["Authorization"] = f"Bearer {self.key}"
        if self.sid:
            h["Mcp-Session-Id"] = self.sid
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(), headers=h, method="POST")
        with urllib.request.urlopen(req, timeout=60) as r:
            self.sid = r.headers.get("Mcp-Session-Id") or self.sid
            text = r.read().decode("utf-8", "replace")
        if notify or not text.strip():
            return None
        data = [ln[5:].strip() for ln in text.splitlines() if ln.startswith("data:")]
        o = json.loads(data[-1] if data else text)
        if o.get("error"):
            raise RuntimeError(f"{method}: {o['error'].get('message')}")
        return o.get("result")

    def start(self):
        self._post("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                  "clientInfo": {"name": "feature-ladder", "version": "1.2.0"}})
        try:
            self._post("notifications/initialized", notify=True)
        except Exception:
            pass
        return self

    def call(self, tool, args):
        res = self._post("tools/call", {"name": tool, "arguments": args})
        text = "\n".join(c.get("text", "") for c in res.get("content", []) if isinstance(c, dict))
        if res.get("isError"):
            raise RuntimeError(f"{tool}: {text[:300]}")
        try:
            return json.loads(text)
        except ValueError:
            return {"text": text}


def _bank_list(payload):
    if isinstance(payload, list):
        return payload
    for k in ("banks", "items", "results", "data"):
        if isinstance(payload.get(k), list):
            return payload[k]
    return []


def server_banks(mcp, ident):
    found, seen = [], set()
    for q in sorted({ident["project"], ident["repo"]} - {""}):
        for b in _bank_list(mcp.call("list_banks", {"query": q, "limit": 100})):
            key = json.dumps(b, sort_keys=True)
            if key not in seen:
                seen.add(key)
                found.append(b)
    return found


# --------------------------------------------------------------- cache ----
def _load_cache():
    try:
        return json.loads(CACHE.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}


def _save_cache(c):
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps(c, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(CACHE)


def resolve(cwd, refresh=False, offline=False):
    ident = identity(cwd)
    p = pinned(ident)
    if p:
        return {"bank_id": p, "how": "pinned", **ident}
    if not ident["is_git"] or Path(ident["root"]) == Path.home().resolve():
        # Not a repository (or the home folder itself): no per-repo bank: no per-repo bank; calls keep the server/default bank.
        return {"bank_id": None, "how": "not-a-repo", **ident}
    cache = _load_cache()
    hit = cache.get(ident["root"])
    if hit and not refresh:
        return {**hit, "how": hit.get("how", "cached"), "cached": True, **ident}
    if offline:
        return None
    mcp = Mcp().start()
    bank, action = choose(ident, server_banks(mcp, ident))
    if action == "create":
        label = f"{ident['owner']}/{ident['repo']}" if ident["owner"] else ident["project"]
        mcp.call("create_bank", {"bank_id": bank, "name": label,
                                 "mission": f"Long-term engineering memory for the {label} codebase: decisions and why, "
                                            "conventions, pitfalls, interfaces, and the real test gates."})
        action = "created"
    rec = {"bank_id": bank, "how": action, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    cache[ident["root"]] = rec
    _save_cache(cache)
    return {**rec, **ident}


# --------------------------------------------------------------- hooks ----
def _stdin_json():
    try:
        return json.loads(sys.stdin.read().lstrip("\ufeff") or "{}")
    except ValueError:
        return {}


def session_start():
    data = _stdin_json()
    cwd = data.get("cwd") or os.getcwd()
    try:
        r = resolve(cwd)
        if not r["bank_id"]:
            print("feature-ladder: this folder is not a git repository, so no per-repo Hindsight bank is used; "
                  "memory goes to the default bank (HINDSIGHT_BANK_ID).")
            return 0
        print(f"feature-ladder: Hindsight bank for this repository is '{r['bank_id']}' ({r['how']}). "
              f"Every Hindsight tool call is routed to it automatically; do not pass bank_id yourself. "
              f"Keep tagging memories project:{r['project']}.")
    except urllib.error.HTTPError as e:
        print(f"feature-ladder: Hindsight rejected the request (HTTP {e.code}); check HINDSIGHT_API_KEY. "
              "Agents will work without memory this session.")
    except Exception as e:  # never block a session
        print(f"feature-ladder: Hindsight not reachable ({type(e).__name__}); agents will work without memory this session.")
    return 0


def pretooluse():
    data = _stdin_json()
    tool = str(data.get("tool_name", ""))
    short = tool.rsplit("__", 1)[-1]
    tin = data.get("tool_input") or {}
    if "hindsight" not in tool or short in SKIP_TOOLS or not isinstance(tin, dict):
        return 0
    try:
        r = resolve(data.get("cwd") or os.getcwd())
    except Exception:
        return 0  # leave the call unchanged; the server's default bank applies
    if not r or not r.get("bank_id") or tin.get("bank_id") == r["bank_id"]:
        return 0
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                             "updatedInput": {**tin, "bank_id": r["bank_id"]}}}))
    return 0


# ---------------------------------------------------------------- main ----
def main(argv):
    if argv and argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cwd = os.getcwd()
    if "--cwd" in argv:
        cwd = argv[argv.index("--cwd") + 1]
    if "--session-start" in argv:
        return session_start()
    if "--pretooluse" in argv:
        return pretooluse()
    ident = identity(cwd)
    if "--candidates" in argv:
        print(json.dumps({**ident, "pinned": pinned(ident), "candidates": candidates(ident),
                          "create_as": candidates(ident)[0]}, indent=2))
        return 0
    if "--choose" in argv:
        raw = json.loads(Path(argv[argv.index("--choose") + 1]).read_text(encoding="utf-8-sig"))
        bank, action = choose(ident, _bank_list(raw) if isinstance(raw, dict) else raw)
        print(json.dumps({"bank_id": bank, "action": action, "project": ident["project"]}, indent=2))
        return 0
    if "--list" in argv:
        mcp = Mcp().start()
        for b in _bank_list(mcp.call("list_banks", {"limit": 100})):
            print(f"  {b.get('bank_id') or b.get('id')}  {b.get('name') or ''}")
        return 0
    r = resolve(cwd, refresh="--refresh" in argv)
    print(json.dumps(r, indent=2) if "--json" in argv else (r["bank_id"] or "(not a git repository: default bank)"))
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    sys.exit(main(sys.argv[1:]))
