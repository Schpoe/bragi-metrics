"""All DB queries for report generation."""

import os
import psycopg2
import psycopg2.extras

TEAMS = ("STORE", "AAONE", "AATWO", "CONNECT", "BEST", "GROW", "TCSA")
TEAMS_SQL = "('STORE','AAONE','AATWO','CONNECT','BEST','GROW','TCSA')"
OBSOLETE_SQL = "LOWER(i.status) NOT IN ('obsolete','won''t do','obsolete / won''t do','obsolete / won')"


def get_conn():
    return psycopg2.connect(
        host=os.environ["POSTGRES_HOST"],
        dbname=os.environ["POSTGRES_DB"],
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
    )


def fetchone(conn, sql, params=()):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def fetchall(conn, sql, params=()):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def prev_quarter(yr, q):
    return (yr - 1, 4) if q == 1 else (yr, q - 1)


def prev_month(yr, m):
    return (yr - 1, 12) if m == 1 else (yr, m - 1)


def qf(alias, yp="%s", qp="%s"):
    """Quarter filter fragment."""
    return f"EXTRACT(YEAR FROM {alias})::int = {yp} AND EXTRACT(QUARTER FROM {alias})::int = {qp}"


def mf(alias, yp="%s", mp="%s"):
    """Month filter fragment."""
    return f"EXTRACT(YEAR FROM {alias})::int = {yp} AND EXTRACT(MONTH FROM {alias})::int = {mp}"


# ── Quarterly ──────────────────────────────────────────────────────────────────

def quarterly_efficiency(conn, yr, q):
    pyr, pq = prev_quarter(yr, q)
    sql = f"""
    WITH cur AS (
      SELECT pd.project_key,
        COUNT(DISTINCT pd.sprint_id) AS nsprints,
        SUM(pd.delivered_points) AS sp_del,
        SUM(pd.committed_points) AS sp_com
      FROM v_planning_deviation_proj pd
      WHERE pd.state='closed' AND pd.project_key IN {TEAMS_SQL} AND {qf('pd.start_date')}
      GROUP BY pd.project_key
    ),
    prv AS (
      SELECT pd.project_key,
        COUNT(DISTINCT pd.sprint_id) AS nsprints,
        SUM(pd.delivered_points) AS sp_del,
        SUM(pd.committed_points) AS sp_com
      FROM v_planning_deviation_proj pd
      WHERE pd.state='closed' AND pd.project_key IN {TEAMS_SQL} AND {qf('pd.start_date')}
      GROUP BY pd.project_key
    )
    SELECT
      COALESCE(c.project_key, p.project_key) AS team,
      COALESCE(c.sp_del,0) AS sp_delivered,
      ROUND(COALESCE(c.sp_del,0)::numeric / NULLIF(c.nsprints,0), 1) AS velocity,
      ROUND(100.0*COALESCE(c.sp_del,0) / NULLIF(c.sp_com,0), 1) AS delivery_pct,
      COALESCE(p.sp_del,0) AS prev_sp_delivered,
      ROUND(COALESCE(p.sp_del,0)::numeric / NULLIF(p.nsprints,0), 1) AS prev_velocity,
      ROUND(100.0*COALESCE(p.sp_del,0) / NULLIF(p.sp_com,0), 1) AS prev_delivery_pct
    FROM cur c FULL JOIN prv p ON c.project_key = p.project_key
    ORDER BY team
    """
    return fetchall(conn, sql, (yr, q, pyr, pq))


def quarterly_lead_time(conn, yr, q):
    pyr, pq = prev_quarter(yr, q)
    sql = f"""
    SELECT i.project_key AS team,
      ROUND(AVG(EXTRACT(EPOCH FROM (i.resolved_at-i.created_at))/86400.0)
        FILTER(WHERE {qf('i.resolved_at')}), 1) AS lead_time,
      ROUND(AVG(EXTRACT(EPOCH FROM (i.resolved_at-i.created_at))/86400.0)
        FILTER(WHERE {qf('i.resolved_at')}), 1) AS prev_lead_time
    FROM issues i
    WHERE i.project_key IN {TEAMS_SQL}
      AND i.issue_type NOT IN ('Epic','Sub-task')
      AND i.resolved_at IS NOT NULL AND {OBSOLETE_SQL}
      AND ({qf('i.resolved_at')} OR {qf('i.resolved_at')})
    GROUP BY i.project_key ORDER BY i.project_key
    """
    return fetchall(conn, sql, (yr, q, pyr, pq, yr, q, pyr, pq))


