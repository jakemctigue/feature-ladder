#!/usr/bin/env python3
"""
Deterministic per-repository Hindsight bank resolution for feature-ladder.

For the repository you are working in, find the Hindsight memory bank that
already belongs to it. The same repository always resolves to the same bank.
A bank is only CREATED on the first memory write (the wrap flush or a gated
retain), so opening a session in a repository never leaves an empty bank
behind.

Order
  1. Pinned:   <repo>/.feature-ladder/hindsight.json  {"bank_id": "..."}
  2. Cached:   ~/.claude/feature-ladder/banks.json  (result of an earlier resolve)
  3. Existing: first candidate that already exists on the server, in this order
                 coding-agent::<owner>-<repo>   (from remote "origin", normalized)
                 <owner>-<repo>
                 coding-agent::<project>        (folder name, normalized)
               plus the ids earlier versions used, so their banks are still found
  4. Planned:  coding-agent::<owner>-<repo> (or coding-agent::<project> without a
               remote); created with a mission, and set to store facts verbatim,
               when the first memory is written

<project> is the folder name of the main worktree root (linked worktrees map to
the same project). <owner> is the whole namespace of remote "origin"
(group/subgroup on GitLab, org/project on Azure DevOps), so two repositories
that share a last path segment never share a bank.

Usage
  resolve_bank.py [--cwd DIR] [--refresh]   resolve (network if not cached); prints the bank id
  resolve_bank.py --session-start           SessionStart hook: resolve, print one status line for context
  resolve_bank.py --pretooluse              PreToolUse hook: gate Hindsight MCP calls and add bank_id
  resolve_bank.py --posttooluse             PostToolUse hook: record a retain that actually ran
  resolve_bank.py --candidates [--cwd DIR]  offline: show identity and candidate ids (JSON)
  resolve_bank.py --choose FILE [--cwd DIR] offline: decide from a saved list_banks result (Claude app)
  resolve_bank.py --configure [--cwd DIR]   set this repository's existing bank to store facts verbatim
                                            (new banks get this automatically)
  resolve_bank.py --list                    list banks on the server

Environment: HINDSIGHT_MCP_URL (default https://api.hindsight.vectorize.io/mcp/),
HINDSIGHT_API_KEY, HINDSIGHT_BANK_ID (the fallback bank outside a repository).
The key is never printed.
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

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "orchestration-ladder" / "scripts"))
import ladder_state  # noqa: E402

DEFAULT_URL = "https://api.hindsight.vectorize.io/mcp/"
DEFAULT_BANK = "feature-ladder"  # same fallback as .mcp.json's X-Bank-Id
CACHE = Path.home() / ".claude" / "feature-ladder" / "banks.json"
PREFIX = "coding-agent::"
SKIP_TOOLS = {"list_banks", "create_bank"}
# The gate already admits only one concrete fact per memory. Server-side
# rewriting ("concise", the default) would reword it, and consolidated
# observations would only restate it, so new banks store the text as given.
BANK_CONFIG = {"retain_extraction_mode": "verbatim", "enable_observations": False}


# ------------------------------------------------------------ identity ----
def _git(args, cwd):
    try:
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _segments(remote):
    """Path segments of a hosted remote URL, or [] for a local path or no remote."""
    remote = str(remote or "").strip()
    if not remote or remote.startswith("file:") or re.match(r"^[A-Za-z]:[\\/]", remote) or remote.startswith(("/", ".", "~")):
        return []
    m = re.match(r"^[a-z][a-z0-9+.-]*://[^/]+/(.+)$", remote, re.I) or re.match(r"^[^/@\s]+@[^:/\s]+:(.+)$", remote)
    if not m:
        return []
    path = re.sub(r"\.git/?$", "", m.group(1).strip("/"))
    return [s for s in path.split("/") if s and s != "_git"]


def parse_remote(remote):
    """(owner, repo). The owner is the whole namespace joined with '-'."""
    segs = _segments(remote)
    if len(segs) < 2:
        return "", ""
    return "-".join(segs[:-1]), segs[-1]


def project_name(root):
    return Path(root).name or "unknown"


def _root(cwd):
    """The folder a bank belongs to: the main checkout (worktrees map to it)."""
    return ladder_state.state_root(cwd)


def identity(cwd):
    cwd = str(Path(cwd).resolve())
    root = _root(cwd)
    if not (root / ".git").exists():  # a submodule, or not a repository at all
        top = _git(["rev-parse", "--show-toplevel"], cwd)
        root = Path(top).resolve() if top else root
    remote = _git(["config", "--get", "remote.origin.url"], str(root))
    owner, repo = parse_remote(remote)
    segs = _segments(remote)
    return {"root": str(root), "project": project_name(root), "owner": owner, "repo": repo,
            "legacy_owner": segs[-2] if len(segs) >= 2 else "",
            "is_git": bool((root / ".git").exists() or remote)}


def _slug(s):
    return re.sub(r"[^a-z0-9._-]+", "-", s.lower()).strip("-")


def candidates(ident):
    """Search order for existing banks; the first entry is the id a new bank
    gets. With a remote, the owner/repo form comes first so two clones that
    share a folder name never share a bank. Ids that earlier versions created
    (last-two-path-segments owner, unslugged folder name) stay in the list so
    their banks are still found."""
    out = []
    owner, repo, project = ident.get("owner"), ident.get("repo"), ident.get("project") or ""
    if owner and repo:
        out += [PREFIX + _slug(f"{owner}-{repo}"), _slug(f"{owner}-{repo}")]
        legacy = ident.get("legacy_owner")
        if legacy and legacy != owner:
            out += [PREFIX + _slug(f"{legacy}-{repo}"), _slug(f"{legacy}-{repo}")]
    out += [PREFIX + _slug(project), PREFIX + project, project]
    seen, uniq = set(), []
    for c in out:
        if c and c != PREFIX and c.lower() not in seen:
            seen.add(c.lower())
            uniq.append(c)
    return uniq


def pinned(root):
    """`root` is a path, or an identity dict (older callers)."""
    root = root["root"] if isinstance(root, dict) else root
    f = Path(root) / ".feature-ladder" / "hindsight.json"
    data = ladder_state.read_json(f)
    return (data.get("bank_id") or None) if isinstance(data, dict) else None


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
def client_version():
    manifest = ladder_state.read_json(HERE.parents[2] / ".claude-plugin" / "plugin.json")
    return str(manifest.get("version")) if isinstance(manifest, dict) and manifest.get("version") else "0"


class Mcp:
    def __init__(self):
        self.url = os.environ.get("HINDSIGHT_MCP_URL") or DEFAULT_URL
        self.key = os.environ.get("HINDSIGHT_API_KEY", "")
        self.bank = os.environ.get("HINDSIGHT_BANK_ID") or DEFAULT_BANK
        self.sid = None
        self.n = 0

    def _post(self, method, params=None, notify=False):
        body = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        if not notify:
            self.n += 1
            body["id"] = self.n
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
             "X-Bank-Id": self.bank}
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
                                  "clientInfo": {"name": "feature-ladder", "version": client_version()}})
        try:
            self._post("notifications/initialized", notify=True)
        except Exception:
            pass
        return self

    def call(self, tool, args):
        res = self._post("tools/call", {"name": tool, "arguments": args}) or {}
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
    if isinstance(payload, dict):
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


def configure(mcp, bank):
    """Store facts as given. An older server that rejects the setting keeps its default."""
    try:
        mcp.call("update_bank", {"bank_id": bank, "config_updates": dict(BANK_CONFIG)})
        return True
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------- cache ----
def _load_cache():
    data = ladder_state.read_json(CACHE)
    return data if isinstance(data, dict) else {}


def _remember(root, rec):
    cache = _load_cache()  # re-read right before writing: another session may have added a repository
    cache[str(root)] = rec
    ladder_state.write_json(CACHE, cache)


def resolve(cwd, refresh=False, offline=False, create=False):
    """Which bank this folder uses. `how` is pinned | existing | created |
    planned | not-a-repo. A planned bank does not exist yet: it is created only
    with create=True (the first memory write). A cached answer costs one git
    call and no network."""
    root = _root(cwd)
    p = pinned(root)
    if p:
        return {"bank_id": p, "how": "pinned", "root": str(root), "project": project_name(root)}
    cache = _load_cache()
    hit = cache.get(str(root))
    if isinstance(hit, dict) and hit.get("bank_id") and not refresh and not (create and hit.get("how") == "planned"):
        return {**hit, "cached": True, "root": str(root), "project": hit.get("project") or project_name(root)}
    ident = identity(cwd)
    root = ident["root"]
    if not ident["is_git"] or Path(root) == Path.home().resolve():
        # Not a repository (or the home folder itself): no per-repo bank; calls keep the server/default bank.
        return {"bank_id": None, "how": "not-a-repo", **ident}
    hit = cache.get(root)
    if isinstance(hit, dict) and hit.get("bank_id") and not refresh and not (create and hit.get("how") == "planned"):
        return {**hit, "cached": True, **ident}
    if offline:
        return None
    mcp = Mcp().start()
    bank, action = choose(ident, server_banks(mcp, ident))
    rec = {"bank_id": bank, "how": action, "project": ident["project"],
           "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if action == "create":
        if create:
            label = f"{ident['owner']}/{ident['repo']}" if ident["owner"] else ident["project"]
            mcp.call("create_bank", {"bank_id": bank, "name": label,
                                     "mission": f"Long-term engineering memory for the {label} codebase: decisions and why, "
                                                "conventions, pitfalls, interfaces, and the real test gates."})
            rec["how"] = "created"
            rec["verbatim"] = configure(mcp, bank)
        else:
            rec["how"] = "planned"
    _remember(root, rec)
    return {**rec, **ident}


def ensure_bank(cwd):
    """Resolve for a write: creates (and configures) the bank if it does not exist yet."""
    return resolve(cwd, create=True)


# --------------------------------------------------------------- hooks ----
def _stdin_json():
    try:
        data = json.loads(sys.stdin.read().lstrip("﻿") or "{}")
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def recall_args(bank, project):
    """The exact recall arguments the gate passes through unchanged."""
    import memory_gate
    return {"bank_id": bank, "tags": [f"project:{project}"], "tags_match": "all",
            "max_tokens": memory_gate.MAX_RECALL_TOKENS, "budget": "low", "types": list(memory_gate.RECALL_TYPES)}


def session_start():
    data = _stdin_json()
    cwd = data.get("cwd") or os.getcwd()
    try:
        r = resolve(cwd)
        if not r["bank_id"]:
            print("feature-ladder: this folder is not a git repository, so no per-repo Hindsight bank is used; "
                  "memory goes to the default bank (HINDSIGHT_BANK_ID).")
            return 0
        if r["how"] == "planned":
            print(f"feature-ladder: this repository has no Hindsight memory yet; its bank '{r['bank_id']}' is "
                  "created on the first memory write (/feature-ladder:wrap). Skip recall: there is nothing to find.")
            return 0
        how = f"cached; {r['how']} {str(r.get('at') or '')[:10]}".strip() if r.get("cached") else r["how"]
        print(f"feature-ladder: Hindsight bank for this repository is '{r['bank_id']}' ({how}). "
              f"For recall pass your query plus exactly {json.dumps(recall_args(r['bank_id'], r['project']))} "
              "so the hook has nothing to rewrite (it forces these values anyway). "
              f"Memories are tagged project:{r['project']}.")
    except urllib.error.HTTPError as e:
        print(f"feature-ladder: Hindsight rejected the request (HTTP {e.code}); check HINDSIGHT_API_KEY. "
              "Agents will work without memory this session.")
    except Exception as e:  # never block a session
        print(f"feature-ladder: Hindsight not reachable ({type(e).__name__}); agents will work without memory this session.")
    return 0


def _deny(reason):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                             "permissionDecisionReason": reason}}))
    return 0


def pretooluse():
    """Apply the memory gate (memory_gate.py: which tools, how much recall,
    which writes) and route the call to this repository's bank."""
    data = _stdin_json()
    tool = str(data.get("tool_name", ""))
    tin = data.get("tool_input") or {}
    if "hindsight" not in tool or not isinstance(tin, dict):
        return 0
    cwd = data.get("cwd") or os.getcwd()
    short = tool.rsplit("__", 1)[-1]
    try:
        import memory_gate
        root = _root(cwd)
        ok, reason, tin2 = memory_gate.gate(tool, tin, root, project_name(root))
    except Exception as e:  # noqa: BLE001 - a broken gate denies the memory call; it never breaks the session
        return _deny(f"feature-ladder: the memory gate could not check this call ({type(e).__name__}); "
                     "carry on without Hindsight.")
    if not ok:
        return _deny(reason)
    if short not in SKIP_TOOLS:
        try:
            r = resolve(cwd, create=(short == "retain"))
        except Exception:  # noqa: BLE001
            r = None  # the server's default bank applies
        if r and short == "recall" and r.get("how") == "planned":
            return _deny("feature-ladder: this repository has no memories yet (its Hindsight bank is created on "
                         "the first write at /feature-ladder:wrap). Carry on without recall.")
        if r and r.get("bank_id"):
            tin2["bank_id"] = r["bank_id"]
    if short == "recall":
        memory_gate.commit_recall(root)
    if tin2 != tin:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                 "updatedInput": tin2}}))
    return 0


