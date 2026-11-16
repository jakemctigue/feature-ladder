#!/usr/bin/env python3
"""
SubagentStop hook: make sure the coordinator queued the feature's memories for
the end of the session (session_budget.py remember) before finishing.

Memory is written once per session, at /feature-ladder:wrap, so workers no
longer retain anything: their `Memory candidates:` line carries it and a bounce
would only cost an extra turn. Only the coordinator is checked: if its last
message has no line starting with "Retained:", the stop is blocked once with a
reason that says what to do. The footer is also checked against the queue: a
"Retained: deferred - <n> lines queued" with n > 0 while
.feature-ladder/session-memory.md is empty is bounced too ("0 lines" is an
honest footer when nothing was worth keeping). It never blocks twice in a row
(stop_hook_active guard), so a stubborn agent cannot loop forever.

Exit 0 with {"decision":"block","reason":...} to send the agent back;
exit 0 with no output to allow the stop.
"""
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "skills" / "orchestration-ladder" / "scripts"))

MARK = re.compile(r"^\s*Retained:\s*\S", re.MULTILINE)
COUNT = re.compile(r"^\s*Retained:[^\n]*?(\d+)\s+lines?\b", re.MULTILINE)
CHECKED = {"coordinator"}


def queued_lines(cwd):
    """How many memory lines are queued for this checkout, or None when unknown."""
    try:
        import ladder_state
        q = ladder_state.state_root(cwd) / ".feature-ladder" / "session-memory.md"
        if not q.is_file():
            return 0
        return sum(1 for l in q.read_text(encoding="utf-8").splitlines() if l.strip())
    except Exception:  # noqa: BLE001 - never block on a check we could not make
        return None


def last_assistant_text(transcript_path: str) -> str:
    """Best-effort: read a JSONL transcript and return the last assistant text."""
    text = ""
    try:
        with open(transcript_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = rec.get("message", rec) if isinstance(rec, dict) else None
                if not isinstance(msg, dict) or msg.get("role") != "assistant":
                    continue
                content = msg.get("content", "")
                if isinstance(content, list):
                    parts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
                    content = "\n".join(parts)
                if content:
                    text = content
    except OSError:
        pass
    return text


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read().lstrip("\ufeff"))
    except Exception:
        return 0  # malformed input: never block on hook failure
    if not isinstance(payload, dict):
        return 0

    # Only the coordinator: everyone else defers memory in its packet.
    agent_type = str(payload.get("agent_type") or payload.get("subagent_type") or "")
    if agent_type.split(":")[-1] not in CHECKED:
        return 0

    if payload.get("stop_hook_active"):
        return 0  # already bounced once; let it finish

    text = payload.get("last_assistant_message") or ""
    if not text:
        path = payload.get("agent_transcript_path") or payload.get("transcript_path") or ""
        if path and os.path.exists(path):
            text = last_assistant_text(path)

    if not text:
        return 0  # nothing to check; allow

    if MARK.search(text):
        claimed = COUNT.search(text)
        cwd = payload.get("cwd")
        if not (claimed and int(claimed.group(1)) > 0 and cwd and queued_lines(cwd) == 0):
            return 0
        print(json.dumps({"decision": "block", "reason": (
            f"feature-ladder: the footer says {claimed.group(1)} lines were queued, but the memory queue is empty. "
            "Queue them now in one call (session_budget.py remember --line ... --line ...) and repeat the footer, "
            "or end with 'Retained: deferred - 0 lines queued for /feature-ladder:wrap' if nothing is worth keeping.")}))
        return 0

    reason = (
        "feature-ladder: before finishing, queue the outcome line and the packets' memory "
        "candidates in one call (session_budget.py remember --line ... --line ...; do not "
        "call retain), then end your final message with "
        "'Retained: deferred - <n> lines queued for /feature-ladder:wrap'."
    )
    print(json.dumps({"decision": "block", "reason": reason}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
