"""moomoo API skills. Offline: a fake OpenD, scripted TypeSafe and text-model answers; no gateway, no paid calls."""

from pathlib import Path

import pandas
import pytest

from jev_ultrafast import moomoo
from jev_ultrafast.skills import Skill

OK = moomoo.sdk.RET_OK
OPTIONS = {"acc_id": 42, "security_firm": "FUTUSG", "trd_env": "REAL", "currency": "USD"}

ACCOUNTS = [
    {"acc_id": 42, "trd_env": "REAL", "acc_type": "MARGIN", "acc_role": "NONE",
     "uni_card_num": "1008", "trdmarket_auth": ["US"]},
    {"acc_id": 7, "trd_env": "SIMULATE", "acc_type": "CASH", "acc_role": "NONE",
     "uni_card_num": "N/A", "trdmarket_auth": ["HK"]},
]
FUNDS = {
    "currency": "USD", "total_assets": 6775.4, "cash": 1275.11, "market_val": 5498.4,
    "available_funds": "N/A", "initial_margin": 0.0, "power": 1118.79, "risk_status": "LEVEL3",
}


def position(code="US.NVDA", name="NVIDIA", currency="USD", qty=9.0, **extra):
    """A position row as OpenD returns it: the app-aligned fields, and the diluted ones that must never be read."""
    return {
        "code": code, "stock_name": name, "currency": currency, "qty": qty, "can_sell_qty": qty,
        "average_cost": 193.599, "nominal_price": 239.229, "market_val": 2153.06,
        "unrealized_pl": 410.666, "pl_ratio_avg_cost": 23.57, "realized_pl": 0.0, "today_pl_val": -0.099,
        # Diluted cost basis: flatters every number above. Nothing in moomoo.py may read these.
        "cost_price": 193.281, "pl_val": 413.536, "pl_ratio": 24.1, "diluted_cost": 193.281,
        **extra,
    }


def skill(**options):
    return Skill(path="moomoo/test", task="Test.", url="", api="moomoo.test", options={**OPTIONS, **options})


class FakeOpenD:
    def __init__(self, positions=(), accounts=ACCOUNTS, funds=FUNDS, logged_in=True, fills=(), orders=()):
        self.positions, self.accounts, self.funds, self.logged_in = list(positions), accounts, funds, logged_in
        self.fills, self.orders, self.calls = list(fills), list(orders), []

    def get_global_state(self):
        return OK, {"trd_logined": self.logged_in, "qot_logined": True}

    def get_acc_list(self):
        return OK, pandas.DataFrame(self.accounts)

    def accinfo_query(self, **kwargs):
        self.calls.append(("accinfo", kwargs))
        return OK, pandas.DataFrame([self.funds])

    def position_list_query(self, **kwargs):
        self.calls.append(("positions", kwargs))
        return OK, pandas.DataFrame(self.positions)

    def history_deal_list_query(self, **kwargs):
        self.calls.append(("fills", kwargs))
        return OK, pandas.DataFrame(self.fills)

    def history_order_list_query(self, **kwargs):
        self.calls.append(("orders", kwargs))
        return OK, pandas.DataFrame(self.orders)

    def close(self):
        pass


@pytest.fixture
def run(monkeypatch):
    """Run an operation against a fake OpenD with scripted answers; returns (record, opend, sent, judged).

    `verdicts` maps TypeSafe question ids to a noul probability or a chosen option. `answers` are the text model's
    replies in order."""

    def runner(operation, opend=None, verdicts=None, answers=(), details="how am I doing?", **options):
        opend = opend or FakeOpenD([position()])
        sent, judged, replies = [], [], iter(answers)
        monkeypatch.setattr(moomoo, "reachable", lambda where: True)
        monkeypatch.setattr(moomoo, "connect", lambda where, firm=None: opend)

        def typesafe(body):
            judged.append(body)
            given = {}
            for key, question in body["questions"].items():
                want = (verdicts or {}).get(key)
                if question["type"] == "noul":
                    given[key] = {"type": "noul", "noul": float(want or 0)}
                else:
                    probabilities = want if isinstance(want, dict) else {
                        k: float(k == (want or moomoo.NONE)) for k in question["criteria"]
                    }
                    choice = max(probabilities, key=probabilities.get)
                    given[key] = {
                        "type": "choice", "choice": choice, "confidence": 1.0, "probabilities": probabilities,
                    }
            return {"model": "test", "usage": {}, "answers": given}

        def complete_json(system, context, schema, purpose):
            sent.append(context)
            return next(replies), {"model": "test", "latency_ms": 1, "usage": {}}

        monkeypatch.setattr(moomoo, "typesafe", typesafe)
        monkeypatch.setattr(moomoo, "complete_json", complete_json)
        record = moomoo.OPERATIONS[operation](skill(**options), details, lambda *a: True)
        return record, opend, sent, judged

    return runner


