"""Football Tips Alerts: verified OLBG current tips; state schema stays at v2."""

import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup


KYIV = ZoneInfo("Europe/Kyiv")
STATE = Path(".state/tips-v2.json")
PROFILES = {
    "Knottlast": "666034",
    "Niffler": "638883",
    "dacris69": "480440",
    "risto123": "411789",
    "Geobetting": "420769",
    "mojojak": "126921",
}
NAMES = list(PROFILES)
SPORTS = {
    "Knottlast": "🐎 Перегони", "Niffler": "🐎 Перегони",
    "dacris69": "🎱 Снукер", "risto123": "⚾ Бейсбол",
    "Geobetting": "⚽ Футбол", "mojojak": "⛳ Гольф",
}
SPORT_IDS = {
    "Knottlast": 2, "Niffler": 2, "dacris69": 8,
    "risto123": 12, "Geobetting": 1, "mojojak": 5,
}
HORSES = {"Knottlast", "Niffler"}
MAX_HTML = 4_000_000
MAX_PAGES = 20
IDENT = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
ROUTE = re.compile(r'(?:type|"type")\s*:\s*"data"\s*,\s*(?:data|"data")\s*:\s*(?=\{)')
DECODER = json.JSONDecoder()


class Unverified(ValueError):
    pass


def now():
    return datetime.now(KYIV).isoformat(timespec="seconds")


def norm(value):
    return " ".join(unicodedata.normalize("NFKC", str(value)).replace("’", "'").split()).casefold()


def digest(*values):
    return hashlib.sha256("|".join(map(str, values)).encode("utf-8")).hexdigest()


def source_url(name, page=1):
    base = "https://www.olbg.com/tipsters/tips/" + PROFILES[name]
    return base if page == 1 else base + "?page=" + str(page)


def configure_profiles():
    raw = os.environ.get("OLBG_TIPSTER_IDS", "").strip()
    if not raw:
        return
    try:
        custom = json.loads(raw)
    except json.JSONDecodeError:
        raise ValueError("OLBG_TIPSTER_IDS must be a JSON object") from None
    if not isinstance(custom, dict):
        raise ValueError("OLBG_TIPSTER_IDS must be a JSON object")
    for name, profile_id in custom.items():
        if name not in PROFILES:
            raise ValueError("Unknown tipster in OLBG_TIPSTER_IDS")
        if isinstance(profile_id, bool) or not re.fullmatch(r"[1-9]\d{0,11}", str(profile_id)):
            raise ValueError("Invalid OLBG ID for " + name)
        PROFILES[name] = str(profile_id)
    if len(set(PROFILES.values())) != len(PROFILES):
        raise ValueError("Two tipsters cannot use the same OLBG ID")


def literal(text, pos=0, depth=0):
    """Read Svelte's JSON-like literals with bare keys. Never execute page code."""
    if depth > 60:
        raise Unverified("Embedded data is nested too deeply")
    while pos < len(text) and text[pos].isspace():
        pos += 1
    if pos >= len(text):
        raise Unverified("Truncated embedded data")
    opening = text[pos]
    if opening in "{[":
        is_object = opening == "{"
        close = "}" if is_object else "]"
        result = {} if is_object else []
        pos += 1
        while True:
            while pos < len(text) and text[pos].isspace():
                pos += 1
            if pos >= len(text):
                raise Unverified("Truncated embedded data")
            if text[pos] == close:
                return result, pos + 1
            if is_object:
                if text[pos] == '"':
                    key, pos = DECODER.raw_decode(text, pos)
                else:
                    match = IDENT.match(text, pos)
                    if not match:
                        raise Unverified("Unsupported embedded object key")
                    key, pos = match[0], match.end()
                while pos < len(text) and text[pos].isspace():
                    pos += 1
                if pos >= len(text) or text[pos] != ":" or key in result:
                    raise Unverified("Invalid embedded object")
                value, pos = literal(text, pos + 1, depth + 1)
                result[key] = value
            else:
                value, pos = literal(text, pos, depth + 1)
                result.append(value)
            while pos < len(text) and text[pos].isspace():
                pos += 1
            if pos < len(text) and text[pos] == close:
                return result, pos + 1
            if pos >= len(text) or text[pos] != ",":
                raise Unverified("Unsupported embedded expression")
            pos += 1
    undefined = re.match(r"(?:void\s+0|undefined)(?![\w$])", text[pos:])
    if undefined:
        return None, pos + undefined.end()
    # Svelte also serializes avatar timestamps as new Date(123). Read the value only.
    date_start = re.match(r"new\s+Date\s*\(\s*", text[pos:])
    if date_start:
        value, end = literal(text, pos + date_start.end(), depth + 1)
        while end < len(text) and text[end].isspace():
            end += 1
        if type(value) not in (str, int, float) or end >= len(text) or text[end] != ")":
            raise Unverified("Unsupported embedded date literal")
        return {"serialized_date": value}, end + 1
    try:
        value, end = DECODER.raw_decode(text, pos)
    except (ValueError, RecursionError):
        raise Unverified("Unsupported embedded literal") from None
    if isinstance(value, float) and not math.isfinite(value):
        raise Unverified("Non-finite embedded number")
    return value, end


