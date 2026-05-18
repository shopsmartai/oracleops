--------------------------------------------------------------------------------
-- OracleOps demo seed
--------------------------------------------------------------------------------
-- Creates a small e-commerce schema with intentional performance traps so
-- every OracleOps skill has something realistic to find. Designed to fit
-- in the Always Free Tier (~50 MB of data, runs in under 2 minutes on
-- 1 OCPU Autonomous Database).
--
-- Run as ADMIN on the oracleopsdemo ADB:
--   sqlplus admin/<pw>@oracleopsdemo_high @examples/seed-demo.sql
-- or paste into Database Actions → SQL Worksheet.
--
-- Idempotent: drops and recreates objects on every run.
--------------------------------------------------------------------------------

SET ECHO ON
SET FEEDBACK ON
SET SERVEROUTPUT ON
WHENEVER SQLERROR CONTINUE

-- 1. Tear down anything from a previous run -----------------------------------

BEGIN
  FOR r IN (SELECT table_name FROM user_tables
             WHERE table_name IN ('ORDER_ITEMS','ORDERS','PRODUCTS','CUSTOMERS')) LOOP
    EXECUTE IMMEDIATE 'DROP TABLE ' || r.table_name || ' CASCADE CONSTRAINTS PURGE';
  END LOOP;
  FOR r IN (SELECT view_name FROM user_views
             WHERE view_name IN ('VW_CUSTOMER_REVENUE')) LOOP
    EXECUTE IMMEDIATE 'DROP VIEW ' || r.view_name;
  END LOOP;
EXCEPTION WHEN OTHERS THEN
  DBMS_OUTPUT.PUT_LINE('Cleanup: ' || SQLERRM);
END;
/

-- 2. CUSTOMERS — 500k rows, narrow table --------------------------------------
-- Trap: queries that filter by UPPER(email) will not use the email index.

CREATE TABLE customers (
  customer_id    NUMBER       NOT NULL,
  email          VARCHAR2(120) NOT NULL,
  signup_date    DATE          NOT NULL,
  country        VARCHAR2(2)   NOT NULL,
  lifetime_value NUMBER(12,2)  DEFAULT 0,
  CONSTRAINT pk_customers PRIMARY KEY (customer_id)
);

CREATE INDEX ix_customers_email ON customers(email);
CREATE INDEX ix_customers_country ON customers(country);

INSERT /*+ APPEND */ INTO customers
SELECT
  LEVEL AS customer_id,
  'user' || LEVEL || '@example.com' AS email,
  DATE '2022-01-01' + DBMS_RANDOM.VALUE(0, 1200) AS signup_date,
  CASE MOD(LEVEL, 7)
    WHEN 0 THEN 'US' WHEN 1 THEN 'GB' WHEN 2 THEN 'IN'
    WHEN 3 THEN 'DE' WHEN 4 THEN 'BR' WHEN 5 THEN 'JP'
    ELSE 'CA'
  END AS country,
  ROUND(DBMS_RANDOM.VALUE(0, 50000), 2) AS lifetime_value
  FROM dual
CONNECT BY LEVEL <= 500000;
COMMIT;

-- 3. PRODUCTS — 5k rows, small lookup table -----------------------------------

CREATE TABLE products (
  product_id   NUMBER        NOT NULL,
  sku          VARCHAR2(40)  NOT NULL,
  category     VARCHAR2(30)  NOT NULL,
  price        NUMBER(10,2)  NOT NULL,
  CONSTRAINT pk_products PRIMARY KEY (product_id)
);

CREATE INDEX ix_products_category ON products(category);

INSERT /*+ APPEND */ INTO products
SELECT
  LEVEL AS product_id,
  'SKU-' || LPAD(LEVEL, 8, '0') AS sku,
  CASE MOD(LEVEL, 5)
    WHEN 0 THEN 'electronics' WHEN 1 THEN 'apparel'
    WHEN 2 THEN 'home' WHEN 3 THEN 'books' ELSE 'beauty'
  END AS category,
  ROUND(DBMS_RANDOM.VALUE(5, 500), 2) AS price
  FROM dual
CONNECT BY LEVEL <= 5000;
COMMIT;

-- 4. ORDERS — 2M rows ---------------------------------------------------------
-- Trap 1: NO index on customer_id, so customer-history lookups go full scan.
-- Trap 2: INITRANS = 1 on a hot table provokes "enq: TX - allocate ITL entry"
--         contention under concurrent inserts (demo for find-lock-contention).
-- Trap 3: status column has skewed distribution (95% 'SHIPPED') with no
--         histogram, so optimizer mis-estimates cardinality.

CREATE TABLE orders (
  order_id    NUMBER        NOT NULL,
  customer_id NUMBER        NOT NULL,
  order_date  DATE          NOT NULL,
  status      VARCHAR2(20)  NOT NULL,
  total       NUMBER(12,2)  NOT NULL,
  CONSTRAINT pk_orders PRIMARY KEY (order_id)
) INITRANS 1 MAXTRANS 2;

CREATE INDEX ix_orders_date ON orders(order_date);
-- DELIBERATELY MISSING: an index on customer_id. recommend-index should
-- propose this as the cheapest fix for customer-history queries.

