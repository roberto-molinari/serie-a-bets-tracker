#!/usr/bin/env python3
"""Test script to demo posting to Bluesky and/or X with mocked fixtures."""

import argparse
from datetime import date, datetime
from unittest.mock import patch

import serie_a_bluesky_tool as tool


def main() -> None:
    parser = argparse.ArgumentParser(description="Test posting to Bluesky/X with mocked fixtures")
    parser.add_argument("--platform", default="both", choices=["bluesky", "x", "both"], help="Platform(s) to post to (default: both)")
    parser.add_argument("--dry-run", action="store_true", help="Dry-run mode (don't actually post)")
    parser.add_argument("--suffix", help="Optional text suffix to force unique test post content")
    cli_args = parser.parse_args()

    suffix = (cli_args.suffix or "").strip()
    if not suffix:
        suffix = datetime.now().strftime("%Y%m%d-%H%M%S")

    # Create a fixture for testing
    fixture = tool.Fixture(
        fixture_id=f"100-{suffix}",
        date_utc="2026-05-13T20:45:00Z",
        home="Inter",
        away=f"Milan [{suffix}]",
        home_score=None,
        away_score=None,
        state="pre",
    )

    mode = "dry-run" if cli_args.dry_run else "live"
    print("=" * 60)
    print(f"Testing posting to {cli_args.platform} ({mode} mode)")
    print("=" * 60)

    args = argparse.Namespace(
        day="today",
        dry_run=cli_args.dry_run,
        platform=cli_args.platform,
        picks_file=None,
        no_cache=False,
    )

    with (
        patch("serie_a_bluesky_tool.fetch_serie_a_fixtures", return_value=[fixture]),
        patch("serie_a_bluesky_tool.ask_openai", return_value={"pick": "HOME", "market": "1X2", "reason": "strong form", "confidence": 75}),
        patch("serie_a_bluesky_tool.ask_claude", return_value={"pick": "DRAW", "market": "1X2", "reason": "balanced", "confidence": 60}),
        patch("serie_a_bluesky_tool.ask_gemini", return_value={"pick": "AWAY", "market": "1X2", "reason": "undervalued", "confidence": 65}),
        patch("serie_a_bluesky_tool.ask_user_pick", return_value="HOME"),
        patch("serie_a_bluesky_tool.load_tracking", return_value=[]),
        patch("serie_a_bluesky_tool.save_tracking"),
        patch("serie_a_bluesky_tool.load_pick_cache", return_value={}),
        patch("serie_a_bluesky_tool.save_pick_cache"),
        patch("serie_a_bluesky_tool.resolve_day", return_value=date(2026, 5, 13)),
    ):
        tool.publish_for_day(args)

    print("\n" + "=" * 60)
    print(f"✅ {mode.capitalize()} completed successfully!")
    print("=" * 60)


if __name__ == "__main__":
    main()
