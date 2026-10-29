
import hashlib
import json
import os
import re
import subprocess
import urllib.parse
import urllib.request

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright


KYIV = ZoneInfo("Europe/Kyiv")
STATE = Path(".state/tips-v2.json")

PROFILES = {
    "Knottlast": "666034",
    "Geobetting": "420769",
}

NAMES = [
    "Knottlast",
    "Niffler",
    "dacris69",
    "risto123",
    "Geobetting",
    "mojojak",
]

SPORTS = {
    "Knottlast": "🐎 Перегони",
    "Niffler": "🐎 Перегони",
    "dacris69": "🎱 Снукер",
    "risto123": "⚾ Бейсбол",
    "Geobetting": "⚽ Футбол",
    "mojojak": "⛳ Гольф",
}

HORSES = {"Knottlast", "Niffler"}

MARKETS = {
    "result",
    "full time result",
    "both teams to score",
    "total goals",
    "correct score",
    "asian hcap",
    "result and btts",
    "total corners",
    "total cards",
    "half time result",
    "half time/full time",
    "anytime goalscorer",
    "first goalscorer",
    "match winner",
    "winner",
    "outright winner",
    "each way",
    "to win",
}

DATE = re.compile(
    r"^\d{1,2}\s+[A-Za-z]+\s+20\d{2}$",
    re.I,
)

FIXTURE = re.compile(
    r"^(?:(\d{1,2}:\d{2})\s+)?"
    r"(.+?)\s+(?:v|vs)\s+(.+)$",
    re.I,
)

RACE = re.compile(
    r"^(\d{1,2}:\d{2})"
    r"(?:\s+\d{1,2}:\d{2})?"
    r"\s+(.+)$"
)

STAKE = re.compile(
    r"^(\d+(?:[.,]\d+)?)\s*"
    r"(EW|E/W|EACH WAY|WIN)$",
    re.I,
)

ODDS = re.compile(
    r"^(?:@\s*|odds\s*[: ]\s*)"
    r"(\d+(?:[.,]\d+)?(?:/\d+(?:[.,]\d+)?)?)$",
    re.I,
)

STOP_PHRASES = (
    "tips are normally settled",
    "tips history",
    "tip history",
    "historical tips",
)

EMPTY_PHRASES = (
    "has no current tips",
    "no current tips",
    "no tips currently",
)

INVALID_SELECTIONS = {
    "view tip",
    "view tips",
    "read more",
    "show more",
    "place bet",
    "bet now",
    "share",
    "login",
    "sign up",
}


def now():
    return datetime.now(KYIV).isoformat(
        timespec="seconds"
    )


def digest(*values):
    raw = "|".join(map(str, values))
    return hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()


def load():
    if not STATE.exists():
        return {
            "tips": {},
            "sources": {},
            "seen": [],
            "schema": 2,
        }

    data = json.loads(
        STATE.read_text(encoding="utf-8")
    )

    if not isinstance(data, dict):
        raise ValueError(
            "Invalid state: expected object"
        )

    if not isinstance(data.get("tips", {}), dict):
        raise ValueError(
            "Invalid state: tips"
        )

    if not isinstance(
        data.get("sources", {}), dict
    ):
        raise ValueError(
            "Invalid state: sources"
        )

    data.setdefault("tips", {})
    data.setdefault("sources", {})
    data.setdefault("seen", [])
    data.setdefault("schema", 2)

    return data


def save(state):
    STATE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    content = json.dumps(
        state,
        ensure_ascii=False,
        indent=2,
    ) + "\n"

    if STATE.exists():
        previous = STATE.read_text(
            encoding="utf-8"
        )

        if previous == content:
            return

    STATE.write_text(
        content,
        encoding="utf-8",
    )

    subprocess.run(
        ["git", "add", str(STATE)],
        check=True,
    )

    changed = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        check=False,
    ).returncode

    if changed == 0:
        return

    if changed != 1:
        raise RuntimeError(
            "Unable to inspect git changes"
        )

    subprocess.run(
        [
            "git",
            "-c",
            "user.name=github-actions[bot]",
            "-c",
            "user.email="
            "41898282+github-actions[bot]"
            "@users.noreply.github.com",
            "commit",
            "-m",
            "Update OLBG tips v2",
        ],
        check=True,
    )

    subprocess.run(
        [
            "git",
            "pull",
            "--rebase",
            "origin",
            "main",
        ],
        check=True,
    )

    subprocess.run(
        [
            "git",
            "push",
            "origin",
            "HEAD:main",
        ],
        check=True,
    )


