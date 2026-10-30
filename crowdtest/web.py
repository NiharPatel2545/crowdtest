"""Local web page for crowdtest: `crowdtest serve [--env-file PATH]`, then open http://127.0.0.1:8720.
Listens on this computer only. One test runs at a time, and the server refuses any test whose estimate is above the cost cap."""
from __future__ import annotations

import asyncio
import uuid
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from crowdtest import personas as P
from crowdtest import localguard, report
from crowdtest.run import MODEL, PRICE_PER_M, Engine, estimate_cost, run_all

HARD_CAP_USD = 2.00            # no test from this page may be estimated above this
app = FastAPI(title="crowdtest", docs_url=None, redoc_url=None, openapi_url=None)
localguard.install(app)
RUNS: dict[str, dict] = {}
STATE = {"key_loaded": False}


class RunRequest(BaseModel):
    url: str = Field(..., max_length=2000)
    goal: str = Field(..., min_length=5, max_length=500)
    personas: list[str] = Field(..., min_length=1, max_length=8)
    max_steps: int = Field(20, ge=5, le=40)
    provider: str = Field("anthropic", pattern="^(anthropic|ollama)$")
    model: str = Field(MODEL, max_length=80)
    lean: bool = True


@app.get("/api/config")
def config() -> dict:
    return {"key_loaded": STATE["key_loaded"], "models": list(PRICE_PER_M), "default_model": MODEL, "cap_usd": HARD_CAP_USD,
            "personas": [{"id": p.id, "name": p.name, "profile": p.profile, "device": p.device} for p in P.LIBRARY]}


@app.get("/api/estimate")
def estimate(n: int = 3, max_steps: int = 20, model: str = MODEL, provider: str = "anthropic", lean: bool = True) -> dict:
    return {"usd": round(estimate_cost(max(1, min(n, 8)), max(5, min(max_steps, 40)), model, lean, provider == "ollama"), 3)}


@app.post("/api/runs")
async def start(req: RunRequest) -> dict:
    if req.provider == "anthropic" and not STATE["key_loaded"]:
        raise HTTPException(400, "No Anthropic API key loaded. Restart with ANTHROPIC_API_KEY set or --env-file, or pick the free local model.")
    if any(r["status"] == "running" for r in RUNS.values()):
        raise HTTPException(409, "A test is already running. Wait for it to finish.")
    url = req.url.strip() if "://" in req.url else "https://" + req.url.strip()
    if urlparse(url).scheme not in ("http", "https") or not urlparse(url).hostname:
        raise HTTPException(422, "That does not look like a website address.")
    if req.provider == "anthropic" and req.model not in PRICE_PER_M:
        raise HTTPException(422, "Unknown model.")
    eng = Engine(req.provider, req.model, None, lean=req.lean)
    people = [p for p in P.LIBRARY if p.id in set(req.personas)]
    if not people:
        raise HTTPException(422, "Pick at least one visitor.")
    est = estimate_cost(len(people), req.max_steps, eng.model, eng.lean, eng.free)
    if est > HARD_CAP_USD:
        raise HTTPException(422, f"Estimated US${est:.2f} is above the US${HARD_CAP_USD:.2f} cap. Use fewer visitors or steps.")
    rid = uuid.uuid4().hex[:10]
    RUNS[rid] = run = {"status": "running", "url": url, "goal": req.goal, "estimate_usd": round(est, 3), "progress": {}}

    async def job():
        try:
            visits = await run_all(people, url, req.goal, req.max_steps, eng, concurrency=1 if eng.free else 2, progress=run["progress"])
            issues, synth = await asyncio.to_thread(report.synthesise, visits, req.goal)
            s = report.summary(visits, synth)
            run.update(status="done", summary=s, issues=issues, html=report.to_html(url, req.goal, visits, issues, s))
        except Exception as e:                       # keep the page informative instead of hanging
            run.update(status="failed", error=f"{type(e).__name__}: {e}"[:300])

    run["task"] = asyncio.create_task(job())
    return {"id": rid, "estimate_usd": run["estimate_usd"]}


