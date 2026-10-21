"""Read Codex usage without double-counting cumulative token_count events."""
import json
import re
import sys
from pathlib import Path
import runtime
sys.path.insert(0, str(runtime.ROOT / 'upstream'))
source = runtime.module('_ladder_ledger', runtime.ROOT / 'upstream/ledger.py')


def summarize_transcript(path):
    totals = {'input': 0, 'cache_read': 0, 'cache_write': 0, 'output': 0, 'total': 0}
    first_user, model, turns, measured = '', None, 0, False
    with open(path, encoding='utf-8') as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            payload = event.get('payload') or {}
            if event.get('type') == 'turn_context':
                model = payload.get('model', model)
                turns += 1
            if event.get('type') == 'response_item' and payload.get('role') == 'user':
                text = '\n'.join(c.get('text', '') for c in payload.get('content', []) if isinstance(c, dict))
                if 'FEATURE LADDER GPT' in text:
                    first_user = text
            if payload.get('type') == 'token_count':
                usage = (payload.get('info') or {}).get('total_token_usage')
                if usage:
                    cached = usage.get('cached_input_tokens', 0)
                    totals = {'input': max(0, usage.get('input_tokens', 0) - cached),
                              'cache_read': cached, 'cache_write': 0,
                              'output': usage.get('output_tokens', 0),
                              'total': usage.get('total_tokens', usage.get('input_tokens', 0) + usage.get('output_tokens', 0))}
                    measured = True
    return dict(totals, turns=turns, model=model, first_user=first_user, measured=measured)


source.summarize_transcript = summarize_transcript
if __name__ == '__main__':
    sys.exit(source.main(sys.argv[1:]))
