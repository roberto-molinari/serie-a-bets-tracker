import argparse
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import serie_a_bluesky_tool as tool

FIX_DATE = date(2026, 5, 8)


def provider_pick(code: str) -> dict:
    return {
        "pick": code,
        "pick_text": code,
        "market": "1X2",
        "reason": "test",
        "confidence": 70,
        "available": True,
    }


class TestHelpers(unittest.TestCase):
    def test_resolve_day(self) -> None:
        today = date.today()
        self.assertEqual(tool.resolve_day("today"), today)
        self.assertEqual(tool.resolve_day("yesterday"), today - timedelta(days=1))
        self.assertEqual(tool.resolve_day("2024-01-15"), date(2024, 1, 15))

    def test_pick_prompt_mentions_1x2_and_totals(self) -> None:
        prompt = tool.pick_prompt("Roma", "Lazio")
        self.assertIn("1X2|TOTAL_GOALS", prompt)
        self.assertIn("line", prompt)
        self.assertIn("best value", prompt)

    def test_get_fixture_odds(self) -> None:
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value = cursor
        cursor.fetchone.side_effect = [
            (150, 220, -110),
            None,
        ]

        odds = tool.get_fixture_odds(conn, "2026-05-17", "Inter", "Milan")
        self.assertEqual(odds, {"HOME": 150, "DRAW": 220, "AWAY": -110})

        no_odds = tool.get_fixture_odds(conn, "2026-05-17", "Inter", "Milan")
        self.assertIsNone(no_odds)

    def test_get_fixture_odds_supports_nearby_date_fallback(self) -> None:
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value = cursor
        cursor.fetchone.side_effect = [
            None,
            (140, 230, 210),
        ]

        odds = tool.get_fixture_odds(conn, "2026-05-22", "Inter", "Milan", allow_nearby_date=True)
        self.assertEqual(odds, {"HOME": 140, "DRAW": 230, "AWAY": 210})

    def test_get_fixture_totals_odds(self) -> None:
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value = cursor
        cursor.fetchone.side_effect = [
            (2.5, -102, -118),
            None,
        ]

        odds = tool.get_fixture_totals_odds(conn, "2026-05-17", "Inter", "Milan", 2.5)
        self.assertEqual(odds, {"line": 2.5, "OVER": -102, "UNDER": -118})

        no_odds = tool.get_fixture_totals_odds(conn, "2026-05-17", "Inter", "Milan", 3.5)
        self.assertIsNone(no_odds)

    def test_get_fixture_totals_odds_supports_nearby_date_fallback(self) -> None:
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value = cursor
        cursor.fetchone.side_effect = [
            None,
            (2.75, -119, 106),
        ]

        odds = tool.get_fixture_totals_odds(
            conn,
            "2026-05-22",
            "Fiorentina",
            "Atalanta",
            2.75,
            allow_nearby_date=True,
        )
        self.assertEqual(odds, {"line": 2.75, "OVER": -119, "UNDER": 106})

    def test_parse_totals_selection_accepts_supported_lines(self) -> None:
        self.assertEqual(tool.parse_totals_selection("OVER 1"), ("OVER", [1.0]))
        self.assertEqual(tool.parse_totals_selection("under 1.5"), ("UNDER", [1.5]))
        self.assertEqual(tool.parse_totals_selection("OVER 1,1.5"), ("OVER", [1.0, 1.5]))
        self.assertEqual(tool.parse_totals_selection("UNDER 1.5,2"), ("UNDER", [1.5, 2.0]))
        self.assertEqual(tool.parse_totals_selection("OVER 3,3.5"), ("OVER", [3.0, 3.5]))
        self.assertEqual(tool.parse_totals_selection("UNDER 4.5,5"), ("UNDER", [4.5, 5.0]))

    def test_parse_totals_selection_rejects_invalid_split_lines(self) -> None:
        self.assertIsNone(tool.parse_totals_selection("OVER 1,2"))
        self.assertIsNone(tool.parse_totals_selection("UNDER 1.5,3"))
        self.assertIsNone(tool.parse_totals_selection("OVER 2.25"))

    def test_settle_totals_pick_handles_push_and_partial_outcomes(self) -> None:
        # OVER 2,2.5 with total goals=2 => half push, half loss
        win_units, push_units = tool.settle_totals_pick(2, "OVER", [2.0, 2.5])
        self.assertEqual(win_units, 0.0)
        self.assertEqual(push_units, 0.5)

        # UNDER 1.5,2 with total goals=2 => half push, half loss
        win_units, push_units = tool.settle_totals_pick(2, "UNDER", [1.5, 2.0])
        self.assertEqual(win_units, 0.0)
        self.assertEqual(push_units, 0.5)

        # OVER 1.5,2 with total goals=2 => half win, half push
        win_units, push_units = tool.settle_totals_pick(2, "OVER", [1.5, 2.0])
        self.assertEqual(win_units, 0.5)
        self.assertEqual(push_units, 0.5)

    def test_settle_totals_leg(self) -> None:
        self.assertEqual(tool.settle_totals_leg(3, "OVER", 2.5), (1.0, 0.0))
        self.assertEqual(tool.settle_totals_leg(2, "OVER", 2.0), (0.0, 1.0))
        self.assertEqual(tool.settle_totals_leg(1, "UNDER", 1.5), (1.0, 0.0))
        self.assertEqual(tool.settle_totals_leg(2, "UNDER", 2.0), (0.0, 1.0))

    def test_short_pick_rendering_supports_totals(self) -> None:
        pick_data = {
            "pick": "OVER 2.5",
            "market": "TOTAL_GOALS",
            "side": "OVER",
            "line_legs": [2.5],
        }
        self.assertEqual(tool.short_pick_for_reply(pick_data, "Fiorentina", "Atalanta"), "Over 2.5")
        self.assertEqual(tool.short_pick_code_for_reply(pick_data), "O2.5")

    def test_extract_structured_picks_supports_totals(self) -> None:
        text = "[PICKS]\nCagliari vs Udinese = UNDER 1.5,2\n[/PICKS]\n"
        picks = tool.extract_structured_picks(text)
        self.assertEqual(picks[tool.normalize_picks_file_key("Cagliari vs Udinese")], "UNDER 1.5,2")


