#!/usr/bin/env python3
"""
SubagentStop hook: make sure every feature-ladder subagent wrote its memories
to Hindsight (or explicitly said why not) before finishing.

Reads the hook payload from stdin. If the subagent's last assistant message does
not contain a line starting with "Retained:", the hook blocks the stop once with
a reason that tells the agent what to do. It never blocks twice in a row
(stop_hook_active guard), so a stubborn agent cannot loop forever.

Exit 0 with {"decision":"block","reason":...} to send the agent back;
exit 0 with no output to allow the stop.
"""
import json
import os
import re
import sys

MARK = re.compile(r"^\s*Retained:\s*\S", re.MULTILINE)


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

    # Only police our own agents.
    agent_type = str(payload.get("agent_type") or payload.get("subagent_type") or "")
    if "feature-ladder" not in agent_type and agent_type not in {
        "coordinator", "domain-expert", "domain-expert-high", "senior-implementer",
        "implementer", "mechanic", "evidence-collector", "second-opinion",
        "deep-verifier", "architect-escalation", "ultra-investigator",
    }:
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
        "feature-ladder: before finishing, retain what you learned to Hindsight "
        "(tools mcp__plugin_feature-ladder_hindsight__retain; see the hindsight-memory "
        "skill), then end your final message with a footer line of the exact form "
        "'Retained: <n> - <kind>: <summary>; ...' or "
        "'Retained: none - <nothing durable learned | hindsight unreachable>'."
    )
    print(json.dumps({"decision": "block", "reason": reason}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