# --- the invariant the whole branch rests on -------------------------------------------------------------------

def test_nothing_in_the_module_can_change_the_account():
    """Read-only is a property of the code, not of a prompt: no mutating SDK call may appear here at all."""
    source = Path("jev_ultrafast/moomoo.py").read_text()
    for forbidden in ("place_order", "place_combo_order", "modify_order", "cancel_order", "cancel_all_order",
                      "change_order", "unlock_trade", "set_price_reminder"):
        # The call form, so the docstring may still name what this module refuses to do.
        assert f".{forbidden}(" not in source, f"{forbidden} must never be called from a read-only module"


def test_the_diluted_cost_fields_are_never_read():
    source = Path("jev_ultrafast/moomoo.py").read_text()
    for forbidden in ('"cost_price"', '"pl_val"', '"pl_ratio"', '"diluted_cost"'):
        assert f"row.get({forbidden})" not in source


# --- options ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "options, message",
    [
        ({"trd_env": "PAPER"}, "trd_env"),
        ({"currency": "GBP"}, "currency"),
        ({"security_firm": "FUTUXX"}, "security_firm"),
        ({"acc_id": "42"}, "acc_id"),
        ({"acc_id": -1}, "acc_id"),
        ({"port": 0}, "port"),
        ({"max_results": 0}, "max_results"),
        ({"timezone": "Mars/Olympus"}, "time zone"),
    ],
)
def test_a_bad_option_fails_loudly(options, message):
    with pytest.raises(ValueError, match=message):
        moomoo.settings(skill(**options))


# --- reading the account ---------------------------------------------------------------------------------------

def test_holdings_report_the_average_cost_numbers_the_app_shows(run):
    record, _, _, _ = run("holdings", verdicts={"answer": 0.1})
    held = record["holdings"][0]
    assert (held["cost"], held["unrealized"], held["percent"]) == (193.6, 410.67, 23.57)
    assert held["price"] == 239.23 and held["value"] == 2153.06
    assert record["funds"]["total"] == 6775.4


def test_available_funds_falls_back_when_the_account_reports_none(run):
    record, _, _, _ = run("holdings", verdicts={"answer": 0.1})
    # available_funds is "N/A" here and initial_margin is 0, so the fallback is total assets.
    assert record["funds"]["available"] == 6775.4


def test_positions_are_read_fresh_rather_than_from_opends_cache(run):
    _, opend, _, _ = run("holdings", verdicts={"answer": 0.1})
    positions = next(kwargs for name, kwargs in opend.calls if name == "positions")
    assert positions["refresh_cache"] is True and positions["acc_id"] == 42


def test_a_missing_app_field_stops_the_read(run):
    broken = position()
    del broken["average_cost"]
    with pytest.raises(RuntimeError, match="average_cost"):
        run("holdings", opend=FakeOpenD([broken]), verdicts={"answer": 0.1})


def test_amounts_in_different_currencies_are_kept_apart(run, capsys):
    opend = FakeOpenD([position(), position("HK.00700", "Tencent", "HKD")])
    record, _, _, _ = run("holdings", opend=opend, verdicts={"answer": 0.1})
    groups = moomoo.by_currency(record["holdings"])
    assert sorted(groups) == ["HKD", "USD"] and len(groups["USD"]) == 1
    printed = capsys.readouterr().out
    # One subtotal row per currency, and never a single figure spanning both.
    assert sum(line.strip().startswith("subtotal") for line in printed.splitlines()) == 2
    assert "subtotals are per currency" in printed


def test_an_empty_account_says_so_rather_than_inventing_a_total(run, capsys):
    record, _, _, _ = run("holdings", opend=FakeOpenD([]), verdicts={"answer": 0.1})
    assert record["holdings"] == [] and record["status"] == "done"
    assert "No positions held." in capsys.readouterr().out


# --- which account ---------------------------------------------------------------------------------------------

def test_several_accounts_without_a_pinned_id_stops_and_names_them(run):
    both = [dict(ACCOUNTS[0]), {**ACCOUNTS[0], "acc_id": 99}]
    with pytest.raises(ValueError, match="2 REAL accounts"):
        run("holdings", opend=FakeOpenD([position()], accounts=both), acc_id=None)


def test_a_pinned_id_that_opend_does_not_show_stops(run):
    with pytest.raises(ValueError, match="no account 1234"):
        run("holdings", acc_id=1234)


def test_one_account_needs_no_pin(run):
    record, _, _, _ = run("holdings", opend=FakeOpenD([position()], accounts=[ACCOUNTS[0]]),
                          acc_id=None, verdicts={"answer": 0.1})
    assert record["account"]["acc_id"] == 42


def test_a_closed_opend_is_a_clear_failure(monkeypatch):
    monkeypatch.setattr(moomoo, "reachable", lambda where: False)
    with pytest.raises(ValueError, match="OpenD running and logged in"):
        moomoo.connect(moomoo.settings(skill()))


