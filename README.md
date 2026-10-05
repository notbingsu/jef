# jef

A personal browser agent driven by a tree of skills. Forked from [browser-use/jev-ultrafast](https://github.com/browser-use/jev-ultrafast).

Each step reads the page into an indexed element table. In one request, [TypeSafe's Jev](https://docs.typesafe.ai/introduction) picks an operation (`CLICK`, `TYPE_TEXT`, `SELECT`, `SCROLL_*`, `WAIT`, `DONE`, `BLOCKED`) and a target element for it. A small LLM writes text only for `TYPE_TEXT`. Code owns execution: model output never becomes selectors, coordinates or JavaScript.

## Setup

```bash
uv sync
cp .env.example .env              # add TYPESAFE_API_KEY and TEXT_MODEL_API_KEY
uv run browser-harness --doctor   # connect Chrome; allow remote debugging when prompted
```

Runs use your existing Chrome profile, so sites you're logged into stay logged in. `TEXT_MODEL` can be any OpenAI-compatible model (set `TEXT_MODEL_BASE_URL`) or a `claude-*` model (uses the Anthropic API with `TEXT_MODEL_API_KEY` as the Anthropic key).

## Run a skill

```bash
uv run jev list
uv run jev run calendar/create-event "Dentist, Friday Oct 9, 3pm, 1 hour"
uv run jev run linkedin-dms/reply "Ada Lovelace: thanks, Thursday works, I'll send an invite"
```

The agent opens a tab in the foreground, so you watch the real page. The terminal prints each action as it executes:

```text
calendar/create-event → https://calendar.google.com/calendar/r/week
   812 ms  CLICK [4] Create  93%
  1530 ms  TYPE_TEXT [2] Add title ← “Dentist”  97%
  ...
           confirm CLICK “Save”? [y/N]
```

The tab stays open afterwards (`--close` to close it). A JSON trace of every decision goes to `artifacts/runs/<skill>/`. `DONE` is the model's claim, so check the tab.

## The skill tree

Directories under `skills/` are branches; `.toml` files are leaves you can run. A leaf inherits from every `_branch.toml` above it: the deepest `url` wins, while `rules` and `confirm` accumulate.

```text
skills/
  _branch.toml             optional; applies to every skill
  calendar/
    _branch.toml           url, rules, confirm for all calendar skills
    create-event.toml
  linkedin-dms/
    _branch.toml
    reply.toml
```

| Key | Where | Meaning |
| --- | --- | --- |
| `task` | leaf, required | What the skill does. The run's details are appended to it to form the goal. |
| `description` | any | Shown by `jev list`. |
| `url` | any | Start page. A leaf needs one from itself or a branch. |
| `rules` | any | Your standing instructions for that site. Sent to every decision and to the text helper. |
| `confirm` | any | Phrases that gate execution. Clicking or selecting an element whose label contains one as whole words pauses for `y/N`. Without a terminal, the run stops instead. |

To add a use case, make a directory with a `_branch.toml` and add leaves. Nest directories for finer branches (`calendar/recurring/weekly-sync.toml`). Unknown keys are rejected, so typos fail loudly.

Prefer `rules` for site knowledge ("the message box is at the bottom of the thread") over scripted steps; the policy still decides every action from the live page. Skills that send messages or invites should list their send buttons in `confirm`. Automating LinkedIn can breach its terms of use, and LinkedIn may restrict accounts it flags.

## Library

```python
from jev_ultrafast import Agent, skills

skill = skills.load("calendar/create-event")
# approve(action, decision) -> bool answers confirm prompts; without it, a confirm action stops the run.
with Agent(skill.url, skill.task + "\nLunch with Sam, Tue 12:30, 45 min",
           rules=skill.rules, confirm=skill.confirm) as agent:
    for state in agent.run():
        print(state["elapsed_ms"], state["status"])
```

## Limits

The DOM reader handles common HTML and ARIA controls. Shadow roots, iframes, canvas, uploads, pop-up tabs, nested scroll containers and complex keyboard widgets can block progress. A run stops after 60 actions, 120 decisions, or three actions in a row that don't change the page. Freshness guards may make pages with live-updating content (chat timestamps, typing indicators) re-decide more often.

## Development

```bash
uv run ruff check .
uv run pytest                          # offline; no paid API calls
node --check jev_ultrafast/snapshot.js
uv run python scripts/check_guards.py  # real local browser, no model calls
```

See [docs/design.md](docs/design.md) for the runtime's freshness and execution rules.
