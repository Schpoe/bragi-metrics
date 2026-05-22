"""Generate department performance PDF reports.

Usage:
  python report.py --quarter 2026-Q2
  python report.py --quarter auto          # previous quarter
  python report.py --month 2026-05
  python report.py --month auto            # previous month
  python report.py --quarter 2026-Q2 --out /reports/my-report.pdf
"""

import argparse
import base64
import io
import os
import sys
from datetime import date, datetime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from jinja2 import Environment, FileSystemLoader
from weasyprint import HTML

import queries as q

REPORTS_DIR = Path(os.environ.get("REPORTS_DIR", "/reports"))
TEAMS = q.TEAMS
TEAM_COLORS = {"STORE": "#4e79a7", "AAONE": "#f28e2b", "AATWO": "#59a14f", "CONNECT": "#e15759"}
MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]

# KPI targets from the Operational Improvement Framework
KPI_TARGETS = {
    "delivery_pct":     {"target": 80,  "op": "gte", "label": "≥80%"},
    "scope_change_pct": {"target": 10,  "op": "lt",  "label": "<10%"},
    "readiness_pct":    {"target": 90,  "op": "gte", "label": "≥90%"},
}

# Active improvement initiatives (ISS register) shown in exec reports
ACTIVE_ISSUES = [
    ("ISS-001", "Unreliable sprint planning"),
    ("ISS-002", "A-Gate items entering sprints incomplete"),
    ("ISS-003", "QA bottleneck inflating lead time & bug count"),
    ("ISS-004", "Mid-sprint scope creep"),
    ("ISS-005", "Release quality & incomplete test evidence"),
]


# ── Helpers ────────────────────────────────────────────────────────────────────

def trend_arrow(cur, prev, higher_is_better=True):
    if cur is None or prev is None:
        return "–"
    diff = float(cur) - float(prev)
    if abs(diff) < 0.01:
        return "→"
    up = diff > 0
    if higher_is_better:
        return "↑" if up else "↓"
    else:
        return "↑" if up else "↓"


def trend_class(cur, prev, higher_is_better=True):
    if cur is None or prev is None:
        return "neutral"
    diff = float(cur) - float(prev)
    if abs(diff) < 0.01:
        return "neutral"
    improved = (diff > 0) == higher_is_better
    return "good" if improved else "bad"


def fmt(val, suffix="", decimals=1):
    if val is None:
        return "—"
    return f"{float(val):.{decimals}f}{suffix}"


def target_status(val, metric_key):
    """Returns 'good', 'bad', or 'neutral' relative to the KPI target."""
    t = KPI_TARGETS.get(metric_key)
    if t is None or val is None:
        return "neutral"
    v = float(val)
    if t["op"] == "gte":
        return "good" if v >= t["target"] else "bad"
    if t["op"] == "lt":
        return "good" if v < t["target"] else "bad"
    return "neutral"


def dept_sum(rows, field):
    vals = [float(r[field]) for r in rows if r.get(field) is not None]
    return sum(vals) if vals else None


def dept_avg(rows, field):
    vals = [float(r[field]) for r in rows if r.get(field) is not None]
    return round(sum(vals) / len(vals), 1) if vals else None


# ── Charts ─────────────────────────────────────────────────────────────────────

