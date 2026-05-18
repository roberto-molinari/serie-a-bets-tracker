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

Third-party Python package for X posting:

- `tweepy` (required only when using `--platform x` or `--platform both`)

Install it with:

```bash
python3 -m pip install tweepy
```

## Environment variables

Set these before running `publish` or `score`.

For a system-wide setup on macOS with zsh, add them to `~/.zshrc`, then reload your shell with `source ~/.zshrc`:

```bash
export OPENAI_API_KEY="..."
export ANTHROPIC_API_KEY="..."
export GEMINI_API_KEY="..."
export BSKY_HANDLE="your-handle.bsky.social"
export BSKY_APP_PASSWORD="xxxx-xxxx-xxxx-xxxx"
export X_CONSUMER_KEY="..."
export X_CONSUMER_SECRET="..."
export X_ACCESS_TOKEN="..."
export X_ACCESS_TOKEN_SECRET="..."
export X_HANDLE="your_x_handle"
```

To verify they are available in new terminals:

```bash
printenv OPENAI_API_KEY
printenv ANTHROPIC_API_KEY
printenv GEMINI_API_KEY
printenv BSKY_HANDLE
printenv X_CONSUMER_KEY
printenv X_ACCESS_TOKEN
```

X variable notes:

- `X_CONSUMER_KEY`, `X_CONSUMER_SECRET`, `X_ACCESS_TOKEN`, and `X_ACCESS_TOKEN_SECRET` are required for X posting.
- `X_HANDLE` is optional and is only used to print friendly tweet URLs in command output.
- If you run Bluesky-only (`--platform bluesky`), X variables are not required.

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

Select posting platform:

```bash
# Bluesky only
python3 serie_a_bluesky_tool.py publish --day today --platform bluesky

# X only
python3 serie_a_bluesky_tool.py publish --day today --platform x

# Both (default)
python3 serie_a_bluesky_tool.py publish --day today --platform both
```

Optional session filter (local kickoff time):

```bash
# All fixtures for the day (default)
python3 serie_a_bluesky_tool.py publish --day tomorrow --session all

# Morning fixtures only (before 12:00 local)
python3 serie_a_bluesky_tool.py publish --day tomorrow --session morning

# Afternoon fixtures only (12:00 local and later)
python3 serie_a_bluesky_tool.py publish --day tomorrow --session afternoon
```

Test mode (no posting):

```bash
python3 serie_a_bluesky_tool.py publish --day today --dry-run
```

The command will:

- show AI picks in terminal,
- ask for your pick per match,
- create thread posts on the selected platform(s),
- persist tracking data to `data/posted_picks.json`.
- cache AI provider responses in `data/ai_pick_cache.json` for reuse on repeated test runs.

With `--dry-run`, it prints post previews and still records generated picks in `data/posted_picks.json`, but does not create posts.

With `--platform x` or `--platform both`, `publish` creates an X root post and then an X AI reply to that root.

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

Use the committed template file to bootstrap your local picks file once:

```bash
cp data/my_picks.example.txt data/my_picks.txt
```

`data/my_picks.txt` is intended for local use and is git-ignored.

Then run:

```bash
python3 serie_a_bluesky_tool.py publish --day today --dry-run --picks-file data/my_picks.txt
```

For high-volume days, you can keep one file and split narrative and picks into session blocks:

```text
[MORNING]
Morning narrative text...
[PICKS]
Inter vs Milan = HOME
Juventus vs Roma = AWAY
[/PICKS]
[/MORNING]

[AFTERNOON]
Afternoon narrative text...
[PICKS]
Napoli vs Lazio = HOME
Atalanta vs Fiorentina = DRAW
[/PICKS]
[/AFTERNOON]
```

When blocks are present, use `--session morning` or `--session afternoon` to choose the block.
When blocks are not present, the file continues to work exactly as before.

If any fixture for that day is missing from the file, `publish` aborts before posting.

When `--picks-file` is used, `publish` creates:

- one root post containing the full text contents of `my_picks.txt`, and
- one single reply to that root containing all ChatGPT/Claude/Gemini picks for the day's fixtures.

When your picks-file text is not a strict 1X2 value (`HOME`/`DRAW`/`AWAY`), it is posted as-is on Bluesky and your personal pick is marked as unavailable for automatic result scoring.

AI reply previews/posts are shown with human-readable pick text (for example, `Napoli to win`, `Draw`) rather than only 1X2 codes.
**Note on scoring:** If you include a `[PICKS]` section with explicit 1X2 codes (HOME/DRAW/AWAY), your picks will be automatically scored when you run `score` after the matches finish. If you don't include a `[PICKS]` section, your freeform picks text is posted as-is on Bluesky, but automatic scoring is not available for your picks.

### Score yesterday's picks and post updates

```bash
python3 serie_a_bluesky_tool.py score --day yesterday
```

Select posting platform:

```bash
# Bluesky only
python3 serie_a_bluesky_tool.py score --day yesterday --platform bluesky

# X only
python3 serie_a_bluesky_tool.py score --day yesterday --platform x

# Both (default)
python3 serie_a_bluesky_tool.py score --day yesterday --platform both
```

Optional session filter (same definitions as `publish`):

```bash
python3 serie_a_bluesky_tool.py score --day tomorrow --session morning
python3 serie_a_bluesky_tool.py score --day tomorrow --session afternoon
```

Test mode (no posting):

```bash
python3 serie_a_bluesky_tool.py score --day yesterday --dry-run
```

When all tracked fixtures for that day are final, `score` posts one scoreboard reply to the existing thread, ordered as Minvest, Gemini, Claude, ChatGPT.

For both Bluesky and X, the scoreboard reply is posted as a reply to the AI picks reply (not directly to the root post).

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

### Test posting helper

Use `test_posting.py` to run a mocked publish flow and quickly verify posting behavior for Bluesky and/or X.

What it does:

- Mocks fixtures and AI picks.
- Runs one publish cycle through `serie_a_bluesky_tool.py`.
- Supports dry-run previews and live posting.
- Adds a unique timestamp suffix by default to avoid duplicate-post rejection on repeated X test runs.

Examples:

```bash
# Dry-run to X only
python3 test_posting.py --platform x --dry-run

# Live post to X only
python3 test_posting.py --platform x

# Live post to both platforms
python3 test_posting.py --platform both

# Force a custom uniqueness suffix
python3 test_posting.py --platform x --suffix smoke-test-1
```

Options:

- `--platform`: `bluesky`, `x`, or `both` (default: `both`).
- `--dry-run`: preview mode; no posts are created.
- `--suffix`: optional text appended into mocked fixture content to guarantee unique post text.

Environment variables:

- For `--platform bluesky`: set `BSKY_HANDLE` and `BSKY_APP_PASSWORD`.
- For `--platform x`: set `X_CONSUMER_KEY`, `X_CONSUMER_SECRET`, `X_ACCESS_TOKEN`, `X_ACCESS_TOKEN_SECRET`.
