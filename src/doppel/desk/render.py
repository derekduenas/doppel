"""Render desk.json into the static page. Every section is HTML at build time (the page needs no
JavaScript to be read); a small inline script only runs the break-even calculator and ticks the
countdowns. The page template lives at ``<repo>/desk/index.html.tmpl``."""

from __future__ import annotations

import html
import math
from pathlib import Path
from typing import Any

import orjson

TEMPLATE = Path("desk") / "index.html.tmpl"
NOT_YET = '<p class="notyet">not yet</p>'


def esc(x: Any) -> str:
    return html.escape("" if x is None else str(x), quote=True)


def num(x: Any, nd: int = 2, na: str = "–", sign: bool = False) -> str:
    if x is None or isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
        return na
    if isinstance(x, int) or (nd == 0):
        s = f"{x:+,.0f}" if sign else f"{x:,.0f}"
    else:
        s = f"{x:+,.{nd}f}" if sign else f"{x:,.{nd}f}"
    return s


def dur(s: Any) -> str:
    if s is None or not isinstance(s, (int, float)) or isinstance(s, bool) or not math.isfinite(s):
        return "–"
    s = float(s)
    neg, s = s < 0, abs(s)
    if s < 120:
        out = f"{s:.0f} s" if s >= 10 else f"{s:.1f} s"
    elif s < 7200:
        out = f"{s / 60:.0f} min"
    elif s < 172800:
        out = f"{int(s // 3600)} h {int(s % 3600 // 60):02d} m"
    else:
        out = f"{int(s // 86400)} d {int(s % 86400 // 3600)} h"
    return "-" + out if neg else out


def short(ts: Any) -> str:
    """'2026-11-27T00:00:00Z' -> '11-27 00:00' (UTC); compact for table cells."""
    t = "" if ts is None else str(ts)
    return esc(t[5:16].replace("T", " ")) if len(t) >= 16 else esc(t)


def src(s: dict | None) -> str:
    if not s:
        return ""
    parts = [f'<span class="f">{esc(s.get("file"))}</span>']
    if s.get("tw"):
        parts.append(f"built {esc(s['tw'])}")
    if s.get("mtime"):
        parts.append(f"file {esc(s['mtime'])}")
    return '<div class="src">' + " · ".join(parts) + "</div>"


def ci(v: dict | None, key: str = "mean") -> str:
    if not isinstance(v, dict):
        return "–"
    c = v.get("ci95") or [None, None]
    return f'{num(v.get(key), 2, sign=True)} <span class="ci">[{num(c[0], 2)}, {num(c[1], 2)}]</span>'


def table(head: list[str], rows: list[list[str]], cls: str = "", head2: str = "") -> str:
    th = "".join(f"<th>{h}</th>" for h in head)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return (f'<div class="tw"><table class="{cls}"><thead>{head2}<tr>{th}</tr></thead>'
            f"<tbody>{body}</tbody></table></div>")


def pill(text: str, kind: str) -> str:
    return f'<span class="pill {esc(kind)}">{esc(text)}</span>'


DECISION_KIND = {"PASS": "good", "KILL": "bad", "CONTINUE": "warn", "DIAGNOSTIC": "muted"}
FLAG_KIND = {"ok": "good", "errors": "warn", "stale": "bad", "never": "muted"}
PHASE_KIND = {"scheduled": "muted", "evaluating": "warn", "awaiting result": "warn", "tested": "good"}


# ---------------------------------------------------------------- sections
def _side_card(title: str, s: dict | None) -> str:
    if not s:
        return f'<div class="card"><h3>{esc(title)}</h3>{NOT_YET}</div>'
    d = s.get("disk") or {}
    stale = pill("status stale", "bad") if s.get("stale") else pill("writing", "good")
    rows = [("status age", dur(s.get("status_age_s"))), ("uptime", dur(s.get("uptime_s"))),
            ("pid", esc(s.get("pid"))), ("streams", num(s.get("streams"), 0)),
            ("disk free", f"{num(d.get('free_gb'), 1)} GB of {num(d.get('total_gb'), 0)} GB"
             f" ({num(d.get('used_pct'), 1)}% used)" if d else "not yet")]
    kv = "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in rows)
    return f'<div class="card"><h3>{esc(title)} {stale}</h3><dl>{kv}</dl>{src(s.get("src"))}</div>'


