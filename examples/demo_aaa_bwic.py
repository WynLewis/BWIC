"""
Pretend AAA tranche BWIC demo.

Tranche specs:
  - Original / current face: $100M
  - Coupon: SOFR + 115 bps  (deal's quoted spread)
  - Reinvestment period remaining: 4 years
  - Non-call period remaining: 1 year
  - Legal final: 6 years (4yr reinvest + 2yr amort)
  - Pricing target: prevailing AAA market is +122 (so the deal trades wide of par
    by ~7 bps × WAL)

Workflow:
  1. Build synthetic Intex-style cashflows under Market and Base scenarios.
  2. Set up a one-line BWIC.
  3. Round 1: 5 dealers bid.
  4. Cut to top 3 (with ties advancing).
  5. Round 2: top 3 revise.
  6. Award + cover.
"""

from __future__ import annotations

import sys
from datetime import date

sys.path.insert(0, "src")

from graam.analytics import BondAnalytics
from graam.bwic_pricing import (
    BASE,
    MARKET,
    BwicLineMulti,
    price_bwic_line_multi,
)
from graam.bwic_workflow import Bwic, BwicLine
from graam.enums import Compounding, Frequency
from graam.models import CashflowEntry, CashflowStream


# ---------------------------------------------------------------------------
# 1. Build synthetic AAA cashflows
# ---------------------------------------------------------------------------

def build_aaa_cashflows(
    spread_bps: float = 115,
    reinvest_months: int = 48,
    amort_months: int = 24,
    sofr_curve: list[float] | None = None,
) -> CashflowStream:
    """
    Synthetic CLO AAA cashflows.

    During the reinvestment period (months 1–reinvest_months) the AAA pays only
    coupon — no principal.  After reinvestment, principal amortizes linearly
    over amort_months (a simplified assumption — real CLOs front-load AAA paydowns).
    """
    settle = date(2026, 4, 28)
    balance = 100_000_000.0
    n = reinvest_months + amort_months

    # SOFR forward curve (per-period forwards in % p.a.)
    if sofr_curve is None:
        sofr_curve = (
            [4.00] * 6        # months 1-6:   front-end, near current SOFR
            + [3.75] * 12     # months 7-18:  cuts come in
            + [3.50] * (n - 18)  # months 19+: terminal rate
        )

    spread_dec = spread_bps / 10_000.0  # 115 bps → 0.0115

    cashflows: list[CashflowEntry] = []
    bal = balance

    for i in range(1, n + 1):
        # Cashflow date = 25th of month i ahead of settle
        yr = settle.year + (settle.month + i - 1) // 12
        mo = (settle.month + i - 1) % 12 + 1
        cf_date = date(yr, mo, 25)

        sofr_pct = sofr_curve[i - 1]
        coupon_pct = sofr_pct + spread_bps / 100.0
        interest = bal * (coupon_pct / 100.0) / 12.0

        if i <= reinvest_months:
            principal = 0.0      # reinvestment period — no paydown
        else:
            principal = balance / amort_months

        prev_bal = bal
        bal = max(bal - principal, 0.0)

        cashflows.append(
            CashflowEntry(
                cashflow_date=cf_date,
                interest=interest,
                principal=principal,
                balance=bal,
                prev_balance=prev_bal,
                index_value=sofr_pct,    # the SOFR forward used for this period
            )
        )

    return CashflowStream(
        cashflows=cashflows,
        settle_date=settle,
        balance=balance,
        day_counter_name="Actual360",
        compounding=Compounding.SIMPLE,
        frequency=Frequency.MONTHLY,
    )


# Two scenarios — the same cashflow shape for this demo (Intex would generate
# slightly different ones in practice; for the demo we keep them identical so
# the DM differences come purely from the workflow, not from cashflow shape).
market_stream = build_aaa_cashflows(spread_bps=115)
base_stream = build_aaa_cashflows(spread_bps=115)


# ---------------------------------------------------------------------------
# 2. Find the price that hits 122 bps DM (current market)
# ---------------------------------------------------------------------------

ba = BondAnalytics(market_stream)
price_at_115 = ba.price_from_dm(115)
price_at_122 = ba.price_from_dm(122)
wal = ba.wal()

