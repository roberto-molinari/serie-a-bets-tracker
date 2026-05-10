#!/usr/bin/env python3
"""Serie A value-pick publisher and scorer for Bluesky.

Workflow:
1) Fetch today's or tomorrow's Serie A fixtures.
2) Ask ChatGPT/Claude/Gemini for one value pick per match.
3) Ask user for own pick.
4) Post user pick to Bluesky and AI picks as a reply.
5) Next day, score all picks and reply with results.
"""

from __future__ import annotations

import argparse
import json
import os
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


def today_utc() -> date:
    # Use local date so --day today/tomorrow matches user-facing fixture schedules.
    return datetime.now().date()


def resolve_day(day_flag: str) -> date:
    if day_flag == "today":
        return today_utc()
    if day_flag == "tomorrow":
        return today_utc() + timedelta(days=1)
    if day_flag == "yesterday":
        return today_utc() - timedelta(days=1)
    raise ValueError(f"Unsupported day: {day_flag}")


def iso_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def clamp_post(text: str, max_chars: int = 300) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


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


def safe_json_extract(text: str) -> dict[str, Any]:
    text = text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"No JSON object found in model output: {text}")
    return json.loads(text[start : end + 1])


def pick_prompt(home: str, away: str) -> str:
    return (
        "Return only valid JSON with this exact schema: "
        '{"pick":"HOME|DRAW|AWAY","market":"1X2","reason":"short text","confidence":0}. '
        f"Match: {home} vs {away}. Give one value betting pick in 1X2 market."
    )


