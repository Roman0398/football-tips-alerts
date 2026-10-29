"""Run the existing monitor, verify OLBG settlements, build stats and monthly reports."""

import json
import math
import os
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from bs4 import BeautifulSoup
import monitor as m


SETTLED = {"win", "loss", "push", "half_win", "half_loss"}
RESULTS = {
    "win": "win",
    "lose": "loss",
    "loss": "loss",
    "void": "push",
    "push": "push",
    "win+push": "half_win",
    "lose+push": "half_loss",
}
INTERVAL = timedelta(minutes=10)


def stamp(value):
    try:
        dt = datetime.fromisoformat(str(value))
        if dt.tzinfo is None:
            return dt.replace(tzinfo=m.KYIV)
        return dt.astimezone(m.KYIV)
    except (TypeError, ValueError):
        return None


def finite(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def month_shift(dt, count):
    index = dt.year * 12 + dt.month - 1 + count
    return dt.replace(
        year=index // 12,
        month=index % 12 + 1,
        day=1,
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )


def history_url(name, page=1):
    base = "https://www.olbg.com/tipsters/tips/history/" + m.PROFILES[name]
    return base if page == 1 else base + "?page=" + str(page)


def history_data(name, html):
    routes = []

    for node in BeautifulSoup(html, "html.parser").find_all("script"):
        text = node.string or node.get_text()

        if "tipsByDate" not in text or "effectiveTipsterId" not in text:
            continue

        for match in m.ROUTE.finditer(text):
            try:
                value, _ = m.literal(text, match.end())
            except ValueError:
                continue

            if (
                isinstance(value, dict)
                and "effectiveTipsterId" in value
                and "tipsByDate" in value
            ):
                routes.append(value)

    if len(routes) != 1:
        raise m.Unverified("History payload is missing or ambiguous")

    data = routes[0]

    if (
        data.get("mode") != "history"
        or str(data.get("effectiveTipsterId")) != m.PROFILES[name]
        or m.norm(data.get("tipsterUsername", "")) != m.norm(name)
    ):
        raise m.Unverified("History identity/mode mismatch")

    if (
        not isinstance(data.get("tipsByDate"), list)
        or type(data.get("hasTips")) is not bool
    ):
        raise m.Unverified("Invalid history list")

    rows = []

    for group in data["tipsByDate"]:
        try:
            day = datetime.strptime(
                group["date"], "%d %B %Y"
            ).date().isoformat()
            events = group["events"]
        except (TypeError, KeyError, ValueError):
            raise m.Unverified("Invalid history date group") from None

        if not isinstance(events, list):
            raise m.Unverified("Invalid history events")

        for event in events:
            if (
                not isinstance(event, dict)
                or not isinstance(event.get("tips"), list)
            ):
                raise m.Unverified("Invalid history event")

            for row in event["tips"]:
                if (
                    not isinstance(row, dict)
                    or not str(row.get("tipid", "")).isdigit()
                ):
                    raise m.Unverified("Invalid history tip ID")

                rows.append((day, row))

    if bool(rows) != data["hasTips"]:
        raise m.Unverified("History hasTips contradicts its rows")

    pagination = data.get("pagination", {})
    pages = pagination.get("totalPages")
    more = pagination.get("hasMore")

    if (
        type(pages) is not int
        or not 0 <= pages <= 100
        or type(more) is not bool
    ):
        raise m.Unverified("Invalid history pagination")

    return rows, pages, more


class HistoryReader(m.Reader):
    def page(self, name, index):
        address = history_url(name, index)

        try:
            return history_data(name, m.fetch_html(address))
        except Exception:
            print("HISTORY BROWSER FALLBACK", name)

        if self.browser is None:
            from playwright.sync_api import sync_playwright

            self.playwright = sync_playwright().start()
            self.browser = self.playwright.chromium.launch(headless=True)

        page = self.browser.new_page(locale="en-GB")

        try:
            response = page.goto(
                address,
                wait_until="domcontentloaded",
                timeout=35000,
            )

            if (
                response is None
                or response.status >= 400
                or page.url.split("?")[0] != address.split("?")[0]
            ):
                raise m.Unverified("History browser load failed")

            error = None

            for attempt in range(6):
                try:
                    return history_data(name, page.content())
                except m.Unverified as exc:
                    error = exc

                if attempt < 5:
                    page.wait_for_timeout(1200)

            raise error

        finally:
            page.close()


def verify_settlement(tip, day, row):
    """Match a recorded tip and use OLBG's combined EW profit."""

    if (
        day != tip.get("date")
        or str(row.get("tipid")) != tip.get("source_tip_id")
    ):
        raise m.Unverified("Settlement tip ID/date mismatch")

    event = str(row.get("eventname", "")).strip()

    if tip["author"] in m.HORSES:
        import re

        event = re.sub(r"^\d{1,2}:\d{2}\s+", "", event).strip()

    if (
        m.norm(event) != m.norm(tip.get("event", ""))
        or m.norm(row.get("selection", ""))
        != m.norm(tip.get("selection", ""))
        or str(row.get("vc_sport_id"))
        != str(m.SPORT_IDS[tip["author"]])
    ):
        raise m.Unverified("Settlement event/selection/sport mismatch")

    kind = m.norm(row.get("winew", "")).replace("/", "")
    expected_kind = m.norm(
        tip.get("bet_type", tip.get("market", ""))
    ).replace("/", "")

    if kind not in ("win", "ew") or kind != expected_kind:
        raise m.Unverified("Settlement bet type mismatch")

    if (
        tip.get("market_olbg") is not None
        and m.norm(row.get("market", ""))
        != m.norm(tip["market_olbg"])
    ):
        raise m.Unverified("Settlement market mismatch")

    raw_result = m.norm(row.get("result", ""))

    if raw_result in ("open", "pending"):
        return False

    result = RESULTS.get(raw_result)

    if result is None:
        raise m.Unverified("Unsupported OLBG settlement label")

    stake = m.number(row.get("stake"))
    source_odds = m.parse_odds(row.get("odds"))
    profit = finite(row.get("profit"))
    reason = None

    if stake is None or source_odds is None or profit is None:
        reason = "OLBG settlement stake, odds or profit missing"

    stored_stake = m.number(tip.get("stake_points"))

    if (
        stake is not None
        and stored_stake is not None
        and not math.isclose(stake, stored_stake, abs_tol=1e-8)
    ):
        reason = "OLBG history stake differs from the recorded stake"

    stored_odds = m.parse_odds(tip.get("odds_olbg"))

    if (
        stored_odds is not None
        and source_odds is not None
        and not math.isclose(stored_odds, source_odds, abs_tol=1e-6)
    ):
        reason = "OLBG history odds differ from the recorded odds"

    total_stake = (
        stake * (2 if kind == "ew" else 1)
        if stake is not None
        else None
    )

    if (
        profit is not None
        and total_stake is not None
        and profit < -total_stake - 1
    ):
        reason = "OLBG profit contradicts the stake"

    if result == "push" and profit not in (0, None):
        reason = "OLBG void result has nonzero profit"

    tip.update(
        result=result,
        result_olbg=row.get("result"),
        settlement_verified=True,
        settled_at=tip.get("settled_at") or m.now(),
        settlement_checked_at=m.now(),
        result_source=history_url(tip["author"]),
        stake_total_points=total_stake,
        profit_points=profit if reason is None else None,
    )

    tip["olbg_settlement"] = {
        "source_tip_id": str(row["tipid"]),
        "result": row.get("result"),
        "odds": source_odds,
        "stake_per_leg": stake,
        "total_stake": total_stake,
        "profit": profit,
        "financial_verified": reason is None,
        "issue": reason,
    }

    if stored_odds is None and source_odds is not None:
        tip["odds_olbg"] = source_odds
        tip["odds_confirmed_from_history_at"] = m.now()

    if stored_stake is None and stake is not None:
        tip["stake_points"] = stake

    if kind == "ew":
        tip["each_way_settlement"] = "OLBG combined win/place profit"

    return True


def settle(state, clock, reader=None):
    checks = state.setdefault("result_checks", {})
    reader = reader or HistoryReader()
    failed = False
    changed = 0

    try:
        for name in m.NAMES:
            candidates = [
                tip
                for tip in state["tips"].values()
                if tip.get("author") == name
                and tip.get("source_tip_id")
                and tip.get("date", "") <= clock.date().isoformat()
                and (
                    tip.get("result", "pending") not in SETTLED
                    or tip.get("date", "")
                    >= (clock - timedelta(days=7)).date().isoformat()
                )
            ]

            if not candidates:
                continue

            previous = stamp(checks.get(name, {}).get("checked_at"))

            if previous and timedelta(0) <= clock - previous < INTERVAL:
                continue

            wanted = {
                str(tip["source_tip_id"]): tip
                for tip in candidates
            }
            oldest = min(tip["date"] for tip in candidates)
            seen = set()
            matched = 0
            pages_read = 0

            try:
                declared = None

                for index in range(1, 21):
                    rows, pages, more = reader.page(name, index)
                    signature = tuple(
                        str(row["tipid"]) for _, row in rows
                    )

                    if signature in seen or (
                        declared is not None and declared != pages
                    ):
                        raise m.Unverified(
                            "History pagination repeated or changed"
                        )

                    seen.add(signature)
                    declared = pages
                    pages_read = index

                    if more != (index < pages):
                        raise m.Unverified(
                            "History pagination metadata disagrees"
                        )

                    for day, row in rows:
                        key = str(row["tipid"])

                        if key in wanted:
                            if verify_settlement(wanted[key], day, row):
                                matched += 1
                            del wanted[key]

                    if (
                        not wanted
                        or not more
                        or (
                            rows
                            and min(day for day, _ in rows) < oldest
                        )
                    ):
                        break

                else:
                    raise m.Unverified("History page limit reached")

                checks[name] = {
                    "status": "verified",
                    "checked_at": clock.isoformat(timespec="seconds"),
                    "matched": matched,
                    "not_found": len(wanted),
                    "pages": pages_read,
                }

                changed += matched

                print(
                    "RESULTS VERIFIED",
                    name,
                    matched,
                    "matched,",
                    len(wanted),
                    "not found",
                )

            except Exception as exc:
                checks[name] = {
                    "status": "unverified",
                    "checked_at": clock.isoformat(timespec="seconds"),
                    "details": type(exc).__name__ + ": " + str(exc)[:160],
                }
                failed = True
                print(
                    "RESULTS UNVERIFIED",
                    name,
                    checks[name]["details"],
                )

    finally:
        reader.close()

    return changed, failed


def metrics(items):
    counts = Counter(
        tip.get("result", "pending") for tip in items
    )
    settled = sum(counts[key] for key in SETTLED)

    valid = [
        tip
        for tip in items
        if tip.get("result") in SETTLED
        and tip.get("settlement_verified") is True
        and tip.get("olbg_settlement", {}).get("financial_verified") is True
        and finite(tip.get("profit_points")) is not None
        and m.number(tip.get("stake_total_points")) is not None
    ]

    stake = sum(finite(tip["stake_total_points"]) for tip in valid)
    profit = sum(finite(tip["profit_points"]) for tip in valid)

    return {
        "total": len(items),
        "settled": settled,
        "pending": len(items) - settled,
        "win": counts["win"],
        "loss": counts["loss"],
        "push": counts["push"],
        "half_win": counts["half_win"],
        "half_loss": counts["half_loss"],
        "ew": sum(tip.get("bet_type") == "EW" for tip in items),
        "counted": len(valid),
        "stake": round(stake, 6),
        "profit": round(profit, 6) if valid else None,
        "roi": round(profit / stake * 100, 6) if stake else None,
    }


def summary(items, label):
    return {
        "label": label,
        "total": metrics(items),
        "cappers": {
            name: metrics(
                [tip for tip in items if tip.get("author") == name]
            )
            for name in m.NAMES
        },
        "sports": {
            m.SPORTS[name]: metrics(
                [
                    tip
                    for tip in items
                    if tip.get("sport") == m.SPORTS[name]
                ]
            )
            for name in m.NAMES
        },
    }


def build_reporting(state, clock):
    items = list(state["tips"].values())
    start = month_shift(clock, 0)

    def select(begin, end):
        return [
            tip
            for tip in items
            if (dt := stamp(tip.get("first_seen_at"))) is not None
            and (begin is None or dt >= begin)
            and (end is None or dt < end)
        ]

    periods = {
        "month": summary(
            select(start, month_shift(start, 1)),
            clock.strftime("%m.%Y"),
        ),
        "last30": summary(
            select(
                clock - timedelta(days=30),
                clock + timedelta(seconds=1),
            ),
            "30 днів",
        ),
        "alltime": summary(items, "Весь час"),
    }

    for size in (3, 6, 12):
        periods["last" + str(size)] = summary(
            select(
                month_shift(start, 1 - size),
                month_shift(start, 1),
            ),
            str(size) + " місяців",
        )

    months = sorted(
        {
            dt.strftime("%Y-%m")
            for tip in items
            if (dt := stamp(tip.get("first_seen_at"))) is not None
        }
    )

    monthly = {}

    for period in months:
        begin = datetime.strptime(period, "%Y-%m").replace(
            tzinfo=m.KYIV
        )
        monthly[period] = summary(
            select(begin, month_shift(begin, 1)),
            period,
        )

    first = min(
        (
            stamp(tip.get("first_seen_at"))
            for tip in items
            if stamp(tip.get("first_seen_at"))
        ),
        default=clock,
    )

    state.setdefault(
        "monitoring_started_at",
        first.isoformat(timespec="seconds"),
    )

    state["reporting"] = {
        "schema": 1,
        "generated_at": clock.isoformat(timespec="seconds"),
        "since": state["monitoring_started_at"],
        "periods": periods,
        "months": monthly,
        "basis": (
            "OLBG published odds, official settled profit "
            "and total stake; EW has two legs"
        ),
    }

    state["current_tip_ids"] = [
        key
        for key, tip in state["tips"].items()
        if (
            source := state["sources"].get(tip.get("author"), {})
        ).get("status") == "verified"
        and (last := stamp(tip.get("last_seen_at"))) is not None
        and (
            checked := stamp(source.get("last_success_at"))
        ) is not None
        and last >= checked
    ]


def metric_text(value):
    profit = (
        "н/д"
        if value["profit"] is None
        else f"{value['profit']:+.2f} пт"
    )
    roi = (
        "н/д"
        if value["roi"] is None
        else f"{value['roi']:+.2f}%"
    )

    return (
        f"Прогнозів: {value['total']} | "
        f"Розраховано: {value['settled']} | "
        f"Очікують: {value['pending']}\n"
        f"Прибуток: {profit} | ROI: {roi}\n"
        f"У розрахунку ROI: {value['counted']}"
    )


def monthly_text(state, period):
    report = state["reporting"]["months"].get(period)

    if report is None:
        report = summary([], period)

    lines = [
        "📊 FOOTBALL TIPS ALERTS",
        "МІСЯЧНИЙ ЗВІТ — " + period,
        "Дані спостереження від "
        + state["monitoring_started_at"][:10],
    ]

    for name in m.NAMES:
        lines += [
            "",
            "👤 " + name,
            metric_text(report["cappers"][name]),
        ]

    lines += [
        "",
        "🏆 ЗАГАЛОМ",
        metric_text(report["total"]),
        "",
        "За видами спорту:",
    ]

    for sport, value in report["sports"].items():
        profit = (
            "н/д"
            if value["profit"] is None
            else f"{value['profit']:+.2f} пт"
        )
        roi = (
            "н/д"
            if value["roi"] is None
            else f"{value['roi']:+.2f}%"
        )

        lines.append(
            f"{sport}: {value['total']} прогнозів | "
            f"{profit} | ROI {roi}"
        )

    lines += [
        "",
        "Коефіцієнти й розрахунок OLBG. "
        "EW враховано обома частинами.",
        "ROI = прибуток / сума ставок за "
        "підтвердженими розрахунками × 100%.",
        "Період — за часом першого виявлення "
        "прогнозу ботом. Непідтверджені дані — н/д.",
    ]

    message = "\n".join(lines)

    if len(message.encode("utf-16-le")) // 2 > 4096:
        raise ValueError(
            "Monthly report exceeds Telegram's message limit"
        )

    return message


def maybe_monthly(
    state,
    clock,
    sender=m.telegram,
    saver=m.save,
):
    start = month_shift(clock, 0)

    if clock < start.replace(hour=10):
        return False

    period = month_shift(start, -1).strftime("%Y-%m")
    ledger = state.setdefault("monthly_reports", {})
    prior = ledger.get(period, {})

    if prior.get("status") in (
        "sent",
        "sending",
        "unknown",
        "no_coverage",
    ):
        return prior.get("status") in ("sending", "unknown")

    legacy = Path(".state/monthly-v2.json")

    if (
        legacy.exists()
        and json.loads(legacy.read_text()).get("sent_period") == period
    ):
        ledger[period] = {
            "status": "sent",
            "migrated_from_legacy_ledger": True,
        }
        saver(state)
        return False

    since = stamp(state["monitoring_started_at"])

    if since and since >= start:
        ledger[period] = {"status": "no_coverage"}
        saver(state)
        return False

    message = monthly_text(state, period)

    ledger[period] = {
        "status": "sending",
        "attempt_at": m.now(),
    }
    saver(state)

    try:
        message_id = sender(message)

    except m.DeliveryRejected as exc:
        ledger[period].update(
            status="pending",
            error=str(exc),
        )
        saver(state)
        print("MONTHLY REJECTED", period)
        return True

    except m.DeliveryUnknown:
        ledger[period]["status"] = "unknown"
        saver(state)
        print("MONTHLY DELIVERY UNKNOWN", period)
        return True

    ledger[period].update(
        status="sent",
        sent_at=m.now(),
        telegram_message_id=message_id,
    )
    saver(state)
    print("MONTHLY SENT", period)
    return False


def main():
    if os.environ.get("RUN_SELFTEST") == "1":
        m.selftest()
        assert metrics([])["roi"] is None
        print("REPORT SELFTEST OK")
        return 0

    status = m.main()
    state = m.load()
    clock = datetime.now(m.KYIV)

    updated, errors = settle(state, clock)
    build_reporting(state, clock)

    if os.environ.get("DRY_RUN") == "1":
        print(
            "DRY RUN",
            updated,
            "settlements; statistics built; no writes or messages",
        )
        return int(bool(status or errors))

    m.save(state)
    monthly_error = maybe_monthly(state, clock)

    print(
        "READY",
        updated,
        "confirmed results; OLBG statistics and menu data updated",
    )

    return int(bool(status or errors or monthly_error))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        message = str(exc)

        for secret in (
            os.environ.get("BOT_TOKEN"),
            os.environ.get("CHAT_ID"),
        ):
            if secret:
                message = message.replace(secret, "[REDACTED]")

        print("FATAL", type(exc).__name__, message[:250])
        sys.exit(1)
