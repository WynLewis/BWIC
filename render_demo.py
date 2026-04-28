"""
Run the synthetic AAA BWIC demo and save results as PNG images.
"""
import sys
sys.path.insert(0, "src")

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.gridspec import GridSpec
import pandas as pd
from datetime import date, datetime, timezone

from graam.bwic_workflow import Bwic, BwicLine, LineState
from graam.bwic_pricing import price_bwic_line_multi, BwicLineMulti
from graam.analytics import BondAnalytics
from graam.models import CashflowEntry, CashflowStream
from graam.enums import Compounding, Frequency

# ── Palette ──────────────────────────────────────────────────────────────────
BG      = "#0d1117"
PANEL   = "#161b22"
BORDER  = "#30363d"
GREEN   = "#3fb950"
YELLOW  = "#d29922"
BLUE    = "#58a6ff"
RED     = "#f85149"
WHITE   = "#e6edf3"
MUTED   = "#8b949e"
HEADER  = "#1f6feb"

plt.rcParams.update({
    "figure.facecolor": BG,
    "axes.facecolor":   PANEL,
    "text.color":       WHITE,
    "font.family":      "monospace",
    "font.size":        10,
})


# ── Build synthetic AAA cashflows ─────────────────────────────────────────────

def build_stream(spread_bps=115, reinvest=48, amort=24):
    settle = date(2026, 4, 28)
    balance = 100_000_000.0
    n = reinvest + amort
    sofr = [4.00]*6 + [3.75]*12 + [3.50]*(n-18)
    cashflows, bal = [], balance
    for i in range(1, n+1):
        yr = settle.year + (settle.month + i - 1) // 12
        mo = (settle.month + i - 1) % 12 + 1
        cf_date = date(yr, mo, 25)
        sf = sofr[i-1]
        interest = bal * (sf + spread_bps/100.0) / 100.0 / 12.0
        principal = 0.0 if i <= reinvest else balance / amort
        prev_bal, bal = bal, max(bal - principal, 0.0)
        cashflows.append(CashflowEntry(cf_date, interest, principal, bal, prev_bal, sf))
    return CashflowStream(cashflows, settle, balance, "Actual360", Compounding.SIMPLE, Frequency.MONTHLY)

market_stream = build_stream()
base_stream   = build_stream()
ba = BondAnalytics(market_stream)

# ── Pricing reference ─────────────────────────────────────────────────────────

multi = price_bwic_line_multi(BwicLineMulti(
    cusip="12345ABC0", tranche_name="AAA", bid_price=100.0,
    nc_date=date(2027, 4, 25),
    streams={"Market": market_stream, "Base": base_stream},
))
mr, br = multi.results["Market"], multi.results["Base"]

# ── BWIC setup ───────────────────────────────────────────────────────────────

bwic = Bwic(bwic_id="BWIC-2026-04-28-001", seller="ABC Asset Mgmt", bwic_date=date(2026, 4, 28))
line = BwicLine(
    line_id="L1", cusip="12345ABC0", tranche_name="AAA",
    current_face=100_000_000.0, nc_date=date(2027, 4, 25),
    streams={"Market": market_stream, "Base": base_stream},
    pricing_scenario="Market",
)
bwic.add_line(line)

# ── Round 1 bids ─────────────────────────────────────────────────────────────

bwic.open_round1()
r1_bids = [
    ("GS",   ba.price_from_dm(120)),
    ("JPM",  ba.price_from_dm(121)),
    ("MS",   ba.price_from_dm(122)),
    ("BofA", ba.price_from_dm(122)),
    ("Citi", ba.price_from_dm(125)),
]
for dealer, price in r1_bids:
    bwic.submit_bid("L1", dealer, round(price, 4))

bwic.close_round1(top_n=3)

# ── Round 2 bids ─────────────────────────────────────────────────────────────

bwic.open_round2()
r2_bids = [
    ("GS",   ba.price_from_dm(118)),
    ("JPM",  ba.price_from_dm(117)),
    ("MS",   ba.price_from_dm(122)),
    ("BofA", ba.price_from_dm(120)),
]
for dealer, price in r2_bids:
    bwic.submit_bid("L1", dealer, round(price, 4))

bwic.submit_bid("L1", "JPM", round(ba.price_from_dm(116.5), 4))
bwic.close_round2()

award = bwic.get_line("L1").award


# ═════════════════════════════════════════════════════════════════════════════
# Helper: draw a clean dark-mode table on an Axes
# ═════════════════════════════════════════════════════════════════════════════

