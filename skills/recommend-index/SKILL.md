---
name: recommend-index
description: Propose a missing index based on a slow query's execution plan and access predicates. Generates the CREATE INDEX DDL with the right column order and selectivity-aware leading column. Always requires confirmation before execution. Performs a dry-run impact estimate when possible.
when_to_use: |
  diagnose-slow-query identified an access-path problem (full scan with
  selective filter, or index range scan on the wrong index), or the user
  explicitly asked "what index should I add". Also called by the
  orchestrator when a SQL_ID's plan shows a TABLE ACCESS FULL with
  high actual rows being filtered out.
requires_toolsets:
  - oracle_db
  - oracle_db_write
required_environment_variables:
  - ORACLE_USER
  - ORACLE_PASSWORD
  - ORACLE_DSN
metadata:
  hermes:
    config:
      auto_load: false
      auto_confirm: false
      requires_user_confirmation: true
      cost_estimate: medium
---

# recommend-index

## When to Use

The optimizer's choice of full scan + filter is not always wrong, but it's wrong more often than people realize. Trigger this skill when:

- `diagnose-slow-query` reports `TABLE ACCESS FULL` on a large table with a highly selective predicate
- A query has `INDEX RANGE SCAN` followed by a `TABLE ACCESS BY INDEX ROWID` returning very few rows (suggests a better composite index)
- Predicates are in `filter()` clauses of the plan rather than `access()` (suggests the index does not cover the filter)
- The user explicitly asks "what index should I add for query X"

Do not use if:

- Stats are stale on the queried table → use `recommend-statistics-refresh` first; the optimizer may pick the right access path once stats are accurate
- The table is small (< 10k rows) → an index will not help much and adds DML overhead
- The query already has an index that the optimizer is choosing not to use → that's a stats or selectivity issue, not a missing-index issue

## Procedure

### Step 1: Get the query and its plan

If the calling skill (`diagnose-slow-query` or `orchestrator`) already pulled the plan, reuse it from MEMORY. Otherwise fetch fresh:

```sql
SELECT * FROM TABLE(DBMS_XPLAN.DISPLAY_CURSOR(:sql_id, NULL, 'ALLSTATS LAST'));
```

### Step 2: Identify the access pattern

Pull the plan's predicate information section. It looks like:

```
Predicate Information (identified by operation id):
   2 - filter("STATUS"='SHIPPED' AND "ORDER_DATE">=:B1)
   3 - access("CUSTOMER_ID"=:B2)
```

`filter()` predicates are applied after rows are fetched. `access()` predicates are used by the index for direct lookup. The goal is to convert `filter()` predicates on selective columns into `access()` predicates by indexing them.

### Step 3: Estimate each candidate column's selectivity

For each column appearing in a filter or join predicate, get its selectivity:

```sql
SELECT column_name,
       num_distinct,
       num_nulls,
       (SELECT num_rows FROM all_tab_statistics
         WHERE owner = :o AND table_name = :t) AS row_count,
       ROUND(num_distinct / NULLIF((SELECT num_rows FROM all_tab_statistics
                                     WHERE owner = :o AND table_name = :t), 0), 4)
       AS selectivity
  FROM all_tab_col_statistics
 WHERE owner = :o
   AND table_name = :t
   AND column_name IN (<columns from predicates>);
```

A column with `selectivity` near 1.0 (every value unique) is highly selective. Near 0 means very few distinct values (poor leading column unless combined with others).

### Step 4: Choose the column order

The leading column matters more than the rest. Rules in order of preference:

1. **Equality predicates first.** A column with `=` against a bind variable is always a better leading column than one with `>` or `LIKE`.
2. **Most selective first** among equality columns. If predicates are `customer_id = ?` (500k distinct) and `status = ?` (4 distinct), lead with `customer_id`.
3. **Range predicates last.** A column used in `BETWEEN`, `>`, `<`, or `LIKE 'X%'` should be the final indexed column.
4. **Columns the SELECT list returns** can be added after the predicate columns to enable an *index-only* scan (no table access). Only worth it if the selectivity is already good and the SELECT list is short.

### Step 5: Special cases

| Predicate shape | Index recommendation |
|---|---|
| `UPPER(col) = 'X'` or `LOWER(col) = 'x'` | Function-based index: `CREATE INDEX ix_t_upper_col ON t(UPPER(col))` |
| `TRUNC(date_col) = ?` | Function-based: `CREATE INDEX ix_t_trunc_dt ON t(TRUNC(date_col))` |
| `col LIKE '%foo%'` (leading wildcard) | No index will help. Consider Oracle Text or full-text search. |
| `col1 = ? AND col2 = ?` where one column matters much more | Single-column index on the more-selective one; not a composite. |
| `(col1, col2)` always queried together | Composite, leading with the more-selective one. |
| `JOIN parent ON parent.id = child.parent_id` and child has no index on parent_id | Foreign key index on `child.parent_id` — crucial for parent-side row deletes too |

