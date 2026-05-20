from __future__ import annotations

# --- Team alias mapping for DB odds lookup ---
TEAM_ALIASES = {
    "como": "Como 1907",
    "parma": "Parma Calcio 1913",
    "genoa": "Genoa CFC",
    "pisa": "AC Pisa 1909",
    "cagliari": "Cagliari Calcio",
    "verona": "Hellas Verona",
    "internazionale": "Inter",
}

# --- Odds lookup from SQLite DB ---
import sqlite3


def get_fixture_odds(conn, match_date, home_team, away_team, allow_nearby_date: bool = False):
    """Return latest 1X2 odds dict for a fixture date/team tuple, or None."""
    canon_home = canonicalize_team_name(home_team)
    canon_away = canonicalize_team_name(away_team)
    db_home = TEAM_ALIASES.get(canon_home, home_team)
    db_away = TEAM_ALIASES.get(canon_away, away_team)

    cur = conn.cursor()
    cur.execute(
        """
        SELECT o.home_moneyline, o.draw_moneyline, o.away_moneyline
        FROM soccer_betting_odds o
        JOIN soccer_matches m ON o.match_id = m.match_id
        JOIN soccer_teams th ON m.home_team_id = th.team_id
        JOIN soccer_teams ta ON m.away_team_id = ta.team_id
        WHERE date(m.match_date) = ?
          AND th.name = ?
          AND ta.name = ?
        ORDER BY o.odds_date DESC
        LIMIT 1
        """,
        (match_date, db_home, db_away),
    )
    row = cur.fetchone()
    if row:
        return {"HOME": row[0], "DRAW": row[1], "AWAY": row[2]}

    if allow_nearby_date:
        cur.execute(
            """
            SELECT o.home_moneyline, o.draw_moneyline, o.away_moneyline
            FROM soccer_betting_odds o
            JOIN soccer_matches m ON o.match_id = m.match_id
            JOIN soccer_teams th ON m.home_team_id = th.team_id
            JOIN soccer_teams ta ON m.away_team_id = ta.team_id
            WHERE th.name = ?
              AND ta.name = ?
              AND abs(julianday(date(m.match_date)) - julianday(?)) <= 3
            ORDER BY abs(julianday(date(m.match_date)) - julianday(?)) ASC, o.odds_date DESC
            LIMIT 1
            """,
            (db_home, db_away, match_date, match_date),
        )
        row = cur.fetchone()
        if row:
            return {"HOME": row[0], "DRAW": row[1], "AWAY": row[2]}
    return None


def get_fixture_totals_odds(conn, match_date, home_team, away_team, line, allow_nearby_date: bool = False):
    """Return latest totals odds dict for an exact line, or None."""
    canon_home = canonicalize_team_name(home_team)
    canon_away = canonicalize_team_name(away_team)
    db_home = TEAM_ALIASES.get(canon_home, home_team)
    db_away = TEAM_ALIASES.get(canon_away, away_team)

    cur = conn.cursor()
    cur.execute(
        """
        SELECT o.over_under, o.over_odds, o.under_odds
        FROM soccer_betting_odds o
        JOIN soccer_matches m ON o.match_id = m.match_id
        JOIN soccer_teams th ON m.home_team_id = th.team_id
        JOIN soccer_teams ta ON m.away_team_id = ta.team_id
        WHERE date(m.match_date) = ?
          AND th.name = ?
          AND ta.name = ?
          AND abs(o.over_under - ?) < 0.0001
        ORDER BY o.odds_date DESC
        LIMIT 1
        """,
        (match_date, db_home, db_away, float(line)),
    )
    row = cur.fetchone()
    if row:
        return {"line": float(row[0]), "OVER": row[1], "UNDER": row[2]}

    if allow_nearby_date:
        cur.execute(
            """
            SELECT o.over_under, o.over_odds, o.under_odds
            FROM soccer_betting_odds o
            JOIN soccer_matches m ON o.match_id = m.match_id
            JOIN soccer_teams th ON m.home_team_id = th.team_id
            JOIN soccer_teams ta ON m.away_team_id = ta.team_id
            WHERE th.name = ?
              AND ta.name = ?
              AND abs(o.over_under - ?) < 0.0001
              AND abs(julianday(date(m.match_date)) - julianday(?)) <= 3
            ORDER BY abs(julianday(date(m.match_date)) - julianday(?)) ASC, o.odds_date DESC
            LIMIT 1
            """,
            (db_home, db_away, float(line), match_date, match_date),
        )
        row = cur.fetchone()
        if row:
            return {"line": float(row[0]), "OVER": row[1], "UNDER": row[2]}
    return None
#!/usr/bin/env python3
"""Serie A value-pick publisher and scorer for Bluesky.

Workflow:
1) Fetch today's or tomorrow's Serie A fixtures.
2) Ask ChatGPT/Claude/Gemini for one value pick per match.
3) Ask user for own pick.
4) Post user pick to Bluesky and AI picks as a reply.
5) Next day, score all picks and reply with a daily scoreboard.
"""

import argparse
import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib import error, request

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
TRACK_FILE = DATA_DIR / "posted_picks.json"
PICK_CACHE_FILE = DATA_DIR / "ai_pick_cache.json"

ESPN_SERIE_A_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/soccer/ita.1/scoreboard"
OPENAI_ENDPOINT = "https://api.openai.com/v1/responses"
ANTHROPIC_ENDPOINT = "https://api.anthropic.com/v1/messages"
GEMINI_ENDPOINT_TMPL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
BSKY_HOST = "https://bsky.social"

ALLOWED_PICKS = {"HOME", "DRAW", "AWAY"}
ALLOWED_TOTAL_SIDES = {"OVER", "UNDER"}


@dataclass
class Fixture:
    fixture_id: str
    date_utc: str
    home: str
    away: str
    home_score: int | None
    away_score: int | None
    state: str


def ensure_storage() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not TRACK_FILE.exists():
        TRACK_FILE.write_text("[]\n", encoding="utf-8")
    if not PICK_CACHE_FILE.exists():
        PICK_CACHE_FILE.write_text("{}\n", encoding="utf-8")


def load_tracking() -> list[dict[str, Any]]:
    ensure_storage()
    raw = TRACK_FILE.read_text(encoding="utf-8").strip()
    if not raw:
        return []
    return json.loads(raw)


def save_tracking(items: list[dict[str, Any]]) -> None:
    ensure_storage()
    TRACK_FILE.write_text(json.dumps(items, indent=2) + "\n", encoding="utf-8")


def load_pick_cache() -> dict[str, dict[str, Any]]:
    ensure_storage()
    raw = PICK_CACHE_FILE.read_text(encoding="utf-8").strip()
    if not raw:
        return {}
    parsed = json.loads(raw)
    if isinstance(parsed, dict):
        return parsed
    return {}


def save_pick_cache(items: dict[str, dict[str, Any]]) -> None:
    ensure_storage()
    PICK_CACHE_FILE.write_text(json.dumps(items, indent=2) + "\n", encoding="utf-8")


def load_simulated_results(path: str) -> dict[str, dict[str, int]]:
    """Load simulated final scores keyed by fixture reference for test scoring.

    Supported JSON formats:
    - object map: {"<fixture_id_or_matchup>": {"home_score": 2, "away_score": 1}}
    - list rows: [{"fixture_id": "<id>", "home_score": 2, "away_score": 1}]
      or [{"matchup": "Home vs Away", "home_score": 2, "away_score": 1}]
    """
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as e:
        raise SystemExit(f"Could not read simulated results file '{path}': {e}")

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise SystemExit(f"Invalid JSON in simulated results file '{path}': {e}")

    normalized: dict[str, dict[str, int]] = {}

    if isinstance(parsed, dict):
        items = parsed.items()
        for fixture_ref, row in items:
            if not isinstance(row, dict):
                raise SystemExit("Each simulated result must be an object with home_score and away_score.")
            try:
                home_score = int(row["home_score"])
                away_score = int(row["away_score"])
            except (KeyError, TypeError, ValueError):
                raise SystemExit(
                    f"Invalid simulated result for fixture '{fixture_ref}'. "
                    "Expected numeric home_score and away_score."
                )
            normalized[str(fixture_ref).strip()] = {"home_score": home_score, "away_score": away_score}
        return normalized

    if isinstance(parsed, list):
        for idx, row in enumerate(parsed, start=1):
            if not isinstance(row, dict):
                raise SystemExit(f"Invalid simulated result entry #{idx}: expected object.")
            fixture_ref = str(row.get("fixture_id", "")).strip()
            if not fixture_ref:
                fixture_ref = str(row.get("matchup", "")).strip()
            if not fixture_ref:
                fixture_ref = str(row.get("fixture", "")).strip()
            if not fixture_ref:
                fixture_ref = str(row.get("match", "")).strip()
            if not fixture_ref:
                raise SystemExit(
                    f"Invalid simulated result entry #{idx}: missing fixture reference "
                    "(fixture_id or matchup)."
                )
            try:
                home_score = int(row["home_score"])
                away_score = int(row["away_score"])
            except (KeyError, TypeError, ValueError):
                raise SystemExit(
                    f"Invalid simulated result entry #{idx} for fixture '{fixture_ref}'. "
                    "Expected numeric home_score and away_score."
                )
            normalized[fixture_ref] = {"home_score": home_score, "away_score": away_score}
        return normalized

    raise SystemExit("Simulated results JSON must be an object map or a list of fixture rows.")