def telegram(message):
    token = os.environ["BOT_TOKEN"]
    chat = os.environ["CHAT_ID"]

    payload = urllib.parse.urlencode(
        {
            "chat_id": chat,
            "text": message[:4096],
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        "https://api.telegram.org/bot"
        + token
        + "/sendMessage",
        data=payload,
    )

    with urllib.request.urlopen(
        request,
        timeout=25,
    ) as response:
        result = json.load(response)

    if not result.get("ok"):
        raise RuntimeError(
            "Telegram rejected message"
        )


def body_from_html(html):
    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    for node in soup(
        ["script", "style", "noscript"]
    ):
        node.decompose()

    return soup.get_text(
        "\n",
        strip=True,
    )


def get_body(browser, address):
    try:
        request = urllib.request.Request(
            address,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept-Language":
                    "en-GB,en;q=0.9",
            },
        )

        with urllib.request.urlopen(
            request,
            timeout=18,
        ) as response:
            html = response.read(
                2500000
            ).decode(
                "utf-8",
                "replace",
            )

        body = body_from_html(html)

        if len(body) > 100 and (
            "'s tips" in body.lower()
            or "current tips" in body.lower()
        ):
            return body

    except Exception as exc:
        print(
            "HTTP FALLBACK",
            type(exc).__name__,
            str(exc)[:100],
        )

    page = browser.new_page(
        locale="en-GB"
    )

    try:
        response = page.goto(
            address,
            wait_until="domcontentloaded",
            timeout=30000,
        )

        if (
            response is not None
            and response.status >= 400
        ):
            raise ValueError(
                "HTTP " + str(response.status)
            )

        page.wait_for_timeout(2000)

        return page.locator(
            "body"
        ).inner_text(
            timeout=15000
        )

    finally:
        page.close()


def parse_odds(value):
    match = ODDS.fullmatch(
        value.strip()
    )

    if not match:
        return None

    raw = match.group(1).replace(
        ",",
        ".",
    )

    try:
        if "/" in raw:
            numerator, denominator = (
                raw.split("/", 1)
            )

            if float(denominator) <= 0:
                return None

            number = (
                1
                + float(numerator)
                / float(denominator)
            )
        else:
            number = float(raw)

        if 1.01 <= number <= 10000:
            return round(number, 4)

    except (ValueError, ZeroDivisionError):
        pass

    return None


def nearby_odds(lines, index):
    for value in lines[
        index + 1:index + 6
    ]:
        number = parse_odds(value)

        if number is not None:
            return number

        if (
            DATE.fullmatch(value)
            or FIXTURE.fullmatch(value)
            or STAKE.fullmatch(value)
        ):
            break

    return None


def valid_selection(value):
    value = value.strip()

    return (
        bool(value)
        and len(value) <= 110
        and value.casefold()
        not in INVALID_SELECTIONS
        and value.casefold()
        not in MARKETS
        and not DATE.fullmatch(value)
        and not STAKE.fullmatch(value)
        and not ODDS.fullmatch(value)
    )


def make_tip(
    name,
    day,
    event,
    time,
    market,
    selection,
    odds,
    stake=None,
):
    key = digest(
        name,
        day,
        event,
        market,
        selection,
    )

    tip = {
        "id": key,
        "author": name,
        "sport": SPORTS[name],
        "date": day,
        "time": time,
        "event": event,
        "market": market,
        "selection": selection,
        "odds_olbg": odds,
        "odds_available": None,
        "first_seen_at": now(),
        "result": "pending",
        "source": (
            "https://www.olbg.com/"
            "tipsters/tips/"
            + PROFILES[name]
        ),
    }

    if stake is not None:
        tip["stake_points"] = stake

    return tip


