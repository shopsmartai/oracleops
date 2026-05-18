---
name: awr-summary-now
description: Summarize the most recent AWR snapshot in plain English. Surfaces top wait events, top SQL by elapsed time, DB time consumption, host CPU, and the foreground/background event split. Flags anything that crossed a warning threshold versus the prior hour.
when_to_use: |
  User asks "what's happening", "any spike", "is the database OK", or wants
  the morning health check. Also runs on a scheduled cron job for the daily
  briefing. Always callable when the user has not specified a particular
  SQL — this skill picks the suspects, diagnose-slow-query handles each one.
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

# awr-summary-now

## When to Use

Open-ended health check. Trigger on:

- "What's happening with the DB right now?"
- "Any spike in the last hour?"
- Scheduled morning brief at 8 AM
- Pre-incident "is this normal?" check before a deploy or migration
- Right after a user reported app-side slowness, before deep-diving into a specific SQL

Do not use this for a known SQL_ID or named query. Use `diagnose-slow-query` for that.

## Procedure

### Step 1: Confirm Diagnostic Pack is licensed

```sql
SELECT value FROM v$option WHERE parameter = 'Diagnostic Pack';
```

If `value` is not `TRUE`, AWR is not available. Inform the user and fall back to `v$session` and `v$sysmetric` for a live snapshot instead of historical AWR.

### Step 2: Pick the snapshot window

Default to the most recent two snapshots. Default AWR snap interval is 60 minutes, so this gives you a one-hour window. The user can override with a phrase like "last 4 hours" or "since 9am" — parse the offset and find snapshots accordingly.

```sql
SELECT snap_id, begin_interval_time, end_interval_time
  FROM dba_hist_snapshot
 WHERE dbid = (SELECT dbid FROM v$database)
   AND instance_number = (SELECT instance_number FROM v$instance)
 ORDER BY snap_id DESC
 FETCH FIRST 4 ROWS ONLY;
```

### Step 3: Pull the AWR diff using built-in views

Use the `dba_hist_*` views rather than the AWR HTML report — the agent needs structured data, not formatted HTML. Five queries cover 90% of what a senior DBA reads in an AWR report:

**Top 5 wait events by time waited:**
```sql
SELECT event_name, total_waits, time_waited_micro / 1e6 AS time_waited_seconds,
       ROUND(time_waited_micro / NULLIF(total_waits, 0) / 1000, 2) AS avg_wait_ms,
       wait_class
  FROM (
    SELECT event_name, wait_class,
           SUM(total_waits_delta) AS total_waits,
           SUM(time_waited_micro_delta) AS time_waited_micro
      FROM dba_hist_system_event
     WHERE snap_id BETWEEN :start_snap AND :end_snap
       AND dbid = (SELECT dbid FROM v$database)
       AND wait_class != 'Idle'
     GROUP BY event_name, wait_class
  )
 ORDER BY time_waited_micro DESC
 FETCH FIRST 5 ROWS ONLY;
```

**Top 5 SQL by elapsed time:**
```sql
SELECT sql_id, plan_hash_value,
       SUM(elapsed_time_delta) / 1e6 AS elapsed_seconds,
       SUM(executions_delta) AS executions,
       ROUND(SUM(elapsed_time_delta) / NULLIF(SUM(executions_delta), 0) / 1000, 2) AS ms_per_exec,
       SUM(buffer_gets_delta) AS buffer_gets,
       SUM(disk_reads_delta) AS disk_reads
  FROM dba_hist_sqlstat
 WHERE snap_id BETWEEN :start_snap AND :end_snap
   AND dbid = (SELECT dbid FROM v$database)
 GROUP BY sql_id, plan_hash_value
 ORDER BY elapsed_seconds DESC
 FETCH FIRST 5 ROWS ONLY;
```

**DB time vs CPU time:**
```sql
SELECT stat_name, SUM(value_delta) AS value_seconds
  FROM (
    SELECT stat_name, value - LAG(value) OVER (PARTITION BY stat_name ORDER BY snap_id) AS value_delta
      FROM dba_hist_sys_time_model
     WHERE snap_id BETWEEN :start_snap AND :end_snap
       AND stat_name IN ('DB time', 'DB CPU', 'sql execute elapsed time', 'parse time elapsed', 'PL/SQL execution elapsed time')
  )
 WHERE value_delta IS NOT NULL
 GROUP BY stat_name
 ORDER BY value_seconds DESC;
```

**Host CPU pressure:**
```sql
SELECT MAX(value) AS max_cpu_pct, AVG(value) AS avg_cpu_pct
  FROM dba_hist_osstat
 WHERE snap_id BETWEEN :start_snap AND :end_snap
   AND stat_name = 'BUSY_TIME'
   AND dbid = (SELECT dbid FROM v$database);
```

