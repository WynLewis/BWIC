"""
Two-round BWIC workflow state machine.

Conventions (confirmed):
  - Round 1: any dealer can bid.
  - Cut to advance: top 3 by best R1 bid per line.  Ties at the cut price all advance,
    so a 4-way tie at 3rd place produces 4 advancing dealers.
  - Round 2: only advancing dealers can bid.  R2 bid must be >= dealer's R1 best bid
    (they can stay flat but never lower).  Dealers may revise their R2 bid until R2
    closes — latest submission replaces earlier ones.
  - Award: highest best-bid (max of R1, R2) per dealer; winner = top dealer.
  - Cover: highest best-bid from a DIFFERENT dealer than the winner.
  - Reserve: optional — if set and the high bid is below it, line goes DNT.

States::

    SETUP → ROUND1_OPEN → ROUND1_CLOSED → ROUND2_OPEN → ROUND2_CLOSED → AWARDED
                                                                  ↘
                                                                   DNT

Typical usage::

    bwic = Bwic(bwic_id="BWIC-2026-04-28")
    bwic.add_line(BwicLine(line_id="L1", cusip="...", current_face=100e6))
    bwic.open_round1()
    bwic.submit_bid("L1", dealer="GS", price=99.50)
    bwic.submit_bid("L1", dealer="JPM", price=99.625)
    bwic.submit_bid("L1", dealer="MS", price=99.50)
    bwic.submit_bid("L1", dealer="BofA", price=99.375)
    bwic.close_round1()                 # top 3 advance (or more on ties)
    bwic.open_round2()
    bwic.submit_bid("L1", "JPM", 99.75) # JPM improves
    bwic.submit_bid("L1", "GS",  99.625)
    bwic.submit_bid("L1", "MS",  99.50) # MS stays flat
    bwic.close_round2()
    print(bwic.get_line("L1").award)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Optional

from .analytics import BondAnalytics
from .models import CashflowStream


# ---------------------------------------------------------------------------
# State enum
# ---------------------------------------------------------------------------

class LineState(str, Enum):
    SETUP = "setup"
    ROUND1_OPEN = "round1_open"
    ROUND1_CLOSED = "round1_closed"
    ROUND2_OPEN = "round2_open"
    ROUND2_CLOSED = "round2_closed"
    AWARDED = "awarded"
    DNT = "dnt"


class BidError(Exception):
    """Raised when a bid is invalid for the current state (wrong round, lowering R2, etc.)."""


# ---------------------------------------------------------------------------
# Bid
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Bid:
    """Single bid submission. Immutable — revisions create a new Bid object."""
    line_id: str
    dealer: str
    price: float
    round: int                              # 1 or 2
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if self.price <= 0:
            raise ValueError(f"Bid price must be > 0, got {self.price}")
        if self.round not in (1, 2):
            raise ValueError(f"Round must be 1 or 2, got {self.round}")


# ---------------------------------------------------------------------------
# Award
# ---------------------------------------------------------------------------

@dataclass
class LineAward:
    """Award outcome for one BWIC line."""
    award_dealer: Optional[str] = None
    award_price: Optional[float] = None
    cover_dealer: Optional[str] = None
    cover_price: Optional[float] = None
    is_dnt: bool = False
    dnt_reason: str = ""
    spread_at_award: Optional[float] = None     # bps DM at the winning price
    spread_at_cover: Optional[float] = None     # bps DM at the cover price

    def __str__(self) -> str:
        if self.is_dnt:
            return f"DNT ({self.dnt_reason or 'no acceptable bid'})"
        cover = f"cover {self.cover_dealer} @ {self.cover_price:.4f}" if self.cover_dealer else "no cover"
        return f"AWARD {self.award_dealer} @ {self.award_price:.4f} | {cover}"


# ---------------------------------------------------------------------------
# BwicLine
# ---------------------------------------------------------------------------

@dataclass
class BwicLine:
    """One line on the BWIC list — a single tranche being auctioned."""
    line_id: str
    cusip: str = ""
    tranche_name: str = ""
    current_face: float = 0.0
    nc_date: Optional[date] = None
    reserve_price: Optional[float] = None       # min acceptable price; None = no reserve

    # Pricing context (optional — used to compute DMs at award/cover)
    streams: dict[str, CashflowStream] = field(default_factory=dict)
    pricing_scenario: str = "Market"            # which stream to use for cover/award DMs

    # State
    state: LineState = LineState.SETUP
    bids: list[Bid] = field(default_factory=list)
    advancing_dealers: list[str] = field(default_factory=list)
    award: Optional[LineAward] = None

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------

    def open_round1(self) -> None:
        if self.state != LineState.SETUP:
            raise BidError(f"Cannot open R1 from state {self.state.value}")
        self.state = LineState.ROUND1_OPEN

    def close_round1(self, top_n: int = 3) -> None:
        """
        Close R1 and determine which dealers advance.

        Top-n by best R1 bid.  Ties at the cut price all advance (e.g. a 4-way tie
        at 3rd place produces 4 advancing dealers).
        """
        if self.state != LineState.ROUND1_OPEN:
            raise BidError(f"Cannot close R1 from state {self.state.value}")

        r1_best = self._best_bid_per_dealer(round_filter=1)
        if not r1_best:
            self.advancing_dealers = []
            self.award = LineAward(is_dnt=True, dnt_reason="No R1 bids")
            self.state = LineState.DNT
            return

        # Sort descending by price, ascending by timestamp (deterministic)
        ranked = sorted(r1_best.values(), key=lambda b: (-b.price, b.timestamp))

        if len(ranked) <= top_n:
            self.advancing_dealers = [b.dealer for b in ranked]
        else:
            cut_price = ranked[top_n - 1].price
            self.advancing_dealers = [b.dealer for b in ranked if b.price >= cut_price]

        self.state = LineState.ROUND1_CLOSED

    def open_round2(self) -> None:
        if self.state != LineState.ROUND1_CLOSED:
            raise BidError(f"Cannot open R2 from state {self.state.value}")
        if not self.advancing_dealers:
            raise BidError("No dealers advanced from R1 — cannot open R2")
        self.state = LineState.ROUND2_OPEN

    def close_round2(self) -> None:
        if self.state != LineState.ROUND2_OPEN:
            raise BidError(f"Cannot close R2 from state {self.state.value}")
        self.state = LineState.ROUND2_CLOSED
        self._compute_award()

    def mark_dnt(self, reason: str = "") -> None:
        """Force-mark this line as Did Not Trade.  Allowed from any state."""
        self.state = LineState.DNT
        self.award = LineAward(is_dnt=True, dnt_reason=reason)

    # ------------------------------------------------------------------
    # Bid submission
    # ------------------------------------------------------------------

    def submit_bid(self, dealer: str, price: float, round_: Optional[int] = None) -> Bid:
        """
        Submit a bid for this line.

        Round is auto-detected from state when not provided.  R2 bids are
        validated against the dealer's best R1 bid (cannot lower).
        """
        if round_ is None:
            if self.state == LineState.ROUND1_OPEN:
                round_ = 1
            elif self.state == LineState.ROUND2_OPEN:
                round_ = 2
            else:
                raise BidError(f"No round currently open (state={self.state.value})")

        if round_ == 1 and self.state != LineState.ROUND1_OPEN:
            raise BidError(f"R1 not open (state={self.state.value})")
        if round_ == 2 and self.state != LineState.ROUND2_OPEN:
            raise BidError(f"R2 not open (state={self.state.value})")

        if round_ == 2:
            if dealer not in self.advancing_dealers:
                raise BidError(f"{dealer} did not advance to R2 on line {self.line_id}")
            r1_best = self._best_bid_per_dealer(round_filter=1).get(dealer)
            if r1_best is not None and price < r1_best.price - 1e-9:
                raise BidError(
                    f"{dealer} R2 bid ({price}) cannot be below their R1 best ({r1_best.price})"
                )

        bid = Bid(line_id=self.line_id, dealer=dealer, price=price, round=round_)
        self.bids.append(bid)
        return bid

    def revise_bid(self, dealer: str, price: float) -> Bid:
        """Alias for submit_bid — semantic for R2 revisions."""
        return self.submit_bid(dealer, price)

    def delete_bid_by_idx(self, idx: int) -> None:
        """
        Remove the bid at position idx in self.bids.

        Only allowed while that bid's round is still open (R1 open for R1 bids,
        R2 open for R2 bids).  Raises BidError if the round is already closed.
        """
        if idx < 0 or idx >= len(self.bids):
            raise BidError(f"Bid index {idx} out of range (0–{len(self.bids)-1})")
        b = self.bids[idx]
        if b.round == 1 and self.state != LineState.ROUND1_OPEN:
            raise BidError("Cannot delete an R1 bid after R1 is closed")
        if b.round == 2 and self.state != LineState.ROUND2_OPEN:
            raise BidError("Cannot delete an R2 bid after R2 is closed")
        self.bids = self.bids[:idx] + self.bids[idx + 1:]

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def round1_bids(self) -> list[Bid]:
        return [b for b in self.bids if b.round == 1]

    def round2_bids(self) -> list[Bid]:
        return [b for b in self.bids if b.round == 2]

    def latest_bid(self, dealer: str, round_: Optional[int] = None) -> Optional[Bid]:
        """Most recent bid from dealer (latest timestamp wins for revisions)."""
        candidates = [b for b in self.bids if b.dealer == dealer]
        if round_ is not None:
            candidates = [b for b in candidates if b.round == round_]
        return max(candidates, key=lambda b: b.timestamp, default=None)

    def _best_bid_per_dealer(self, round_filter: Optional[int] = None) -> dict[str, Bid]:
        """
        Each dealer's best bid (highest price; latest timestamp on price ties).

        Within a single round we use the LATEST bid as that dealer's R-N bid
        (revisions replace earlier bids).  Across rounds we take the higher of R1 and R2.
        """
        # Step 1: for each (dealer, round), keep the latest submission
        latest_in_round: dict[tuple[str, int], Bid] = {}
        for b in self.bids:
            if round_filter is not None and b.round != round_filter:
                continue
            key = (b.dealer, b.round)
            existing = latest_in_round.get(key)
            if existing is None or b.timestamp >= existing.timestamp:
                latest_in_round[key] = b

        # Step 2: for each dealer, take the highest-priced of their per-round latests
        best: dict[str, Bid] = {}
        for (dealer, _round), bid in latest_in_round.items():
            existing = best.get(dealer)
            if existing is None or bid.price > existing.price:
                best[dealer] = bid
            elif bid.price == existing.price and bid.timestamp > existing.timestamp:
                best[dealer] = bid
        return best

    def best_bid_per_dealer(self) -> dict[str, Bid]:
        """Public accessor — best bid per dealer across both rounds."""
        return self._best_bid_per_dealer()

    def winning_bid(self) -> Optional[Bid]:
        if not self.award or self.award.is_dnt:
            return None
        ranked = sorted(self._best_bid_per_dealer().values(), key=lambda b: (-b.price, b.timestamp))
        return ranked[0] if ranked else None

    def cover_bid(self) -> Optional[Bid]:
        """Highest bid from a different dealer than the winner."""
        winner = self.winning_bid()
        if winner is None:
            return None
        ranked = sorted(
            (b for b in self._best_bid_per_dealer().values() if b.dealer != winner.dealer),
            key=lambda b: (-b.price, b.timestamp),
        )
        return ranked[0] if ranked else None

    # ------------------------------------------------------------------
    # Award computation
    # ------------------------------------------------------------------

    def _compute_award(self) -> None:
        best = self._best_bid_per_dealer()
        if not best:
            self.award = LineAward(is_dnt=True, dnt_reason="No bids received")
            self.state = LineState.DNT
            return

        ranked = sorted(best.values(), key=lambda b: (-b.price, b.timestamp))
        winner = ranked[0]

        # Reserve price check
        if self.reserve_price is not None and winner.price < self.reserve_price:
            self.award = LineAward(
                is_dnt=True,
                dnt_reason=f"High bid {winner.price:.4f} below reserve {self.reserve_price:.4f}",
            )
            self.state = LineState.DNT
            return

        # Cover = highest bid from a different dealer
        cover = next((b for b in ranked[1:] if b.dealer != winner.dealer), None)

        award = LineAward(
            award_dealer=winner.dealer,
            award_price=winner.price,
            cover_dealer=cover.dealer if cover else None,
            cover_price=cover.price if cover else None,
        )

        # Optional: DM at award / cover using the chosen pricing stream
        stream = self.streams.get(self.pricing_scenario) if self.streams else None
        if stream:
            try:
                ba = BondAnalytics(stream)
                award.spread_at_award = ba.dm_from_price(winner.price)
                if cover is not None:
                    award.spread_at_cover = ba.dm_from_price(cover.price)
            except Exception:
                pass  # leave as None; pricing failure shouldn't block the award

        self.award = award
        self.state = LineState.AWARDED


# ---------------------------------------------------------------------------
# Bwic — top-level container
# ---------------------------------------------------------------------------

@dataclass
class Bwic:
    """A complete BWIC: list of lines plus round-level controls."""
    bwic_id: str
    seller: str = ""
    bwic_date: Optional[date] = None
    cover_time_r1: Optional[datetime] = None
    cover_time_r2: Optional[datetime] = None
    lines: list[BwicLine] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Line management
    # ------------------------------------------------------------------

    def add_line(self, line: BwicLine) -> None:
        if any(l.line_id == line.line_id for l in self.lines):
            raise ValueError(f"Duplicate line_id: {line.line_id}")
        self.lines.append(line)

    def get_line(self, line_id: str) -> BwicLine:
        for l in self.lines:
            if l.line_id == line_id:
                return l
        raise KeyError(f"No line with id {line_id!r}")

    # ------------------------------------------------------------------
    # Round-level transitions (apply to every active line)
    # ------------------------------------------------------------------

    def open_round1(self) -> None:
        for l in self.lines:
            if l.state == LineState.SETUP:
                l.open_round1()

    def close_round1(self, top_n: int = 3) -> None:
        for l in self.lines:
            if l.state == LineState.ROUND1_OPEN:
                l.close_round1(top_n=top_n)

    def open_round2(self) -> None:
        for l in self.lines:
            if l.state == LineState.ROUND1_CLOSED and l.advancing_dealers:
                l.open_round2()

    def close_round2(self) -> None:
        for l in self.lines:
            if l.state == LineState.ROUND2_OPEN:
                l.close_round2()

    # ------------------------------------------------------------------
    # Bid submission / queries
    # ------------------------------------------------------------------

    def submit_bid(self, line_id: str, dealer: str, price: float) -> Bid:
        return self.get_line(line_id).submit_bid(dealer, price)

    def delete_bid_by_idx(self, line_id: str, idx: int) -> None:
        """Delete bid at position idx within the named line's bids list."""
        self.get_line(line_id).delete_bid_by_idx(idx)

    # ------------------------------------------------------------------
    # Reporting — DataFrames for the marimo blotter
    # ------------------------------------------------------------------

    def blotter(self):
        """One row per line summarising state, top bids, award, and cover."""
        import pandas as pd
        rows = []
        for l in self.lines:
            row = {
                "line_id": l.line_id,
                "cusip": l.cusip,
                "tranche": l.tranche_name,
                "face": l.current_face,
                "state": l.state.value,
                "n_r1": len(l.round1_bids()),
                "n_r2": len(l.round2_bids()),
                "advanced": ",".join(l.advancing_dealers),
            }
            if l.award:
                row.update({
                    "award_dealer": l.award.award_dealer,
                    "award_price": l.award.award_price,
                    "cover_dealer": l.award.cover_dealer,
                    "cover_price": l.award.cover_price,
                    "dm_award": l.award.spread_at_award,
                    "dm_cover": l.award.spread_at_cover,
                    "dnt": l.award.is_dnt,
                })
            rows.append(row)
        return pd.DataFrame(rows)

    def bid_log(self):
        """One row per bid — the audit trail."""
        import pandas as pd
        rows = []
        for l in self.lines:
            for b in l.bids:
                rows.append({
                    "line_id": b.line_id,
                    "tranche": l.tranche_name,
                    "round": b.round,
                    "dealer": b.dealer,
                    "price": b.price,
                    "timestamp": b.timestamp,
                })
        return pd.DataFrame(rows).sort_values(["line_id", "timestamp"]).reset_index(drop=True)

    def color_sheet(self):
        """
        Post-trade color: one row per awarded line with award/cover/range.
        Suitable for emailing out to the market after the BWIC closes.
        """
        import pandas as pd
        rows = []
        for l in self.lines:
            if not l.award:
                continue
            best = l._best_bid_per_dealer()
            prices = sorted([b.price for b in best.values()], reverse=True)
            row = {
                "cusip": l.cusip,
                "tranche": l.tranche_name,
                "face": l.current_face,
                "n_bids": len(best),
                "high": prices[0] if prices else None,
                "cover": l.award.cover_price,
                "low": prices[-1] if prices else None,
                "dm_cover": l.award.spread_at_cover,
                "result": "DNT" if l.award.is_dnt else "TRADED",
            }
            rows.append(row)
        return pd.DataFrame(rows)
