"""moomoo holdings through the moomoo OpenAPI, no browser.

Read-only by construction: nothing here places, changes or cancels an order, and the SDK's `unlock_trade` is never
called, so these skills work with trading still locked. Every query goes to an OpenD gateway you run and log into on
this machine; there is no cloud endpoint and no token, so a closed OpenD is a failed run rather than a stale answer.

TypeSafe makes the closed-set judgments: which holding the request means, and whether a question needs working out
rather than a list. The text model writes only free text and dates into a typed schema. Code finds every candidate,
resolves the account, turns the SDK's DataFrames into plain rows at the boundary, and validates each field it keeps.
"""

import math
import re
import socket
from datetime import date, datetime, timedelta
from time import perf_counter
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import moomoo as sdk  # the OpenAPI SDK, not this module: an absolute import. It brings pandas, paid once per server.

from . import config, console
from .memory import Unanswered
from .model import complete_json, conform, nullable, strict, typesafe, validate_choice, validate_noul
from .questions import (
    HOLDING,
    HOLDINGS_ANSWER,
    HOLDINGS_ANSWER_NEEDED,
    HOLDINGS_ANSWER_NEEDED_CRITERIA,
    MOOMOO_ARGUMENTS,
)

# A TypeSafe answer below this probability is not acted on, as elsewhere in jev.
SURE = 0.5
NONE = "NONE"
# OpenD listens on this machine. A short probe first, so a closed gateway fails at once instead of in the SDK's retries.
HOST, PORT = "127.0.0.1", 11111
PROBE_WAIT = 2
ENVIRONMENTS = ("REAL", "SIMULATE")
FIRMS = ("FUTUSECURITIES", "FUTUINC", "FUTUSG", "FUTUAU", "FUTUCA", "FUTUJP", "FUTUMY")
CURRENCIES = ("HKD", "USD", "CNH", "JPY", "AUD", "CAD", "MYR", "SGD")
DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
# A request that names no range reads the last month. Code chooses it, and the run says so: a window nobody
# asked for must never be a silent assumption, and a follow-up can replace it.
ACTIVITY_DAYS = 30
ANSWER_LENGTH = 600

RANGE = strict(start=nullable("string"), end=nullable("string"), missing=nullable("string"))
ANSWER_SCHEMA = strict(answer=nullable("string"))

# The position fields the moomoo app shows, and the only cost and P&L fields read here. The SDK also returns
# cost_price, pl_val and pl_ratio on a diluted cost basis, which overstate gains and understate losses against the
# app; a report built on them would quietly disagree with what you see on your phone.
NEEDED = (
    "code",
    "stock_name",
    "currency",
    "qty",
    "average_cost",
    "nominal_price",
    "market_val",
    "unrealized_pl",
    "pl_ratio_avg_cost",
)
ACCOUNT_KEYS = ("acc_id", "security_firm", "trd_env", "acc_type", "acc_role", "uni_card_num", "trdmarket_auth")

# The SDK logs its connections to stdout, and a run speaks only through the console, so that is turned off once here.
sdk.SysConfig.enable_console_log(False)