def draw_table(ax, df, title, col_colors=None):
    ax.set_facecolor(PANEL)
    ax.axis("off")
    ax.set_title(title, color=WHITE, fontsize=11, fontweight="bold",
                 loc="left", pad=8)

    cols   = list(df.columns)
    data   = df.values.tolist()
    n_rows = len(data)
    n_cols = len(cols)

    # Header background
    for j, col in enumerate(cols):
        ax.add_patch(mpatches.FancyBboxPatch(
            (j/(n_cols), 1 - 1/(n_rows+1)),
            1/n_cols, 1/(n_rows+1),
            boxstyle="square,pad=0", linewidth=0,
            facecolor=HEADER, transform=ax.transAxes, zorder=2,
        ))
        ax.text(
            (j + 0.5)/n_cols, 1 - 0.5/(n_rows+1), col,
            ha="center", va="center", transform=ax.transAxes,
            color=WHITE, fontsize=9, fontweight="bold", zorder=3,
        )

    # Rows
    for i, row in enumerate(data):
        bg = PANEL if i % 2 == 0 else "#1c2128"
        for j, val in enumerate(row):
            ax.add_patch(mpatches.FancyBboxPatch(
                (j/n_cols, (n_rows - i - 1)/(n_rows+1)),
                1/n_cols, 1/(n_rows+1),
                boxstyle="square,pad=0", linewidth=0,
                facecolor=bg, transform=ax.transAxes, zorder=1,
            ))
            txt = str(val) if val is not None else "—"
            color = col_colors.get(j, WHITE) if col_colors else WHITE
            ax.text(
                (j + 0.5)/n_cols, (n_rows - i - 0.5)/(n_rows+1), txt,
                ha="center", va="center", transform=ax.transAxes,
                color=color, fontsize=9, zorder=3,
            )

    # Border
    for spine in ax.spines.values():
        spine.set_edgecolor(BORDER)
        spine.set_linewidth(0.5)


# ═════════════════════════════════════════════════════════════════════════════
# IMAGE 1 — Pricing Reference
# ═════════════════════════════════════════════════════════════════════════════

fig, ax = plt.subplots(figsize=(12, 3.5))
fig.patch.set_facecolor(BG)
ax.set_facecolor(PANEL)

ref_df = pd.DataFrame([{
    "CUSIP":         "12345ABC0",
    "Tranche":       "AAA",
    "Face":          "$100MM",
    "NC Date":       "Apr-27",
    "Coupon":        "S+115 bps",
    "WAL":           f"{mr.wal_to_maturity:.2f}y",
    "Mkt DM-Mat":    f"{mr.dm_to_maturity:.1f}",
    "Mkt DM-Call":   f"{mr.dm_to_call:.1f}",
    "Mkt DM-Worst":  f"{mr.dm_to_worst:.1f}",
    "Base DM-Worst": f"{br.dm_to_worst:.1f}",
}])

draw_table(ax, ref_df, "Pricing Reference  (DM at par — Market 20/2/30 · Base 15/5/50)")
fig.suptitle("BWIC-2026-04-28-001 · ABC Asset Mgmt · Apr 28 2026",
             color=MUTED, fontsize=10, y=1.01)
plt.tight_layout()
plt.savefig("demo_01_pricing_reference.png", dpi=150, bbox_inches="tight",
            facecolor=BG)
plt.close()
print("Saved demo_01_pricing_reference.png")


# ═════════════════════════════════════════════════════════════════════════════
# IMAGE 2 — Round 1 bids + advancing dealers
# ═════════════════════════════════════════════════════════════════════════════

r1_df = pd.DataFrame([
    {"Dealer": d, "Price": f"{round(p,4):.4f}", "DM (bps)": f"{ba.dm_from_price(p):.1f}",
     "Advance?": "✓" if d in bwic.get_line("L1").advancing_dealers else ""}
    for d, p in r1_bids
])

fig, ax = plt.subplots(figsize=(8, 3.5))
fig.patch.set_facecolor(BG)
draw_table(ax, r1_df, "Round 1 — 5 dealers bid  (top 3 cut, ties advance → 4 dealers to R2)",
           col_colors={3: GREEN})
plt.tight_layout()
plt.savefig("demo_02_round1.png", dpi=150, bbox_inches="tight", facecolor=BG)
plt.close()
print("Saved demo_02_round1.png")


# ═════════════════════════════════════════════════════════════════════════════
# IMAGE 3 — Round 2 bids + award
# ═════════════════════════════════════════════════════════════════════════════

r2_raw = [
    ("GS",   ba.price_from_dm(118)),
    ("JPM",  ba.price_from_dm(117)),
    ("MS",   ba.price_from_dm(122)),
    ("BofA", ba.price_from_dm(120)),
    ("JPM",  ba.price_from_dm(116.5)),  # revision
]
r2_df = pd.DataFrame([
    {"Dealer": d,
     "Price": f"{round(p,4):.4f}",
     "DM (bps)": f"{ba.dm_from_price(p):.1f}",
     "Note": "revision" if d == "JPM" and i == 4 else ""}
    for i, (d, p) in enumerate(r2_raw)
])