def route_data(soup, name):
    candidates = []
    payload_present = False
    for node in soup.find_all("script"):
        script = node.string or node.get_text()
        if "tipsByDate" not in script or "effectiveTipsterId" not in script:
            continue
        payload_present = True
        for match in ROUTE.finditer(script):
            try:
                data, _ = literal(script, match.end())
            except (ValueError, RecursionError):
                continue
            if isinstance(data, dict) and "effectiveTipsterId" in data and "tipsByDate" in data:
                candidates.append(data)
    if not candidates:
        if payload_present:
            raise Unverified("Current-tip payload exists but cannot be decoded")
        return None
    if len(candidates) != 1:
        raise Unverified("Ambiguous current-tip payloads")
    data = candidates[0]
    if data.get("mode") != "current":
        raise Unverified("History/profile data cannot be used as current tips")
    if str(data.get("effectiveTipsterId")) != PROFILES[name]:
        raise Unverified("OLBG profile ID does not match the configured ID")
    if norm(data.get("tipsterUsername", "")) != norm(name):
        raise Unverified("OLBG username does not match the configured tipster")
    return data


def body_from_html(html):
    soup = BeautifulSoup(html, "html.parser")
    for node in soup(["head", "script", "style", "noscript", "template", "nav", "footer"]):
        node.decompose()
    return (soup.body or soup).get_text("\n", strip=True)


def current_section(name, body):
    lines = [" ".join(x.split()) for x in body.splitlines() if x.strip()]
    normal = [norm(x) for x in lines]
    heading = norm(name + "'s Tips")
    markers = []
    for i, value in enumerate(normal):
        if value == "section: current tips":
            markers.append(i)
        elif value == "section:" and i + 1 < len(lines) and normal[i + 1] == "current tips":
            markers.append(i + 1)
    if markers:
        start = next((i for i in range(markers[-1] + 1, min(len(lines), markers[-1] + 12))
                      if normal[i] == heading), None)
    else:
        headings = [i for i, value in enumerate(normal) if value == heading]
        start = headings[0] if len(headings) == 1 else None
    if start is None:
        raise Unverified("Cannot locate the exact Current Tips heading for " + name)
    end = next((i for i in range(start + 1, len(lines))
                if normal[i].startswith("tips are normally settled")), None)
    if end is None:
        raise Unverified("Current Tips section is incomplete: footer missing")
    return lines[start + 1:end]


def number(value, minimum=0):
    if isinstance(value, bool) or value is None:
        return None
    try:
        value = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value > minimum else None


def parse_odds(value):
    if value is None:
        return None
    raw = re.sub(r"^(?:@\s*|odds\s*[: ]\s*)", "", str(value).strip(), flags=re.I)
    if re.fullmatch(r"\d+(?:[.,]\d+)?/\d+(?:[.,]\d+)?", raw):
        a, b = (number(x) for x in raw.split("/"))
        return round(1 + a / b, 6) if a is not None and b is not None else None
    return number(raw, minimum=1)


def plain(value, field, limit=300):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise Unverified("Missing or invalid tip field: " + field)
    if "<" in value or ">" in value:
        raise Unverified("Unexpected markup in tip field: " + field)
    return " ".join(value.split())


