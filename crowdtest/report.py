"""Turns the visits into a report: success rate, the problems most visitors hit (grouped by one Claude call, with a plain
fallback), and a card per visitor."""
from __future__ import annotations

import base64
import json
import statistics
from collections import Counter
from datetime import datetime, timezone
from html import escape
from pathlib import Path

SYNTH_PROMPT = """You are a UX researcher. Below are reports from simulated visitors who tried to complete one goal on a website.
Group their problems into the distinct issues behind them (merge the same issue described differently). For each issue give:
title (short), how many visitors hit it (count distinct visitors), severity (blocker / major / minor), and one concrete fix.
Order by count, then severity. At most 8 issues. Reply with JSON only: {{"issues": [{{"title": "...", "visitors": 2, "severity": "major", "fix": "..."}}]}}

Goal: {goal}
Reports:
{reports}"""


def synthesise(visits: list[dict], goal: str, model: str = "claude-haiku-5-5") -> tuple[list[dict], float]:
    """Group friction points across visitors with one Claude call. Returns (issues, cost in USD); falls back to counting."""
    reports = [{"visitor": v["name"], **{k: v["visit"][k] for k in ("goal_achieved", "stuck_points", "trust_concerns", "missing")}}
               for v in visits if v.get("visit")]
    if not reports:
        return [], 0.0
    try:
        import anthropic
        from crowdtest.run import PRICE_PER_M
        client = anthropic.Anthropic()
        r = client.messages.create(model=model, max_tokens=4000, output_config={"effort": "low"},
                                   messages=[{"role": "user", "content": SYNTH_PROMPT.format(goal=goal, reports=json.dumps(reports, indent=1))}])
        text = next((b.text for b in r.content if b.type == "text"), "")
        issues = json.loads(text[text.index("{"):text.rindex("}") + 1])["issues"]
        pin, pout = PRICE_PER_M.get(model, (0.10, 0.50))
        return issues, round((r.usage.input_tokens * pin + r.usage.output_tokens * pout) / 1e6, 4)
    except Exception:
        return fallback_issues(visits), 0.0


def fallback_issues(visits: list[dict]) -> list[dict]:
    c = Counter()
    for v in visits:
        for s in set((v.get("visit") or {}).get("stuck_points", [])):
            c[s.strip()] += 1
    return [{"title": t, "visitors": n, "severity": "major" if n > 1 else "minor", "fix": ""} for t, n in c.most_common(8)]


def summary(visits: list[dict], extra_cost: float = 0.0) -> dict:
    done = [v for v in visits if v.get("visit")]
    ok = [v for v in done if v["visit"]["goal_achieved"]]
    steps = [v["steps"] for v in visits if v.get("steps")]
    ret = Counter((v["visit"]["would_return"] or "").strip().lower()[:5] for v in done)
    return {"visitors": len(visits), "reported": len(done), "achieved": len(ok),
            "success_pct": round(100 * len(ok) / len(visits)) if visits else 0,
            "median_steps": statistics.median(steps) if steps else 0,
            "would_return": {k: ret.get(k, 0) for k in ("yes", "maybe", "no")},
            "cost_usd": round(sum(v.get("cost_usd", 0) for v in visits) + extra_cost, 3)}


def _thumb(path: str | None) -> str:
    try:
        if path and Path(path).exists() and Path(path).stat().st_size < 1_500_000:
            return f'<img alt="Last screen this visitor saw" src="data:image/png;base64,{base64.b64encode(Path(path).read_bytes()).decode()}">'
    except OSError:
        pass
    return ""


CSS = """
:root{--bg:#f7f7f4;--card:#fff;--ink:#16190f;--mut:#5b6155;--line:#e2e4dc;--ok:#3d7a1f;--bad:#c0392b;--warn:#b9770e}
@media(prefers-color-scheme:dark){:root{--bg:#0f120e;--card:#171b16;--ink:#eef0ea;--mut:#a3aa9c;--line:#2a3027;--ok:#8fd16a;--bad:#ef6f5e;--warn:#e3a23f}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:920px;margin:0 auto;padding:32px 16px 64px}h1{font-size:28px;margin:0}h2{margin:36px 0 12px;font-size:21px}.mut{color:var(--mut)}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:22px 0}
.kpis div{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px}.kpis b{display:block;font-size:26px}
.issue{background:var(--card);border:1px solid var(--line);border-left:5px solid var(--c);border-radius:12px;padding:12px 16px;margin:10px 0}
.issue h3{margin:0;font-size:16px}.issue p{margin:4px 0 0}.pill{font-size:12px;font-weight:700;text-transform:uppercase;color:var(--c)}
.v{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px;margin:12px 0;display:grid;gap:14px;grid-template-columns:1fr}
@media(min-width:760px){.v{grid-template-columns:1fr 260px}}.v h3{margin:0}.v ul{margin:6px 0 0;padding-left:20px}
.v img{width:100%;border-radius:10px;border:1px solid var(--line)}.q{font-style:italic;border-left:3px solid var(--line);padding-left:10px;margin:8px 0}
.res{font-weight:700}.res.ok{color:var(--ok)}.res.no{color:var(--bad)}footer{margin-top:40px;font-size:13px;color:var(--mut)}
"""