### Step 6: Estimate impact before recommending

Use the cost-based optimizer to project the new plan *without creating the index*. Two options:

**A. `dbms_advisor` (preferred, if licensed)**

```sql
DECLARE
  l_task_name VARCHAR2(40) := 'oracleops_recommend_index_' || TO_CHAR(SYSDATE,'YYYYMMDDHH24MISS');
BEGIN
  DBMS_SQLTUNE.CREATE_TUNING_TASK(
    sql_id   => :sql_id,
    task_name=> l_task_name,
    scope    => 'COMPREHENSIVE'
  );
  DBMS_SQLTUNE.EXECUTE_TUNING_TASK(l_task_name);
END;
/

-- Inspect:
SELECT DBMS_SQLTUNE.REPORT_TUNING_TASK('<task_name>', 'TEXT', 'TYPICAL') FROM DUAL;
```

Diagnostic Pack + Tuning Pack required. Reports estimated cost improvement.

**B. Manual virtual-index test (always available)**

Create a "no-segment" index that the optimizer will consider but doesn't actually build:

```sql
CREATE INDEX ix_orders_customer_id ON orders(customer_id) NOSEGMENT;
```

Then re-explain the original query:

```sql
ALTER SESSION SET "_use_nosegment_indexes" = TRUE;
EXPLAIN PLAN FOR <original_sql>;
SELECT * FROM TABLE(DBMS_XPLAN.DISPLAY(NULL, NULL, 'BASIC'));
ALTER SESSION SET "_use_nosegment_indexes" = FALSE;
DROP INDEX ix_orders_customer_id;
```

If the new plan uses the virtual index AND the estimated cost drops by > 50%, recommend it. If not, propose a different column order or skip.

### Step 7: Produce the recommendation

Use this output structure:

```
INDEX RECOMMENDATION for SQL_ID <id>

PROPOSED DDL
CREATE INDEX <owner>.<index_name>
  ON <owner>.<table>(<col1> [, <col2>, ...])
  [TABLESPACE <ts>]
  ONLINE;

JUSTIFICATION
- Current plan: TABLE ACCESS FULL on <table>, filtering <pct>% of rows
- Predicate "<col> = :B1" is currently a filter(), not an access()
- Column selectivity: <col1>=<sel1>, <col2>=<sel2>
- Estimated cost: current <cost1>, with new index <cost2> (<pct>% reduction)

DOWNSIDES
- INSERT/UPDATE/DELETE on <table> will be slower by an estimated <n> ms each
- Storage cost: roughly <size> MB (=<rows> rows × <bytes>/entry)
- One more index to maintain during DDL

WANT ME TO CREATE IT? [yes / no / show-other-options]

ALTERNATIVES (if you said "show-other-options")
- Function-based index on UPPER(<col>) — only if your queries use UPPER
- Composite (<col1>, <col2>) with col2 first — only if col2 is more selective
- Skip indexing; refresh stats instead — try `recommend-statistics-refresh`
```

### Step 8: On user "yes", execute via the confirmation-gated tool. On "no", record the denial.

The user's response is consumed by the agent. Two branches:

**Branch A — user says yes (or any standard affirmative).** CREATE INDEX is a Tier 1 write (not DROP / TRUNCATE / ALTER), so the plain affirmative allowlist applies: `yes`, `y`, `confirm`, `proceed`, `ok`, `do it`, `go ahead`. Pass the user's literal response as `user_confirmation_token` to `oracle_write_with_confirmation`:

```sql
CREATE INDEX <owner>.<index_name> ON <owner>.<table>(<columns>) ONLINE;
```

Always use `ONLINE`. Always run during low-load hours if the table is large. If the table is > 100M rows, propose `PARALLEL <n>` and warn that this consumes resources.

After creation, capture stats:

```sql
EXEC DBMS_STATS.GATHER_INDEX_STATS(USER, '<index_name>');
```

**Branch B — user says no, not now, show alternatives, or any negative.** Do NOT call the write tool. Instead, call `oracle_record_denial` with:

- `proposed_sql`: the exact CREATE INDEX DDL we proposed
- `user_response`: the user's literal denial text
- `reason`: the agent's interpretation of why (e.g., "user wants composite index instead", "user prefers to wait until off-hours", "user wants to refresh stats first and see if optimizer picks better access path")

The denial gets appended to the same audit log as approvals (with `event: "denied"`), so the trail captures both what was done AND what was proposed-but-not-done. This is gold for tuning later: you can see exactly where the agent's judgment diverged from what a human DBA chose.