def chart_to_b64(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode()


def sparkline(trend_data, title, unit=""):
    fig, ax = plt.subplots(figsize=(5.5, 2.4))
    labels = [d["label"] for d in trend_data]
    for team in TEAMS:
        vals = [d["vals"].get(team) for d in trend_data]
        if any(v is not None for v in vals):
            ax.plot(labels, vals, marker="o", markersize=4, linewidth=2,
                    color=TEAM_COLORS[team], label=team)
    ax.set_title(title, fontsize=9, pad=3)
    ax.tick_params(axis="both", labelsize=7)
    ax.set_ylabel(unit, fontsize=7)
    ax.grid(axis="y", alpha=0.25, linestyle="--")
    ax.legend(fontsize=7, loc="best", framealpha=0.6)
    fig.tight_layout(pad=0.4)
    return chart_to_b64(fig)


def bar_grouped(rows, cur_field, prev_field, title, unit=""):
    fig, ax = plt.subplots(figsize=(5.5, 2.4))
    teams = [r["team"] for r in rows]
    cur_v = [float(r.get(cur_field) or 0) for r in rows]
    prev_v = [float(r.get(prev_field) or 0) for r in rows]
    x = range(len(teams))
    ax.bar([i - 0.2 for i in x], cur_v, width=0.35, color="#4e79a7", label="This period")
    ax.bar([i + 0.2 for i in x], prev_v, width=0.35, color="#b0b0b0", label="Prev period")
    ax.set_xticks(list(x))
    ax.set_xticklabels(teams, fontsize=8)
    ax.tick_params(axis="y", labelsize=7)
    ax.set_title(title, fontsize=9, pad=3)
    ax.set_ylabel(unit, fontsize=7)
    ax.legend(fontsize=7, framealpha=0.6)
    ax.grid(axis="y", alpha=0.25, linestyle="--")
    fig.tight_layout(pad=0.4)
    return chart_to_b64(fig)


# ── Data assembly ──────────────────────────────────────────────────────────────

def build_quarterly_data(conn, yr, qnum):
    eff_rows = q.quarterly_efficiency(conn, yr, qnum)
    lt_rows = q.quarterly_lead_time(conn, yr, qnum)
    qual_rows = q.quarterly_quality(conn, yr, qnum)
    trans_rows, readiness_valid = q.quarterly_transparency(conn, yr, qnum)

    lt_map = {r["team"]: r for r in lt_rows}
    for row in eff_rows:
        lt = lt_map.get(row["team"], {})
        row["lead_time"] = lt.get("lead_time")
        row["prev_lead_time"] = lt.get("prev_lead_time")

    qual_map = {r["team"]: r for r in qual_rows}
    trans_map = {r["team"]: r for r in trans_rows}

    teams_data = []
    for team in TEAMS:
        e = next((r for r in eff_rows if r["team"] == team), {})
        ql = qual_map.get(team, {})
        tr = trans_map.get(team, {})
        teams_data.append({
            "team": team,
            "sp_delivered": e.get("sp_delivered"),
            "velocity": e.get("velocity"),
            "delivery_pct": e.get("delivery_pct"),
            "lead_time": e.get("lead_time"),
            "prev_sp_delivered": e.get("prev_sp_delivered"),
            "prev_velocity": e.get("prev_velocity"),
            "prev_delivery_pct": e.get("prev_delivery_pct"),
            "prev_lead_time": e.get("prev_lead_time"),
            "bugs_created": ql.get("bugs_created"),
            "bugs_resolved": ql.get("bugs_resolved"),
            "prev_bugs_created": ql.get("prev_bugs_created"),
            "prev_bugs_resolved": ql.get("prev_bugs_resolved"),
            "issues_resolved": tr.get("issues_resolved"),
            "scope_change_pct": tr.get("scope_change_pct"),
            "readiness_pct": tr.get("readiness_pct"),
            "prev_issues_resolved": tr.get("prev_issues_resolved"),
            "prev_scope_change_pct": tr.get("prev_scope_change_pct"),
            "prev_readiness_pct": tr.get("prev_readiness_pct"),
        })

    dept = {
        "sp_delivered": dept_sum(teams_data, "sp_delivered"),
        "prev_sp_delivered": dept_sum(teams_data, "prev_sp_delivered"),
        "velocity": dept_avg(teams_data, "velocity"),
        "prev_velocity": dept_avg(teams_data, "prev_velocity"),
        "delivery_pct": dept_avg(teams_data, "delivery_pct"),
        "prev_delivery_pct": dept_avg(teams_data, "prev_delivery_pct"),
        "lead_time": dept_avg(teams_data, "lead_time"),
        "prev_lead_time": dept_avg(teams_data, "prev_lead_time"),
        "bugs_created": dept_sum(teams_data, "bugs_created"),
        "prev_bugs_created": dept_sum(teams_data, "prev_bugs_created"),
        "bugs_resolved": dept_sum(teams_data, "bugs_resolved"),
        "prev_bugs_resolved": dept_sum(teams_data, "prev_bugs_resolved"),
        "issues_resolved": dept_sum(teams_data, "issues_resolved"),
        "prev_issues_resolved": dept_sum(teams_data, "prev_issues_resolved"),
        "scope_change_pct": dept_avg(teams_data, "scope_change_pct"),
        "prev_scope_change_pct": dept_avg(teams_data, "prev_scope_change_pct"),
        "readiness_pct": dept_avg(teams_data, "readiness_pct") if readiness_valid else None,
        "prev_readiness_pct": dept_avg(teams_data, "prev_readiness_pct") if readiness_valid else None,
    }

    trends = {k: q.trend_quarterly(conn, k) for k in
              ("velocity", "delivery_pct", "lead_time", "bugs_created", "bugs_resolved", "issues_resolved")}

    charts = {
        "velocity_trend": sparkline(trends["velocity"], "Avg Velocity (SP/sprint)", "SP"),
        "delivery_trend": sparkline(trends["delivery_pct"], "Delivery %", "%"),
        "lead_time_trend": sparkline(trends["lead_time"], "Avg Lead Time", "days"),
        "bugs_created_trend": sparkline(trends["bugs_created"], "Bugs Created", "count"),
        "bugs_resolved_trend": sparkline(trends["bugs_resolved"], "Bugs Resolved", "count"),
        "issues_resolved_trend": sparkline(trends["issues_resolved"], "Issues Resolved", "count"),
    }

    pyr, pq = q.prev_quarter(yr, qnum)
    return {
        "period_label": f"Q{qnum} {yr}",
        "prev_period_label": f"Q{pq} {pyr}",
        "report_type": "Quarterly",
        "teams": teams_data,
        "dept": dept,
        "charts": charts,
        "readiness_valid": readiness_valid,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


def build_monthly_data(conn, yr, m):
    rows = q.monthly_data(conn, yr, m)
    teams_data = []
    for team in TEAMS:
        r = next((x for x in rows if x["team"] == team), {})
        teams_data.append({
            "team": team,
            "bugs_created": r.get("bugs_created"),
            "bugs_resolved": r.get("bugs_resolved"),
            "issues_resolved": r.get("issues_resolved"),
            "lead_time": r.get("lead_time"),
            "prev_bugs_created": r.get("prev_bugs_created"),
            "prev_bugs_resolved": r.get("prev_bugs_resolved"),
            "prev_issues_resolved": r.get("prev_issues_resolved"),
            "prev_lead_time": r.get("prev_lead_time"),
        })

    dept = {k: (dept_sum if "pct" not in k and "time" not in k else dept_avg)(teams_data, k)
            for k in ("bugs_created", "bugs_resolved", "issues_resolved", "lead_time",
                      "prev_bugs_created", "prev_bugs_resolved", "prev_issues_resolved", "prev_lead_time")}

    charts = {
        "bugs_bar": bar_grouped(teams_data, "bugs_created", "prev_bugs_created", "Bugs Created", "count"),
        "bugs_resolved_bar": bar_grouped(teams_data, "bugs_resolved", "prev_bugs_resolved", "Bugs Resolved", "count"),
        "throughput_bar": bar_grouped(teams_data, "issues_resolved", "prev_issues_resolved", "Issues Resolved", "count"),
        "lead_time_bar": bar_grouped(teams_data, "lead_time", "prev_lead_time", "Avg Lead Time", "days"),
    }

    pyr, pm = q.prev_month(yr, m)
    return {
        "period_label": f"{MONTHS[m-1]} {yr}",
        "prev_period_label": f"{MONTHS[pm-1]} {pyr}",
        "report_type": "Monthly",
        "teams": teams_data,
        "dept": dept,
        "charts": charts,
        "readiness_valid": False,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


# ── Render ─────────────────────────────────────────────────────────────────────

def render_pdf(data, report_type, out_path):
    tpl_dir = Path(__file__).parent / "templates"
    env = Environment(loader=FileSystemLoader(str(tpl_dir)))
    env.globals.update(
        trend_arrow=trend_arrow, trend_class=trend_class, fmt=fmt,
        target_status=target_status, kpi_targets=KPI_TARGETS, active_issues=ACTIVE_ISSUES,
    )
    tpl_name = "quarterly_report.html" if report_type == "quarterly" else "monthly_report.html"
    html_str = env.get_template(tpl_name).render(**data)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    HTML(string=html_str).write_pdf(str(out_path))
    print(f"Written: {out_path}")


# ── CLI ────────────────────────────────────────────────────────────────────────

def resolve_quarter(val):
    if val == "auto":
        today = date.today()
        qnum = (today.month - 1) // 3 + 1
        return q.prev_quarter(today.year, qnum)
    yr, qn = val.split("-Q")
    return int(yr), int(qn)


def resolve_month(val):
    if val == "auto":
        today = date.today()
        return q.prev_month(today.year, today.month)
    yr, m = val.split("-")
    return int(yr), int(m)


def main():
    p = argparse.ArgumentParser()
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--quarter", help="2026-Q2 or 'auto'")
    g.add_argument("--month", help="2026-05 or 'auto'")
    p.add_argument("--out", help="Output path override")
    args = p.parse_args()

    conn = q.get_conn()
    try:
        if args.quarter:
            yr, qnum = resolve_quarter(args.quarter)
            data = build_quarterly_data(conn, yr, qnum)
            default = REPORTS_DIR / f"dept-quarterly-{yr}-Q{qnum}.pdf"
            rtype = "quarterly"
        else:
            yr, m = resolve_month(args.month)
            data = build_monthly_data(conn, yr, m)
            default = REPORTS_DIR / f"dept-monthly-{yr}-{m:02d}.pdf"
            rtype = "monthly"
        render_pdf(data, rtype, Path(args.out) if args.out else default)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