def quarterly_quality(conn, yr, q):
    pyr, pq = prev_quarter(yr, q)
    sql = f"""
    SELECT i.project_key AS team,
      COUNT(*) FILTER(WHERE i.issue_type='Bug' AND {qf('i.created_at')}) AS bugs_created,
      COUNT(*) FILTER(WHERE i.issue_type='Bug' AND i.resolved_at IS NOT NULL
        AND {qf('i.resolved_at')}) AS bugs_resolved,
      COUNT(*) FILTER(WHERE i.issue_type='Bug' AND {qf('i.created_at')}) AS prev_bugs_created,
      COUNT(*) FILTER(WHERE i.issue_type='Bug' AND i.resolved_at IS NOT NULL
        AND {qf('i.resolved_at')}) AS prev_bugs_resolved
    FROM issues i
    WHERE i.project_key IN {TEAMS_SQL}
    GROUP BY i.project_key ORDER BY i.project_key
    """
    return fetchall(conn, sql, (yr, q, yr, q, pyr, pq, pyr, pq))


def quarterly_transparency(conn, yr, q, readiness_valid_yr=2026, readiness_valid_q=2):
    pyr, pq = prev_quarter(yr, q)
    valid = (yr > readiness_valid_yr) or (yr == readiness_valid_yr and q >= readiness_valid_q)

    sql = f"""
    WITH scope_agg AS (
      SELECT pd.project_key,
        SUM(COALESCE(a.sp,0)+COALESCE(r.sp,0)) FILTER(WHERE {qf('pd.start_date')}) AS chg,
        SUM(pd.committed_points)               FILTER(WHERE {qf('pd.start_date')}) AS com,
        SUM(COALESCE(a.sp,0)+COALESCE(r.sp,0)) FILTER(WHERE {qf('pd.start_date')}) AS prev_chg,
        SUM(pd.committed_points)               FILTER(WHERE {qf('pd.start_date')}) AS prev_com
      FROM v_planning_deviation_proj pd
      LEFT JOIN (
        SELECT si.sprint_id, i.project_key, SUM(COALESCE(si.story_points_at_add,0)) AS sp
        FROM sprint_issues si JOIN issues i ON i.key = si.issue_key
        WHERE si.was_in_initial_scope=FALSE AND si.removed_at IS NULL
          AND i.issue_type NOT IN ('Epic','Sub-task') AND {OBSOLETE_SQL}
        GROUP BY si.sprint_id, i.project_key
      ) a ON a.sprint_id = pd.sprint_id AND a.project_key = pd.project_key
      LEFT JOIN (
        SELECT si.sprint_id, i.project_key, SUM(COALESCE(si.story_points_at_add,0)) AS sp
        FROM sprint_issues si JOIN issues i ON i.key = si.issue_key
        WHERE si.removed_at IS NOT NULL
          AND i.issue_type NOT IN ('Epic','Sub-task') AND {OBSOLETE_SQL}
        GROUP BY si.sprint_id, i.project_key
      ) r ON r.sprint_id = pd.sprint_id AND r.project_key = pd.project_key
      WHERE pd.state='closed' AND pd.project_key IN {TEAMS_SQL}
        AND ({qf('pd.start_date')} OR {qf('pd.start_date')})
      GROUP BY pd.project_key
    )
    SELECT i.project_key AS team,
      COUNT(*) FILTER(WHERE i.issue_type NOT IN ('Epic','Sub-task')
        AND i.resolved_at IS NOT NULL AND {OBSOLETE_SQL}
        AND {qf('i.resolved_at')}) AS issues_resolved,
      COUNT(*) FILTER(WHERE i.issue_type NOT IN ('Epic','Sub-task')
        AND i.resolved_at IS NOT NULL AND {OBSOLETE_SQL}
        AND {qf('i.resolved_at')}) AS prev_issues_resolved,
      ROUND(100.0*sa.chg / NULLIF(sa.com,0), 1) AS scope_change_pct,
      ROUND(100.0*sa.prev_chg / NULLIF(sa.prev_com,0), 1) AS prev_scope_change_pct
    FROM issues i
    LEFT JOIN scope_agg sa ON sa.project_key = i.project_key
    WHERE i.project_key IN {TEAMS_SQL}
    GROUP BY i.project_key, sa.chg, sa.com, sa.prev_chg, sa.prev_com
    ORDER BY i.project_key
    """
    # param order: FILTER(added), FILTER(initial), FILTER(prev_added), FILTER(prev_initial),
    # WHERE OR(cur), WHERE OR(prev), issues_resolved FILTER, prev_issues_resolved FILTER
    rows = fetchall(conn, sql, (yr, q, yr, q, pyr, pq, pyr, pq, yr, q, pyr, pq, yr, q, pyr, pq))

    if valid:
        rsql = f"""
        SELECT i.project_key AS team,
          ROUND(100.0 * COUNT(*) FILTER(WHERE i.story_points IS NOT NULL AND i.story_points > 0
            AND COALESCE(i.has_acceptance_criteria,FALSE)=TRUE AND i.assignee IS NOT NULL
            AND i.epic_key IS NOT NULL AND cardinality(i.components) > 0
            AND {qf('i.created_at')})
          / NULLIF(COUNT(*) FILTER(WHERE {qf('i.created_at')}), 0), 1) AS readiness_pct,
          ROUND(100.0 * COUNT(*) FILTER(WHERE i.story_points IS NOT NULL AND i.story_points > 0
            AND COALESCE(i.has_acceptance_criteria,FALSE)=TRUE AND i.assignee IS NOT NULL
            AND i.epic_key IS NOT NULL AND cardinality(i.components) > 0
            AND {qf('i.created_at')})
          / NULLIF(COUNT(*) FILTER(WHERE {qf('i.created_at')}), 0), 1) AS prev_readiness_pct
        FROM issues i
        WHERE i.project_key IN {TEAMS_SQL}
          AND i.issue_type IN ('Story','Task','Improvement') AND {OBSOLETE_SQL}
        GROUP BY i.project_key ORDER BY i.project_key
        """
        rm = {r["team"]: r for r in fetchall(conn, rsql, (yr, q, yr, q, pyr, pq, pyr, pq))}
        for row in rows:
            rd = rm.get(row["team"], {})
            row["readiness_pct"] = rd.get("readiness_pct")
            row["prev_readiness_pct"] = rd.get("prev_readiness_pct")
    else:
        for row in rows:
            row["readiness_pct"] = None
            row["prev_readiness_pct"] = None

    return rows, valid