If the user said "show alternatives" specifically, follow up with the alternative-index options from Step 7 before re-proposing.

**Branch C — user says nothing or asks a clarifying question.** Answer the clarifying question. Do NOT pre-execute. Do NOT auto-confirm after some timeout.

### Step 8b (rare): DROP/ALTER on the index later requires Tier 2 confirmation

If the user later asks you to drop the index you just created, that DDL is a DROP statement which is destructive. The confirmation gate requires the user to type the index name (e.g., `IX_ORDERS_CUSTOMER_ID`) or the literal phrase `I understand`. Plain "yes" is not sufficient for DROP. This is enforced inside `oracle_write_with_confirmation` itself — the calling skill doesn't need to police it, but it should set the user's expectations: "Reply with the index name `IX_ORDERS_CUSTOMER_ID` (or `I understand`) to confirm the drop."

### Step 9: Re-run diagnose-slow-query to confirm

Call `diagnose-slow-query` again on the original SQL_ID and compare. The new `plan_hash_value` must differ from the old; if it doesn't, the optimizer didn't pick up the index (rare, but happens — usually because stats on the new index lag). Force a hard parse:

```sql
EXEC DBMS_SHARED_POOL.PURGE('<sql_address>,<hash_value>', 'C');
```

## Pitfalls

- **Index on low-cardinality columns alone.** A `status` column with 4 distinct values is a bad single-column index. The optimizer often won't use it. Combine it with a more selective column or skip it.
- **Composite index column order is permanent.** Once created, swapping leading column requires drop + create. Test thoroughly before recommending.
- **Function-based indexes need `query_rewrite_enabled = TRUE`** and the query must use the *exact* same function expression. `UPPER(email)` and `upper(email)` work, but `UPPER(EMAIL)` and `UPPER(email)` are the same to Oracle — case in the column name doesn't matter, case in the function name doesn't matter, but extra spaces or operator differences will prevent use.
- **`ONLINE` is required for production indexes.** Without it, the index build takes a TM lock that blocks all DML on the table. Default is offline, so always specify ONLINE.
- **Parallel index builds.** `PARALLEL 8` builds faster but consumes 8 worker slaves. On a 1-OCPU ADB Always Free instance, you do not have 8 slaves available. Use `PARALLEL 2` at most, or stay serial.
- **Index-organized tables and reverse-key indexes.** These exist and have specific use cases (the latter for sequence-keyed hot blocks). Do not recommend them speculatively. Only if the user explicitly has a known reason.
- **Foreign key columns without indexes.** A child table missing an index on its FK column will cause parent-side DELETEs to take a TM lock. This is a separate, common, and underdiagnosed issue. The skill should flag missing FK indexes when scanning predicates, even if the user's specific query doesn't show the symptom.
- **Hidden parameters.** `_use_nosegment_indexes` is undocumented. It works in 11g+ but Oracle Support may decline tickets. Mention this if a user asks why we used it.
- **Vector indexes (23ai/26ai).** If the column is a `VECTOR` type and the query uses `VECTOR_DISTANCE`, the right index is `CREATE VECTOR INDEX` not a B-tree. Different syntax, different choice of approximate-nearest-neighbor algorithm (HNSW, IVF). Out of scope for this skill in v1.

## Verification

After the user creates the recommended index:

1. **The plan changes.** Re-run `diagnose-slow-query` on the original SQL_ID. `plan_hash_value` MUST be different. If not, the optimizer didn't pick it up — usually because cardinality estimates lie. Run `dbms_stats.gather_index_stats` and `dbms_stats.gather_table_stats(method_opt => 'FOR ALL INDEXED COLUMNS SIZE AUTO')`.
2. **Cost drops.** The plan's `Cost` column should be lower. Not necessarily by the percentage advisor predicted — predictions are estimates.
3. **Elapsed time drops.** Run the query a few times (after a hard parse) and confirm `v$sql.elapsed_time / executions` is at least 50% lower.
4. **No regressions.** Run `top-sql-this-hour` after the change. Confirm no other SQL_ID has degraded; sometimes a new index helps query A but hurts query B's INSERT cost. If a regression appears, decide whether the gain is worth the trade.

Capture into MEMORY:
```
On <date>, created index <name> on <table>(<cols>) for SQL_ID <id>.
Before: <ms> ms/exec, plan <hash1>. After: <ms> ms/exec, plan <hash2>.
DML cost on <table>: <before> -> <after> ms.
```

After three index recommendations for the same application, the Curator may promote a `recommend-indexes-for-<app>` super-skill that proposes index sets rather than single indexes.
