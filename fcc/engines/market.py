"""What it actually takes to win a player in *your* league.

The first FAAB model looked only at winning bids, which answers "what did
players like this go for". That is not quite the question you ask standing over
the bid box. The real question is **"what do I have to beat"**, and answering it
needs the losing bids too:

* **Winning bids** are the price to beat. To win a comparable player you must
  clear what the winner paid.
* **Losing bids** show how contested the player was. A $40 winner over a $6
  runner-up means one manager overpaid an empty room; $40 over $38 means the
  room is awake and $41 is genuinely what it costs.

So this module groups a league's transaction log into :class:`Claim` objects —
one per player, carrying the winner and every underbidder — and answers
empirically: *of the comparable claims in this league's history, how many would
a bid of $X have won?*

Nothing here models a player's value. That is `faab.py`'s job, and the two are
deliberately separate: the market says what winning costs, the value model says
what winning is worth. A recommendation needs both, and they can disagree.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

#: Below this many comparable claims, a segment is a curiosity, not a market.
MIN_CLAIMS_FOR_SIGNAL = 4


@dataclass
class Claim:
    """One waiver player, and every bid placed on them."""

    player_id: str
    player_name: str = ""
    position: str | None = None
    week: int | None = None
    winning_bid: int | None = None
    losing_bids: list[int] = field(default_factory=list)

    @property
    def contested(self) -> bool:
        return bool(self.losing_bids)

    @property
    def runner_up(self) -> int | None:
        return max(self.losing_bids) if self.losing_bids else None

    @property
    def bidder_count(self) -> int:
        return len(self.losing_bids) + (1 if self.winning_bid is not None else 0)

    @property
    def margin(self) -> int | None:
        """How far the winner cleared the field by. Large = a soft room."""
        if self.winning_bid is None or self.runner_up is None:
            return None
        return self.winning_bid - self.runner_up

    @property
    def overpay_ratio(self) -> float | None:
        """Winner's premium over the runner-up, as a fraction of the winning bid."""
        if self.winning_bid is None or not self.winning_bid or self.runner_up is None:
            return None
        return (self.winning_bid - self.runner_up) / self.winning_bid


def parse_claims(transactions: list[dict], players: dict[str, dict] | None = None) -> list[Claim]:
    """Group a Sleeper transaction log into per-player claims.

    Sleeper emits one transaction per manager per player, so several rows
    describe a single contested claim. They are grouped by the player being
    added; the completed one is the winner and the failed ones are the field.
    """
    players = players or {}
    grouped: dict[tuple[str, int | None], Claim] = {}

    for txn in transactions or []:
        if txn.get("type") != "waiver":
            continue
        adds = txn.get("adds") or {}
        if not adds:
            continue
        bid = (txn.get("settings") or {}).get("waiver_bid")
        try:
            amount = int(bid)
        except (TypeError, ValueError):
            continue
        if amount < 0:
            continue

        week = txn.get("leg")
        for player_id in adds:
            key = (str(player_id), week)
            claim = grouped.get(key)
            if claim is None:
                raw = players.get(str(player_id), {})
                claim = Claim(
                    player_id=str(player_id),
                    player_name=raw.get("full_name") or raw.get("team") or str(player_id),
                    position=raw.get("position"),
                    week=week,
                )
                grouped[key] = claim

            if txn.get("status") == "complete":
                # Two winners for one player in one week should be impossible;
                # if it happens the log is telling us something we don't model,
                # so keep the larger and say so rather than silently picking.
                if claim.winning_bid is not None:
                    log.warning(
                        "two winning bids for %s in week %s (%s, %s)",
                        claim.player_name, week, claim.winning_bid, amount,
                    )
                    claim.winning_bid = max(claim.winning_bid, amount)
                else:
                    claim.winning_bid = amount
            else:
                claim.losing_bids.append(amount)

    return list(grouped.values())


@dataclass
class BidMarket:
    """The empirical answer to 'what does winning cost here'."""

    claims: list[Claim] = field(default_factory=list)
    segment: str = "all claims"

    # -- filtering --------------------------------------------------------
    @property
    def resolved(self) -> list[Claim]:
        """Claims somebody actually won — the only ones with a price to beat."""
        return [c for c in self.claims if c.winning_bid is not None]

    def for_position(self, position: str | None) -> BidMarket:
        if not position:
            return self
        matched = [c for c in self.claims if (c.position or "").upper() == position.upper()]
        return BidMarket(claims=matched, segment=f"{position.upper()} claims")

    def comparable(self, position: str | None) -> BidMarket:
        """Narrowest segment with enough claims to mean anything.

        Widens from position-specific to the whole league rather than reporting
        a confident number off two data points — and the `segment` field always
        says which one you actually got.
        """
        narrow = self.for_position(position)
        if len(narrow.resolved) >= MIN_CLAIMS_FOR_SIGNAL:
            return narrow
        if narrow.resolved:
            log.info(
                "only %d resolved %s claims; widening to the whole league",
                len(narrow.resolved), position,
            )
        return BidMarket(claims=self.claims, segment="all positions (too few at this position)")

    # -- the questions that matter ----------------------------------------
    def win_probability(self, bid: int) -> float | None:
        """Share of comparable historical claims this bid would have won.

        A tie counts as half: Sleeper settles equal bids on waiver priority, so
        matching the winner is a coin flip rather than a win.
        """
        resolved = self.resolved
        if not resolved:
            return None
        score = 0.0
        for claim in resolved:
            if bid > claim.winning_bid:
                score += 1
            elif bid == claim.winning_bid:
                score += 0.5
        return score / len(resolved)

    def price_for_win_probability(self, target: float, budget_total: int) -> int | None:
        """Smallest whole-dollar bid whose historical win rate reaches *target*."""
        if not self.resolved:
            return None
        ceiling = max([c.winning_bid for c in self.resolved] + [budget_total])
        for bid in range(0, int(ceiling) + 2):
            prob = self.win_probability(bid)
            if prob is not None and prob >= target:
                return bid
        return None

    @property
    def contest_rate(self) -> float | None:
        """How often a claim drew more than one bidder."""
        if not self.claims:
            return None
        return sum(1 for c in self.claims if c.contested) / len(self.claims)

    @property
    def median_overpay(self) -> float | None:
        """Typical premium the winner paid over the runner-up.

        High means winners routinely clear an empty room, so the runner-up
        level is often enough. Low means the room is awake.
        """
        ratios = [c.overpay_ratio for c in self.claims if c.overpay_ratio is not None]
        return statistics.median(ratios) if ratios else None

    @property
    def usable(self) -> bool:
        return len(self.resolved) >= MIN_CLAIMS_FOR_SIGNAL

    def summary(self) -> dict:
        resolved = self.resolved
        winners = [c.winning_bid for c in resolved]
        runners = [c.runner_up for c in self.claims if c.runner_up is not None]
        return {
            "segment": self.segment,
            "claims": len(self.claims),
            "resolved": len(resolved),
            "usable": self.usable,
            "median_winning_bid": statistics.median(winners) if winners else None,
            "max_winning_bid": max(winners) if winners else None,
            "median_runner_up": statistics.median(runners) if runners else None,
            "contest_rate": self.contest_rate,
            "median_overpay": self.median_overpay,
        }