def _failed(response):
    if isinstance(response, dict):
        return bool(response.get("isError") or response.get("is_error") or response.get("error"))
    return False


def posttooluse():
    """A retain that actually ran is recorded, so it is never written twice.
    A retain that failed is not, so it can be retried."""
    data = _stdin_json()
    tool = str(data.get("tool_name", ""))
    if "hindsight" not in tool or tool.rsplit("__", 1)[-1] != "retain" or _failed(data.get("tool_response")):
        return 0
    try:
        import memory_gate
        memory_gate.confirm(_root(data.get("cwd") or os.getcwd()), data.get("tool_input"))
    except Exception:  # noqa: BLE001 - bookkeeping must never break the session
        pass
    return 0


# ---------------------------------------------------------------- main ----
def main(argv):
    if argv and argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cwd = os.getcwd()
    if "--cwd" in argv:
        i = argv.index("--cwd")
        if i + 1 >= len(argv):
            print("resolve_bank: --cwd needs a directory", file=sys.stderr)
            return 1
        cwd = argv[i + 1]
    if "--session-start" in argv:
        return session_start()
    if "--pretooluse" in argv:
        return pretooluse()
    if "--posttooluse" in argv:
        return posttooluse()
    ident = identity(cwd)
    if "--candidates" in argv:
        print(json.dumps({**ident, "pinned": pinned(ident), "candidates": candidates(ident),
                          "create_as": candidates(ident)[0]}, indent=2))
        return 0
    if "--choose" in argv:
        i = argv.index("--choose")
        if i + 1 >= len(argv):
            print("resolve_bank: --choose needs a file with a list_banks result", file=sys.stderr)
            return 1
        raw = json.loads(Path(argv[i + 1]).read_text(encoding="utf-8-sig"))
        bank, action = choose(ident, _bank_list(raw))
        print(json.dumps({"bank_id": bank, "action": action, "project": ident["project"],
                          "config_updates": BANK_CONFIG if action == "create" else None}, indent=2))
        return 0
    if "--list" in argv:
        mcp = Mcp().start()
        for b in _bank_list(mcp.call("list_banks", {"limit": 100})):
            print(f"  {b.get('bank_id') or b.get('id')}  {b.get('name') or ''}")
        return 0
    if "--configure" in argv:
        r = resolve(cwd)
        if not r or not r.get("bank_id") or r.get("how") == "planned":
            print("resolve_bank: this repository has no bank yet; it is configured when it is created")
            return 1
        ok = configure(Mcp().start(), r["bank_id"])
        print(json.dumps({"bank_id": r["bank_id"], "configured": ok, "config_updates": BANK_CONFIG}, indent=2))
        return 0 if ok else 3
    r = resolve(cwd, refresh="--refresh" in argv)
    print(json.dumps(r, indent=2) if "--json" in argv else (r["bank_id"] or "(not a git repository: default bank)"))
    return 0


if __name__ == "__main__":
    ladder_state.utf8_stdio()
    sys.exit(main(sys.argv[1:]))
