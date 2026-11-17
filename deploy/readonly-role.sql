-- Intelligence Corner read-only role kit (Phase 11, GOLIVE-03).
--
-- Inputs: run these SET lines in the same session BEFORE this file
--   SET ic_ro.role = 'intelligence_corner';
--   SET ic_ro.schemas = 'public,billing';          -- in-scope schemas, comma list
--   SET ic_ro.revoke = 'public.api_tokens';        -- schema.table comma list (context pack revoke_select)
--   SET ic_ro.password = '...';                    -- optional; an empty value keeps the existing password
--   SET ic_ro.statement_timeout = '30s';           -- optional, default 30s
--   SET ic_ro.reset_revoke = 'on';                 -- optional; forget the previously recorded revoke list
--
-- The revoke list is sticky: tables recorded on the role by an earlier run stay
-- revoked on a re-run unless ic_ro.reset_revoke = 'on' (agentctl --reset-revoke).
-- `agentctl readonly-role` (or `python -m app.readonlykit render`) writes those lines for you.
--
-- Views and materialized views that read a revoked table are revoked too (they would expose its rows).
--
-- Run it on the PRIMARY with an admin login: roles are cluster-wide and a hot
-- standby cannot CREATE ROLE. Point Intelligence Corner at a replica with --host.
-- A schema that was in scope on an earlier run and is not listed now loses its
-- grants (Quick 029): REVOKE removes only grants made by the same grantor, or by
-- the owner when a superuser runs the kit, so run the kit as the same admin each time.
--
-- It is idempotent and safe to re-run after migrations add tables. New tables
-- are NOT auto-granted, by design, so a new sensitive table is never exposed silently.
-- It never touches PUBLIC. On PostgreSQL 14 and older a DBA decides
-- whether to run REVOKE CREATE ON SCHEMA public FROM PUBLIC. The selftest
-- (agentctl selftest) checks the effective result.
DO $kit$
DECLARE
  r   text := current_setting('ic_ro.role', true);
  sch text := current_setting('ic_ro.schemas', true);
  rv  text := coalesce(current_setting('ic_ro.revoke', true), '');
  pw  text := current_setting('ic_ro.password', true);
  st  text := coalesce(nullif(current_setting('ic_ro.statement_timeout', true), ''), '30s');
  s   text;
  t   text;
  prev text;
  revoked_oids oid[] := '{}';
  vn  text;
  vr  text;
  scoped text[];
  rid oid;
  rel text;
