# Phase 14 (WH-01): analytics warehouse. Base pinned by tag and digest; PGDG FDW packages pinned by
# upstream version. If a pinned revision disappears from apt.postgresql.org, take it from
# apt-archive.postgresql.org.
FROM pgvector/pgvector:0.8.7-pg16-bookworm@sha256:7b822b0aac60967beb1ea5e576b8602c94c300a157d187f385ae3e0da199b90a
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      "postgresql-16-mysql-fdw=2.9.3-*" "postgresql-16-tds-fdw=2.0.5-*" \
 && rm -rf /var/lib/apt/lists/*
# track_utility=off: on PG16 pg_stat_statements would otherwise store CREATE/ALTER USER MAPPING
#   and ALTER ROLE ... PASSWORD statements verbatim, passwords included.
# log_min_error_statement=panic: a failing DDL statement carrying a password literal is never
#   written to the server log. log_statement=none for the same reason.
CMD ["postgres", \
     "-c", "shared_preload_libraries=pg_stat_statements", \
     "-c", "pg_stat_statements.track_utility=off", \
     "-c", "log_min_error_statement=panic", \
     "-c", "log_statement=none", \
     "-c", "shared_buffers=256MB", "-c", "effective_cache_size=768MB", \
     "-c", "work_mem=32MB", "-c", "maintenance_work_mem=128MB", \
     "-c", "max_parallel_workers_per_gather=2", "-c", "max_connections=50", \
     "-c", "random_page_cost=1.1"]