def structured_tip(name, raw, date_label):
    selection = plain(raw.get("selection"), "selection", 160)
    event = plain(raw.get("eventname"), "eventname")
    source_id = raw.get("tipid")
    if isinstance(source_id, bool) or not re.fullmatch(r"[1-9]\d{0,17}", str(source_id)):
        raise Unverified("Missing or invalid OLBG tip ID")
    try:
        dt = datetime.fromisoformat(raw["event_datetime"])
        label = re.sub(r"(?<=\d)(?:st|nd|rd|th)\b", "", date_label, flags=re.I)
        day = datetime.strptime(label, "%d %B %Y").date()
    except (KeyError, TypeError, ValueError):
        raise Unverified("Missing or invalid source event date") from None
    if dt.date() != day:
        raise Unverified("Event date disagrees with its Current Tips date group")
    bet_type = str(raw.get("winew", "")).strip().upper()
    if bet_type in ("E/W", "EACH WAY"):
        bet_type = "EW"
    if bet_type not in ("WIN", "EW"):
        raise Unverified("Unknown OLBG bet type")
    raw_market = raw.get("market")
    if not isinstance(raw_market, str):
        raise Unverified("Missing OLBG market field")
    market = bet_type if name in HORSES or not raw_market.strip() else plain(raw_market, "market", 160)
    if norm(raw.get("result", "")) not in ("open", "pending"):
        raise Unverified("A non-open tip appeared in the current-tip payload")
    if name in HORSES:
        event = re.sub(r"^\d{1,2}:\d{2}\s+", "", event).strip()
        if not event:
            raise Unverified("Missing racecourse")
    tip = {
        "id": digest("olbg-tip", PROFILES[name], source_id),
        "author": name, "sport": SPORTS[name],
        "date": dt.date().isoformat(), "time": dt.strftime("%H:%M"),
        "event": event, "market": market, "selection": selection,
        "odds_olbg": parse_odds(raw.get("odds")), "odds_available": None,
        "first_seen_at": now(), "result": "pending", "source": source_url(name),
        "source_tip_id": str(source_id), "source_event_id": str(raw.get("eventid", "")),
        "source_sport_id": SPORT_IDS[name], "market_olbg": raw_market,
        "bet_type": bet_type, "event_datetime_olbg": raw["event_datetime"],
        "handicap_olbg": raw.get("hcap"), "published_at": None,
    }
    stake = number(raw.get("stake"))
    if stake is not None:
        tip["stake_points"] = stake
    if bet_type == "EW":
        tip["each_way_terms"] = None
    return tip


@dataclass
class PageResult:
    tips: list
    all_count: int
    ignored: int
    has_more: bool
    pages: int
    signature: tuple
    proof: str


