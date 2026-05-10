#!/usr/bin/env python3
"""Simple CLI to track Serie A bets in a local JSON file."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, asdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_FILE = DATA_DIR / "bets.json"
ALLOWED_RESULTS = {"won", "lost", "void"}


@dataclass
class Bet:
    id: int
    created_at: str
    match: str
    selection: str
    odds: float
    stake: float
    status: str
    payout: float
    profit: float


def ensure_storage() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not DATA_FILE.exists():
        DATA_FILE.write_text("[]\n", encoding="utf-8")


def load_bets() -> list[dict[str, Any]]:
    ensure_storage()
    raw = DATA_FILE.read_text(encoding="utf-8").strip()
    if not raw:
        return []
    return json.loads(raw)


def save_bets(bets: list[dict[str, Any]]) -> None:
    ensure_storage()
    DATA_FILE.write_text(json.dumps(bets, indent=2) + "\n", encoding="utf-8")


def next_bet_id(bets: list[dict[str, Any]]) -> int:
    if not bets:
        return 1
    return max(int(b["id"]) for b in bets) + 1


def add_bet(args: argparse.Namespace) -> None:
    bets = load_bets()
    bet = Bet(
        id=next_bet_id(bets),
        created_at=args.date or date.today().isoformat(),
        match=args.match,
        selection=args.selection,
        odds=round(args.odds, 2),
        stake=round(args.stake, 2),
        status="open",
        payout=0.0,
        profit=0.0,
    )
    bets.append(asdict(bet))
    save_bets(bets)
    print(f"Added bet #{bet.id}: {bet.match} | {bet.selection} @ {bet.odds} (stake {bet.stake:.2f})")


def list_bets(args: argparse.Namespace) -> None:
    bets = load_bets()
    if args.status != "all":
        bets = [b for b in bets if b["status"] == args.status]

    if not bets:
        print("No bets found.")
        return

    header = f"{'ID':<4} {'Date':<12} {'Match':<28} {'Selection':<24} {'Odds':>6} {'Stake':>8} {'Status':<6} {'Profit':>8}"
    print(header)
    print("-" * len(header))

    for b in bets:
        print(
            f"{b['id']:<4} {b['created_at']:<12} {b['match']:<28.28} {b['selection']:<24.24} "
            f"{b['odds']:>6.2f} {b['stake']:>8.2f} {b['status']:<6} {b['profit']:>8.2f}"
        )


def settle_bet(args: argparse.Namespace) -> None:
    bets = load_bets()
    bet = next((b for b in bets if int(b["id"]) == args.id), None)
    if not bet:
        raise SystemExit(f"Bet #{args.id} not found.")

    if bet["status"] != "open":
        raise SystemExit(f"Bet #{args.id} is already settled as '{bet['status']}'.")

    result = args.result
    stake = float(bet["stake"])
    odds = float(bet["odds"])

    if result == "won":
        payout = round(stake * odds, 2)
        profit = round(payout - stake, 2)
    elif result == "lost":
        payout = 0.0
        profit = round(-stake, 2)
    else:  # void
        payout = round(stake, 2)
        profit = 0.0

    bet["status"] = result
    bet["payout"] = payout
    bet["profit"] = profit

    save_bets(bets)
    print(f"Settled bet #{args.id} as {result}. Payout: {payout:.2f}, Profit: {profit:.2f}")


def summary(_args: argparse.Namespace) -> None:
    bets = load_bets()
    open_bets = [b for b in bets if b["status"] == "open"]
    settled_bets = [b for b in bets if b["status"] != "open"]

    open_stake = round(sum(float(b["stake"]) for b in open_bets), 2)
    settled_stake = round(sum(float(b["stake"]) for b in settled_bets), 2)
    settled_profit = round(sum(float(b["profit"]) for b in settled_bets), 2)

    roi = 0.0
    if settled_stake > 0:
        roi = round((settled_profit / settled_stake) * 100, 2)

    won = sum(1 for b in settled_bets if b["status"] == "won")
    lost = sum(1 for b in settled_bets if b["status"] == "lost")
    void = sum(1 for b in settled_bets if b["status"] == "void")

    print("Bets summary")
    print("------------")
    print(f"Total bets:        {len(bets)}")
    print(f"Open bets:         {len(open_bets)}")
    print(f"Settled bets:      {len(settled_bets)}")
    print(f"W/L/V:             {won}/{lost}/{void}")
    print(f"Open exposure:     {open_stake:.2f}")
    print(f"Settled stake:     {settled_stake:.2f}")
    print(f"Settled profit:    {settled_profit:.2f}")
    print(f"ROI (settled):     {roi:.2f}%")


def valid_date(value: str) -> str:
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must be in YYYY-MM-DD format") from exc
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serie A bets tracker")
    subparsers = parser.add_subparsers(dest="command", required=True)

    add = subparsers.add_parser("add", help="Add a new bet")
    add.add_argument("--match", required=True, help="Match, e.g. Inter vs Milan")
    add.add_argument("--selection", required=True, help="Selection, e.g. Inter to win")
    add.add_argument("--odds", required=True, type=float, help="Decimal odds, e.g. 1.85")
    add.add_argument("--stake", required=True, type=float, help="Stake amount")
    add.add_argument("--date", type=valid_date, help="Bet date YYYY-MM-DD (default: today)")
    add.set_defaults(func=add_bet)

    list_cmd = subparsers.add_parser("list", help="List bets")
    list_cmd.add_argument(
        "--status",
        default="all",
        choices=["all", "open", "won", "lost", "void"],
        help="Filter by status",
    )
    list_cmd.set_defaults(func=list_bets)

    settle = subparsers.add_parser("settle", help="Settle a bet")
    settle.add_argument("--id", required=True, type=int, help="Bet ID")
    settle.add_argument("--result", required=True, choices=sorted(ALLOWED_RESULTS), help="Result")
    settle.set_defaults(func=settle_bet)

    summary_cmd = subparsers.add_parser("summary", help="Show quick statistics")
    summary_cmd.set_defaults(func=summary)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if getattr(args, "odds", 1.0) <= 1.0:
        raise SystemExit("odds must be greater than 1.0")
    if getattr(args, "stake", 0.0) <= 0.0:
        raise SystemExit("stake must be greater than 0")

    args.func(args)


if __name__ == "__main__":
    main()
