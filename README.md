# crowdtest

Simulated visitors try one goal on your website and tell you where they got stuck. Each visitor is an AI browser agent
(browser-use + Claude) playing a different person: a rushed phone user, a sceptic, a 68-year-old who distrusts unlabelled
icons, a non-native English speaker, and so on. You get a report with how many reached the goal, the problems most of them
hit (grouped, with a fix for each) and a card per visitor with their own words and the last screen they saw.

```
crowdtest https://yoursite.com --goal "find the price and start a free trial" --visitors 5
5 visitors, up to 25 steps each, claude-haiku-5-5: estimated cost up to US$0.17
3 of 5 reached the goal; median 14 steps; cost US$0.061
  [blocker] Sign-up button does nothing on mobile (2 visitors)
  [major] Pricing only reachable from the footer (3 visitors)
Report: crowdtest-report.html
```

## What it is for, and what it is not

Use it before launch or after a redesign to find usability problems cheaply: confusing labels, buried pages, broken flows,
missing information, things that look untrustworthy. It does **not** tell you whether people will buy: simulated visitors
are more patient and agreeable than real ones. Treat their quotes as hints, never as customer feedback.

## Safety built in

Every visitor is told to use only obviously fake test data, never to pay, subscribe, delete an account or message a real
person (it stops at the payment step and reports how far it got), and not to attack the site. Browsing is restricted to the
target domain. Run it on sites you own or have permission to test.

## Install and run (Windows PowerShell)

```powershell
uv venv --python 3.12 .venv
uv pip install --python .venv\Scripts\python.exe -e . pytest
.venv\Scripts\python.exe -m crowdtest https://yoursite.com --goal "sign up for the free plan" --dry-run
$env:ANTHROPIC_API_KEY = "<your key>"
.venv\Scripts\python.exe -m crowdtest https://yoursite.com --goal "sign up for the free plan" --visitors 5
```

browser-use needs a Chromium install; if it is missing, run `uvx playwright install chromium`.

### Web page

```powershell
.venv\Scripts\python.exe -m crowdtest serve --env-file C:\path\to\.env
```

Open http://127.0.0.1:8720: enter the site and the goal, tick the visitors, see the cost estimate, press **Start the test**.
Progress shows live per visitor and the report opens on the page when they finish. One test at a time, and the server
refuses anything estimated above US$2. It listens on this computer only and answers only its own page; the API key is
never sent to the browser.

Options: `--visitors N` (1-8 from the built-in library), `--persona-file my_customers.json` (your own list of
`{id, name, profile, behaviour, device}`), `--max-steps`, `--concurrency`, `--model` (default `claude-haiku-5-5`),
`--max-cost` (refuses to start above this estimate, default US$1), `--env-file` (reads only the ANTHROPIC_API_KEY line),
`--json`, `--dry-run` (prints the prompts and estimate, calls nothing).

## Cost

Haiku 5.5 costs US$0.10 per million input tokens and US$0.50 per million output. A visitor of about 25 steps costs roughly
US$0.02-0.04, so a five-visitor test is a few cents. The report shows the measured cost.

## Tests

`.venv\Scripts\python.exe -m pytest -q` runs the offline checks (no browser, no API calls): prompts carry the safety rules,
the summary counts are right, visitor text is escaped, and the cost guard stops an expensive run before any call.
