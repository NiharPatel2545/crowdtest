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


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>crowdtest</title><style>
:root{--bg:#f6f7f3;--card:#fff;--ink:#14170f;--mut:#5c6356;--line:#e3e6dd;--acc:#2f6f2a;--acc2:#e9f2e4;--on:#fff;--bad:#c0392b;--warn:#b9770e}
@media(prefers-color-scheme:dark){:root{--bg:#0d100c;--card:#151a14;--ink:#eef1ea;--mut:#a2aa9b;--line:#262d23;--acc:#8fd16a;--acc2:#1b2617;--on:#0d100c;--bad:#ef6f5e;--warn:#e3a23f}}
[hidden]{display:none!important}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
.top{border-bottom:1px solid var(--line);background:var(--card)}.top div{max-width:1000px;margin:0 auto;padding:14px 16px;display:flex;align-items:center;gap:10px;justify-content:space-between}
.brand{display:flex;align-items:center;gap:10px;font-weight:700;font-size:18px;letter-spacing:-.01em}.brand svg{width:28px;height:28px;color:var(--acc)}
.top a{color:var(--mut);font-size:14px;text-decoration:none}.top a:hover{color:var(--ink)}
main{max-width:1000px;margin:0 auto;padding:32px 16px 56px}h1{margin:0;font-size:clamp(26px,4vw,34px);letter-spacing:-.02em}p.lead{color:var(--mut);margin:6px 0 22px;max-width:62ch}
form,.box{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:20px;display:grid;gap:14px;margin-top:16px;box-shadow:0 1px 2px rgba(0,0,0,.04)}
label{font-size:13px;font-weight:600;color:var(--mut);display:grid;gap:6px;text-transform:uppercase;letter-spacing:.04em}
input,select,textarea{font:inherit;text-transform:none;letter-spacing:0;font-weight:400;padding:12px 14px;border-radius:12px;border:1px solid var(--line);background:var(--bg);color:var(--ink)}
input:focus,select:focus,textarea:focus{outline:2px solid var(--acc);outline-offset:1px}
.row{display:flex;gap:12px;flex-wrap:wrap;align-items:end}.row label{flex:1 1 170px}
button{font:inherit;font-weight:600;padding:12px 24px;border:0;border-radius:999px;background:var(--acc);color:var(--on);cursor:pointer}button:hover{filter:brightness(1.05)}button:disabled{opacity:.55;cursor:not-allowed}
.note{margin:0;font-size:13px;color:var(--mut)}.err{color:var(--bad)}.warn{color:var(--warn)}.mut{color:var(--mut)}
iframe{width:100%;height:82vh;border:1px solid var(--line);border-radius:18px;margin-top:18px;background:var(--card)}
footer.site{max-width:1000px;margin:0 auto;padding:0 16px 32px;font-size:13px;color:var(--mut)}footer.site a{color:inherit}
.tabs{display:inline-flex;background:var(--card);border:1px solid var(--line);border-radius:999px;padding:4px;margin-top:4px}
.tabs button{background:none;color:var(--mut);padding:8px 18px}.tabs button[aria-selected=true]{background:var(--acc);color:var(--on)}
.ppl{display:grid;gap:8px;grid-template-columns:repeat(auto-fit,minmax(230px,1fr))}.ppl label{text-transform:none;letter-spacing:0;font-weight:400;display:flex;gap:10px;align-items:flex-start;background:var(--bg);border:1px solid var(--line);border-radius:12px;padding:10px;color:var(--ink);cursor:pointer;font-size:14px}
.ppl small{display:block;color:var(--mut)}.kpi{font-size:13px;color:var(--mut)}.kpi b{display:block;font-size:20px;color:var(--ink)}
.v{display:flex;justify-content:space-between;gap:10px;padding:8px 0;border-top:1px solid var(--line)}.v:first-child{border-top:0}
.bar{height:6px;border-radius:6px;background:var(--line);overflow:hidden;margin-top:4px}.bar i{display:block;height:100%;background:var(--acc);transition:width .4s}
</style></head><body><header class="top"><div><span class="brand"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="8" cy="8" r="3"/><circle cx="16.5" cy="9.5" r="2.5"/><path d="M2.5 20c.6-3.4 3-5.5 5.5-5.5s4.9 2.1 5.5 5.5"/><path d="M14 15.2c.8-.5 1.6-.7 2.5-.7 2.1 0 4 1.7 4.5 4.5"/></svg>crowdtest</span><a href="https://github.com/NiharPatel2545/crowdtest" target="_blank" rel="noopener">GitHub</a></div></header><main>
<h1>Where do people get stuck on your site?</h1><p class="lead">Simulated people try your website and report what confused them, what was missing and what made them hesitate.</p>
<div class="tabs" role="tablist"><button type="button" role="tab" aria-selected="true" data-t="v">Browsing visitors</button><button type="button" role="tab" aria-selected="false" data-t="c">Crowd (100s of people)</button></div>
<div id="key"></div>
<form id="f" data-pane="v"><label>Website<input id="url" placeholder="https://yoursite.com" required></label>
<label>What should visitors try to do?<textarea id="goal" rows="2" required placeholder="e.g. find the price and sign up for the free plan"></textarea></label>
<div><label style="margin-bottom:6px">Visitors</label><div class="ppl" id="ppl"></div></div>
<div class="row"><label>Max steps<input id="steps" type="number" min="5" max="40" value="20"></label>
<label>Engine<select id="prov"><option value="anthropic">Claude (paid, best)</option><option value="ollama">Ollama on this PC (free)</option></select></label>
<label>Model<input id="model" list="models"><datalist id="models"></datalist></label>
<label>Mode<select id="lean"><option value="true">Lean (cheapest)</option><option value="false">Full (sees screenshots)</option></select></label><div class="kpi">Estimated cost<b id="est">…</b></div></div>
<div class="row"><button id="go">Start the test</button><span id="st" aria-live="polite"></span></div>
<p class="note">Real browser visitors find broken flows. They use only fake test data, never pay or send messages, and stay on your site.</p></form>
<form id="fc" data-pane="c" hidden><label>Website<input id="curl" placeholder="https://yoursite.com" required></label>
<label>What should people try to do?<textarea id="cgoal" rows="2" required placeholder="e.g. understand what this product does and decide whether to try it"></textarea></label>
<label>Who is the site for? (optional)<input id="caud" placeholder="e.g. farmer or farm adviser; leave empty for the general public"></label>
<div class="row"><label>People<input id="cn" type="number" min="10" max="1000" value="100"></label><label>Pages to read<input id="cpages" type="number" min="1" max="15" value="8"></label>
<label>Engine<select id="cprov"><option value="anthropic">Claude Haiku (about US$0.06 per 100)</option><option value="ollama">Ollama on this PC (free, slower)</option></select></label></div>
<div class="row"><button id="cgo">Start the crowd</button><span id="cst" aria-live="polite"></span></div>
<p class="note">The site is read once; each person is generated from mixed traits and makes one short call. It shows how different people understand and trust your pages, not broken clicks.</p></form>
<div class="box" id="live" hidden><b id="livet">Visitors at work</b><div id="prog"></div></div><iframe id="out" title="Report" hidden></iframe>
<script>
const $ = s => document.querySelector(s), esc = s => String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'})[c])
let cfg
const chosen = () => [...document.querySelectorAll('#ppl input:checked')].map(i => i.value)
async function est(){ const r = await (await fetch(`/api/estimate?n=${chosen().length}&max_steps=${$('#steps').value}&model=${encodeURIComponent($('#model').value)}&provider=${$('#prov').value}&lean=${$('#lean').value}`)).json()
  $('#est').textContent = r.usd === 0 ? 'free' : `up to US$${r.usd.toFixed(2)}`; $('#est').className = r.usd > cfg.cap_usd ? 'err' : '' }
async function init(){ cfg = await (await fetch('/api/config')).json()
  $('#key').innerHTML = cfg.key_loaded ? '' : '<p class="box warn">No Anthropic API key loaded: Claude runs cannot start (free Ollama runs still can). Restart with ANTHROPIC_API_KEY set or --env-file.</p>'
  $('#ppl').innerHTML = cfg.personas.map((p, i) => `<label><input type="checkbox" value="${p.id}" ${i < 3 ? 'checked' : ''}><span><b>${esc(p.name)}</b> · ${p.device}<small>${esc(p.profile)}</small></span></label>`).join('')
  $('#models').innerHTML = [...cfg.models, 'qwen3:8b', 'qwen2.5:7b', 'llama3.1:8b'].map(m => `<option value="${m}">`).join(''); $('#model').value = cfg.default_model
  $('#prov').addEventListener('change', () => { $('#model').value = $('#prov').value === 'ollama' ? 'qwen3:8b' : cfg.default_model; est() })
  document.querySelectorAll('#ppl input, #steps, #model, #lean').forEach(el => el.addEventListener('change', est)); est() }
async function poll(id){ const r = await (await fetch('/api/runs/' + id)).json(), max = +$('#steps').value
  $('#prog').innerHTML = Object.values(r.progress).map(p => `<div class="v"><span>${esc(p.name)} <span class="mut">· ${p.status}</span></span><span style="flex:0 0 40%"><span class="mut" style="font-size:13px">step ${p.step}</span><div class="bar"><i style="width:${p.status === 'done' ? 100 : Math.min(100, 100 * p.step / max)}%"></i></div></span></div>`).join('')
  if (r.status === 'running') return setTimeout(() => poll(id), 2000)
  $('#go').disabled = false
  if (r.status === 'failed') { $('#st').className = 'err'; $('#st').textContent = r.error; return }
  const s = r.summary; $('#st').className = ''; $('#st').textContent = `${s.achieved} of ${s.visitors} reached the goal · cost US$${s.cost_usd.toFixed(3)}`
  $('#out').srcdoc = r.html; $('#out').hidden = false }
$('#f').addEventListener('submit', async e => { e.preventDefault(); $('#go').disabled = true; $('#st').className = ''; $('#st').textContent = 'Starting…'; $('#out').hidden = true
  try { const r = await fetch('/api/runs', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({url: $('#url').value, goal: $('#goal').value, personas: chosen(), max_steps: +$('#steps').value, provider: $('#prov').value, model: $('#model').value, lean: $('#lean').value === 'true'})})
    const d = await r.json(); if (!r.ok) throw new Error(typeof d.detail === 'string' ? d.detail : (d.detail?.[0]?.msg || 'Could not start.'))
    $('#st').textContent = d.estimate_usd ? `Running (estimate up to US$${d.estimate_usd.toFixed(2)}). This takes a few minutes.` : 'Running on this computer (free). This takes a few minutes.'; $('#live').hidden = false; poll(d.id)
  } catch(err) { $('#st').className = 'err'; $('#st').textContent = err.message; $('#go').disabled = false } })
const post = async (u, body) => { const r = await fetch(u, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)}), d = await r.json()
  if (!r.ok) throw new Error(typeof d.detail === 'string' ? d.detail : (d.detail?.[0]?.msg || 'Could not start.')); return d }
