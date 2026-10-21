#!/usr/bin/env python3
"""Original Feature Ladder scoring, mapped to explicit GPT models."""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'scripts'))
import runtime

score = runtime.router.score
clean = runtime.router.clean
keyword_hit = runtime.router.keyword_hit
KEYWORDS = runtime.router.KEYWORDS
route = runtime.route

if __name__ == '__main__':
    if '--table' in sys.argv:
        for low, high, agent, model, effort, label in runtime.router.BANDS:
            print(low, high, agent, runtime.CONFIG['models'][model], effort)
    elif len(sys.argv) > 2 and sys.argv[1] == '--file':
        print(json.dumps(route(runtime.read_json(sys.argv[2])), indent=2))
    elif len(sys.argv) > 2 and sys.argv[1] == '--describe':
        print(json.dumps(route(runtime.router.describe(' '.join(sys.argv[2:]))), indent=2))
    else:
        print(json.dumps(route(json.loads(sys.argv[1])), indent=2))
