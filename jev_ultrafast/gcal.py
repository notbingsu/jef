"""Google Calendar through its REST API, no browser. Ported from notbingsu/tele_gcal.

TypeSafe makes the closed-set judgments: which search result, which guests, which category. The text model writes
only free text and times into a typed schema. Code resolves event IDs from its own search results, validates every
field, and asks before any create, update or delete. Mutations are never retried.
"""

import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta
from pathlib import Path
from time import perf_counter
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from .model import complete_json, conform, nullable, strict, typesafe, validate_choice, validate_noul
from .questions import CALENDAR, CATEGORY, COLOR, EVENT, INVITE, RECOLOR, UNINVITE, UNINVITE_CRITERIA

API = "https://www.googleapis.com/calendar/v3"
SCOPES = ["https://www.googleapis.com/auth/calendar"]
CLIENT = httpx.Client(timeout=25)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
COLORS = {str(n) for n in range(1, 12)}
NONE = "NONE"
# A TypeSafe answer below this probability is not acted on: a pick stops the run, other judgments are left out.
SURE = 0.5


FIELDS = dict(
    summary=nullable("string"),
    start=nullable("string"),
    end=nullable("string"),
    description=nullable("string"),
    location=nullable("string"),
)
CREATE = strict(**FIELDS, missing=nullable("string"))
SEARCH = strict(
    query=nullable("string"), time_min=nullable("string"), time_max=nullable("string"), missing=nullable("string")
)
CHANGE = strict(changes=strict(**FIELDS), missing=nullable("string"))


def credentials():
    # Imported here so browser skills and offline tests never need Google's auth libraries.
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    secrets = Path(os.environ.get("GOOGLE_CLIENT_SECRETS_JSON", "config/client_secrets.json"))
    token = Path(os.environ.get("GOOGLE_TOKEN_JSON", "config/token.json"))
    creds = None
    if token.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token), SCOPES)
        except ValueError:
            creds = None
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError:
            creds = None
    if not creds or not creds.valid:
        if not secrets.exists():
            raise ValueError(f"Calendar API skills need a Google OAuth client file at {secrets}; see README.")
        flow = InstalledAppFlow.from_client_secrets_file(str(secrets), SCOPES)
        creds = flow.run_local_server(port=int(os.environ.get("GOOGLE_OAUTH_LOCAL_SERVER_PORT", "8765")))
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text(creds.to_json())
    token.chmod(0o600)
    return creds


class Calendar:
    def __init__(self, calendar_id="primary"):
        self.path = f"/calendars/{quote(calendar_id, safe='')}"
        self.creds = credentials()

    def call(self, method, path, **kwargs):
        if not self.creds.valid:
            from google.auth.transport.requests import Request

            self.creds.refresh(Request())
        try:
            response = CLIENT.request(
                method, API + path, headers={"Authorization": f"Bearer {self.creds.token}"}, **kwargs
            )
        except httpx.HTTPError:
            raise RuntimeError("Google Calendar connection failed; check the calendar before trying again.") from None
        if response.is_error:
            try:
                reason = response.json()["error"]["message"]
            except (ValueError, KeyError, TypeError):
                reason = response.reason_phrase
            raise RuntimeError(f"Google Calendar returned HTTP {response.status_code}: {reason}")
        return response.json() if response.content else {}

    def calendars(self):
        items, params = [], {}
        while True:
            page = self.call("GET", "/users/me/calendarList", params=params)
            items += page.get("items", [])
            if not page.get("nextPageToken"):
                return items
            params["pageToken"] = page["nextPageToken"]

    def time_zone(self):
        return self.call("GET", self.path)["timeZone"]

    def events(self, query=None, time_min=None, time_max=None, limit=10):
        params = {"singleEvents": "true", "orderBy": "startTime"}
        for key, value in (("q", query), ("timeMin", time_min), ("timeMax", time_max)):
            if value:
                params[key] = value
        items = []
        while len(items) < limit:
            params["maxResults"] = min(250, limit - len(items))
            page = self.call("GET", self.path + "/events", params=params)
            items += page.get("items", [])
            if not page.get("nextPageToken"):
                break
            params["pageToken"] = page["nextPageToken"]
        return items[:limit]

    def insert(self, body):
        return self.call("POST", self.path + "/events", json=body)

    def patch(self, event_id, body):
        return self.call("PATCH", f"{self.path}/events/{quote(event_id, safe='')}", json=body)

    def delete(self, event_id):
        self.call("DELETE", f"{self.path}/events/{quote(event_id, safe='')}")


