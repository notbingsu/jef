# Dynamic operation + target

The input is a goal: a skill's `task` plus the run's details. Every page observation builds an indexed table of accessible elements and their current values. One node receives one index, even when it supports both clicking and typing.

One TypeSafe request asks which operation to perform and which target would be appropriate for each available operation. The executor consumes only the target head corresponding to the selected operation. This avoids serial operation-then-target calls and rejects targets incompatible with the operation. Dropdown targets include a code-owned option index.

Operation and target questions receive the same next-step rules and the skill's `rules` (as `skill_rules`). Target criteria include current values and checked/selected state. The questions run independently: a target cannot read the operation answer, so its premise explicitly names the operation it assumes.

TYPE_TEXT sends the goal, skill rules, selected field, visible page context, and recent actions to a small LLM. Its JSON must contain exactly one valid `text` value. The code does not extract quoted literals. A value can be reused after a stale decision only while the entire helper input is identical, and is discarded after a successful mutation.

## Routing

A run starts from a plain-language request. One TypeSafe request asks a `use_case` question over the top-level branches plus `NONE`, and, for each branch with more than one leaf, a skill question over that branch's leaves (deeper branches flattened, with their descriptions attached as `within`). Skill questions run without seeing the use-case answer, so each names the use case it assumes. Only the chosen branch's skill head is validated and consumed; a branch with one leaf needs no head. The product of the two probabilities is the route's confidence: under 0.5 the user confirms first, and without a terminal nothing runs. The full request becomes the skill's details.

## Skills

A skill resolves to a start URL, a task, rules, and confirm phrases by walking `skills/` from the root to the leaf. Rules are model input. Confirm phrases are not: the executor matches them, as whole words and case-insensitively, against the observed label of a chosen click/select target. A match calls the approver before any input. A refusal, or no approver, ends the run with status `declined`. Approval does not bypass freshness: the executor still rechecks the page afterwards, and a stale page means a new decision and, if it is gated, a new prompt.

## API skills

A leaf with `api = "service.operation"` skips the browser. The operation sends the task, details, skill rules, `now`, and its own instructions to the text model with a strict JSON schema (every field present, nullable). The Anthropic path enforces the schema; the OpenAI-compatible path does not, so code re-checks types and rejects unknown keys either way. A non-null `missing` stops the run with the model's question.

For Google Calendar, the text model writes only what has no candidate list: titles, descriptions, locations, search terms, and ISO times resolved from `now`. TypeSafe answers everything that picks from candidates code already holds, asked in the same round trip as the text model whenever their inputs are independent:

| Step | TypeSafe question | Candidates (from code) |
| --- | --- | --- |
| search (find, update, delete) | `category` Choice | `options.keywords` categories + `NONE` |
| pick (update, delete) | `event` Choice | the search results, each with a code-computed relative day + `NONE` |
| create, update | `color` Choice | `options.colors` categories + `NONE` |
| create, update | `invite_*` Noul per address | addresses a regex finds in the details |
| update | `remove_*` Noul per guest | guests already on the picked event |

Update is three stages: search terms ∥ category, then the pick, then changes for the picked event ∥ color and guests. The text model sees only the picked event, never the other results. A pick under 0.5 probability, or `NONE`, stops the run; other judgments under 0.5 are not applied. Code maps the choice to the event ID, the category to its `colorId`, and builds the guest list. Every field is validated before the confirmation prompt: times must parse and be ordered, and colors must be Google's 1–11. Mutations are sent once, never retried.

Dates and times stay with the text model. Jev reads dates as text and is unreliable at date arithmetic, so a TypeSafe version would ask for each date's parts (month, day, weekday, week offset, hour, minute, duration) and assemble them in code. That removes no text-model call while titles still need one.

## Runtime

One browser-side DOM snapshot supplies common HTML/ARIA roles, names, values, visible text, and executable targets. A WeakMap gives each actual node a code-owned identity; a Map keeps the live references used for execution. Replaced elements receive new identities, disconnected references are pruned, and navigation starts a new cache. These IDs are not CDP backend node IDs. Geometry is always read again immediately before input.

