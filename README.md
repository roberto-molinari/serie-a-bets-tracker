# Serie A Bets Automation Tool

This workspace now includes two scripts:

- `serie_a_bluesky_tool.py`: end-to-end workflow for Serie A fixtures, AI picks, your pick, Bluesky posting, and next-day scoring.
- `bets_tracker.py`: simple local bet tracker created earlier.

## What `serie_a_bluesky_tool.py` does

1. Fetches Serie A fixtures for `today` or `tomorrow`.
2. For each match asks:
   - ChatGPT (OpenAI API)
   - Claude (Anthropic API)
   - Gemini (Google API)
3. Prompts you for your own pick (`HOME`, `DRAW`, `AWAY`).
4. Posts your pick as a Bluesky post.
5. Posts AI picks as a reply to your post.
6. Saves thread metadata in `data/posted_picks.json`.
7. Later, posts one daily scoreboard reply for tracked picks (`score` command).

## Requirements

- Python 3.10+

No third-party Python package is required for this script.

## Environment variables

Set these before running `publish` or `score`.

For a system-wide setup on macOS with zsh, add them to `~/.zshrc`, then reload your shell with `source ~/.zshrc`:

```bash
export OPENAI_API_KEY="..."
export ANTHROPIC_API_KEY="..."
export GEMINI_API_KEY="..."
export BSKY_HANDLE="your-handle.bsky.social"
export BSKY_APP_PASSWORD="xxxx-xxxx-xxxx-xxxx"
```

To verify they are available in new terminals:

```bash
printenv OPENAI_API_KEY
printenv ANTHROPIC_API_KEY
printenv GEMINI_API_KEY
printenv BSKY_HANDLE
```

Optional model overrides:

```bash
export OPENAI_MODEL="gpt-4.1-mini"
export ANTHROPIC_MODEL="claude-sonnet-4-6"
export GEMINI_MODEL="gemini-2.5-flash"
```

## Usage

### List fixtures

```bash
python3 serie_a_bluesky_tool.py fixtures --day today
python3 serie_a_bluesky_tool.py fixtures --day tomorrow
```

### Publish picks and posts

```bash
python3 serie_a_bluesky_tool.py publish --day today
python3 serie_a_bluesky_tool.py publish --day yesterday
```

Test mode (no posting):

```bash
python3 serie_a_bluesky_tool.py publish --day today --dry-run
```

The command will:

- show AI picks in terminal,
- ask for your pick per match,
- create Bluesky thread posts,
- persist tracking data to `data/posted_picks.json`.
- cache AI provider responses in `data/ai_pick_cache.json` for reuse on repeated test runs.

With `--dry-run`, it prints post previews and still records generated picks in `data/posted_picks.json`, but does not create Bluesky posts.

If any of the three AI providers fails to return a pick for any fixture, `publish` aborts before prompting you for picks and before creating any posts, so you do not end up with partial AI coverage.

By default, `publish` uses cached AI picks for the same fixture/day/provider/model to avoid extra token usage during testing. Use `--no-cache` to force fresh API calls:

```bash
python3 serie_a_bluesky_tool.py publish --day today --dry-run --no-cache
```

To run non-interactively, provide your picks in a text file.

You can use fully free-form text with no structure (one non-empty line per fixture, in the same order shown by `fixtures`):

```text
I like Cagliari at home, thin edge.
Inter to win, better squad and form.
Juventus should control this matchup.
```

You can also use keyed lines if you prefer:

```text
# key can be fixture id, or "Home vs Away"
100=HOME
Lazio vs Internazionale=AWAY
```

Then run:
For automatic scoring of your picks, include a `[PICKS]` section with explicit 1X2 codes:

```text
My picks for today:
I like Napoli at home, their form is strong.
Bologna has been playing well away recently.

[PICKS]
Napoli vs Bologna = HOME
Juventus vs Roma = AWAY
[/PICKS]
```

The `[PICKS]` section is automatically removed from the Bluesky post (only the narrative text above it is posted), but the 1X2 codes are extracted and used for automatic scoring.

Then run:

```bash
python3 serie_a_bluesky_tool.py publish --day today --dry-run --picks-file data/my_picks.txt
```

If any fixture for that day is missing from the file, `publish` aborts before posting.

When `--picks-file` is used, `publish` creates:

- one root post containing the full text contents of `my_picks.txt`, and
- one single reply to that root containing all ChatGPT/Claude/Gemini picks for the day's fixtures.

When your picks-file text is not a strict 1X2 value (`HOME`/`DRAW`/`AWAY`), it is posted as-is on Bluesky and your personal pick is marked as unavailable for automatic result scoring.

AI reply previews/posts are shown with human-readable pick text (for example, `Napoli to win`, `Draw`) rather than only 1X2 codes.
**Note on scoring:** If you include a `[PICKS]` section with explicit 1X2 codes (HOME/DRAW/AWAY), your picks will be automatically scored when you run `score` after the matches finish. If you don't include a `[PICKS]` section, your freeform picks text is posted as-is on Bluesky, but automatic scoring is not available for your picks.

AI reply previews/posts are shown with human-readable pick text (for example, `Napoli to win`, `Draw`) rather than only 1X2 codes.
### Score yesterday's picks and post updates

```bash
python3 serie_a_bluesky_tool.py score --day yesterday
```

Test mode (no posting):

```bash
python3 serie_a_bluesky_tool.py score --day yesterday --dry-run
```

When all tracked fixtures for that day are final, `score` posts one scoreboard reply to the existing thread, ordered as Minvest, Gemini, Claude, ChatGPT.

With `--dry-run`, it prints the scoreboard preview without posting and without marking tracked picks as scored.

## Notes and current assumptions

- Picks are constrained to 1X2 (`HOME`, `DRAW`, `AWAY`) to allow automatic scoring.
- Fixture feed source is ESPN Serie A scoreboard API.
- Bluesky posts are capped to 300 characters.

## Existing local tracker

The original local tracker is still available:

```bash
python3 bets_tracker.py --help
```

## Tests

Run unit tests with:

```bash
python3 -m unittest discover -s tests -p "test_*.py" -v
```
