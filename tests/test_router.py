"""Routing a plain-language request to one leaf skill. Offline: TypeSafe answers are scripted."""

import pytest

from jev_ultrafast import cli, model, router


def write(root, path, text):
    file = root / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(text)


@pytest.fixture
def tree(tmp_path, monkeypatch):
    write(tmp_path, "calendar/_branch.toml", 'description = "Google Calendar"\n')
    write(tmp_path, "calendar/create.toml", 'description = "Add"\ntask = "Create."\napi = "gcal.create_event"\n')
    write(tmp_path, "calendar/find.toml", 'description = "Show events"\ntask = "Find."\napi = "gcal.find_events"\n')
    write(tmp_path, "calendar/browser/_branch.toml", 'description = "In the browser"\nurl = "https://cal.test/"\n')
    write(tmp_path, "calendar/browser/create.toml", 'description = "Add one event"\ntask = "Create."\n')
    write(tmp_path, "notes/_branch.toml", 'description = "Notes"\nurl = "https://notes.test/"\n')
    write(tmp_path, "notes/add.toml", 'description = "Add a note"\ntask = "Add."\n')
    monkeypatch.setenv("JEV_SKILLS_DIR", str(tmp_path))
    return tmp_path


def choice(ids, selected):
    return {"choice": selected, "confidence": 1.0, "probabilities": {i: float(i == selected) for i in ids}}


@pytest.fixture
def answers(monkeypatch):
    """Script TypeSafe: answers(body) -> {question: answer}. Records every request body."""
    sent = []

    def install(make):
        def typesafe(body):
            sent.append(body)
            return {"model": "test", "answers": make(body["questions"])}

        monkeypatch.setattr(router, "typesafe", typesafe)
        return sent

    return install


def test_one_request_with_heads_only_for_branches_that_need_a_choice(tree, answers):
    sent = answers(lambda q: {
        "use_case": choice(q["use_case"]["criteria"], "calendar"),
        "skill_in_calendar_0": choice(q["skill_in_calendar_0"]["criteria"], "calendar/find"),
    })
    path, record = router.route("any flights this month?")
    assert path == "calendar/find" and record["use_case"] == "calendar"
    questions = sent[0]["questions"]
    assert len(sent) == 1 and set(questions) == {"use_case", "skill_in_calendar_0"}
    assert set(questions["use_case"]["criteria"]) == {"calendar", "notes", router.NONE}
    fallback = questions["skill_in_calendar_0"]["criteria"]["calendar/browser/create"]
    assert fallback["kind"] == "browser" and fallback["within"] == "In the browser"
    assert questions["skill_in_calendar_0"]["criteria"]["calendar/create"]["kind"] == "api"


def test_single_leaf_use_case_needs_no_skill_head(tree, answers):
    answers(lambda q: {"use_case": choice(q["use_case"]["criteria"], "notes"), "skill_in_calendar_0": {"x": 1}})
    path, record = router.route("note: buy milk")
    assert path == "notes/add" and record["skill_probability"] == 1.0


def test_none_routes_nowhere(tree, answers):
    answers(lambda q: {"use_case": choice(q["use_case"]["criteria"], router.NONE)})
    assert router.route("order a pizza")[0] is None


def test_chosen_head_must_offer_the_skill(tree, answers):
    answers(lambda q: {
        "use_case": choice(q["use_case"]["criteria"], "calendar"),
        "skill_in_calendar_0": choice([*q["skill_in_calendar_0"]["criteria"], "notes/add"], "notes/add"),
    })
    with pytest.raises(ValueError, match="Invalid TypeSafe"):
        router.route("add a note")


def test_missing_typesafe_key_stops_before_any_request(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    with pytest.raises(ValueError, match="TYPESAFE_API_KEY"):
        model.typesafe({})


@pytest.fixture
def runs(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "load_environment", lambda: None)
    monkeypatch.setattr(cli, "run_api", lambda skill, details, *_: calls.append((skill.path, details)) or 0)
    monkeypatch.setattr(cli, "run_browser", lambda skill, details, *_: calls.append(skill.path))
    return calls


def routed(path, use_case=0.9, skill=0.9):
    return lambda request: (path, {"use_case_probability": use_case, "skill_probability": skill, "latency_ms": 5})


def test_request_runs_the_routed_skill_with_the_whole_request(tree, runs, monkeypatch):
    monkeypatch.setattr(cli.router, "route", routed("calendar/find"))
    assert cli.main(["any", "flights", "this", "month?"]) == 0
    assert runs == [("calendar/find", "any flights this month?")]


def test_unsure_route_without_a_terminal_runs_nothing(tree, runs, monkeypatch):
    monkeypatch.setattr(cli.router, "route", routed("calendar/find", 0.6, 0.6))
    assert cli.main(["flights?"]) == 1 and runs == []


def test_route_only_and_explicit_skill(tree, runs, monkeypatch):
    monkeypatch.setattr(cli.router, "route", routed("notes/add"))
    assert cli.main(["--route-only", "note: buy milk"]) == 0 and runs == []
    monkeypatch.setattr(cli.router, "route", lambda request: pytest.fail("--skill must not route"))
    cli.main(["--skill", "notes/add", "buy", "milk"])
    assert runs == ["notes/add"]
