
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

MARKETS = {
    "Result",
    "Full Time Result",
    "Both Teams to Score",
    "Total Goals",
    "Correct Score",
    "Asian Hcap",
    "Result and BTTS",
    "Total Corners",
    "Total Cards",
    "Half Time Result",
    "Half Time/Full Time",
    "Anytime Goalscorer",
    "First Goalscorer",
    "Match Winner",
    "Winner",
    "Outright Winner",
    "Each Way",
    "To Win",
}

DATE = re.compile(r"^\d{1,2} [A-Za-z]+ 20\d{2}$")
FIXTURE = re.compile(
    r"^(?:(\d{1,2}:\d{2})\s+)?(.+?)\s+v\s+(.+)$",
    re.I,
)
RACE = re.compile(
    r"^(\d{1,2}:\d{2})(?:\s+\d{1,2}:\d{2})?\s+(.+)$"
)
ODDS = re.compile(
    r"^(?:@\s*|odds\s*[: ]\s*)(\d+(?:[.,]\d+)?)$",
    re.I,
)
STAKE = re.compile(r"^(\d+(?:\.\d+)?)\s*(EW|WIN)$", re.I)


def now():
    return datetime.now(KYIV).isoformat(timespec="seconds")


def digest(*args):
    value = "|".join(map(str, args))
    return hashlib.sha256(value.encode()).hexdigest()


def load():
    if STATE.exists():
        obj = json.loads(STATE.read_text(encoding="utf-8"))
        if not isinstance(obj, dict):
            raise ValueError("State must be an object")
        return obj

    return {
        "tips": {},
        "sources": {},
        "seen": [],
        "schema": 2,
    }


def save(state):
    STATE.parent.mkdir(exist_ok=True, parents=True)

    STATE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    subprocess.run(["git", "add", str(STATE)], check=True)

    changed = subprocess.run(
        ["git", "diff", "--cached", "--quiet"]
    ).returncode

    if changed:
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=github-actions[bot]",
                "-c",
                "user.email=41898282+github-actions[bot]@users.noreply.github.com",
                "commit",
                "-m",
                "Update OLBG tips v2",
            ],
            check=True,
        )

        subprocess.run(
            ["git", "pull", "--rebase", "origin", "main"],
            check=True,
        )

        subprocess.run(
            ["git", "push", "origin", "HEAD:main"],
            check=True,
        )


def telegram(message):
    token = os.environ["BOT_TOKEN"]
    chat = os.environ["CHAT_ID"]

    data = urllib.parse.urlencode(
        {
            "chat_id": chat,
            "text": message[:4096],
            "disable_web_page_preview": "true",
        }
    ).encode()

    request = urllib.request.Request(
        "https://api.telegram.org/bot" + token + "/sendMessage",
        data=data,
    )

    with urllib.request.urlopen(request, timeout=25) as response:
        result = json.load(response)

    if not result.get("ok"):
        raise RuntimeError("Telegram rejected message")


def body_from_html(html):
    soup = BeautifulSoup(html, "html.parser")

    for node in soup(["script", "style", "noscript"]):
        node.decompose()

    return soup.get_text("\n", strip=True)


def get_body(browser, address):
    try:
        request = urllib.request.Request(
            address,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept-Language": "en-GB,en;q=0.9",
            },
        )

        with urllib.request.urlopen(
            request, timeout=18
        ) as response:
            body = body_from_html(
                response.read(2500000).decode(
                    "utf-8", "replace"
                )
            )

        if len(body) > 100 and (
            "has no current tips" in body.lower()
            or "'s tips" in body.lower()
        ):
            return body

    except Exception as exc:
        print("HTTP", type(exc).__name__, str(exc)[:100])

    page = browser.new_page(locale="en-GB")

    try:
        response = page.goto(
            address,
            wait_until="domcontentloaded",
            timeout=30000,
        )

        if response and response.status >= 400:
            raise ValueError("HTTP " + str(response.status))

        page.wait_for_timeout(2000)

        return page.locator("body").inner_text()

    finally:
        page.close()