def number(value, default=0.0):
    """A float from the SDK, which answers "N/A" for a field an account type does not carry."""
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def identifier(value):
    """An account id, kept exact. OpenD sends it as a 64-bit integer and a real one runs to 18 digits, past what a
    float holds exactly: through float(), 283726802396538239 comes back as ...240 and then never matches the id
    pinned in the skill's options."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def money(value):
    """Rounded for display and for the trace: the SDK answers in full binary precision (117.380000000000003)."""
    return round(number(value), 2)


def rows(frame):
    """The SDK answers with a pandas DataFrame; jev works in plain rows, so the frame stops at this boundary."""
    if frame is None or not hasattr(frame, "iloc") or len(frame) == 0:
        return []
    return [{column: frame.iloc[i][column] for column in frame.columns} for i in range(len(frame))]


def settings(skill):
    """The skill's options, validated like a skill file: a typo must fail loudly, not read the wrong account."""
    options = skill.options
    env = str(options.get("trd_env", "REAL")).upper()
    if env not in ENVIRONMENTS:
        raise ValueError(f"moomoo skills: trd_env must be REAL or SIMULATE, not {options.get('trd_env')!r}")
    firm = options.get("security_firm")
    if firm is not None and str(firm).upper() not in FIRMS:
        raise ValueError(f"moomoo skills: security_firm must be one of {', '.join(FIRMS)}")
    currency = str(options.get("currency", "USD")).upper()
    if currency not in CURRENCIES:
        raise ValueError(f"moomoo skills: currency must be one of {', '.join(CURRENCIES)}")
    acc_id = options.get("acc_id")
    if acc_id is not None and (type(acc_id) is not int or acc_id <= 0):
        raise ValueError(
            "moomoo skills: acc_id must be an account's numeric id; `jev list my moomoo accounts` lists them"
        )
    host = options.get("host", HOST)
    if not isinstance(host, str) or not host.strip():
        raise ValueError("moomoo skills: host must be the address OpenD listens on")
    port = options.get("port", PORT)
    if type(port) is not int or not 0 < port < 65536:
        raise ValueError("moomoo skills: port must be OpenD's port number")
    limit = options.get("max_results", 20)
    if type(limit) is not int or limit <= 0:
        raise ValueError("moomoo skills: max_results must be a positive whole number")
    zone = zone_of(options.get("timezone"))
    return {
        "trd_env": env,
        "security_firm": str(firm).upper() if firm else None,
        "currency": currency,
        "acc_id": acc_id,
        "host": host.strip(),
        "port": port,
        "max_results": limit,
        "zone": zone,
    }


def zone_of(name):
    if not name:
        return datetime.now().astimezone().tzinfo
    try:
        return ZoneInfo(str(name))
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"moomoo skills: unknown time zone {name!r}") from None


def reachable(where):
    with socket.socket() as probe:
        probe.settimeout(PROBE_WAIT)
        return probe.connect_ex((where["host"], where["port"])) == 0


def connect(where, firm=None):
    """A trade connection to OpenD. The SDK defaults `filter_trdmarket` to HK, which hides accounts in every other
    market, so each connection here asks for NONE and lets code do the filtering."""
    if not reachable(where):
        raise ValueError(
            f"moomoo skills need OpenD running and logged in on {where['host']}:{where['port']}. "
            "Open the moomoo OpenD app, log in, and try again."
        )
    name = firm or where["security_firm"] or "NONE"
    return sdk.OpenSecTradeContext(
        filter_trdmarket=sdk.TrdMarket.NONE,
        host=where["host"],
        port=where["port"],
        security_firm=getattr(sdk.SecurityFirm, name),
    )


def close(context):
    try:
        context.close()
    except Exception:  # noqa: BLE001 - a failed close must not lose a read that already succeeded
        pass


def answered(ret, data, what):
    """One SDK reply. A read is never retried here: OpenD is local, so a failure is a closed or busy gateway, and
    the rate limit on a refreshed read makes a blind second attempt the wrong move."""
    if ret != sdk.RET_OK:
        raise RuntimeError(f"moomoo: {what} failed: {data}")
    return data


def signed_in(context):
    ret, state = context.get_global_state()
    state = answered(ret, state, "asking OpenD for its state")
    if not state.get("trd_logined"):
        raise ValueError("OpenD is running but not logged in for trading; log in in the OpenD window and try again.")
    return state


def accounts_of(where):
    """Every account OpenD will show. The brokerage is part of the connection, so with none configured each is tried
    and the ones that are not yours simply return nothing."""
    found, seen = [], set()
    for name in [where["security_firm"]] if where["security_firm"] else FIRMS:
        context = connect(where, name)
        try:
            signed_in(context)
            ret, data = context.get_acc_list()
            if ret != sdk.RET_OK:
                continue
            for row in rows(data):
                acc_id = identifier(row.get("acc_id"))
                if not acc_id or acc_id in seen:
                    continue
                seen.add(acc_id)
                found.append(
                    {
                        "acc_id": acc_id,
                        "security_firm": name,
                        "trd_env": str(row.get("trd_env") or ""),
                        "acc_type": str(row.get("acc_type") or ""),
                        "acc_role": str(row.get("acc_role") or ""),
                        "uni_card_num": str(row.get("uni_card_num") or "N/A"),
                        "trdmarket_auth": list(row.get("trdmarket_auth") or []),
                    }
                )
        finally:
            close(context)
    return found


