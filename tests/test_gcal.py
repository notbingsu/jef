"""Calendar API skills. Offline: a fake calendar, scripted text-model and TypeSafe answers; no paid calls."""

from zoneinfo import ZoneInfo

import pytest

from jev_ultrafast import gcal
from jev_ultrafast.skills import Skill

ZONE = ZoneInfo("Asia/Singapore")
OPTIONS = {"default_duration_minutes": 60, "keywords": {"Flights": ["flight", "SQ"]}}


DETAILS = "Dentist Fri 9 Oct 3pm with ada@example.com"
SEARCH = {"query": "dentist", "time_min": None, "time_max": None, "missing": None}


def asked(judged, key):
    """The question with this id from whichever TypeSafe request carried it."""
    return next(body["questions"][key] for body in judged if key in body["questions"])


def skill(**options):
    return Skill(path="calendar/test", task="Test.", url="", api="gcal.test", options={**OPTIONS, **options})


def event(summary="Dentist", start="2026-10-09T15:00:00+08:00", end="2026-10-09T16:00:00+08:00", **extra):
    return {"id": "evt-" + summary, "summary": summary, "start": {"dateTime": start}, "end": {"dateTime": end}, **extra}


class FakeCalendar:
    def __init__(self, items=()):
        self.items, self.calls = list(items), []

    def events(self, query=None, time_min=None, time_max=None, limit=10):
        self.calls.append(("events", query, time_min, time_max, limit))
        return [e for e in self.items if not query or query.lower() in e["summary"].lower()][:limit]

    def insert(self, body):
        self.calls.append(("insert", body))
        return {**body, "id": "new", "htmlLink": "https://calendar.test/new"}

    def patch(self, event_id, body):
        self.calls.append(("patch", event_id, body))
        return {**next(e for e in self.items if e["id"] == event_id), **body}

    def delete(self, event_id):
        self.calls.append(("delete", event_id))


def verdict(question, want):
    """A TypeSafe answer: a noul value, or a choice given as an option or as its probabilities (default NONE)."""
    if question["type"] == "noul":
        return {"type": "noul", "noul": float(want or 0)}
    probabilities = (
        want if isinstance(want, dict) else {k: float(k == (want or gcal.NONE)) for k in question["criteria"]}
    )
    choice = max(probabilities, key=probabilities.get)
    return {"type": "choice", "choice": choice, "confidence": 1.0, "probabilities": probabilities}


@pytest.fixture
def run(monkeypatch):
    """Run an operation against a fake calendar with scripted answers; returns (record, calendar, sent, judged).

    `answers` are the text model's replies in order. `verdicts` maps TypeSafe question ids to answers."""

    def runner(operation, answers, items=(), approve=True, verdicts=None, details=DETAILS, **options):
        calendar, sent, judged = FakeCalendar(items), [], []
        replies = iter(answers)

        def complete_json(system, context, schema, purpose):
            sent.append(context)
            return next(replies), {"model": "test", "latency_ms": 1, "usage": {}}

        def typesafe(body):
            judged.append(body)
            questions = body["questions"]
            return {"model": "test", "answers": {k: verdict(q, (verdicts or {}).get(k)) for k, q in questions.items()}}

        monkeypatch.setattr(gcal, "setup", lambda _skill: (calendar, ZONE))
        monkeypatch.setattr(gcal, "complete_json", complete_json)
        monkeypatch.setattr(gcal, "typesafe", typesafe)
        record = operation(skill(**options), details, lambda *_: approve)
        return record, calendar, sent, judged

    return runner


def nulls(**values):
    return {**dict.fromkeys(gcal.CREATE["properties"]), **values}


def change(**values):
    return {"changes": {**dict.fromkeys(gcal.FIELDS), **values}, "missing": None}


def test_create_fills_default_duration_and_waits_for_confirmation(run):
    answer = nulls(summary="Dentist", start="2026-10-09T15:00:00")
    record, calendar, sent, judged = run(gcal.create_event, [answer], verdicts={"invite_0": 0.9})
    body = calendar.calls[-1][1]
    assert record["status"] == "done"
    assert body["start"] == {"dateTime": "2026-10-09T15:00:00+08:00", "timeZone": "Asia/Singapore"}
    assert body["end"]["dateTime"] == "2026-10-09T16:00:00+08:00"
    assert body["attendees"] == [{"email": "ada@example.com"}]
    # The text model never sees guests or colors; TypeSafe judges the one address in the details.
    assert "attendees" not in gcal.CREATE["properties"] and "colors" not in sent[0]
    assert sent[0]["timezone"] == "Asia/Singapore"
    assert list(judged[0]["questions"]) == ["invite_0"]