def extract(name, body):
    lines = [
        value.strip()
        for value in body.splitlines()
        if value.strip()
    ]

    heading = name.casefold() + "'s tips"

    start = next(
        (
            i
            for i, value in enumerate(lines)
            if heading in value.casefold()
            and "history"
            not in value.casefold()
        ),
        None,
    )

    if start is None:
        raise ValueError(
            "Current Tips section not found"
        )

    lines = lines[start + 1:]

    end = next(
        (
            i
            for i, value in enumerate(lines)
            if any(
                phrase in value.casefold()
                for phrase in STOP_PHRASES
            )
        ),
        len(lines),
    )

    lines = lines[:end]

    if any(
        phrase in value.casefold()
        for value in lines
        for phrase in EMPTY_PHRASES
    ):
        return []

    result = []
    day = None
    event = None
    event_time = None

    for i, value in enumerate(lines):
        if DATE.fullmatch(value):
            try:
                day = datetime.strptime(
                    value,
                    "%d %B %Y",
                ).date().isoformat()

            except ValueError:
                day = None

            event = None
            event_time = None
            continue

        fixture = FIXTURE.fullmatch(
            value
        )

        if day and fixture:
            event_time = fixture.group(1)
            event = (
                fixture.group(2).strip()
                + " v "
                + fixture.group(3).strip()
            )
            continue

        if day and name in HORSES:
            race = RACE.fullmatch(
                value
            )

            if race:
                event_time = race.group(1)
                event = race.group(2)
                continue

        if not day or not event:
            continue

        market = value.casefold()

        if (
            market in MARKETS
            and i > 0
        ):
            selection = lines[i - 1]

            if not valid_selection(
                selection
            ):
                continue

            odds = nearby_odds(
                lines,
                i,
            )

            result.append(
                make_tip(
                    name,
                    day,
                    event,
                    event_time,
                    value,
                    selection,
                    odds,
                )
            )

            continue

        if name in HORSES:
            stake = STAKE.fullmatch(
                value
            )

            if not stake or i < 1:
                continue

            selection = lines[i - 1]

            if not valid_selection(
                selection
            ):
                continue

            stake_type = (
                stake.group(2).upper()
            )

            if stake_type in (
                "EW",
                "E/W",
                "EACH WAY",
            ):
                stake_type = "EW"

            amount = float(
                stake.group(1).replace(
                    ",",
                    ".",
                )
            )

            odds = nearby_odds(
                lines,
                i,
            )

            result.append(
                make_tip(
                    name,
                    day,
                    event,
                    event_time,
                    stake_type,
                    selection,
                    odds,
                    amount,
                )
            )

    unique = {
        tip["id"]: tip
        for tip in result
    }

    if not unique:
        raise ValueError(
            "Page loaded but no validated "
            "current tips or empty marker"
        )

    return list(unique.values())


def overlap(tip, items):
    if tip["author"] not in HORSES:
        return []

    return [
        other
        for other in items
        if (
            other["id"] != tip["id"]
            and other["author"] in HORSES
            and other["author"]
            != tip["author"]
            and other["date"]
            == tip["date"]
            and other["event"].casefold()
            == tip["event"].casefold()
            and other["selection"].casefold()
            == tip["selection"].casefold()
        )
    ]


def format_message(tip, duplicates):
    message = (
        f"{tip['sport']} "
        "НОВИЙ ПРОГНОЗ\n\n"
        f"👤 Капер: {tip['author']}\n"
        f"🏟 Подія: {tip['event']}\n"
        f"📅 Дата: {tip['date']}\n"
    )

    if tip.get("time"):
        message += (
            f"🕒 Час: {tip['time']}\n"
        )

    message += (
        f"🎯 Прогноз: "
        f"{tip['selection']}\n"
        f"📌 Ринок: {tip['market']}\n"
    )

    if tip.get("stake_points") is not None:
        message += (
            "💵 Ставка OLBG: "
            f"{tip['stake_points']} "
            "points\n"
        )

    odds = tip.get("odds_olbg")

    if odds is None:
        message += (
            "💰 Коефіцієнт: "
            "не підтверджено\n"
        )
    else:
        message += (
            f"💰 Коефіцієнт OLBG: "
            f"{odds:.2f}\n"
        )

    if duplicates:
        names = ", ".join(
            sorted(
                {
                    item["author"]
                    for item in duplicates
                }
            )
        )

        message += (
            "\n🐎 Збіг з капером: "
            + names
            + "\n"
        )

    message += (
        "\n🔗 " + tip["source"]
    )

    return message


def diagnostics(name, body):
    # Public OLBG page text only.
    # Never print environment variables,
    # bot tokens, chat IDs or cookies.
    print()
    print(
        "========== OLBG DIAGNOSTICS =========="
    )
    print("TIPSTER:", name)
    print("BODY LENGTH:", len(body))

    lines = [
        line.strip()
        for line in body.splitlines()
        if line.strip()
    ]

    print("TOTAL LINES:", len(lines))

    heading = name.casefold() + "'s tips"

    starts = [
        i
        for i, line in enumerate(lines)
        if heading in line.casefold()
    ]

    print("HEADING POSITIONS:", starts[:10])

    if starts:
        start = starts[0]
    else:
        start = 0

    end = min(
        len(lines),
        start + 100,
    )

    print("DIAGNOSTIC TEXT START")

    for i in range(start, end):
        line = lines[i][:250]

        print(
            f"LINE {i:04d}: {line}"
        )

    print("DIAGNOSTIC TEXT END")
    print(
        "========== END DIAGNOSTICS =========="
    )
    print()