# ── Monthly ────────────────────────────────────────────────────────────────────

def monthly_data(conn, yr, m):
    pyr, pm = prev_month(yr, m)
    sql = f"""
    SELECT i.project_key AS team,
      COUNT(*) FILTER(WHERE i.issue_type='Bug' AND {mf('i.created_at')}) AS bugs_created,
      COUNT(*) FILTER(WHERE i.issue_type='Bug' AND i.resolved_at IS NOT NULL
        AND {mf('i.resolved_at')}) AS bugs_resolved,
      COUNT(*) FILTER(WHERE i.issue_type NOT IN ('Epic','Sub-task')
        AND i.resolved_at IS NOT NULL AND {OBSOLETE_SQL}
        AND {mf('i.resolved_at')}) AS issues_resolved,
      ROUND(AVG(EXTRACT(EPOCH FROM (i.resolved_at-i.created_at))/86400.0)
        FILTER(WHERE i.issue_type NOT IN ('Epic','Sub-task')
          AND i.resolved_at IS NOT NULL AND {OBSOLETE_SQL}
          AND {mf('i.resolved_at')}), 1) AS lead_time,
      COUNT(*) FILTER(WHERE i.issue_type='Bug' AND {mf('i.created_at')}) AS prev_bugs_created,
      COUNT(*) FILTER(WHERE i.issue_type='Bug' AND i.resolved_at IS NOT NULL
        AND {mf('i.resolved_at')}) AS prev_bugs_resolved,
      COUNT(*) FILTER(WHERE i.issue_type NOT IN ('Epic','Sub-task')
        AND i.resolved_at IS NOT NULL AND {OBSOLETE_SQL}
        AND {mf('i.resolved_at')}) AS prev_issues_resolved,
      ROUND(AVG(EXTRACT(EPOCH FROM (i.resolved_at-i.created_at))/86400.0)
        FILTER(WHERE i.issue_type NOT IN ('Epic','Sub-task')
          AND i.resolved_at IS NOT NULL AND {OBSOLETE_SQL}
          AND {mf('i.resolved_at')}), 1) AS prev_lead_time
    FROM issues i
    WHERE i.project_key IN {TEAMS_SQL}
    GROUP BY i.project_key ORDER BY i.project_key
    """
    return fetchall(conn, sql, (yr, m, yr, m, yr, m, yr, m, pyr, pm, pyr, pm, pyr, pm, pyr, pm))