def _cells(r: dict | None, rust: bool) -> list[str]:
    if not r:
        return ['<span class="muted">–</span>'] * 6
    drops = num(r.get("drops"), 0) if rust else '<span class="muted" title="the Python recorder has no drop counter">n/a</span>'
    if rust and r.get("write_errors"):
        drops += f' <span class="bad" title="write errors">we {num(r["write_errors"], 0)}</span>'
    err = num(r.get("errors"), 0)
    if r.get("last_error"):
        err = f'<span title="{esc(r["last_error"])}">{err} ⓘ</span>'
    return [num(r.get("msgs"), 0), dur(r.get("age_at_build_s")), num(r.get("reconnects"), 0),
            err, drops, pill(r.get("flag") or "?", FLAG_KIND.get(r.get("flag") or "", "muted"))]


def section_recorder(rec: dict) -> str:
    cards = _side_card("Python recorder (tape)", rec.get("python")) + _side_card("Rust recorder (tape-rust)", rec.get("rust"))
    streams = rec.get("streams") or []
    if not streams:
        body = NOT_YET
    else:
        h2 = ('<tr><th></th><th colspan="6" class="grp">Python</th>'
              '<th colspan="6" class="grp">Rust</th></tr>')
        sub = ["msgs", "last msg", "reconn", "errors", "drops", "state"]
        rows = [[f'<span class="mono">{esc(s["name"])}</span>'] + _cells(s.get("python"), False) + _cells(s.get("rust"), True)
                for s in streams]
        body = table(["stream"] + sub + sub, rows, "num", h2)
    th = rec.get("thresholds") or {}
    note = (f'<p class="note">Last-message age is as of the build: the status file\'s own age plus the age it '
            f'reports. Flag "stale" = no message for {num(th.get("stale_msg_s"), 0)} s or a status file older than '
            f'{num(th.get("stale_status_s"), 0)} s.</p>')
    return f'<div class="cards">{cards}</div>{body}{note}'


def _countdown(g: dict) -> str:
    ph = g.get("phase")
    ew = g.get("eval_window") or {}
    if ph == "scheduled":
        return f'starts in <span class="cd" data-t="{esc(ew.get("start_s"))}">{dur(g.get("seconds_to_start"))}</span>'
    if ph == "awaiting result":
        return '<span class="muted">window closed</span>'
    if ph == "evaluating":
        return f'<span class="cd" data-t="{esc(ew.get("end_s"))}">{dur(g.get("seconds_remaining"))}</span> left'
    return "–"


def _twin_cells(t: dict | None) -> list[str]:
    """verdict (+ replay range and source underneath), net c/window, net c/contract, windows, commercial."""
    if not t:
        return ['<span class="notyet">not yet</span>', "–", "–", "–", "–"]
    c = t.get("commercial") or {}
    lb = c.get("net_usd_per_day_lower_bound_after_ops")
    hurdle = "" if lb is None else (pill("clears", "good") if c.get("clears_hurdle") else pill("below", "bad"))
    fit = t.get("window_fit")
    fitk = {"prereg": "good", "partial": "warn"}.get(fit or "", "bad")
    dec = t.get("decision") or "?"
    rng = t.get("range_iso") or [None, None]
    bds = c.get("best_day_share_of_total")
    best = f"{100 * bds:.0f}%" if isinstance(bds, (int, float)) else "–"
    comm = (f'{num(lb, 2, sign=True)} {hurdle}<div class="sub">{num(c.get("days"), 0)} d · best day {best} of total'
            f' · break-even {num(c.get("breakeven_contracts_per_day"), 0)} ct/day</div>') if c else "–"
    return [pill(dec, DECISION_KIND.get(dec, "muted")) + " " + pill(fit or "?", fitk)
            + f'<div class="sub">replay {short(rng[0])} → {short(rng[1])}</div>' + src(t.get("src")),
            ci(t.get("net_c_per_window")), ci(t.get("net_c_per_contract"), "value"),
            f'{num(t.get("windows_evaluated"), 0)} / {num(t.get("windows_traded"), 0)}', comm]