def resolve_simulated_fixture_ref(fixture_ref: str, fixtures_by_id: dict[str, Fixture]) -> str:
    raw_ref = str(fixture_ref).strip()
    if not raw_ref:
        return ""

    if raw_ref in fixtures_by_id:
        return raw_ref

    matchup = split_matchup_text(raw_ref)
    if not matchup:
        return ""

    wanted_key = canonical_matchup_key(matchup[0], matchup[1])
    matched_ids = [
        fixture_id
        for fixture_id, fixture in fixtures_by_id.items()
        if canonical_matchup_key(fixture.home, fixture.away) == wanted_key
    ]

    if len(matched_ids) == 1:
        return matched_ids[0]

    if len(matched_ids) > 1:
        raise SystemExit(
            f"Ambiguous simulated fixture reference '{raw_ref}'. "
            "Multiple fixtures matched; use fixture_id instead."
        )

    return ""


def today_utc() -> date:
    # Use local date so --day today/tomorrow matches user-facing fixture schedules.
    return datetime.now().date()


def resolve_day(day: str) -> datetime.date:
    from datetime import datetime, timedelta

    if day == "yesterday":
        return datetime.now().date() - timedelta(days=1)
    elif day == "today":
        return datetime.now().date()
    elif day == "tomorrow":
        return datetime.now().date() + timedelta(days=1)
    else:
        try:
            return datetime.strptime(day, "%Y-%m-%d").date()
        except ValueError:
            raise ValueError(f"Invalid date format: {day}. Use 'yesterday', 'today', 'tomorrow', or 'YYYY-MM-DD'.")


def iso_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def clamp_post(text: str, max_chars: int = 300) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def at_uri_to_post_url(uri: str) -> str:
    if not uri.startswith("at://"):
        return ""

    parts = uri[len("at://") :].split("/")
    if len(parts) != 3:
        return ""

    repo, collection, rkey = parts
    if not repo or collection != "app.bsky.feed.post" or not rkey:
        return ""

    return f"https://bsky.app/profile/{repo}/post/{rkey}"


def http_json(method: str, url: str, headers: dict[str, str] | None = None, body: dict[str, Any] | None = None) -> dict[str, Any]:
    req_headers = {"Content-Type": "application/json"}
    if headers:
        req_headers.update(headers)

    payload = None
    if body is not None:
        payload = json.dumps(body).encode("utf-8")

    req = request.Request(url=url, method=method, headers=req_headers, data=payload)
    try:
        with request.urlopen(req, timeout=30) as res:
            raw = res.read().decode("utf-8")
            if not raw:
                return {}
            return json.loads(raw)
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise SystemExit(f"HTTP {exc.code} for {url}: {detail}") from exc
    except error.URLError as exc:
        raise SystemExit(f"Network error for {url}: {exc}") from exc


def fetch_serie_a_fixtures(target_day: date) -> list[Fixture]:
    date_key = target_day.strftime("%Y%m%d")
    url = f"{ESPN_SERIE_A_SCOREBOARD}?dates={date_key}"
    payload = http_json("GET", url)

    fixtures: list[Fixture] = []
    for event in payload.get("events", []):
        competition = (event.get("competitions") or [{}])[0]
        competitors = competition.get("competitors") or []

        home_team = next((c for c in competitors if c.get("homeAway") == "home"), None)
        away_team = next((c for c in competitors if c.get("homeAway") == "away"), None)
        if not home_team or not away_team:
            continue

        def parse_score(team: dict[str, Any]) -> int | None:
            val = team.get("score")
            if val is None or val == "":
                return None
            try:
                return int(val)
            except ValueError:
                return None

        fixtures.append(
            Fixture(
                fixture_id=str(event.get("id")),
                date_utc=str(event.get("date")),
                home=str(home_team.get("team", {}).get("displayName", "Home")),
                away=str(away_team.get("team", {}).get("displayName", "Away")),
                home_score=parse_score(home_team),
                away_score=parse_score(away_team),
                state=str(event.get("status", {}).get("type", {}).get("state", "pre")),
            )
        )

    fixtures.sort(key=lambda f: f.date_utc)
    return fixtures


def fixture_local_hour(fixture: Fixture) -> int:
    raw = str(fixture.date_utc)
    dt_utc = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return dt_utc.astimezone().hour


def fixture_session_label(fixture: Fixture) -> str:
    return "morning" if fixture_local_hour(fixture) < 12 else "afternoon"


def filter_fixtures_by_session(fixtures: list[Fixture], session_filter: str) -> list[Fixture]:
    if session_filter == "all":
        return fixtures
    return [f for f in fixtures if fixture_session_label(f) == session_filter]


def safe_json_extract(text: str) -> dict[str, Any]:
    text = text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"No JSON object found in model output: {text}")
    return json.loads(text[start : end + 1])


def format_line_value(line: float) -> str:
    if float(line).is_integer():
        return str(int(line))
    return f"{line:.1f}"


def parse_totals_selection(text: str) -> tuple[str, list[float]] | None:
    raw = " ".join(text.strip().upper().split())
    match = re.match(r"^(OVER|UNDER)\s+([0-9]+(?:\.[05])?)\s*(?:,\s*([0-9]+(?:\.[05])?))?$", raw)
    if not match:
        return None

    side = str(match.group(1))
    first = float(match.group(2))
    second_raw = match.group(3)

    if second_raw is None:
        return side, [first]

    second = float(second_raw)
    leg1, leg2 = (first, second) if first <= second else (second, first)

    # Quarter-line splits are the only valid two-leg totals inputs:
    # X,X.5 or X.5,X+1
    if abs(leg2 - leg1 - 0.5) > 1e-9:
        return None
    if leg1 % 0.5 != 0 or leg2 % 0.5 != 0:
        return None

    return side, [leg1, leg2]


def parse_pick_spec(raw_pick: str) -> dict[str, Any] | None:
    normalized = raw_pick.strip().upper()
    if normalized in ALLOWED_PICKS:
        return {"market": "1X2", "pick": normalized}

    totals = parse_totals_selection(normalized)
    if totals is None:
        return None

    side, line_legs = totals
    line_text = ",".join(format_line_value(v) for v in line_legs)
    return {
        "market": "TOTAL_GOALS",
        "pick": f"{side} {line_text}",
        "side": side,
        "line_legs": line_legs,
    }


def settle_totals_pick(total_goals: int, side: str, line_legs: list[float]) -> tuple[float, float]:
    if side not in ALLOWED_TOTAL_SIDES or not line_legs:
        return 0.0, 0.0

    leg_stake = 1.0 / float(len(line_legs))
    win_units = 0.0
    push_units = 0.0

    for line in line_legs:
        if side == "OVER":
            if total_goals > line:
                win_units += leg_stake
            elif total_goals == line:
                push_units += leg_stake
        else:
            if total_goals < line:
                win_units += leg_stake
            elif total_goals == line:
                push_units += leg_stake

    return win_units, push_units


def settle_totals_leg(total_goals: int, side: str, line: float) -> tuple[float, float]:
    if side not in ALLOWED_TOTAL_SIDES:
        return 0.0, 0.0
    if side == "OVER":
        if total_goals > line:
            return 1.0, 0.0
        if total_goals == line:
            return 0.0, 1.0
        return 0.0, 0.0

    if total_goals < line:
        return 1.0, 0.0
    if total_goals == line:
        return 0.0, 1.0
    return 0.0, 0.0


def pick_spec_from_ai_pick_data(pick_data: dict[str, Any]) -> dict[str, Any] | None:
    market = str(pick_data.get("market", "")).strip().upper()
    pick = str(pick_data.get("pick", "")).strip().upper()

    if pick in ALLOWED_PICKS:
        return {"market": "1X2", "pick": pick}

    if market in {"TOTAL_GOALS", "TOTALS"}:
        side = str(pick_data.get("side", pick)).strip().upper()
        line_legs_raw = pick_data.get("line_legs")
        if isinstance(line_legs_raw, list) and line_legs_raw:
            line_legs = sorted(float(v) for v in line_legs_raw)
            line_text = ",".join(format_line_value(v) for v in line_legs)
            return {
                "market": "TOTAL_GOALS",
                "pick": f"{side} {line_text}",
                "side": side,
                "line_legs": line_legs,
            }

        line_raw = pick_data.get("line")
        if line_raw is not None and side in ALLOWED_TOTAL_SIDES:
            parsed = parse_totals_selection(f"{side} {line_raw}")
            if parsed is not None:
                parsed_side, parsed_legs = parsed
                line_text = ",".join(format_line_value(v) for v in parsed_legs)
                return {
                    "market": "TOTAL_GOALS",
                    "pick": f"{parsed_side} {line_text}",
                    "side": parsed_side,
                    "line_legs": parsed_legs,
                }

    parsed_pick = parse_pick_spec(pick)
    return parsed_pick