def test_only_addresses_in_the_details_can_be_invited(run):
    details = "Planning sync Fri 9 Oct 3pm with ada@example.com; send notes to Notes@Example.com and ada@EXAMPLE.com"
    answer = nulls(summary="Planning sync", start="2026-10-09T15:00:00")
    record, calendar, _, judged = run(
        gcal.create_event, [answer], verdicts={"invite_0": 0.9, "invite_1": 0.2}, details=details
    )
    invites = {k: asked(judged, k)["instructions"]["email"] for k in ("invite_0", "invite_1")}
    assert len(judged) == 1 and len(judged[0]["questions"]) == 2
    assert invites == {"invite_0": "ada@example.com", "invite_1": "Notes@Example.com"}
    assert calendar.calls[-1][1]["attendees"] == [{"email": "ada@example.com"}]


def test_create_color_comes_from_the_chosen_category(run):
    colors = {"physical": "6", "meetings": "7"}
    answer = nulls(summary="Gym", start="2026-10-09T07:00:00")
    _, calendar, _, judged = run(gcal.create_event, [answer], verdicts={"color": "meetings"}, colors=colors)
    assert set(asked(judged, "color")["criteria"]) == {"physical", "meetings", gcal.NONE}
    assert calendar.calls[-1][1]["colorId"] == "7"
    # An unsure category leaves the color alone.
    unsure = {"color": {"physical": 0.45, "meetings": 0.4, gcal.NONE: 0.15}}
    _, calendar, _, _ = run(gcal.create_event, [answer], verdicts=unsure, colors=colors)
    assert "colorId" not in calendar.calls[-1][1]


def test_no_typesafe_request_without_questions(run):
    _, _, _, judged = run(gcal.create_event, [nulls(summary="Gym", start="2026-10-09")], details="Gym Fri 9 Oct")
    assert judged == []


def test_declined_create_changes_nothing(run):
    record, calendar, _, _ = run(gcal.create_event, [nulls(summary="Dentist", start="2026-10-09")], approve=False)
    assert record["status"] == "declined"
    assert not any(call[0] == "insert" for call in calendar.calls)
    assert record["body"]["end"] == {"date": "2026-10-10"}  # all-day end date is exclusive


@pytest.mark.parametrize(
    "answer, message",
    [
        (nulls(missing="Which day?"), "Need more detail: Which day?"),
        (nulls(summary="Dentist", start="next friday"), "Unreadable"),
        (nulls(summary="Dentist", start="2026-10-09T15:00:00", end="2026-10-09T14:00:00"), "end before"),
        (nulls(summary="Dentist", start="2026-10-09", end="2026-10-09T14:00:00"), "both be dates"),
        ({**nulls(summary="Dentist"), "start": 3}, "did not match"),
        ({**nulls(summary="Dentist"), "attendees": ["ada@example.com"]}, "did not match"),
        (None, "did not match"),
    ],
)
def test_invalid_create_arguments_never_reach_the_calendar(run, answer, message):
    with pytest.raises(ValueError, match=message):
        run(gcal.create_event, [answer])


def test_update_picks_by_choice_and_moving_keeps_duration(run):
    items = [event("Standup"), event("Dentist")]
    record, calendar, sent, judged = run(
        gcal.update_event, [SEARCH, change(start="2026-10-09T17:30:00")], items=items, verdicts={"event": "1"}
    )
    kind, event_id, body = calendar.calls[-1]
    assert (kind, event_id) == ("patch", "evt-Dentist")
    assert body["start"]["dateTime"] == "2026-10-09T17:30:00+08:00"
    assert body["end"]["dateTime"] == "2026-10-09T18:30:00+08:00"
    assert body["start"]["date"] is None  # clears an all-day date if the event had one
    options = asked(judged, "event")["criteria"]
    assert set(options) == {"1", gcal.NONE} and options["1"]["event"].endswith("Dentist")
    # The text model fills changes for the picked event only; it never sees the other results.
    assert sent[1]["event"]["event"] == options["1"]["event"]


