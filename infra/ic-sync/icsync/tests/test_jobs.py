import os
import unittest
from unittest import mock

from icsync import jobs


class PureRules(unittest.TestCase):
    def test_effective_chunk(self):
        self.assertEqual(jobs.effective_chunk({"max_rows": 300, "chunk_size": 50000}), 300)
        self.assertEqual(jobs.effective_chunk({"max_rows": 2000000, "chunk_size": 50000}), 50000)

    def test_max_items(self):
        self.assertEqual(jobs.max_items_for(1000, 500), 2)
        self.assertEqual(jobs.max_items_for(300, 300), 1)
        self.assertEqual(jobs.max_items_for(10, 500), 1)

    def test_budget(self):
        now = [0.0]
        seen = []

        def run_one(t, rem):
            seen.append((t["warehouse_table"], rem))
            now[0] += 6
            return {"table": t["warehouse_table"], "status": "ok"}

        tabs = [{"warehouse_table": n} for n in "abc"]
        out = jobs.run_with_budget(tabs, 10, run_one, clock=lambda: now[0])
        self.assertEqual(seen, [("a", 10), ("b", 4)])
        self.assertEqual(out[2]["status"], "skipped")
        self.assertTrue(out[2]["capped"])
        self.assertEqual(out[2]["reconcile_note"], "run time budget spent")

    def test_env(self):
        jobs.dlt_env()
        self.assertEqual(os.environ["SCHEMA__NAMING"], "direct")
        self.assertEqual(os.environ["DESTINATION__POSTGRES__REPLACE_STRATEGY"], "insert-from-staging")
        self.assertEqual(os.environ["RUNTIME__DLTHUB_TELEMETRY"], "false")
        self.assertEqual(os.environ["LOAD__WORKERS"], "2")

    def test_stalled(self):
        self.assertTrue(jobs.stalled(True, 0, "x", "x"))
        self.assertFalse(jobs.stalled(True, 5, "x", "x"))
        self.assertFalse(jobs.stalled(True, 0, "x", "y"))
        self.assertFalse(jobs.stalled(False, 0, "x", "x"))
        self.assertTrue(jobs.stalled(True, 0, None, None))
        self.assertIn("backfill cannot advance", jobs.STALL_ERROR)

    def test_hints(self):
        h = jobs.table_hints({"cursor": "u", "merge_key": ["id"], "initial_value": None})
        self.assertEqual(h["write_disposition"], {"disposition": "merge", "strategy": "delete-insert"})
        self.assertEqual(h["primary_key"], ["id"])
        self.assertEqual(h["incremental"]["row_order"], "asc")
        self.assertEqual(jobs.table_hints({"cursor": None})["write_disposition"], "replace")
        with self.assertRaises(ValueError):
            jobs.table_hints({"cursor": "u", "merge_key": []})

    def test_classify(self):
        class DataValidationError(Exception):
            pass

        class PipelineStepFailed(Exception):
            pass

        try:
            try:
                raise DataValidationError()
            except DataValidationError as e:
                raise PipelineStepFailed() from e
        except PipelineStepFailed as outer:
            self.assertEqual(jobs.classify_error(outer), "DataValidationError")
        self.assertEqual(jobs.classify_error(KeyError()), "KeyError")

    def test_mssql_rowversion_read_as_bigint(self):
        try:
            import sqlalchemy as sa
            from sqlalchemy.dialects import mssql
        except ImportError:
            self.skipTest("sqlalchemy not installed")
        t = sa.Table("orders", sa.MetaData(), sa.Column("id", sa.Integer), sa.Column("rv", mssql.TIMESTAMP()))
        table_adapter, query_adapter = jobs.mssql_rowversion_adapters()
        t = table_adapter(t)
        self.assertIsInstance(t.c.rv.type, sa.BigInteger)
        sql = str(query_adapter(sa.select(t).where(t.c.rv >= 5), t).compile(dialect=mssql.dialect()))
        self.assertIn("CAST(orders.rv AS BIGINT) AS rv", sql)
        self.assertIn("WHERE orders.rv >=", sql)
        plain = sa.Table("plain", sa.MetaData(), sa.Column("id", sa.Integer))
        q = sa.select(plain)
        self.assertIs(jobs.mssql_rowversion_adapters()[1](q, table_adapter(plain)), q)

    def test_status(self):
        s = lambda *x: jobs.run_status([{"status": v} for v in x])  # noqa: E731
        self.assertEqual(s("ok", "failed"), "partial")
        self.assertEqual(s("ok", "ok"), "ok")
        self.assertEqual(s("failed", "failed"), "failed")
        self.assertEqual(s("ok", "skipped"), "ok")
        self.assertEqual(s("failed", "skipped"), "failed")


if __name__ == "__main__":
    unittest.main()


class ResetRun(unittest.TestCase):
    def _run(self, reset):
        calls = []
        before = []

        class Pipe:
            def run(self, r, **kw):
                calls.append(kw)

            def sync_destination(self):
                pass

        t = {"remote_table": "o", "warehouse_table": "o", "cursor": "id", "merge_key": ["id"],
             "reconcile": False, "initial_value": None, "reset": reset}
        job = {"kind": "postgres", "tables": [t], "reconcile": False, "dataset": "d",
               "caps": {"max_rows": 100, "max_seconds": 60, "chunk_size": 10}}

        def cs(pipe, name):
            before.append(1)
            return 5 if len(before) == 1 else 3

        with mock.patch.object(jobs, "dlt_env"), mock.patch.object(jobs, "_pipeline", return_value=Pipe()),                 mock.patch.object(jobs, "_resource", return_value=(None, {"chunks": 0, "last": 0})),                 mock.patch.object(jobs, "_row_count", return_value=3),                 mock.patch.object(jobs, "_cursor_state", side_effect=cs),                 mock.patch.object(jobs, "_analyze"),                 mock.patch("icsync.conn.source_engine", return_value=None):
            res = jobs.run_sync(job, {})
        return calls, res

    def test_reset_drops_resources_only_for_flagged_table(self):
        calls, res = self._run(True)
        self.assertEqual(calls, [{"loader_file_format": "csv", "refresh": "drop_resources"}])
        self.assertEqual(res["status"], "ok")

    def test_no_reset_has_no_refresh(self):
        calls, _ = self._run(False)
        self.assertEqual(calls, [{"loader_file_format": "csv"}])