INSERT /*+ APPEND */ INTO orders
SELECT
  LEVEL AS order_id,
  TRUNC(DBMS_RANDOM.VALUE(1, 500000)) AS customer_id,
  DATE '2024-01-01' + DBMS_RANDOM.VALUE(0, 500) AS order_date,
  CASE
    WHEN MOD(LEVEL, 20) = 0 THEN 'PENDING'
    WHEN MOD(LEVEL, 50) = 0 THEN 'CANCELLED'
    WHEN MOD(LEVEL, 200) = 0 THEN 'FRAUD_HOLD'
    ELSE 'SHIPPED'
  END AS status,
  ROUND(DBMS_RANDOM.VALUE(10, 2000), 2) AS total
  FROM dual
CONNECT BY LEVEL <= 2000000;
COMMIT;

-- 5. ORDER_ITEMS — 5M rows ---------------------------------------------------
-- Trap: NO index on product_id, so product-level reporting joins go via
-- HASH JOIN on the full table.

CREATE TABLE order_items (
  order_item_id NUMBER       NOT NULL,
  order_id      NUMBER       NOT NULL,
  product_id    NUMBER       NOT NULL,
  qty           NUMBER       NOT NULL,
  line_total    NUMBER(12,2) NOT NULL,
  CONSTRAINT pk_order_items PRIMARY KEY (order_item_id)
);

CREATE INDEX ix_order_items_order ON order_items(order_id);
-- DELIBERATELY MISSING: index on product_id.

INSERT /*+ APPEND */ INTO order_items
SELECT
  LEVEL AS order_item_id,
  TRUNC(DBMS_RANDOM.VALUE(1, 2000000)) AS order_id,
  TRUNC(DBMS_RANDOM.VALUE(1, 5000))     AS product_id,
  TRUNC(DBMS_RANDOM.VALUE(1, 6))        AS qty,
  ROUND(DBMS_RANDOM.VALUE(5, 800), 2)   AS line_total
  FROM dual
CONNECT BY LEVEL <= 5000000;
COMMIT;

-- 6. View with scalar subquery in the SELECT list ----------------------------
-- Classic anti-pattern: per-row subquery instead of a join. rewrite-bad-query
-- should detect this and propose a JOIN+GROUP BY rewrite.

CREATE OR REPLACE VIEW vw_customer_revenue AS
SELECT
  c.customer_id,
  c.email,
  (SELECT NVL(SUM(o.total), 0)
     FROM orders o
    WHERE o.customer_id = c.customer_id
      AND o.status = 'SHIPPED') AS revenue
  FROM customers c;

-- 7. Make statistics stale on ORDERS ----------------------------------------
-- We gather full stats on everything except ORDERS, then update ORDERS so
-- the cardinality model is off. recommend-statistics-refresh should flag it.

EXEC DBMS_STATS.GATHER_SCHEMA_STATS(USER, CASCADE => TRUE);

-- Simulate an unstats'd churn: 5% of orders move from SHIPPED to RETURNED
-- without re-gathering. After this, the optimizer thinks RETURNED rows
-- don't exist, so any query filtering on status='RETURNED' will badly
-- mis-estimate.

ALTER TABLE orders ADD (returned_at DATE);

UPDATE orders
   SET status = 'RETURNED', returned_at = SYSDATE - DBMS_RANDOM.VALUE(0, 30)
 WHERE MOD(order_id, 20) = 5;
COMMIT;

-- Intentionally do NOT regather stats here.

-- 8. Insert a single PENDING order with a peculiar customer_id ---------------
-- This is the "needle in a haystack" candidate for bind-variable-peeking
-- demos: an atypical bind value will hard-parse a bad plan.

INSERT INTO orders (order_id, customer_id, order_date, status, total)
VALUES (2000001, 1, SYSDATE, 'PENDING', 9999.99);
COMMIT;

-- 9. Summary ----------------------------------------------------------------

PROMPT
PROMPT ============================================================
PROMPT OracleOps demo data loaded.
PROMPT ============================================================
PROMPT
PROMPT Tables:
SELECT table_name, num_rows
  FROM user_tab_statistics
 WHERE table_name IN ('CUSTOMERS','PRODUCTS','ORDERS','ORDER_ITEMS')
 ORDER BY num_rows DESC;

PROMPT
PROMPT Built-in performance traps to find:
PROMPT   1. orders.customer_id has no index  ->  recommend-index
PROMPT   2. order_items.product_id has no index
PROMPT   3. orders has INITRANS=1            ->  ITL contention under load
PROMPT   4. orders status histogram missing  ->  recommend-statistics-refresh
PROMPT   5. vw_customer_revenue is a scalar  ->  rewrite-bad-query
PROMPT      subquery anti-pattern
PROMPT
PROMPT Suggested queries to run for demos:
PROMPT   -- Full table scan because customer_id has no index
PROMPT   SELECT * FROM orders WHERE customer_id = 42;
PROMPT
PROMPT   -- Function on indexed column kills the index
PROMPT   SELECT * FROM customers WHERE UPPER(email) = 'USER42@EXAMPLE.COM';
PROMPT
PROMPT   -- Scalar subquery anti-pattern
PROMPT   SELECT * FROM vw_customer_revenue WHERE customer_id BETWEEN 100 AND 200;
PROMPT
PROMPT   -- Status with stale stats
PROMPT   SELECT COUNT(*) FROM orders WHERE status = 'RETURNED';
PROMPT
PROMPT ============================================================