def parse_page(name, html):
    soup = BeautifulSoup(html, "html.parser")
    data = route_data(soup, name)
    if data is None:
        section = current_section(name, body_from_html(html))
        message = norm(" ".join(section))
        author = re.escape(norm(name))
        empty_pattern = (author + r" has no current tips\s*[.!]?"
                         r"(?:\s+you can check\s+" + author + r"'s tips history"
                         r"\s+or you can\s+make a tip\s*[.!]?)?")
        # A blank section, generic no-tips text, or hidden list is never proof of zero.
        if re.fullmatch(empty_pattern, message):
            return PageResult([], 0, 0, False, 0, (), "visible named empty marker")
        raise Unverified("Current section has no verifiable data or named empty marker")
    groups, has_tips = data.get("tipsByDate"), data.get("hasTips")
    if not isinstance(groups, list) or type(has_tips) is not bool:
        raise Unverified("Invalid tipsByDate / hasTips fields")
    rows = []
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("events"), list):
            raise Unverified("Invalid current-tip date group")
        for event in group["events"]:
            if not isinstance(event, dict) or not isinstance(event.get("tips"), list):
                raise Unverified("Invalid current-tip event group")
            for row in event["tips"]:
                if not isinstance(row, dict):
                    raise Unverified("Invalid current-tip record")
                rows.append((row, group.get("date")))
    if bool(rows) != has_tips:
        raise Unverified("hasTips disagrees with the current-tip list")
    pagination = data.get("pagination")
    if not isinstance(pagination, dict):
        raise Unverified("Pagination metadata is missing")
    pages, more = pagination.get("totalPages"), pagination.get("hasMore")
    if type(pages) is not int or not 0 <= pages <= MAX_PAGES or type(more) is not bool:
        raise Unverified("Invalid pagination metadata or too many pages")
    if not rows and (more or pages > 1):
        raise Unverified("Empty page contradicts pagination")
    result, ignored, signature = {}, 0, []
    for row, label in rows:
        sid = row.get("vc_sport_id")
        if isinstance(sid, bool) or not re.fullmatch(r"[1-9]\d*", str(sid)):
            raise Unverified("Tip sport is not identified")
        signature.append(str(row.get("tipid")))
        if int(sid) != SPORT_IDS[name]:
            ignored += 1
            continue
        tip = structured_tip(name, row, label)
        prior = result.get(tip["id"])
        if prior and any(prior.get(k) != tip.get(k) for k in ("event", "market", "selection", "date", "time")):
            raise Unverified("Conflicting records share the same OLBG tip ID")
        result[tip["id"]] = tip
    proof = "embedded current-tip data" if rows else "embedded hasTips=false and empty tipsByDate"
    return PageResult(list(result.values()), len(rows), ignored, more, pages, tuple(signature), proof)


def fetch_html(address):
    request = urllib.request.Request(address, headers={
        "User-Agent": "Mozilla/5.0", "Accept-Language": "en-GB,en;q=0.9",
    })
    with urllib.request.urlopen(request, timeout=25) as response:
        target = urllib.parse.urlparse(response.url)
        expected = urllib.parse.urlparse(address)
        if target.hostname not in ("www.olbg.com", "olbg.com") or target.path.rstrip("/") != expected.path:
            raise Unverified("Unexpected redirect from the current-tip page")
        raw = response.read(MAX_HTML + 1)
    if len(raw) > MAX_HTML:
        raise Unverified("HTML response is too large")
    return raw.decode("utf-8", "replace")


class Reader:
    def __init__(self):
        self.playwright = None
        self.browser = None

    def close(self):
        if self.browser:
            self.browser.close()
        if self.playwright:
            self.playwright.stop()

    def browser_page(self, name, address):
        if self.browser is None:
            from playwright.sync_api import sync_playwright
            self.playwright = sync_playwright().start()
            self.browser = self.playwright.chromium.launch(headless=True)
        page = self.browser.new_page(locale="en-GB")
        try:
            response = page.goto(address, wait_until="domcontentloaded", timeout=35000)
            if response is None or response.status >= 400:
                raise Unverified("Browser failed to load the source page")
            target = urllib.parse.urlparse(page.url)
            if target.hostname not in ("www.olbg.com", "olbg.com") or target.path.rstrip("/") != urllib.parse.urlparse(address).path:
                raise Unverified("Unexpected browser redirect")
            last_error = None
            for attempt in range(6):
                try:
                    return parse_page(name, page.content())
                except Unverified as exc:
                    last_error = exc
                if attempt < 5:
                    page.wait_for_timeout(1200)
            raise last_error
        finally:
            page.close()

    def read(self, name):
        tips, all_count, ignored, signatures = {}, 0, 0, set()
        methods = set()
        declared_pages = None
        for page_number in range(1, MAX_PAGES + 1):
            address = source_url(name, page_number)
            try:
                result = parse_page(name, fetch_html(address))
                methods.add("HTTP")
            except Exception as exc:
                # A title or a placeholder is insufficient: parsing failure also triggers the browser.
                print("BROWSER FALLBACK", name, type(exc).__name__)
                result = self.browser_page(name, address)
                methods.add("browser")
            if page_number > 1 and result.signature in signatures:
                raise Unverified("Pagination repeated an earlier page")
            if declared_pages is None:
                declared_pages = result.pages
            elif result.pages != declared_pages or result.pages < page_number:
                raise Unverified("Pagination changed during this check")
            signatures.add(result.signature)
            for tip in result.tips:
                tips.setdefault(tip["id"], tip)
            all_count += result.all_count
            ignored += result.ignored
            expected_more = page_number < result.pages
            if result.has_more != expected_more:
                raise Unverified("Pagination hasMore disagrees with totalPages")
            if not result.has_more:
                return list(tips.values()), {
                    "all_sports_count": all_count, "filtered_out": ignored,
                    "pages": page_number, "proof": result.proof,
                    "transport": "+".join(sorted(methods)),
                }
        raise Unverified("Pagination did not finish")


