"""Offline checks (no browser, no API calls). Pass marks: every prompt carries the safety rules; the summary counts match the
visits; the report escapes visitor text and labels quotes as simulated; the cost guard stops an expensive run before any call."""
from crowdtest import personas as P
from crowdtest import report
from crowdtest.cli import main
from crowdtest.run import estimate_cost, task_prompt


def visit(name, ok, stuck, ret="maybe", steps=10):
    return {"persona": name, "name": name, "profile": "p", "device": "desktop", "steps": steps, "seconds": 30, "cost_usd": 0.02,
            "errors": [], "last_screenshot": None,
            "visit": {"goal_achieved": ok, "summary": "s", "stuck_points": stuck, "trust_concerns": [], "missing": [],
                      "would_return": ret, "quote": "<b>hi</b>"}}


VISITS = [visit("A", True, ["Pricing link hidden in footer"], "yes", 8),
          visit("B", False, ["Pricing link hidden in footer", "Sign-up button did nothing"], "no", 20),
          {**visit("C", False, []), "visit": None, "errors": ["TimeoutError"], "cost_usd": 0.0, "steps": 0}]


def test_prompts_carry_safety_rules():
    for p in P.LIBRARY:
        t = task_prompt(p, "https://example.com", "sign up")
        assert "Never complete a payment" in t and "test@example.com" in t and "Stay on this website" in t
    assert P.pick(3)[0].device == "mobile" and len(P.pick(20)) == len(P.LIBRARY)


def test_summary_and_fallback():
    s = report.summary(VISITS, extra_cost=0.001)
    assert (s["visitors"], s["reported"], s["achieved"], s["success_pct"]) == (3, 2, 1, 33)
    assert s["would_return"] == {"yes": 1, "maybe": 0, "no": 1} and s["median_steps"] == 14 and s["cost_usd"] == 0.041
    top = report.fallback_issues(VISITS)[0]
    assert top["title"] == "Pricing link hidden in footer" and top["visitors"] == 2


def test_report_html():
    html = report.to_html("https://example.com", "sign up", VISITS, report.fallback_issues(VISITS), report.summary(VISITS))
    assert "<b>hi</b>" not in html and "&lt;b&gt;hi&lt;/b&gt;" in html
    assert "(simulated visitor)" in html and "Run failed" in html and "1 of 3" in html


def test_cost_guard_and_dry_run(capsys):
    assert estimate_cost(5, 25) < 1.0                      # Haiku: a normal test stays under a dollar
    assert main(["https://example.com", "--goal", "x", "--visitors", "8", "--max-steps", "400", "--max-cost", "0.5"]) == 2
    assert main(["https://example.com", "--goal", "x", "--visitors", "2", "--dry-run"]) == 0
    assert "Maya" in capsys.readouterr().out


def test_web_page_is_local_only_and_never_shows_the_key():
    from fastapi.testclient import TestClient
    from crowdtest.web import STATE, app
    STATE["key_loaded"] = False
    c = TestClient(app, base_url="http://127.0.0.1:8720")
    cfg = c.get("/api/config").json()
    assert cfg["key_loaded"] is False and "sk-" not in str(cfg)
    body = {"url": "https://example.com", "goal": "sign up please", "personas": ["rushed"]}
    assert c.post("/api/runs", json=body, headers={"Origin": "https://evil.example"}).status_code == 403
    assert c.post("/api/runs", json=body, headers={"Origin": "http://127.0.0.1:8720"}).status_code == 400   # no key: refused before any call
    assert TestClient(app, base_url="http://rebind.example").get("/").status_code == 403
