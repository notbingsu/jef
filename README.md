# jef

A personal agent driven by a tree of skills. Forked from [browser-use/jev-ultrafast](https://github.com/browser-use/jev-ultrafast).

You say what you want in plain words; Jev picks the skill. Skills come in two kinds:

- **Browser skills** drive a real Chrome tab. Each step reads the page into an indexed element table. In one request, [TypeSafe's Jev](https://docs.typesafe.ai/introduction) picks an operation (`CLICK`, `TYPE_TEXT`, `SELECT`, `SCROLL_*`, `WAIT`, `DONE`, `BLOCKED`) and a target element for it. A small LLM writes text only for `TYPE_TEXT`. Code owns execution: model output never becomes selectors, coordinates or JavaScript.
- **API skills** call a service directly, with no browser. TypeSafe makes the closed-set calls (which event, which guests, which category) and the text model writes titles, times and search terms; code validates both and asks before changing anything. Google Calendar was the first, ported from [tele_gcal](https://github.com/notbingsu/tele_gcal); [moomoo](#moomoo-holdings) reads a brokerage account.

## Setup

```bash
uv sync
cp .env.example .env              # add TYPESAFE_API_KEY and TEXT_MODEL_API_KEY
uv run browser-harness --doctor   # connect Chrome; allow remote debugging when prompted
```

Browser runs use your existing Chrome profile, so sites you're logged into stay logged in.

### Settings

Secrets and settings live apart, because they are handled differently:

| File | Holds | In git |
| --- | --- | --- |
| `.env` | API keys only: `TYPESAFE_API_KEY`, `TEXT_MODEL_API_KEY`, `TELEGRAM_BOT_TOKEN` | no |
| `jev.toml` | everything else, typed and validated | yes |

`jev.toml` is optional; a missing key takes its default. Like skills, unknown keys and bad values are rejected, so a typo fails loudly instead of being ignored. A setting still in `.env` (`TEXT_MODEL=…`) is an error that says where it moved.

| Key | Default | Meaning |
| --- | --- | --- |
| `trace` | `full` | How much of each run `artifacts/runs/` keeps: `full`, `low` or `off`. See [Traces](#traces). `--trace` overrides it for one run. |
| `typesafe_model` | `jev-latest` | TypeSafe model. Pin a version such as `jev-1.13.0` to keep answers stable when the alias moves. |
| `text_model` | `deepseek-chat` | Text model for TYPE_TEXT, calendar arguments and reports. A `claude-*` model uses the Anthropic API, with `TEXT_MODEL_API_KEY` as the Anthropic key; anything else uses an OpenAI-compatible endpoint. |
| `text_model_base_url` | `https://api.deepseek.com/v1` | That endpoint. Not used for `claude-*`. |
| `text_model_reasoning` | `low` | `none` turns reasoning off on that endpoint. Not used for `claude-*`. |
| `server` | `true` | Hand requests to a long-running `jev` server (see [The server](#the-server)). `--no-server` runs one request in-process instead. |
| `server_idle_minutes` | `10` | The server exits after this long with nothing to do, closing its warm tabs. |
| `run_timeout_seconds` | `180` | A run that is still going stops at its next safe point after this long. |
| `approval_wait_seconds` | `120` | How long a request waits for you to click Allow on Chrome's "Allow remote debugging?" prompt. |
| `telegram_allowed` | `[]` | Numeric Telegram ids that may drive jev from a chat (see [Telegram](#telegram)). Empty means nobody. |

For the Calendar API skills, create an OAuth client of type **Desktop app** in Google Cloud (Calendar API enabled) and save its JSON as `config/client_secrets.json`. The first calendar run opens a consent page and saves `config/token.json`. tele_gcal's `client_secrets.json` and `token.json` work as-is (same scope). Both files are git-ignored. The consent page redirects to `http://localhost:8765/`; a Web application client (tele_gcal's is one) must have that redirect URI registered, while a Desktop app client accepts it as is.

For the moomoo skills, install [moomoo OpenD](https://openapi.moomoo.com/) and log in. Every query goes to that
gateway on `127.0.0.1:11111`: there is no cloud endpoint and no API key, so nothing works while OpenD is closed, and
no credential of yours reaches jev. The skills are read-only and never unlock trading, so leave the trade password
locked. `jev list my moomoo accounts` shows your account ids; pin one as `acc_id` in `skills/moomoo/_branch.toml`.

## Ask jev

```bash
uv run jev dentist friday oct 9 at 3pm for an hour
uv run jev any flights this month?
uv run jev how is my nvidia doing
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
| `--close` / `--no-close` | Close the tab afterwards, or keep it. Default: closed after a background run, kept after a visible one. |
| `--background` / `--no-background` | Override the skill's `background` setting, e.g. to watch a LinkedIn run happen. |
| `--trace full\|low\|off` | Record this run at another level than `jev.toml`'s `trace`, e.g. `--trace full` to debug one run. |
| `--no-server` | Run this request in this process, as before the server existed. Useful when debugging jev itself. |
| `--stop-server` | Stop the server once its current run ends. The next `jev` starts a fresh one. |

A browser skill opens a tab in the foreground, so you watch the real page. The terminal prints each action as it executes:

```text
route: linkedin-dms/reply  (94%, 380 ms)
linkedin-dms/reply → https://www.linkedin.com/messaging/
   812 ms  CLICK [4] Ada Lovelace  93%
  1530 ms  TYPE_TEXT [9] Write a message… ← “Thanks, Thursday works…”  97%
           confirm CLICK “Send”? [y/N]
```

The tab stays open afterwards (`--close` to close it). `DONE` is the model's claim, so check the tab.

A `background` skill (LinkedIn) opens its tab in the background instead: same Chrome, same profile, same logins, but nothing jumps in front of what you were doing, and the tab closes when the run ends. There is no tab to read, so a skill with `report = true` (like `linkedin-dms/check`) prints what the finished page showed:

```text
route: linkedin-dms/check  (100%, 310 ms)
linkedin-dms/check → https://www.linkedin.com/messaging/  (background tab)
   395 ms  WAIT  94%  (no change)
  3540 ms  DONE: the model reports success. The trace has the page it saw.
           trace: artifacts/runs/linkedin-dms/check/20261005T094838Z.json
  Katharine Tan                Sep 29     Katharine: Thanks for sending me your resume. Speak soon!
  Joy Z                        Sep 22     You: Hi Joy, my pleasure to connect! Glad to be noticed by Airwallex 😆
```

The report is code-checked: the text model only selects and copies, and a name, date or preview that isn't on the page word for word is left out (the CLI says how many). It covers the visible part of the page (about 6,000 characters), newest conversations first, and needs `TEXT_MODEL_API_KEY`.

A background run still needs Chrome running; if it isn't, Browser Harness starts it and you get a window. `--no-background` watches a run, and `--no-close` keeps the tab so you can look at the page yourself.

An API skill prints what it will do and waits for you before any create, update or delete:

```text
calendar/update-event → gcal.update_event
  from: Fri 9 Oct 3pm–4pm  Dentist
  to:   Fri 9 Oct 5.30pm–6.30pm  Dentist
  match: 96%
  Update “Dentist”? [y/N]
```

### Traces

Each run can write a JSON trace to `artifacts/runs/<skill>/` (git-ignored), at the level `trace` sets:

| Level | Keeps | Size |
| --- | --- | --- |
| `full` | Everything: the page text and element table, every model request and raw answer. What you need to see why a run went wrong. | ~250 KB for a LinkedIn check |
| `low` | What ran and what came of it: route, status, each action and decision (operation, target, confidence, latency, tokens), calendar arguments and changes, any report. No page text, element tables, request bodies or raw answers. | a few KB |
| `off` | Nothing; the terminal output is the only record. | none |

A full trace holds whatever the page showed, such as your messages. `low` keeps a run's answer (a report, a calendar change) without the rest of the page. When a run misbehaves, repeat it with `--trace full`.

## The server

`jev …` hands its request to a long-running server for this project and streams back what the server says and asks. The first call starts it (about 2 s); after that a request starts in milliseconds, because the model SDKs are imported, the HTTP connections and the Chrome connection are open, and a background skill's tab is still warm. A second LinkedIn check reuses the open messaging page and is `DONE` in about 0.3 s instead of 3 s of page loading.

- **One at a time.** Requests run in order; a second one prints `queued behind 1 request`.
- **Ctrl-C** cancels at the next safe point: before a decision, an action, a model call or a confirmation. Nothing already sent is interrupted. A second Ctrl-C leaves at once. A client that goes away cancels its run, and any question it was asked is answered no.
- **Warm tabs**, for background skills only: one per site, reused only if the last run there ended `DONE` and the tab is still on the skill's page; otherwise the next run starts from the skill's URL. A tab you closed, or one Chrome discarded or that stops answering, is replaced. Visible tabs are yours once their run ends. Traces record `"tab": "new" | "reused" | "navigated"`.
- **Never stale.** The server serves only the code, `jev.toml` and `.env` it started with. Change any of them and the next request says `jev changed since its server started; starting a fresh one`. Skills are read from disk on every request.
- **Slow or silent.** A run stops after `run_timeout_seconds` at its next safe point (`TIMEOUT`). A Chrome that doesn't answer a read for 5 s is re-read up to three times, then the run stops and the tab is dropped. If Chrome never confirms an input, the run ends `UNCERTAIN`: the input may have landed, so it is never retried, and the tab is left open for you to look at. A dropped connection to a model or a calendar lookup is retried once; a calendar change never is. Google sign-in gives up after 2 minutes.
- **Chrome approval stays manual.** When the server needs a new Chrome connection (after Chrome restarts), the request says so; click Allow within `approval_wait_seconds`.
- **Exits on its own** after `server_idle_minutes` idle. Its socket, log and lock live in `artifacts/server/`; the socket is readable only by you, since a request can drive your logged-in Chrome.

## Telegram

`jev-telegram` drives the same agent from a chat: say what you want, and approve a change with a button.

```bash
cp .env.example .env              # add TELEGRAM_BOT_TOKEN from BotFather's /newbot
# jev.toml: telegram_allowed = [123456789]   @userinfobot tells you your numeric id
uv run jev-telegram               # from the project root
```

It is a second client of the same server, not a bot of its own: a chat request gets the warm imports, the warm Chrome
connection and the one-at-a-time queue a terminal request gets. **One message is one run.** There is no chat state
machine, because `console.ask` returns only a bool — every question jev asks is a y/N gate, so it becomes two
buttons, and the run itself holds the state while it waits on the socket.

```text
you   move the dentist to 5.30pm
jev   route: calendar/update-event  (86%, 410 ms)
      calendar/update-event → gcal.update_event
        from: Fri 9 Oct 3pm–4pm  Dentist
        to:   Fri 9 Oct 5.30pm–6.30pm  Dentist
        match: 96%
jev   Update “Dentist”?            [ Yes ]  [ No ]
```

A run's lines are coalesced into one message that is edited as it goes, since Telegram takes about one message a
second; it is sent as a monospace block so jev's columns stay aligned on a phone. `/help` lists the skill tree,
`/cancel` stops a run at its next safe point, and `/start` says what to type. A second message while a run is going
is refused rather than queued; a request from another allowed chat queues on the server and says `queued behind 1
request`.

Browser skills always run **in the background** from a chat: nobody is watching a tab from a phone, and a visible one
would steal focus on your desktop. So a skill you mean to drive from chat wants `report = true`, or the chat sees
only the action lines and `DONE`.

Before you rely on it:

- **Anyone in `telegram_allowed` can drive your logged-in Chrome and your calendar.** The default is empty, meaning
  nobody, and the bot refuses to start until you set it. An id that is not on the list runs nothing and gets no
  reply at all — only a log line naming the id.
- **Private chats only.** A group is refused even from an allowed id, because the output would go to everyone in it.
  Turn group invitations off with BotFather's `/setjoingroups`, and leave `/setprivacy` enabled.
- **Do the first Chrome approval and the first Google sign-in at the keyboard.** Chrome's "Allow remote debugging?"
  prompt and Google's consent page cannot be clicked from a phone; over Telegram they just time out.
- **Raise `run_timeout_seconds` if you answer from a phone.** A run's budget starts when it does, not when it asks,
  so a confirmation left for a few minutes can hit the clock. A calendar change you approve still goes ahead; a
  browser run stops at its next safe point.
- **Restart the bot after changing jev.** A code change retires the *server*, which the bot restarts automatically,
  but the bot process keeps running the code it started with. Settings, including the allowlist, are read once too.
- The token is a credential: `.env` only, and it is git-ignored.

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
    _branch.toml           background = true, rules, confirm
    check.toml             read-only; report = true
    reply.toml
  moomoo/
    _branch.toml           acc_id, security_firm, trd_env, currency
    accounts.toml          api = "moomoo.accounts"
    holdings.toml          api = "moomoo.holdings"
    position.toml          api = "moomoo.position"
    activity.toml          api = "moomoo.activity"
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
| `background` | any | Browser only. Open the tab in the background of your Chrome rather than switching to it, and close it afterwards. The deepest layer that sets it wins; `--background`/`--no-background` override it. |
| `report` | any | Browser only. After `DONE`, the text model reads the finished page and the CLI prints what it shows. |

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

### Questions about your schedule

Some questions aren't answered by a list. "When is the next evening I'm free?" needs the evenings worked out from what's on. When you search, TypeSafe also judges whether your words need a written answer rather than the matching events. That judgment rides in the same request as the search's other judgments, so it adds no wait. Only if it says yes (over 50%) does the text model get one more call, with your question and every event in the range:

```text
calendar/find-events → gcal.find_events
  → Wed 7 Oct after 6pm is your next available evening, as your work ends at 6pm and you have no events scheduled that evening.
  (worked out from 19 events; the trace lists them)
```

- **Everything in range:** an answer fetches up to 100 events, not the 10 a list shows, and the search plans a long enough range ("the next two weeks") with no keyword filter.
- **Code does the calendar math the model shouldn't:** the model gets each event's start and end, plus every date in the range with its weekday already written.
- **Stated assumptions:** time outside the range counts as unknown, and if 100 events didn't cover it the model is told the list may be incomplete. If it has to assume a definition, such as what counts as evening, it says which. Add your own as a rule in `skills/calendar/_branch.toml`, e.g. "Evening means 6pm to 10pm."
- **A failed answer still shows the events,** and the run still succeeds. The judgment's probability and the answer are in the trace.
- **Lists stay lists:** "any events this week" scores about 0.1 and makes no extra call.

Search defaults to upcoming events unless your details point at the past. Update and delete on a recurring event affect only the matched occurrence. tele_gcal's "add to calendar" template link isn't ported.

## moomoo holdings

The `moomoo/` skills read a brokerage account through moomoo's OpenAPI: what you hold, how one position is doing,
and what was traded over a range.

```text
route: moomoo/holdings  (100%, 300 ms)
moomoo/holdings → moomoo.holdings
  account 283726802396538239 (REAL, FUTUSG)
  USD  total 6,774.32   positions 5,497.38   cash 1,275.05   available 4,955.16   risk LEVEL3

  USD   code       name                  qty       cost      price        value         P/L    P/L %
        US.NVDA    NVIDIA                  9     193.60     238.91     2,150.18     +407.78   23.40%
        US.GOOGL   Alphabet-A              4     211.80     348.24     1,392.96     +545.77   64.42%
        subtotal                                                       3,543.14     +953.55
```

**Read-only by construction.** Nothing in `jev_ultrafast/moomoo.py` places, changes or cancels an order, and the
SDK's `unlock_trade` is never called — a test asserts that none of those calls appears in the module at all. So
these skills work with trading still locked in OpenD, and no run can move your money.

| Option | Default | Meaning |
| --- | --- | --- |
| `acc_id` | | Which account to read. `jev list my moomoo accounts` shows the ids. |
| `security_firm` | tries each | The moomoo entity the account is with, e.g. `FUTUSG`. |
| `trd_env` | `REAL` | `REAL` reads the account you trade in; `SIMULATE` reads the paper one. |
| `currency` | `USD` | The single currency account totals are converted to. |
| `max_results` | 20 | Rows an activity listing shows. |
| `timezone` | this machine's | Used for "last week" and the like. |
| `host` / `port` | `127.0.0.1` / `11111` | Where OpenD listens. |

What the code guarantees, whatever the model returns:

- **Average cost, as the app shows it.** The SDK also returns `cost_price`, `pl_val` and `pl_ratio` on a diluted
  cost basis, which overstate gains and understate losses; nothing here reads them, so the numbers match your phone.
  A missing app field stops the read rather than reporting a zero.
- **Currencies are never added.** Each position is shown and subtotalled under its own currency, and the one
  account-wide total comes from the funds query, which states the currency it is in.
- **One account, pinned.** With several accounts in the environment and no `acc_id`, a run stops and names them
  rather than guess which portfolio you meant. Ids are read as exact integers: a real one is 18 digits, past what a
  float holds.
- **Fresh, not cached.** Positions and funds are read with `refresh_cache`, which is what makes them match the app.
  That path allows 10 reads per 30 s per account, and a run reads once.
- **Holdings are found by code.** `moomoo/position` offers TypeSafe only the rows just read, so no model ever names
  an instrument; under 50% the run stops and names the likeliest two.
- **Dates are checked.** The text model writes the activity range; code checks the shape and the order, and an open
  range covers the last week.

### Questions about your portfolio

As with the calendar, TypeSafe judges whether your words need a written answer rather than a list, in the same
request. Only if it says yes does the text model get one more call, with the holdings just read:

```text
moomoo/holdings → moomoo.holdings
  → Your biggest position is NVIDIA at $2,150.73 USD, roughly 39% of your invested portfolio value ($5,497.38 USD).
```

The answer is worked out only from the positions fetched, is shown and traced but never acted on, and the skill's
rules tell it to describe the account rather than suggest buying or selling anything. A failed answer still shows
the holdings, and the run still succeeds.

> Automating a brokerage account is yours to get right. moomoo's own disclaimer is explicit that the skill pack is
> a technical aid, not investment advice, and that you are responsible for what an agent does with it. These skills
> only read, which is the main reason to keep them that way.


## Library

```python
from jev_ultrafast import Agent, skills

skill = skills.load("calendar/browser/create-event")
# approve(action, decision) -> bool answers confirm prompts; without it, a confirm action stops the run.
with Agent(skill.url, skill.task + "\nLunch with Sam, Tue 12:30, 45 min",
           rules=skill.rules, confirm=skill.confirm, background=skill.background) as agent:
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
