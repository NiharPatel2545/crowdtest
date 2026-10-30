"""Runs each persona as a browser agent (browser-use + Claude) on the target site and collects a structured account of the visit."""
from __future__ import annotations

import asyncio
import os
import time
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from crowdtest.personas import Persona

MODEL = "claude-haiku-5-5"
PRICE_PER_M = {"claude-haiku-5-5": (0.10, 0.50), "claude-sonnet-5-5": (2.00, 10.00), "claude-opus-5-5": (4.00, 20.00)}
TOKENS_PER_STEP = 12_000      # rough input per agent step (page state + history); used only for the up-front estimate


class Visit(BaseModel):
    """What the persona reports at the end of the visit (the agent fills this in)."""
    goal_achieved: bool = Field(description="Did you complete the goal?")
    summary: str = Field(description="One or two sentences: what you did and how it went")
    stuck_points: list[str] = Field(default_factory=list, description="Each place you got stuck, confused or had to guess, in order, naming the page or element")
    trust_concerns: list[str] = Field(default_factory=list, description="Anything that made you doubt the site or hesitate")
    missing: list[str] = Field(default_factory=list, description="Information or features you looked for and could not find")
    would_return: str = Field(description="yes, maybe or no")
    quote: str = Field(description="One honest sentence in your own voice about the experience")


def task_prompt(p: Persona, url: str, goal: str) -> str:
    return f"""You are {p.name}. {p.profile}
How you browse: {p.behaviour}
You are using a {'phone' if p.device == 'mobile' else 'laptop'}.

Go to {url} and try to do this: {goal}

Behave like this person, not like an expert tester: notice what they would notice, get confused where they would, and give up
where they would. Stay on this website; do not use search engines or other sites.

Rules for test data and safety:
- Never enter real personal information. If a form insists, use obviously fake test data: name "Test User", email test@example.com.
- Never complete a payment, purchase, subscription, account deletion, or anything that sends a message to a real person.
  If you reach a payment or confirmation step, stop there and report how far you got.
- Do not try to break or attack the site; use it as a normal visitor.

When you finish (or give up), report your visit honestly in the requested structure."""


def estimate_cost(n: int, max_steps: int, model: str = MODEL) -> float:
    pin, pout = PRICE_PER_M.get(model, PRICE_PER_M[MODEL])
    return n * max_steps * (TOKENS_PER_STEP * pin + 600 * pout) / 1e6


async def run_one(p: Persona, url: str, goal: str, max_steps: int, model: str, sem: asyncio.Semaphore) -> dict:
    from browser_use import Agent, BrowserProfile
    from browser_use.llm import ChatAnthropic

    host = urlparse(url).hostname or ""
    async with sem:
        t0 = time.time()
        out = {"persona": p.id, "name": p.name, "profile": p.profile, "device": p.device}
        try:
            agent = Agent(task=task_prompt(p, url, goal), llm=ChatAnthropic(model=model, max_tokens=8192),
                          output_model_schema=Visit, calculate_cost=True,
                          browser_profile=BrowserProfile(headless=True, window_size=p.viewport, viewport=p.viewport,
                                                         allowed_domains=[host, f"*.{host}"]))
            hist = await agent.run(max_steps=max_steps)
            visit = hist.structured_output
            usage = getattr(hist, "usage", None)
            tin = getattr(usage, "total_prompt_tokens", 0) or 0
            tout = getattr(usage, "total_completion_tokens", 0) or 0
            pin, pout = PRICE_PER_M.get(model, PRICE_PER_M[MODEL])
            shots = [s for s in (hist.screenshot_paths() or []) if s]
            out.update(
                finished=hist.is_done(), steps=hist.number_of_steps(), seconds=round(time.time() - t0),
                visit=visit.model_dump() if visit else None, final_text=hist.final_result(),
                errors=[str(e)[:300] for e in hist.errors() if e][:5], urls=[u for u in hist.urls() if u][:40],
                tokens_in=tin, tokens_out=tout, cost_usd=round((tin * pin + tout * pout) / 1e6, 4),
                last_screenshot=shots[-1] if shots else None)
        except Exception as e:                        # one failed persona must not lose the others
            out.update(finished=False, steps=0, seconds=round(time.time() - t0), visit=None, final_text=None,
                       errors=[f"{type(e).__name__}: {e}"[:300]], urls=[], tokens_in=0, tokens_out=0, cost_usd=0.0, last_screenshot=None)
        return out


async def run_all(personas: list[Persona], url: str, goal: str, max_steps: int = 25, model: str = MODEL, concurrency: int = 2) -> list[dict]:
    os.environ.setdefault("ANONYMIZED_TELEMETRY", "false")
    sem = asyncio.Semaphore(concurrency)
    return await asyncio.gather(*(run_one(p, url, goal, max_steps, model, sem) for p in personas))
