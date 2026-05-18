--------------------------------------------------------------------------------
-- OracleOps demo seed (plain SQL — no PL/SQL anonymous blocks)
--------------------------------------------------------------------------------
-- Creates a small e-commerce schema with intentional performance traps so
-- every OracleOps skill has something realistic to find. Designed to fit
-- in the Always Free Tier and seed in 30-60 seconds on 1 OCPU
-- Autonomous Database.
--
-- Run any of these ways:
--   1. OCI Database Actions -> SQL -> paste this file -> Run Script
--   2. SQL*Plus: sqlplus admin/<pw>@oracleopsdemo_high @examples/seed-demo.sql
--   3. Python loader: python scripts/load-seed.py
--
-- Idempotent: uses Oracle 23ai's DROP TABLE IF EXISTS so re-runs are safe.
-- If you are on Oracle 19c or older, replace IF EXISTS with PL/SQL blocks,
-- or simply let the DROP statements error on first run (they have no effect).
--------------------------------------------------------------------------------

-- 1. Tear down anything from a previous run -----------------------------------
-- Drop child tables first to avoid FK ordering issues.

DROP VIEW IF EXISTS vw_customer_revenue;
DROP TABLE IF EXISTS order_items CASCADE CONSTRAINTS PURGE;
DROP TABLE IF EXISTS orders CASCADE CONSTRAINTS PURGE;
DROP TABLE IF EXISTS products CASCADE CONSTRAINTS PURGE;
DROP TABLE IF EXISTS customers CASCADE CONSTRAINTS PURGE;

-- 2. CUSTOMERS - 100k rows ---------------------------------------------------
-- Trap: queries that filter by UPPER(email) will not use the email index.

CREATE TABLE customers (
  customer_id    NUMBER        NOT NULL,
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
CONNECT BY LEVEL <= 100000;
COMMIT;

-- 3. PRODUCTS - 1k rows ------------------------------------------------------

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
CONNECT BY LEVEL <= 1000;
COMMIT;

-- 4. ORDERS - 500k rows ------------------------------------------------------
-- Trap 1: NO index on customer_id, so customer-history lookups go full scan.
-- Trap 2: INITRANS=1 on a hot table provokes "enq: TX - allocate ITL entry"
--         contention under concurrent inserts (find-lock-contention demo).
-- Trap 3: status column has skewed distribution (95% 'SHIPPED') with no
--         histogram, so optimizer mis-estimates cardinality.

CREATE TABLE orders (
  order_id    NUMBER        NOT NULL,
  customer_id NUMBER        NOT NULL,
  order_date  DATE          NOT NULL,
  status      VARCHAR2(20)  NOT NULL,
  total       NUMBER(12,2)  NOT NULL,
  returned_at DATE,
  CONSTRAINT pk_orders PRIMARY KEY (order_id)
) INITRANS 1 MAXTRANS 2;

CREATE INDEX ix_orders_date ON orders(order_date);
-- DELIBERATELY MISSING: an index on customer_id. recommend-index should
-- propose this as the cheapest fix for customer-history queries.

INSERT /*+ APPEND */ INTO orders
SELECT
  LEVEL AS order_id,
  TRUNC(DBMS_RANDOM.VALUE(1, 100000)) AS customer_id,
  DATE '2024-01-01' + DBMS_RANDOM.VALUE(0, 500) AS order_date,
  CASE
    WHEN MOD(LEVEL, 20) = 0 THEN 'PENDING'
    WHEN MOD(LEVEL, 50) = 0 THEN 'CANCELLED'
    WHEN MOD(LEVEL, 200) = 0 THEN 'FRAUD_HOLD'
    ELSE 'SHIPPED'
  END AS status,
  ROUND(DBMS_RANDOM.VALUE(10, 2000), 2) AS total,
  NULL AS returned_at
  FROM dual
CONNECT BY LEVEL <= 500000;
COMMIT;

-- 5. ORDER_ITEMS - 1M rows ---------------------------------------------------
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
  TRUNC(DBMS_RANDOM.VALUE(1, 500000))  AS order_id,
  TRUNC(DBMS_RANDOM.VALUE(1, 1000))    AS product_id,
  TRUNC(DBMS_RANDOM.VALUE(1, 6))       AS qty,
  ROUND(DBMS_RANDOM.VALUE(5, 800), 2)  AS line_total
  FROM dual
CONNECT BY LEVEL <= 1000000;
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

-- 7. Gather full stats, then make a subset stale -----------------------------
-- After this block, the optimizer believes RETURNED rows don't exist.

BEGIN DBMS_STATS.GATHER_SCHEMA_STATS(USER, CASCADE => TRUE); END;
/

UPDATE orders
   SET status = 'RETURNED', returned_at = SYSDATE - DBMS_RANDOM.VALUE(0, 30)
 WHERE MOD(order_id, 20) = 5;
COMMIT;

-- Intentionally do NOT regather stats here so recommend-statistics-refresh
-- has something to find.

-- 8. Insert a single PENDING order with a peculiar customer_id ---------------
-- Atypical bind value for bind-variable-peeking demos.

INSERT INTO orders (order_id, customer_id, order_date, status, total)
VALUES (500001, 1, SYSDATE, 'PENDING', 9999.99);
COMMIT;

-- 9. Final row counts --------------------------------------------------------

SELECT table_name, num_rows
  FROM user_tab_statistics
 WHERE table_name IN ('CUSTOMERS','PRODUCTS','ORDERS','ORDER_ITEMS')
 ORDER BY num_rows DESC;