def parse_when(value, zone):
    """YYYY-MM-DD → date. ISO date-time → aware datetime; a value without an offset is read in `zone`."""
    try:
        if DAY.fullmatch(value):
            return date.fromisoformat(value)
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError(f"Unreadable date or time {value!r}; nothing changed.") from None
    return moment if moment.tzinfo else moment.replace(tzinfo=zone)


def event_times(event, zone):
    def read(field):
        return date.fromisoformat(field["date"]) if field.get("date") else parse_when(field["dateTime"], zone)

    return read(event["start"]), read(event["end"])


def day(value):
    return f"{value:%a} {value.day} {value:%b}"


def clock(moment):
    minutes = f".{moment.minute:02d}" if moment.minute else ""
    return f"{moment.hour % 12 or 12}{minutes}{'am' if moment.hour < 12 else 'pm'}"


def describe(event, zone):
    """One line per event, in tele_gcal's time style: Fri 9 Oct 3.30pm–4.30pm  Title  @ Place."""
    start, end = event_times(event, zone)
    if isinstance(start, datetime):
        start, end = start.astimezone(zone), end.astimezone(zone)
        if start.date() == end.date():
            when = f"{day(start)} {clock(start)}–{clock(end)}"
        else:
            when = f"{day(start)} {clock(start)} – {day(end)} {clock(end)}"
    else:
        last = end - timedelta(days=1)
        when = day(start) + (f" – {day(last)}" if last > start else "") + ", all day"
    location = f"  @ {event['location']}" if event.get("location") else ""
    return f"{when}  {event.get('summary') or '(no title)'}{location}"


def brief(event):
    return {k: event.get(k) for k in ("id", "summary", "start", "end", "location", "htmlLink")}


def guests(event):
    return [a["email"] for a in event.get("attendees", []) if a.get("email")]


def emails(text):
    """Each address in the text once, ignoring case, in order. These are the only guests that can be added."""
    found = {}
    for email in EMAIL.findall(text):
        found.setdefault(email.lower(), email)
    return list(found.values())


def event_body(args, zone, options, current=None):
    """A validated Calendar API body and notes for the user. With `current`, only the fields that change."""
    body, notes = {}, []
    for key in ("summary", "description", "location"):
        if args.get(key):
            body[key] = args[key].strip()
    if current is None and not body.get("summary"):
        raise ValueError("Need more detail: what should the event be called?")

    start, end = (parse_when(args[k], zone) if args.get(k) else None for k in ("start", "end"))
    old_start, old_end = event_times(current, zone) if current else (None, None)
    if current is None and start is None:
        raise ValueError("Need more detail: when does the event start?")
    if start is not None or end is not None:
        start = old_start if start is None else start
        if end is None:
            if old_start is not None and type(start) is type(old_start):
                end = start + (old_end - old_start)  # moving an event keeps its length
            elif isinstance(start, datetime):
                end = start + timedelta(minutes=int(options.get("default_duration_minutes", 60)))
            else:
                end = start + timedelta(days=1)
        if type(start) is not type(end):
            raise ValueError("Start and end must both be dates or both be times; nothing changed.")
        if end <= start:
            raise ValueError("The event would end before it starts; nothing changed.")
        for key, value in (("start", start), ("end", end)):
            if isinstance(value, datetime):
                body[key] = {"dateTime": value.isoformat(), "timeZone": zone.key}
            else:
                body[key] = {"date": value.isoformat()}
            if current is not None:
                # PATCH merges into the old start/end; clear the other kind when switching all-day ↔ timed.
                body[key].setdefault("date", None)
                body[key].setdefault("dateTime", None)

    if args.get("attendees") is not None:
        # Built in code from addresses in your details and guests already on the event; see judged_fields.
        body["attendees"] = [{"email": email} for email in args["attendees"]]

    if args.get("color_id") and args["color_id"] != (current or {}).get("colorId"):
        if args["color_id"] in COLORS:
            body["colorId"] = args["color_id"]
        else:
            notes.append(f"ignored unknown color {args['color_id']!r}")
    return body, notes