fig, ax = plt.subplots(figsize=(9, 4))
fig.patch.set_facecolor(BG)
draw_table(ax, r2_df, "Round 2 — advancing dealers only  (latest revision wins)")
plt.tight_layout()
plt.savefig("demo_03_round2.png", dpi=150, bbox_inches="tight", facecolor=BG)
plt.close()
print("Saved demo_03_round2.png")


# ═════════════════════════════════════════════════════════════════════════════
# IMAGE 4 — Award + Color Sheet
# ═════════════════════════════════════════════════════════════════════════════

fig = plt.figure(figsize=(13, 6))
fig.patch.set_facecolor(BG)
gs_layout = GridSpec(2, 2, figure=fig, hspace=0.55, wspace=0.4)

# ── Award card ───────────────────────────────────────────────────────────────
ax_award = fig.add_subplot(gs_layout[0, :])
ax_award.set_facecolor("#0c2d1e")
ax_award.axis("off")
ax_award.set_title("AWARD", color=GREEN, fontsize=13, fontweight="bold", loc="left", pad=6)

award_lines = [
    ("Winner", f"JPM  @  {award.award_price:.4f}  (DM {award.spread_at_award:.1f} bps)", GREEN),
    ("Cover",  f"GS   @  {award.cover_price:.4f}  (DM {award.spread_at_cover:.1f} bps)",  YELLOW),
    ("Spread to market (DM 122)", f"+{122 - award.spread_at_award:.1f} bps tighter than market", BLUE),
]
for i, (label, val, color) in enumerate(award_lines):
    y = 0.78 - i * 0.28
    ax_award.text(0.01, y, f"{label}:", transform=ax_award.transAxes,
                  color=MUTED, fontsize=10)
    ax_award.text(0.22, y, val, transform=ax_award.transAxes,
                  color=color, fontsize=11, fontweight="bold")

for spine in ax_award.spines.values():
    spine.set_edgecolor(GREEN)
    spine.set_linewidth(1)

# ── Color sheet table ────────────────────────────────────────────────────────
ax_color = fig.add_subplot(gs_layout[1, :])
color_df = bwic.color_sheet()[["cusip","tranche","face","n_bids","high","cover","low","dm_cover","result"]]
color_df = color_df.copy()
color_df["face"]   = color_df["face"].apply(lambda v: f"${v/1e6:.0f}MM")
color_df["high"]   = color_df["high"].apply(lambda v: f"{v:.4f}" if v else "—")
color_df["cover"]  = color_df["cover"].apply(lambda v: f"{v:.4f}" if v else "—")
color_df["low"]    = color_df["low"].apply(lambda v: f"{v:.4f}" if v else "—")
color_df["dm_cover"]= color_df["dm_cover"].apply(lambda v: f"{v:.1f}" if v else "—")
color_df.columns  = ["CUSIP","Tranche","Face","# Bids","High","Cover","Low","DM Cover","Result"]

draw_table(ax_color, color_df, "Color Sheet",
           col_colors={8: GREEN})

fig.suptitle("BWIC-2026-04-28-001 · ABC Asset Mgmt · Apr 28 2026",
             color=MUTED, fontsize=10)
plt.savefig("demo_04_award_color.png", dpi=150, bbox_inches="tight", facecolor=BG)
plt.close()
print("Saved demo_04_award_color.png")


# ═════════════════════════════════════════════════════════════════════════════
# IMAGE 5 — Bloomberg IB color message
# ═════════════════════════════════════════════════════════════════════════════

best = bwic.get_line("L1")._best_bid_per_dealer()
prices = sorted([b.price for b in best.values()], reverse=True)

color_ib = f"""*** COLOR — BWIC-2026-04-28-001 ***

L1 AAA $100MM:
  TRADED @ {award.award_price:.4f}  (DM {award.spread_at_award:.1f})
  Cover  @ {award.cover_price:.4f}  (DM {award.spread_at_cover:.1f})
  Range  {prices[-1]:.4f} – {prices[0]:.4f} | {len(best)} bids

Thanks for participating."""

fig, ax = plt.subplots(figsize=(10, 3.5))
fig.patch.set_facecolor(BG)
ax.set_facecolor("#0c1a0c")
ax.axis("off")
ax.set_title("Bloomberg IB — Color message", color=GREEN, fontsize=11,
             fontweight="bold", loc="left", pad=6)
ax.text(0.03, 0.5, color_ib, transform=ax.transAxes,
        color=GREEN, fontsize=10, va="center", fontfamily="monospace",
        bbox=dict(boxstyle="round,pad=0.4", facecolor="#0c1a0c", edgecolor=GREEN, linewidth=1))
for spine in ax.spines.values():
    spine.set_edgecolor(GREEN)
    spine.set_linewidth(1)
plt.tight_layout()
plt.savefig("demo_05_ib_color.png", dpi=150, bbox_inches="tight", facecolor=BG)
plt.close()
print("Saved demo_05_ib_color.png")

print("\nAll images saved.")