def pick_prompt(home: str, away: str) -> str:
    return (
        "Return only valid JSON with this exact schema: "
        '{"market":"1X2|TOTAL_GOALS","pick":"HOME|DRAW|AWAY|OVER|UNDER","line":"required for TOTAL_GOALS (e.g. 2, 2.5, 2,2.5, 2.5,3)","reason":"short text","confidence":0}. '
        f"Match: {home} vs {away}. Choose the single best value pick across BOTH markets (1X2 or TOTAL_GOALS). "
        "Do not default to 1X2 if a totals angle looks better. "
        "If market is TOTAL_GOALS, pick must be OVER or UNDER and line must be provided."
    )


def normalize_pick(raw: dict[str, Any], provider: str) -> dict[str, Any]:
    market_raw = str(raw.get("market", "")).strip().upper()
    pick = str(raw.get("pick", "")).strip().upper()

    parsed_spec: dict[str, Any] | None = None

    if pick in ALLOWED_PICKS:
        parsed_spec = {"market": "1X2", "pick": pick}
    elif market_raw in {"TOTAL_GOALS", "TOTALS"}:
        side = str(raw.get("side", pick)).strip().upper()
        line_raw = raw.get("line", "")
        line_text = str(line_raw).strip()
        if side in ALLOWED_TOTAL_SIDES and line_text:
            parsed_spec = parse_pick_spec(f"{side} {line_text}")
        else:
            parsed_spec = parse_pick_spec(pick)
    else:
        parsed_spec = parse_pick_spec(pick)

    if parsed_spec is None:
        raise SystemExit(f"{provider} returned invalid pick: {pick}")

    confidence = raw.get("confidence", 0)
    try:
        confidence_int = int(confidence)
    except (TypeError, ValueError):
        confidence_int = 0
    confidence_int = max(0, min(100, confidence_int))

    return {
        "pick": parsed_spec["pick"],
        "market": parsed_spec["market"],
        "side": parsed_spec.get("side", ""),
        "line_legs": parsed_spec.get("line_legs", []),
        "line": ",".join(format_line_value(v) for v in parsed_spec.get("line_legs", [])),
        "reason": str(raw.get("reason", "No reason provided")).strip(),
        "confidence": confidence_int,
        "available": True,
    }


def unavailable_pick(reason: str) -> dict[str, Any]:
    return {
        "pick": "",
        "market": "1X2",
        "reason": reason,
        "confidence": 0,
        "available": False,
    }


def get_provider_pick(provider: str, picker: Any, home: str, away: str) -> dict[str, Any]:
    try:
        return picker(home, away)
    except (SystemExit, Exception) as exc:
        reason = str(exc).strip() or f"{provider} failed"
        print(f"Warning: {provider} unavailable for {home} vs {away}: {reason}")
        return unavailable_pick(reason)


def provider_model(provider: str) -> str:
    if provider == "ChatGPT":
        return os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
    if provider == "Claude":
        return os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    if provider == "Gemini":
        return os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    return ""


def pick_cache_key(provider: str, fixture: Fixture, target_day: date) -> str:
    parts = [
        target_day.isoformat(),
        fixture.fixture_id,
        fixture.home,
        fixture.away,
        provider,
        provider_model(provider),
    ]
    return "||".join(parts)


def get_or_fetch_provider_pick(
    provider: str,
    picker: Any,
    fixture: Fixture,
    target_day: date,
    cache: dict[str, dict[str, Any]],
    use_cache: bool,
) -> tuple[dict[str, Any], bool]:
    cache_key = pick_cache_key(provider, fixture, target_day)
    if use_cache:
        cached = cache.get(cache_key, {})
        cached_pick = cached.get("pick")
        if isinstance(cached_pick, dict) and cached_pick.get("available", True):
            print(f"Using cached {provider} pick for {fixture.home} vs {fixture.away}")
            return cached_pick, False

    fetched_pick = get_provider_pick(provider, picker, fixture.home, fixture.away)
    if use_cache and fetched_pick.get("available", True):
        cache[cache_key] = {
            "provider": provider,
            "model": provider_model(provider),
            "fixture_id": fixture.fixture_id,
            "fixture_date": target_day.isoformat(),
            "home": fixture.home,
            "away": fixture.away,
            "pick": fetched_pick,
            "cached_at": iso_now(),
        }
        return fetched_pick, True

    return fetched_pick, False


def format_ai_pick_line(name: str, pick_data: dict[str, Any]) -> str:
    pick_text = str(pick_data.get("pick_text", "")).strip()
    if pick_text:
        return f"- {name}: {pick_text} ({int(pick_data.get('confidence', 0))}%)"

    pick = str(pick_data.get("pick", "")).strip().upper()
    if pick in ALLOWED_PICKS:
        return f"- {name}: {pick} ({int(pick_data.get('confidence', 0))}%)"
    parsed_spec = pick_spec_from_ai_pick_data(pick_data)
    if parsed_spec and parsed_spec.get("market") == "TOTAL_GOALS":
        return f"- {name}: {parsed_spec.get('pick')} ({int(pick_data.get('confidence', 0))}%)"
    return f"- {name}: unavailable"


def format_score_line(name: str, pick_data: dict[str, Any], outcome: str) -> str:
    pick = str(pick_data.get("pick", "")).strip().upper()
    if pick not in ALLOWED_PICKS:
        return f"- {name}: unavailable"

    ok = pick == outcome
    emoji = "✅" if ok else "❌"
    return f"- {name}: {pick} {emoji}"


def format_scoreboard_line(name: str, wins: int, total: int) -> str:
    if total <= 0:
        return f"{name}: n/a"
    ratio = wins / total
    if ratio >= 0.67:
        status = "🟢"
    elif ratio >= 0.34:
        status = "🟡"
    else:
        status = "🔴"
    wins_text = format_line_value(float(wins)) if isinstance(wins, float) else str(wins)
    return f"{name}: {wins_text}/{total} {status}"


def build_scoreboard_reply(day_label: str, participant_rows: list[tuple[str, int, int]]) -> str:
    lines = [f"Scoreboard for {day_label}", ""]
    lines.extend(format_scoreboard_line(name, wins, total) for name, wins, total in participant_rows)
    return "\n".join(lines)


def build_scoreboard_reply_with_payout(
    day_label: str,
    participant_rows: list[tuple[str, float, int, float, float, float]],
) -> str:
    lines = [f"Scoreboard for {day_label}", ""]
    for name, wins, total, odds_bets, returns, missing_odds in participant_rows:
        status = "🟢" if wins and wins == total else ("🟡" if wins else "🔴")
        if odds_bets > 0:
            net = returns - float(odds_bets)
            roi = (net / float(odds_bets)) * 100.0
            metrics = f" | return: {returns:.2f}u | ROI: {roi:+.1f}%"
        else:
            metrics = " | return: 0.00u | ROI: n/a"

        if missing_odds > 0:
            metrics += f" | partial: missing odds for {format_line_value(float(missing_odds))}u"

        lines.append(f"{name}: {format_line_value(float(wins))}/{total} {status}{metrics}")
    return "\n".join(lines)


def human_pick_text(pick: str, home: str, away: str) -> str:
    if pick == "HOME":
        return f"{home} to win"
    if pick == "AWAY":
        return f"{away} to win"
    if pick == "DRAW":
        return "Draw"

    totals = parse_totals_selection(pick)
    if totals is not None:
        side, line_legs = totals
        line_text = ",".join(format_line_value(v) for v in line_legs)
        return f"{side.title()} {line_text} goals"

    return ""


def enrich_pick_for_display(pick_data: dict[str, Any], home: str, away: str) -> dict[str, Any]:
    enriched = dict(pick_data)
    existing_text = str(enriched.get("pick_text", "")).strip()
    if existing_text:
        return enriched

    pick = str(enriched.get("pick", "")).strip().upper()
    text = human_pick_text(pick, home, away)
    if text:
        enriched["pick_text"] = text
    return enriched