# ── Trend (sparklines) ─────────────────────────────────────────────────────────

def trend_quarterly(conn, metric, n=6):
    """Last n quarters of dept-level metric per team.
    Returns list of {"label": "2026-Q1", "vals": {"STORE": 24.5, ...}}
    """
    if metric in ("velocity", "delivery_pct"):
        sql = f"""
        SELECT pd.project_key AS team,
          EXTRACT(YEAR FROM pd.start_date)::int AS yr,
          EXTRACT(QUARTER FROM pd.start_date)::int AS q,
          {'ROUND(SUM(pd.delivered_points)::numeric/NULLIF(COUNT(DISTINCT pd.sprint_id),0),1)' if metric=='velocity'
           else 'ROUND(100.0*SUM(pd.delivered_points)/NULLIF(SUM(pd.committed_points),0),1)'} AS val
        FROM v_planning_deviation_proj pd
        WHERE pd.state='closed' AND pd.project_key IN {TEAMS_SQL}
        GROUP BY pd.project_key, yr, q ORDER BY yr, q
        """
    elif metric == "lead_time":
        sql = f"""
        SELECT i.project_key AS team,
          EXTRACT(YEAR FROM i.resolved_at)::int AS yr,
          EXTRACT(QUARTER FROM i.resolved_at)::int AS q,
          ROUND(AVG(EXTRACT(EPOCH FROM (i.resolved_at-i.created_at))/86400.0),1) AS val
        FROM issues i
        WHERE i.project_key IN {TEAMS_SQL}
          AND i.issue_type NOT IN ('Epic','Sub-task')
          AND i.resolved_at IS NOT NULL AND {OBSOLETE_SQL}
        GROUP BY i.project_key, yr, q ORDER BY yr, q
        """
    elif metric == "bugs_created":
        sql = f"""
        SELECT i.project_key AS team,
          EXTRACT(YEAR FROM i.created_at)::int AS yr,
          EXTRACT(QUARTER FROM i.created_at)::int AS q,
          COUNT(*)::float AS val
        FROM issues i WHERE i.project_key IN {TEAMS_SQL} AND i.issue_type='Bug'
        GROUP BY i.project_key, yr, q ORDER BY yr, q
        """
    elif metric == "bugs_resolved":
        sql = f"""
        SELECT i.project_key AS team,
          EXTRACT(YEAR FROM i.resolved_at)::int AS yr,
          EXTRACT(QUARTER FROM i.resolved_at)::int AS q,
          COUNT(*)::float AS val
        FROM issues i WHERE i.project_key IN {TEAMS_SQL}
          AND i.issue_type='Bug' AND i.resolved_at IS NOT NULL
        GROUP BY i.project_key, yr, q ORDER BY yr, q
        """
    else:  # issues_resolved
        sql = f"""
        SELECT i.project_key AS team,
          EXTRACT(YEAR FROM i.resolved_at)::int AS yr,
          EXTRACT(QUARTER FROM i.resolved_at)::int AS q,
          COUNT(*)::float AS val
        FROM issues i WHERE i.project_key IN {TEAMS_SQL}
          AND i.issue_type NOT IN ('Epic','Sub-task')
          AND i.resolved_at IS NOT NULL AND {OBSOLETE_SQL}
        GROUP BY i.project_key, yr, q ORDER BY yr, q
        """

    rows = fetchall(conn, sql)
    quarters = sorted(set((r["yr"], r["q"]) for r in rows))[-n:]
    result = []
    for yr, q in quarters:
        vals = {r["team"]: float(r["val"]) for r in rows if r["yr"] == yr and r["q"] == q}
        result.append({"label": f"{yr}-Q{int(q)}", "vals": vals})
    return result


