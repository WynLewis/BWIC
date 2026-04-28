"""
BWIC Manager — marimo reactive application for two-round CLO BWIC auctions.

Stage 1 (this file): BWIC setup, line builder, Intex upload, multi-scenario pricing table.

Run with::

    marimo edit app.py        # interactive editor
    marimo run  app.py        # read-only app server
"""
from __future__ import annotations

import marimo as mo

app = mo.App(width="full", app_title="BWIC Manager")


# ============================================================================
# Imports
# ============================================================================

@app.cell
def _imports():
    import sys
    import io
    sys.path.insert(0, "src")

    import marimo as mo
    import pandas as pd
    from datetime import date, datetime, timedelta

    from graam.bwic_workflow import Bwic, BwicLine as WfBwicLine, LineState, BidError
    from graam.bwic_pricing import price_bwic_line_multi, BwicLineMulti
    from graam.analytics import BondAnalytics
    from graam.intex_loader import load_intex_excel
    from graam.models import CashflowEntry, CashflowStream
    from graam.enums import Compounding, Frequency

    return (
        BidError,
        BondAnalytics,
        Bwic,
        BwicLineMulti,
        CashflowEntry,
        CashflowStream,
        Compounding,
        Frequency,
        LineState,
        WfBwicLine,
        date,
        datetime,
        io,
        load_intex_excel,
        mo,
        pd,
        price_bwic_line_multi,
        timedelta,
    )


# ============================================================================
# Application state
# ============================================================================

@app.cell
def _state(mo):
    get_state, set_state = mo.state(
        {
            "bwic_id": "BWIC-2026-04-28-001",
            "seller": "ABC Asset Mgmt",
            "lines_config": [],   # list of dicts: line_id, cusip, tranche, face, nc_date, reserve, market_stream, base_stream
            "bwic": None,         # graam.bwic_workflow.Bwic — created on Open R1
            "phase": "setup",     # setup | r1_open | r1_closed | r2_open | awarded
            "notifications": [],
            "r1_end_time": None,  # datetime — when R1 should close (for countdown display)
            "r2_end_time": None,
        }
    )
    return get_state, set_state


# ============================================================================
# Helper functions
# ============================================================================

@app.cell
def _helpers(BwicLineMulti, io, load_intex_excel, price_bwic_line_multi):

    def load_stream_from_bytes(data: bytes, settle_date=None, balance_override=None):
        """Load a CashflowStream from uploaded Excel bytes."""
        return load_intex_excel(
            io.BytesIO(data),
            settle_date=settle_date,
            balance_override=balance_override,
        )

    def price_line_at(line_cfg: dict, bid_price: float):
        """Price a line config at the given bid under all available scenarios."""
        streams = {}
        if line_cfg.get("market_stream") is not None:
            streams["Market"] = line_cfg["market_stream"]
        if line_cfg.get("base_stream") is not None:
            streams["Base"] = line_cfg["base_stream"]
        if not streams:
            return None
        return price_bwic_line_multi(
            BwicLineMulti(
                cusip=line_cfg.get("cusip", ""),
                tranche_name=line_cfg.get("tranche", ""),
                bid_price=bid_price,
                nc_date=line_cfg.get("nc_date"),
                streams=streams,
            )
        )

    def fmt(v, decimals=1, suffix=""):
        return f"{v:.{decimals}f}{suffix}" if v is not None else "—"

    def countdown(end_time, now):
        """Return (text, kind) for a countdown badge."""
        if end_time is None:
            return ("Timer not set", "neutral")
        secs = int((end_time - now).total_seconds())
        if secs <= 0:
            over = -secs
            mm, ss = divmod(over, 60)
            return (f"⏰ EXPIRED  ({mm:02d}:{ss:02d} over)", "danger")
        mm, ss = divmod(secs, 60)
        kind = "warn" if secs <= 60 else ("warn" if secs <= 300 else "success")
        return (f"⏳ {mm:02d}:{ss:02d}  remaining (closes {end_time.strftime('%H:%M:%S')})", kind)

    return countdown, fmt, load_stream_from_bytes, price_line_at


# ============================================================================
# Demo cashflow builder (synthetic AAA — usable without uploading any file)
# ============================================================================