def ask_openai(home: str, away: str) -> dict[str, Any]:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit("OPENAI_API_KEY is required")

    body = {
        "model": os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
        "input": pick_prompt(home, away),
    }
    payload = http_json("POST", OPENAI_ENDPOINT, headers={"Authorization": f"Bearer {api_key}"}, body=body)

    text = payload.get("output_text")
    if not text:
        output = payload.get("output") or []
        if output and isinstance(output, list):
            content = output[0].get("content") or []
            if content and isinstance(content, list):
                text = str(content[0].get("text", ""))
    if not text:
        text = json.dumps(payload)
    return normalize_pick(safe_json_extract(text), "ChatGPT")


def ask_claude(home: str, away: str) -> dict[str, Any]:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise SystemExit("ANTHROPIC_API_KEY is required")

    body = {
        "model": os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
        "max_tokens": 200,
        "messages": [{"role": "user", "content": pick_prompt(home, away)}],
    }
    payload = http_json(
        "POST",
        ANTHROPIC_ENDPOINT,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        body=body,
    )

    content = payload.get("content") or []
    text = ""
    if content and isinstance(content, list):
        text = str(content[0].get("text", ""))
    return normalize_pick(safe_json_extract(text), "Claude")


def ask_gemini(home: str, away: str) -> dict[str, Any]:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise SystemExit("GEMINI_API_KEY is required")

    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    endpoint = GEMINI_ENDPOINT_TMPL.format(model=model, api_key=api_key)
    body = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": pick_prompt(home, away)}],
            }
        ]
    }

    payload = http_json("POST", endpoint, body=body)
    text = ""
    candidates = payload.get("candidates") or []
    if candidates:
        parts = candidates[0].get("content", {}).get("parts") or []
        if parts:
            text = str(parts[0].get("text", ""))
    return normalize_pick(safe_json_extract(text), "Gemini")


def bsky_login() -> dict[str, str]:
    handle = os.getenv("BSKY_HANDLE")
    app_password = os.getenv("BSKY_APP_PASSWORD")
    if not handle or not app_password:
        raise SystemExit("BSKY_HANDLE and BSKY_APP_PASSWORD are required")

    payload = http_json(
        "POST",
        f"{BSKY_HOST}/xrpc/com.atproto.server.createSession",
        body={"identifier": handle, "password": app_password},
    )
    return {
        "did": str(payload["did"]),
        "access_jwt": str(payload["accessJwt"]),
    }


def bsky_create_post(session: dict[str, str], text: str, reply_to: dict[str, str] | None = None) -> dict[str, str]:
    record: dict[str, Any] = {
        "$type": "app.bsky.feed.post",
        "text": clamp_post(text),
        "createdAt": iso_now(),
    }
    if reply_to:
        record["reply"] = {
            "root": {"uri": reply_to["root_uri"], "cid": reply_to["root_cid"]},
            "parent": {"uri": reply_to["parent_uri"], "cid": reply_to["parent_cid"]},
        }

    payload = http_json(
        "POST",
        f"{BSKY_HOST}/xrpc/com.atproto.repo.createRecord",
        headers={"Authorization": f"Bearer {session['access_jwt']}"},
        body={
            "repo": session["did"],
            "collection": "app.bsky.feed.post",
            "record": record,
        },
    )

    return {"uri": str(payload["uri"]), "cid": str(payload["cid"])}


def x_create_client() -> Any:
    """Create an authenticated X (Twitter) API v2 client."""
    try:
        import tweepy  # noqa: PLC0415
    except ImportError:
        raise SystemExit("tweepy is required for X posting. Install it with: pip install tweepy")

    consumer_key = os.getenv("X_CONSUMER_KEY")
    consumer_secret = os.getenv("X_CONSUMER_SECRET")
    access_token = os.getenv("X_ACCESS_TOKEN")
    access_token_secret = os.getenv("X_ACCESS_TOKEN_SECRET")
    if not all([consumer_key, consumer_secret, access_token, access_token_secret]):
        raise SystemExit(
            "X API credentials are required: X_CONSUMER_KEY, X_CONSUMER_SECRET, "
            "X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET"
        )
    return tweepy.Client(
        consumer_key=consumer_key,
        consumer_secret=consumer_secret,
        access_token=access_token,
        access_token_secret=access_token_secret,
    )


def x_create_post(client: Any, text: str, reply_to_tweet_id: str | None = None) -> dict[str, str]:
    """Post a tweet to X. Returns {"id": tweet_id}."""
    clamped = clamp_post(text, max_chars=280)
    kwargs: dict[str, Any] = {"text": clamped}
    if reply_to_tweet_id:
        kwargs["in_reply_to_tweet_id"] = reply_to_tweet_id
    try:
        response = client.create_tweet(**kwargs)
    except Exception as exc:  # pragma: no cover - network/API failure path
        response = getattr(exc, "response", None)
        detail_parts: list[str] = []
        if response is not None:
            status = getattr(response, "status_code", None)
            if status is not None:
                detail_parts.append(f"status={status}")
            body_text = getattr(response, "text", "")
            if body_text:
                detail_parts.append(body_text.strip())
        detail = f" ({' | '.join(detail_parts)})" if detail_parts else ""
        raise SystemExit(f"X post failed: {exc.__class__.__name__}: {exc}{detail}") from exc
    return {"id": str(response.data["id"])}


def x_post_url(tweet_id: str) -> str:
    handle = os.getenv("X_HANDLE", "")
    if not handle or not tweet_id:
        return ""
    handle = handle.lstrip("@")
    return f"https://x.com/{handle}/status/{tweet_id}"


def outcome_from_scores(home_score: int | None, away_score: int | None) -> str | None:
    if home_score is None or away_score is None:
        return None
    if home_score > away_score:
        return "HOME"
    if home_score < away_score:
        return "AWAY"
    return "DRAW"


def ask_user_pick(home: str, away: str) -> str:
    while True:
        entered = input(f"Your pick for {home} vs {away} [HOME/DRAW/AWAY]: ").strip().upper()
        if entered in ALLOWED_PICKS:
            return entered
        print("Invalid pick. Use HOME, DRAW, or AWAY.")


def normalize_picks_file_key(raw: str) -> str:
    return " ".join(raw.strip().lower().split())


def canonicalize_team_name(raw: str) -> str:
    cleaned = compact_team_name(raw)
    cleaned = cleaned.replace("/", " ").replace("-", " ").lower()
    cleaned = re.sub(r"[^a-z0-9\s]", " ", cleaned)
    normalized = " ".join(cleaned.split())
    aliases = {
        "inter": "internazionale",
    }
    return aliases.get(normalized, normalized)


def split_matchup_text(raw: str) -> tuple[str, str] | None:
    text = " ".join(raw.strip().split())
    parts = re.split(r"\s+vs\s+|\s+v\s+|\s*/\s*", text, maxsplit=1, flags=re.IGNORECASE)
    if len(parts) != 2:
        return None
    left = parts[0].strip()
    right = parts[1].strip()
    if not left or not right:
        return None
    return left, right


def canonical_matchup_key(home: str, away: str) -> str:
    return f"{canonicalize_team_name(home)}|{canonicalize_team_name(away)}"


def resolve_structured_pick_for_fixture(structured_picks: dict[str, str], fixture: Fixture) -> str:
    fixture_key = normalize_picks_file_key(f"{fixture.home} vs {fixture.away}")
    fixture_canonical_key = canonical_matchup_key(fixture.home, fixture.away)
    pick = structured_picks.get(fixture_key, "")
    if not pick:
        pick = structured_picks.get(fixture_canonical_key, "")
    return pick


def validate_structured_picks_for_fixtures(structured_picks: dict[str, str], fixtures: list[Fixture]) -> None:
    missing: list[str] = []
    for fixture in fixtures:
        pick = resolve_structured_pick_for_fixture(structured_picks, fixture)
        if not pick:
            missing.append(f"{fixture.home} vs {fixture.away}")

    if missing:
        joined = "; ".join(missing)
        raise SystemExit(
            "Invalid [PICKS] section: missing or unmatched picks for fixtures: "
            f"{joined}. Use 'Home vs Away = HOME|DRAW|AWAY' or 'Home vs Away = OVER 2.5'. "
            "Aborting before AI calls, posting, cache writes, or tracking updates."
        )


def extract_structured_picks(raw_text: str) -> dict[str, str]:
    """Extract picks from [PICKS] / [/PICKS] markers.
    
    Returns dict mapping normalized team names to normalized pick text.
    """
    picks_by_fixture_key: dict[str, str] = {}
    
    sections = re.findall(r"\[PICKS\](.*?)\[/PICKS\]", raw_text, flags=re.IGNORECASE | re.DOTALL)
    for picks_section in sections:
        for line in picks_section.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue

            if "=" not in stripped:
                continue

            fixture_part, pick_part = stripped.split("=", 1)
            fixture_part = fixture_part.strip()
            pick_part = " ".join(pick_part.strip().upper().split())

            if not fixture_part or not pick_part:
                continue

            parsed_pick = parse_pick_spec(pick_part)
            if parsed_pick is None:
                raise SystemExit(
                    f"Invalid pick in [PICKS] section: '{pick_part}' "
                    "(must be HOME/DRAW/AWAY or OVER/UNDER with valid totals line)"
                )

            normalized_pick = str(parsed_pick.get("pick", pick_part))

            fixture_key = normalize_picks_file_key(fixture_part)
            picks_by_fixture_key[fixture_key] = normalized_pick

            matchup = split_matchup_text(fixture_part)
            if matchup is not None:
                parsed_home, parsed_away = matchup
                picks_by_fixture_key[canonical_matchup_key(parsed_home, parsed_away)] = normalized_pick
    
    return picks_by_fixture_key


