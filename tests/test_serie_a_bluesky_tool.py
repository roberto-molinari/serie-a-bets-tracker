import argparse
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import serie_a_bluesky_tool as tool


class TestHelpers(unittest.TestCase):
    def test_resolve_day(self) -> None:
        with patch("serie_a_bluesky_tool.today_utc", return_value=date(2026, 5, 8)):
            self.assertEqual(tool.resolve_day("today"), date(2026, 5, 8))
            self.assertEqual(tool.resolve_day("tomorrow"), date(2026, 5, 9))
            self.assertEqual(tool.resolve_day("yesterday"), date(2026, 5, 7))

    def test_outcome_from_scores(self) -> None:
        self.assertEqual(tool.outcome_from_scores(2, 1), "HOME")
        self.assertEqual(tool.outcome_from_scores(0, 1), "AWAY")
        self.assertEqual(tool.outcome_from_scores(1, 1), "DRAW")
        self.assertIsNone(tool.outcome_from_scores(None, 1))

    def test_clamp_post(self) -> None:
        self.assertEqual(tool.clamp_post("abc", max_chars=5), "abc")
        self.assertEqual(tool.clamp_post("abcdef", max_chars=5), "abcd…")

    def test_safe_json_extract(self) -> None:
        parsed = tool.safe_json_extract('prefix {"pick":"HOME","confidence":90} suffix')
        self.assertEqual(parsed["pick"], "HOME")
        with self.assertRaises(ValueError):
            tool.safe_json_extract("no json here")

    def test_normalize_pick(self) -> None:
        pick = tool.normalize_pick({"pick": "home", "confidence": 120, "reason": "x"}, "ChatGPT")
        self.assertEqual(pick["pick"], "HOME")
        self.assertEqual(pick["confidence"], 100)
        self.assertEqual(pick["market"], "1X2")
        self.assertTrue(pick["available"])

        with self.assertRaises(SystemExit):
            tool.normalize_pick({"pick": "BTTS", "confidence": 10}, "ChatGPT")

    def test_format_helpers_show_unavailable_pick(self) -> None:
        unavailable = tool.unavailable_pick("quota exceeded")

        self.assertEqual(tool.format_ai_pick_line("ChatGPT", unavailable), "- ChatGPT: unavailable")
        self.assertEqual(tool.format_score_line("ChatGPT", unavailable, "HOME"), "- ChatGPT: unavailable")

    def test_fixture_session_label(self) -> None:
        morning_fixture = tool.Fixture(
            fixture_id="m1",
            date_utc="2026-05-17T09:30:00Z",
            home="A",
            away="B",
            home_score=None,
            away_score=None,
            state="pre",
        )
        afternoon_fixture = tool.Fixture(
            fixture_id="a1",
            date_utc="2026-05-17T15:30:00Z",
            home="C",
            away="D",
            home_score=None,
            away_score=None,
            state="pre",
        )

        self.assertIn(tool.fixture_session_label(morning_fixture), {"morning", "afternoon"})
        self.assertIn(tool.fixture_session_label(afternoon_fixture), {"morning", "afternoon"})

    def test_picks_text_for_session_blocks(self) -> None:
        raw = (
            "[MORNING]\n"
            "Morning text\n"
            "[PICKS]\nA vs B = HOME\n[/PICKS]\n"
            "[/MORNING]\n"
            "\n"
            "[AFTERNOON]\n"
            "Afternoon text\n"
            "[PICKS]\nC vs D = AWAY\n[/PICKS]\n"
            "[/AFTERNOON]\n"
        )

        morning = tool.picks_text_for_session(raw, "morning")
        afternoon = tool.picks_text_for_session(raw, "afternoon")
        all_text = tool.picks_text_for_session(raw, "all")

        self.assertIn("Morning text", morning)
        self.assertNotIn("Afternoon text", morning)
        self.assertIn("Afternoon text", afternoon)
        self.assertNotIn("Morning text", afternoon)
        self.assertIn("Morning text", all_text)
        self.assertIn("Afternoon text", all_text)

    def test_load_picks_file_scopes_to_session_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            picks_path = Path(tmp_dir) / "picks.txt"
            picks_path.write_text(
                "[MORNING]\n"
                "Morning summary\n"
                "[PICKS]\nInter vs Milan = HOME\n[/PICKS]\n"
                "[/MORNING]\n"
                "\n"
                "[AFTERNOON]\n"
                "Afternoon summary\n"
                "[PICKS]\nJuventus vs Roma = AWAY\n[/PICKS]\n"
                "[/AFTERNOON]\n",
                encoding="utf-8",
            )

            morning_data = tool.load_picks_file(str(picks_path), session_filter="morning")
            self.assertIn("Morning summary", morning_data["raw_text"])
            self.assertNotIn("Afternoon summary", morning_data["raw_text"])
            self.assertEqual(
                morning_data["structured_picks"][tool.normalize_picks_file_key("Inter vs Milan")],
                "HOME",
            )


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

        saved: dict[str, list[dict]] = {}
        args = argparse.Namespace(day="today", dry_run=True)

        with (
            patch("serie_a_bluesky_tool.fetch_serie_a_fixtures", return_value=[fixture]),
            patch("serie_a_bluesky_tool.ask_openai", return_value={"pick": "HOME", "market": "1X2", "reason": "r", "confidence": 60}),
            patch("serie_a_bluesky_tool.ask_claude", return_value={"pick": "DRAW", "market": "1X2", "reason": "r", "confidence": 55}),
            patch("serie_a_bluesky_tool.ask_gemini", return_value={"pick": "AWAY", "market": "1X2", "reason": "r", "confidence": 58}),
            patch("serie_a_bluesky_tool.ask_user_pick", return_value="HOME"),
            patch("serie_a_bluesky_tool.load_tracking", return_value=[]),
            patch("serie_a_bluesky_tool.save_tracking", side_effect=lambda items: saved.setdefault("items", items)),
            patch("serie_a_bluesky_tool.load_pick_cache", return_value={}),
            patch("serie_a_bluesky_tool.save_pick_cache"),
            patch("serie_a_bluesky_tool.bsky_login") as mock_login,
            patch("serie_a_bluesky_tool.bsky_create_post") as mock_post,
            patch("serie_a_bluesky_tool.resolve_day", return_value=date(2026, 5, 8)),
        ):
            tool.publish_for_day(args)

        mock_login.assert_not_called()
        mock_post.assert_not_called()

        items = saved["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["my_pick"], "HOME")
        self.assertTrue(items[0]["root_post"]["uri"].startswith("dryrun://root/"))

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

        args = argparse.Namespace(day="today", dry_run=True)

        with (
            patch("serie_a_bluesky_tool.fetch_serie_a_fixtures", return_value=[fixture]),
            patch("serie_a_bluesky_tool.ask_openai", side_effect=SystemExit("OPENAI_API_KEY is required")),
            patch("serie_a_bluesky_tool.ask_claude", return_value={"pick": "DRAW", "market": "1X2", "reason": "r", "confidence": 55, "available": True}),
            patch("serie_a_bluesky_tool.ask_gemini", return_value={"pick": "AWAY", "market": "1X2", "reason": "r", "confidence": 58, "available": True}),
            patch("serie_a_bluesky_tool.load_tracking", return_value=[]),
            patch("serie_a_bluesky_tool.save_tracking") as mock_save,
            patch("serie_a_bluesky_tool.load_pick_cache", return_value={}),
            patch("serie_a_bluesky_tool.save_pick_cache"),
            patch("serie_a_bluesky_tool.bsky_login") as mock_login,
            patch("serie_a_bluesky_tool.bsky_create_post") as mock_post,
            patch("serie_a_bluesky_tool.resolve_day", return_value=date(2026, 5, 8)),
        ):
            with self.assertRaises(SystemExit) as exc:
                tool.publish_for_day(args)

        mock_login.assert_not_called()
        mock_post.assert_not_called()
        mock_save.assert_not_called()
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
        target_day = date(2026, 5, 8)
        cache = {
            tool.pick_cache_key("ChatGPT", fixture, target_day): {
                "pick": {"pick": "HOME", "market": "1X2", "reason": "cached", "confidence": 70, "available": True}
            },
            tool.pick_cache_key("Claude", fixture, target_day): {
                "pick": {"pick": "DRAW", "market": "1X2", "reason": "cached", "confidence": 64, "available": True}
            },
            tool.pick_cache_key("Gemini", fixture, target_day): {
                "pick": {"pick": "AWAY", "market": "1X2", "reason": "cached", "confidence": 66, "available": True}
            },
        }

        saved: dict[str, list[dict]] = {}
        args = argparse.Namespace(day="today", dry_run=True, no_cache=False)

        with (
            patch("serie_a_bluesky_tool.fetch_serie_a_fixtures", return_value=[fixture]),
            patch("serie_a_bluesky_tool.ask_openai") as mock_openai,
            patch("serie_a_bluesky_tool.ask_claude") as mock_claude,
            patch("serie_a_bluesky_tool.ask_gemini") as mock_gemini,
            patch("serie_a_bluesky_tool.ask_user_pick", return_value="HOME"),
            patch("serie_a_bluesky_tool.load_tracking", return_value=[]),
            patch("serie_a_bluesky_tool.save_tracking", side_effect=lambda items: saved.setdefault("items", items)),
            patch("serie_a_bluesky_tool.load_pick_cache", return_value=cache),
            patch("serie_a_bluesky_tool.save_pick_cache") as mock_save_cache,
            patch("serie_a_bluesky_tool.resolve_day", return_value=target_day),
        ):
            tool.publish_for_day(args)

        mock_openai.assert_not_called()
        mock_claude.assert_not_called()
        mock_gemini.assert_not_called()
        mock_save_cache.assert_not_called()
        self.assertEqual(saved["items"][0]["ai_picks"]["chatgpt"]["reason"], "cached")

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

        args = argparse.Namespace(day="today", dry_run=True, no_cache=True)

        with (
            patch("serie_a_bluesky_tool.fetch_serie_a_fixtures", return_value=[fixture]),
            patch("serie_a_bluesky_tool.ask_openai", return_value={"pick": "HOME", "market": "1X2", "reason": "live", "confidence": 60}),
            patch("serie_a_bluesky_tool.ask_claude", return_value={"pick": "DRAW", "market": "1X2", "reason": "live", "confidence": 55}),
            patch("serie_a_bluesky_tool.ask_gemini", return_value={"pick": "AWAY", "market": "1X2", "reason": "live", "confidence": 58}),
            patch("serie_a_bluesky_tool.ask_user_pick", return_value="HOME"),
            patch("serie_a_bluesky_tool.load_tracking", return_value=[]),
            patch("serie_a_bluesky_tool.save_tracking"),
            patch("serie_a_bluesky_tool.load_pick_cache") as mock_load_cache,
            patch("serie_a_bluesky_tool.save_pick_cache") as mock_save_cache,
            patch("serie_a_bluesky_tool.resolve_day", return_value=date(2026, 5, 8)),
        ):
            tool.publish_for_day(args)

        mock_load_cache.assert_not_called()
        mock_save_cache.assert_not_called()

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
            saved: dict[str, list[dict]] = {}
            args = argparse.Namespace(day="today", dry_run=True, no_cache=True, picks_file=str(picks_path))

            with (
                patch("serie_a_bluesky_tool.fetch_serie_a_fixtures", return_value=[fixture]),
                patch("serie_a_bluesky_tool.ask_openai", return_value={"pick": "HOME", "market": "1X2", "reason": "r", "confidence": 60}),
                patch("serie_a_bluesky_tool.ask_claude", return_value={"pick": "DRAW", "market": "1X2", "reason": "r", "confidence": 55}),
                patch("serie_a_bluesky_tool.ask_gemini", return_value={"pick": "AWAY", "market": "1X2", "reason": "r", "confidence": 58}),
                patch("serie_a_bluesky_tool.ask_user_pick") as mock_ask_user_pick,
                patch("serie_a_bluesky_tool.load_tracking", return_value=[]),
                patch("serie_a_bluesky_tool.save_tracking", side_effect=lambda items: saved.setdefault("items", items)),
                patch("serie_a_bluesky_tool.resolve_day", return_value=date(2026, 5, 8)),
            ):
                tool.publish_for_day(args)

        mock_ask_user_pick.assert_not_called()
        self.assertEqual(saved["items"][0]["my_pick"], "")
        self.assertEqual(saved["items"][0]["my_pick_text"], "")

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
            saved: dict[str, list[dict]] = {}
            args = argparse.Namespace(day="today", dry_run=True, no_cache=True, picks_file=str(picks_path))

            with (
                patch("serie_a_bluesky_tool.fetch_serie_a_fixtures", return_value=[fixture1, fixture2]),
                patch("serie_a_bluesky_tool.ask_openai", return_value={"pick": "HOME", "market": "1X2", "reason": "r", "confidence": 60}),
                patch("serie_a_bluesky_tool.ask_claude", return_value={"pick": "DRAW", "market": "1X2", "reason": "r", "confidence": 55}),
                patch("serie_a_bluesky_tool.ask_gemini", return_value={"pick": "AWAY", "market": "1X2", "reason": "r", "confidence": 58}),
                patch("serie_a_bluesky_tool.ask_user_pick") as mock_ask_user_pick,
                patch("serie_a_bluesky_tool.load_tracking", return_value=[]),
                patch("serie_a_bluesky_tool.save_tracking", side_effect=lambda items: saved.setdefault("items", items)),
                patch("serie_a_bluesky_tool.resolve_day", return_value=date(2026, 5, 8)),
            ):
                tool.publish_for_day(args)

        mock_ask_user_pick.assert_not_called()
        self.assertEqual(saved["items"][0]["my_pick"], "")
        self.assertEqual(saved["items"][0]["my_pick_text"], "")
        self.assertEqual(saved["items"][1]["my_pick_text"], "")
        self.assertEqual(saved["items"][0]["root_post"]["uri"], saved["items"][1]["root_post"]["uri"])
        self.assertEqual(saved["items"][0]["ai_reply_post"]["uri"], saved["items"][1]["ai_reply_post"]["uri"])

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
            args = argparse.Namespace(day="today", dry_run=True, no_cache=True, picks_file=str(picks_path))

            with (
                patch("serie_a_bluesky_tool.fetch_serie_a_fixtures", return_value=[fixture]),
                patch("serie_a_bluesky_tool.ask_openai", return_value={"pick": "HOME", "market": "1X2", "reason": "r", "confidence": 60}),
                patch("serie_a_bluesky_tool.ask_claude", return_value={"pick": "DRAW", "market": "1X2", "reason": "r", "confidence": 55}),
                patch("serie_a_bluesky_tool.ask_gemini", return_value={"pick": "AWAY", "market": "1X2", "reason": "r", "confidence": 58}),
                patch("serie_a_bluesky_tool.load_tracking", return_value=[]),
                patch("serie_a_bluesky_tool.save_tracking") as mock_save_tracking,
                patch("serie_a_bluesky_tool.resolve_day", return_value=date(2026, 5, 8)),
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

        saved: dict[str, list[dict]] = {}
        args = argparse.Namespace(day="yesterday", dry_run=True)

        with (
            patch("serie_a_bluesky_tool.load_tracking", return_value=tracking),
            patch("serie_a_bluesky_tool.fetch_serie_a_fixtures", return_value=[fixture]),
            patch("serie_a_bluesky_tool.save_tracking", side_effect=lambda items: saved.setdefault("items", items)),
            patch("serie_a_bluesky_tool.bsky_login") as mock_login,
            patch("serie_a_bluesky_tool.bsky_create_post") as mock_post,
            patch("serie_a_bluesky_tool.resolve_day", return_value=date(2026, 5, 7)),
        ):
            tool.score_for_day(args)

        mock_login.assert_not_called()
        mock_post.assert_not_called()

        items = saved["items"]
        self.assertFalse(items[0]["scored"])
        self.assertNotIn("result", items[0])

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
            saved: dict[str, list[dict]] = {}
            args = argparse.Namespace(day="today", dry_run=True, no_cache=True, picks_file=str(picks_path))

            with (
                patch("serie_a_bluesky_tool.fetch_serie_a_fixtures", return_value=[fixture]),
                patch("serie_a_bluesky_tool.ask_openai", return_value={"pick": "HOME", "market": "1X2", "reason": "r", "confidence": 60}),
                patch("serie_a_bluesky_tool.ask_claude", return_value={"pick": "DRAW", "market": "1X2", "reason": "r", "confidence": 55}),
                patch("serie_a_bluesky_tool.ask_gemini", return_value={"pick": "AWAY", "market": "1X2", "reason": "r", "confidence": 58}),
                patch("serie_a_bluesky_tool.load_tracking", return_value=[]),
                patch("serie_a_bluesky_tool.save_tracking", side_effect=lambda items: saved.setdefault("items", items)),
                patch("serie_a_bluesky_tool.resolve_day", return_value=date(2026, 5, 8)),
            ):
                tool.publish_for_day(args)

        # Verify my_pick is set to the 1X2 code from [PICKS] section
        self.assertEqual(saved["items"][0]["my_pick"], "AWAY")
        # Verify my_pick_text is empty (since we're using structured [PICKS])
        self.assertEqual(saved["items"][0]["my_pick_text"], "")

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
            saved: dict[str, list[dict]] = {}
            args = argparse.Namespace(day="today", dry_run=True, no_cache=True, picks_file=str(picks_path))

            with (
                patch("serie_a_bluesky_tool.fetch_serie_a_fixtures", return_value=[fixture]),
                patch("serie_a_bluesky_tool.ask_openai", return_value={"pick": "HOME", "market": "1X2", "reason": "r", "confidence": 60}),
                patch("serie_a_bluesky_tool.ask_claude", return_value={"pick": "DRAW", "market": "1X2", "reason": "r", "confidence": 55}),
                patch("serie_a_bluesky_tool.ask_gemini", return_value={"pick": "AWAY", "market": "1X2", "reason": "r", "confidence": 58}),
                patch("serie_a_bluesky_tool.load_tracking", return_value=[]),
                patch("serie_a_bluesky_tool.save_tracking", side_effect=lambda items: saved.setdefault("items", items)),
                patch("serie_a_bluesky_tool.resolve_day", return_value=date(2026, 5, 8)),
            ):
                tool.publish_for_day(args)

        self.assertEqual(saved["items"][0]["my_pick"], "HOME")



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


if __name__ == "__main__":
    unittest.main()