def load():
    if not STATE.exists():
        return {"schema": 2, "tips": {}, "sources": {}, "seen": []}
    state = json.loads(STATE.read_text(encoding="utf-8"))
    if not isinstance(state, dict) or state.get("schema", 2) != 2:
        raise ValueError("Invalid state object or unsupported schema; history was not reset")
    for key in ("tips", "sources"):
        if not isinstance(state.setdefault(key, {}), dict):
            raise ValueError("Invalid state field: " + key)
    if not isinstance(state.setdefault("seen", []), list) or any(not isinstance(x, str) for x in state["seen"]):
        raise ValueError("Invalid state field: seen")
    if any(not isinstance(x, dict) for x in state["tips"].values()):
        raise ValueError("Invalid historical tip; history was not reset")
    state.setdefault("schema", 2)
    return state


def save(state):
    """Atomic local write; Actions commits only this state file and then pushes it."""
    STATE.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(state, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if STATE.exists() and STATE.read_text(encoding="utf-8") == content:
        return
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=STATE.parent, delete=False) as tmp:
        temporary = Path(tmp.name)
        try:
            tmp.write(content)
            tmp.flush()
            os.fsync(tmp.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, STATE)
    finally:
        temporary.unlink(missing_ok=True)
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    subprocess.run(["git", "add", "--", str(STATE)], check=True)
    diff = subprocess.run(["git", "diff", "--cached", "--quiet", "--", str(STATE)], check=False).returncode
    if diff not in (0, 1):
        raise RuntimeError("Unable to inspect git state changes")
    if diff == 1:
        subprocess.run([
            "git", "-c", "user.name=github-actions[bot]",
            "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com",
            "commit", "--only", "-m", "Update OLBG tips v2", "--", str(STATE),
        ], check=True)
    subprocess.run(["git", "fetch", "origin", "main"], check=True)
    base = subprocess.check_output(["git", "merge-base", "HEAD", "origin/main"], text=True).strip()
    remote_changed = subprocess.run(
        ["git", "diff", "--quiet", base, "origin/main", "--", str(STATE)], check=False,
    ).returncode
    if remote_changed != 0:
        raise RuntimeError("Remote state changed concurrently; stopping before further alerts")
    subprocess.run(["git", "rebase", "origin/main"], check=True)
    subprocess.run(["git", "push", "origin", "HEAD:main"], check=True)


def same_tip(left, right):
    keys = ("author", "date", "event", "market", "selection")
    if any(norm(left.get(k, "")) != norm(right.get(k, "")) for k in keys):
        return False
    # A course can have several races on the same day.
    return not (left.get("time") and right.get("time") and left["time"] != right["time"])


def remember(state, tip):
    tips = state["tips"]
    key = tip["id"] if tip["id"] in tips else None
    if key is None:
        matches = [k for k, old in tips.items() if
                   (old.get("author") == tip["author"] and old.get("source_tip_id") == tip["source_tip_id"])
                   or (not old.get("source_tip_id") and same_tip(old, tip))]
        if len(matches) > 1:
            raise ValueError("Ambiguous historical duplicate; history was not merged")
        key = matches[0] if matches else None
    if key is not None:
        old = tips[key]
        if old.get("author") != tip["author"]:
            raise ValueError("Tip ID conflicts with historical author")
        # Old releases recorded a tip only after a successful Telegram send.
        old.setdefault("alert_status", "sent_legacy")
        old["last_seen_at"] = now()
        for field in ("source_tip_id", "source_event_id", "source_sport_id", "bet_type",
                      "market_olbg", "event_datetime_olbg", "handicap_olbg"):
            old.setdefault(field, tip.get(field))
        if old.get("odds_olbg_latest") != tip.get("odds_olbg"):
            old["odds_olbg_latest"] = tip.get("odds_olbg")
            old["odds_olbg_latest_at"] = now()
        # Original odds, available odds, first_seen_at, results and unknown fields stay intact.
        return old
    legacy = [digest(tip["author"], tip["date"], tip["event"], tip["market"], tip["selection"])]
    if tip["author"] in HORSES:
        legacy.append(digest(tip["author"], tip["date"], tip["event"], tip["selection"], tip["market"]))
    sent_before = any(uid in state["seen"] for uid in [tip["id"], *legacy])
    tip["alert_status"] = "sent_legacy" if sent_before else "pending"
    tip["last_seen_at"] = now()
    tips[tip["id"]] = tip
    return tip