def picks_text_for_session(raw_text: str, session_filter: str) -> str:
    block_pattern = re.compile(r"\[(MORNING|AFTERNOON)\](.*?)\[/\1\]", flags=re.IGNORECASE | re.DOTALL)
    blocks = {name.lower(): content.strip() for name, content in block_pattern.findall(raw_text)}

    if session_filter == "all":
        if not blocks:
            return raw_text
        parts: list[str] = []
        for key in ("morning", "afternoon"):
            value = blocks.get(key)
            if value:
                parts.append(value)
        return "\n\n".join(parts).strip()

    if blocks:
        selected = blocks.get(session_filter, "").strip()
        if not selected:
            raise SystemExit(
                f"Picks file does not contain a [{session_filter.upper()}]...[/" 
                f"{session_filter.upper()}] section."
            )
        return selected

    return raw_text


def load_picks_file(path_str: str, session_filter: str = "all") -> dict[str, Any]:
    path = Path(path_str)
    if not path.exists():
        raise SystemExit(f"Picks file not found: {path}")

    raw_full_text = path.read_text(encoding="utf-8")
    scoped_text = picks_text_for_session(raw_full_text, session_filter)
    
    # Extract structured picks from [PICKS] / [/PICKS] markers
    has_structured_picks_section = bool(re.search(r"\[PICKS\].*?\[/PICKS\]", scoped_text, flags=re.IGNORECASE | re.DOTALL))
    structured_picks = extract_structured_picks(scoped_text)
    
    # Remove [PICKS] / [/PICKS] section from text for posting
    raw_text_for_posting = re.sub(r"\[PICKS\].*?\[/PICKS\]", "", scoped_text, flags=re.IGNORECASE | re.DOTALL).strip()

    picks_by_key: dict[str, str] = {}
    picks_in_order_lines: list[str] = []
    picks_in_order_blocks: list[str] = []
    current_block: list[str] = []
    non_keyed_raw_lines: list[str] = []

    def flush_block() -> None:
        if current_block:
            picks_in_order_blocks.append("\n".join(current_block).strip())
            current_block.clear()

    in_picks_block = False
    for line_no, raw_line in enumerate(scoped_text.splitlines(), start=1):
        stripped = raw_line.strip()
        if stripped.startswith("#"):
            continue

        upper = stripped.upper()
        if upper == "[PICKS]":
            in_picks_block = True
            continue
        if upper == "[/PICKS]":
            in_picks_block = False
            continue
        if in_picks_block:
            continue

        if "=" not in raw_line:
            if stripped == "":
                non_keyed_raw_lines.append("")
                flush_block()
            else:
                line_text = raw_line.rstrip()
                non_keyed_raw_lines.append(line_text)
                picks_in_order_lines.append(line_text)
                current_block.append(line_text)
            continue

        key, value = raw_line.split("=", 1)
        key_norm = normalize_picks_file_key(key)
        pick_text = value.strip()
        if not pick_text:
            raise SystemExit(f"Invalid empty pick text at line {line_no}.")
        if not key_norm:
            raise SystemExit(f"Invalid empty key at line {line_no}.")

        picks_by_key[key_norm] = pick_text

    flush_block()

    non_keyed_full_text = "\n".join(non_keyed_raw_lines).strip()

    if not picks_by_key and not picks_in_order_lines and not non_keyed_full_text:
        raise SystemExit("Picks file is empty. Add at least one non-comment pick entry.")

    return {
        "by_key": picks_by_key,
        "by_order_lines": picks_in_order_lines,
        "by_order_blocks": picks_in_order_blocks,
        "non_keyed_full_text": non_keyed_full_text,
        "raw_text": raw_text_for_posting,
        "structured_picks": structured_picks,
        "has_structured_picks_section": has_structured_picks_section,
    }


def pick_from_file(picks_data: dict[str, Any], fixture: Fixture, order_index: int, fixture_count: int) -> str | None:
    picks_by_key = picks_data.get("by_key", {})
    picks_by_order_lines = picks_data.get("by_order_lines", [])
    picks_by_order_blocks = picks_data.get("by_order_blocks", [])
    non_keyed_full_text = str(picks_data.get("non_keyed_full_text", "")).strip()

    by_id = picks_by_key.get(normalize_picks_file_key(fixture.fixture_id))
    if by_id:
        return by_id

    matchup_key = normalize_picks_file_key(f"{fixture.home} vs {fixture.away}")
    by_matchup = picks_by_key.get(matchup_key)
    if by_matchup:
        return by_matchup

    if fixture_count == 1 and non_keyed_full_text:
        return non_keyed_full_text

    if len(picks_by_order_blocks) == fixture_count and order_index < len(picks_by_order_blocks):
        return str(picks_by_order_blocks[order_index])

    if len(picks_by_order_lines) == fixture_count and order_index < len(picks_by_order_lines):
        return str(picks_by_order_lines[order_index])

    if order_index < len(picks_by_order_lines):
        return str(picks_by_order_lines[order_index])

    return None


def normalize_my_pick_for_scoring(raw_pick_text: str) -> dict[str, Any] | None:
    return parse_pick_spec(raw_pick_text)


def short_pick_for_reply(pick_data: dict[str, Any], home: str, away: str) -> str:
    parsed = pick_spec_from_ai_pick_data(pick_data)
    if not parsed:
        return "N/A"

    market = str(parsed.get("market", "")).upper()
    pick = str(parsed.get("pick", "")).strip().upper()

    if market == "1X2" and pick == "HOME":
        return home
    if market == "1X2" and pick == "AWAY":
        return away
    if market == "1X2" and pick == "DRAW":
        return "Draw"

    if market == "TOTAL_GOALS":
        side = str(parsed.get("side", "")).upper()
        line_legs = [float(v) for v in parsed.get("line_legs", [])]
        if side in ALLOWED_TOTAL_SIDES and line_legs:
            line_text = ",".join(format_line_value(v) for v in line_legs)
            return f"{side.title()} {line_text}"

    return "N/A"


def short_pick_code_for_reply(pick_data: dict[str, Any]) -> str:
    parsed = pick_spec_from_ai_pick_data(pick_data)
    if not parsed:
        return "?"

    market = str(parsed.get("market", "")).upper()
    pick = str(parsed.get("pick", "")).strip().upper()

    if market == "1X2" and pick == "HOME":
        return "H"
    if market == "1X2" and pick == "AWAY":
        return "A"
    if market == "1X2" and pick == "DRAW":
        return "D"

    if market == "TOTAL_GOALS":
        side = str(parsed.get("side", "")).upper()
        line_legs = [float(v) for v in parsed.get("line_legs", [])]
        if side in ALLOWED_TOTAL_SIDES and line_legs:
            line_text = ",".join(format_line_value(v) for v in line_legs)
            return f"{'O' if side == 'OVER' else 'U'}{line_text}"

    return "?"


def compact_team_name(name: str) -> str:
    cleaned = " ".join(name.split())
    lowered = cleaned.lower()
    for prefix in ("ac ", "as ", "hellas ", "fc "):
        if lowered.startswith(prefix):
            cleaned = cleaned[len(prefix) :]
            break
    return cleaned


def build_aggregate_ai_reply(prefetched: list[tuple[Fixture, dict[str, Any], dict[str, Any], dict[str, Any]]]) -> str:
    lines = ["AI picks (1X2) C=ChatGPT A=Claude G=Gemini"]
    for fixture, gpt_pick, claude_pick, gemini_pick in prefetched:
        home = compact_team_name(fixture.home)
        away = compact_team_name(fixture.away)
        lines.append(
            f"{home}/{away}: "
            f"C {short_pick_for_reply(gpt_pick, home, away)}, "
            f"A {short_pick_for_reply(claude_pick, home, away)}, "
            f"G {short_pick_for_reply(gemini_pick, home, away)}"
        )
    return "\n".join(lines)


def build_x_aggregate_ai_reply(prefetched: list[tuple[Fixture, dict[str, Any], dict[str, Any], dict[str, Any]]]) -> str:
    """Build a compact X reply for the multi-match picks-file flow."""
    lines = ["AI picks 1X2 C/A/G"]
    for fixture, gpt_pick, claude_pick, gemini_pick in prefetched:
        home = compact_team_name(fixture.home)
        away = compact_team_name(fixture.away)
        lines.append(
            f"{home}/{away}: "
            f"{short_pick_code_for_reply(gpt_pick)}/"
            f"{short_pick_code_for_reply(claude_pick)}/"
            f"{short_pick_code_for_reply(gemini_pick)}"
        )
    return "\n".join(lines)


