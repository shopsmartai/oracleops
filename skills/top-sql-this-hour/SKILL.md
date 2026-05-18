---
name: top-sql-this-hour
description: Return the most expensive SQL by elapsed time in the most recent window. Reports SQL_ID, plan hash, total elapsed seconds, executions, ms-per-execution, buffer gets, disk reads, and the first 200 characters of SQL text. Foundation for almost every other diagnostic skill.
when_to_use: |
  User asks "what's eating the database", "what's slow right now", "what
  queries should I tune", "show me the top SQL". Also called by the
  orchestrator when a generic complaint comes in and we need to surface
  candidate SQL to drill into.
requires_toolsets:
  - oracle_db
required_environment_variables:
  - ORACLE_USER
  - ORACLE_PASSWORD
  - ORACLE_DSN
metadata:
  hermes:
    config:
      auto_load: false
      auto_confirm: true
      cost_estimate: low
---

# top-sql-this-hour

## When to Use

The shortest path from "the database is slow" to "here are the suspects." Call this skill when:

- The user is generic: "what's slow", "show me top SQL", "why is the DB busy"
- The orchestrator needs a candidate list before drilling into individual SQL with `diagnose-slow-query`
- The user asks for a particular *window* (last hour, today, since 9am) and a list of expensive SQL across it
- Pre-incident sanity check before a deploy or DDL change ("what's running heavily right now?")

Do not use this if the user has already named a specific SQL_ID or query — go directly to `diagnose-slow-query` for that.

## Procedure

### Step 1: Determine the window

Default to the last 60 minutes via `v$active_session_history` (ASH) — this works without the Diagnostic Pack license check because we use the in-memory view first.

If the user says:
- "last hour" → 60 min
- "today" → since 00:00 in DB timezone
- "since 9am" → parse the time, calculate offset
- "last N hours" → use N
- "right now" → last 5 min from ASH

For windows longer than 1 hour, fall back to AWR (`dba_hist_sqlstat`) — but check Diagnostic Pack first:

```sql
SELECT value FROM v$option WHERE parameter = 'Diagnostic Pack';
```

If not licensed, cap the window at 1 hour and inform the user.

### Step 2: Query the right source

**Window ≤ 60 min — use ASH (always available):**

```sql
WITH ash_window AS (
  SELECT sql_id,
         COUNT(*) AS active_samples,
         SUM(time_waited) / 1e6 AS total_wait_seconds
    FROM v$active_session_history
   WHERE sample_time >= SYSTIMESTAMP - INTERVAL '60' MINUTE
     AND sql_id IS NOT NULL
   GROUP BY sql_id
)
SELECT a.sql_id,
       a.active_samples,
       ROUND(a.total_wait_seconds, 2) AS wait_seconds,
       s.executions,
       ROUND(s.elapsed_time / NULLIF(s.executions, 0) / 1000, 2) AS ms_per_exec,
       s.buffer_gets,
       s.disk_reads,
       s.plan_hash_value,
       SUBSTR(s.sql_fulltext, 1, 200) AS sql_text_preview
  FROM ash_window a
  LEFT JOIN v$sql s ON s.sql_id = a.sql_id
 ORDER BY a.active_samples DESC
 FETCH FIRST 10 ROWS ONLY;
```

ASH samples once per second per active session, so `active_samples` directly proxies DB time consumed.

**Window > 60 min — use AWR:**

```sql
WITH bounds AS (
  SELECT MIN(snap_id) AS start_snap, MAX(snap_id) AS end_snap
    FROM dba_hist_snapshot
   WHERE begin_interval_time >= SYSTIMESTAMP - NUMTODSINTERVAL(:hours, 'HOUR')
     AND dbid = (SELECT dbid FROM v$database)
)
SELECT sql_id,
       plan_hash_value,
       SUM(elapsed_time_delta) / 1e6 AS elapsed_seconds,
       SUM(executions_delta) AS executions,
       ROUND(SUM(elapsed_time_delta) / NULLIF(SUM(executions_delta), 0) / 1000, 2) AS ms_per_exec,
       SUM(buffer_gets_delta) AS buffer_gets,
       SUM(disk_reads_delta) AS disk_reads
  FROM dba_hist_sqlstat,
       bounds
 WHERE snap_id BETWEEN bounds.start_snap AND bounds.end_snap
   AND dbid = (SELECT dbid FROM v$database)
 GROUP BY sql_id, plan_hash_value
 ORDER BY elapsed_seconds DESC
 FETCH FIRST 10 ROWS ONLY;
```

For SQL text on AWR results, hit `dba_hist_sqltext`:

```sql
SELECT sql_text FROM dba_hist_sqltext WHERE sql_id = :sql_id;
```

### Step 3: Annotate each row

For each SQL_ID in the top 10, add a quick characterization. The goal is to give the user a hint about which one to drill into first.

