"""TypeSafe makes choices; an optional small OpenAI-compatible model writes field values."""

import functools
import json
import math
import os
import time

import anthropic
import httpx

from . import config
from .questions import NEXT_ACTION, REPORT, TARGET, TEXT_VALUE

CLIENT = httpx.Client(http2=True, timeout=25)
# Structured output: Claude can only return {"text": string | null}. Values are still validated below.
TEXT_SCHEMA = {
    "type": "object",
    "properties": {"text": {"anyOf": [{"type": "string"}, {"type": "null"}]}},
    "required": ["text"],
    "additionalProperties": False,
}


def nullable(kind, **extra):
    return {"anyOf": [{"type": kind, **extra}, {"type": "null"}]}


def strict(**properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def conform(value, spec):
    """Check text-model output against a schema built with nullable/strict. Omitted keys count as null; unknown
    keys are rejected."""
    kinds = spec.get("anyOf", [spec])
    if value is None:
        return any(kind["type"] == "null" for kind in kinds)
    for kind in kinds:
        if kind["type"] == "string" and isinstance(value, str):
            return True
        if kind["type"] == "array" and isinstance(value, list):
            return all(conform(item, kind["items"]) for item in value)
        if kind["type"] == "object" and isinstance(value, dict):
            properties = kind["properties"]
            return not set(value) - set(properties) and all(conform(value.get(k), s) for k, s in properties.items())
    return False


# A dropped connection, as opposed to a timeout. A long-lived jev serve can meet one on a pooled connection.
TRANSIENT = (httpx.ConnectError, httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError)


def post_json(url, key, body):
    """POST to a model. A model call changes nothing, so a dropped connection is retried once; a timeout is not,
    since retrying would only double the wait."""
    reconnected = False
    for attempt in range(3):
        try:
            response = CLIENT.post(url, json=body, headers={"Authorization": f"Bearer {key}"})
        except TRANSIENT:
            if reconnected:
                raise RuntimeError("Model connection failed; no action executed.") from None
            reconnected = True
            continue
        except httpx.HTTPError:
            raise RuntimeError("Model connection failed; no action executed.") from None
        if response.status_code in {429, 529, 503} and attempt < 2:
            time.sleep(0.5 * 2**attempt)
            continue
        if response.is_error:
            raise RuntimeError(f"Model provider returned HTTP {response.status_code}; no action executed.")
        return response.json()
    raise RuntimeError("Model unavailable")


def typesafe(body):
    key = os.environ.get("TYPESAFE_API_KEY")
    if not key:
        raise ValueError("Jev needs TYPESAFE_API_KEY; nothing was chosen.")
    return post_json("https://api.typesafe.ai/v1/systemone", key, body)


def validate_choice(answer, ids):
    try:
        probabilities = answer["probabilities"]
        numbers = [*probabilities.values(), answer["confidence"]]
        valid = (
            answer["choice"] in ids
            and set(probabilities) == set(ids)
            and all(type(n) in (int, float) and math.isfinite(n) and 0 <= n <= 1 for n in numbers)
            and abs(sum(probabilities.values()) - 1) < 0.02
            and probabilities[answer["choice"]] >= max(probabilities.values()) - 1e-6
        )
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError("Invalid TypeSafe response; no action executed.")
    return answer


def validate_noul(answer):
    """The probability of yes."""
    value = answer.get("noul") if isinstance(answer, dict) else None
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError("Invalid TypeSafe response; no action executed.")
    return value


def action_space(actions):
    """One index per observed element; each operation has its own valid target choices."""
    elements, indices, targets, controls = [], {}, {}, {}
    operations = {"click": "CLICK", "fill": "TYPE_TEXT", "select": "SELECT"}
    for action in actions:
        kind = action["kind"]
        if kind not in operations:
            controls[action["id"].upper()] = action
            continue
        node = action["node"]
        if node not in indices:
            index = str(len(elements) + 1)
            indices[node] = index
            element = {k: action[k] for k in ("role", "value", "checked", "selected", "expanded") if k in action}
            element.update(index=index, label=action["label"].split(" → ")[0], operations=[])
            if kind == "select":
                element["value"] = action.get("current_value", "")
                element["options"] = []
            elements.append(element)
        index = indices[node]
        operation = operations[kind]
        group = targets.setdefault(operation, {})
        element = elements[int(index) - 1]
        if operation not in element["operations"]:
            element["operations"].append(operation)
        target = index
        if kind == "select":
            target = f"{index}:{len(element['options']) + 1}"
            element["options"].append({"index": target, "label": action["label"], "value": action["value"]})
        group[target] = action
    return elements, targets, controls


def choose(state, goal, history, rules=()):
    elements, targets, controls = action_space(state["actions"])
    # Skill rules are the user's standing instructions for this site, shared by every head.
    base = {"goal": goal, **({"skill_rules": list(rules)} if rules else {})}
    labels = {
        "CLICK": "Click an element, button, menu option, autocomplete suggestion, or calendar day.",
        "TYPE_TEXT": "Enter or replace text in an editable field. A small LLM will supply the value from the goal.",
        "SELECT": "Select an observed dropdown value.",
    }
    operations = {key: labels[key] for key in targets}
    operations.update({key: value["label"] for key, value in controls.items()})
    operations.update(DONE="Every requirement is visibly satisfied.", BLOCKED="No supported operation can progress.")
    questions = {
        "operation": {"type": "choice", "criteria": operations, "instructions": {**base, "rules": NEXT_ACTION}}
    }
    for operation, candidates in targets.items():
        questions[operation.lower() + "_target"] = {
            "type": "choice",
            "criteria": {
                index: {
                    "element": f"[{index}] {a['label']}",
                    "current_value": a.get("current_value", a.get("value", "")),
                    **{k: a[k] for k in ("role", "checked", "selected", "expanded") if k in a},
                }
                for index, a in candidates.items()
            },
            "instructions": {**base, "operation": operation, "rules": [NEXT_ACTION, TARGET]},
        }
    body = {
        "model": config.get("typesafe_model"),
        "state": {
            "page": {k: state[k] for k in ("url", "title", "text")},
            "elements": elements,
            "recent_actions": [
                {k: h.get(k) for k in ("action", "kind", "text", "page_changed")} for h in history[-10:]
            ],
        },
        "questions": questions,
    }
    started = time.perf_counter()
    result = typesafe(body)
    operation_answer = validate_choice(result["answers"].get("operation", {}), operations)
    operation = operation_answer["choice"]
    target = None
    target_answer = None
    probabilities = {}
    if operation in targets:
        # Unused target heads cannot cause an action. Validate the head selected by the operation.
        target_answer = validate_choice(result["answers"].get(operation.lower() + "_target", {}), targets[operation])
        target = target_answer["choice"]
        choice = targets[operation][target]["id"]
        probabilities = {a["id"]: target_answer["probabilities"][index] for index, a in targets[operation].items()}
    else:
        choice = controls[operation]["id"] if operation in controls else operation
        probabilities[choice] = operation_answer["probabilities"][operation]
    return {
        "choice": choice,
        "operation": operation,
        "target": target,
        "confidence": operation_answer["confidence"],
        "probabilities": probabilities,
        "operation_probabilities": operation_answer["probabilities"],
        "target_probabilities": target_answer["probabilities"] if target_answer else {},
        "target_confidence": target_answer["confidence"] if target_answer else None,
        "raw_answers": result["answers"],
        "model": result["model"],
        "usage": result.get("usage", {}),
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "request": body,
    }


def field_context(goal, action, page, history, rules=()):
    return {
        "goal": goal,
        **({"skill_rules": list(rules)} if rules else {}),
        "field": {k: action.get(k) for k in ("label", "role", "value")},
        "page": {"title": page["title"], "text": page["text"][:6000]},
        "recent_actions": [{k: h.get(k) for k in ("action", "text")} for h in history[-6:]],
    }


@functools.lru_cache(maxsize=1)
def claude_client(key):
    # One client per key keeps the connection warm between text-model calls.
    return anthropic.Anthropic(api_key=key, timeout=25, max_retries=2)


def claude_content(key, model, system, schema, context):
    try:
        response = claude_client(key).messages.create(
            model=model,
            max_tokens=1024,
            system=system,
            messages=[{"role": "user", "content": json.dumps(context)}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
    except anthropic.APIStatusError as error:
        raise RuntimeError(f"Model provider returned HTTP {error.status_code}; no action executed.") from None
    except anthropic.APIError:
        raise RuntimeError("Model connection failed; no action executed.") from None
    # A refusal or truncated response is not a usable answer.
    text = next((b.text for b in response.content if b.type == "text"), None)
    return (text if response.stop_reason == "end_turn" else None), response.usage.to_dict()


def chat_content(key, model, system, _schema, context):
    # json_object mode does not enforce the schema; callers validate every field.
    base = config.get("text_model_base_url").rstrip("/")
    reasoning = {"thinking": {"type": "disabled"}} if "api.deepseek.com/" in base else {"reasoning": {"effort": "low"}}
    if config.get("text_model_reasoning") == "none":
        reasoning = {"reasoning": {"enabled": False}}
    result = post_json(
        base + "/chat/completions",
        key,
        {
            "model": model,
            "max_tokens": 1024,
            "response_format": {"type": "json_object"},
            **reasoning,
            "messages": [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": json.dumps(context),
                },
            ],
        },
    )
    try:
        return result["choices"][0]["message"]["content"], result.get("usage", {})
    except (KeyError, IndexError, TypeError):
        return None, result.get("usage", {})


def complete_json(system, context, schema, purpose):
    """One text-model call that should return a JSON object. Returns (object or None, call info)."""
    key = os.environ.get("TEXT_MODEL_API_KEY")
    if not key:
        raise ValueError(f"{purpose} needs TEXT_MODEL_API_KEY; nothing is hardcoded or guessed.")
    model = config.get("text_model")
    started = time.perf_counter()
    # claude-* models use the Anthropic Messages API; anything else uses an OpenAI-compatible endpoint.
    content, usage = (claude_content if model.startswith("claude-") else chat_content)(
        key, model, system, schema, context
    )
    try:
        output = json.loads(content)
    except (TypeError, ValueError):
        output = None
    info = {"model": model, "latency_ms": round((time.perf_counter() - started) * 1000), "usage": usage}
    return (output if isinstance(output, dict) else None), info


def field_text(context):
    output, info = complete_json(TEXT_VALUE, context, TEXT_SCHEMA, "TYPE_TEXT")
    value = (output or {}).get("text")
    if set(output or {}) != {"text"} or not isinstance(value, str) or not value.strip() or len(value) > 2000:
        raise ValueError("Text helper returned no valid field value; nothing typed.")
    return value, info


ENTRY = strict(name=nullable("string"), when=nullable("string"), text=nullable("string"))
REPORT_SCHEMA = strict(entries={"type": "array", "items": ENTRY}, missing=nullable("string"))


def squash(text):
    return " ".join(text.split())


def page_report(goal, page, rules=()):
    """What the finished page shows, as the details asked. The text model only selects and copies: code keeps an
    value only when it appears verbatim in the page text: a wrong `when` or `text` is blanked, a wrong `name` drops
    the entry. Returns (entries, missing, what was left out, info)."""
    context = {
        "goal": goal,
        **({"skill_rules": list(rules)} if rules else {}),
        "page": {k: page[k] for k in ("url", "title", "text")},
    }
    output, info = complete_json(REPORT, context, REPORT_SCHEMA, "Reporting a page")
    if output is None or not conform(output, REPORT_SCHEMA) or not isinstance(output.get("entries"), list):
        raise ValueError("The text model's report did not match the schema; nothing is shown.")
    seen = squash(page["text"])
    entries, dropped = [], []
    for entry in output["entries"]:
        values = {k: squash(entry[k]) if entry.get(k) else None for k in ("name", "when", "text")}
        wrong = [k for k, v in values.items() if (v and v not in seen) or (k == "name" and not v)]
        if wrong:
            dropped.append({"entry": entry, "fields": wrong})
        if values["name"] and "name" not in wrong:
            entries.append({k: None if k in wrong else v for k, v in values.items()})
    return entries, output.get("missing"), dropped, info