def to_html(url: str, goal: str, visits: list[dict], issues: list[dict], s: dict) -> str:
    sev = {"blocker": "var(--bad)", "major": "var(--warn)", "minor": "var(--mut)"}
    iss = "".join(f'<div class="issue" style="--c:{sev.get(i.get("severity"), "var(--mut)")}"><span class="pill">{escape(str(i.get("severity", "")))} · '
                  f'{i.get("visitors", 0)} of {s["visitors"]} visitors</span><h3>{escape(str(i.get("title", "")))}</h3>'
                  + (f'<p><b>Fix:</b> {escape(str(i["fix"]))}</p>' if i.get("fix") else "") + "</div>" for i in issues) or "<p>No common problems reported.</p>"
    cards = []
    for v in visits:
        vi = v.get("visit") or {}
        lst = lambda k, t: (f"<p><b>{t}</b></p><ul>" + "".join(f"<li>{escape(x)}</li>" for x in vi.get(k, [])) + "</ul>") if vi.get(k) else ""
        res = ('<span class="res ok">Reached the goal</span>' if vi.get("goal_achieved") else '<span class="res no">Did not reach the goal</span>') if vi \
            else f'<span class="res no">Run failed</span> <span class="mut">{escape("; ".join(v.get("errors", [])[:1]))}</span>'
        cards.append(f'<article class="v"><div><h3>{escape(v["name"])} <span class="mut">· {escape(v["device"])}</span></h3>'
                     f'<p class="mut">{escape(v["profile"])}</p><p>{res} · {v.get("steps", 0)} steps · {v.get("seconds", 0)} s</p>'
                     + (f'<p>{escape(vi.get("summary", ""))}</p><p class="q">“{escape(vi.get("quote", ""))}” <span class="mut">(simulated visitor)</span></p>' if vi else "")
                     + lst("stuck_points", "Got stuck") + lst("trust_concerns", "Hesitated because") + lst("missing", "Looked for and could not find")
                     + (f'<p class="mut">Would come back: {escape(vi.get("would_return", ""))}</p>' if vi else "")
                     + f'</div><div>{_thumb(v.get("last_screenshot"))}</div></article>')
    wr = s["would_return"]
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Crowd test: {escape(url)}</title><style>{CSS}</style></head><body><main>
<h1>Crowd test</h1><p class="mut">{escape(url)} · {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}</p><p><b>Goal:</b> {escape(goal)}</p>
<div class="kpis"><div><b>{s['achieved']} of {s['visitors']}</b>reached the goal</div><div><b>{s['median_steps']}</b>median steps</div>
<div><b>{wr['yes']} / {wr['maybe']} / {wr['no']}</b>would return (yes / maybe / no)</div><div><b>US${s['cost_usd']:.2f}</b>cost of this test</div></div>
<h2>What got in the way</h2>{iss}<h2>Each visitor</h2>{''.join(cards)}
<footer>Visitors are AI agents playing the people described, browsing the live site. Use this to find usability problems before real users do;
it does not measure demand, and simulated visitors are more patient and agreeable than real ones.</footer></main></body></html>"""


def crowd_html(url: str, goal: str, pages: list[dict], s: dict, issues: list[dict], engine: str, cost: float) -> str:
    rows = lambda pairs: "".join(f"<li>{escape(t)} <span class='mut'>({n})</span></li>" for t, n in pairs) or "<li class='mut'>none</li>"
    grp = "".join(f"<h3>By {escape(t)}</h3><table>" + "".join(
        f"<tr><td>{escape(k)}</td><td>{v['people']} people</td><td><div class='bar'><i style='width:{v['would_complete_pct']}%'></i></div></td><td>{v['would_complete_pct']}%</td></tr>"
        for k, v in g.items()) + "</table>" for t, g in s["groups"].items())
    iss = "".join(f"<div class='issue' style='--c:var(--warn)'><span class='pill'>{i.get('mentions', i.get('people', 0))} mentions</span><h3>{escape(str(i.get('title', '')))}</h3>"
                  + (f"<p><b>Fix:</b> {escape(str(i['fix']))}</p>" if i.get("fix") else "") + "</div>" for i in issues) or "<p>No common problems reported.</p>"
    quotes = "".join(f"<p class='q'>“{escape(q)}” <span class='mut'>({escape(p['age'])}, {escape(p['job'])}, {escape(p['country'])}, {escape(p['device'])})</span></p>" for p, q in s["quotes"])
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Crowd test: {escape(url)}</title><style>{CSS}
table{{width:100%;border-collapse:collapse;font-size:14px}}td{{padding:6px 8px;border-top:1px solid var(--line)}}td:nth-child(3){{width:40%}}
.bar{{height:8px;border-radius:6px;background:var(--line);overflow:hidden}}.bar i{{display:block;height:100%;background:var(--ok)}}h3{{margin:18px 0 6px;font-size:16px}}
</style></head><body><main><h1>Crowd test</h1><p class="mut">{escape(url)} · {len(pages)} pages read · engine {escape(engine)}</p><p><b>Goal:</b> {escape(goal)}</p>
<div class="kpis"><div><b>{s['answered']}</b>people reacted (of {s['people']})</div><div><b>{s['would_complete_pct']}%</b>would complete the goal ({s['maybe_pct']}% maybe)</div>
<div><b>{s['would_return_pct']}%</b>would come back</div><div><b>{'free' if not cost else f'US${cost:.2f}'}</b>cost of this test</div></div>
<h2>What got in the way</h2>{iss}<h2>Who struggles</h2>{grp}
<h2>Most common, word for word</h2><p><b>Confusing</b></p><ul>{rows(s['top_confusing'])}</ul><p><b>Missing</b></p><ul>{rows(s['top_missing'])}</ul><p><b>Trust</b></p><ul>{rows(s['top_trust'])}</ul>
<h2>In their words</h2>{quotes}
<footer>Each person is an AI playing a generated profile, reading the page text the crawler captured. Crowd mode shows how different
people understand and trust your pages; it cannot click, so use the browsing visitors to find broken flows. Simulated people
are more agreeable than real ones: read percentages as relative, not as a forecast.</footer></main></body></html>"""