- **High `ms_per_exec`, low `executions`** → individual query is slow, candidate for `diagnose-slow-query`
- **Low `ms_per_exec`, very high `executions`** → not a tuning target per execution; tell the user to look at the *caller* (likely an app loop)
- **High `disk_reads` relative to `buffer_gets`** → I/O-bound query, `recommend-index` is the most likely fix
- **`buffer_gets` enormous (> 1e8) with low `disk_reads`** → CPU-bound on cache hits, usually a bad join or runaway access predicate
- **Multiple plan_hash_values for the same SQL_ID** → plan instability, adaptive cursor sharing or stats change in flight

### Step 4: Output structure

```
TOP SQL BY ELAPSED TIME — window: <start> to <end>

# | SQL_ID         | ms/exec | execs    | DB s | gets        | reads   | hint
--+----------------+---------+----------+------+-------------+---------+----------
1 | 4a2g8htg9k7bn  | 12450.2 | 12       | 149  | 8.2e+08     | 1.1e+06 | I/O bound — try recommend-index
2 | 9z3xb1...      | 0.3     | 4.5e+06  | 1350 | 1.2e+09     | 0       | app loop — look at caller
3 | ...            | ...     | ...      | ...  | ...         | ...     | ...

SQL TEXT PREVIEWS
[1] 4a2g8htg9k7bn  (plan 2814927105)
    SELECT * FROM ORDERS WHERE CUSTOMER_ID = :B1 ...
[2] 9z3xb1mqf2t7w  (plan 348190274)
    SELECT 1 FROM DUAL WHERE :B1 IS NOT NULL ...

OBSERVATIONS
- Top consumer (4a2g8htg9k7bn) is 60% of total DB time in the window
- 2 SQLs have multiple plan_hash_values — possible plan instability
- 1 SQL did 8.2e8 buffer gets per execution — suspect bad access path

SUGGESTED NEXT STEPS
- diagnose-slow-query for SQL_ID 4a2g8htg9k7bn
- Investigate caller of 9z3xb1... — execution rate of 75/sec
```

### Step 5: Pass useful context to the orchestrator

Capture the top three SQL_IDs into MEMORY so subsequent skills in the same session know what we're working on:

```
TOP_SQL_<window_start_iso>: [
  {sql_id: 4a2g8htg9k7bn, ms_per_exec: 12450.2, plan: 2814927105},
  {sql_id: 9z3xb1mqf2t7w, ms_per_exec: 0.3, plan: 348190274},
  ...
]
```

The orchestrator skill reads this on the next turn so it can route "tune the top one" without re-querying.

## Pitfalls

- **ASH gives you "active samples", not "elapsed time".** A query that ran 30 seconds but blocked the whole time will show 30 active samples — that's a lock issue, not a SQL tuning issue. Cross-check with `v$session.event`: if blocked queries dominate ASH, route to `find-lock-contention`.
- **`v$sql.executions` resets when the cursor ages out.** A query may show 12 executions but actually have run 12,000 times today. Use `dba_hist_sqlstat` if the cursor cache numbers look implausibly low.
- **Recursive SQL.** Oracle's own background queries (cursor parsing, stats gathering, AWR snap) appear in `v$sql`. Filter them out:
  ```sql
  AND parsing_schema_name != 'SYS'
  ```
  but keep them in the result list if explicitly requested — sometimes the DB IS bottlenecked on SYS internal work.
- **Adaptive plans.** A single SQL_ID can have multiple `plan_hash_value` entries due to adaptive cursor sharing. Showing both is the correct behavior — the user needs to know the plan is unstable.
- **AWR snap timing.** Default snap interval is 60 minutes. A "last 30 minutes" query will likely span just one open snap and one closed snap. AWR delta math will undercount the still-open snap. ASH is the only reliable sub-snap-interval source.
- **`buffer_gets` vs DB time.** A query with billions of buffer gets in microseconds because everything's cached can dominate `buffer_gets` but barely show in DB time. Always rank by DB time first, mention buffer gets only as a hint.

## Verification

After the user acts on the top suggestion (typically `diagnose-slow-query` on rank 1), re-run this skill on the same window. Confirm:

1. The previously rank-1 SQL_ID has moved to rank 3 or below, OR its DB time share has dropped by > 50%.
2. No new SQL_ID has appeared in the top 5 that wasn't there before — sometimes a fix shifts load to a different query.
3. The DB time aggregate for the window has dropped.

If the top SQL is still on top with similar DB time, the previous fix did not take effect — re-engage `diagnose-slow-query` and look for the real bottleneck.

Capture into MEMORY:
```
On <date> window <start>-<end>, top SQL was <id> (<ms> ms/exec, <pct>% of DB time).
After remediation <action>, top SQL is now <id> with <ms> ms/exec.
```
