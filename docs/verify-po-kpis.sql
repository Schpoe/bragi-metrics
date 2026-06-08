-- Verify the 6 recommended PO KPIs return real data.
-- Run on the server:
--   docker compose exec -T postgres psql -U metrics -d jira_metrics -f - < docs/verify-po-kpis.sql
-- All-projects, latest quarter that has closed sprints. Non-null / non-zero = panel has data.

\echo '== latest quarter with closed sprints =='
SELECT date_trunc('quarter', start_date)::date AS quarter, COUNT(*) closed_sprints
FROM v_planning_deviation WHERE state='closed'
GROUP BY 1 ORDER BY 1 DESC LIMIT 4;

\set q '(SELECT MAX(date_trunc(''quarter'', start_date)::date) FROM v_planning_deviation WHERE state=''closed'')'

\echo '== 1. Planning Accuracy % (target >=80) =='
SELECT ROUND(AVG(delivery_pct)::numeric,1) AS planning_accuracy_pct
FROM v_planning_deviation
WHERE state='closed' AND delivery_pct IS NOT NULL
  AND date_trunc('quarter', start_date) = :q;

\echo '== 2. Scope Change % (target <=10) =='
SELECT ROUND(AVG(100.0*(COALESCE(a.sp,0)+COALESCE(r.sp,0))/NULLIF(pd.committed_points,0))::numeric,1) AS scope_change_pct
FROM v_planning_deviation pd
LEFT JOIN (SELECT sprint_id, SUM(COALESCE(si.story_points_at_add,0)) sp FROM sprint_issues si JOIN issues i ON i.key=si.issue_key
  WHERE si.was_in_initial_scope=FALSE AND si.removed_at IS NULL AND i.issue_type NOT IN ('Epic','Sub-task') AND i.status!='Obsolete / Won''t Do' GROUP BY si.sprint_id) a ON a.sprint_id=pd.sprint_id
LEFT JOIN (SELECT sprint_id, SUM(COALESCE(si.story_points_at_add,0)) sp FROM sprint_issues si JOIN issues i ON i.key=si.issue_key
  WHERE si.removed_at IS NOT NULL AND i.issue_type NOT IN ('Epic','Sub-task') AND i.status!='Obsolete / Won''t Do' GROUP BY si.sprint_id) r ON r.sprint_id=pd.sprint_id
WHERE pd.state='closed' AND pd.committed_points>0 AND date_trunc('quarter', pd.start_date) = :q;

\echo '== 3. Story Readiness / DOR Rate % (target >=90) =='
WITH d AS (
  SELECT si.sprint_id, COUNT(*) total,
    COUNT(*) FILTER (WHERE i.assignee IS NOT NULL AND i.qa_assignee IS NOT NULL
      AND COALESCE(si.story_points_at_add,i.story_points,0)>0 AND i.has_acceptance_criteria=TRUE
      AND i.epic_key IS NOT NULL AND cardinality(i.components)>0) ready
  FROM sprint_issues si JOIN issues i ON i.key=si.issue_key JOIN sprints s ON s.id=si.sprint_id
  WHERE i.issue_type NOT IN ('Epic','Sub-task','Bug') AND si.was_in_initial_scope=TRUE AND si.removed_at IS NULL
    AND s.state='closed' AND date_trunc('quarter', s.start_date) = :q
  GROUP BY si.sprint_id HAVING COUNT(*)>0)
SELECT ROUND(AVG(100.0*ready/NULLIF(total,0))::numeric,1) AS dor_rate_pct FROM d;

\echo '== 4. PROD Item Achievement / Avg Completion % (target >=90) =='
SELECT COALESCE(ROUND(AVG(completion_pct_issues),1),0) AS prod_completion_pct
FROM v_prod_item_progress
WHERE prod_key IN (SELECT DISTINCT ep.prod_key FROM v_prod_epic_progress ep JOIN issues i ON i.key=ep.epic_key
  WHERE date_trunc('quarter', i.created_at) = :q);

\echo '== 5. Ticket Reopens / Quality of Work (target low) =='
SELECT COUNT(DISTINCT t.issue_key) AS ticket_reopens
FROM issue_transitions t
WHERE LOWER(t.from_status) LIKE '%test%' AND LOWER(t.to_status) LIKE '%progress%'
  AND date_trunc('quarter', t.transitioned_at) = :q;

\echo '== 6. Release Quality Score (lower better; weighted bugs/release) =='
SELECT ROUND(COALESCE(SUM(CASE i.priority WHEN 'Blocker' THEN 5 WHEN 'Critical' THEN 4 WHEN 'High' THEN 3 WHEN 'Medium' THEN 2 WHEN 'Low' THEN 1 ELSE 0 END),0)::numeric
  / NULLIF((SELECT COUNT(DISTINCT r2.id) FROM releases r2 WHERE r2.released=TRUE AND date_trunc('quarter', r2.release_date) = :q),0),1) AS release_quality_score
FROM releases r JOIN issues i ON r.name=ANY(i.fix_versions) AND r.project_key=i.project_key
WHERE i.issue_type='Bug' AND r.released=TRUE AND date_trunc('quarter', r.release_date) = :q;