def section_strategies(st: dict) -> str:
    reg = st.get("registry") or {}
    if not reg.get("present"):
        return NOT_YET
    v = reg.get("verify") or {}
    chain = (pill("chain intact", "good") if v.get("ok") else
             pill(f"chain: {v.get('n_problems', '?')} problem(s)", "bad"))
    out = [f'<p class="meta">{chain} {num(reg.get("lines"), 0)} lines · head <span class="mono">'
           f'{esc((reg.get("head") or "")[:16])}</span>{src(reg.get("src"))}</p>']
    if v.get("problems"):
        out.append('<ul class="probs">' + "".join(f"<li>{esc(p)}</li>" for p in v["problems"]) + "</ul>")
    for fam in st.get("families") or []:
        rows = []
        for g in fam.get("genomes") or []:
            ew = g.get("eval_window") or {}
            reg_cell = esc(g.get("registry_status"))
            if g.get("p_holm") is not None:
                reg_cell += f' <span class="ci">p {num(g.get("p_value"), 3)} · Holm {num(g.get("p_holm"), 3)}</span>'
            rows.append([f'<span class="mono">{esc(g.get("name"))}</span><div class="sub mono">{esc((g.get("genome_id") or "")[:12])}</div>',
                         f'<span class="nw">{short(ew.get("start"))} →</span> <span class="nw">{short(ew.get("end"))}</span>',
                         pill(g.get("phase") or "?", PHASE_KIND.get(g.get("phase") or "", "muted")),
                         _countdown(g), reg_cell] + _twin_cells(g.get("twin")))
        head = ["genome", "eval window (UTC)", "phase", "time", "registry", "twin verdict · replay range · source",
                "net c/window [95% CI]", "net c/contract [CI]", "windows eval / traded",
                "commercial LB $/day"]
        out.append(f'<h3 class="fam">{esc(fam.get("family"))} <span class="sub">{num(fam.get("m"), 0)} variants · '
                   f'{num(fam.get("tested"), 0)} tested · {num(fam.get("pending"), 0)} pending</span></h3>'
                   f'<p class="hyp">{esc(fam.get("hypothesis"))}</p>' + table(head, rows, "num reg"))
    out.append('<p class="note">Twin verdict = latest non-diagnostic, zero-delay replay of that genome '
               '(<span class="mono">lab:twin/**</span>). "prereg" = the replay range is exactly the registered '
               'window; "partial" = inside it (interim); "off-window" = exploratory, not evidence for the '
               'registered test. Commercial LB = lower bound $/day after operating cost (A6), ex-incentives.</p>')
    out.append(_h3(st.get("h3") or {}))
    if st.get("unregistered_twin_runs"):
        rows = [[f'<span class="mono">{esc(t.get("name"))}</span>'] + _twin_cells({**t, "window_fit": "off-window"})
                for t in st["unregistered_twin_runs"]]
        out.append('<h3 class="fam">Twin runs of unregistered genomes</h3>'
                   + table(["genome", "twin verdict · replay range · source", "net c/window [95% CI]",
                            "net c/contract [CI]", "windows eval / traded", "commercial LB $/day"], rows, "num reg"))
    return "".join(out)