def preview(body, zone, current=None):
    if current is None:
        lines = [describe(body, zone)]
    else:
        merged = {**current, **body}
        lines = [f"from: {describe(current, zone)}", f"to:   {describe(merged, zone)}"]
    if "attendees" in body:
        old = f"{', '.join(guests(current)) or 'none'} → " if current else ""
        lines.append(f"guests: {old}{', '.join(guests(body)) or 'none'}")
    if "description" in body:
        lines.append(f"description: {body['description']}")
    if "colorId" in body:
        lines.append(f"color: {body['colorId']}")
    if current and current.get("recurringEventId"):
        lines.append("(this occurrence only, not the whole series)")
    return lines


def setup(skill):
    calendar = Calendar(skill.options.get("calendar_id", "primary"))
    name = skill.options.get("timezone") or calendar.time_zone()
    try:
        zone = ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"Unknown time zone {name!r}") from None
    return calendar, zone


def ask(skill, details, zone, schema, instructions, **context):
    now = datetime.now(zone)
    request = {
        "task": skill.task,
        "details": details,
        "instructions": instructions,
        "now": now.isoformat(timespec="minutes"),
        "weekday": f"{now:%A}",
        "timezone": zone.key,
        **({"skill_rules": list(skill.rules)} if skill.rules else {}),
        **context,
    }
    output, info = complete_json(CALENDAR, request, schema, "Calendar skills")
    if output is None or not conform(output, schema):
        raise ValueError("The text model's arguments did not match the operation; nothing changed.")
    if output.get("missing"):
        raise ValueError(f"Need more detail: {output['missing']}")
    return output, info


def judge(skill, details, questions, **state):
    """One TypeSafe request over the details. Returns (answers, call info); no questions means no request."""
    if not questions:
        return {}, None
    rules = {"skill_rules": list(skill.rules)} if skill.rules else {}
    body = {
        "model": os.environ.get("TYPESAFE_MODEL", "jev-latest"),
        "state": {"request": details, **state},
        "questions": {key: {**q, "instructions": {**q["instructions"], **rules}} for key, q in questions.items()},
    }
    started = perf_counter()
    result = typesafe(body)
    info = {
        "model": result.get("model"),
        "latency_ms": round((perf_counter() - started) * 1000),
        "usage": result.get("usage", {}),
        "answers": result["answers"],
        "request": body,
    }
    return result["answers"], info


def together(first, second):
    """Run two independent model calls side by side; returns both results."""
    with ThreadPoolExecutor(max_workers=1) as pool:
        later = pool.submit(second)
        return first(), later.result()


def chosen(answers, key, questions):
    """The option a Choice picked with probability SURE or more, else None. NONE is None."""
    if key not in questions:
        return None
    answer = validate_choice(answers.get(key, {}), questions[key]["criteria"])
    choice = answer["choice"]
    return choice if choice != NONE and answer["probabilities"][choice] >= SURE else None


def holds(answers, key):
    return validate_noul(answers.get(key, {})) > SURE


def category_question(options):
    keywords = options.get("keywords")
    if not keywords:
        return {}
    criteria = {name: {"keywords": list(words)} for name, words in keywords.items()}
    return {
        "category": {
            "type": "choice",
            "instructions": {"question": CATEGORY},
            "criteria": {**criteria, NONE: "None of these kinds clearly fits."},
        }
    }


def color_question(options, current=None):
    colors = options.get("colors")
    if not colors:
        return {}
    return {
        "color": {
            "type": "choice",
            "instructions": {"question": RECOLOR if current else COLOR},
            "criteria": {**dict.fromkeys(colors), NONE: "The event clearly fits none of these categories."},
        }
    }


def guest_questions(details, current=None):
    """A Noul per address in the details that isn't a guest yet (invite?) and per guest on the event (remove?)."""
    attendees = [a for a in (current or {}).get("attendees", []) if a.get("email")]
    known = {a["email"].lower() for a in attendees}
    questions = {}
    for i, email in enumerate(e for e in emails(details) if e.lower() not in known):
        questions[f"invite_{i}"] = {"type": "noul", "instructions": {"question": INVITE, "email": email}}
    for i, attendee in enumerate(attendees):
        # Requests name guests ("drop sam"); a name next to the address lets TypeSafe match it.
        name = attendee.get("displayName") or re.sub(r"[._]+", " ", attendee["email"].split("@")[0])
        questions[f"remove_{i}"] = {
            "type": "noul",
            "instructions": {"question": UNINVITE, "guest": {"email": attendee["email"], "name": name}},
            "criteria": UNINVITE_CRITERIA,
        }
    return questions