def resolve(where):
    """Which account a run reads. `acc_id` in the skill's options pins it; with none set, one account in this
    environment is used and several is an error, because reading whichever came back first would silently answer
    about the wrong portfolio."""
    found = accounts_of(where)
    if where["acc_id"]:
        for account in found:
            if account["acc_id"] == where["acc_id"]:
                return account
        raise ValueError(
            f"moomoo: OpenD shows no account {where['acc_id']}; `jev list my moomoo accounts` lists yours."
        )
    matching = [a for a in found if a["trd_env"] == where["trd_env"] and a["acc_role"] != "MASTER"]
    if not matching:
        raise ValueError(f"moomoo: OpenD shows no {where['trd_env']} securities account.")
    if len(matching) > 1:
        listed = ", ".join(str(a["acc_id"]) for a in matching)
        raise ValueError(
            f"moomoo: {len(matching)} {where['trd_env']} accounts ({listed}). Set acc_id in "
            "skills/moomoo/_branch.toml so every run reads the same one."
        )
    return matching[0]


def holding(row):
    """One position in the fields the moomoo app shows."""
    return {
        "code": str(row.get("code") or ""),
        "name": str(row.get("stock_name") or ""),
        "currency": str(row.get("currency") or ""),
        "quantity": number(row.get("qty")),
        "sellable": number(row.get("can_sell_qty")),
        "cost": money(row.get("average_cost")),
        "price": money(row.get("nominal_price")),
        "value": money(row.get("market_val")),
        "unrealized": money(row.get("unrealized_pl")),
        "percent": round(number(row.get("pl_ratio_avg_cost")), 2),
        "realized": money(row.get("realized_pl")),
        "today": money(row.get("today_pl_val")),
    }


def funds_of(row, asked):
    """The account totals. `available_funds` is N/A on some account types, where moomoo's own documented fallback is
    total assets less initial margin."""
    total, margin = money(row.get("total_assets")), money(row.get("initial_margin"))
    available = row.get("available_funds")
    if str(available) in {"N/A", "None", ""} or available is None:
        available = total - margin if margin > 0 else total
    reported = str(row.get("currency") or "")
    return {
        # Every amount below is in this currency, whether the account converted to the one asked for or ignored it.
        "currency": reported if reported and reported != "N/A" else asked,
        "total": total,
        "cash": money(row.get("cash")),
        "value": money(row.get("market_val")),
        "available": money(available),
        "power": money(row.get("power")),
        "risk": str(row.get("risk_status") or "N/A"),
    }


def positions_of(context, where, account):
    """The account's positions. `refresh_cache` asks OpenD for fresh numbers instead of its cache, which is what
    makes them match the app; it is limited to 10 reads per 30 s per account, so one run reads once."""
    ret, data = context.position_list_query(
        trd_env=getattr(sdk.TrdEnv, where["trd_env"]), acc_id=account["acc_id"], refresh_cache=True
    )
    data = answered(ret, data, "reading positions")
    if data is not None and hasattr(data, "columns") and len(data):
        if missing := [field for field in NEEDED if field not in set(data.columns)]:
            raise RuntimeError(f"moomoo: OpenD left out {', '.join(missing)}; the SDK may need upgrading.")
    return [holding(row) for row in rows(data)]


def portfolio(where, account):
    """Funds and positions, over one connection. Read-only: both are queries."""
    context = connect(where, account["security_firm"])
    try:
        signed_in(context)
        ret, data = context.accinfo_query(
            trd_env=getattr(sdk.TrdEnv, where["trd_env"]),
            acc_id=account["acc_id"],
            refresh_cache=True,
            currency=where["currency"],
        )
        found = rows(answered(ret, data, "reading account funds"))
        funds = funds_of(found[0], where["currency"]) if found else {}
        return funds, positions_of(context, where, account)
    finally:
        close(context)


def by_currency(positions):
    """Positions grouped by their own currency. Amounts in different currencies are never added: an account total
    comes from the funds query, which states the one currency it is in."""
    groups = {}
    for position in positions:
        groups.setdefault(position["currency"] or "?", []).append(position)
    return groups