def overlap(tip, items):
    if tip["author"] not in HORSES:
        return []
    matches = set()
    for other in items:
        if other.get("author") not in HORSES or other.get("author") == tip["author"]:
            continue
        if other.get("date") != tip["date"] or norm(other.get("selection", "")) != norm(tip["selection"]):
            continue
        same_event_id = tip.get("source_event_id") and tip["source_event_id"] == other.get("source_event_id")
        same_race = (norm(other.get("event", "")) == norm(tip["event"])
                     and bool(tip.get("time")) and tip["time"] == other.get("time"))
        if same_event_id or same_race:
            matches.add(other["author"])
    return sorted(matches)


def format_message(tip, duplicates):
    lines = [f"{tip['sport']} НОВИЙ ПРОГНОЗ", "", f"👤 Капер: {tip['author']}",
             f"🏟 Подія: {tip['event']}", f"📅 Дата OLBG: {tip['date']}"]
    if tip.get("time"):
        lines.append(f"🕒 Час OLBG: {tip['time']}")
    lines += [f"🎯 Прогноз: {tip['selection']}", f"📌 Ринок: {tip['market']}"]
    if tip.get("bet_type") == "EW":
        lines.append("🐎 Тип: Each Way (EW)")
    if tip.get("stake_points") is not None:
        lines.append(f"💵 Ставка OLBG: {tip['stake_points']:g} points")
    odds = tip.get("odds_olbg")
    lines.append("💰 Коефіцієнт OLBG: " + (f"{odds:g}" if odds is not None else "не підтверджено"))
    if duplicates:
        lines.append("🐎 Збіг з капером: " + ", ".join(duplicates))
    lines += ["", "🔗 " + tip["source"]]
    return "\n".join(lines)


class DeliveryRejected(RuntimeError):
    pass


class DeliveryUnknown(RuntimeError):
    pass


