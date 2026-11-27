-- SPDX-License-Identifier: MIT
-- Copyright (c) 2026 naturalTuna — https://github.com/naturalTuna/pg_tune_ai
-- 비교 벤치 데이터 (PostgreSQL 16, shared_preload_libraries = 'pg_qualstats,pg_stat_statements', pg_qualstats.sample_rate = 1)
CREATE EXTENSION IF NOT EXISTS pg_qualstats; CREATE EXTENSION IF NOT EXISTS hypopg;
CREATE TABLE customers (id int PRIMARY KEY, grade char(1), name text);
INSERT INTO customers SELECT g, CASE WHEN g % 200 = 0 THEN 'V' ELSE 'N' END, 'c'||g FROM generate_series(1,20000) g;
CREATE TABLE orders (id int PRIMARY KEY, status varchar(10), cust_id int, created_at timestamp, amt int, "Region" int, note text);
INSERT INTO orders SELECT g, CASE WHEN g % 100 = 0 THEN 'A' ELSE 'B' END, 1 + g % 20000,
  timestamp '2024-01-01' + (g % 50000) * interval '10 minutes', g % 1000, g % 50, md5(g::text) FROM generate_series(1,300000) g;
ANALYZE;
