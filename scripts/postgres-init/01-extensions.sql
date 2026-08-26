-- Run once, by the postgres image entrypoint, on first cluster initialisation.
--
-- pg_stat_statements gives us per-fingerprint execution statistics straight
-- from the server. Aperture's own fingerprinting (sqlglot, Week 2 Day 9) is
-- the primary source of truth, but pg_stat_statements is the independent
-- cross-check used when validating the missing-index detector in Week 3.
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

-- pg_trgm backs the product catalogue text search (ILIKE on products.title).
-- Without it that filter is a sequential scan on every catalogue browse, which
-- would drown the deliberately planted pathologies in unrelated noise.
CREATE EXTENSION IF NOT EXISTS pg_trgm;
