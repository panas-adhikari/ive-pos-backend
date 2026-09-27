\set ON_ERROR_STOP on
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
SELECT version_num AS schema_revision FROM alembic_version ORDER BY version_num;
-- No identity values or customer details; compare outputs during a write freeze.
SELECT format('SELECT %L AS table_name, count(*) AS rows FROM %I.%I;',
              tablename, schemaname, tablename)
FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename \gexec
SELECT organization_id, store_id, currency, status, count(*) AS bills,
       sum(total_minor) AS sales_minor, sum(cash_received_minor) AS cash_received_minor
FROM sales GROUP BY organization_id, store_id, currency, status
ORDER BY organization_id, store_id, currency, status;
SELECT count(*) AS lines, sum(quantity) AS sold_units,
       sum(line_total_minor) AS line_revenue_minor, sum(line_cost_minor) AS line_cost_minor
FROM sale_lines;
SELECT organization_id, store_id, product_id, quantity
FROM stock_balances ORDER BY organization_id, store_id, product_id;
SELECT organization_id, store_id, kind, count(*) AS movements, sum(delta) AS quantity_delta
FROM stock_movements GROUP BY organization_id, store_id, kind
ORDER BY organization_id, store_id, kind;
SELECT count(*) AS orphan_lines FROM sale_lines l LEFT JOIN sales s ON s.id=l.sale_id
WHERE s.id IS NULL;
COMMIT;