def extract(name, body):
    lines = [
        x.strip()
        for x in body.splitlines()
        if x.strip()
    ]

    start = next(
        (
            i
            for i, x in enumerate(lines)
            if name.casefold() + "'s tips" in x.casefold()
            and "history" not in x.casefold()
        ),
        None,
    )

    if start is None:
        raise ValueError("Current Tips section not found")

    lines = lines[start + 1:]

    end = next(
        (
            i
            for i, x in enumerate(lines)
            if "tips are normally settled" in x.casefold()
        ),
        len(lines),
    )

    lines = lines[:end]

    if any(
        name.casefold() + " has no current tips"
        in x.casefold()
        for x in lines
    ):
        return []

    result = []
    day = None
    event = None
    time = None

    for i, x in enumerate(lines):
        if DATE.fullmatch(x):
            try:
                day = datetime.strptime(
                    x, "%d %B %Y"
                ).date().isoformat()
            except ValueError:
                day = None

            event = None
            time = None
            continue

        match = FIXTURE.fullmatch(x)

        if day and match:
            time = match[1]
            event = match[2] + " v " + match[3]
            continue

        race = RACE.fullmatch(x)

        if day and race and name in (
            "Knottlast",
            "Niffler",
        ):
            time = race[1]
            event = race[2]
            continue

        if not day or not event:
            continue

        if x in MARKETS and i > 0:
            selection = lines[i - 1]

            if (
                len(selection) > 110
                or selection in MARKETS
            ):
                continue

            nearby = lines[
                max(0, i - 7):min(len(lines), i + 7)
            ]

            odd = next(
                (
                    float(o[1].replace(",", "."))
                    for value in nearby
                    if (o := ODDS.fullmatch(value))
                ),
                None,
            )

            if odd is not None and not (
                1.01 <= odd <= 10000
            ):
                odd = None

            key = digest(
                name, day, event, x, selection
            )

            result.append(
                {
                    "id": key,
                    "author": name,
                    "sport": SPORTS[name],
                    "date": day,
                    "time": time,
                    "event": event,
                    "market": x,
                    "selection": selection,
                    "odds_olbg": odd,
                    "odds_available": None,
                    "first_seen_at": now(),
                    "result": "pending",
                    "source": (
                        "https://www.olbg.com/tipsters/tips/"
                        + PROFILES[name]
                    ),
                }
            )

        elif (
            name in ("Knottlast", "Niffler")
            and (stake := STAKE.fullmatch(x))
            and i >= 1
        ):
            selection = lines[i - 1]

            if not selection or len(selection) > 100:
                continue

            nearby = lines[i:min(len(lines), i + 7)]

            odd = next(
                (
                    float(o[1].replace(",", "."))
                    for value in nearby
                    if (o := ODDS.fullmatch(value))
                ),
                None,
            )

            key = digest(
                name,
                day,
                event,
                selection,
                stake[2],
            )

            result.append(
                {
                    "id": key,
                    "author": name,
                    "sport": SPORTS[name],
                    "date": day,
                    "time": time,
                    "event": event,
                    "market": stake[2].upper(),
                    "selection": selection,
                    "odds_olbg": odd,
                    "odds_available": None,
                    "first_seen_at": now(),
                    "result": "pending",
                    "source": (
                        "https://www.olbg.com/tipsters/tips/"
                        + PROFILES[name]
                    ),
                    "stake_points": float(stake[1]),
                }
            )

    if not result:
        raise ValueError(
            "Unverified page: no validated current tips"
        )

    return list(
        {tip["id"]: tip for tip in result}.values()
    )


def overlap(tip, items):
    if tip["author"] not in (
        "Knottlast",
        "Niffler",
    ):
        return []

    return [
        other
        for other in items
        if other["id"] != tip["id"]
        and other["author"] in (
            "Knottlast",
            "Niffler",
        )
        and other["author"] != tip["author"]
        and other["date"] == tip["date"]
        and other["event"].casefold()
        == tip["event"].casefold()
        and other["selection"].casefold()
        == tip["selection"].casefold()
    ]


def main():
    state = load()
    tips = state.setdefault("tips", {})
    sources = state.setdefault("sources", {})

    custom = json.loads(
        os.environ.get("OLBG_TIPSTER_IDS", "{}")
        or "{}"
    )

    for name, profile_id in custom.items():
        if (
            name in NAMES
            and re.fullmatch(
                r"\d{1,12}", str(profile_id)
            )
        ):
            PROFILES[name] = str(profile_id)

    if os.environ.get("RUN_SELFTEST") == "1":
        sample = (
            "Geobetting's Tips\n"
            "28 October 2026\n"
            "17:00 A v B\n"
            "Over 2.5\n"
            "Total Goals\n"
            "Tips are normally settled"
        )

        assert len(
            extract("Geobetting", sample)
        ) == 1

        print("SELFTEST OK")
        return

    found = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True
        )

        try:
            for name in NAMES:
                if name not in PROFILES:
                    sources[name] = {
                        "status": "not_configured",
                        "checked_at": now(),
                    }

                    print("NOT CONFIGURED", name)
                    continue

                try:
                    address = (
                        "https://www.olbg.com/tipsters/tips/"
                        + PROFILES[name]
                    )

                    batch = extract(
                        name,
                        get_body(browser, address),
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

        finally:
            browser.close()

    allitems = list(tips.values()) + found

    for tip in found:
        if tip["id"] in tips:
            continue

        duplicates = overlap(tip, allitems)

        message = (
            f"{tip['sport']} НОВИЙ ПРОГНОЗ\n"
            f"👤 {tip['author']}\n"
            f"🏟 {tip['event']}\n"
            f"📅 {tip['date']} "
            f"{tip.get('time') or ''}\n"
            f"🎯 {tip['selection']} "
            f"— {tip['market']}\n"
            f"💰 Коеф. OLBG: "
            f"{tip['odds_olbg'] if tip['odds_olbg'] is not None else 'не підтверджено'}"
        )

        if duplicates:
            names = ", ".join(
                sorted(
                    {
                        other["author"]
                        for other in duplicates
                    }
                )
            )

            message += (
                "\n🐎 ЗБІГ З ІНШИМ КАПЕРОМ: "
                + names
            )

        message += "\n🔗 " + tip["source"]

        telegram(message)

        tips[tip["id"]] = tip
        save(state)

        print(
            "SENT",
            tip["author"],
            tip["id"][:10],
        )

    save(state)

    print(
        "DONE",
        len(found),
        "current parsed,",
        len(tips),
        "total recorded",
    )


if __name__ == "__main__":
    main()