def selftest():
    football = (
        "Geobetting's Tips\n"
        "28 October 2026\n"
        "17:00 Arsenal v Chelsea\n"
        "Over 2.5\n"
        "Total Goals\n"
        "@1.85\n"
        "Tips are normally settled"
    )

    horse = (
        "Knottlast's Tips\n"
        "28 October 2026\n"
        "16:21 Pontefract\n"
        "Mount King\n"
        "25 EW\n"
        "@11.00\n"
        "Tips are normally settled"
    )

    empty = (
        "Geobetting's Tips\n"
        "Geobetting has no current tips\n"
        "Tips are normally settled"
    )

    a = extract(
        "Geobetting",
        football,
    )

    b = extract(
        "Knottlast",
        horse,
    )

    c = extract(
        "Geobetting",
        empty,
    )

    assert len(a) == 1
    assert a[0]["odds_olbg"] == 1.85

    assert len(b) == 1
    assert b[0]["market"] == "EW"
    assert b[0]["stake_points"] == 25
    assert b[0]["odds_olbg"] == 11.0

    assert c == []

    print("SELFTEST OK")


def configure_profiles():
    raw = os.environ.get(
        "OLBG_TIPSTER_IDS",
        "",
    ).strip()

    if not raw:
        return

    try:
        custom = json.loads(raw)
    except json.JSONDecodeError:
        print(
            "CONFIG ERROR: "
            "OLBG_TIPSTER_IDS must be JSON"
        )
        return

    if not isinstance(custom, dict):
        print(
            "CONFIG ERROR: "
            "expected JSON object"
        )
        return

    for name, profile_id in custom.items():
        if (
            name in NAMES
            and re.fullmatch(
                r"\d{1,12}",
                str(profile_id),
            )
        ):
            PROFILES[name] = str(
                profile_id
            )


def main():
    configure_profiles()

    if os.environ.get(
        "RUN_SELFTEST"
    ) == "1":
        selftest()
        return

    state = load()

    tips = state["tips"]
    sources = state["sources"]

    found = []

    with sync_playwright() as playwright:
        browser = (
            playwright.chromium.launch(
                headless=True
            )
        )

        try:
            for name in NAMES:
                if name not in PROFILES:
                    sources[name] = {
                        "status":
                            "not_configured",
                        "checked_at": now(),
                    }

                    print(
                        "NOT CONFIGURED",
                        name,
                    )
                    continue

                address = (
                    "https://www.olbg.com/"
                    "tipsters/tips/"
                    + PROFILES[name]
                )

                body = None

                try:
                    body = get_body(
                        browser,
                        address,
                    )

                    batch = extract(
                        name,
                        body,
                    )

                    found.extend(batch)

                    sources[name] = {
                        "status": "verified",
                        "count": len(batch),
                        "checked_at": now(),
                    }

                    print(
                        "VERIFIED",
                        name,
                        len(batch),
                    )

                except Exception as exc:
                    sources[name] = {
                        "status": "unverified",
                        "checked_at": now(),
                        "details": str(exc)[:140],
                    }

                    print(
                        "UNVERIFIED",
                        name,
                        str(exc)[:140],
                    )

                    # Temporary diagnostic mode.
                    # Only for Knottlast.
                    if (
                        name == "Knottlast"
                        and body is not None
                    ):
                        diagnostics(
                            name,
                            body,
                        )

        finally:
            browser.close()

    allitems = (
        list(tips.values())
        + found
    )

    sent = 0

    for tip in found:
        key = tip["id"]

        if key in tips:
            continue

        duplicates = overlap(
            tip,
            allitems,
        )

        message = format_message(
            tip,
            duplicates,
        )

        telegram(message)

        tips[key] = tip

        save(state)

        sent += 1

        print(
            "SENT",
            tip["author"],
            key[:10],
        )

    save(state)

    print(
        "DONE",
        len(found),
        "current parsed,",
        len(tips),
        "total recorded,",
        sent,
        "new alerts",
    )


if __name__ == "__main__":
    main()