# Release Quality ──────────────────────────────────────────────────────────────

def release_quality(conn, project_key=None, released_only=False, limit=20,
                    release_name=None):
    """Per-release metrics: issue counts, bug rate, open issues, overdue status.

    release_name does a case-insensitive substring match on the fix version
    name, so "Bose QCE 3.0.3", "QCE 3.0.3", or "3.0.3" all resolve.
    """
    conditions = ["r.archived = FALSE"]
    params = []
    if project_key:
        conditions.append("r.project_key = %s")
        params.append(project_key)
    if release_name:
        conditions.append("r.name ILIKE %s")
        params.append(f"%{release_name}%")
    if released_only:
        conditions.append("r.released = TRUE")
    where = " AND ".join(conditions)
    params.append(limit)
    sql = f"""
    SELECT
        r.project_key                                                       AS team,
        r.name                                                              AS release,
        r.release_date,
        r.released,
        COUNT(i.key)                                                        AS total_issues,
        COUNT(i.key) FILTER (WHERE i.issue_type = 'Bug')                   AS bug_count,
        COUNT(i.key) FILTER (
            WHERE i.issue_type NOT IN ('Bug','Epic','Sub-task')
        )                                                                   AS story_count,
        ROUND(
            100.0 * COUNT(i.key) FILTER (WHERE i.issue_type = 'Bug')
            / NULLIF(COUNT(i.key) FILTER (
                WHERE i.issue_type NOT IN ('Epic','Sub-task')
            ), 0), 1
        )                                                                   AS bug_pct,
        COUNT(i.key) FILTER (
            WHERE i.resolved_at IS NOT NULL
              AND i.issue_type NOT IN ('Epic','Sub-task')
              AND {OBSOLETE_SQL}
        )                                                                   AS resolved_issues,
        COUNT(i.key) FILTER (
            WHERE i.resolved_at IS NULL
              AND i.issue_type NOT IN ('Epic','Sub-task')
              AND {OBSOLETE_SQL}
        )                                                                   AS open_issues,
        COUNT(i.key) FILTER (
            WHERE i.issue_type = 'Bug'
              AND r.released = TRUE
              AND r.release_date IS NOT NULL
              AND i.resolved_at > r.release_date
        )                                                                   AS bugs_after_release,
        CASE
            WHEN NOT r.released
              AND r.release_date IS NOT NULL
              AND r.release_date < CURRENT_DATE
            THEN TRUE ELSE FALSE
        END                                                                 AS is_overdue,
        CASE
            WHEN NOT r.released AND r.release_date IS NOT NULL
            THEN (r.release_date - CURRENT_DATE)::integer
            ELSE NULL
        END                                                                 AS days_until_release
    FROM releases r
    LEFT JOIN issues i
        ON r.name = ANY(i.fix_versions)
       AND i.project_key = r.project_key
    WHERE {where}
    GROUP BY r.id, r.project_key, r.name, r.release_date, r.released
    ORDER BY r.release_date DESC NULLS LAST, r.project_key
    LIMIT %s
    """
    return fetchall(conn, sql, tuple(params))
