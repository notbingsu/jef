# jef

Read README.md before editing. Keep the loop small: page -> indexed elements -> operation + target -> execution.

- The user's input is a plain-language request. router.py picks the leaf in one TypeSafe request (use_case head + one skill head per multi-leaf branch); consume only the chosen branch's head. Low-confidence routes ask before running.
- Use cases live in skills/ as TOML (branches are directories, leaves are files). Site knowledge goes in a skill's rules, never in Python. No scripted step sequences or hardcoded field values in code.
- TypeSafe chooses an operation and operation-specific target heads in one request. Consume only the selected operation's target.
- Targets must map to observed elements and supported operations. Never let the model emit selectors or executable code.
- TYPE_TEXT invokes the text LLM. Cache a stale retry's value only while its entire helper input is identical.
- Never retry a browser mutation. Log execution before observing its result.
- confirm phrases gate execution in code; they are not model instructions. A gated action without approval stops the run.
- API skills (api = "service.operation"): TypeSafe makes closed-set judgments over code-found candidates (search results, addresses in the details, configured categories); the text model writes only free text and times into a typed schema. Code validates every field, resolves IDs from its own search results, and confirms every mutation. Never retry a mutation.
- There are no screenshots; the user watches the real tab and the terminal output. A `background` skill opens its tab in the background of the same Chrome (same profile, same logins), never activates it, and closes it at the end, so a skill with `report = true` prints what the page showed. Never launch a separate Chrome for this: a second instance cannot share the running profile.
- A report is the text model selecting and copying from observed page text; code keeps only values found word for word on the page. It never changes a run's status.
- Keep credentials server-side and .env ignored. .env holds only secrets (API keys); every other setting goes in jev.toml through config.py, validated like skills. Tests must not call paid APIs; they run on default settings (tests/conftest.py).
- A DONE choice is not proof of success.
- Do not commit or push unless the user requests it.

Checks: uv run ruff check ., uv run pytest, node --check jev_ultrafast/snapshot.js, uv build.
