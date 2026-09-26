"""Local pipeline probe: run the real Ollama model without a DB or a server.

Fast iteration loop for the agent pipeline: it calls the real planner/reviser/
reviewer through the vendored provider and the real prompts, then applies the
deterministic guards -- no Django DB, no OpenSCAD, no Celery.

    OLLAMA_BASE_URL=http://192.168.1.250:11434 \
      uv run python scripts/probe_pipeline.py "prompt" [--model mistral-nemo:12b]

Exits non-zero when the pipeline produced something the guards reject, so it can
be used as a quick smoke test.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
os.environ.setdefault("DATABASE_URL", "sqlite:///./probe.sqlite3")
os.environ.setdefault("DJANGO_SECRET_KEY", "probe-insecure")
os.environ.setdefault("DJANGO_DEBUG", "true")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import django  # noqa: E402

django.setup()

from agents.graph.consistency import consistency_issues  # noqa: E402
from agents.graph.planner import (  # noqa: E402
    PLANNER_SYSTEM_PROMPT,
    make_planner_node,
)
from agents.llm.ollama import OllamaProvider  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt")
    parser.add_argument("--model", default=os.environ.get("PROBE_MODEL", "mistral-nemo:12b"))
    parser.add_argument("--json", action="store_true", help="print the raw specification only")
    args = parser.parse_args()

    base_url = os.environ.get("OLLAMA_BASE_URL", "http://192.168.1.250:11434")
    provider = OllamaProvider(base_url=base_url, model=args.model)

    state = {
        "prompt": args.prompt,
        "skills": [],
        "clarify_policy": "assume",
        "max_attempts": 3,
        "history": [],
    }
    node = make_planner_node(provider=provider, max_attempts=3)
    result = node(state)

    if result.get("status") == "failed":
        print("PLANNER FAILED:", result.get("error"))
        return 1

    spec = result["specification"]
    if args.json:
        print(json.dumps(spec, indent=2, ensure_ascii=False))
    else:
        print(f"model      : {args.model}")
        print(f"system     : {PLANNER_SYSTEM_PROMPT[:80]}...")
        trace = (result.get("llm_trace") or [{}])[-1]
        print(f"input      : {trace.get('prompt', '')[:160]}")
        print(f"object     : {spec.get('object')}")
        print(f"dimensions : {spec.get('dimensions')}")
        for primitive in spec.get("primitives") or []:
            profile = primitive.get("profile") or []
            print(
                f"primitive  : {primitive.get('type')}/{primitive.get('role')} "
                f"h={primitive.get('height')} wall={primitive.get('wall_thickness')} "
                f"profile={len(profile)} pont"
            )
            for point in profile[:12]:
                print(f"             ({point.get('x')}, {point.get('y')})")
        assumptions = result.get("assumptions") or []
        for item in assumptions:
            print(f"assumption : {item.get('field')} = {item.get('answer')}")

    issues = consistency_issues(args.prompt, spec)
    print()
    if issues:
        print(f"GUARD ELTÉRÉSEK ({len(issues)}):")
        for item in issues:
            print("  -", item)
        return 2
    print("GUARD: nincs determinisztikus eltérés")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