def judged_fields(answers, questions, options, current=None):
    """color_id and attendees from TypeSafe's answers. Code maps the category and builds the guest list."""
    fields = {}
    if category := chosen(answers, "color", questions):
        fields["color_id"] = str(options["colors"][category])
    add = [
        q["instructions"]["email"] for key, q in questions.items() if key.startswith("invite_") and holds(answers, key)
    ]
    drop = {
        q["instructions"]["guest"]["email"].lower()
        for key, q in questions.items()
        if key.startswith("remove_") and holds(answers, key)
    }
    if add or drop:
        fields["attendees"] = [e for e in guests(current or {}) if e.lower() not in drop] + add
    return fields


def search(calendar, zone, args, options, category=None):
    """The text model picks the query and range, TypeSafe the keyword category; Google supplies the events."""
    limit = int(options.get("max_results", 10))
    bounds = []
    for key, upper in (("time_min", False), ("time_max", True)):
        moment = parse_when(args[key], zone) if args.get(key) else None
        if moment is not None and not isinstance(moment, datetime):
            moment = datetime.combine(moment + timedelta(days=int(upper)), time.min, zone)
        bounds.append(moment)
    time_min, time_max = bounds
    now = datetime.now(zone)
    if time_min is None and (time_max is None or time_max > now):
        time_min = now  # upcoming events unless the details point at the past
    time_min, time_max = (m.isoformat() if m else None for m in (time_min, time_max))
    keywords = options.get("keywords", {}).get(category) if category else None
    if not keywords:
        return calendar.events((args.get("query") or "").strip() or None, time_min, time_max, limit)
    # A category search ("flights") lists the range and matches any of its keywords locally, as tele_gcal did.
    terms = [term.lower() for term in dict.fromkeys([category, *keywords])]

    def text(event):
        return " ".join(event.get(k) or "" for k in ("summary", "description", "location")).lower()

    return [e for e in calendar.events(None, time_min, time_max, 250) if any(t in text(e) for t in terms)][:limit]


def find(skill, details, calendar, zone, instructions):
    """Search terms from the text model and a keyword category from TypeSafe, asked side by side."""
    questions = category_question(skill.options)
    (args, call), (answers, judged) = together(
        lambda: ask(skill, details, zone, SEARCH, instructions),
        lambda: judge(skill, details, questions),
    )
    category = chosen(answers, "category", questions)
    events = search(calendar, zone, args, skill.options, category)
    record = {"search": args, "category": category, "model_calls": [call], "judgments": [judged] if judged else []}
    return events, record


def relative_day(event, zone):
    """Day offsets are computed here; TypeSafe compares words like "tomorrow", not dates."""
    start = event_times(event, zone)[0]
    gap = ((start.astimezone(zone).date() if isinstance(start, datetime) else start) - datetime.now(zone).date()).days
    return {0: "today", 1: "tomorrow", -1: "yesterday"}.get(gap, f"in {gap} days" if gap > 0 else f"{-gap} days ago")


def pick(skill, details, calendar, zone):
    """Search, then TypeSafe picks one result or none. Returns (event, probability, record). IDs come from code."""
    events, record = find(
        skill,
        details,
        calendar,
        zone,
        "Find the existing event the details refer to: a search query and a time range that includes it.",
    )
    if not events:
        raise ValueError("No matching event found; nothing changed.")
    criteria = {
        str(i): {
            "event": describe(e, zone),
            "day": relative_day(e, zone),
            "description": (e.get("description") or "")[:500],
            "guests": guests(e),
        }
        for i, e in enumerate(events, 1)
    }
    criteria[NONE] = "None of these events is the one the request refers to."
    questions = {"event": {"type": "choice", "instructions": {"question": EVENT}, "criteria": criteria}}
    answers, judged = judge(skill, details, questions)
    record["judgments"].append(judged)
    answer = validate_choice(answers.get("event", {}), criteria)
    choice, probabilities = answer["choice"], answer["probabilities"]
    if choice == NONE:
        raise ValueError("None of the events found is the one you mean; nothing changed.")
    if probabilities[choice] < SURE:
        likely = [k for k in sorted(probabilities, key=probabilities.get, reverse=True) if k != NONE][:2]
        options = " or ".join(f"“{criteria[k]['event']}”" for k in likely)
        raise ValueError(f"Not sure which event you mean: {options}. Nothing changed; say which one.")
    return events[int(choice) - 1], probabilities[choice], record


