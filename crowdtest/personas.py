"""Simulated visitors. Each one has a background, a device and a temperament that changes how they use a site.
They find usability problems; they do not predict whether real people will buy (simulated users are too agreeable for that)."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

MOBILE = {"width": 390, "height": 844}
DESKTOP = {"width": 1366, "height": 900}


@dataclass
class Persona:
    id: str
    name: str
    profile: str          # who they are, in a sentence or two
    behaviour: str        # how they browse: patience, what they read, when they give up
    device: str = "desktop"

    @property
    def viewport(self) -> dict:
        return MOBILE if self.device == "mobile" else DESKTOP


LIBRARY = [
    Persona("rushed", "Maya, 29", "Marketing coordinator, checking the site on her phone between meetings.",
            "Skims, does not read long paragraphs, taps the most obvious button. Gives up after two or three dead ends.", "mobile"),
    Persona("sceptic", "Tom, 47", "Small-business owner who has been burned by software that overpromised.",
            "Looks for proof before acting: pricing, who is behind it, privacy, real examples. Notes anything vague or too good to be true."),
    Persona("low-tech", "Margaret, 68", "Retired teacher, comfortable with email and online banking, nothing more.",
            "Reads every word slowly, is unsure about icons without labels and jargon, worries about clicking the wrong thing."),
    Persona("comparer", "Arjun, 35", "Operations manager comparing three options for his team before a purchase decision.",
            "Methodical: wants features, limits and price side by side. Opens several pages to compare details."),
    Persona("power", "Lena, 31", "Software engineer who wants to get to the real thing quickly.",
            "Ignores marketing copy, looks for docs, an API, a quick start or a demo. Impatient with forced sign-ups and slow pages."),
    Persona("non-native", "Carlos, 40", "Restaurant owner; Spanish is his first language, his English is basic.",
            "Struggles with idioms and long sentences, relies on clear labels and pictures, re-reads unclear instructions.", "mobile"),
    Persona("first-visit", "Priya, 24", "Student who heard about the product from a friend and knows nothing else about it.",
            "Curious but has no context: needs the page to explain what it is and what to do first."),
    Persona("busy-parent", "Dan, 38", "Parent of two using a phone one-handed in the evening.",
            "Short attention span, interrupted easily, wants to finish the task in under five minutes.", "mobile"),
]


def pick(n: int) -> list[Persona]:
    return [LIBRARY[i % len(LIBRARY)] for i in range(max(1, min(n, len(LIBRARY))))]


def load(path: str) -> list[Persona]:
    """A JSON list of {id, name, profile, behaviour, device} for your own target customers."""
    data = json.loads(Path(path).read_text("utf-8"))
    return [Persona(**{k: str(v) for k, v in p.items() if k in Persona.__dataclass_fields__}) for p in data]


def to_dict(p: Persona) -> dict:
    return asdict(p)