print("=" * 70)
print("AAA Tranche — pricing reference")
print("=" * 70)
print(f"  Current face:        $100,000,000")
print(f"  Coupon spread:        S+115 bps")
print(f"  Reinvest left:        4 years (48 months)")
print(f"  Non-call left:        1 year")
print(f"  Legal final:          6 years (settle + 72 months)")
print(f"  WAL (to maturity):    {wal:.2f} years")
print()
print(f"  Price @ DM 115 (deal coupon): {price_at_115:.4f}")
print(f"  Price @ DM 122 (current mkt): {price_at_122:.4f}")
print(f"  Implied discount: {price_at_115 - price_at_122:.4f} pts")
print()


# ---------------------------------------------------------------------------
# 3. Set up the BWIC
# ---------------------------------------------------------------------------

nc_date = date(2027, 4, 25)  # 1 year non-call

bwic = Bwic(
    bwic_id="BWIC-2026-04-28-001",
    seller="ABC Asset Mgmt",
    bwic_date=date(2026, 4, 28),
)

line = BwicLine(
    line_id="L1",
    cusip="12345ABC0",
    tranche_name="AAA",
    current_face=100_000_000.0,
    nc_date=nc_date,
    streams={"Market": market_stream, "Base": base_stream},
    pricing_scenario="Market",      # use Market for cover/award DM display
    reserve_price=None,             # no reserve
)
bwic.add_line(line)


# ---------------------------------------------------------------------------
# 4. Round 1 — 5 dealers bid
# ---------------------------------------------------------------------------

bwic.open_round1()
print("=" * 70)
print("ROUND 1 — open")
print("=" * 70)

# Simulate dealer bids around the 122 market level.  Best bidders price
# tighter than 122 (= higher price); weaker bidders price wider (= lower price).
# Price @ DM 122 is roughly the par-of-market level.
r1_bids = [
    ("GS",      ba.price_from_dm(120)),    # tightest (best for seller)
    ("JPM",     ba.price_from_dm(121)),
    ("MS",      ba.price_from_dm(122)),    # at market
    ("BofA",    ba.price_from_dm(122)),    # tied with MS at the cut
    ("Citi",    ba.price_from_dm(125)),    # wide of market
]

for dealer, price in r1_bids:
    bid = bwic.submit_bid("L1", dealer, round(price, 4))
    dm = ba.dm_from_price(price)
    print(f"  R1 bid:  {dealer:6s}  @ {price:.4f}  (DM {dm:.1f} bps)")

bwic.close_round1(top_n=3)
line = bwic.get_line("L1")
print()
print(f"  Cut to top 3 (ties advance):  {line.advancing_dealers}")
print(f"  Note: MS and BofA tied at DM 122 → both advance, so 4 dealers in R2")
print()


# ---------------------------------------------------------------------------
# 5. Round 2 — top advancers improve
# ---------------------------------------------------------------------------

bwic.open_round2()
print("=" * 70)
print("ROUND 2 — open")
print("=" * 70)

r2_bids = [
    ("GS",   ba.price_from_dm(118)),     # GS improves to 118
    ("JPM",  ba.price_from_dm(117)),     # JPM jumps to 117 — best!
    ("MS",   ba.price_from_dm(122)),     # MS stays flat at 122
    ("BofA", ba.price_from_dm(120)),     # BofA improves to 120
]

for dealer, price in r2_bids:
    bid = bwic.submit_bid("L1", dealer, round(price, 4))
    dm = ba.dm_from_price(price)
    print(f"  R2 bid:  {dealer:6s}  @ {price:.4f}  (DM {dm:.1f} bps)")

print()
print("  JPM revises 117 → 116.5 (one final tweak before R2 closes)")
new_price = round(ba.price_from_dm(116.5), 4)
bwic.submit_bid("L1", "JPM", new_price)
print(f"  R2 bid:  JPM     @ {new_price:.4f}  (DM 116.5 bps)")
print()

bwic.close_round2()


# ---------------------------------------------------------------------------
# 6. Award + cover
# ---------------------------------------------------------------------------

print("=" * 70)
print("AWARD")
print("=" * 70)
award = line.award
print(f"  Winner:   {award.award_dealer}  @ {award.award_price:.4f}")
print(f"  Cover:    {award.cover_dealer}  @ {award.cover_price:.4f}")
print(f"  DM @ award: {award.spread_at_award:.1f} bps")
print(f"  DM @ cover: {award.spread_at_cover:.1f} bps")
print()

print("=" * 70)
print("COLOR SHEET")
print("=" * 70)
print(bwic.color_sheet().to_string(index=False))
print()

print("=" * 70)
print("BID LOG (audit trail)")
print("=" * 70)
print(bwic.bid_log().to_string(index=False))