BEGIN
  IF coalesce(r, '') = '' OR coalesce(sch, '') = '' THEN
    RAISE EXCEPTION 'SET ic_ro.role and ic_ro.schemas first';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
    EXECUTE format('CREATE ROLE %I LOGIN NOINHERIT', r);
  END IF;
  EXECUTE format('ALTER ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 10', r);
  IF coalesce(pw, '') <> '' THEN
    EXECUTE format('ALTER ROLE %I PASSWORD %L', r, pw);
  END IF;
  EXECUTE format('ALTER ROLE %I SET default_transaction_read_only = on', r);
  EXECUTE format('ALTER ROLE %I SET statement_timeout = %L', r, st);
  EXECUTE format('ALTER ROLE %I SET idle_in_transaction_session_timeout = %L', r, '60s');
  EXECUTE format('GRANT CONNECT ON DATABASE %I TO %I', current_database(), r);
  IF coalesce(current_setting('ic_ro.reset_revoke', true), '') <> 'on' THEN
    prev := coalesce(split_part(shobj_description(
              (SELECT oid FROM pg_roles WHERE rolname = r), 'pg_authid'), 'revoke_select: ', 2), '');
    rv := concat_ws(',', nullif(prev, ''), nullif(rv, ''));
  END IF;
  -- Quick 029: schemas dropped from scope lose their grants. Only schemas where this role holds a direct ACL entry; PUBLIC is never touched.
  scoped := ARRAY(SELECT btrim(x) FROM unnest(string_to_array(sch, ',')) AS x);
  SELECT oid INTO rid FROM pg_roles WHERE rolname = r;
  FOR s IN
    SELECT n.nspname::text FROM pg_namespace n
     WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
       AND n.nspname NOT LIKE 'pg\_toast%' AND n.nspname NOT LIKE 'pg\_temp%'
       AND NOT (n.nspname = ANY(scoped))
       AND (EXISTS (SELECT 1 FROM aclexplode(n.nspacl) a WHERE a.grantee = rid)
            OR EXISTS (SELECT 1 FROM pg_class c, aclexplode(c.relacl) a
                        WHERE c.relnamespace = n.oid AND a.grantee = rid))
  LOOP
    EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM %I', s, r);
    FOR rel IN
      SELECT c.relname::text FROM pg_class c, aclexplode(c.relacl) a
       WHERE c.relnamespace = (SELECT oid FROM pg_namespace WHERE nspname = s) AND a.grantee = rid
    LOOP
      EXECUTE format('REVOKE ALL ON TABLE %I.%I FROM %I', s, rel, r);
    END LOOP;
    EXECUTE format('REVOKE ALL ON SCHEMA %I FROM %I', s, r);
    RAISE NOTICE 'schema % is out of scope: its grants were revoked', s;
  END LOOP;
  FOREACH s IN ARRAY string_to_array(sch, ',') LOOP
    s := btrim(s);
    CONTINUE WHEN s = '';
    EXECUTE format('GRANT USAGE ON SCHEMA %I TO %I', s, r);
    EXECUTE format('GRANT SELECT ON ALL TABLES IN SCHEMA %I TO %I', s, r);
  END LOOP;
  FOREACH t IN ARRAY string_to_array(rv, ',') LOOP
    t := btrim(t);
    CONTINUE WHEN t = '';
    IF to_regclass(format('%I.%I', split_part(t, '.', 1), split_part(t, '.', 2))) IS NULL THEN
      RAISE NOTICE 'revoke list: % not found, skipped', t;
      CONTINUE;
    END IF;
    EXECUTE format('REVOKE ALL ON TABLE %I.%I FROM %I', split_part(t, '.', 1), split_part(t, '.', 2), r);
    revoked_oids := revoked_oids || to_regclass(format('%I.%I', split_part(t, '.', 1), split_part(t, '.', 2)))::oid;
  END LOOP;
  -- Phase 11 gap closure (WR-02): views and materialized views over a revoked table run with their owner's rights, so revoke them too.
  IF cardinality(revoked_oids) > 0 THEN
    FOR vn, vr IN
      WITH RECURSIVE dep(oid) AS (
        SELECT DISTINCT rw.ev_class
          FROM pg_depend d JOIN pg_rewrite rw ON rw.oid = d.objid
         WHERE d.classid = 'pg_rewrite'::regclass AND d.refclassid = 'pg_class'::regclass
           AND d.refobjid = ANY(revoked_oids) AND rw.ev_class <> d.refobjid
        UNION
        SELECT rw.ev_class
          FROM dep JOIN pg_depend d ON d.refobjid = dep.oid
          JOIN pg_rewrite rw ON rw.oid = d.objid
         WHERE d.classid = 'pg_rewrite'::regclass AND d.refclassid = 'pg_class'::regclass
           AND rw.ev_class <> d.refobjid
      )
      SELECT n.nspname::text, c.relname::text
        FROM dep JOIN pg_class c ON c.oid = dep.oid JOIN pg_namespace n ON n.oid = c.relnamespace
       WHERE c.relkind IN ('v', 'm')
    LOOP
      EXECUTE format('REVOKE ALL ON TABLE %I.%I FROM %I', vn, vr, r);
      RAISE NOTICE 'revoked view %.% (it reads a revoked table)', vn, vr;
    END LOOP;
  END IF;
  EXECUTE format('COMMENT ON ROLE %I IS %L', r,
    'Intelligence Corner read-only role (deploy/readonly-role.sql). revoke_select: ' || rv);
END
$kit$;