class CrowdRequest(BaseModel):
    url: str = Field(..., max_length=2000)
    goal: str = Field(..., min_length=5, max_length=500)
    audience: str = Field("", max_length=120)
    people: int = Field(100, ge=10, le=1000)
    max_pages: int = Field(8, ge=1, le=15)
    provider: str = Field("ollama", pattern="^(anthropic|ollama)$")
    model: str | None = Field(None, max_length=80)


@app.post("/api/crowd")
async def crowd_start(req: CrowdRequest) -> dict:
    from crowdtest import crowd
    if req.provider == "anthropic" and not STATE["key_loaded"]:
        raise HTTPException(400, "No Anthropic API key loaded. Use the free local engine, or restart with --env-file.")
    if any(r["status"] == "running" for r in RUNS.values()):
        raise HTTPException(409, "A test is already running. Wait for it to finish.")
    url = req.url.strip() if "://" in req.url else "https://" + req.url.strip()
    if urlparse(url).scheme not in ("http", "https") or not urlparse(url).hostname:
        raise HTTPException(422, "That does not look like a website address.")
    est = round(req.people * 0.0008, 3) if req.provider == "anthropic" else 0.0     # measured about US$0.0006 a person, pages cached
    if est > HARD_CAP_USD:
        raise HTTPException(422, f"Estimated US${est:.2f} is above the US${HARD_CAP_USD:.2f} cap.")
    rid = uuid.uuid4().hex[:10]
    RUNS[rid] = run = {"status": "running", "kind": "crowd", "url": url, "goal": req.goal, "estimate_usd": est, "progress": {}}

    def job():
        try:
            pages = crowd.crawl(url, req.max_pages)
            if not pages:
                return run.update(status="failed", error="No readable pages: the site may build its pages with JavaScript only.")
            backend = crowd.Backend(req.provider, req.model)
            results = crowd.run_crowd(url, req.goal, req.people, backend, pages, workers=8 if req.provider == "anthropic" else 1,
                                      progress=run["progress"], audience=req.audience.strip())
            s = crowd.summarise(results)
            issues = crowd.group_issues(backend, s)
            cost = backend.cost_usd()
            run.update(status="done", summary={k: v for k, v in s.items() if not k.endswith("_all")}, issues=issues, cost_usd=cost,
                       html=report.crowd_html(url, req.goal, pages, s, issues, f"{backend.provider}/{backend.model}", cost))
        except Exception as e:
            run.update(status="failed", error=f"{type(e).__name__}: {e}"[:300])

    run["task"] = asyncio.create_task(asyncio.to_thread(job))
    return {"id": rid, "estimate_usd": est}


