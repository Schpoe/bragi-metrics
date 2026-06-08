# Plan — BambooHR Absences → Team Capacity KPI

Status: **Implemented — pending deploy & live verification** · Owner: Benjamin · Created: 2026-06-08

> Decisions taken: roster from Jira assignees (a); ratio = Option A velocity-scaled;
> link by email (robust, `assignee_account_id`/`assignee_email` added + backfilled);
> history window `2026-01-01`. PII: name + work email + absence dates/type stored.
> Code landed: `init.sql` (columns, `bamboohr_employees`, `absences`, `v_team_capacity`),
> `jira-sync/bamboohr.py`, `sync_absences()` + `backfill_assignee_identity()` in `sync.py`,
> 3 panels in `po-kpis.json`, env wiring, `test_bamboohr.py`. Awaiting server deploy + the
> Task-4/5 hand-checks before flipping the Confluence Capacity status to LIVE.

Brings BambooHR absence data into the metrics platform to unlock the **Capacity** KPI
(PO Competence Model, Core KPI, target ratio **0.8–1.2** = over/under committed). Today the
platform has no availability data; this plan adds it using the same pattern as the existing
Jira sync.

Related: [PO KPI Dashboard — Measurable KPI Set](https://bragi.atlassian.net/wiki/spaces/PM1/pages/5189894145),
[metrics-reference.md](metrics-reference.md).

---

## 1. Goal & success criteria

- **G1** Approved absences (time off + company holidays) from BambooHR land in PostgreSQL, refreshed on the existing sync cadence.
- **G2** BambooHR people are linked to Jira users so absences can be attributed to a team.
- **G3** A `v_team_capacity` view yields, per sprint, **available person-days** for the team.
- **G4** A Grafana panel shows the Capacity ratio with the 0.8–1.2 target band.

Success = G1–G3 produce non-null numbers for the latest closed sprints, and G4 renders the ratio
matching a hand-checked sample sprint.

---

## 2. Data source — BambooHR API

- **Auth:** HTTP Basic. Username = API key, password = literal `x`.
- **Base:** `https://{BAMBOOHR_SUBDOMAIN}.bamboohr.com/api/v1/`
- **Rate limits:** requests may be throttled; on `503` honour the `Retry-After` header and back off (same defensive style as the Jira client).

### Endpoints used

| Purpose | Method / path | Notes |
|---|---|---|
| Who's Out (absences + holidays) | `GET /time_off/whos_out/?start=YYYY-MM-DD&end=YYYY-MM-DD` | Returns a list sorted by date. Item fields: `id`, `type` (`timeOff` \| `holiday`), `employeeId` (absent for holidays), `name`, `start`, `end`. Inclusive date range. |
| Employee directory | `GET /employees/directory` | Fields per employee: `id`, `displayName`, `firstName`, `lastName`, `workEmail`, plus others. Used to map `employeeId` → email. |

**Why Who's Out (not time-off requests):** it already merges approved time-off *and* company
holidays into one date-ranged feed, which is exactly the "nominal availability minus
absences/holidays" the KPI definition calls for. No status filtering or policy math needed.

**Window:** pull `whos_out` from `JIRA_HISTORY_START` (or a `BAMBOOHR_HISTORY_START`) through
`today + 90d`. Re-pull a rolling window each run; upserts make it idempotent.

---

## 3. Schema (add to [init.sql](../init.sql))

Same upsert conventions as `issues` / `sprints`.

```sql
-- People from BambooHR, mapped to Jira where possible.
CREATE TABLE IF NOT EXISTS bamboohr_employees (
    employee_id      INTEGER PRIMARY KEY,      -- BambooHR id
    display_name     TEXT,
    work_email       TEXT,
    jira_account_id  TEXT,                      -- resolved via email match; NULL if unmatched
    updated_at       TIMESTAMPTZ DEFAULT NOW()
);

-- One row per absence span (time off or holiday).
CREATE TABLE IF NOT EXISTS absences (
    id           BIGINT PRIMARY KEY,            -- BambooHR item id
    employee_id  INTEGER,                       -- NULL for company-wide holidays
    kind         TEXT NOT NULL,                 -- 'timeOff' | 'holiday'
    start_date   DATE NOT NULL,
    end_date     DATE NOT NULL,
    label        TEXT,                          -- 'name' field
    synced_at    TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_absences_dates ON absences (start_date, end_date);
CREATE INDEX IF NOT EXISTS idx_absences_emp   ON absences (employee_id);
```

Holidays (`employee_id IS NULL`) apply to the whole team; per-person time off attributes to
one person via `employee_id → bamboohr_employees.jira_account_id`.

---

## 4. Roster — derived from Jira assignees (decision a)

No team-membership table. A team's roster for a sprint = the distinct Jira assignees on that
sprint's committed issues.

```sql
-- roster(sprint) = distinct assignees on the sprint's initial-scope issues
SELECT DISTINCT i.assignee_account_id
FROM sprint_issues si
JOIN issues i ON i.key = si.issue_key
WHERE si.sprint_id = $sprint
  AND si.was_in_initial_scope = TRUE
  AND i.assignee_account_id IS NOT NULL
```

- **Pro:** zero extra data, auto-updates as teams change.
- **Caveat:** someone fully absent the whole sprint may have *no* assigned issues and thus be
  invisible to the roster — understating absence. Mitigation noted in §8; acceptable for v1
  because the KPI is a coarse over/under signal, not payroll.
- Requires `issues.assignee_account_id` to exist. **Verify** it is synced; if only display name
  is stored, add the account-id column to the issue sync first (Task 0).

---

## 5. Sync module (extend [sync.py](../jira-sync/sync.py))

Reuse the existing `jira-sync` container, cron (07:00 / 19:00 UTC), and `sync_log`. No new service.

Add a `sync_absences(conn)` step, called from `main()` alongside the other steps (wrapped in the
same `try/except → errors.append` so a BambooHR outage never breaks the Jira sync):

1. `GET /employees/directory` → upsert `bamboohr_employees` (`ON CONFLICT (employee_id) DO UPDATE`).
2. Resolve `jira_account_id`: match `work_email` to a Jira user. Reuse the Jira user lookup the
   sync already performs, or match against emails already on `issues`. Log unmatched employees
   (like the existing QA-field gap logging).
3. `GET /time_off/whos_out` for the rolling window → upsert `absences` (`ON CONFLICT (id) DO UPDATE`).
4. Delete `absences` rows whose `id` is no longer returned within the pulled window (handles
   cancelled requests) — scope the delete to the pulled date window only.

New env vars (add to [.env.example](../.env.example) and the `jira-sync` service in
[docker-compose.yml](../docker-compose.yml)): `BAMBOOHR_SUBDOMAIN`, `BAMBOOHR_API_KEY`,
optional `BAMBOOHR_HISTORY_START`. Also add `BAMBOOHR_` to the `printenv` filter in
[entrypoint.sh](../jira-sync/entrypoint.sh) so cron sees them.

---

## 6. Capacity calculation & view

### Available person-days per sprint

```text
working_days(sprint)      = business days in [start_date, end_date]   -- e.g. 10 for a 2-week sprint
roster                    = distinct assignees on committed issues (see §4)
nominal_days              = |roster| * working_days
absence_days              = Σ over roster of (business days each person is out within the sprint window)
                          + (company holidays within window) * |roster|
available_days            = nominal_days - absence_days
```

A `v_team_capacity` view materialises `committed_points`, `nominal_days`, `absence_days`,
`available_days`, and the roster size per sprint, project-majority filtered to a team.

### The ratio (units gap) — decision needed

Confluence Capacity = **committed work vs capacity**, target 0.8–1.2. Commitment is in story
points; availability is in person-days. Three ways to bridge:

| Option | Formula | Trade-off |
|---|---|---|
| **A — velocity-scaled (recommended)** | `ratio = committed_SP / (avg_velocity * available_days / avg_available_days)` | Reuses `avg_velocity` already computed in Sprint Detail. No absolute SP/day constant. Expected output scales the team's *normal* velocity by how staffed the sprint is (80% staffed → expect 0.8× velocity). |
| B — absolute SP/day | `ratio = committed_SP / (available_days * SP_per_day)` | Needs a configured/derived `SP_per_day`. More assumptions, more drift. |
| C — availability only | report `available_days / nominal_days` as a staffing % | Avoids SP entirely but no longer means the Confluence 0.8–1.2 commit ratio; pair with the existing Velocity-Booked panel. |

**Recommendation: Option A.** It needs only `available_days` from BambooHR plus the existing
velocity, and the 0.8–1.2 band keeps its original meaning. Final pick is a sign-off item before
building §7.

---

## 7. Grafana panel (extend [po-kpis.json](../grafana/provisioning/dashboards/po-kpis.json))

- New stat panel **Capacity Ratio** sourced from `v_team_capacity`, project + quarter filtered.
- Threshold band: green 0.8–1.2, yellow just outside, red far outside (mirrors the gauge style
  already used for Delivery %).
- Add a per-sprint bar (Committed vs Available-equivalent) next to the existing velocity charts.
- Re-export per [README.md](../README.md) §"incorporate layout changes" (Share → Export → save
  over the provisioned JSON; `allowUiUpdates:false`).

---

## 8. Risks & edge cases

- **Email mismatch** between BambooHR `workEmail` and Jira → unmatched person, absence dropped.
  Mitigate: log unmatched, surface a small "unmapped employees" count; allow a manual override map later.
- **Fully-absent person not in roster** (no assigned issues) → absence undercounted (§4). v1 accepts it.
- **Part-day absences** — Who's Out is date-granular; treat any listed day as a full day off for v1.
- **Holidays double-counting** with weekends — only count business days within the window.
- **BambooHR throttling** — honour `Retry-After`; isolate failures from the Jira sync.
- **PII / GDPR** — only store name, work email, absence *dates* and type. No medical/leave reason.
  Confirm this is acceptable; the `label` field could carry a leave reason — store a coarse `kind`
  only, not free-text reason, if policy requires.

---

## 9. Out of scope (v1)

- Team Resource Allocation and Planning-ahead KPIs (same data could feed them later).
- Per-domain capacity split (only team-level for v1).
- Manual team-roster override table.

---

## 10. Task breakdown

Each task lists its verification.

- **Task 0 — Precondition: Jira account id on issues.**
  Confirm `issues.assignee_account_id` is synced. If absent, add the column + populate in the
  issue sync. *Verify:* `SELECT COUNT(*) FROM issues WHERE assignee_account_id IS NOT NULL > 0`.

- **Task 1 — Schema.** Add `bamboohr_employees`, `absences`, indexes to `init.sql`.
  *Verify:* tables exist after `docker compose up`; idempotent re-run is clean.

- **Task 2 — BambooHR client.** New module/functions: basic-auth session, `whos_out(start,end)`,
  `directory()`, 503/`Retry-After` handling.
  *Verify:* unit test against a recorded JSON fixture; live smoke call returns rows.

- **Task 3 — `sync_absences(conn)`.** Directory upsert → email→Jira match → whos_out upsert →
  stale-row prune. Wire into `main()` with isolated `try/except`. Add env vars + entrypoint filter.
  *Verify:* after a run, `absences` and `bamboohr_employees` populate; `sync_log` still succeeds;
  unmatched employees logged.

- **Task 4 — Capacity view.** `v_team_capacity` with nominal/absence/available days + roster size.
  *Verify:* hand-check one sprint's available_days against BambooHR Who's Out for that window.

- **Task 5 — Ratio.** Implement the signed-off option (A recommended) in the view/panel query.
  *Verify:* a known over-committed sprint reads > 1.2; a holiday-heavy sprint reads lower.

- **Task 6 — Grafana panel.** Add Capacity Ratio stat + per-sprint bar; re-export JSON.
  *Verify:* panel renders for a sample team/quarter; matches Task 5 numbers.

- **Task 7 — Docs.** Add a Capacity section to `metrics-reference.md`; flip the Confluence page's
  Capacity status from roadmap to LIVE.
  *Verify:* reference doc describes the exact formula chosen.

---

## 11. Open decisions (need sign-off before coding)

1. **Ratio method** — confirm Option A (velocity-scaled) vs B/C (§6).
2. **PII scope** — confirm storing name + work email + absence dates/type is acceptable; whether to drop `label` (§8).
3. **History window** — how far back to pull Who's Out (`BAMBOOHR_HISTORY_START`).