@app.cell
def _demo_builder(CashflowEntry, CashflowStream, Compounding, Frequency, date):

    def build_demo_stream(spread_bps=115, reinvest_months=48, amort_months=24):
        settle = date(2026, 4, 28)
        balance = 100_000_000.0
        n = reinvest_months + amort_months
        sofr = [4.00] * 6 + [3.75] * 12 + [3.50] * (n - 18)
        cashflows = []
        bal = balance
        for i in range(1, n + 1):
            yr = settle.year + (settle.month + i - 1) // 12
            mo_ = (settle.month + i - 1) % 12 + 1
            cf_date = date(yr, mo_, 25)
            sofr_pct = sofr[i - 1]
            interest = bal * (sofr_pct + spread_bps / 100.0) / 100.0 / 12.0
            principal = 0.0 if i <= reinvest_months else balance / amort_months
            prev_bal = bal
            bal = max(bal - principal, 0.0)
            cashflows.append(
                CashflowEntry(
                    cashflow_date=cf_date,
                    interest=interest,
                    principal=principal,
                    balance=bal,
                    prev_balance=prev_bal,
                    index_value=sofr_pct,
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

    return (build_demo_stream,)


# ============================================================================
# Header / status banner
# ============================================================================

@app.cell
def _header(get_state, mo):
    _s = get_state()
    _phase_map = {
        "setup":     ("SETUP",            "neutral"),
        "r1_open":   ("ROUND 1 — OPEN",   "success"),
        "r1_closed": ("ROUND 1 — CLOSED", "warn"),
        "r2_open":   ("ROUND 2 — OPEN",   "success"),
        "r2_closed": ("ROUND 2 — CLOSED", "warn"),
        "awarded":   ("AWARDED",          "success"),
    }
    _label, _kind = _phase_map.get(_s["phase"], ("?", "neutral"))
    _header_md = mo.vstack(
        [
            mo.md("# BWIC Manager"),
            mo.callout(
                mo.md(
                    f"**{_label}** · {len(_s['lines_config'])} line(s) · "
                    f"`{_s['bwic_id']}` · seller _{_s['seller']}_"
                ),
                kind=_kind,
            ),
        ]
    )
    _header_md
    return


# ============================================================================
# Round duration inputs + 1Hz tick for countdown displays
# ============================================================================

@app.cell
def _round_duration_inputs(mo):
    r1_dur_inp = mo.ui.number(value=30, start=1, stop=240, step=1, label="R1 duration (min)")
    r2_dur_inp = mo.ui.number(value=15, start=1, stop=240, step=1, label="R2 duration (min)")
    return r1_dur_inp, r2_dur_inp


@app.cell
def _ticker(mo):
    # 1-second refresh — display cells that depend on tick.value re-run each second
    tick = mo.ui.refresh(default_interval="1s", options=["1s", "5s", "off"])
    return (tick,)


# ============================================================================
# Stable form inputs (don't depend on mutable state — values persist)
# ============================================================================

@app.cell
def _meta_inputs(mo):
    bwic_id_input = mo.ui.text(value="BWIC-2026-04-28-001", label="BWIC ID", full_width=True)
    seller_input  = mo.ui.text(value="ABC Asset Mgmt",      label="Seller",  full_width=True)
    return bwic_id_input, seller_input


@app.cell
def _line_inputs(date, mo):
    cusip_inp     = mo.ui.text(value="",          label="CUSIP")
    tranche_inp   = mo.ui.text(value="",          label="Tranche")
    face_inp      = mo.ui.number(value=100_000_000, step=1_000_000, label="Current Face ($)")
    nc_date_inp   = mo.ui.date(value=date(2027, 4, 25), label="NC Date")
    reserve_inp   = mo.ui.number(value=None, start=0, stop=120, step=0.0625, label="Reserve Price (optional)")
    market_upload = mo.ui.file(filetypes=[".xlsx", ".xls"], label="Market scenario (20 CPR / 2 CDR / 30 SEV)")
    base_upload   = mo.ui.file(filetypes=[".xlsx", ".xls"], label="Base scenario (15 CPR / 5 CDR / 50 SEV)")
    return (
        base_upload,
        cusip_inp,
        face_inp,
        market_upload,
        nc_date_inp,
        reserve_inp,
        tranche_inp,
    )


# ============================================================================
# Action buttons — Setup phase
# ============================================================================

@app.cell
def _setup_actions(
    base_upload,
    build_demo_stream,
    bwic_id_input,
    cusip_inp,
    date,
    datetime,
    face_inp,
    get_state,
    load_stream_from_bytes,
    market_upload,
    mo,
    nc_date_inp,
    r1_dur_inp,
    reserve_inp,
    seller_input,
    set_state,
    timedelta,
    tranche_inp,
):
    def _add_line(_):
        s = get_state()
        idx = len(s["lines_config"]) + 1
        line_id = f"L{idx}"

        market_stream = None
        base_stream = None
        notif = list(s["notifications"])

        try:
            if market_upload.value:
                market_stream = load_stream_from_bytes(
                    market_upload.value[0].contents,
                    balance_override=float(face_inp.value or 0) or None,
                )
        except Exception as exc:
            notif.append(f"{line_id}: Market upload failed — {exc}")
        try:
            if base_upload.value:
                base_stream = load_stream_from_bytes(
                    base_upload.value[0].contents,
                    balance_override=float(face_inp.value or 0) or None,
                )
        except Exception as exc:
            notif.append(f"{line_id}: Base upload failed — {exc}")

        new_line = {
            "line_id": line_id,
            "cusip": cusip_inp.value or f"DEMO{idx:04d}",
            "tranche": tranche_inp.value or f"Tranche {idx}",
            "face": float(face_inp.value or 0),
            "nc_date": nc_date_inp.value,
            "reserve": float(reserve_inp.value) if reserve_inp.value else None,
            "market_stream": market_stream,
            "base_stream": base_stream,
        }
        set_state(
            {
                **s,
                "bwic_id": bwic_id_input.value or s["bwic_id"],
                "seller": seller_input.value or s["seller"],
                "lines_config": s["lines_config"] + [new_line],
                "notifications": notif,
            }
        )

    def _load_demo(_):
        s = get_state()
        market = build_demo_stream(spread_bps=115)
        base = build_demo_stream(spread_bps=115)
        demo_line = {
            "line_id": f"L{len(s['lines_config']) + 1}",
            "cusip": "12345ABC0",
            "tranche": "AAA",
            "face": 100_000_000.0,
            "nc_date": date(2027, 4, 25),
            "reserve": None,
            "market_stream": market,
            "base_stream": base,
        }
        set_state({**s, "lines_config": s["lines_config"] + [demo_line]})

    def _clear_lines(_):
        s = get_state()
        set_state({**s, "lines_config": [], "notifications": []})

    def _open_r1(_):
        # Lazy import inside the callback so the cell doesn't depend on the workflow module
        from graam.bwic_workflow import Bwic, BwicLine as WfBwicLine
        s = get_state()
        if not s["lines_config"]:
            set_state({**s, "notifications": s["notifications"] + ["Cannot open R1 — add at least one line first."]})
            return
        bwic = Bwic(
            bwic_id=bwic_id_input.value or s["bwic_id"],
            seller=seller_input.value or s["seller"],
            bwic_date=date.today(),
        )
        for cfg in s["lines_config"]:
            streams = {}
            if cfg.get("market_stream") is not None:
                streams["Market"] = cfg["market_stream"]
            if cfg.get("base_stream") is not None:
                streams["Base"] = cfg["base_stream"]
            line = WfBwicLine(
                line_id=cfg["line_id"],
                cusip=cfg.get("cusip", ""),
                tranche_name=cfg.get("tranche", ""),
                current_face=float(cfg.get("face") or 0),
                nc_date=cfg.get("nc_date"),
                reserve_price=cfg.get("reserve"),
                streams=streams,
                pricing_scenario="Market" if "Market" in streams else (next(iter(streams)) if streams else "Market"),
            )
            bwic.add_line(line)
        bwic.open_round1()
        _r1_end = datetime.now() + timedelta(minutes=int(r1_dur_inp.value or 30))
        set_state({**s, "bwic": bwic, "phase": "r1_open", "r1_end_time": _r1_end})

    add_line_btn   = mo.ui.button(label="+ Add Line",                      kind="success", on_click=_add_line)
    demo_btn       = mo.ui.button(label="Load Demo (AAA $100M S+115)",     kind="neutral", on_click=_load_demo)
    clear_btn      = mo.ui.button(label="Clear Lines",                     kind="danger",  on_click=_clear_lines)
    open_r1_btn    = mo.ui.button(label="Open Round 1 →",                  kind="success", on_click=_open_r1)
    return add_line_btn, clear_btn, demo_btn, open_r1_btn


# ============================================================================
# Pricing reference table — multi-scenario DM at par for all loaded lines
# ============================================================================

@app.cell
def _pricing_table(get_state, mo, pd, price_line_at):
    _s = get_state()
    _rows = []
    for _cfg in _s["lines_config"]:
        _result = price_line_at(_cfg, 100.0)  # at par
        _row = {
            "Line": _cfg["line_id"],
            "CUSIP": _cfg.get("cusip", ""),
            "Tranche": _cfg.get("tranche", ""),
            "Face ($mm)": (_cfg.get("face") or 0) / 1_000_000,
            "NC Date": _cfg.get("nc_date"),
            "Reserve": _cfg.get("reserve"),
            "Streams": ", ".join(
                k for k in ("Market", "Base") if _cfg.get(f"{k.lower()}_stream") is not None
            ) or "—",
        }
        if _result is not None:
            for _label in ("Market", "Base"):
                _r = _result.results.get(_label)
                if _r is None:
                    continue
                _row[f"{_label} DM-Mat"] = round(_r.dm_to_maturity, 1) if _r.dm_to_maturity is not None else None
                _row[f"{_label} DM-Call"] = round(_r.dm_to_call, 1) if _r.dm_to_call is not None else None
                _row[f"{_label} DM-Worst"] = round(_r.dm_to_worst, 1) if _r.dm_to_worst is not None else None
                _row[f"{_label} WAL"] = round(_r.wal_to_maturity, 2) if _r.wal_to_maturity is not None else None
        _rows.append(_row)

    _pricing_df = pd.DataFrame(_rows) if _rows else pd.DataFrame()
    pricing_table_md = (
        mo.vstack(
            [
                mo.md("### Pricing reference (DM at par, both scenarios)"),
                mo.ui.table(_pricing_df, selection=None, page_size=20),
            ]
        )
        if not _pricing_df.empty
        else mo.md("_No lines yet — add one above or click_ **Load Demo**.")
    )
    return (pricing_table_md,)


# ============================================================================
# Setup section layout
# ============================================================================

@app.cell
def _setup_section(
    add_line_btn,
    base_upload,
    bwic_id_input,
    clear_btn,
    cusip_inp,
    demo_btn,
    face_inp,
    get_state,
    market_upload,
    mo,
    nc_date_inp,
    open_r1_btn,
    pricing_table_md,
    r1_dur_inp,
    r2_dur_inp,
    reserve_inp,
    seller_input,
    tranche_inp,
):
    _s = get_state()
    mo.stop(_s["phase"] != "setup", mo.md("_Setup is closed — round in progress._"))

    _setup_md = mo.vstack(
        [
            mo.md("## 1 · BWIC details"),
            mo.hstack([bwic_id_input, seller_input], gap=2, justify="start"),
            mo.md("## 2 · Add lines"),
            mo.hstack([cusip_inp, tranche_inp, face_inp, nc_date_inp, reserve_inp], gap=1, justify="start"),
            mo.hstack([market_upload, base_upload], gap=2, justify="start"),
            mo.hstack([add_line_btn, demo_btn, clear_btn], gap=1, justify="start"),
            mo.md("## 3 · Pricing reference"),
            pricing_table_md,
            mo.md("---"),
            mo.md("## 4 · Round timing"),
            mo.hstack([r1_dur_inp, r2_dur_inp], gap=2, justify="start"),
            mo.hstack([open_r1_btn], justify="end"),
        ]
    )
    _setup_md
    return


# ============================================================================
# Notifications panel (errors, validation messages)
# ============================================================================

@app.cell
def _notifications(get_state, mo):
    _s = get_state()
    mo.stop(not _s["notifications"])
    _notifs_md = mo.callout(
        mo.vstack([mo.md(f"- {n}") for n in _s["notifications"][-8:]]),
        kind="warn",
    )
    _notifs_md
    return


# ============================================================================
# Stage 2 — Stable bid-form inputs (persist across state changes)
# ============================================================================

@app.cell
def _bid_stable_inputs(mo):
    dealer_inp = mo.ui.text(value="", label="Dealer", placeholder="GS, JPM, MS…")
    price_inp  = mo.ui.number(
        value=100.0, step=0.0625, start=50.0, stop=150.0,
        label="Bid Price (% par)",
    )
    return dealer_inp, price_inp


# ============================================================================
# Round 1 — bid entry, live DM preview, blotter, close button
# ============================================================================

@app.cell
def _r1_section(
    BidError, countdown, datetime, dealer_inp, fmt, get_state, mo, pd,
    price_inp, price_line_at, set_state, tick, timedelta,
):
    _ = tick.value  # subscribe to 1Hz tick so the countdown re-renders
    _s = get_state()
    mo.stop(_s["phase"] != "r1_open")
    _cd_text, _cd_kind = countdown(_s.get("r1_end_time"), datetime.now())

    def _extend_r1(_):
        _st = get_state()
        _end = _st.get("r1_end_time") or datetime.now()
        set_state({**_st, "r1_end_time": _end + timedelta(minutes=5)})

    _extend_r1_btn = mo.ui.button(label="+5 min", kind="neutral", on_click=_extend_r1)

    _bwic   = _s["bwic"]
    _lines  = _s["lines_config"]
    _opts   = {c["line_id"]: f"{c['line_id']} · {c.get('tranche','')} {c.get('cusip','')}"
               for c in _lines}

    r1_line_sel = mo.ui.dropdown(options=_opts, label="Line")

    # ── Live DM preview ────────────────────────────────────────────────────
    _sel   = r1_line_sel.value
    _price = price_inp.value
    _dm_txt = "Select a line to see live DM"
    if _sel:
        _cfg = next((c for c in _lines if c["line_id"] == _sel), None)
        if _cfg:
            try:
                _pr = price_line_at(_cfg, _price)
                if _pr:
                    _parts = []
                    for _lbl in ("Market", "Base"):
                        _r = _pr.results.get(_lbl)
                        if _r and _r.dm_to_worst is not None:
                            _parts.append(
                                f"**{_lbl}** DM-W: {fmt(_r.dm_to_worst)} | "
                                f"DM-M: {fmt(_r.dm_to_maturity)} | "
                                f"WAL: {fmt(_r.wal_to_maturity, 2)}y"
                            )
                    _dm_txt = " · ".join(_parts) if _parts else "No streams loaded for this line"
            except Exception as _exc:
                _dm_txt = f"Pricing error: {_exc}"

    # ── Callbacks ──────────────────────────────────────────────────────────
    def _submit(_):
        _st = get_state()
        _b  = _st.get("bwic")
        if _b is None or not r1_line_sel.value or not dealer_inp.value.strip():
            return
        try:
            _b.submit_bid(r1_line_sel.value, dealer_inp.value.strip(), price_inp.value)
            set_state({**_st, "bwic": _b, "notifications": []})
        except BidError as _e:
            set_state({**_st, "notifications": _st["notifications"] + [str(_e)]})

    def _close_r1(_):
        _st = get_state()
        _b  = _st.get("bwic")
        if _b is None:
            return
        _b.close_round1(top_n=3)
        set_state({**_st, "bwic": _b, "phase": "r1_closed"})

    _submit_btn   = mo.ui.button(label="Submit Bid ▶",  kind="success", on_click=_submit)
    _close_r1_btn = mo.ui.button(label="Close Round 1", kind="warn",    on_click=_close_r1)

    # ── Blotter (live) ─────────────────────────────────────────────────────
    _bid_rows = []
    if _bwic:
        for _line in _bwic.lines:
            for _bid in _line.round1_bids():
                _bid_rows.append({
                    "Line":   _bid.line_id,
                    "Dealer": _bid.dealer,
                    "Price":  round(_bid.price, 4),
                    "Time":   _bid.timestamp.strftime("%H:%M:%S"),
                })
    _blotter_df = pd.DataFrame(_bid_rows) if _bid_rows else pd.DataFrame(columns=["Line","Dealer","Price","Time"])

    mo.vstack([
        mo.md("## Round 1 — Bid Intake"),
        mo.hstack([
            mo.callout(mo.md(_cd_text), kind=_cd_kind),
            _extend_r1_btn, tick,
        ], gap=1, justify="start"),
        mo.hstack([r1_line_sel, dealer_inp, price_inp, _submit_btn], gap=1, justify="start"),
        mo.callout(mo.md(_dm_txt), kind="neutral"),
        mo.md("### Live bids"),
        mo.ui.table(_blotter_df, selection=None, page_size=30),
        mo.md("---"),
        mo.hstack([_close_r1_btn], justify="end"),
    ])
    return


# ============================================================================
# Round 1 results — advancing dealers + Open R2
# ============================================================================

@app.cell
def _r1_results(datetime, get_state, mo, pd, r2_dur_inp, set_state, timedelta):
    _s = get_state()
    mo.stop(_s["phase"] != "r1_closed")

    _bwic = _s["bwic"]

    def _open_r2(_):
        _st = get_state()
        _b  = _st.get("bwic")
        if _b is None:
            return
        try:
            _b.open_round2()
            _r2_end = datetime.now() + timedelta(minutes=int(r2_dur_inp.value or 15))
            set_state({**_st, "bwic": _b, "phase": "r2_open", "r2_end_time": _r2_end})
        except Exception as _exc:
            set_state({**_st, "notifications": _st["notifications"] + [str(_exc)]})

    _open_r2_btn = mo.ui.button(label="Open Round 2 →", kind="success", on_click=_open_r2)

    _adv_rows = []
    if _bwic:
        for _line in _bwic.lines:
            _best = _line._best_bid_per_dealer(round_filter=1)
            _ranked = sorted(_best.values(), key=lambda b: (-b.price, b.timestamp))
            for _i, _bid in enumerate(_ranked):
                _adv_rows.append({
                    "Line":     _line.line_id,
                    "Tranche":  _line.tranche_name,
                    "Rank":     _i + 1,
                    "Dealer":   _bid.dealer,
                    "R1 Price": round(_bid.price, 4),
                    "Advance?": "✓" if _bid.dealer in _line.advancing_dealers else "",
                })

    _adv_df = pd.DataFrame(_adv_rows) if _adv_rows else pd.DataFrame()

    mo.vstack([
        mo.md("## Round 1 — Results"),
        mo.ui.table(_adv_df, selection=None, page_size=30),
        mo.md("---"),
        mo.hstack([_open_r2_btn], justify="end"),
    ])
    return


# ============================================================================
# Round 2 — bid entry, live DM preview, blotter, close button
# ============================================================================

@app.cell
def _r2_section(
    BidError, countdown, datetime, dealer_inp, fmt, get_state, mo, pd,
    price_inp, price_line_at, set_state, tick, timedelta,
):
    _ = tick.value
    _s = get_state()
    mo.stop(_s["phase"] != "r2_open")
    _cd_text, _cd_kind = countdown(_s.get("r2_end_time"), datetime.now())

    def _extend_r2(_):
        _st = get_state()
        _end = _st.get("r2_end_time") or datetime.now()
        set_state({**_st, "r2_end_time": _end + timedelta(minutes=5)})

    _extend_r2_btn = mo.ui.button(label="+5 min", kind="neutral", on_click=_extend_r2)

    _bwic  = _s["bwic"]
    _lines = _s["lines_config"]

    # Only advancing dealers matter — show per-line advancing sets
    _adv_map: dict[str, list[str]] = {}
    if _bwic:
        for _line in _bwic.lines:
            _adv_map[_line.line_id] = _line.advancing_dealers

    _opts = {c["line_id"]: f"{c['line_id']} · {c.get('tranche','')} (adv: {', '.join(_adv_map.get(c['line_id'], []))})"
             for c in _lines}

    r2_line_sel = mo.ui.dropdown(options=_opts, label="Line")

    # ── Live DM preview ────────────────────────────────────────────────────
    _sel   = r2_line_sel.value
    _price = price_inp.value
    _dm_txt = "Select a line to see live DM"
    if _sel:
        _cfg = next((c for c in _lines if c["line_id"] == _sel), None)
        if _cfg:
            try:
                _pr = price_line_at(_cfg, _price)
                if _pr:
                    _parts = []
                    for _lbl in ("Market", "Base"):
                        _r = _pr.results.get(_lbl)
                        if _r and _r.dm_to_worst is not None:
                            _parts.append(
                                f"**{_lbl}** DM-W: {fmt(_r.dm_to_worst)} | "
                                f"DM-M: {fmt(_r.dm_to_maturity)} | "
                                f"WAL: {fmt(_r.wal_to_maturity, 2)}y"
                            )
                    _dm_txt = " · ".join(_parts) if _parts else "No streams for this line"
            except Exception as _exc:
                _dm_txt = f"Pricing error: {_exc}"

    # ── Callbacks ──────────────────────────────────────────────────────────
    def _submit_r2(_):
        _st = get_state()
        _b  = _st.get("bwic")
        if _b is None or not r2_line_sel.value or not dealer_inp.value.strip():
            return
        try:
            _b.submit_bid(r2_line_sel.value, dealer_inp.value.strip(), price_inp.value)
            set_state({**_st, "bwic": _b, "notifications": []})
        except BidError as _e:
            set_state({**_st, "notifications": _st["notifications"] + [str(_e)]})

    def _close_r2(_):
        _st = get_state()
        _b  = _st.get("bwic")
        if _b is None:
            return
        _b.close_round2()
        set_state({**_st, "bwic": _b, "phase": "awarded"})

    _submit_btn   = mo.ui.button(label="Submit Bid ▶",  kind="success", on_click=_submit_r2)
    _close_r2_btn = mo.ui.button(label="Close Round 2", kind="warn",    on_click=_close_r2)

    # ── R2 blotter ─────────────────────────────────────────────────────────
    _bid_rows = []
    if _bwic:
        for _line in _bwic.lines:
            for _bid in _line.round2_bids():
                _bid_rows.append({
                    "Line":   _bid.line_id,
                    "Dealer": _bid.dealer,
                    "Price":  round(_bid.price, 4),
                    "Time":   _bid.timestamp.strftime("%H:%M:%S"),
                })
    _blotter_df = pd.DataFrame(_bid_rows) if _bid_rows else pd.DataFrame(columns=["Line","Dealer","Price","Time"])

    # ── Advancing dealers reminder ─────────────────────────────────────────
    _adv_lines = [f"**{lid}**: {', '.join(adv)}" for lid, adv in _adv_map.items() if adv]

    mo.vstack([
        mo.md("## Round 2 — Bid Intake"),
        mo.hstack([
            mo.callout(mo.md(_cd_text), kind=_cd_kind),
            _extend_r2_btn, tick,
        ], gap=1, justify="start"),
        mo.callout(mo.md("Advancing dealers: " + " · ".join(_adv_lines)), kind="neutral"),
        mo.hstack([r2_line_sel, dealer_inp, price_inp, _submit_btn], gap=1, justify="start"),
        mo.callout(mo.md(_dm_txt), kind="neutral"),
        mo.md("### R2 bids submitted"),
        mo.ui.table(_blotter_df, selection=None, page_size=30),
        mo.md("---"),
        mo.hstack([_close_r2_btn], justify="end"),
    ])
    return


# ============================================================================
# Stage 3 — Award screen + color sheet + bid log + CSV exports
# ============================================================================

@app.cell
def _award_section(get_state, mo, pd, set_state):
    _s = get_state()
    mo.stop(_s["phase"] != "awarded")

    _bwic = _s["bwic"]

    # ── Award cards (one per line) ─────────────────────────────────────────
    _cards = []
    for _line in _bwic.lines:
        _aw = _line.award
        if _aw is None:
            continue
        if _aw.is_dnt:
            _cards.append(mo.callout(
                mo.md(f"### {_line.line_id} · {_line.tranche_name}\n\n**DNT** — {_aw.dnt_reason}"),
                kind="danger",
            ))
            continue
        _award_md = (
            f"### {_line.line_id} · {_line.tranche_name}  \n"
            f"**🏆 AWARD** · {_aw.award_dealer} @ **{_aw.award_price:.4f}**"
            + (f"  (DM {_aw.spread_at_award:.1f} bps)" if _aw.spread_at_award is not None else "")
            + "  \n"
            f"**📊 COVER** · "
            + (f"{_aw.cover_dealer} @ {_aw.cover_price:.4f}" if _aw.cover_dealer else "_no cover_")
            + (f"  (DM {_aw.spread_at_cover:.1f} bps)" if _aw.spread_at_cover is not None else "")
        )
        _cards.append(mo.callout(mo.md(_award_md), kind="success"))

    # ── Color sheet ────────────────────────────────────────────────────────
    _color_df = _bwic.color_sheet()

    # ── Bid log (audit trail) ──────────────────────────────────────────────
    _bid_log_df = _bwic.bid_log()
    if not _bid_log_df.empty:
        _bid_log_df = _bid_log_df.copy()
        _bid_log_df["timestamp"] = _bid_log_df["timestamp"].apply(
            lambda t: t.strftime("%Y-%m-%d %H:%M:%S") if hasattr(t, "strftime") else str(t)
        )

    # ── Downloads ──────────────────────────────────────────────────────────
    _bwic_id_safe = (_s.get("bwic_id") or "bwic").replace(" ", "_")
    _color_csv = _color_df.to_csv(index=False).encode("utf-8") if not _color_df.empty else b""
    _bidlog_csv = _bid_log_df.to_csv(index=False).encode("utf-8") if not _bid_log_df.empty else b""

    _color_dl = mo.download(
        data=_color_csv,
        filename=f"{_bwic_id_safe}_color_sheet.csv",
        label="↓ Color sheet (CSV)",
    )
    _bidlog_dl = mo.download(
        data=_bidlog_csv,
        filename=f"{_bwic_id_safe}_bid_log.csv",
        label="↓ Bid log (CSV)",
    )

    # ── Reset BWIC ─────────────────────────────────────────────────────────
    def _reset(_):
        set_state({
            "bwic_id": "BWIC-2026-04-28-001",
            "seller": "ABC Asset Mgmt",
            "lines_config": [],
            "bwic": None,
            "phase": "setup",
            "notifications": [],
        })

    _reset_btn = mo.ui.button(label="↺ New BWIC", kind="danger", on_click=_reset)

    mo.vstack([
        mo.md("## 🏆 BWIC Awarded"),
        mo.vstack(_cards) if _cards else mo.md("_No awards._"),
        mo.md("### Color sheet"),
        mo.ui.table(_color_df, selection=None, page_size=30) if not _color_df.empty
            else mo.md("_(no color)_"),
        mo.md("### Bid log (audit trail)"),
        mo.ui.table(_bid_log_df, selection=None, page_size=50) if not _bid_log_df.empty
            else mo.md("_(no bids)_"),
        mo.md("### Export"),
        mo.hstack([_color_dl, _bidlog_dl], gap=1, justify="start"),
        mo.md("---"),
        mo.hstack([_reset_btn], justify="end"),
    ])
    return


# ============================================================================
# Bloomberg IB messages — copy/paste these into IB chat during the BWIC
# ============================================================================

@app.cell
def _ib_messages(date, datetime, get_state, mo, pd):
    _s = get_state()
    _bwic = _s.get("bwic")
    _phase = _s["phase"]
    _bwic_id = _s.get("bwic_id") or "BWIC"
    _seller = _s.get("seller") or "Seller"

    def _fmt_face(face):
        if face >= 1_000_000:
            return f"{face/1_000_000:.0f}MM"
        return f"{face:,.0f}"

    def _fmt_dt(dt):
        return dt.strftime("%H:%M EST") if dt else "TBD"

    # ── 1. ANNOUNCEMENT (sent before R1 opens — covers full lineup) ────────
    _lines_cfg = _s["lines_config"]
    _today_str = date.today().strftime("%a %m/%d/%y")
    _r1_when = _fmt_dt(_s.get("r1_end_time"))
    _r2_when = _fmt_dt(_s.get("r2_end_time"))

    _announce_lines = [
        f"*** BWIC ANNOUNCEMENT — {_today_str} ***",
        f"Seller:  {_seller}",
        f"BWIC ID: {_bwic_id}",
        "",
        f"R1 due:  {_r1_when}  (top 3 advance, ties move)",
        f"R2 due:  {_r2_when}  (last & best)",
        "Color:   post-trade, T+0",
        "",
        "LINEUP:",
    ]
    for _i, _c in enumerate(_lines_cfg, 1):
        _nc = _c.get("nc_date").strftime("%m/%y") if _c.get("nc_date") else "n/a"
        _res = f"{_c['reserve']:.4f}" if _c.get("reserve") else "NONE"
        _announce_lines.append(
            f"  {_i}) {_c.get('cusip','')} {_c.get('tranche','')} "
            f"${_fmt_face(_c.get('face') or 0)} | NC {_nc} | Reserve: {_res}"
        )
    _announce_lines += [
        "",
        "Format: direct via IB, all-in price (% par).",
        "Bids subject to seller discretion. Cover/color out post-trade.",
    ]
    announce_msg = "\n".join(_announce_lines)

    # ── 2. R1 OPEN ─────────────────────────────────────────────────────────
    r1_open_msg = "\n".join([
        f"*** R1 OPEN — {_bwic_id} ***",
        f"Bids due {_r1_when}.",
        "Direct via IB, all-in price.",
        "Top 3 advance to R2 (ties move).",
    ])

    # ── 3. R1 CLOSED / advancers ───────────────────────────────────────────
    _r1_close_lines = [f"*** R1 CLOSED — {_bwic_id} ***", ""]
    if _bwic and _phase in ("r1_closed", "r2_open", "awarded"):
        _r1_close_lines.append("Advancing to R2:")
        for _line in _bwic.lines:
            _r1_close_lines.append(
                f"  {_line.line_id} {_line.tranche_name}: "
                + (", ".join(_line.advancing_dealers) if _line.advancing_dealers else "—")
            )
        _r1_close_lines += ["", f"R2 opens shortly — last & best by {_r2_when}."]
    else:
        _r1_close_lines.append("(R1 not yet closed)")
    r1_closed_msg = "\n".join(_r1_close_lines)

    # ── 4. R2 OPEN ─────────────────────────────────────────────────────────
    r2_open_msg = "\n".join([
        f"*** R2 OPEN — Last & Best ***",
        f"You are advancing.  Last & best due {_r2_when}.",
        "You may stay flat — cannot lower.",
        "Direct via IB, all-in price.",
    ])

    # ── 5. COLOR (post-award) ──────────────────────────────────────────────
    _color_lines = [f"*** COLOR — {_bwic_id} ***", ""]
    if _bwic and _phase == "awarded":
        for _line in _bwic.lines:
            _aw = _line.award
            if _aw is None:
                continue
            _hdr = f"{_line.line_id} {_line.tranche_name} ${_fmt_face(_line.current_face)}"
            if _aw.is_dnt:
                _color_lines.append(f"{_hdr}: DNT — {_aw.dnt_reason}")
                continue
            _best = _line._best_bid_per_dealer()
            _prices = sorted([b.price for b in _best.values()], reverse=True)
            _color_lines.append(f"{_hdr}:")
            _color_lines.append(
                f"  TRADED @ {_aw.award_price:.4f}"
                + (f"  (DM {_aw.spread_at_award:.1f})" if _aw.spread_at_award is not None else "")
            )
            if _aw.cover_dealer:
                _color_lines.append(
                    f"  Cover  @ {_aw.cover_price:.4f}"
                    + (f"  (DM {_aw.spread_at_cover:.1f})" if _aw.spread_at_cover is not None else "")
                )
            _color_lines.append(
                f"  Range  {_prices[-1]:.4f} – {_prices[0]:.4f} | {len(_best)} bids"
            )
            _color_lines.append("")
        _color_lines.append("Thanks for participating.")
    else:
        _color_lines.append("(Color available once awarded.)")
    color_msg = "\n".join(_color_lines)

    # ── Phase-aware "active" message label ─────────────────────────────────
    _active_label = {
        "setup":     "Pre-launch announcement",
        "r1_open":   "Round 1 open",
        "r1_closed": "Round 1 closed — advancers",
        "r2_open":   "Round 2 — last & best",
        "awarded":   "Color (post-trade)",
    }.get(_phase, "Announcement")

    _active_msg = {
        "setup":     announce_msg,
        "r1_open":   r1_open_msg,
        "r1_closed": r1_closed_msg,
        "r2_open":   r2_open_msg,
        "awarded":   color_msg,
    }.get(_phase, announce_msg)

    _all_messages_md = mo.accordion({
        "📣 1 · BWIC announcement (pre-launch)": mo.md(f"```text\n{announce_msg}\n```"),
        "🔔 2 · Round 1 open":                    mo.md(f"```text\n{r1_open_msg}\n```"),
        "📋 3 · R1 closed — advancers":           mo.md(f"```text\n{r1_closed_msg}\n```"),
        "🎯 4 · Round 2 last & best":             mo.md(f"```text\n{r2_open_msg}\n```"),
        "📊 5 · Color (post-award)":              mo.md(f"```text\n{color_msg}\n```"),
    })

    mo.vstack([
        mo.md("## 📨 Bloomberg IB messages"),
        mo.callout(
            mo.vstack([
                mo.md(f"**Active message** — _{_active_label}_"),
                mo.md(f"```text\n{_active_msg}\n```"),
            ]),
            kind="success",
        ),
        mo.md("### All messages"),
        _all_messages_md,
    ])
    return


if __name__ == "__main__":
    app.run()