@pytest.mark.parametrize(
    "pick, message",
    [
        (gcal.NONE, "None of the events"),
        ({"1": 0.45, "2": 0.4, gcal.NONE: 0.15}, "Not sure which event you mean: “Fri 9 Oct 3pm–4pm  Dentist” or"),
        ({"1": 0.9, "3": 0.1}, "Invalid TypeSafe response"),
    ],
)
def test_update_stops_unless_one_result_is_clearly_meant(run, pick, message):
    items = [event(), event("Dentist", start="2026-10-16T15:00:00+08:00", end="2026-10-16T16:00:00+08:00")]
    with pytest.raises(ValueError, match=message):
        run(gcal.update_event, [SEARCH], items=items, verdicts={"event": pick})


def test_guests_change_only_when_typesafe_says_so(run):
    items = [event(attendees=[{"email": "sam@example.com", "displayName": "Sam Ng"}, {"email": "kim@example.com"}])]
    moved = change(summary="Dentist (moved)")
    _, calendar, _, judged = run(gcal.update_event, [SEARCH, moved], items, verdicts={"event": "1"})
    assert "attendees" not in calendar.calls[-1][2]
    assert asked(judged, "invite_0")["instructions"]["email"] == "ada@example.com"
    assert asked(judged, "remove_0")["instructions"]["guest"] == {"email": "sam@example.com", "name": "Sam Ng"}
    assert asked(judged, "remove_1")["instructions"]["guest"] == {"email": "kim@example.com", "name": "kim"}

    swap = {"event": "1", "invite_0": 0.9, "remove_0": 0.8}
    _, calendar, _, _ = run(gcal.update_event, [SEARCH, moved], items, verdicts=swap)
    assert calendar.calls[-1][2]["attendees"] == [{"email": "kim@example.com"}, {"email": "ada@example.com"}]


def test_update_skips_a_color_the_event_already_has(run):
    items = [event(colorId="7")]
    with pytest.raises(ValueError, match="did not describe any change"):
        run(
            gcal.update_event,
            [SEARCH, change()],
            items,
            verdicts={"event": "1", "color": "meetings"},
            colors={"meetings": "7"},
        )


def test_delete_needs_confirmation(run):
    record, calendar, _, _ = run(gcal.delete_event, [SEARCH], [event()], approve=False, verdicts={"event": "1"})
    assert record["status"] == "declined" and calendar.calls[-1][0] == "events"
    record, calendar, sent, _ = run(gcal.delete_event, [SEARCH], [event()], verdicts={"event": "1"})
    assert calendar.calls[-1] == ("delete", "evt-Dentist")
    assert len(sent) == 1  # one text-model call: the search; TypeSafe picks the event


def test_find_uses_the_keyword_category_typesafe_picks(run):
    items = [event("SQ 318 to London"), event("Dentist")]
    plan = {"query": "flight", "time_min": "2026-10-01", "time_max": "2026-10-31", "missing": None}
    record, calendar, _, judged = run(gcal.find_events, [plan], items, verdicts={"category": "Flights"})
    assert [e["summary"] for e in record["events"]] == ["SQ 318 to London"]
    assert record["category"] == "Flights" and calendar.calls[0][1] is None
    assert asked(judged, "category")["criteria"]["Flights"] == {"keywords": ["flight", "SQ"]}


def test_keyword_category_lists_the_range_and_filters_locally():
    calendar = FakeCalendar([event("SQ 318 to London"), event("Dentist"), event("Lunch", location="Flight club")])
    args = {"query": "flights", "time_min": "2026-10-01", "time_max": "2026-10-31"}
    found = gcal.search(calendar, ZONE, args, OPTIONS, "Flights")
    assert [e["summary"] for e in found] == ["SQ 318 to London", "Lunch"]
    _, query, time_min, time_max, limit = calendar.calls[0]
    assert query is None and limit == 250
    assert (time_min, time_max) == ("2026-10-01T00:00:00+08:00", "2026-11-01T00:00:00+08:00")


def test_search_defaults_to_upcoming_unless_the_range_is_in_the_past():
    calendar = FakeCalendar()
    gcal.search(calendar, ZONE, {"query": "dentist"}, OPTIONS)
    assert calendar.calls[-1][2] is not None
    gcal.search(calendar, ZONE, {"query": "dentist", "time_max": "2020-01-01"}, OPTIONS)
    assert calendar.calls[-1][2] is None


