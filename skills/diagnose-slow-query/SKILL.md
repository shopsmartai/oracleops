---
name: diagnose-slow-query
description: Diagnose why an Oracle SQL query is slow. Pulls execution plan, runtime statistics, and wait events. Identifies the dominant bottleneck (full scan, bad join order, missing index, stale stats, parsing pressure) and proposes the smallest viable fix.
when_to_use: |
  User provides a SQL statement or a SQL_ID and asks why it is slow, what the
  plan looks like, or how to make it faster. Also triggers on phrases like
  "the report is slow", "this query hangs", "explain plan", "tune this".
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

# diagnose-slow-query

## When to Use

Trigger this skill when the user wants to understand or fix a slow Oracle SQL query. Common phrasings:

- "Why is this query slow?"
- "Tune this SELECT for me"
- "What's the plan for SQL_ID `4a2g8htg9k7bn`?"
- "The orders report has been slow since yesterday afternoon"
- A complaint about a specific application page that maps to a known SQL pattern

Do not use this skill for systemic complaints like "the database is slow" or "the server is on fire." For those, run `awr-summary-now` and `find-lock-contention` first, then this skill on whichever individual SQL the AWR top list surfaces.

## Procedure

### Step 1: Get the SQL text

Three input shapes are valid:

1. **Raw SQL** pasted by the user. Use it directly.
2. **SQL_ID** (a 13-character alphanumeric string like `4a2g8htg9k7bn`). Pull the text:
   ```sql
   SELECT sql_fulltext
     FROM v$sql
    WHERE sql_id = :sql_id
      AND ROWNUM = 1;
   ```
   If empty, the cursor aged out of the shared pool. Try AWR history:
   ```sql
   SELECT sql_text
     FROM dba_hist_sqltext
    WHERE sql_id = :sql_id;
   ```
3. **Application label** ("the orders report"). Search the agent's MEMORY for prior mappings of application names to SQL_IDs. If not found, ask the user for the SQL or SQL_ID.

### Step 2: Get the actual execution plan, not the EXPLAIN PLAN guess

`EXPLAIN PLAN FOR` shows what the optimizer *would* do under guessed bind values. It is frequently wrong because of bind variable peeking and adaptive cursor sharing. Always prefer the real plan from the cursor cache or AWR.

```sql
-- Real plan, if the cursor is still in memory
SELECT * FROM TABLE(dbms_xplan.display_cursor(:sql_id, NULL, 'ALLSTATS LAST +PEEKED_BINDS'));
```

If the cursor has aged out and AWR is licensed (Diagnostic Pack):
```sql
SELECT * FROM TABLE(dbms_xplan.display_awr(:sql_id, NULL, NULL, 'ALL'));
```

Fall back to `EXPLAIN PLAN` only as a last resort and *flag the result as estimated* in the response to the user.

### Step 3: Identify the dominant bottleneck

Scan the plan for these patterns in order. Stop at the first match — the *dominant* bottleneck is the one with the highest cost or actual rows. Do not list every minor issue.

| Pattern in plan | Likely cause | Suggested next skill |
|---|---|---|
| `TABLE ACCESS FULL` on a table with > 1M rows when filter selectivity is high | Missing or unused index | `recommend-index` |
| `NESTED LOOPS` driving > 100k rows | Bad join order / cardinality misestimate | Check stats freshness, consider `USE_HASH` hint |
| `HASH JOIN` with `BYTES` exceeding PGA target | Spill to TEMP | Increase `pga_aggregate_target` or rewrite |
| `INDEX SKIP SCAN` | Wrong leading column on composite index | `recommend-index` |
| `BUFFER SORT` or `SORT (UNIQUE)` on > 10k rows | Missing ORDER BY index or unnecessary DISTINCT | `rewrite-bad-query` |
| Plan starts with a small step but `A-Rows` is much larger than `E-Rows` | Stats are stale | `recommend-statistics-refresh` |
| `Predicate Information` shows `filter()` instead of `access()` on the indexed column | Function applied to indexed column → index unusable | `rewrite-bad-query` (suggest function-based index or refactor) |

### Step 4: Compare estimated vs actual rows

In the `ALLSTATS LAST` output, look at the `E-Rows` (estimated) vs `A-Rows` (actual) columns. A discrepancy of more than 10x at any plan step means the optimizer's cardinality model is wrong. This is almost always because of:

1. **Stale statistics.** Check `user_tab_statistics.last_analyzed` for involved tables. If older than two weeks AND the table has had significant DML, recommend `dbms_stats.gather_table_stats`.
2. **Skewed column.** A histogram is missing on a column with non-uniform distribution. Recommend `method_opt => 'FOR COLUMNS col1 SIZE AUTO'`.
3. **Correlated predicates.** Two predicates the optimizer thinks are independent but actually correlate. Recommend extended statistics: `dbms_stats.create_extended_stats`.

### Step 5: Check for the easy wins first

Before recommending anything complex, check these in order:

- **Are statistics fresh?** If not, the cheapest fix is `dbms_stats.gather_table_stats` (and a smarter optimizer might solve the problem with no schema changes).
- **Is there a bind variable peeking issue?** Look at `PEEKED_BINDS` section of the plan. If the binds at hard-parse time were atypical, the plan is wrong for the current binds. Suggested fix: `dbms_shared_pool.purge` on the cursor to force re-parse, or use `BIND_AWARE` cursor sharing.
- **Has the application changed?** Check `v$sql.last_active_time` for nearby SQL_IDs of the same application. If new SQL_IDs appeared recently, something deployed.

### Step 6: Report

Output structure (use this verbatim layout for consistency across sessions):

```
SQL_ID: <id> (or "ad-hoc query")
Last seen: <last_active_time>
Avg elapsed per execution: <ms>
Total executions: <n>

DOMINANT BOTTLENECK
<one-sentence diagnosis>

EVIDENCE
- E-Rows vs A-Rows mismatch at step <n>: <e> estimated, <a> actual (<ratio>x off)
- Wait event chain: <top 2 events from v$active_session_history for this SQL_ID>
- Plan operation: <the bad operation>

REMEDIATION OPTIONS (cheapest first)
1. <fix> — risk: <low/med/high>, downtime: <none/seconds/minutes>, expected gain: <%>
2. <alternative>
3. <last resort>

NEXT SKILL TO RUN
<one of recommend-index / recommend-statistics-refresh / rewrite-bad-query / null>
```

## Pitfalls

- **Bind variable peeking.** The plan shown by `display_cursor` is the plan that was hard-parsed with the *first* bind values the cursor saw. If those binds were atypical (e.g., a status code that matches 90% of rows when the typical query matches 0.1%), the plan is wrong for the typical case. Always check `+PEEKED_BINDS`.
- **Adaptive Cursor Sharing (ACS).** Oracle 11g+ can have multiple plans for the same SQL_ID under different bind value groups. `v$sql_plan_statistics_all` will show multiple plans. Pull all of them and check which one fires for which binds.
- **Result cache.** A query that looks fast in `v$sql.elapsed_time / executions` may actually be hitting the result cache and dodging the real plan cost. Check `executions` vs `parse_calls` — if executions are way higher than parse_calls, the cache is doing the work.
- **AWR licensing.** `dbms_xplan.display_awr` and `dba_hist_*` views require the Diagnostic Pack license. Do not run them on Standard Edition or unlicensed Enterprise Edition. Use the cursor cache only.
- **Parallel hints.** A query with `/*+ PARALLEL */` has a different plan shape; `PX SEND` and `PX RECEIVE` rows in the plan are not bottlenecks themselves — the underlying operation is.
- **Standby and read-only DBs.** On Active Data Guard, you cannot create indexes or refresh stats. Flag this and recommend the fix on the primary.

## Verification

After the user applies a recommended fix, re-run the skill and confirm:

1. **Plan change.** New `PLAN_HASH_VALUE` (`v$sql.plan_hash_value`) — must be different from before, otherwise the fix did not take effect.
2. **Elapsed time drop.** `v$sql.elapsed_time / executions` is at least 50% lower than the baseline measurement captured in step 6.
3. **E-Rows vs A-Rows agreement.** If stats was the issue, the new plan's estimates should be within 2x of actuals.
4. **No regression elsewhere.** Run `top-sql-this-hour` after the fix. Confirm no other SQL_ID that previously was not on the list has appeared with degraded performance — sometimes a new index helps query A but hurts query B's INSERT-time index maintenance.

Capture the before/after into MEMORY:
```
On <date>, SQL_ID <id> ("<app label>") fixed by <remediation>.
Baseline: <n> ms/exec. After: <n> ms/exec. Plan hash <before> → <after>.
```

This memory is what the auto-skill-generation Curator promotes into a `diagnose-{app}-pattern` skill after the third occurrence of the same app + remediation.
