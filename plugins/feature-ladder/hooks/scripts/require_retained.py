#!/usr/bin/env python3
"""
SubagentStop hook: make sure the coordinator queued the feature's memories for
the end of the session (session_budget.py remember) before finishing.

Memory is written once per session, at /feature-ladder:wrap, so workers no
longer retain anything: their `Memory candidates:` line carries it and a bounce
would only cost an extra turn. Only the coordinator is checked: if its last
message has no line starting with "Retained:", the stop is blocked once with a
reason that says what to do. It never blocks twice in a row (stop_hook_active
guard), so a stubborn agent cannot loop forever.

Exit 0 with {"decision":"block","reason":...} to send the agent back;
exit 0 with no output to allow the stop.
"""
import json
import os
import re
import sys

MARK = re.compile(r"^\s*Retained:\s*\S", re.MULTILINE)
CHECKED = {"coordinator"}


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
                msg = rec.get("message", rec)
                if msg.get("role") != "assistant":
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
