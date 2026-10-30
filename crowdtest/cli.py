"""crowdtest https://yoursite.com --goal "Find the price and start a free trial" [--visitors 5] [--dry-run]"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from crowdtest import personas as P
from crowdtest import report
from crowdtest.run import MODEL, estimate_cost, run_all, task_prompt


def load_key(env_file: str | None) -> bool:
    """Use ANTHROPIC_API_KEY from the environment, else read only that line from an .env file. The key is never printed."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return True
    if env_file and Path(env_file).exists():
        for line in Path(env_file).read_text("utf-8").splitlines():
            if line.strip().startswith("ANTHROPIC_API_KEY="):
                os.environ["ANTHROPIC_API_KEY"] = line.split("=", 1)[1].strip().strip('"').strip("'")
                return True
    return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="crowdtest", description="Simulated visitors try a goal on your website and report where they got stuck.")
    ap.add_argument("url")
    ap.add_argument("--goal", required=True, help='what visitors try to do, e.g. "find the price and sign up for the free plan"')
    ap.add_argument("--visitors", type=int, default=5, help="how many personas from the built-in library (max 8)")
    ap.add_argument("--persona-file", help="JSON list of your own personas instead of the library")
    ap.add_argument("--max-steps", type=int, default=25)
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--max-cost", type=float, default=1.00, help="refuse to start if the estimated cost is higher (USD)")
    ap.add_argument("--env-file", help="read ANTHROPIC_API_KEY from this .env file if it is not set")
    ap.add_argument("--out", default="crowdtest-report.html")
    ap.add_argument("--json", help="also write raw results as JSON")
    ap.add_argument("--dry-run", action="store_true", help="print the personas, prompts and cost estimate; call nothing")
    a = ap.parse_args(argv)

    people = P.load(a.persona_file) if a.persona_file else P.pick(a.visitors)
    url = a.url if "://" in a.url else "https://" + a.url
    est = estimate_cost(len(people), a.max_steps, a.model)
    print(f"{len(people)} visitors, up to {a.max_steps} steps each, {a.model}: estimated cost up to US${est:.2f}")
    if a.dry_run:
        for p in people:
            print(f"\n--- {p.name} ({p.device}) ---\n{task_prompt(p, url, a.goal)}")
        return 0
    if est > a.max_cost:
        print(f"Stopped: estimate is above --max-cost {a.max_cost:.2f}. Raise it or use fewer visitors / steps.")
        return 2
    if not load_key(a.env_file):
        print("Set ANTHROPIC_API_KEY or pass --env-file.")
        return 2
    visits = asyncio.run(run_all(people, url, a.goal, a.max_steps, a.model, a.concurrency))
    issues, synth_cost = report.synthesise(visits, a.goal)
    s = report.summary(visits, synth_cost)
    Path(a.out).write_text(report.to_html(url, a.goal, visits, issues, s), encoding="utf-8")
    if a.json:
        Path(a.json).write_text(json.dumps({"url": url, "goal": a.goal, "summary": s, "issues": issues, "visits": visits}, indent=2), encoding="utf-8")
    print(f"\n{s['achieved']} of {s['visitors']} reached the goal; median {s['median_steps']} steps; cost US${s['cost_usd']:.3f}")
    for i in issues[:5]:
        print(f"  [{i.get('severity', '')}] {i.get('title', '')} ({i.get('visitors', 0)} visitors)")
    print(f"Report: {Path(a.out).resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