**Foreground vs background split:**
```sql
SELECT class_name, SUM(time_waited_micro_delta) / 1e6 AS time_waited_seconds
  FROM dba_hist_system_event
 WHERE snap_id BETWEEN :start_snap AND :end_snap
 GROUP BY class_name
 ORDER BY time_waited_seconds DESC;
```

### Step 4: Compare against the prior window

Run the same five queries against the snapshot window *before* the requested one. The diff (latest hour vs prior hour) is what makes the summary useful. A 200 ms `db file sequential read` average wait is only alarming if last hour it was 8 ms.

### Step 5: Apply thresholds

Use these conservative defaults for "worth mentioning" thresholds. Override if MEMORY has site-specific thresholds for this DB:

| Metric | Mention if |
|---|---|
| Top wait event share of DB time | > 25% |
| Average `db file sequential read` | > 20 ms (SSD) or > 50 ms (HDD) |
| Average `log file sync` | > 20 ms |
| `enq:` waits (any kind) | Present in top 5 |
| Host CPU peak | > 80% |
| `parse time elapsed` / `DB time` | > 5% |
| Hard parses | > 100/sec |
| Hour-over-hour change in any top SQL elapsed time | > 50% |

### Step 6: Output structure

```
AWR SUMMARY — <db_name> — <start_time> to <end_time>

HEADLINE
<one sentence: either "all clear" or "X is the dominant issue">

DB TIME BREAKDOWN
- DB CPU: <n>s (<pct>%)
- SQL execute: <n>s
- Parse: <n>s
- PL/SQL: <n>s

TOP WAIT EVENTS
1. <event> — <n>s (<pct>% of DB time, <delta> vs prior hour) — <wait_class>
2. ...
5. ...

TOP SQL BY ELAPSED TIME
1. SQL_ID <id> — <ms_per_exec> ms/exec × <n> executions — <delta>% vs prior
   [first 80 chars of sql_text]
2. ...
5. ...

HOST CPU
Peak: <n>%, Avg: <n>%

ANYTHING ANOMALOUS?
<bullet list of metrics that crossed threshold, or "nothing crossed a warning threshold">

SUGGESTED NEXT STEPS
<bullet list of skill calls to drill in, or "monitor only">
```

### Step 7: If something is on fire, escalate

If any of these are true, do not wait for the user to ask — recommend running the next skill immediately:

- `enq: TX - row lock contention` is the top wait event → next skill: `find-lock-contention`
- A single SQL_ID consumes > 40% of DB time → next skill: `diagnose-slow-query` for that SQL_ID
- `log file sync` average > 50 ms → next skill: investigate redo (not yet a separate skill; surface raw findings)
- Host CPU peaked at 100% AND a SQL_ID is doing > 1e9 buffer gets → next skill: `diagnose-slow-query`

## Pitfalls

- **Diagnostic Pack licensing.** AWR views are licensed. Do not query them without confirming `v$option`. Falling back to `v$sysmetric` and `v$session_event` gives a live (not historical) view without the licensing concern.
- **Snap interval too long.** If the user wants "the last 15 minutes" but snaps are 60 minutes apart, you cannot answer with AWR. Use `v$active_session_history` (ASH) instead — it is licensed under Diagnostic Pack too but samples every second.
- **RAC.** On a Real Application Clusters database, AWR is per-instance. The user might be asking about the cluster, not just node 1. Loop over all instances using `gv$instance` and aggregate.
- **Snap miss.** Sometimes AWR snaps fail silently (e.g., during maintenance windows). Check `dba_hist_snapshot.error_count` — if non-zero, mention "snapshot N had errors, summary may be incomplete."
- **`db file sequential read` is not always bad.** On a transaction-heavy OLTP database, single-block reads at 5 ms average are normal. The threshold of 20 ms is for "worth investigating," not "definitely broken."
- **The "top SQL" list can lie.** A query that ran 1,000,000 times in 0.1 ms each will dominate by total elapsed time but is not a tuning target. Always show `ms_per_exec` alongside total — the user needs to see whether to fix the SQL or fix the calling pattern.

## Verification

After the user takes action (re-runs a fix), call this skill again with the same window. Confirm:

1. The wait event that was dominant is no longer #1.
2. The top SQL by elapsed time has moved from rank 1 toward rank 5 or below.
3. The dominant wait class has shifted from `User I/O` / `Concurrency` / `Application` toward `Idle` or `CPU`.
4. DB time per second has dropped.

If none of those happened, the fix did not work and the user should re-engage `diagnose-slow-query` on the still-top SQL.

Capture into MEMORY:
```
On <date>, awr-summary at <time>: dominant issue was <event>, fixed by <action>.
Result: dominant event after fix was <event>, db time / sec dropped from <n> to <n>.
```
