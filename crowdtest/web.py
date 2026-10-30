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
:root{--bg:#f7f7f4;--card:#fff;--ink:#16190f;--mut:#5b6155;--line:#e2e4dc;--acc:#2f6f2a;--on:#fff;--bad:#c0392b;--warn:#b9770e}
@media(prefers-color-scheme:dark){:root{--bg:#0f120e;--card:#171b16;--ink:#eef0ea;--mut:#a3aa9c;--line:#2a3027;--acc:#8fd16a;--on:#0f120e;--bad:#ef6f5e;--warn:#e3a23f}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1000px;margin:0 auto;padding:28px 16px 48px}h1{margin:0;font-size:30px}p.mut,.mut{color:var(--mut)}
form,.box{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:18px;display:grid;gap:12px;margin-top:16px}
label{font-size:14px;color:var(--mut);display:grid;gap:6px}input,select,textarea{font:inherit;padding:11px 13px;border-radius:10px;border:1px solid var(--line);background:var(--bg);color:var(--ink)}
.ppl{display:grid;gap:8px;grid-template-columns:repeat(auto-fit,minmax(220px,1fr))}.ppl label{flex-direction:row;display:flex;gap:10px;align-items:flex-start;background:var(--bg);border:1px solid var(--line);border-radius:12px;padding:10px;color:var(--ink);cursor:pointer}
.ppl small{display:block;color:var(--mut)}.row{display:flex;gap:12px;flex-wrap:wrap;align-items:end}.row label{flex:1 1 160px}
button{font:inherit;font-weight:600;padding:12px 22px;border:0;border-radius:999px;background:var(--acc);color:var(--on);cursor:pointer}button:disabled{opacity:.6;cursor:not-allowed}
.err{color:var(--bad)}.warn{color:var(--warn)}.v{display:flex;justify-content:space-between;gap:10px;padding:8px 0;border-top:1px solid var(--line)}.v:first-child{border-top:0}
.bar{height:6px;border-radius:6px;background:var(--line);overflow:hidden;margin-top:4px}.bar i{display:block;height:100%;background:var(--acc)}
iframe{width:100%;height:85vh;border:1px solid var(--line);border-radius:16px;margin-top:16px;background:var(--card)}
</style></head><body><main>
<h1>crowdtest</h1><p class="mut">Simulated visitors try one goal on your website and tell you where they got stuck.</p>
<div id="key"></div>
<form id="f"><label>Website<input id="url" placeholder="https://yoursite.com" required></label>
<label>What should visitors try to do?<textarea id="goal" rows="2" required placeholder="e.g. find the price and sign up for the free plan"></textarea></label>
<div><div class="mut" style="font-size:14px;margin-bottom:6px">Visitors</div><div class="ppl" id="ppl"></div></div>
<div class="row"><label>Max steps per visitor<input id="steps" type="number" min="5" max="40" value="20"></label>
<label>Engine<select id="prov"><option value="anthropic">Claude (paid, best)</option><option value="ollama">Ollama on this PC (free)</option></select></label>
<label>Model<input id="model" list="models"><datalist id="models"></datalist></label>
<label>Mode<select id="lean"><option value="true">Lean (no screenshots, cheapest)</option><option value="false">Full (sees screenshots)</option></select></label><div><div class="mut" style="font-size:14px">Estimated cost</div><b id="est">…</b></div></div>
<div class="row"><button id="go">Start the test</button><span id="st" aria-live="polite"></span></div>
<p class="mut" style="margin:0;font-size:13px">Visitors use only fake test data, never pay or send messages, and stay on your site. Test sites you own or may test. They find usability problems; they do not predict demand.</p></form>
<div class="box" id="live" hidden><b>Visitors at work</b><div id="prog"></div></div><iframe id="out" title="Report" hidden></iframe>
<script>
const $ = s => document.querySelector(s), esc = s => String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'})[c])
let cfg
const chosen = () => [...document.querySelectorAll('#ppl input:checked')].map(i => i.value)
async function est(){ const r = await (await fetch(`/api/estimate?n=${chosen().length}&max_steps=${$('#steps').value}&model=${encodeURIComponent($('#model').value)}&provider=${$('#prov').value}&lean=${$('#lean').value}`)).json()
  $('#est').textContent = r.usd === 0 ? 'free' : `up to US$${r.usd.toFixed(2)}`; $('#est').className = r.usd > cfg.cap_usd ? 'err' : '' }
async function init(){ cfg = await (await fetch('/api/config')).json()
  $('#key').innerHTML = cfg.key_loaded ? '' : '<p class="box warn">No Anthropic API key loaded, so tests cannot start. Restart with ANTHROPIC_API_KEY set or --env-file.</p>'
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
init()
</script></main></body></html>"""


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
