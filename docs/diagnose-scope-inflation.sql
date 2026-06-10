-- Diagnose mid-sprint-addition inflation in v_planning_deviation (closed sprints).
-- Shows, per closed sprint, how much of committed/delivered came from mid-sprint
-- additions vs initial scope. Before the was_added_mid_sprint fix, the dashboard
-- counted the "mid" columns into committed (→ low Delivery %) and delivered (→ high Velocity).
--
-- Run on the server:
--   docker compose exec -T postgres psql -U metrics -d jira_metrics -f - < docs/diagnose-scope-inflation.sql
--
-- Narrow to a team/quarter by editing the WHERE on s.name / start_date below.

\echo '== committed / delivered split: initial scope vs mid-sprint additions =='
SELECT
    s.name AS sprint,
    date_trunc('quarter', s.start_date)::date AS quarter,
    -- committed (non-punted)
    SUM(ssf.story_points) FILTER (WHERE ssf.was_punted = FALSE AND ssf.was_added_mid_sprint = FALSE) AS committed_initial,
    SUM(ssf.story_points) FILTER (WHERE ssf.was_punted = FALSE AND ssf.was_added_mid_sprint = TRUE)  AS committed_mid_added,
    -- delivered (completed)
    SUM(ssf.story_points) FILTER (WHERE ssf.was_completed = TRUE AND ssf.was_added_mid_sprint = FALSE) AS delivered_initial,
    SUM(ssf.story_points) FILTER (WHERE ssf.was_completed = TRUE AND ssf.was_added_mid_sprint = TRUE)  AS delivered_mid_added
FROM sprint_scope_final ssf
JOIN sprints s ON s.id = ssf.sprint_id
JOIN issues i ON i.key = ssf.issue_key
WHERE i.issue_type NOT IN ('Epic', 'Sub-task')
  AND i.status != 'Obsolete / Won''t Do'
  AND s.name ILIKE '%Sprint 6%'              -- <-- adjust to the sprint(s) you want
  AND date_trunc('quarter', s.start_date) = '2026-01-01'::date
GROUP BY s.name, quarter
ORDER BY s.name;

\echo '== resulting Delivery % from the (now fixed) view =='
SELECT sprint_name, committed_points, delivered_points, delivery_pct
FROM v_planning_deviation
WHERE state = 'closed'
  AND sprint_name ILIKE '%Sprint 6%'
  AND date_trunc('quarter', start_date) = '2026-01-01'::date
ORDER BY sprint_name;