@app.get("/api/runs/{rid}")
def status(rid: str) -> dict:
    run = RUNS.get(rid)
    if not run:
        raise HTTPException(404, "No such test.")
    return {k: v for k, v in run.items() if k != "task"}


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return PAGE


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>crowdtest: watch a crowd try your site</title><style>
:root{--bg:#f2f3f5;--surface:#fff;--sunk:#e6e8ec;--ink:#101216;--mut:#5b616b;--line:#dcdfe5;--acc:#1f5fbf;--on:#fff;--yes:#17803d;--maybe:#c48a00;--no:#c2321f;--fail:#8a8f98}
@media(prefers-color-scheme:dark){:root{--bg:#0d0f12;--surface:#161a1f;--sunk:#222830;--ink:#eef1f5;--mut:#9aa2ad;--line:#2a3038;--acc:#6aa0ff;--on:#0d0f12;--yes:#58c27a;--maybe:#e6b23f;--no:#f07462;--fail:#5f6670}}
[hidden]{display:none!important}*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:17px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;-webkit-font-smoothing:antialiased}
a{color:inherit}button,input,select,textarea{font:inherit;color:inherit}
.wrap{max-width:1240px;margin:0 auto;padding:0 28px}
.top{display:flex;align-items:center;justify-content:space-between;height:72px}
.mark{display:flex;align-items:center;gap:10px;font-weight:700;font-size:19px;letter-spacing:-.02em;text-decoration:none}.mark svg{width:26px;height:26px;color:var(--acc)}
.top a.gh{font-size:15px;color:var(--mut);text-decoration:none}.top a.gh:hover{color:var(--ink)}
.hero{display:grid;grid-template-columns:1fr 1fr;gap:56px;padding:48px 0 96px;align-items:start}
h1{font-size:clamp(44px,6vw,82px);line-height:.94;letter-spacing:-.045em;margin:0;max-width:10ch}
.lead{font-size:20px;color:var(--mut);max-width:38ch;margin:26px 0 32px}
.seg{display:inline-flex;background:var(--sunk);border-radius:4px;padding:4px;margin-bottom:22px}
.seg button{border:0;background:none;padding:10px 18px;border-radius:3px;font-weight:600;font-size:15px;color:var(--mut);cursor:pointer}
.seg button[aria-selected=true]{background:var(--surface);color:var(--ink)}
form{display:grid;gap:16px}
label{display:grid;gap:6px;font-size:15px;font-weight:600}
input,select,textarea{border:1px solid var(--line);background:var(--surface);border-radius:4px;padding:13px 14px;font-size:17px;font-weight:400;width:100%}
input:focus,select:focus,textarea:focus{outline:3px solid color-mix(in srgb,var(--acc) 40%,transparent);outline-offset:1px;border-color:var(--acc)}
textarea{resize:vertical;min-height:84px}
.two{display:grid;grid-template-columns:1fr 1fr;gap:14px}.three{display:grid;grid-template-columns:1fr 1fr 1fr;gap:14px}
.go{display:flex;align-items:center;gap:18px;flex-wrap:wrap;margin-top:6px}
.btn{border:0;background:var(--acc);color:var(--on);border-radius:4px;padding:15px 26px;font-weight:650;font-size:17px;cursor:pointer;transition:opacity .15s}
.btn:hover{opacity:.88}.btn:active{opacity:.75}.btn:disabled{opacity:.5;cursor:progress}
.cost{font-size:15px;color:var(--mut)}.cost b{color:var(--ink)}
.fine{font-size:14px;color:var(--mut);margin:0;max-width:56ch}.err{color:var(--no);font-weight:600}
.ppl{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px}
.ppl label{display:flex;gap:8px;align-items:flex-start;font-weight:400;font-size:14px;background:var(--surface);border:1px solid var(--line);border-radius:4px;padding:9px 10px;cursor:pointer}
.ppl input{width:auto;margin-top:3px}.ppl b{display:block;font-size:15px}
.stage{background:var(--surface);border-radius:6px;padding:28px;position:sticky;top:20px}
.stage h2{font-size:22px;margin:0;letter-spacing:-.02em}.stage .sub{color:var(--mut);font-size:15px;margin:4px 0 20px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(var(--d,14px),1fr));gap:5px}
.grid i{display:block;aspect-ratio:1;border-radius:50%;background:var(--sunk);transition:background .35s,transform .35s}
.grid i.y{background:var(--yes)}.grid i.m{background:var(--maybe)}.grid i.n{background:var(--no)}.grid i.x{background:var(--fail)}
.grid i.pop{animation:pop .45s ease-out}@keyframes pop{40%{transform:scale(1.5)}}
.legend{display:flex;gap:18px;flex-wrap:wrap;font-size:14px;color:var(--mut);margin-top:20px}.legend span{display:flex;align-items:center;gap:7px}
.legend i{width:11px;height:11px;border-radius:50%;display:inline-block}
.tally{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-top:22px}
.tally div{background:var(--bg);border-radius:4px;padding:14px}.tally b{display:block;font-size:30px;letter-spacing:-.03em;line-height:1.1}.tally span{font-size:14px;color:var(--mut)}
.vis{display:grid;gap:10px}.vis .v{background:var(--bg);border-radius:4px;padding:14px 16px}
.vis .v b{font-size:16px}.vis .v small{color:var(--mut);font-size:14px;margin-left:6px}
.track{height:6px;background:var(--sunk);border-radius:3px;margin-top:10px;overflow:hidden}.track i{display:block;height:100%;background:var(--acc);transition:width .5s}
.v.done .track i{background:var(--yes)}.v.failed .track i{background:var(--no)}
.empty{color:var(--mut);font-size:15px}
.res{background:var(--surface);padding:64px 0 88px}
.res h2{font-size:clamp(30px,3.4vw,46px);letter-spacing:-.035em;line-height:1.02;margin:0 0 8px}
.res .sub{color:var(--mut);margin:0 0 28px}
iframe{width:100%;height:85vh;border:0;border-radius:6px;background:var(--bg)}
footer{padding:40px 0 48px;color:var(--mut);font-size:14px}
@media(max-width:900px){.hero{grid-template-columns:1fr;gap:36px;padding:24px 0 56px}.stage{position:static;padding:20px}.wrap{padding:0 18px}.three{grid-template-columns:1fr 1fr}}
@media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
</style></head><body>
<div class="wrap"><header class="top"><a class="mark" href="/"><svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><circle cx="5" cy="6" r="2.4"/><circle cx="12" cy="6" r="2.4"/><circle cx="19" cy="6" r="2.4"/><circle cx="5" cy="13" r="2.4"/><circle cx="12" cy="13" r="2.4"/><circle cx="19" cy="13" r="2.4" opacity=".35"/><circle cx="5" cy="20" r="2.4"/><circle cx="12" cy="20" r="2.4" opacity=".35"/><circle cx="19" cy="20" r="2.4" opacity=".35"/></svg>crowdtest</a>
<a class="gh" href="https://github.com/NiharPatel2545/crowdtest" target="_blank" rel="noopener">GitHub</a></header>
<section class="hero"><div><h1>Watch a crowd try your site.</h1>
<p class="lead">Hundreds of simulated people read your pages and tell you what confused them, what was missing and what stopped them.</p>
<div class="seg" role="tablist"><button type="button" role="tab" aria-selected="true" data-t="c">Crowd · hundreds of people</button><button type="button" role="tab" aria-selected="false" data-t="v">Visitors · real browsers</button></div>
<div id="key"></div>
<form id="fc" data-pane="c"><label>Website<input id="curl" placeholder="yoursite.com" required></label>
<label>What should people try to do?<textarea id="cgoal" required placeholder="Understand what this product does and decide whether to try it"></textarea></label>
<label>Who is the site for?<input id="caud" placeholder="Leave empty for the general public, or e.g. small cafe owners"></label>
<div class="three"><label>People<input id="cn" type="number" min="10" max="1000" value="100"></label><label>Pages to read<input id="cpages" type="number" min="1" max="15" value="8"></label>
<label>Engine<select id="cprov"><option value="anthropic">Claude Haiku</option><option value="ollama">Ollama (free)</option></select></label></div>
<div class="go"><button class="btn" id="cgo">Start the crowd</button><span class="cost" id="ccost"></span></div>
<p id="cst" class="fine" aria-live="polite">The site is read once; each person is built from mixed traits and makes one short call. It shows how people understand and trust your pages, not broken clicks.</p></form>
<form id="f" data-pane="v" hidden><label>Website<input id="url" placeholder="yoursite.com" required></label>
<label>What should visitors try to do?<textarea id="goal" required placeholder="Find the price and sign up for the free plan"></textarea></label>
<div><div style="font-size:15px;font-weight:600;margin-bottom:8px">Visitors</div><div class="ppl" id="ppl"></div></div>
<div class="three"><label>Max steps<input id="steps" type="number" min="5" max="40" value="20"></label>
<label>Engine<select id="prov"><option value="anthropic">Claude Haiku</option><option value="ollama">Ollama (free)</option></select></label>
<label>Mode<select id="lean"><option value="true">Lean, cheapest</option><option value="false">Full, sees screenshots</option></select></label></div>
<input id="model" type="hidden">
<div class="go"><button class="btn" id="go">Send the visitors</button><span class="cost">Estimated cost <b id="est">…</b></span></div>
<p id="st" class="fine" aria-live="polite">Real browser visitors find broken flows. They use fake test data only, never pay or send messages, and stay on your site.</p></form></div>
<aside class="stage" aria-live="polite"><div data-pane="c"><h2 id="ctitle">The crowd</h2><p class="sub" id="csub">100 people, waiting for a site to read.</p><div class="grid" id="grid"></div>
<div class="legend"><span><i style="background:var(--yes)"></i>Would do it</span><span><i style="background:var(--maybe)"></i>Maybe</span><span><i style="background:var(--no)"></i>Would not</span><span><i style="background:var(--sunk)"></i>Waiting</span></div>
<div class="tally"><div><b id="ty">–</b><span>would do it</span></div><div><b id="tm">–</b><span>maybe</span></div><div><b id="tn">–</b><span>would not</span></div></div></div>
<div data-pane="v" hidden><h2>The visitors</h2><p class="sub">Each one drives a real browser and reports back.</p><div class="vis" id="vis"><p class="empty">Pick visitors and send them to your site.</p></div></div></aside></section></div>
<section class="res" id="res" hidden><div class="wrap"><h2 id="rtitle"></h2><p class="sub" id="rsub"></p><iframe id="out" title="Full report"></iframe></div></section>
<div class="wrap"><footer>crowdtest is open source and runs on your computer only. Simulated people are more patient and agreeable than real ones: read the numbers as relative, never as a forecast.</footer></div>
<script>
const $ = s => document.querySelector(s), esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'})[c])
let cfg, shown = 0
const post = async (u, body) => { const r = await fetch(u, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)}), d = await r.json()
  if (!r.ok) throw new Error(typeof d.detail === 'string' ? d.detail : (d.detail?.[0]?.msg || 'Could not start.')); return d }