def normalize_pick(raw: dict[str, Any], provider: str) -> dict[str, Any]:
    pick = str(raw.get("pick", "")).strip().upper()
    if pick not in ALLOWED_PICKS:
        raise SystemExit(f"{provider} returned invalid pick: {pick}")

    confidence = raw.get("confidence", 0)
    try:
        confidence_int = int(confidence)
    except (TypeError, ValueError):
        confidence_int = 0
    confidence_int = max(0, min(100, confidence_int))

    return {
        "pick": pick,
        "market": "1X2",
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
    return f"- {name}: unavailable"


def format_score_line(name: str, pick_data: dict[str, Any], outcome: str) -> str:
    pick = str(pick_data.get("pick", "")).strip().upper()
    if pick not in ALLOWED_PICKS:
        return f"- {name}: unavailable"

    ok = pick == outcome
    emoji = "✅" if ok else "❌"
    return f"- {name}: {pick} {emoji}"


def human_pick_text(pick: str, home: str, away: str) -> str:
    if pick == "HOME":
        return f"{home} to win"
    if pick == "AWAY":
        return f"{away} to win"
    if pick == "DRAW":
        return "Draw"
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


def load_picks_file(path_str: str) -> dict[str, Any]:
    path = Path(path_str)
    if not path.exists():
        raise SystemExit(f"Picks file not found: {path}")

    picks_by_key: dict[str, str] = {}
    picks_in_order_lines: list[str] = []
    picks_in_order_blocks: list[str] = []
    current_block: list[str] = []
    non_keyed_raw_lines: list[str] = []

    def flush_block() -> None:
        if current_block:
            picks_in_order_blocks.append("\n".join(current_block).strip())
            current_block.clear()

    for line_no, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = raw_line.strip()
        if stripped.startswith("#"):
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
        "raw_text": path.read_text(encoding="utf-8").strip(),
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


def normalize_my_pick_for_scoring(raw_pick_text: str) -> str:
    normalized = raw_pick_text.strip().upper()
    if normalized in ALLOWED_PICKS:
        return normalized
    return ""


def short_pick_for_reply(pick_data: dict[str, Any], home: str, away: str) -> str:
    pick = str(pick_data.get("pick", "")).strip().upper()
    if pick == "HOME":
        return home
    if pick == "AWAY":
        return away
    if pick == "DRAW":
        return "Draw"
    return "N/A"


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


def publish_for_day(args: argparse.Namespace) -> None:
    target_day = resolve_day(args.day)
    fixtures = fetch_serie_a_fixtures(target_day)
    if not fixtures:
        print("No Serie A fixtures found for that day.")
        return

    dry_run = bool(getattr(args, "dry_run", False))
    use_cache = not bool(getattr(args, "no_cache", False))
    session = None
    tracking = load_tracking()
    pick_cache = load_pick_cache() if use_cache else {}
    cache_changed = False
    prefetched: list[tuple[Fixture, dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    provider_failures: list[str] = []
    picks_data = load_picks_file(args.picks_file) if getattr(args, "picks_file", None) else None

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

    if not dry_run:
        session = bsky_login()

    if picks_data is not None:
        picks_resolved: list[tuple[Fixture, dict[str, Any], dict[str, Any], dict[str, Any], str, str]] = [
            (fixture, gpt_pick, claude_pick, gemini_pick, "", "")
            for fixture, gpt_pick, claude_pick, gemini_pick in prefetched
        ]

        root_text = str(picks_data.get("raw_text", "")).strip()
        if not root_text:
            raise SystemExit("Picks file content is empty after parsing.")

        print("Using full picks-file text as single root post body.")

        if dry_run:
            root_post = {"uri": f"dryrun://root/{target_day.isoformat()}", "cid": "dryrun-root-cid"}
            print("\n[DRY-RUN] ROOT POST (this is the main post):")
            print("----- ROOT POST TEXT START -----")
            print(clamp_post(root_text))
            print("----- ROOT POST TEXT END -----")
        else:
            root_post = bsky_create_post(session, root_text)

        reply_text = build_aggregate_ai_reply([(f, g, c, m) for f, g, c, m, _, _ in picks_resolved])
        if dry_run:
            ai_reply = {"uri": f"dryrun://reply/{target_day.isoformat()}", "cid": "dryrun-reply-cid"}
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
            )

        for fixture, gpt_pick, claude_pick, gemini_pick, my_pick_text, my_pick_scoring in picks_resolved:
            tracking.append(
                {
                    "fixture_id": fixture.fixture_id,
                    "fixture_date": target_day.isoformat(),
                    "home": fixture.home,
                    "away": fixture.away,
                    "my_pick": my_pick_scoring,
                    "my_pick_text": my_pick_text,
                    "ai_picks": {
                        "chatgpt": gpt_pick,
                        "claude": claude_pick,
                        "gemini": gemini_pick,
                    },
                    "root_post": root_post,
                    "ai_reply_post": ai_reply,
                    "scored": False,
                }
            )

        if dry_run:
            print(f"[DRY-RUN] Would post root: {root_post['uri']}")
            print(f"[DRY-RUN] Would post AI reply: {ai_reply['uri']}")
        else:
            print(f"Posted root: {root_post['uri']}")
            print(f"Posted AI reply: {ai_reply['uri']}")

        save_tracking(tracking)
        if dry_run:
            print("\nDone dry-run publish. No Bluesky posts were created.")
        else:
            print("\nDone publishing picks.")
        return

    for idx, (fixture, gpt_pick, claude_pick, gemini_pick) in enumerate(prefetched):
        my_pick_text = ask_user_pick(fixture.home, fixture.away)

        my_pick_scoring = normalize_my_pick_for_scoring(my_pick_text)

        if my_pick_scoring:
            my_pick_line = f"My pick (1X2): {my_pick_scoring}"
        else:
            my_pick_line = f"My pick: {my_pick_text}"
        root_text = f"Serie A value pick - {fixture.home} vs {fixture.away}\n{my_pick_line}"
        if dry_run:
            root_post = {"uri": f"dryrun://root/{fixture.fixture_id}", "cid": "dryrun-root-cid"}
            print("\n[DRY-RUN] ROOT POST (this is the main post):")
            print("----- ROOT POST TEXT START -----")
            print(clamp_post(root_text))
            print("----- ROOT POST TEXT END -----")
        else:
            root_post = bsky_create_post(session, root_text)

        reply_text = (
            "AI value picks (1X2):\n"
            f"{format_ai_pick_line('ChatGPT', gpt_pick)}\n"
            f"{format_ai_pick_line('Claude', claude_pick)}\n"
            f"{format_ai_pick_line('Gemini', gemini_pick)}"
        )

        if dry_run:
            ai_reply = {"uri": f"dryrun://reply/{fixture.fixture_id}", "cid": "dryrun-reply-cid"}
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
            )

        tracking.append(
            {
                "fixture_id": fixture.fixture_id,
                "fixture_date": target_day.isoformat(),
                "home": fixture.home,
                "away": fixture.away,
                "my_pick": my_pick_scoring,
                "my_pick_text": my_pick_text,
                "ai_picks": {
                    "chatgpt": gpt_pick,
                    "claude": claude_pick,
                    "gemini": gemini_pick,
                },
                "root_post": root_post,
                "ai_reply_post": ai_reply,
                "scored": False,
            }
        )

        if dry_run:
            print(f"[DRY-RUN] Would post root: {root_post['uri']}")
            print(f"[DRY-RUN] Would post AI reply: {ai_reply['uri']}")
        else:
            print(f"Posted root: {root_post['uri']}")
            print(f"Posted AI reply: {ai_reply['uri']}")

    save_tracking(tracking)
    if dry_run:
        print("\nDone dry-run publish. No Bluesky posts were created.")
    else:
        print("\nDone publishing picks.")


def score_for_day(args: argparse.Namespace) -> None:
    target_day = resolve_day(args.day)
    tracking = load_tracking()
    day_items = [i for i in tracking if i.get("fixture_date") == target_day.isoformat() and not i.get("scored")]
    if not day_items:
        print("No unscored tracked posts for that day.")
        return

    fixtures = {f.fixture_id: f for f in fetch_serie_a_fixtures(target_day)}
    dry_run = bool(getattr(args, "dry_run", False))
    session = None if dry_run else bsky_login()

    for item in day_items:
        fixture = fixtures.get(str(item.get("fixture_id")))
        if not fixture:
            print(f"Skipping {item.get('home')} vs {item.get('away')}: fixture not found in feed.")
            continue
        if fixture.state != "post":
            print(f"Skipping {fixture.home} vs {fixture.away}: match not final yet (state={fixture.state}).")
            continue

        outcome = outcome_from_scores(fixture.home_score, fixture.away_score)
        if not outcome:
            print(f"Skipping {fixture.home} vs {fixture.away}: score unavailable.")
            continue

        my_pick = str(item.get("my_pick", ""))
        chatgpt_pick = item.get("ai_picks", {}).get("chatgpt", {})
        claude_pick = item.get("ai_picks", {}).get("claude", {})
        gemini_pick = item.get("ai_picks", {}).get("gemini", {})

        score_text = (
            f"Result update - {fixture.home} {fixture.home_score}-{fixture.away_score} {fixture.away}\n"
            f"Outcome (1X2): {outcome}\n"
            f"{format_score_line('Me', {'pick': my_pick}, outcome)}\n"
            f"{format_score_line('ChatGPT', chatgpt_pick, outcome)}\n"
            f"{format_score_line('Claude', claude_pick, outcome)}\n"
            f"{format_score_line('Gemini', gemini_pick, outcome)}"
        )

        root_uri = str(item["root_post"]["uri"])
        root_cid = str(item["root_post"]["cid"])
        if dry_run:
            score_post = {"uri": f"dryrun://score/{fixture.fixture_id}", "cid": "dryrun-score-cid"}
            print("\n[DRY-RUN] Score reply preview:")
            print(clamp_post(score_text))
            print(f"[DRY-RUN] Would post score reply for {fixture.home} vs {fixture.away}: {score_post['uri']}")
        else:
            score_post = bsky_create_post(
                session,
                score_text,
                reply_to={
                    "root_uri": root_uri,
                    "root_cid": root_cid,
                    "parent_uri": root_uri,
                    "parent_cid": root_cid,
                },
            )

            item["scored"] = True
            item["result"] = {
                "outcome": outcome,
                "home_score": fixture.home_score,
                "away_score": fixture.away_score,
                "score_post": score_post,
                "scored_at": iso_now(),
            }

            print(f"Scored and posted update for {fixture.home} vs {fixture.away}: {score_post['uri']}")

    save_tracking(tracking)
    if dry_run:
        print("Done dry-run scoring. No Bluesky posts were created and tracking was not marked scored.")
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
    fixtures.add_argument("--day", default="today", choices=["today", "tomorrow", "yesterday"])
    fixtures.set_defaults(func=list_fixtures_cmd)

    publish = subparsers.add_parser("publish", help="Generate picks and publish to Bluesky")
    publish.add_argument("--day", default="today", choices=["today", "tomorrow"])
    publish.add_argument("--dry-run", action="store_true", help="Print post previews without posting to Bluesky")
    publish.add_argument("--no-cache", action="store_true", help="Disable local AI-pick cache and force fresh provider calls")
    publish.add_argument("--picks-file", help="Path to text file with your picks text. Supports keyed lines (fixture_id=...) and free-form lines in fixture order")
    publish.set_defaults(func=publish_for_day)

    score = subparsers.add_parser("score", help="Post result score updates to existing threads")
    score.add_argument("--day", default="yesterday", choices=["yesterday", "today", "tomorrow"])
    score.add_argument("--dry-run", action="store_true", help="Print score reply previews without posting to Bluesky")
    score.set_defaults(func=score_for_day)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