def _h3(h: dict) -> str:
    title = f'<h3 class="fam" id="h3">H3 four-arm comparison <span class="sub">{esc(h.get("family") or "")}</span></h3>'
    if not h.get("ready"):
        return title + f'<p class="notyet">not yet: {esc(h.get("why_not") or "no results")}</p>'
    rows = []
    for r in h.get("rows") or []:
        vb = r.get("vs_base")
        if vb:
            c = vb.get("ci95") or [None, None]
            delta = (f'{num(vb.get("delta_c_per_window"), 2, sign=True)} <span class="ci">[{num(c[0], 2)}, '
                     f'{num(c[1], 2)}]</span><div class="sub">{num(vb.get("n_pairs"), 0)} paired windows · '
                     f'{num(vb.get("delta_usd_total"), 2, sign=True)} $</div>')
        else:
            delta = '<span class="muted">reference</span>'
        dec = r.get("decision") or "?"
        rows.append([f'<b>{esc(r.get("arm"))}</b><div class="sub mono">{esc(r.get("name"))}</div>{src(r.get("src"))}',
                     pill(dec, DECISION_KIND.get(dec, "muted")), ci(r.get("net_c_per_window")), delta,
                     num(r.get("total_net_usd"), 2, sign=True), num(r.get("contracts"), 0),
                     num(r.get("max_drawdown_usd"), 2), num(r.get("commercial_lb_usd_day"), 2, sign=True),
                     f'{num(r.get("windows_evaluated"), 0)} / {num(r.get("windows_traded"), 0)}'])
    rng = h.get("range_iso") or [None, None]
    fit = h.get("window_fit")
    out = (title + f'<p class="meta">replay range {esc(rng[0])} → {esc(rng[1])} · registered window '
           f'{esc((h.get("eval_window") or [None, None])[0])} → {esc((h.get("eval_window") or [None, None])[1])} '
           f'{pill(fit or "?", {"prereg": "good", "partial": "warn"}.get(fit or "", "bad"))}</p>'
           + table(["arm · source", "verdict", "net c/window [95% CI]", "Δ vs base c/window [95% CI]", "net $",
                    "contracts", "max DD $", "commercial LB $/day", "windows eval / traded"], rows, "num reg"))
    if h.get("delay_sweep"):
        srows = []
        for r in h["delay_sweep"]:
            vb = r.get("vs_base") or {}
            c = vb.get("ci95") or [None, None]
            srows.append([f'{num(r.get("flow_delay_s"), 0)} s', ci(r.get("net_c_per_window")),
                          f'{num(vb.get("delta_c_per_window"), 2, sign=True)} <span class="ci">[{num(c[0], 2)}, {num(c[1], 2)}]</span>',
                          num(vb.get("n_pairs"), 0), src(r.get("src"))])
        out += ('<h4>ident arm delay sweep (intl flow seen late)</h4>'
                + table(["delay", "net c/window [CI]", "Δ vs base c/window [CI]", "pairs", "source"], srows, "num"))
    out += ('<p class="note">Δ vs base = arm minus base net cents on identical eligible windows (matched by slug), '
            'moving-block bootstrap (12 windows, 2,000 draws, seed 0, as in the twin verdict). No winner is '
            'declared here: the registry\'s Holm ledger decides.</p>')
    return out


def _edge_tbl(cells: dict | None, label: str) -> str:
    if not isinstance(cells, dict) or not cells:
        return NOT_YET
    rows = []
    for k, c in cells.items():
        if not isinstance(c, dict):
            rows.append([esc(k), "–", "–", "–", "–", "–"])
            continue
        rows.append([esc(k), num(c.get("n"), 0), num(c.get("contracts"), 0), num(c.get("maker_pnl_c"), 2, sign=True),
                     num(c.get("model_edge_c"), 2, sign=True), num(c.get("adverse_c"), 2, sign=True)])
    return table([label, "prints", "contracts", "maker c/ct", "fair-v1 edge c/ct", "adverse c/ct"], rows, "num")