document.querySelectorAll('[data-t]').forEach(b => b.addEventListener('click', () => { document.querySelectorAll('[data-t]').forEach(x => x.setAttribute('aria-selected', String(x === b)))
  document.querySelectorAll('[data-pane]').forEach(f => f.hidden = f.dataset.pane !== b.dataset.t) }))
async function cpoll(id){ const r = await (await fetch('/api/runs/' + id)).json(), p = r.progress || {}
  $('#prog').innerHTML = p.total ? `<div class="v"><span>${p.done} of ${p.total} people reacted${p.failed ? ` <span class="warn">(${p.failed} failed)</span>` : ''}</span><span style="flex:0 0 50%"><div class="bar"><i style="width:${100 * p.done / p.total}%"></i></div></span></div>` : '<span class="mut">Reading the site…</span>'
  if (r.status === 'running') return setTimeout(() => cpoll(id), 1500)
  $('#cgo').disabled = false
  if (r.status === 'failed') { $('#cst').className = 'err'; $('#cst').textContent = r.error; return }
  const s = r.summary; $('#cst').className = ''; $('#cst').textContent = `${s.would_complete_pct}% would complete the goal (${s.maybe_pct}% maybe) · ${s.answered} of ${s.people} reacted · ${r.cost_usd ? 'US$' + r.cost_usd.toFixed(3) : 'free'}`
  $('#out').srcdoc = r.html; $('#out').hidden = false }
$('#fc').addEventListener('submit', async e => { e.preventDefault(); $('#cgo').disabled = true; $('#cst').className = ''; $('#cst').textContent = 'Starting…'; $('#out').hidden = true
  try { const d = await post('/api/crowd', {url: $('#curl').value, goal: $('#cgoal').value, audience: $('#caud').value, people: +$('#cn').value, max_pages: +$('#cpages').value, provider: $('#cprov').value})
    $('#cst').textContent = d.estimate_usd ? `Running (estimate up to US$${d.estimate_usd.toFixed(2)})` : 'Running on this computer (free)'; $('#livet').textContent = 'The crowd is reading'; $('#live').hidden = false; cpoll(d.id)
  } catch(err) { $('#cst').className = 'err'; $('#cst').textContent = err.message; $('#cgo').disabled = false } })
init()
</script></main><footer class="site">crowdtest · open source · runs on your computer only</footer></body></html>"""


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