def list_calendars(skill, details, confirm):
    items = Calendar(skill.options.get("calendar_id", "primary")).calendars()
    for item in items:
        mark = "*" if item.get("primary") else " "
        print(f"  {mark} {item.get('summary', '(no title)'):<32} {item['id']}  {item.get('timeZone', '')}")
    keys = ("id", "summary", "timeZone", "primary")
    return {"status": "done", "calendars": [{k: item.get(k) for k in keys} for item in items]}


def find_events(skill, details, confirm):
    calendar, zone = setup(skill)
    events, record = find(
        skill,
        details,
        calendar,
        zone,
        "Choose a search query and time range for the events the details ask about. "
        "Use a null query to list everything in the range.",
    )
    for event in events:
        print(f"  {describe(event, zone)}")
    if not events:
        print("  No matching events.")
    return {"status": "done", **record, "events": [brief(e) for e in events]}


def create_event(skill, details, confirm):
    calendar, zone = setup(skill)
    questions = {**color_question(skill.options), **guest_questions(details)}
    (args, call), (answers, judged) = together(
        lambda: ask(skill, details, zone, CREATE, "Fill in the new event from the details."),
        lambda: judge(skill, details, questions),
    )
    args = {**args, **judged_fields(answers, questions, skill.options)}
    body, notes = event_body(args, zone, skill.options)
    record = {"arguments": args, "model_calls": [call], "judgments": [judged] if judged else [], "body": body}
    if not confirm("Create this event", [*notes, *preview(body, zone)]):
        return {"status": "declined", **record}
    event = calendar.insert(body)
    print(f"  created: {describe(event, zone)}\n  {event.get('htmlLink', '')}")
    return {"status": "done", **record, "event": brief(event)}


def update_event(skill, details, confirm):
    calendar, zone = setup(skill)
    event, probability, record = pick(skill, details, calendar, zone)
    current = {"event": describe(event, zone), "description": (event.get("description") or "")[:500]}
    questions = {**color_question(skill.options, event), **guest_questions(details, event)}
    (answer, call), (answers, judged) = together(
        lambda: ask(
            skill,
            details,
            zone,
            CHANGE,
            "In `changes`, set only the fields of `event` that the details change.",
            event=current,
        ),
        lambda: judge(skill, details, questions, event=current["event"]),
    )
    changes = {**(answer.get("changes") or {}), **judged_fields(answers, questions, skill.options, event)}
    body, notes = event_body(changes, zone, skill.options, current=event)
    record["model_calls"].append(call)
    record["judgments"] += [judged] if judged else []
    if not body:
        raise ValueError("The details did not describe any change; nothing changed.")
    record.update(event=brief(event), changes=changes, body=body)
    lines = [*notes, *preview(body, zone, event), f"match: {probability:.0%}"]
    if not confirm(f"Update “{event.get('summary') or '(no title)'}”", lines):
        return {"status": "declined", **record}
    updated = calendar.patch(event["id"], body)
    print(f"  updated: {describe(updated, zone)}")
    return {"status": "done", **record, "updated": brief(updated)}


def delete_event(skill, details, confirm):
    calendar, zone = setup(skill)
    event, probability, record = pick(skill, details, calendar, zone)
    record["event"] = brief(event)
    lines = [describe(event, zone)] + (["(this occurrence only)"] if event.get("recurringEventId") else [])
    if not confirm(f"Delete “{event.get('summary') or '(no title)'}”", [*lines, f"match: {probability:.0%}"]):
        return {"status": "declined", **record}
    calendar.delete(event["id"])
    print("  deleted.")
    return {"status": "done", **record}


OPERATIONS = {
    "list_calendars": list_calendars,
    "find_events": find_events,
    "create_event": create_event,
    "update_event": update_event,
    "delete_event": delete_event,
}