def judge(skill, details, questions, **state):
    """One TypeSafe request over the details. Returns (answers, call info); no questions means no request."""
    if not questions:
        return {}, None
    rules = {"skill_rules": list(skill.rules)} if skill.rules else {}
    body = {
        "model": config.get("typesafe_model"),
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


def ask(skill, details, where, schema, instructions, stop_if_missing=True, **context):
    """One text-model call for the free text and dates an operation needs.

    With `stop_if_missing` false, a question the model raises is ignored: an operation that has a default and says
    when it used one can answer that question itself, and asking the model not to ask is a prompt fight code wins
    by not reading the answer."""
    console.check()  # a safe point: before a model call, and nothing here ever changes anything anyway
    now = datetime.now(where["zone"])
    request = {
        "task": skill.task,
        "details": details,
        "instructions": instructions,
        "now": now.isoformat(timespec="minutes"),
        "weekday": f"{now:%A}",
        "timezone": str(getattr(where["zone"], "key", where["zone"])),
        **({"skill_rules": list(skill.rules)} if skill.rules else {}),
        **context,
    }
    output, info = complete_json(MOOMOO_ARGUMENTS, request, schema, "moomoo skills")
    if output is None or not conform(output, schema):
        raise ValueError("The text model's arguments did not match the operation; nothing was read.")
    if output.get("missing") and stop_if_missing:
        # Answerable in words, so it is kept for one follow-up rather than simply failing.
        raise Unanswered(output["missing"], f"Need more detail: {output['missing']}")
    return output, info


def answer_question():
    return {
        "answer": {
            "type": "noul",
            "instructions": {"question": HOLDINGS_ANSWER_NEEDED},
            "criteria": HOLDINGS_ANSWER_NEEDED_CRITERIA,
        }
    }


def answer(skill, details, funds, positions):
    """The text model's answer to the question, worked out only from the holdings read. Returns (text or None, info).
    It is shown and recorded, never acted on."""
    console.check()  # a safe point: the read is done
    context = {
        "question": details,
        "funds": funds,
        "holdings": positions,
        "currencies": sorted(by_currency(positions)),
        **({"skill_rules": list(skill.rules)} if skill.rules else {}),
    }
    output, info = complete_json(HOLDINGS_ANSWER, context, ANSWER_SCHEMA, "Answering a moomoo holdings question")
    text = output.get("answer") if output and conform(output, ANSWER_SCHEMA) else None
    if not isinstance(text, str) or not text.strip() or len(text) > ANSWER_LENGTH:
        return None, info
    return text.strip(), info


def show_funds(funds):
    if not funds:
        return
    console.say(
        f"  {funds['currency']}  total {funds['total']:,.2f}   positions {funds['value']:,.2f}   "
        f"cash {funds['cash']:,.2f}   available {funds['available']:,.2f}   risk {funds['risk']}"
    )


def show(positions):
    """The holdings table. Every amount is that position's own currency, so a mixed account gets a block each."""
    groups = by_currency(positions)
    for currency, held in groups.items():
        console.say(
            f"\n  {currency:<6}{'code':<11}{'name':<17}{'qty':>8}{'cost':>11}{'price':>11}"
            f"{'value':>13}{'P/L':>12}{'P/L %':>9}"
        )
        for p in sorted(held, key=lambda p: p["value"], reverse=True):
            console.say(
                f"  {'':<6}{p['code']:<11}{p['name'][:16]:<17}{p['quantity']:>8,.0f}{p['cost']:>11,.2f}"
                f"{p['price']:>11,.2f}{p['value']:>13,.2f}{p['unrealized']:>+12,.2f}{p['percent']:>8,.2f}%"
            )
        # A subtotal is only ever within one currency; the account's own total is in the funds line above.
        console.say(
            f"  {'':<6}{'subtotal':<11}{'':<17}{'':>8}{'':>11}{'':>11}"
            f"{sum(p['value'] for p in held):>13,.2f}{sum(p['unrealized'] for p in held):>+12,.2f}"
        )
    if len(groups) > 1:
        console.say("\n  (subtotals are per currency; the account total above is the one converted figure)")


def holdings(skill, details, confirm):
    """Everything held in the account, and a written answer when the request needs working out rather than a list."""
    where = settings(skill)
    account = resolve(where)
    questions = answer_question()
    answers, judged = judge(skill, details, questions)
    needed = validate_noul(answers.get("answer", {}))
    console.check()  # the judgment is back; nothing has been read yet
    funds, positions = portfolio(where, account)
    record = {
        "account": {k: account[k] for k in ACCOUNT_KEYS},
        "funds": funds,
        "holdings": positions,
        "judgments": [judged] if judged else [],
        "answer_needed": round(needed, 3),
    }
    console.say(f"  account {account['acc_id']} ({account['trd_env']}, {account['security_firm']})")
    show_funds(funds)
    if not positions:
        console.say("  No positions held.")
        return {"status": "done", **record}
    if needed > SURE:
        # The question needs reasoning over the holdings, not just the holdings: the last mile is the text model's.
        try:
            text, info = answer(skill, details, funds, positions)
            record["answer"] = {"text": text, "model_call": info}
        except (ValueError, RuntimeError) as error:
            text, record["answer"] = None, {"error": str(error)}
        console.say(
            f"  → {text}"
            if text
            else f"  no answer: {record['answer'].get('error', 'the holdings could not answer it')}"
        )
    show(positions)
    return {"status": "done", **record}


def position(skill, details, confirm):
    """One holding. The candidates are the rows read from the account, and TypeSafe picks among them; no model ever
    names an instrument."""
    where = settings(skill)
    account = resolve(where)
    funds, positions = portfolio(where, account)
    if not positions:
        raise ValueError("Nothing is held in this account, so there is no position to show.")
    criteria = {
        str(i): {
            "code": p["code"],
            "name": p["name"],
            "held": f"{p['quantity']:g} at {p['cost']:g} {p['currency']}",
        }
        for i, p in enumerate(positions, 1)
    }
    criteria[NONE] = "None of these holdings is the one the request refers to."
    questions = {"holding": {"type": "choice", "instructions": {"question": HOLDING}, "criteria": criteria}}
    console.check()  # a safe point: read, and nothing changed
    answers, judged = judge(skill, details, questions)
    chosen = validate_choice(answers.get("holding", {}), criteria)
    choice, probabilities = chosen["choice"], chosen["probabilities"]
    record = {
        "account": {k: account[k] for k in ACCOUNT_KEYS},
        "funds": funds,
        "judgments": [judged] if judged else [],
    }
    if choice == NONE:
        raise Unanswered("None of your holdings is the one you mean; nothing was shown.")
    if probabilities[choice] < SURE:
        likely = [k for k in sorted(probabilities, key=probabilities.get, reverse=True) if k != NONE][:2]
        options = " or ".join(f"{criteria[k]['code']} ({criteria[k]['name']})" for k in likely)
        raise Unanswered(f"Not sure which holding you mean: {options}. Say which one.")
    held = positions[int(choice) - 1]
    console.say(f"  account {account['acc_id']} ({account['trd_env']}, {account['security_firm']})  match: "
                f"{probabilities[choice]:.0%}")
    console.say(f"\n  {held['code']}  {held['name']}  ({held['currency']})")
    console.say(f"    quantity   {held['quantity']:,.0f}   ({held['sellable']:,.0f} sellable)")
    console.say(f"    cost       {held['cost']:,.2f}   average cost, as the app shows it")
    console.say(f"    price      {held['price']:,.2f}")
    console.say(f"    value      {held['value']:,.2f}")
    console.say(f"    unrealized {held['unrealized']:+,.2f}   ({held['percent']:+,.2f}%)")
    console.say(f"    realized   {held['realized']:+,.2f}")
    console.say(f"    today      {held['today']:+,.2f}")
    return {"status": "done", **record, "holding": held, "match": round(probabilities[choice], 3)}


def span(args, zone):
    """The range to read, as (start, end, default?). The text model writes a date only where the details name one;
    code checks the shape and the order and fills in what is missing. `default?` is whether code chose how far back
    to look, which is what an unnamed start means: the model routinely fills the end in with today, and today is not
    something the person asked for. A missing start is anchored to the end rather than to today, so naming only an
    end cannot invert the range."""
    today = datetime.now(zone).date()
    named = {}
    for key in ("start", "end"):
        value = (args.get(key) or "").strip()
        if value and not DAY.fullmatch(value):
            raise ValueError(f"The text model's {key} date {value!r} is not YYYY-MM-DD; nothing was read.")
        named[key] = value or None
    last = date.fromisoformat(named["end"]) if named["end"] else today
    first = date.fromisoformat(named["start"]) if named["start"] else last - timedelta(days=ACTIVITY_DAYS)
    if first > last:
        raise ValueError(f"That range ends before it starts ({first} to {last}); nothing was read.")
    return first.isoformat(), last.isoformat(), named["start"] is None


def deal(row):
    return {
        "code": str(row.get("code") or ""),
        "name": str(row.get("stock_name") or ""),
        "side": str(row.get("trd_side") or ""),
        "quantity": number(row.get("qty")),
        "price": money(row.get("price")),
        "when": str(row.get("create_time") or ""),
    }


def order(row):
    return {
        "code": str(row.get("code") or ""),
        "name": str(row.get("stock_name") or ""),
        "side": str(row.get("trd_side") or ""),
        "status": str(row.get("order_status") or ""),
        "quantity": number(row.get("qty")),
        "filled": number(row.get("dealt_qty")),
        "price": money(row.get("price")),
        "when": str(row.get("create_time") or ""),
    }


def activity(skill, details, confirm):
    """Fills and orders over a date range the text model reads out of the request."""
    where = settings(skill)
    account = resolve(where)
    args, call = ask(
        skill,
        details,
        where,
        RANGE,
        "Give the date range of account activity the details name. Use null for any bound they do not name. "
        "Do not ask for a range: when none is named, code reads the last month.",
        stop_if_missing=False,
    )
    start, end, default = span(args, where["zone"])
    console.check()  # a safe point: the range is set and nothing has been read
    context = connect(where, account["security_firm"])
    try:
        signed_in(context)
        env = getattr(sdk.TrdEnv, where["trd_env"])
        ret, data = context.history_deal_list_query(start=start, end=end, trd_env=env, acc_id=account["acc_id"])
        fills = [deal(row) for row in rows(answered(ret, data, "reading fills"))]
        ret, data = context.history_order_list_query(start=start, end=end, trd_env=env, acc_id=account["acc_id"])
        orders = [order(row) for row in rows(answered(ret, data, "reading orders"))]
    finally:
        close(context)
    limit = where["max_results"]
    record = {
        "account": {k: account[k] for k in ACCOUNT_KEYS},
        "searched": {"from": start, "to": end, "default": default},
        "model_calls": [call],
        "fills": fills,
        "orders": orders,
    }
    console.say(f"  account {account['acc_id']}  {start} to {end}")
    if default:
        # Said out loud, and offered back: the next request may replace this window instead of starting over.
        record["assumed"] = (
            f"No start date was given, so I read the last month, {start} to {end}. Say another range to change it."
        )
        console.say("  (no start date given, so the last month above; say another range to change it)")
    console.say(f"\n  {len(fills)} fill(s)" + (f", showing {limit}" if len(fills) > limit else ""))
    for f in fills[:limit]:
        console.say(f"    {f['when'][:16]:<17}{f['side']:<5}{f['code']:<11}{f['quantity']:>8,.0f} @ {f['price']:,.2f}")
    console.say(f"\n  {len(orders)} order(s)" + (f", showing {limit}" if len(orders) > limit else ""))
    for o in orders[:limit]:
        console.say(
            f"    {o['when'][:16]:<17}{o['side']:<5}{o['code']:<11}{o['quantity']:>8,.0f} @ {o['price']:,.2f}  "
            f"{o['status']}"
        )
    if not fills and not orders:
        console.say("  Nothing in that range.")
    return {"status": "done", **record}


def accounts(skill, details, confirm):
    """The accounts OpenD will show, with the ids a skill's `acc_id` option takes."""
    where = settings(skill)
    found = accounts_of(where)
    for account in found:
        console.say(
            f"  {account['acc_id']:<20}{account['trd_env']:<10}{account['acc_type']:<18}"
            f"{account['security_firm']:<16}card {account['uni_card_num']:<18}"
            f"{','.join(account['trdmarket_auth'])}"
        )
    if not found:
        console.say("  OpenD shows no accounts.")
    return {"status": "done", "accounts": found}


OPERATIONS = {
    "accounts": accounts,
    "holdings": holdings,
    "position": position,
    "activity": activity,
}