def section_evidence(ev: dict) -> str:
    out = []
    f = ev.get("findings") or {}
    out.append("<h3>Findings</h3>")
    if f.get("rows"):
        rows = [[f'<b>{esc(r["id"])}</b>', esc(r.get("line")), f'<span class="sub">{esc(r.get("confidence"))}</span>']
                for r in f["rows"]]
        out.append(table(["id", "finding (first sentence)", "confidence"], rows, "find"))
        out.append('<div class="src">' + " · ".join(f'<span class="f">{esc(x)}</span>' for x in f.get("files") or [])
                   + (f' · file {esc(f["rows"][-1]["src"].get("mtime"))}' if f["rows"] else "") + "</div>")
    else:
        out.append(NOT_YET)

    out.append("<h3>Census (latest)</h3>")
    c = ev.get("census")
    if c:
        rows = [[esc(r.get("role")), esc(r.get("label")), num(r.get("wallets"), 0), num(r.get("positive"), 0),
                 num(r.get("per_day"), 0, sign=True)] for r in c.get("rows") or []]
        sc = c.get("selfcheck") or {}
        out.append(f'<p class="meta">cohort <span class="mono">{esc((c.get("cohort_sha256") or "")[:12])}</span> · '
                   f'{num(c.get("wallets"), 0)} wallets, {num(c.get("eligible"), 0)} eligible · self-check '
                   f'{num(sc.get("ok"), 0)}/{num(sc.get("window_specialists"), 0)} window specialists</p>')
        out.append(table(["role", "mechanism", "wallets", "positive", "window econ $/day"], rows, "num"))
        out.append(src(c.get("src")))
    else:
        out.append(NOT_YET)

    out.append("<h3>Fill-conditional edge (adverse, E24 style)</h3>")
    if ev.get("adverse"):
        for a in ev["adverse"]:
            t = a.get("total") or {}
            out.append(f'<p class="meta">BTC {esc(a.get("hz"))} · {num(t.get("n"), 0)} prints, {num(t.get("contracts"), 0)} '
                       f'contracts · maker {num(t.get("maker_pnl_c"), 2, sign=True)} c vs fair-v1 '
                       f'{num(t.get("model_edge_c"), 2, sign=True)} c · adverse {num(t.get("adverse_c"), 2, sign=True)} c/ct</p>')
            out.append('<div class="two">' + _edge_tbl(a.get("by_model_edge"), "fair-v1 edge")
                       + _edge_tbl(a.get("by_tte"), "time to close") + "</div>")
            out.append(src(a.get("src")))
    else:
        out.append(NOT_YET)

    out.append("<h3>Clock 0 (anonymous print → taker informedness, E27 style)</h3>")
    k = ev.get("clock0")
    if k:
        kv = [("status", esc(k.get("status"))), ("scores as of", esc(k.get("as_of"))), ("until", esc(k.get("until"))),
              ("txs (train / test)", f'{num(k.get("n"), 0)} ({num(k.get("n_train"), 0)} / {num(k.get("n_test"), 0)})'),
              ("label", f'{esc(k.get("label"))} · threshold {num(k.get("excess_threshold_c"), 2)} c · base rate '
                        f'{num(k.get("base_rate_label"), 3)}'),
              ("AUC model / size only", f'<b>{num(k.get("auc_model"), 3)}</b> / {num(k.get("auc_size_only"), 3)}'),
              ("corr with weight", num(k.get("corr_weight_model"), 3)),
              ("markout spearman", num(k.get("markout_spearman"), 3)),
              ("top features", esc(", ".join(f"{n} {v:+.3f}" for n, v in (k.get("top_features") or [])
                                             if isinstance(v, (int, float)))))]
        out.append('<dl class="kv">' + "".join(f"<dt>{a}</dt><dd>{b}</dd>" for a, b in kv) + "</dl>")
        if k.get("by_predicted_quintile"):
            rows = [[num(q.get("quintile"), 0), num(q.get("n"), 0), num(q.get("informed_rate"), 3),
                     num(q.get("taker_markout_c_usd_wtd"), 2, sign=True)] for q in k["by_predicted_quintile"]]
            out.append(table(["predicted quintile", "txs", "informed rate", "taker markout c ($-wtd)"], rows, "num"))
        out.append(src(k.get("src")))
    else:
        out.append(NOT_YET)

    out.append("<h3>Cross-venue PM US 15m vs Kalshi KXBTC15M (xvenue)</h3>")
    if ev.get("xvenue"):
        rows = []
        for x in ev["xvenue"]:
            fe, eq, s = x.get("fees") or {}, x.get("equivalence") or {}, x.get("summary") or {}
            rows.append([f'Θ {num(fe.get("pmus_coef"), 4)} · rebate {num(fe.get("pmus_rebate"), 2)}',
                         f'{num(eq.get("strike_match"), 0)}/{num(eq.get("checked"), 0)} strike · '
                         f'{num(eq.get("result_match"), 0)} result',
                         num(s.get("windows"), 0), num(s.get("windows_with_arb"), 0), num(s.get("episodes"), 0),
                         num(s.get("episode_dollars"), 2), num(s.get("episode_dollars_ex_last60s"), 2),
                         num(s.get("arb_dollars_upper_total"), 2), src(x.get("src"))])
        out.append(table(["fees", "equivalence", "windows", "with arb", "episodes", "episode $", "ex last 60 s $",
                          "upper bound $", "source"], rows, "num"))
        out.append('<p class="note">Taker-taker on displayed quotes: optimistic on simultaneity, no leg risk. '
                   'The upper bound re-counts overlapping seconds.</p>')
    else:
        out.append(NOT_YET)
    return "".join(out)


