"""All DB queries for report generation."""

import os
import psycopg2
import psycopg2.extras

TEAMS = ("STORE", "AAONE", "AATWO", "CONNECT")
TEAMS_SQL = "('STORE','AAONE','AATWO','CONNECT')"
OBSOLETE_SQL = "LOWER(i.status) NOT IN ('obsolete','won''t do','obsolete / won''t do','obsolete / won')"

SPRINT_PROJ_CTE = """
sprint_proj AS (
  SELECT si.sprint_id, i.project_key, COUNT(*) AS cnt
  FROM sprint_issues si JOIN issues i ON i.key = si.issue_key
  WHERE i.project_key IN ('STORE','AAONE','AATWO','CONNECT')
  GROUP BY si.sprint_id, i.project_key
),
primary_proj AS (
  SELECT DISTINCT ON (sprint_id) sprint_id, project_key
  FROM sprint_proj ORDER BY sprint_id, cnt DESC
)"""


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
    WITH {SPRINT_PROJ_CTE},
    sprint_committed AS (
      SELECT sprint_id, SUM(COALESCE(story_points,0)) AS sp
      FROM sprint_scope_final WHERE was_punted = FALSE GROUP BY sprint_id
    ),
    sprint_delivered AS (
      SELECT sprint_id, SUM(COALESCE(story_points,0)) AS sp
      FROM sprint_scope_final WHERE was_completed = TRUE GROUP BY sprint_id
    ),
    cur AS (
      SELECT pp.project_key,
        COUNT(DISTINCT s.id) AS nsprints,
        SUM(COALESCE(sd.sp,0)) AS sp_del,
        SUM(COALESCE(sc.sp,0)) AS sp_com
      FROM sprints s JOIN primary_proj pp ON pp.sprint_id = s.id
      LEFT JOIN sprint_committed sc ON sc.sprint_id = s.id
      LEFT JOIN sprint_delivered sd ON sd.sprint_id = s.id
      WHERE s.state='closed' AND {qf('s.start_date')}
      GROUP BY pp.project_key
    ),
    prv AS (
      SELECT pp.project_key,
        COUNT(DISTINCT s.id) AS nsprints,
        SUM(COALESCE(sd.sp,0)) AS sp_del,
        SUM(COALESCE(sc.sp,0)) AS sp_com
      FROM sprints s JOIN primary_proj pp ON pp.sprint_id = s.id
      LEFT JOIN sprint_committed sc ON sc.sprint_id = s.id
      LEFT JOIN sprint_delivered sd ON sd.sprint_id = s.id
      WHERE s.state='closed' AND {qf('s.start_date')}
      GROUP BY pp.project_key
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
    WITH {SPRINT_PROJ_CTE},
    scope_agg AS (
      SELECT pp.project_key,
        SUM(added) FILTER(WHERE {qf('s.start_date')}) AS added,
        SUM(initial) FILTER(WHERE {qf('s.start_date')}) AS initial,
        SUM(added) FILTER(WHERE {qf('s.start_date')}) AS prev_added,
        SUM(initial) FILTER(WHERE {qf('s.start_date')}) AS prev_initial
      FROM sprints s JOIN primary_proj pp ON pp.sprint_id = s.id
      JOIN (
        SELECT sprint_id,
          COUNT(*) FILTER(WHERE was_in_initial_scope=FALSE AND removed_at IS NULL) AS added,
          COUNT(*) FILTER(WHERE was_in_initial_scope=TRUE) AS initial
        FROM sprint_issues GROUP BY sprint_id
      ) si ON si.sprint_id = s.id
      WHERE s.state='closed' AND ({qf('s.start_date')} OR {qf('s.start_date')})
      GROUP BY pp.project_key
    )
    SELECT i.project_key AS team,
      COUNT(*) FILTER(WHERE i.issue_type NOT IN ('Epic','Sub-task')
        AND i.resolved_at IS NOT NULL AND {OBSOLETE_SQL}
        AND {qf('i.resolved_at')}) AS issues_resolved,
      COUNT(*) FILTER(WHERE i.issue_type NOT IN ('Epic','Sub-task')
        AND i.resolved_at IS NOT NULL AND {OBSOLETE_SQL}
        AND {qf('i.resolved_at')}) AS prev_issues_resolved,
      ROUND(100.0*sa.added / NULLIF(sa.initial,0), 1) AS scope_change_pct,
      ROUND(100.0*sa.prev_added / NULLIF(sa.prev_initial,0), 1) AS prev_scope_change_pct
    FROM issues i
    LEFT JOIN scope_agg sa ON sa.project_key = i.project_key
    WHERE i.project_key IN {TEAMS_SQL}
    GROUP BY i.project_key, sa.added, sa.initial, sa.prev_added, sa.prev_initial
    ORDER BY i.project_key
    """
    rows = fetchall(conn, sql, (yr, q, pyr, pq, yr, q, pyr, pq, yr, q, pyr, pq))

    if valid:
        rsql = f"""
        SELECT i.project_key AS team,
          ROUND(100.0 * COUNT(*) FILTER(WHERE i.story_points IS NOT NULL
            AND COALESCE(i.has_acceptance_criteria,FALSE)=TRUE AND i.assignee IS NOT NULL
            AND {qf('i.created_at')})
          / NULLIF(COUNT(*) FILTER(WHERE {qf('i.created_at')}), 0), 1) AS readiness_pct,
          ROUND(100.0 * COUNT(*) FILTER(WHERE i.story_points IS NOT NULL
            AND COALESCE(i.has_acceptance_criteria,FALSE)=TRUE AND i.assignee IS NOT NULL
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
        WITH {SPRINT_PROJ_CTE},
        sprint_committed AS (
          SELECT sprint_id, SUM(COALESCE(story_points,0)) AS sp
          FROM sprint_scope_final WHERE was_punted=FALSE GROUP BY sprint_id
        ),
        sprint_delivered AS (
          SELECT sprint_id, SUM(COALESCE(story_points,0)) AS sp
          FROM sprint_scope_final WHERE was_completed=TRUE GROUP BY sprint_id
        )
        SELECT pp.project_key AS team,
          EXTRACT(YEAR FROM s.start_date)::int AS yr,
          EXTRACT(QUARTER FROM s.start_date)::int AS q,
          {'ROUND(SUM(COALESCE(sd.sp,0))::numeric/NULLIF(COUNT(DISTINCT s.id),0),1)' if metric=='velocity'
           else 'ROUND(100.0*SUM(COALESCE(sd.sp,0))/NULLIF(SUM(COALESCE(sc.sp,0)),0),1)'} AS val
        FROM sprints s JOIN primary_proj pp ON pp.sprint_id=s.id
        LEFT JOIN sprint_committed sc ON sc.sprint_id=s.id
        LEFT JOIN sprint_delivered sd ON sd.sprint_id=s.id
        WHERE s.state='closed'
        GROUP BY pp.project_key, yr, q ORDER BY yr, q
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