const chosen = () => [...document.querySelectorAll('#ppl input:checked')].map(i => i.value)
document.querySelectorAll('[data-t]').forEach(b => b.addEventListener('click', () => { document.querySelectorAll('[data-t]').forEach(x => x.setAttribute('aria-selected', String(x === b)))
  document.querySelectorAll('[data-pane]').forEach(f => f.hidden = f.dataset.pane !== b.dataset.t) }))
function dots(n){ const g = $('#grid'); g.style.setProperty('--d', n > 600 ? '9px' : n > 250 ? '12px' : '16px'); g.innerHTML = '<i></i>'.repeat(n); shown = 0
  $('#csub').textContent = `${n} people, waiting for a site to read.`; ['ty', 'tm', 'tn'].forEach(k => $('#' + k).textContent = '–') }
function ccost(){ const n = +$('#cn').value || 0; $('#ccost').innerHTML = $('#cprov').value === 'ollama' ? 'Free, runs on this computer' : `About <b>US$${(n * 0.0006).toFixed(2)}</b>` }
$('#cn').addEventListener('input', () => { const n = Math.max(10, Math.min(1000, +$('#cn').value || 10)); dots(n); ccost() }); $('#cprov').addEventListener('change', ccost)
async function est(){ const r = await (await fetch(`/api/estimate?n=${chosen().length}&max_steps=${$('#steps').value}&model=${encodeURIComponent($('#model').value)}&provider=${$('#prov').value}&lean=${$('#lean').value}`)).json()
  $('#est').textContent = r.usd === 0 ? 'free' : `up to US$${r.usd.toFixed(2)}` }