def section_calculator(c: dict) -> str:
    d = c.get("defaults") or {}
    ex = c.get("example") or {}
    opts = "".join(
        f'<option value="{i}" data-net="{esc(p.get("net_c_per_contract"))}" data-vol="{esc(p.get("contracts_per_day"))}" '
        f'data-op="{esc(p.get("op_usd_month"))}" data-src="{esc((p.get("src") or {}).get("file"))}" '
        f'data-note="{esc(p.get("note"))}" title="{esc(p.get("note"))}">'
        f'{esc(p.get("label"))} ({num(p.get("net_c_per_contract"), 2, sign=True)} c)</option>'
        for i, p in enumerate(c.get("presets") or []))
    preset = (f'<label>preset <select id="calc-preset"><option value="">manual</option>{opts}</select></label>'
              if opts else '<p class="sub">no presets yet (twin verdicts with contracts fill this list)</p>')
    notes = "".join(f'<li>{esc(p.get("label"))}: {esc(p.get("note"))}</li>' for p in c.get("presets") or [] if p.get("note"))
    notes = f'<ul class="note">{notes}</ul>' if notes else ""
    be = ex.get("breakeven_contracts_per_day")
    return f"""
<form id="calc" class="calc" onsubmit="return false">
  {preset}
  <label>net c / contract <input id="calc-net" type="number" step="0.01" value="{esc(d.get("net_c_per_contract"))}"></label>
  <label>contracts / day <input id="calc-vol" type="number" step="100" min="0" value="{esc(d.get("contracts_per_day"))}"></label>
  <label>operating $ / month <input id="calc-op" type="number" step="5" min="0" value="{esc(d.get("op_usd_month"))}"></label>
</form>
<div class="calc-out">
  <div><span class="k">edge $/day</span><span class="v" id="calc-edge">{num(ex.get("edge_usd_per_day"), 2, sign=True)}</span></div>
  <div><span class="k">operating $/day</span><span class="v" id="calc-opd">{num(ex.get("op_usd_per_day"), 2)}</span></div>
  <div><span class="k">net $/day</span><span class="v big" id="calc-net-day">{num(ex.get("net_usd_per_day"), 2, sign=True)}</span></div>
  <div><span class="k">break-even contracts/day</span><span class="v big" id="calc-be">{num(be, 0) if be is not None else "never (net ≤ 0)"}</span></div>
</div>
<div class="src" id="calc-src"></div>{notes}
<p class="note">{esc(c.get("formula"))}. Same arithmetic as the twin's commercial block (A6). Incentives and
rebates are not included; the PM US 70% API-taker rebate ends 2026-12-31 (E25).</p>"""


def body(desk: dict) -> str:
    probs = desk.get("problems") or []
    pr = ("" if not probs else '<section id="problems"><h2>Inputs skipped</h2><ul class="probs">'
          + "".join(f'<li><span class="mono">{esc(p["file"])}</span>: {esc(p["error"])}</li>' for p in probs)
          + "</ul></section>")
    return (f'<section id="recorder"><h2><span class="n">1</span>Recorder health</h2>{section_recorder(desk.get("recorder") or {})}</section>'
            f'<section id="strategies"><h2><span class="n">2</span>Strategy registry</h2>{section_strategies(desk.get("strategies") or {})}</section>'
            f'<section id="evidence"><h2><span class="n">3</span>Edge evidence</h2>{section_evidence(desk.get("evidence") or {})}</section>'
            f'<section id="calculator"><h2><span class="n">4</span>Break-even calculator</h2>{section_calculator(desk.get("calculator") or {})}</section>'
            + pr)


def embed(desk: dict) -> str:
    raw = orjson.dumps(desk, option=orjson.OPT_SORT_KEYS | orjson.OPT_NON_STR_KEYS).decode()
    return raw.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026") \
        .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def render(desk: dict, template: str) -> str:
    n_probs = len(desk.get("problems") or [])
    built = esc(desk.get("built"))
    status = f'built {built} UTC · {n_probs} input(s) skipped' if n_probs else f"built {built} UTC"
    return (template.replace("<!--DESK:STATUS-->", status)
            .replace("<!--DESK:BODY-->", body(desk))
            .replace("/*DESK:DATA*/", embed(desk)))
