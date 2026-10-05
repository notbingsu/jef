# jef

A personal agent driven by a tree of skills. Forked from [browser-use/jev-ultrafast](https://github.com/browser-use/jev-ultrafast).

You say what you want in plain words; Jev picks the skill. Skills come in two kinds:

- **Browser skills** drive a real Chrome tab. Each step reads the page into an indexed element table. In one request, [TypeSafe's Jev](https://docs.typesafe.ai/introduction) picks an operation (`CLICK`, `TYPE_TEXT`, `SELECT`, `SCROLL_*`, `WAIT`, `DONE`, `BLOCKED`) and a target element for it. A small LLM writes text only for `TYPE_TEXT`. Code owns execution: model output never becomes selectors, coordinates or JavaScript.
- **API skills** call a service directly, with no browser. TypeSafe makes the closed-set calls (which event, which guests, which category) and the text model writes titles, times and search terms; code validates both and asks before changing anything. Google Calendar is the first, ported from [tele_gcal](https://github.com/notbingsu/tele_gcal).

## Setup

```bash
uv sync
cp .env.example .env              # add TYPESAFE_API_KEY and TEXT_MODEL_API_KEY
uv run browser-harness --doctor   # connect Chrome; allow remote debugging when prompted
```

Browser runs use your existing Chrome profile, so sites you're logged into stay logged in. `TEXT_MODEL` can be any OpenAI-compatible model (set `TEXT_MODEL_BASE_URL`) or a `claude-*` model (uses the Anthropic API with `TEXT_MODEL_API_KEY` as the Anthropic key).

For the Calendar API skills, create an OAuth client of type **Desktop app** in Google Cloud (Calendar API enabled) and save its JSON as `config/client_secrets.json`. The first calendar run opens a consent page and saves `config/token.json`. tele_gcal's `client_secrets.json` and `token.json` work as-is (same scope). Both files are git-ignored; the paths are configurable in `.env`.

## Ask jev

```bash
uv run jev dentist friday oct 9 at 3pm for an hour
uv run jev any flights this month?
uv run jev move the dentist to 5.30pm
uv run jev "reply to Ada Lovelace on linkedin: thanks, Thursday works, I'll send an invite"
```

Jev picks the skill from your words. One TypeSafe request carries a `use_case` question (each top-level branch, or `NONE`) plus a skill question for every use case with more than one leaf. Only the answer for the chosen use case counts, the same way the browser loop picks an operation and its target. Deeper branches are flattened into their top-level question, with their descriptions attached, and `api` skills are preferred over browser ones when both fit. Your whole request then goes to the chosen skill as its details.

```text
route: calendar/update-event  (86%, 410 ms)
calendar/update-event → gcal.update_event
```

If use case × skill confidence is under 50%, jev asks before running; without a terminal it stops. A `NONE` answer runs nothing. Routing works from `description` and `task`, so describe what each skill is for.

| Flag | Does |
| --- | --- |
| `--list` | Show the skill tree. |
| `--route-only` | Show which skill Jev picks, then stop. Useful while writing descriptions. |
| `--skill calendar/update-event` | Skip routing and run that leaf with the rest of the words as details. |
| `--close` | Close a browser skill's tab afterwards. |

A browser skill opens a tab in the foreground, so you watch the real page. The terminal prints each action as it executes:

```text
route: linkedin-dms/reply  (94%, 380 ms)
linkedin-dms/reply → https://www.linkedin.com/messaging/
   812 ms  CLICK [4] Ada Lovelace  93%
  1530 ms  TYPE_TEXT [9] Write a message… ← “Thanks, Thursday works…”  97%
           confirm CLICK “Send”? [y/N]
```

The tab stays open afterwards (`--close` to close it). `DONE` is the model's claim, so check the tab.

An API skill prints what it will do and waits for you before any create, update or delete:

```text
calendar/update-event → gcal.update_event
  from: Fri 9 Oct 3pm–4pm  Dentist
  to:   Fri 9 Oct 5.30pm–6.30pm  Dentist
  match: 96%
  Update “Dentist”? [y/N]
```

Every run writes a JSON trace to `artifacts/runs/<skill>/`.

## The skill tree

Directories under `skills/` are branches; `.toml` files are leaves you can run. A leaf inherits from every `_branch.toml` above it: the deepest `url` wins, `rules` and `confirm` accumulate, and `options` merge key by key.

```text
skills/
  calendar/
    _branch.toml           shared rules + options (calendar, colors, keywords)
    create-event.toml      api = "gcal.create_event"
    find-events.toml       api = "gcal.find_events"
    update-event.toml      api = "gcal.update_event"
    delete-event.toml      api = "gcal.delete_event"
    list-calendars.toml    api = "gcal.list_calendars"
    browser/
      _branch.toml         url, browser-only rules, confirm
      create-event.toml    browser fallback
  linkedin-dms/
    _branch.toml
    reply.toml
```

| Key | Where | Meaning |
| --- | --- | --- |
| `task` | leaf, required | What the skill does. The run's details are added to it. |
| `description` | any | What the skill is for. Jev routes on it; `jev --list` shows it. |
| `api` | leaf | Run this `service.operation` instead of a browser. |
| `url` | any | Start page. Browser leaves need one from themselves or a branch. |
| `rules` | any | Your standing instructions. Sent to every browser decision, the text helper, and API argument filling. |
| `confirm` | any | Browser only. Clicking or selecting an element whose label contains one of these phrases as whole words pauses for `y/N`. Without a terminal, the run stops instead. API skills always confirm changes. |
| `options` | any | A table of settings for API operations. |

To add a use case, make a directory with a `_branch.toml` and add leaves. Nest directories for finer branches (`calendar/recurring/weekly-sync.toml`). Unknown keys are rejected, so typos fail loudly.

Prefer `rules` for site knowledge ("the message box is at the bottom of the thread") over scripted steps; the policy still decides every action from the live page. Browser skills that send messages or invites should list their send buttons in `confirm`. Automating LinkedIn can breach its terms of use, and LinkedIn may restrict accounts it flags.

## Google Calendar

The `calendar/` operations ported from tele_gcal: list calendars, find events, create, update and delete. Its `preferences.json` defaults now live in `skills/calendar/_branch.toml`: the prompt preferences as `rules`, and the rest as `options`:

| Option | Default | Meaning |
| --- | --- | --- |
| `calendar_id` | `primary` | Which calendar. `jev list my calendars` shows IDs. |
| `timezone` | the calendar's own | IANA zone used for "tomorrow", "3pm", and new events. |
| `default_duration_minutes` | 60 | Length of a new event given only a start. |
| `max_results` | 10 | Events listed by a search. |
| `colors` | | Category → Google `colorId` (1–11). TypeSafe picks a category for create and update when one fits with 50%+ probability. |
| `keywords` | | Category → keywords. When TypeSafe judges the request is about a category ("flights"), the search lists the range and matches any keyword in an event's title, description or location. |

What the code guarantees, whatever the model returns:

- **Event IDs:** update and delete first search, then TypeSafe picks one result (or none). Under 50% probability the run stops and names the likeliest two; otherwise the prompt shows the match probability. No model ever supplies an ID.
- **Guests:** code finds the addresses in your details; TypeSafe judges, one address at a time, whether to invite it, and for update whether to remove each existing guest. Nobody else can be added. Google isn't asked to email guests (`sendUpdates` is left at its default, none).
- **Times:** they must parse, and an event must end after it starts. Moving an event with only a new start keeps its length.
- **Missing details:** if the request is missing something, the run stops with the model's question and changes nothing.
- **No retries:** a create, update or delete is never retried. If the connection drops mid-request, check the calendar.

Search defaults to upcoming events unless your details point at the past. Update and delete on a recurring event affect only the matched occurrence. tele_gcal's "add to calendar" template link isn't ported.

## Library

```python
from jev_ultrafast import Agent, skills

skill = skills.load("calendar/browser/create-event")
# approve(action, decision) -> bool answers confirm prompts; without it, a confirm action stops the run.
with Agent(skill.url, skill.task + "\nLunch with Sam, Tue 12:30, 45 min",
           rules=skill.rules, confirm=skill.confirm) as agent:
    for state in agent.run():
        print(state["elapsed_ms"], state["status"])
```

API operations are plain functions: `jev_ultrafast.gcal.OPERATIONS["create_event"](skill, details, confirm)`, where `confirm(title, lines) -> bool`. Routing is `jev_ultrafast.router.route(request)`, which returns the leaf path (or `None`) and the probabilities.

## Limits

The DOM reader handles common HTML and ARIA controls. Shadow roots, iframes, canvas, uploads, pop-up tabs, nested scroll containers and complex keyboard widgets can block progress. A browser run stops after 60 actions, 120 decisions, or three actions in a row that don't change the page. Freshness guards may make pages with live-updating content (chat timestamps, typing indicators) re-decide more often.

## Development

```bash
uv run ruff check .
uv run pytest                          # offline; no paid API calls, no Google calls
node --check jev_ultrafast/snapshot.js
uv run python scripts/check_guards.py  # real local browser, no model calls
```

See [docs/design.md](docs/design.md) for the runtime's freshness and execution rules.