async function init(){ cfg = await (await fetch('/api/config')).json()
  $('#key').innerHTML = cfg.key_loaded ? '' : '<p class="fine err" style="margin-bottom:16px">No Anthropic API key loaded: pick Ollama (free), or restart with --env-file.</p>'
  if (!cfg.key_loaded) { $('#cprov').value = 'ollama'; $('#prov').value = 'ollama' }
  $('#ppl').innerHTML = cfg.personas.map((p, i) => `<label><input type="checkbox" value="${p.id}" ${i < 3 ? 'checked' : ''}><span><b>${esc(p.name)}</b>${esc(p.profile.split(',')[0].split('.')[0])} · ${p.device}</span></label>`).join('')
  const syncModel = () => $('#model').value = $('#prov').value === 'ollama' ? 'qwen3:8b' : cfg.default_model
  $('#prov').addEventListener('change', () => { syncModel(); est() }); syncModel()
  document.querySelectorAll('#ppl input, #steps, #lean').forEach(el => el.addEventListener('change', est)); est(); dots(100); ccost() }
function paint(marks){ const cells = $('#grid').children
  for (; shown < marks.length && shown < cells.length; shown++) { cells[shown].className = marks[shown] + ' pop' }
  const n = marks.length || 1, pct = k => Math.round(100 * marks.filter(m => m === k).length / n) + '%'
  if (marks.length) { $('#ty').textContent = pct('y'); $('#tm').textContent = pct('m'); $('#tn').textContent = pct('n') } }