def test_describe_uses_compact_times():
    assert gcal.describe(event(start="2026-10-09T15:30:00+08:00", location="Clinic"), ZONE) == (
        "Fri 9 Oct 3.30pm–4pm  Dentist  @ Clinic"
    )
    all_day = {"summary": "Trip", "start": {"date": "2026-10-09"}, "end": {"date": "2026-10-12"}}
    assert gcal.describe(all_day, ZONE) == "Fri 9 Oct – Sun 11 Oct, all day  Trip"


QUESTION = "when is the next evening i am free"
WEEK = {"query": None, "time_min": "2026-10-06", "time_max": "2026-10-12", "missing": None}


def evening(day, summary="Dinner"):
    return event(summary, start=f"2026-10-{day:02d}T19:00:00+08:00", end=f"2026-10-{day:02d}T21:00:00+08:00")


def test_a_question_gets_a_written_answer_from_everything_in_the_range(run, capsys):
    items = [evening(6), evening(7, "Sync")]
    reply = {"answer": "Thursday 8 Oct is your next free evening (taking evening as 6pm-10pm)."}
    record, calendar, sent, judged = run(
        gcal.find_events, [WEEK, reply], items, verdicts={"answer": 0.9}, details=QUESTION
    )
    # One TypeSafe request carries both the category and the answer judgment, beside the search plan.
    assert len(judged) == 1 and {"answer", "category"} <= set(judged[0]["questions"])
    assert calendar.calls[0][-1] == gcal.ANSWER_LIMIT  # an answer reasons over everything, not the first ten
    context = sent[1]
    assert context["question"] == QUESTION and context["complete"] is True
    assert context["days"][:3] == ["Tue 6 Oct", "Wed 7 Oct", "Thu 8 Oct"] and context["days"][-1] == "Mon 12 Oct"
    assert [e["when"] for e in context["events"]] == ["Tue 6 Oct 7pm–9pm  Dinner", "Wed 7 Oct 7pm–9pm  Sync"]
    assert record["answer"]["text"].startswith("Thursday") and record["answer_needed"] == 0.9
    out = capsys.readouterr().out
    assert "→ Thursday 8 Oct is your next free evening" in out and "Dinner" in out  # the evidence follows


def test_a_plain_listing_asks_no_text_model_for_an_answer(run, capsys):
    record, calendar, sent, _ = run(
        gcal.find_events, [WEEK], [evening(6)], verdicts={"answer": 0.1}, details="what's on"
    )
    assert len(sent) == 1 and "answer" not in record and calendar.calls[0][-1] == 10
    assert "Dinner" in capsys.readouterr().out


def test_an_empty_range_still_gets_an_answer(run):
    record, _, sent, _ = run(
        gcal.find_events, [WEEK, {"answer": "Tonight: nothing is on."}], [], verdicts={"answer": 0.9}, details=QUESTION
    )
    assert sent[1]["events"] == [] and record["answer"]["text"] == "Tonight: nothing is on."


@pytest.mark.parametrize("reply", [None, {"answer": None}, {"answer": "  "}, {"answer": 3}, {"answer": "x" * 601}])
def test_an_unusable_answer_still_shows_the_events(run, capsys, reply):
    record, _, _, _ = run(gcal.find_events, [WEEK, reply], [evening(6)], verdicts={"answer": 0.9}, details=QUESTION)
    out = capsys.readouterr().out
    assert record["status"] == "done" and "no answer" in out and "Dinner" in out


def test_a_full_fetch_is_flagged_incomplete_and_summarised(run, capsys):
    items = [evening(6, f"Event {n}") for n in range(gcal.ANSWER_LIMIT)]
    record, _, sent, _ = run(
        gcal.find_events, [WEEK, {"answer": "Unclear."}], items, verdicts={"answer": 0.9}, details=QUESTION
    )
    assert sent[1]["complete"] is False
    out = capsys.readouterr().out
    assert f"worked out from {gcal.ANSWER_LIMIT} events" in out and "Event 99" not in out


def test_days_cover_the_range_and_an_open_range_gets_two_weeks():
    start = gcal.datetime(2026, 10, 6, 9, tzinfo=ZONE)
    end = gcal.datetime(2026, 10, 9, tzinfo=ZONE)  # midnight: the 8th is the last whole day
    assert gcal.days(start, end, ZONE) == ["Tue 6 Oct", "Wed 7 Oct", "Thu 8 Oct"]
    assert len(gcal.days(start, None, ZONE)) == gcal.OPEN_RANGE_DAYS + 1
