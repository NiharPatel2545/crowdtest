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
from crowdtest.run import MODEL, Engine, estimate_cost, run_all, task_prompt


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
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["crowd"]:
        return crowd_main(argv[1:])
    if argv[:1] == ["serve"]:                     # crowdtest serve [--port 8720] [--env-file PATH]: the local web page
        from crowdtest.web import serve
        opt = lambda k: argv[argv.index(k) + 1] if k in argv else None
        serve(int(opt("--port") or 8720), key_loaded=load_key(opt("--env-file")))
        return 0
    ap = argparse.ArgumentParser(prog="crowdtest", description="Simulated visitors try a goal on your website and report where they got stuck.")
    ap.add_argument("url")
    ap.add_argument("--goal", required=True, help='what visitors try to do, e.g. "find the price and sign up for the free plan"')
    ap.add_argument("--visitors", type=int, default=5, help="how many personas from the built-in library (max 8)")
    ap.add_argument("--persona-file", help="JSON list of your own personas instead of the library")
    ap.add_argument("--max-steps", type=int, default=25)
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--provider", choices=["anthropic", "ollama", "openai"], default="anthropic",
                    help="anthropic (paid, best), ollama (free, runs on your GPU) or openai (any OpenAI-compatible server, e.g. LM Studio)")
    ap.add_argument("--model", help=f"model name (default {MODEL} for anthropic; e.g. qwen3:8b for ollama)")
    ap.add_argument("--base-url", help="server address for ollama/openai, e.g. http://localhost:1234/v1")
    ap.add_argument("--full", action="store_true", help="let visitors see screenshots (better judgement of layout, about 2x the tokens)")
    ap.add_argument("--max-cost", type=float, default=1.00, help="refuse to start if the estimated cost is higher (USD)")
    ap.add_argument("--env-file", help="read ANTHROPIC_API_KEY from this .env file if it is not set")
    ap.add_argument("--out", default="crowdtest-report.html")
    ap.add_argument("--json", help="also write raw results as JSON")
    ap.add_argument("--dry-run", action="store_true", help="print the personas, prompts and cost estimate; call nothing")
    a = ap.parse_args(argv)

    people = P.load(a.persona_file) if a.persona_file else P.pick(a.visitors)
    url = a.url if "://" in a.url else "https://" + a.url
    eng = Engine(a.provider, a.model or (MODEL if a.provider == "anthropic" else "qwen3:8b"), a.base_url, lean=not a.full)
    est = estimate_cost(len(people), a.max_steps, eng.model, eng.lean, eng.free)
    print(f"{len(people)} visitors, up to {a.max_steps} steps each, {eng.provider}/{eng.model} ({'lean' if eng.lean else 'full'}): "
          + ("free (runs on this computer)" if eng.free else f"estimated cost up to US${est:.2f}"))
    if a.dry_run:
        for p in people:
            print(f"\n--- {p.name} ({p.device}) ---\n{task_prompt(p, url, a.goal)}")
        return 0
    if est > a.max_cost:
        print(f"Stopped: estimate is above --max-cost {a.max_cost:.2f}. Raise it or use fewer visitors / steps.")
        return 2
    if eng.provider == "anthropic" and not load_key(a.env_file):
        print("Set ANTHROPIC_API_KEY or pass --env-file (or use --provider ollama for a free local model).")
        return 2
    load_key(a.env_file)                       # optional for local models: used only to group the findings at the end
    visits = asyncio.run(run_all(people, url, a.goal, a.max_steps, eng, a.concurrency))
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


def crowd_main(argv: list[str]) -> int:
    """crowdtest crowd URL --goal "..." --people 300 [--provider ollama|anthropic|openai]"""
    from crowdtest import crowd
    ap = argparse.ArgumentParser(prog="crowdtest crowd", description="Hundreds of generated people react to your pages (one short call each).")
    ap.add_argument("url")
    ap.add_argument("--goal", required=True)
    ap.add_argument("--people", type=int, default=200)
    ap.add_argument("--audience", default="", help='who the site is for, e.g. "farmer or farm adviser" (default: the general public)')
    ap.add_argument("--provider", choices=["ollama", "anthropic", "openai"], default="ollama", help="ollama (free, no key, default), anthropic (cheap, cached) or openai (any OpenAI-compatible server)")
    ap.add_argument("--model")
    ap.add_argument("--base-url")
    ap.add_argument("--max-pages", type=int, default=8)
    ap.add_argument("--workers", type=int, default=4, help="calls at once (keep 1-2 for a local model on one GPU)")
    ap.add_argument("--env-file")
    ap.add_argument("--out", default="crowd-report.html")
    ap.add_argument("--json")
    ap.add_argument("--dry-run", action="store_true", help="crawl and show what people will read; call no model")
    a = ap.parse_args(argv)
    url = a.url if "://" in a.url else "https://" + a.url
    pages = crowd.crawl(url, a.max_pages)
    chars = sum(len(p["text"]) for p in pages)
    print(f"Read {len(pages)} pages ({chars:,} characters) from {url}")
    if not pages:
        print("Nothing readable: the site may build its pages with JavaScript only, or block plain requests.")
        return 2
    if a.dry_run:
        for p in pages:
            print(f"  {p['url']}  {p['title'][:60]}  ({len(p['text'])} chars)")
        print(f"\nExample person: {crowd.people(1)[0].describe()}")
        return 0
    if a.provider == "anthropic" and not load_key(a.env_file):
        print("Set ANTHROPIC_API_KEY or pass --env-file (or use the free --provider ollama).")
        return 2
    backend = crowd.Backend(a.provider, a.model, a.base_url)
    prog = {}
    results = crowd.run_crowd(url, a.goal, a.people, backend, pages, a.workers, prog, a.audience)
    s = crowd.summarise(results)
    issues = crowd.group_issues(backend, s)
    cost = backend.cost_usd()
    Path(a.out).write_text(report.crowd_html(url, a.goal, pages, s, issues, f"{backend.provider}/{backend.model}", cost), encoding="utf-8")
    if a.json:
        Path(a.json).write_text(json.dumps({"url": url, "goal": a.goal, "summary": s, "issues": issues, "results": results, "tokens": backend.tokens}, indent=1), encoding="utf-8")
    print(f"{s['answered']} of {s['people']} people reacted; {s['would_complete_pct']}% would complete the goal; cost " + ("free" if not cost else f"US${cost:.3f}"))
    for i in issues[:5]:
        print(f"  {i.get('mentions', 0):>4} mentions: {i.get('title', '')}")
    print(f"Report: {Path(a.out).resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