function done(r, title, sub){ $('#rtitle').textContent = title; $('#rsub').textContent = sub; $('#out').srcdoc = r.html; $('#res').hidden = false
  setTimeout(() => $('#res').scrollIntoView({behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth'}), 600) }
async function cpoll(id){ const r = await (await fetch('/api/runs/' + id)).json(), p = r.progress || {}
  if (p.total) { $('#csub').textContent = `${p.done} of ${p.total} people have reacted${p.failed ? ` (${p.failed} failed)` : ''}.`; paint(p.marks || []) } else $('#csub').textContent = 'Reading your pages…'
  if (r.status === 'running') return setTimeout(() => cpoll(id), 1200)
  $('#cgo').disabled = false
  if (r.status === 'failed') { $('#cst').className = 'fine err'; $('#cst').textContent = r.error; return }
  const s = r.summary; $('#cst').className = 'fine'; $('#cst').textContent = `Done: ${s.answered} of ${s.people} reacted · ${r.cost_usd ? 'cost US$' + r.cost_usd.toFixed(3) : 'free'}.`
  done(r, `${s.would_complete_pct}% would do it, ${s.maybe_pct}% maybe.`, `What ${s.answered} people found on ${r.url}: the problems, who struggles, and their own words.`) }
$('#fc').addEventListener('submit', async e => { e.preventDefault(); const n = Math.max(10, Math.min(1000, +$('#cn').value || 100)); dots(n)
  $('#cgo').disabled = true; $('#cst').className = 'fine'; $('#cst').textContent = 'Starting…'; $('#res').hidden = true
  try { const d = await post('/api/crowd', {url: $('#curl').value, goal: $('#cgoal').value, audience: $('#caud').value, people: n, max_pages: +$('#cpages').value, provider: $('#cprov').value})
    $('#cst').textContent = d.estimate_usd ? `Running, about US$${d.estimate_usd.toFixed(2)} at most.` : 'Running on this computer, free.'; cpoll(d.id)
  } catch (err) { $('#cst').className = 'fine err'; $('#cst').textContent = err.message; $('#cgo').disabled = false } })
async function vpoll(id){ const r = await (await fetch('/api/runs/' + id)).json(), max = +$('#steps').value
  $('#vis').innerHTML = Object.values(r.progress).map(p => `<div class="v ${p.status}"><b>${esc(p.name)}</b><small>${p.status === 'running' ? 'step ' + p.step : p.status}</small><div class="track"><i style="width:${p.status === 'done' || p.status === 'failed' ? 100 : Math.min(100, 100 * p.step / max)}%"></i></div></div>`).join('')
  if (r.status === 'running') return setTimeout(() => vpoll(id), 2000)
  $('#go').disabled = false
  if (r.status === 'failed') { $('#st').className = 'fine err'; $('#st').textContent = r.error; return }
  const s = r.summary; $('#st').className = 'fine'; $('#st').textContent = `Done · cost US$${s.cost_usd.toFixed(3)}.`
  done(r, `${s.achieved} of ${s.visitors} visitors reached the goal.`, 'Where each one got stuck, in their own words, with the last screen they saw.') }
$('#f').addEventListener('submit', async e => { e.preventDefault(); $('#go').disabled = true; $('#st').className = 'fine'; $('#st').textContent = 'Starting…'; $('#res').hidden = true
  try { const d = await post('/api/runs', {url: $('#url').value, goal: $('#goal').value, personas: chosen(), max_steps: +$('#steps').value, provider: $('#prov').value, model: $('#model').value, lean: $('#lean').value === 'true'})
    $('#st').textContent = d.estimate_usd ? `Running, up to US$${d.estimate_usd.toFixed(2)}. A few minutes.` : 'Running on this computer, free. A few minutes.'; vpoll(d.id)
  } catch (err) { $('#st').className = 'fine err'; $('#st').textContent = err.message; $('#go').disabled = false } })
init()
</script></body></html>"""


def serve(port: int = 8720, key_loaded: bool = False) -> None:
    import socket
    import uvicorn
    with socket.socket() as probe:                     # say so plainly if something already uses the port
        if probe.connect_ex(("127.0.0.1", port)) == 0:
            print(f"Port {port} is already in use: crowdtest may already be running at http://127.0.0.1:{port}. "
                  f"Close the other window, or pick another port with --port.")
            return
    STATE["key_loaded"] = key_loaded
    print(f"crowdtest is running: open http://127.0.0.1:{port}  (Ctrl+C to stop)" + ("" if key_loaded else "  [no API key loaded]"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