def publish_for_day(args: argparse.Namespace) -> None:
    target_day = resolve_day(args.day)
    session_filter = getattr(args, "session", "all")
    test_mode = bool(getattr(args, "test_mode", False))
    picks_data = load_picks_file(args.picks_file, session_filter=session_filter) if getattr(args, "picks_file", None) else None

    fixtures = filter_fixtures_by_session(fetch_serie_a_fixtures(target_day), session_filter)
    if not fixtures:
        if session_filter == "all":
            print("No Serie A fixtures found for that day.")
        else:
            print(f"No Serie A fixtures found for that day in session '{session_filter}'.")
        return

    if picks_data is not None and picks_data.get("has_structured_picks_section", False):
        validate_structured_picks_for_fixtures(picks_data.get("structured_picks", {}), fixtures)

    dry_run = bool(getattr(args, "dry_run", False))
    use_cache = (not bool(getattr(args, "no_cache", False))) and (not test_mode)
    session = None
    tracking = load_tracking()
    pick_cache = load_pick_cache() if use_cache else {}
    cache_changed = False
    prefetched: list[tuple[Fixture, dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    provider_failures: list[str] = []

    print(f"Using session filter: {session_filter}")

    for fixture in fixtures:
        print(f"\nMatch: {fixture.home} vs {fixture.away}")
        gpt_pick, gpt_cached = get_or_fetch_provider_pick("ChatGPT", ask_openai, fixture, target_day, pick_cache, use_cache)
        claude_pick, claude_cached = get_or_fetch_provider_pick("Claude", ask_claude, fixture, target_day, pick_cache, use_cache)
        gemini_pick, gemini_cached = get_or_fetch_provider_pick("Gemini", ask_gemini, fixture, target_day, pick_cache, use_cache)

        gpt_pick = enrich_pick_for_display(gpt_pick, fixture.home, fixture.away)
        claude_pick = enrich_pick_for_display(claude_pick, fixture.home, fixture.away)
        gemini_pick = enrich_pick_for_display(gemini_pick, fixture.home, fixture.away)
        cache_changed = cache_changed or gpt_cached or claude_cached or gemini_cached

        print("ChatGPT:", gpt_pick)
        print("Claude:", claude_pick)
        print("Gemini:", gemini_pick)

        failed = [
            name
            for name, pick_data in (
                ("ChatGPT", gpt_pick),
                ("Claude", claude_pick),
                ("Gemini", gemini_pick),
            )
            if not pick_data.get("available", True)
        ]
        if failed:
            provider_failures.append(f"{fixture.home} vs {fixture.away}: {', '.join(failed)}")
            continue

        prefetched.append((fixture, gpt_pick, claude_pick, gemini_pick))

    if provider_failures:
        details = "; ".join(provider_failures)
        raise SystemExit(
            "Aborting publish because not all AI providers returned picks. "
            f"No prompts were shown and no posts were created. Failures: {details}"
        )

    if use_cache and cache_changed:
        save_pick_cache(pick_cache)

    platform = getattr(args, "platform", "both")
    post_bluesky = platform in ("bluesky", "both")
    post_x = platform in ("x", "both")

    if not dry_run:
        session = bsky_login() if post_bluesky else None
        x_client = x_create_client() if post_x else None
    else:
        session = None
        x_client = None

    if picks_data is not None:
        structured_picks = picks_data.get("structured_picks", {})
        
        # Build picks_resolved with structured picks for scoring
        picks_resolved: list[tuple[Fixture, dict[str, Any], dict[str, Any], dict[str, Any], str, str]] = []
        for fixture, gpt_pick, claude_pick, gemini_pick in prefetched:
            pick_1x2_code = resolve_structured_pick_for_fixture(structured_picks, fixture)
            # my_pick_text is just the fixture name (for display), my_pick_scoring is the 1X2 code
            picks_resolved.append((fixture, gpt_pick, claude_pick, gemini_pick, "", pick_1x2_code))

        root_text = str(picks_data.get("raw_text", "")).strip()
        if not root_text:
            raise SystemExit("Picks file content is empty after parsing.")

        print("Using full picks-file text as single root post body.")

        if dry_run:
            root_post = {"uri": f"dryrun://root/{target_day.isoformat()}", "cid": "dryrun-root-cid"}
            x_root_post = {"id": f"dryrun-x-root-{target_day.isoformat()}"}
            print("\n[DRY-RUN] ROOT POST (this is the main post):")
            print("----- ROOT POST TEXT START -----")
            print(clamp_post(root_text))
            print("----- ROOT POST TEXT END -----")
        else:
            root_post = bsky_create_post(session, root_text) if post_bluesky else {"uri": "", "cid": ""}
            x_root_post = x_create_post(x_client, root_text) if post_x else {"id": ""}

        reply_text = build_aggregate_ai_reply([(f, g, c, m) for f, g, c, m, _, _ in picks_resolved])
        x_reply_text = build_x_aggregate_ai_reply([(f, g, c, m) for f, g, c, m, _, _ in picks_resolved])
        if dry_run:
            ai_reply = {"uri": f"dryrun://reply/{target_day.isoformat()}", "cid": "dryrun-reply-cid"}
            x_ai_reply = {"id": f"dryrun-x-reply-{target_day.isoformat()}"}
            print("\n[DRY-RUN] REPLY POST (this is posted as a reply to the root):")
            print("----- REPLY POST TEXT START -----")
            print(clamp_post(reply_text))
            print("----- REPLY POST TEXT END -----")
        else:
            ai_reply = bsky_create_post(
                session,
                reply_text,
                reply_to={
                    "root_uri": root_post["uri"],
                    "root_cid": root_post["cid"],
                    "parent_uri": root_post["uri"],
                    "parent_cid": root_post["cid"],
                },
            ) if post_bluesky else {"uri": "", "cid": ""}
            x_ai_reply = x_create_post(x_client, reply_text, reply_to_tweet_id=x_root_post["id"] or None) if post_x else {"id": ""}

        for fixture, gpt_pick, claude_pick, gemini_pick, my_pick_text, my_pick_scoring in picks_resolved:
            tracking.append(
                {
                    "fixture_id": fixture.fixture_id,
                    "fixture_date": target_day.isoformat(),
                    "home": fixture.home,
                    "away": fixture.away,
                    "test_mode": test_mode,
                    "session": fixture_session_label(fixture),
                    "my_pick": my_pick_scoring,
                    "my_pick_text": my_pick_text,
                    "ai_picks": {
                        "chatgpt": gpt_pick,
                        "claude": claude_pick,
                        "gemini": gemini_pick,
                    },
                    "root_post": root_post,
                    "ai_reply_post": ai_reply,
                    "x_root_post": x_root_post,
                    "x_ai_reply_post": x_ai_reply,
                    "scored": False,
                }
            )

        if dry_run:
            if post_bluesky:
                print(f"[DRY-RUN] Would post Bluesky root: {root_post['uri']}")
                print(f"[DRY-RUN] Would post Bluesky AI reply: {ai_reply['uri']}")
            if post_x:
                print(f"[DRY-RUN] Would post X root tweet (id: {x_root_post['id']})")
                print(f"[DRY-RUN] Would post X AI reply tweet (id: {x_ai_reply['id']})")
        else:
            if post_bluesky:
                print(f"Posted Bluesky root: {root_post['uri']}")
                print(f"Posted Bluesky AI reply: {ai_reply['uri']}")
            if post_x:
                print(f"Posted X root tweet: {x_post_url(x_root_post['id']) or x_root_post['id']}")
                print(f"Posted X AI reply tweet: {x_post_url(x_ai_reply['id']) or x_ai_reply['id']}")

        save_tracking(tracking)
        if dry_run:
            print("\nDone dry-run publish. No posts were created.")
        else:
            print("\nDone publishing picks.")
def score_for_date_range(args: argparse.Namespace) -> None:
    start_date_str = getattr(args, "start_date", None)
    end_date_str = getattr(args, "end_date", None)
    day_str = getattr(args, "day", None)
    test_mode = bool(getattr(args, "test_mode", False))

    if day_str:
        start_date = end_date = resolve_day(day_str)
    elif start_date_str and end_date_str:
        start_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()
        end_date = datetime.strptime(end_date_str, "%Y-%m-%d").date()
    else:
        raise SystemExit("Scoring requires either --day or both --start-date and --end-date.")

    session_filter = getattr(args, "session", "all")
    recalculate = getattr(args, "recalculate", False)
    dry_run = bool(getattr(args, "dry_run", False))
    simulated_results_path = getattr(args, "sim_results_file", None)

    if simulated_results_path and (not test_mode or not dry_run):
        raise SystemExit("--sim-results-file is only allowed with --test-mode and --dry-run.")

    tracking = load_tracking()

    if test_mode:
        source_items = [i for i in tracking if bool(i.get("test_mode", False))]
    else:
        source_items = [i for i in tracking if not bool(i.get("test_mode", False))]
    
    # Filter items within the date range
    date_items = [
        i for i in source_items
        if start_date <= datetime.strptime(i.get("fixture_date"), "%Y-%m-%d").date() <= end_date
    ]

    if not recalculate:
        date_items = [i for i in date_items if not i.get("scored")]
    if session_filter != "all":
        date_items = [i for i in date_items if str(i.get("session", "")).strip().lower() in ("", session_filter)]
    
    if not date_items:
        print(f"No unscored tracked posts found for the specified date range.")
        return

    # Deduplicate by fixture_id
    seen: dict[str, dict[str, Any]] = {}
    for item in date_items:
        fixture_id = str(item.get("fixture_id", ""))
        if not fixture_id:
            continue
        
        current = seen.get(fixture_id)
        if current is None:
            seen[fixture_id] = item
        else:
            current_root_uri = str(current.get("root_post", {}).get("uri", ""))
            item_root_uri = str(item.get("root_post", {}).get("uri", ""))
            current_is_real = current_root_uri.startswith("at://")
            item_is_real = item_root_uri.startswith("at://")

            if item_is_real and not current_is_real:
                seen[fixture_id] = item
            elif current_is_real or not item_is_real:
                current_my_pick = str(current.get("my_pick", "")).strip()
                item_my_pick = str(item.get("my_pick", "")).strip()
                if item_my_pick and not current_my_pick:
                    seen[fixture_id] = item
    
    unique_items = list(seen.values())

    # Fetch all fixtures for the entire date range at once
    all_fixtures: dict[str, Fixture] = {}
    current_date = start_date
    while current_date <= end_date:
        for f in fetch_serie_a_fixtures(current_date):
            all_fixtures[f.fixture_id] = f
        current_date += timedelta(days=1)

    if simulated_results_path:
        simulated_results = load_simulated_results(simulated_results_path)
        unresolved_refs: list[str] = []
        for fixture_ref, result in simulated_results.items():
            fixture_id = resolve_simulated_fixture_ref(fixture_ref, all_fixtures)
            if not fixture_id:
                unresolved_refs.append(str(fixture_ref))
                continue

            fixture = all_fixtures.get(fixture_id)
            if not fixture:
                unresolved_refs.append(str(fixture_ref))
                continue

            all_fixtures[fixture_id] = Fixture(
                fixture_id=fixture.fixture_id,
                date_utc=fixture.date_utc,
                home=fixture.home,
                away=fixture.away,
                home_score=int(result["home_score"]),
                away_score=int(result["away_score"]),
                state="post",
            )

        if unresolved_refs:
            unresolved_text = ", ".join(unresolved_refs)
            raise SystemExit(
                "Could not resolve simulated fixture reference(s): "
                f"{unresolved_text}. Use fixture_id or 'Home vs Away' for fixtures in the selected date range."
            )

    odds_db_path = getattr(args, "odds_db", None)
    odds_conn = None
    if odds_db_path:
        try:
            odds_conn = sqlite3.connect(odds_db_path)
        except Exception as e:
            print(f"[Warning] Could not open odds DB: {e}")
            odds_conn = None

    final_rows: list[dict[str, Any]] = []
    pending_rows: list[str] = []

    for item in unique_items:
        fixture = all_fixtures.get(str(item.get("fixture_id")))
        if not fixture:
            pending_rows.append(f"{item.get('home')} vs {item.get('away')}: fixture not found in feed.")
            continue
        
        if fixture.state != "post":
            pending_rows.append(f"{fixture.home} vs {fixture.away}: match not final yet (state={fixture.state}).")
            continue

        outcome = outcome_from_scores(fixture.home_score, fixture.away_score)
        if not outcome:
            pending_rows.append(f"{fixture.home} vs {fixture.away}: score unavailable.")
            continue

        odds = None
        if odds_conn:
            try:
                odds = get_fixture_odds(
                    odds_conn,
                    fixture.date_utc[:10],
                    fixture.home,
                    fixture.away,
                    allow_nearby_date=True,
                )
            except Exception as e:
                print(f"[Warning] Odds lookup failed for {fixture.home} vs {fixture.away}: {e}")
                odds = None

        final_rows.append({"item": item, "fixture": fixture, "outcome": outcome, "odds": odds})

    if pending_rows:
        if odds_conn:
            odds_conn.close()
        print("Waiting for all tracked matches to finish before posting the scoreboard:")
        for row in pending_rows:
            print(f"- {row}")
        return

    if not final_rows:
        if odds_conn:
            odds_conn.close()
        print("No final tracked matches found for the specified date range.")
        return

    total_matches = len(final_rows)
    participant_rows: list[tuple[str, float, int, float, float, float]] = []
    for label, key in (("Minvest", "my_pick"), ("Gemini", "gemini"), ("Claude", "claude"), ("ChatGPT", "chatgpt")):
        wins = 0.0
        odds_bets = 0.0
        returns = 0.0
        graded_picks = 0.0
        for row in final_rows:
            outcome = row["outcome"]
            item = row["item"]
            fixture = row["fixture"]
            odds = row.get("odds")
            if key == "my_pick":
                pick_spec = normalize_my_pick_for_scoring(str(item.get("my_pick", "")))
            else:
                pick_data = item.get("ai_picks", {}).get(key, {})
                pick_spec = pick_spec_from_ai_pick_data(pick_data)

            if not pick_spec:
                continue

            graded_picks += 1.0

            market = str(pick_spec.get("market", "")).upper()
            if market == "1X2":
                pick = str(pick_spec.get("pick", "")).strip().upper()
                has_odds = bool(odds and pick in odds and odds[pick] is not None)
                if has_odds:
                    odds_bets += 1.0

                if pick == outcome:
                    wins += 1.0
                    if has_odds:
                        odd = float(odds[pick])
                        if odd > 0:
                            returns += 1 + (odd / 100)
                        else:
                            returns += 1 + (100 / abs(odd))
                continue

            if market == "TOTAL_GOALS":
                side = str(pick_spec.get("side", "")).upper()
                line_legs = [float(v) for v in pick_spec.get("line_legs", [])]
                if side not in ALLOWED_TOTAL_SIDES or not line_legs:
                    continue

                total_goals = int(fixture.home_score or 0) + int(fixture.away_score or 0)
                win_units, push_units = settle_totals_pick(total_goals, side, line_legs)
                wins += win_units

                # Prefer a direct quarter-line price for split picks when available
                # (e.g. OVER 2.5,3 can be priced from OVER 2.75).
                if len(line_legs) == 2 and abs(line_legs[1] - line_legs[0] - 0.5) < 1e-9:
                    quarter_line = (line_legs[0] + line_legs[1]) / 2.0
                    quarter_odds = None
                    if odds_conn:
                        quarter_odds = get_fixture_totals_odds(
                            odds_conn,
                            fixture.date_utc[:10],
                            fixture.home,
                            fixture.away,
                            quarter_line,
                            allow_nearby_date=True,
                        )

                    quarter_odd = None
                    if quarter_odds and side in quarter_odds:
                        quarter_odd = quarter_odds.get(side)

                    if quarter_odd is not None:
                        odds_bets += 1.0
                        odd_value = float(quarter_odd)
                        if odd_value > 0:
                            win_return = 1 + (odd_value / 100)
                        else:
                            win_return = 1 + (100 / abs(odd_value))

                        returns += (win_units * win_return) + push_units
                        continue

                leg_stake = 1.0 / float(len(line_legs))
                for leg_line in line_legs:
                    totals_odds = None
                    if odds_conn:
                        totals_odds = get_fixture_totals_odds(
                            odds_conn,
                            fixture.date_utc[:10],
                            fixture.home,
                            fixture.away,
                            leg_line,
                            allow_nearby_date=True,
                        )

                    leg_odd = None
                    if totals_odds and side in totals_odds:
                        leg_odd = totals_odds.get(side)

                    if leg_odd is None:
                        continue

                    odds_bets += leg_stake
                    leg_win_units, leg_push_units = settle_totals_leg(total_goals, side, leg_line)

                    odd_value = float(leg_odd)
                    if odd_value > 0:
                        win_return = 1 + (odd_value / 100)
                    else:
                        win_return = 1 + (100 / abs(odd_value))

                    returns += (leg_stake * leg_win_units * win_return) + (leg_stake * leg_push_units)
                continue

        missing_odds = max(0, graded_picks - odds_bets)
        participant_rows.append((label, wins, total_matches, odds_bets, returns, missing_odds))

    if odds_conn:
        odds_conn.close()

    if start_date == end_date:
        score_label = start_date.strftime("%Y-%m-%d")
    else:
        score_label = f"{start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}"
    
    if session_filter != "all":
        score_label += f" ({session_filter})"

    score_text = build_scoreboard_reply_with_payout(score_label, participant_rows)

    platform = getattr(args, "platform", "both")
    post_bluesky = platform in ("bluesky", "both")
    post_x = platform in ("x", "both")

    # For date range scoring, we don't post a reply to a specific thread
    if start_date != end_date:
        print("\n--- Aggregated Scoreboard ---")
        print(score_text)
        print("---------------------------\n")
        if not dry_run:
            # Mark items as scored
            for row in final_rows:
                item = row["item"]
                if not recalculate:
                    item["scored"] = True
            save_tracking(tracking)
        print("Done scoring for date range.")
        return

    root_item = final_rows[0]["item"]
    root_uri = str(root_item.get("root_post", {}).get("uri", ""))
    root_cid = str(root_item.get("root_post", {}).get("cid", ""))
    ai_reply_uri = str(root_item.get("ai_reply_post", {}).get("uri", ""))
    ai_reply_cid = str(root_item.get("ai_reply_post", {}).get("cid", ""))
    x_ai_reply_id = str(root_item.get("x_ai_reply_post", {}).get("id", "")) or None
    root_url = at_uri_to_post_url(root_uri) if root_uri else None
    ai_reply_url = at_uri_to_post_url(ai_reply_uri) if ai_reply_uri else None

    if not dry_run:
        session = bsky_login() if post_bluesky else None
        x_client = x_create_client() if post_x else None
    else:
        session = None
        x_client = None

    def preview_pick_text(pick_spec: dict[str, Any] | None, fixture: Fixture) -> str:
        if not pick_spec:
            return "N/A"
        market = str(pick_spec.get("market", "")).upper()
        if market == "1X2":
            pick = str(pick_spec.get("pick", "")).strip().upper()
            return human_pick_text(pick, fixture.home, fixture.away) or pick or "N/A"
        if market == "TOTAL_GOALS":
            return str(pick_spec.get("pick", "")).strip() or "N/A"
        return "N/A"

    if dry_run:
        score_post = {"uri": f"dryrun://score/{start_date.isoformat()}", "cid": "dryrun-score-cid"}
        x_score_post = {"id": f"dryrun-x-score-{start_date.isoformat()}"}
        print("\n[DRY-RUN] Picks being scored:")
        for row in final_rows:
            fixture = row["fixture"]
            item = row["item"]
            my_spec = normalize_my_pick_for_scoring(str(item.get("my_pick", "")))
            gemini_spec = pick_spec_from_ai_pick_data(item.get("ai_picks", {}).get("gemini", {}))
            claude_spec = pick_spec_from_ai_pick_data(item.get("ai_picks", {}).get("claude", {}))
            chatgpt_spec = pick_spec_from_ai_pick_data(item.get("ai_picks", {}).get("chatgpt", {}))

            print(f"- {fixture.home} vs {fixture.away}")
            print(f"  Minvest: {preview_pick_text(my_spec, fixture)}")
            print(f"  Gemini: {preview_pick_text(gemini_spec, fixture)}")
            print(f"  Claude: {preview_pick_text(claude_spec, fixture)}")
            print(f"  ChatGPT: {preview_pick_text(chatgpt_spec, fixture)}")
        print("\n[DRY-RUN] Scoreboard reply preview:")
        print(clamp_post(score_text))
        if post_bluesky:
            print("[DRY-RUN] Bluesky reply target (AI picks reply):")
            if ai_reply_url:
                print(f"- post_url: {ai_reply_url}")
            else:
                print("- post_url: unavailable (dry-run placeholder)")
            print(f"[DRY-RUN] Would create Bluesky scoreboard reply: {score_post['uri']}")
        if post_x:
            print(f"[DRY-RUN] Would create X scoreboard reply (replying to tweet id: {x_ai_reply_id or 'none'})")
    else:
        score_post = bsky_create_post(
            session,
            score_text,
            reply_to={
                "root_uri": root_uri,
                "root_cid": root_cid,
                "parent_uri": ai_reply_uri,
                "parent_cid": ai_reply_cid,
            },
        ) if post_bluesky else {"uri": "", "cid": ""}
        x_score_post = x_create_post(x_client, score_text, reply_to_tweet_id=x_ai_reply_id) if post_x else {"id": ""}

        for row in final_rows:
            item = row["item"]
            fixture = row["fixture"]
            outcome = row["outcome"]
            if not recalculate:
                item["scored"] = True
            item["result"] = {
                "outcome": outcome,
                "home_score": fixture.home_score,
                "away_score": fixture.away_score,
                "score_post": score_post,
                "x_score_post": x_score_post,
                "scored_at": iso_now(),
            }

        if post_bluesky:
            print(f"Posted Bluesky scoreboard reply: {score_post['uri']}")
        if post_x:
            print(f"Posted X scoreboard reply: {x_post_url(x_score_post['id']) or x_score_post['id']}")

    if not dry_run and not recalculate:
        save_tracking(tracking)

    if dry_run:
        print("Done dry-run scoring. No posts were created and tracking was not marked scored.")
    elif recalculate:
        print("Done recalculating scores. Tracking was not persisted to database.")
    else:
        print("Done scoring.")


def list_fixtures_cmd(args: argparse.Namespace) -> None:

    target_day = resolve_day(args.day)
    fixtures = fetch_serie_a_fixtures(target_day)
    if not fixtures:
        print("No Serie A fixtures found for that day.")
        return

    print(f"Serie A fixtures for {target_day.isoformat()}")
    print("-" * 45)
    for f in fixtures:
        dt = f.date_utc.replace("T", " ").replace("Z", " UTC")
        print(f"{f.fixture_id} | {dt} | {f.home} vs {f.away} | state={f.state}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serie A picks publisher + Bluesky scoring tool")
    subparsers = parser.add_subparsers(dest="command", required=True)

    fixtures = subparsers.add_parser("fixtures", help="List Serie A fixtures")
    fixtures.add_argument("--day", default="today", help="Target day: 'yesterday', 'today', 'tomorrow', or 'YYYY-MM-DD'")
    fixtures.set_defaults(func=list_fixtures_cmd)

    publish = subparsers.add_parser("publish", help="Generate picks and publish to Bluesky")
    publish.add_argument("--day", default="today", help="Target day: 'yesterday', 'today', 'tomorrow', or 'YYYY-MM-DD'")
    publish.add_argument("--session", default="all", choices=["all", "morning", "afternoon"], help="Session filter by local kickoff time (default: all)")
    publish.add_argument("--dry-run", action="store_true", help="Print post previews without posting to Bluesky")
    publish.add_argument("--test-mode", action="store_true", help="Disable cache I/O and tag created tracking rows as test data")
    publish.add_argument("--no-cache", action="store_true", help="Disable local AI-pick cache and force fresh provider calls")
    publish.add_argument("--picks-file", help="Path to text file with your picks text. Supports keyed lines (fixture_id=...) and free-form lines in fixture order")
    publish.add_argument("--platform", default="both", choices=["bluesky", "x", "both"], help="Platform(s) to post to (default: both)")

    publish.add_argument(
        "--odds-db",
        help="Path to SQLite odds database for payout-based scoring (optional)",
        default=None,
    )
    publish.set_defaults(func=publish_for_day)

    score = subparsers.add_parser("score", help="Post result score updates to existing threads")
    score.add_argument("--day", help="Target day: 'yesterday', 'today', 'tomorrow', or 'YYYY-MM-DD'")
    score.add_argument("--start-date", help="Start date for scoring range (YYYY-MM-DD)")
    score.add_argument("--end-date", help="End date for scoring range (YYYY-MM-DD)")
    score.add_argument("--session", default="all", choices=["all", "morning", "afternoon"], help="Session filter by local kickoff time (default: all)")
    score.add_argument("--dry-run", action="store_true", help="Print score reply previews without posting to Bluesky")
    score.add_argument("--test-mode", action="store_true", help="Score only test-mode tracking rows (normal runs ignore test rows)")
    score.add_argument(
        "--sim-results-file",
        dest="sim_results_file",
        help="Path to JSON fixture score overrides (test-mode + dry-run only).",
    )
    score.add_argument("--platform", default="both", choices=["bluesky", "x", "both"], help="Platform(s) to post to (default: both)")

    score.add_argument(
        "--odds-db",
        help="Path to SQLite odds database for payout-based scoring (optional)",
        default=None,
    )
    score.add_argument(
        "--recalculate",
        action="store_true",
        help="Recalculate scores for already-scored items without posting or updating tracking",
    )
    score.set_defaults(func=score_for_date_range)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