def telegram(message):
    # Do not log urllib exceptions: their URLs contain BOT_TOKEN.
    payload = urllib.parse.urlencode({
        "chat_id": os.environ["CHAT_ID"], "text": message,
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    request = urllib.request.Request(
        "https://api.telegram.org/bot" + os.environ["BOT_TOKEN"] + "/sendMessage", data=payload,
    )
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            answer = json.load(response)
    except urllib.error.HTTPError as exc:
        if 400 <= exc.code < 500:
            raise DeliveryRejected("Telegram HTTP " + str(exc.code)) from None
        raise DeliveryUnknown("Telegram delivery could not be confirmed") from None
    except Exception:
        raise DeliveryUnknown("Telegram delivery could not be confirmed") from None
    if not isinstance(answer, dict):
        raise DeliveryUnknown("Invalid Telegram response")
    if answer.get("ok") is False:
        raise DeliveryRejected("Telegram rejected the message")
    result = answer.get("result")
    if answer.get("ok") is not True or not isinstance(result, dict) or type(result.get("message_id")) is not int:
        raise DeliveryUnknown("Telegram message ID was not confirmed")
    return result["message_id"]


def deliver(state, current, sender=telegram, saver=save):
    sent, failed = 0, False
    for tip in current:
        status = tip.get("alert_status")
        if status in ("sending", "unknown"):
            print("DELIVERY UNKNOWN", tip["author"], tip["id"][:12], "automatic resend suppressed")
            failed = True
            continue
        if status != "pending":
            continue
        message = format_message(tip, overlap(tip, state["tips"].values()))
        tip["alert_status"] = "sending"
        tip["alert_attempt_at"] = now()
        # Persist BEFORE the POST. An interrupted/ambiguous delivery is not blindly repeated.
        saver(state)
        try:
            message_id = sender(message)
        except DeliveryRejected as exc:
            tip["alert_status"] = "pending"
            tip["alert_error"] = str(exc)
            saver(state)
            print("TELEGRAM REJECTED", str(exc))
            return sent, True
        except DeliveryUnknown as exc:
            tip["alert_status"] = "unknown"
            tip["alert_error"] = str(exc)
            saver(state)
            print("DELIVERY UNKNOWN", tip["author"], tip["id"][:12])
            return sent, True
        tip.update(alert_status="sent", alert_sent_at=now(), telegram_message_id=message_id)
        tip.pop("alert_error", None)
        if tip["id"] not in state["seen"]:
            state["seen"].append(tip["id"])
        saver(state)
        sent += 1
        print("SENT", tip["author"], tip["id"][:12])
    return sent, failed


def main():
    if os.environ.get("RUN_SELFTEST") == "1":
        selftest()
        return 0
    configure_profiles()
    dry_run = os.environ.get("DRY_RUN") == "1"
    if not dry_run and any(not os.environ.get(k, "").strip() for k in ("BOT_TOKEN", "CHAT_ID")):
        raise ValueError("BOT_TOKEN and CHAT_ID must be configured")
    state = load()
    found, failures = [], []
    reader = Reader()
    try:
        for name in NAMES:
            old = state["sources"].get(name, {})
            info = {"checked_at": now(), "url": source_url(name), "profile_id": PROFILES[name]}
            try:
                batch, evidence = reader.read(name)
                found.extend(batch)
                info.update(status="verified", count=len(batch), last_success_at=now(), **evidence)
                print("VERIFIED", name, len(batch), "all_sports=" + str(evidence["all_sports_count"]),
                      "filtered_out=" + str(evidence["filtered_out"]), evidence["proof"])
            except Exception as exc:
                failures.append(name)
                info.update(status="unverified", count=None,
                            details=type(exc).__name__ + ": " + str(exc)[:180])
                if isinstance(old, dict):
                    info["last_success_at"] = old.get("last_success_at")
                    info["last_verified_count"] = old.get("count") if old.get("status") == "verified" else old.get("last_verified_count")
                print("UNVERIFIED", name, info["details"])
            state["sources"][name] = info
    finally:
        reader.close()
    found = list({tip["id"]: tip for tip in found}.values())
    if dry_run:
        print("DRY RUN", len(found), "current parsed; no writes and no Telegram messages")
        return int(bool(failures))
    current = [remember(state, tip) for tip in found]
    save(state)
    sent, delivery_failed = deliver(state, current)
    print("DONE", len(found), "current parsed,", len(state["tips"]), "total recorded,", sent, "new alerts,",
          len(failures), "unverified sources")
    return int(bool(failures) or delivery_failed)


def selftest():
    # Synthetic fixtures only: no network, state writes, git or Telegram.
    body = "Knottlast's Tips\nNavigation\nSection: Current Tips\nKnottlast's Tips\nKnottlast has no current tips.\nTips are normally settled"
    assert current_section("Knottlast", body) == ["Knottlast has no current tips."]
    assert parse_odds("@9/2") == 5.5 and parse_odds("1/0") is None
    assert parse_odds(True) is None and parse_odds("nan") is None
    value, _ = literal('{mode:"current",tips:[],stamp:new Date(123),missing:void 0}')
    assert value["tips"] == [] and value["missing"] is None
    for source in ('{a:alert(1)}', '{a:1,a:2}', '{a:NaN}'):
        try:
            literal(source)
        except Unverified:
            pass
        else:
            raise AssertionError("Unsafe or ambiguous data was accepted")
    print("SELFTEST OK")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        message = str(exc)
        for secret in (os.environ.get("BOT_TOKEN"), os.environ.get("CHAT_ID")):
            if secret:
                message = message.replace(secret, "[REDACTED]")
        print("FATAL", type(exc).__name__, message[:250])
        sys.exit(1)

