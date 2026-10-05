# Dynamic operation + target

The input is a goal: a skill's `task` plus the run's details. Every page observation builds an indexed table of accessible elements and their current values. One node receives one index, even when it supports both clicking and typing.

One TypeSafe request asks which operation to perform and which target would be appropriate for each available operation. The executor consumes only the target head corresponding to the selected operation. This avoids serial operation-then-target calls and rejects targets incompatible with the operation. Dropdown targets include a code-owned option index.

Operation and target questions receive the same next-step rules and the skill's `rules` (as `skill_rules`). Target criteria include current values and checked/selected state. The questions run independently: a target cannot read the operation answer, so its premise explicitly names the operation it assumes.

TYPE_TEXT sends the goal, skill rules, selected field, visible page context, and recent actions to a small LLM. Its JSON must contain exactly one valid `text` value. The code does not extract quoted literals. A value can be reused after a stale decision only while the entire helper input is identical, and is discarded after a successful mutation.

## Skills

A skill resolves to a start URL, a task, rules, and confirm phrases by walking `skills/` from the root to the leaf. Rules are model input. Confirm phrases are not: the executor matches them, as whole words and case-insensitively, against the observed label of a chosen click/select target. A match calls the approver before any input. A refusal, or no approver, ends the run with status `declined`. Approval does not bypass freshness: the executor still rechecks the page afterwards, and a stale page means a new decision and, if it is gated, a new prompt.

## Runtime

One browser-side DOM snapshot supplies common HTML/ARIA roles, names, values, visible text, and executable targets. A WeakMap gives each actual node a code-owned identity; a Map keeps the live references used for execution. Replaced elements receive new identities, disconnected references are pruned, and navigation starts a new cache. These IDs are not CDP backend node IDs. Geometry is always read again immediately before input.

The model sees visible text, never screenshots. The agent's tab opens in the foreground; focus emulation keeps animation frames running if another tab is brought forward.

Freshness compares semantic state instead of counting DOM mutations. Before a click/select, guards compare the document, full URL, viewport, safe form values/states, selected target, and nearby form/dialog/row context. Text generation, typing, scrolling, waiting, and completion use a full semantic comparison. The executor rechecks target visibility, enabled state, geometry, and click occlusion. Scoped guards intentionally permit unrelated visible content to change; this is a practical heuristic, not proof that arbitrary page changes are irrelevant to the goal.

Browser mutations are not retried by transport recovery. Completed execution is logged before the next observation, including when that observation encounters a navigation. An interrupted native-select evaluation stops because its change event may already have fired. Typing uses a browser select-all command followed by CDP text insertion, so existing input contents are replaced.

The next observation waits for up to two animation frames or 50 ms after an interaction. Editable ARIA comboboxes instead wait for visible options, capped at 200 ms. An explicit WAIT is 100 ms.

## Boundaries

Sixty browser actions and 120 decision requests bound a run. Up to 250 action candidates are retained; truncated candidates cannot be selected. Credentials remain server-side. Tabs share the existing Chrome profile.

Name resolution covers common labels, ARIA references, and text; it is not the browser's full accessibility algorithm. Shadow roots, frames, canvas, uploads, nested scrolling, pop-ups, and complex keyboard interactions can block progress. A valid action can still be wrong; the model's DONE choice is not proof the task succeeded.