def test_opend_running_but_not_logged_in_says_which(run):
    with pytest.raises(ValueError, match="not logged in"):
        run("holdings", opend=FakeOpenD([position()], logged_in=False))


# --- questions over the holdings -------------------------------------------------------------------------------

def test_a_question_gets_a_written_answer_from_the_holdings(run, capsys):
    record, _, sent, _ = run(
        "holdings", verdicts={"answer": 0.9}, answers=[{"answer": "NVIDIA is 32% of the account."}],
        details="what is my biggest position?",
    )
    assert record["answer"]["text"] == "NVIDIA is 32% of the account."
    assert "NVIDIA is 32% of the account." in capsys.readouterr().out
    # The model sees only what was read, never the diluted fields.
    assert sent[0]["holdings"] == record["holdings"] and "cost_price" not in str(sent[0])


def test_a_plain_listing_asks_no_text_model(run):
    record, _, sent, _ = run("holdings", verdicts={"answer": 0.1}, details="what do I hold?")
    assert sent == [] and "answer" not in record


def test_an_unusable_answer_still_shows_the_holdings(run, capsys):
    record, _, _, _ = run("holdings", verdicts={"answer": 0.9}, answers=[{"answer": None}])
    assert record["answer"]["text"] is None
    assert "US.NVDA" in capsys.readouterr().out


# --- one holding -----------------------------------------------------------------------------------------------

def test_a_holding_is_picked_from_the_rows_that_were_read(run, capsys):
    opend = FakeOpenD([position(), position("US.AAPL", "Apple")])
    record, _, _, judged = run("position", opend=opend, verdicts={"holding": "2"}, details="how is apple doing?")
    assert record["holding"]["code"] == "US.AAPL" and record["match"] == 1.0
    # Every option offered was a row from the account, plus NONE.
    criteria = judged[0]["questions"]["holding"]["criteria"]
    assert sorted(criteria) == ["1", "2", "NONE"]
    assert "US.AAPL" in capsys.readouterr().out


@pytest.mark.parametrize(
    "pick, message",
    [
        (moomoo.NONE, "None of your holdings"),
        ({"1": 0.45, "2": 0.4, moomoo.NONE: 0.15}, "Not sure which holding"),
    ],
)
def test_an_unclear_holding_stops_rather_than_guesses(run, pick, message):
    opend = FakeOpenD([position(), position("US.AAPL", "Apple")])
    with pytest.raises(ValueError, match=message):
        run("position", opend=opend, verdicts={"holding": pick})


def test_an_empty_account_has_no_position_to_show(run):
    with pytest.raises(ValueError, match="Nothing is held"):
        run("position", opend=FakeOpenD([]))


# --- activity --------------------------------------------------------------------------------------------------

def test_an_open_range_covers_the_last_week(run):
    record, _, _, _ = run("activity", answers=[{"start": None, "end": None, "missing": None}])
    start, end = record["searched"]["from"], record["searched"]["to"]
    assert moomoo.DAY.fullmatch(start) and moomoo.DAY.fullmatch(end) and start < end


def test_the_range_the_text_model_writes_is_the_one_queried(run):
    record, opend, _, _ = run("activity", answers=[{"start": "2026-10-01", "end": "2026-10-06", "missing": None}])
    assert record["searched"] == {"from": "2026-10-01", "to": "2026-10-06"}
    assert next(k for n, k in opend.calls if n == "fills")["start"] == "2026-10-01"


@pytest.mark.parametrize(
    "reply, message",
    [
        ({"start": "last Monday", "end": None, "missing": None}, "not YYYY-MM-DD"),
        ({"start": "2026-10-09", "end": "2026-10-01", "missing": None}, "ends before it starts"),
        ({"start": None, "end": None, "missing": "which week?"}, "Need more detail"),
    ],
)
def test_a_range_that_does_not_check_out_reads_nothing(run, reply, message):
    with pytest.raises(ValueError, match=message):
        run("activity", answers=[reply])


# --- accounts --------------------------------------------------------------------------------------------------

def test_a_long_account_id_survives_exactly(run, capsys):
    """A real acc_id runs to 18 digits. Read through a float it comes back one too high and never matches the pin."""
    big = 283726802396538239
    account = {**ACCOUNTS[0], "acc_id": big}
    record, _, _, _ = run("holdings", opend=FakeOpenD([position()], accounts=[account]),
                          acc_id=big, verdicts={"answer": 0.1})
    assert record["account"]["acc_id"] == big
    assert str(big) in capsys.readouterr().out


def test_accounts_lists_the_ids_a_pin_needs(run, capsys):
    record, _, _, _ = run("accounts")
    assert [a["acc_id"] for a in record["accounts"]] == [42, 7]
    assert "42" in capsys.readouterr().out