The model sees visible text, never screenshots. The agent's tab opens in the foreground; focus emulation keeps animation frames running if another tab is brought forward.

A `background` run uses the same Chrome and profile, so the same logins, but `Target.createTarget` is given `background: true` and nothing activates the target afterwards: the tab is created without being switched to. Focus emulation, already enabled for every run, is what makes this work — an unfocused tab would otherwise throttle animation frames and timers. CDP input is dispatched to the target's renderer rather than through the window system, so clicking and typing do not need the tab to be in front. A background run closes its tab when it ends, since an unseen tab is clutter; `--no-close` keeps it. If the site shows a sign-in page the skill's rules tell the model to choose BLOCKED. Nothing else about observation, freshness or execution changes. True headless is not available for these skills: Chrome cannot open a second instance on a profile the running Chrome already holds, and a fresh profile has none of your logins.

Freshness compares semantic state instead of counting DOM mutations. Before a click/select, guards compare the document, full URL, viewport, safe form values/states, selected target, and nearby form/dialog/row context. Text generation, typing, scrolling, waiting, and completion use a full semantic comparison. The executor rechecks target visibility, enabled state, geometry, and click occlusion. Scoped guards intentionally permit unrelated visible content to change; this is a practical heuristic, not proof that arbitrary page changes are irrelevant to the goal.

Browser mutations are not retried by transport recovery. Completed execution is logged before the next observation, including when that observation encounters a navigation. An interrupted native-select evaluation stops because its change event may already have fired. Typing uses a browser select-all command followed by CDP text insertion, so existing input contents are replaced.

The next observation waits for up to two animation frames or 50 ms after an interaction. Editable ARIA comboboxes instead wait for visible options, capped at 200 ms. An explicit WAIT is 100 ms.

## Reports

A skill with `report = true` ends with a read of the finished page, since a background run has no tab to look at. After a DONE choice, the page's observed text goes to the text model with the goal and skill rules, and it returns entries of `name`, `when` and `text` under a strict schema. Code checks each value against the page text, ignoring whitespace: a value not found word for word is blanked, and an entry whose name is not found is dropped. The model selects and copies; it cannot add. The trace keeps the entries and what was left out; a full trace also keeps the page text. A failed report is recorded in the trace and never changes the run's status. The report covers only the visible text the snapshot captured (6,000 characters), and like DONE it is a claim about that page, not proof about the account.

## Settings and traces

`.env` holds only secrets (API keys); every other setting is in `jev.toml`, read by `config.py`. The split follows how each is handled: secrets stay out of git and out of traces, settings are worth keeping in git and reviewing. Settings are validated the way skills are: unknown keys and values outside a key's choices are rejected, and a setting left in the environment under its old variable name is an error rather than silently ignored. Tests replace `jev.toml` with the defaults (`tests/conftest.py`), so a developer's `text_model` can never route an offline test to a paid API.

`trace` sets what a run records. `full` is the whole state machine: every observation, decision request and raw answer. `low` keeps the run's outcome and its steps (each decision's operation, target, confidence, latency and token use, calendar arguments and changes, and any report) and drops page text, element tables, request bodies and raw answers, which are both most of the bytes and all of the page content. `off` writes nothing. A trace is written after execution, so its level never changes what runs.

## Boundaries

Sixty browser actions and 120 decision requests bound a run. Up to 250 action candidates are retained; truncated candidates cannot be selected. Credentials remain server-side. Tabs share the existing Chrome profile, in the foreground or the background.

Name resolution covers common labels, ARIA references, and text; it is not the browser's full accessibility algorithm. Shadow roots, frames, canvas, uploads, nested scrolling, pop-ups, and complex keyboard interactions can block progress. A valid action can still be wrong; the model's DONE choice is not proof the task succeeded.
