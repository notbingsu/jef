# jef

Read README.md before editing. Keep the loop small: page -> indexed elements -> operation + target -> execution.

- Use cases live in skills/ as TOML (branches are directories, leaves are files). Site knowledge goes in a skill's rules, never in Python. No scripted step sequences or hardcoded field values in code.
- TypeSafe chooses an operation and operation-specific target heads in one request. Consume only the selected operation's target.
- Targets must map to observed elements and supported operations. Never let the model emit selectors or executable code.
- TYPE_TEXT invokes the text LLM. Cache a stale retry's value only while its entire helper input is identical.
- Never retry a browser mutation. Log execution before observing its result.
- confirm phrases gate execution in code; they are not model instructions. A gated action without approval stops the run.
- There are no screenshots; the user watches the real tab and the terminal output.
- Keep credentials server-side and .env ignored. Tests must not call paid APIs.
- A DONE choice is not proof of success.
- Do not commit or push unless the user requests it.

Checks: uv run ruff check ., uv run pytest, node --check jev_ultrafast/snapshot.js, uv build.