class TestDryRunModes(unittest.TestCase):
    def test_publish_dry_run_does_not_post(self) -> None:
        fixture = tool.Fixture(
            fixture_id="100",
            date_utc="2026-05-08T18:45:00Z",
            home="Inter",
            away="Milan",
            home_score=None,
            away_score=None,
            state="pre",
        )

        saved: list[dict] = []
        with tempfile.TemporaryDirectory() as tmp_dir:
            picks_path = Path(tmp_dir) / "picks.txt"
            picks_path.write_text("Notes for post\n[PICKS]\nInter vs Milan = HOME\n[/PICKS]\n", encoding="utf-8")
            args = argparse.Namespace(
                day="today",
                dry_run=True,
                session="all",
                platform="both",
                odds_db=None,
                no_cache=False,
                picks_file=str(picks_path),
            )

            with (
                patch("serie_a_bluesky_tool.resolve_day", return_value=FIX_DATE),
                patch("serie_a_bluesky_tool.fetch_serie_a_fixtures", return_value=[fixture]),
                patch("serie_a_bluesky_tool.load_tracking", return_value=[]),
                patch("serie_a_bluesky_tool.save_tracking", side_effect=lambda items: saved.extend(items)),
                patch("serie_a_bluesky_tool.ask_openai", return_value=provider_pick("HOME")),
                patch("serie_a_bluesky_tool.ask_claude", return_value=provider_pick("DRAW")),
                patch("serie_a_bluesky_tool.ask_gemini", return_value=provider_pick("AWAY")),
                patch("serie_a_bluesky_tool.bsky_login", return_value=MagicMock()) as mock_login,
                patch("serie_a_bluesky_tool.x_create_client", return_value=MagicMock()) as mock_x_login,
            ):
                tool.publish_for_day(args)

            mock_login.assert_not_called()
            mock_x_login.assert_not_called()
        self.assertEqual(len(saved), 1)
        self.assertTrue(saved[0]["root_post"]["uri"].startswith("dryrun://root/"))

    def test_publish_dry_run_aborts_when_provider_fails(self) -> None:
        fixture = tool.Fixture(
            fixture_id="101",
            date_utc="2026-05-08T18:45:00Z",
            home="Roma",
            away="Lazio",
            home_score=None,
            away_score=None,
            state="pre",
        )

        args = argparse.Namespace(
            day="today",
            dry_run=True,
            session="all",
            platform="both",
            odds_db=None,
            no_cache=False,
            picks_file=None,
        )

        with (
            patch("serie_a_bluesky_tool.resolve_day", return_value=FIX_DATE),
            patch("serie_a_bluesky_tool.fetch_serie_a_fixtures", return_value=[fixture]),
            patch("serie_a_bluesky_tool.load_tracking", return_value=[]),
            patch("serie_a_bluesky_tool.ask_openai", side_effect=Exception("Provider failed")),
            patch("serie_a_bluesky_tool.ask_claude", return_value=provider_pick("DRAW")),
            patch("serie_a_bluesky_tool.ask_gemini", return_value=provider_pick("AWAY")),
        ):
            with self.assertRaises(SystemExit) as exc:
                tool.publish_for_day(args)

        self.assertIn("Aborting publish because not all AI providers returned picks", str(exc.exception))

    def test_publish_dry_run_uses_cached_picks_without_provider_calls(self) -> None:
        fixture = tool.Fixture(
            fixture_id="102",
            date_utc="2026-05-08T18:45:00Z",
            home="Juventus",
            away="Napoli",
            home_score=None,
            away_score=None,
            state="pre",
        )

        cache = {
            tool.pick_cache_key("ChatGPT", fixture, FIX_DATE): {
                "pick": provider_pick("AWAY") | {"reason": "cached-gpt"}
            },
            tool.pick_cache_key("Claude", fixture, FIX_DATE): {
                "pick": provider_pick("DRAW") | {"reason": "cached-claude"}
            },
            tool.pick_cache_key("Gemini", fixture, FIX_DATE): {
                "pick": provider_pick("HOME") | {"reason": "cached-gemini"}
            },
        }

        saved: list[dict] = []
        with tempfile.TemporaryDirectory() as tmp_dir:
            picks_path = Path(tmp_dir) / "picks.txt"
            picks_path.write_text("Notes for post\n[PICKS]\nJuventus vs Napoli = AWAY\n[/PICKS]\n", encoding="utf-8")
            args = argparse.Namespace(
                day="today",
                dry_run=True,
                no_cache=False,
                session="all",
                platform="both",
                odds_db=None,
                picks_file=str(picks_path),
            )

            with (
                patch("serie_a_bluesky_tool.resolve_day", return_value=FIX_DATE),
                patch("serie_a_bluesky_tool.fetch_serie_a_fixtures", return_value=[fixture]),
                patch("serie_a_bluesky_tool.load_tracking", return_value=[]),
                patch("serie_a_bluesky_tool.save_tracking", side_effect=lambda items: saved.extend(items)),
                patch("serie_a_bluesky_tool.load_pick_cache", return_value=cache),
                patch("serie_a_bluesky_tool.ask_openai") as mock_openai,
                patch("serie_a_bluesky_tool.ask_claude") as mock_claude,
                patch("serie_a_bluesky_tool.ask_gemini") as mock_gemini,
            ):
                tool.publish_for_day(args)

            mock_openai.assert_not_called()
            mock_claude.assert_not_called()
            mock_gemini.assert_not_called()
        self.assertEqual(saved[0]["ai_picks"]["chatgpt"]["reason"], "cached-gpt")

    def test_publish_dry_run_can_disable_cache(self) -> None:
        fixture = tool.Fixture(
            fixture_id="103",
            date_utc="2026-05-08T18:45:00Z",
            home="Atalanta",
            away="Torino",
            home_score=None,
            away_score=None,
            state="pre",
        )

        args = argparse.Namespace(
            day="today",
            dry_run=True,
            no_cache=True,
            session="all",
            platform="both",
            odds_db=None,
            picks_file=None,
        )

        with (
            patch("serie_a_bluesky_tool.resolve_day", return_value=FIX_DATE),
            patch("serie_a_bluesky_tool.fetch_serie_a_fixtures", return_value=[fixture]),
            patch("serie_a_bluesky_tool.load_tracking", return_value=[]),
            patch("serie_a_bluesky_tool.save_tracking"),
            patch("serie_a_bluesky_tool.load_pick_cache") as mock_load_cache,
            patch("serie_a_bluesky_tool.save_pick_cache") as mock_save_cache,
            patch("serie_a_bluesky_tool.ask_openai", return_value=provider_pick("HOME")),
            patch("serie_a_bluesky_tool.ask_claude", return_value=provider_pick("DRAW")),
            patch("serie_a_bluesky_tool.ask_gemini", return_value=provider_pick("AWAY")),
        ):
            tool.publish_for_day(args)

        mock_load_cache.assert_not_called()
        mock_save_cache.assert_not_called()

    def test_publish_test_mode_disables_cache_and_tags_tracking(self) -> None:
        fixture = tool.Fixture(
            fixture_id="103",
            date_utc="2026-05-08T18:45:00Z",
            home="Atalanta",
            away="Torino",
            home_score=None,
            away_score=None,
            state="pre",
        )

        saved: list[dict] = []
        with tempfile.TemporaryDirectory() as tmp_dir:
            picks_path = Path(tmp_dir) / "picks.txt"
            picks_path.write_text("Notes\n[PICKS]\nAtalanta vs Torino = HOME\n[/PICKS]\n", encoding="utf-8")
            args = argparse.Namespace(
                day="today",
                dry_run=True,
                no_cache=False,
                test_mode=True,
                session="all",
                platform="both",
                odds_db=None,
                picks_file=str(picks_path),
            )

            with (
                patch("serie_a_bluesky_tool.resolve_day", return_value=FIX_DATE),
                patch("serie_a_bluesky_tool.fetch_serie_a_fixtures", return_value=[fixture]),
                patch("serie_a_bluesky_tool.load_tracking", return_value=[]),
                patch("serie_a_bluesky_tool.save_tracking", side_effect=lambda items: saved.extend(items)),
                patch("serie_a_bluesky_tool.load_pick_cache") as mock_load_cache,
                patch("serie_a_bluesky_tool.save_pick_cache") as mock_save_cache,
                patch("serie_a_bluesky_tool.ask_openai", return_value=provider_pick("HOME")),
                patch("serie_a_bluesky_tool.ask_claude", return_value=provider_pick("DRAW")),
                patch("serie_a_bluesky_tool.ask_gemini", return_value=provider_pick("AWAY")),
            ):
                tool.publish_for_day(args)

        mock_load_cache.assert_not_called()
        mock_save_cache.assert_not_called()
        self.assertTrue(saved[0]["test_mode"])

    def test_publish_dry_run_uses_picks_file(self) -> None:
        fixture = tool.Fixture(
            fixture_id="104",
            date_utc="2026-05-08T18:45:00Z",
            home="Fiorentina",
            away="Bologna",
            home_score=None,
            away_score=None,
            state="pre",
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            picks_path = Path(tmp_dir) / "picks.txt"
            picks_path.write_text("104=AWAY\n", encoding="utf-8")
            saved: list[dict] = []
            args = argparse.Namespace(
                day="today",
                dry_run=True,
                no_cache=True,
                picks_file=str(picks_path),
                session="all",
                platform="both",
                odds_db=None,
            )

            with (
                patch("serie_a_bluesky_tool.resolve_day", return_value=FIX_DATE),
                patch("serie_a_bluesky_tool.fetch_serie_a_fixtures", return_value=[fixture]),
                patch("serie_a_bluesky_tool.load_tracking", return_value=[]),
                patch("serie_a_bluesky_tool.save_tracking", side_effect=lambda items: saved.extend(items)),
                patch("serie_a_bluesky_tool.ask_openai", return_value=provider_pick("HOME")),
                patch("serie_a_bluesky_tool.ask_claude", return_value=provider_pick("DRAW")),
                patch("serie_a_bluesky_tool.ask_gemini", return_value=provider_pick("AWAY")),
            ):
                tool.publish_for_day(args)

        self.assertEqual(saved[0]["my_pick"], "")
        self.assertEqual(saved[0]["my_pick_text"], "")

    def test_publish_dry_run_uses_freeform_lines_from_picks_file(self) -> None:
        fixture1 = tool.Fixture(
            fixture_id="106",
            date_utc="2026-05-08T18:45:00Z",
            home="Empoli",
            away="Parma",
            home_score=None,
            away_score=None,
            state="pre",
        )
        fixture2 = tool.Fixture(
            fixture_id="107",
            date_utc="2026-05-08T20:45:00Z",
            home="Torino",
            away="Lecce",
            home_score=None,
            away_score=None,
            state="pre",
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            picks_path = Path(tmp_dir) / "picks.txt"
            picks_path.write_text(
                "Empoli to edge this one late\nwith a low-scoring game\n\n"
                "Torino to grind out a 1-0 win\n",
                encoding="utf-8",
            )
            saved: list[dict] = []
            args = argparse.Namespace(
                day="today",
                dry_run=True,
                no_cache=True,
                picks_file=str(picks_path),
                session="all",
                platform="both",
                odds_db=None,
            )

            with (
                patch("serie_a_bluesky_tool.resolve_day", return_value=FIX_DATE),
                patch("serie_a_bluesky_tool.fetch_serie_a_fixtures", return_value=[fixture1, fixture2]),
                patch("serie_a_bluesky_tool.load_tracking", return_value=[]),
                patch("serie_a_bluesky_tool.save_tracking", side_effect=lambda items: saved.extend(items)),
                patch("serie_a_bluesky_tool.ask_openai", return_value=provider_pick("HOME")),
                patch("serie_a_bluesky_tool.ask_claude", return_value=provider_pick("DRAW")),
                patch("serie_a_bluesky_tool.ask_gemini", return_value=provider_pick("AWAY")),
            ):
                tool.publish_for_day(args)

        self.assertEqual(len(saved), 2)
        self.assertEqual(saved[0]["my_pick_text"], "")
        self.assertEqual(saved[1]["my_pick_text"], "")
        self.assertEqual(saved[0]["root_post"]["uri"], saved[1]["root_post"]["uri"])
        self.assertEqual(saved[0]["ai_reply_post"]["uri"], saved[1]["ai_reply_post"]["uri"])

    def test_publish_dry_run_does_not_require_per_fixture_picks_in_file(self) -> None:
        fixture = tool.Fixture(
            fixture_id="105",
            date_utc="2026-05-08T18:45:00Z",
            home="Roma",
            away="Napoli",
            home_score=None,
            away_score=None,
            state="pre",
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            picks_path = Path(tmp_dir) / "picks.txt"
            picks_path.write_text("My full daily picks text block\n", encoding="utf-8")
            args = argparse.Namespace(
                day="today",
                dry_run=True,
                no_cache=True,
                picks_file=str(picks_path),
                session="all",
                platform="both",
                odds_db=None,
            )

            with (
                patch("serie_a_bluesky_tool.resolve_day", return_value=FIX_DATE),
                patch("serie_a_bluesky_tool.fetch_serie_a_fixtures", return_value=[fixture]),
                patch("serie_a_bluesky_tool.load_tracking", return_value=[]),
                patch("serie_a_bluesky_tool.save_tracking") as mock_save_tracking,
                patch("serie_a_bluesky_tool.ask_openai", return_value=provider_pick("HOME")),
                patch("serie_a_bluesky_tool.ask_claude", return_value=provider_pick("DRAW")),
                patch("serie_a_bluesky_tool.ask_gemini", return_value=provider_pick("AWAY")),
            ):
                tool.publish_for_day(args)

        mock_save_tracking.assert_called_once()

    def test_score_dry_run_does_not_post_or_mark_scored(self) -> None:
        tracking = [
            {
                "fixture_id": "100",
                "fixture_date": "2026-05-07",
                "home": "Inter",
                "away": "Milan",
                "my_pick": "HOME",
                "ai_picks": {
                    "chatgpt": {"pick": "HOME"},
                    "claude": {"pick": "DRAW"},
                    "gemini": {"pick": "AWAY"},
                },
                "root_post": {"uri": "at://root", "cid": "cid1"},
                "ai_reply_post": {"uri": "at://reply", "cid": "cid2"},
                "scored": False,
            }
        ]
        fixture = tool.Fixture(
            fixture_id="100",
            date_utc="2026-05-07T18:45:00Z",
            home="Inter",
            away="Milan",
            home_score=2,
            away_score=1,
            state="post",
        )

        args = argparse.Namespace(
            day="yesterday",
            dry_run=True,
            session="all",
            platform="both",
            odds_db=None,
            recalculate=False,
            start_date=None,
            end_date=None,
        )

        with (
            patch("serie_a_bluesky_tool.resolve_day", return_value=date(2026, 5, 7)),
            patch("serie_a_bluesky_tool.load_tracking", return_value=tracking),
            patch("serie_a_bluesky_tool.fetch_serie_a_fixtures", return_value=[fixture]),
            patch("serie_a_bluesky_tool.save_tracking") as mock_save_tracking,
            patch("serie_a_bluesky_tool.bsky_login", return_value=MagicMock()) as mock_login,
            patch("serie_a_bluesky_tool.bsky_create_post") as mock_post,
        ):
            tool.score_for_date_range(args)

        mock_login.assert_not_called()
        mock_post.assert_not_called()
        mock_save_tracking.assert_not_called()
        self.assertFalse(tracking[0].get("scored", False))

    def test_score_dry_run_prints_picks_being_scored(self) -> None:
        tracking = [
            {
                "fixture_id": "204",
                "fixture_date": "2026-05-07",
                "home": "Inter",
                "away": "Milan",
                "my_pick": "OVER 2.5",
                "ai_picks": {
                    "chatgpt": {"pick": "HOME", "market": "1X2", "available": True},
                    "claude": {"pick": "DRAW", "market": "1X2", "available": True},
                    "gemini": {"pick": "AWAY", "market": "1X2", "available": True},
                },
                "root_post": {"uri": "at://root", "cid": "cid1"},
                "ai_reply_post": {"uri": "at://reply", "cid": "cid2"},
                "test_mode": True,
                "scored": False,
            }
        ]
        fixture = tool.Fixture(
            fixture_id="204",
            date_utc="2026-05-07T18:45:00Z",
            home="Inter",
            away="Milan",
            home_score=2,
            away_score=1,
            state="post",
        )

        args = argparse.Namespace(
            day="yesterday",
            dry_run=True,
            session="all",
            platform="both",
            odds_db=None,
            recalculate=False,
            start_date=None,
            end_date=None,
            test_mode=True,
            sim_results_file=None,
        )

        with (
            patch("serie_a_bluesky_tool.resolve_day", return_value=date(2026, 5, 7)),
            patch("serie_a_bluesky_tool.load_tracking", return_value=tracking),
            patch("serie_a_bluesky_tool.fetch_serie_a_fixtures", return_value=[fixture]),
            patch("builtins.print") as mock_print,
        ):
            tool.score_for_date_range(args)

        rendered = "\n".join(" ".join(str(p) for p in c.args) for c in mock_print.call_args_list)
        self.assertIn("[DRY-RUN] Picks being scored:", rendered)
        self.assertIn("Inter vs Milan", rendered)
        self.assertIn("Minvest: OVER 2.5", rendered)
        self.assertIn("Gemini: Milan to win", rendered)
        self.assertIn("Claude: Draw", rendered)
        self.assertIn("ChatGPT: Inter to win", rendered)

    def test_score_dry_run_test_mode_supports_simulated_results_file(self) -> None:
        tracking = [
            {
                "fixture_id": "200",
                "fixture_date": "2026-05-07",
                "home": "Inter",
                "away": "Milan",
                "my_pick": "OVER 2.5",
                "ai_picks": {
                    "chatgpt": {"market": "TOTAL_GOALS", "side": "OVER", "line_legs": [2.5], "available": True},
                    "claude": {"pick": "DRAW", "market": "1X2", "available": True},
                    "gemini": {"pick": "HOME", "market": "1X2", "available": True},
                },
                "root_post": {"uri": "at://root", "cid": "cid1"},
                "ai_reply_post": {"uri": "at://reply", "cid": "cid2"},
                "test_mode": True,
                "scored": False,
            }
        ]
        fixture = tool.Fixture(
            fixture_id="200",
            date_utc="2026-05-07T18:45:00Z",
            home="Inter",
            away="Milan",
            home_score=None,
            away_score=None,
            state="pre",
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            sim_path = Path(tmp_dir) / "sim_results.json"
            sim_path.write_text(
                json.dumps({"Inter vs Milan": {"home_score": 2, "away_score": 1}}),
                encoding="utf-8",
            )

            args = argparse.Namespace(
                day="yesterday",
                dry_run=True,
                session="all",
                platform="both",
                odds_db=None,
                recalculate=False,
                start_date=None,
                end_date=None,
                test_mode=True,
                sim_results_file=str(sim_path),
            )

            with (
                patch("serie_a_bluesky_tool.resolve_day", return_value=date(2026, 5, 7)),
                patch("serie_a_bluesky_tool.load_tracking", return_value=tracking),
                patch("serie_a_bluesky_tool.fetch_serie_a_fixtures", return_value=[fixture]),
                patch("serie_a_bluesky_tool.save_tracking") as mock_save_tracking,
                patch("builtins.print") as mock_print,
            ):
                tool.score_for_date_range(args)

        mock_save_tracking.assert_not_called()
        rendered = "\n".join(" ".join(str(p) for p in c.args) for c in mock_print.call_args_list)
        self.assertIn("[DRY-RUN] Scoreboard reply preview:", rendered)
        self.assertNotIn("match not final yet", rendered)

    def test_score_simulated_results_unresolved_matchup_fails_fast(self) -> None:
        tracking = [
            {
                "fixture_id": "201",
                "fixture_date": "2026-05-07",
                "home": "Inter",
                "away": "Milan",
                "my_pick": "HOME",
                "ai_picks": {
                    "chatgpt": {"pick": "HOME", "market": "1X2", "available": True},
                    "claude": {"pick": "DRAW", "market": "1X2", "available": True},
                    "gemini": {"pick": "AWAY", "market": "1X2", "available": True},
                },
                "root_post": {"uri": "at://root", "cid": "cid1"},
                "ai_reply_post": {"uri": "at://reply", "cid": "cid2"},
                "test_mode": True,
                "scored": False,
            }
        ]
        fixture = tool.Fixture(
            fixture_id="201",
            date_utc="2026-05-07T18:45:00Z",
            home="Inter",
            away="Milan",
            home_score=None,
            away_score=None,
            state="pre",
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            sim_path = Path(tmp_dir) / "sim_results.json"
            sim_path.write_text(
                json.dumps({"Roma vs Lazio": {"home_score": 1, "away_score": 0}}),
                encoding="utf-8",
            )

            args = argparse.Namespace(
                day="yesterday",
                dry_run=True,
                session="all",
                platform="both",
                odds_db=None,
                recalculate=False,
                start_date=None,
                end_date=None,
                test_mode=True,
                sim_results_file=str(sim_path),
            )

            with (
                patch("serie_a_bluesky_tool.resolve_day", return_value=date(2026, 5, 7)),
                patch("serie_a_bluesky_tool.load_tracking", return_value=tracking),
                patch("serie_a_bluesky_tool.fetch_serie_a_fixtures", return_value=[fixture]),
            ):
                with self.assertRaises(SystemExit) as ctx:
                    tool.score_for_date_range(args)

        self.assertIn("Could not resolve simulated fixture reference", str(ctx.exception))

    def test_score_totals_with_odds_db_does_not_use_closed_connection(self) -> None:
        tracking = [
            {
                "fixture_id": "202",
                "fixture_date": "2026-05-07",
                "home": "Inter",
                "away": "Milan",
                "my_pick": "OVER 2.5",
                "ai_picks": {
                    "chatgpt": {"market": "TOTAL_GOALS", "side": "OVER", "line_legs": [2.5], "available": True},
                    "claude": {"pick": "DRAW", "market": "1X2", "available": True},
                    "gemini": {"pick": "HOME", "market": "1X2", "available": True},
                },
                "root_post": {"uri": "at://root", "cid": "cid1"},
                "ai_reply_post": {"uri": "at://reply", "cid": "cid2"},
                "test_mode": True,
                "scored": False,
            }
        ]
        fixture = tool.Fixture(
            fixture_id="202",
            date_utc="2026-05-07T18:45:00Z",
            home="Inter",
            away="Milan",
            home_score=2,
            away_score=1,
            state="post",
        )

        args = argparse.Namespace(
            day="yesterday",
            dry_run=True,
            session="all",
            platform="both",
            odds_db="/tmp/fake-odds.db",
            recalculate=False,
            start_date=None,
            end_date=None,
            test_mode=True,
            sim_results_file=None,
        )

        with (
            patch("serie_a_bluesky_tool.resolve_day", return_value=date(2026, 5, 7)),
            patch("serie_a_bluesky_tool.load_tracking", return_value=tracking),
            patch("serie_a_bluesky_tool.fetch_serie_a_fixtures", return_value=[fixture]),
            patch("serie_a_bluesky_tool.sqlite3.connect", return_value=MagicMock()),
            patch("serie_a_bluesky_tool.get_fixture_odds", return_value=None),
            patch("serie_a_bluesky_tool.get_fixture_totals_odds", return_value={"line": 2.5, "OVER": -110, "UNDER": -110}),
            patch("builtins.print"),
        ):
            # Regression: this used to raise sqlite3.ProgrammingError due to closed connection.
            tool.score_for_date_range(args)

    def test_score_totals_split_pick_uses_quarter_line_odds_fallback(self) -> None:
        tracking = [
            {
                "fixture_id": "203",
                "fixture_date": "2026-05-07",
                "home": "Fiorentina",
                "away": "Atalanta",
                "my_pick": "OVER 2.5,3",
                "ai_picks": {
                    "chatgpt": {"market": "TOTAL_GOALS", "side": "OVER", "line_legs": [2.5], "available": True},
                    "claude": {"market": "TOTAL_GOALS", "side": "OVER", "line_legs": [2.5], "available": True},
                    "gemini": {"market": "TOTAL_GOALS", "side": "OVER", "line_legs": [2.5], "available": True},
                },
                "root_post": {"uri": "at://root", "cid": "cid1"},
                "ai_reply_post": {"uri": "at://reply", "cid": "cid2"},
                "test_mode": True,
                "scored": False,
            }
        ]
        fixture = tool.Fixture(
            fixture_id="203",
            date_utc="2026-05-07T18:45:00Z",
            home="Fiorentina",
            away="Atalanta",
            home_score=2,
            away_score=1,
            state="post",
        )

        args = argparse.Namespace(
            day="yesterday",
            dry_run=True,
            session="all",
            platform="both",
            odds_db="/tmp/fake-odds.db",
            recalculate=False,
            start_date=None,
            end_date=None,
            test_mode=True,
            sim_results_file=None,
        )

        def fake_totals_odds(_conn, _date, _home, _away, line, allow_nearby_date=False):
            if abs(float(line) - 2.75) < 1e-9:
                return {"line": 2.75, "OVER": -119, "UNDER": 106}
            return None

        with (
            patch("serie_a_bluesky_tool.resolve_day", return_value=date(2026, 5, 7)),
            patch("serie_a_bluesky_tool.load_tracking", return_value=tracking),
            patch("serie_a_bluesky_tool.fetch_serie_a_fixtures", return_value=[fixture]),
            patch("serie_a_bluesky_tool.sqlite3.connect", return_value=MagicMock()),
            patch("serie_a_bluesky_tool.get_fixture_odds", return_value=None),
            patch("serie_a_bluesky_tool.get_fixture_totals_odds", side_effect=fake_totals_odds),
            patch("builtins.print") as mock_print,
        ):
            tool.score_for_date_range(args)

        rendered = "\n".join(" ".join(str(p) for p in c.args) for c in mock_print.call_args_list)
        self.assertIn("Minvest: 0.5/1", rendered)
        self.assertIn("return: 1.42u", rendered)
        self.assertIn("ROI: +42.0%", rendered)

    def test_score_simulated_results_requires_test_mode_and_dry_run(self) -> None:
        args = argparse.Namespace(
            day="yesterday",
            dry_run=False,
            session="all",
            platform="both",
            odds_db=None,
            recalculate=False,
            start_date=None,
            end_date=None,
            test_mode=False,
            sim_results_file="data/sim_results.json",
        )

        with self.assertRaises(SystemExit) as ctx:
            tool.score_for_date_range(args)

        self.assertIn("--sim-results-file is only allowed", str(ctx.exception))

    def test_publish_dry_run_uses_picks_section_for_auto_scoring(self) -> None:
        fixture = tool.Fixture(
            fixture_id="106",
            date_utc="2026-05-08T18:45:00Z",
            home="Napoli",
            away="Bologna",
            home_score=None,
            away_score=None,
            state="pre",
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            picks_path = Path(tmp_dir) / "picks.txt"
            picks_path.write_text(
                "My picks today:\n\n"
                "Napoli looks strong at home.\n"
                "\n"
                "[PICKS]\n"
                "Napoli vs Bologna = AWAY\n"
                "[/PICKS]\n",
                encoding="utf-8",
            )
            saved: list[dict] = []
            args = argparse.Namespace(
                day="today",
                dry_run=True,
                no_cache=True,
                picks_file=str(picks_path),
                session="all",
                platform="both",
                odds_db=None,
            )

            with (
                patch("serie_a_bluesky_tool.resolve_day", return_value=FIX_DATE),
                patch("serie_a_bluesky_tool.fetch_serie_a_fixtures", return_value=[fixture]),
                patch("serie_a_bluesky_tool.load_tracking", return_value=[]),
                patch("serie_a_bluesky_tool.save_tracking", side_effect=lambda items: saved.extend(items)),
                patch("serie_a_bluesky_tool.ask_openai", return_value=provider_pick("HOME")),
                patch("serie_a_bluesky_tool.ask_claude", return_value=provider_pick("DRAW")),
                patch("serie_a_bluesky_tool.ask_gemini", return_value=provider_pick("AWAY")),
            ):
                tool.publish_for_day(args)

        self.assertEqual(saved[0]["my_pick"], "AWAY")
        self.assertEqual(saved[0]["my_pick_text"], "")

    def test_publish_dry_run_matches_picks_section_with_team_prefix_variants(self) -> None:
        fixture = tool.Fixture(
            fixture_id="107",
            date_utc="2026-05-08T18:45:00Z",
            home="AC Milan",
            away="AS Roma",
            home_score=None,
            away_score=None,
            state="pre",
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            picks_path = Path(tmp_dir) / "picks.txt"
            picks_path.write_text(
                "Matchday notes for my post.\n\n"
                "[PICKS]\n"
                "Milan vs Roma = HOME\n"
                "[/PICKS]\n",
                encoding="utf-8",
            )
            saved: list[dict] = []
            args = argparse.Namespace(
                day="today",
                dry_run=True,
                no_cache=True,
                picks_file=str(picks_path),
                session="all",
                platform="both",
                odds_db=None,
            )

            with (
                patch("serie_a_bluesky_tool.resolve_day", return_value=FIX_DATE),
                patch("serie_a_bluesky_tool.fetch_serie_a_fixtures", return_value=[fixture]),
                patch("serie_a_bluesky_tool.load_tracking", return_value=[]),
                patch("serie_a_bluesky_tool.save_tracking", side_effect=lambda items: saved.extend(items)),
                patch("serie_a_bluesky_tool.ask_openai", return_value=provider_pick("HOME")),
                patch("serie_a_bluesky_tool.ask_claude", return_value=provider_pick("DRAW")),
                patch("serie_a_bluesky_tool.ask_gemini", return_value=provider_pick("AWAY")),
            ):
                tool.publish_for_day(args)

        self.assertEqual(saved[0]["my_pick"], "HOME")
        self.assertEqual(saved[0]["my_pick_text"], "")

    def test_publish_dry_run_fails_fast_on_unmatched_structured_pick(self) -> None:
        fixture = tool.Fixture(
            fixture_id="108",
            date_utc="2026-05-08T18:45:00Z",
            home="Internazionale",
            away="Hellas Verona",
            home_score=None,
            away_score=None,
            state="pre",
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            picks_path = Path(tmp_dir) / "picks.txt"
            picks_path.write_text(
                "Matchday notes.\n\n"
                "[PICKS]\n"
                "Juventus vs Fiorentina = HOME\n"
                "[/PICKS]\n",
                encoding="utf-8",
            )
            args = argparse.Namespace(
                day="today",
                dry_run=True,
                no_cache=True,
                picks_file=str(picks_path),
                session="all",
                platform="both",
                odds_db=None,
            )

            with (
                patch("serie_a_bluesky_tool.resolve_day", return_value=FIX_DATE),
                patch("serie_a_bluesky_tool.fetch_serie_a_fixtures", return_value=[fixture]),
                patch("serie_a_bluesky_tool.ask_openai") as mock_openai,
                patch("serie_a_bluesky_tool.ask_claude") as mock_claude,
                patch("serie_a_bluesky_tool.ask_gemini") as mock_gemini,
                patch("serie_a_bluesky_tool.load_tracking", return_value=[]),
                patch("serie_a_bluesky_tool.save_tracking") as mock_save_tracking,
            ):
                with self.assertRaises(SystemExit) as ctx:
                    tool.publish_for_day(args)

        self.assertIn("Invalid [PICKS] section", str(ctx.exception))
        mock_openai.assert_not_called()
        mock_claude.assert_not_called()
        mock_gemini.assert_not_called()
        mock_save_tracking.assert_not_called()


class TestParser(unittest.TestCase):
    def test_publish_and_score_support_dry_run(self) -> None:
        parser = tool.build_parser()
        publish_args = parser.parse_args(["publish", "--day", "today", "--dry-run"])
        score_args = parser.parse_args(["score", "--day", "yesterday", "--dry-run"])

        self.assertTrue(publish_args.dry_run)
        self.assertTrue(score_args.dry_run)

    def test_publish_supports_no_cache(self) -> None:
        parser = tool.build_parser()
        publish_args = parser.parse_args(["publish", "--day", "today", "--no-cache"])
        self.assertTrue(publish_args.no_cache)

    def test_publish_supports_picks_file(self) -> None:
        parser = tool.build_parser()
        publish_args = parser.parse_args(["publish", "--day", "today", "--picks-file", "data/picks.txt"])
        self.assertEqual(publish_args.picks_file, "data/picks.txt")

    def test_publish_supports_yesterday(self) -> None:
        parser = tool.build_parser()
        publish_args = parser.parse_args(["publish", "--day", "yesterday"])
        self.assertEqual(publish_args.day, "yesterday")

    def test_publish_supports_explicit_date(self) -> None:
        parser = tool.build_parser()
        publish_args = parser.parse_args(["publish", "--day", "2026-05-22"])
        self.assertEqual(publish_args.day, "2026-05-22")

    def test_publish_and_score_support_test_mode(self) -> None:
        parser = tool.build_parser()
        publish_args = parser.parse_args(["publish", "--day", "today", "--test-mode"])
        score_args = parser.parse_args(["score", "--day", "yesterday", "--test-mode"])
        self.assertTrue(publish_args.test_mode)
        self.assertTrue(score_args.test_mode)

    def test_score_supports_sim_results_file(self) -> None:
        parser = tool.build_parser()
        score_args = parser.parse_args([
            "score",
            "--day",
            "yesterday",
            "--test-mode",
            "--dry-run",
            "--sim-results-file",
            "data/sim_results.json",
        ])
        self.assertEqual(score_args.sim_results_file, "data/sim_results.json")


if __name__ == "__main__":
    unittest.main()
