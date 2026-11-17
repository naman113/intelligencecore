import unittest

from icsync.conn import rewrite_loopback
from icsync.probe import (canonical_mssql_type, canonical_mysql_type, cursor_candidates,
                          mysql_grants_read_only, sample_value)

try:
    import sqlalchemy  # noqa: F401
    HAVE_SA = True
except ImportError:
    HAVE_SA = False


class RuleTests(unittest.TestCase):
    def test_grants(self):
        self.assertTrue(mysql_grants_read_only(["GRANT SELECT ON `ops`.* TO `src_ro`@`%`"]))
        self.assertFalse(mysql_grants_read_only(["GRANT SELECT, INSERT ON `ops`.* TO `u`@`%`"]))
        self.assertFalse(mysql_grants_read_only(["GRANT ALL PRIVILEGES ON *.* TO `u`@`%`"]))
        self.assertTrue(mysql_grants_read_only(["GRANT USAGE ON *.* TO `u`@`%`",
                                                "GRANT SELECT, CREATE TEMPORARY TABLES ON `d`.* TO `u`@`%`"]))

    def test_grants_quick028(self):
        ro = "GRANT SELECT ON `d`.* TO `u`@`%`"
        for bad in ("GRANT `app_writer`@`%` TO `u`@`%`",
                    "GRANT SELECT, UPDATE (name, note) ON `d`.`t` TO `u`@`%`",
                    "GRANT SELECT, INSERT (a) ON `d`.* TO `u`@`%`",
                    "GRANT SELECT, TRIGGER ON `d`.* TO `u`@`%`",
                    "GRANT SELECT, EVENT ON `d`.* TO `u`@`%`",
                    "GRANT CREATE ROUTINE ON `d`.* TO `u`@`%`",
                    "GRANT ALTER ROUTINE, SELECT ON `d`.* TO `u`@`%`",
                    "GRANT EXECUTE ON `d`.* TO `u`@`%`",
                    "GRANT FILE ON *.* TO `u`@`%`",
                    "GRANT SELECT, INDEX ON `d`.* TO `u`@`%`",
                    "GRANT SELECT ON `d`.* TO `u`@`%` WITH GRANT OPTION"):
            self.assertFalse(mysql_grants_read_only([ro, bad]), bad)
        # column-level SELECT with a comma list is still read-only
        self.assertTrue(mysql_grants_read_only(["GRANT SELECT (a, b), SHOW VIEW ON `d`.`t` TO `u`@`%`"]))
        self.assertTrue(mysql_grants_read_only([ro, "GRANT USAGE ON *.* TO `u`@`%`"]))

    def test_cursor_candidates(self):
        cols = [{"name": "id", "type": "integer"}, {"name": "created", "type": "timestamp without time zone"},
                {"name": "updated_at", "type": "timestamp with time zone"}, {"name": "rv", "type": "rowversion"},
                {"name": "d", "type": "date"}]
        out = cursor_candidates(cols, [["updated_at", "x"], ["id"]])
        self.assertEqual(out[0]["column"], "updated_at")
        self.assertTrue(out[0]["indexed"])
        self.assertEqual({c["column"] for c in out}, {"created", "updated_at", "rv", "d"})
        self.assertFalse(next(c for c in out if c["column"] == "created")["indexed"])
        self.assertNotIn("id", {c["column"] for c in out})

    def test_index_only_when_first_column(self):
        cols = [{"name": "updated_at", "type": "datetime"}]
        self.assertFalse(cursor_candidates(cols, [["id", "updated_at"]])[0]["indexed"])

    def test_mssql_types(self):
        self.assertEqual(canonical_mssql_type("nvarchar", -1, 0, 0), "nvarchar(max)")
        self.assertEqual(canonical_mssql_type("nvarchar", 100, 0, 0), "nvarchar(50)")
        self.assertEqual(canonical_mssql_type("varchar", 20, 0, 0), "varchar(20)")
        self.assertEqual(canonical_mssql_type("decimal", 9, 12, 2), "decimal(12,2)")
        self.assertEqual(canonical_mssql_type("datetime2", 8, 27, 7), "datetime2(7)")
        self.assertEqual(canonical_mssql_type("int", 4, 10, 0), "int")

    def test_mysql_type(self):
        self.assertEqual(canonical_mysql_type("VARCHAR(20)"), "varchar(20)")

    def test_sample_value(self):
        self.assertIsNone(sample_value(None))
        self.assertEqual(len(sample_value("x" * 500)), 60)
        self.assertEqual(sample_value(b"\x01\x02"), "0102")
        self.assertEqual(len(sample_value(b"\xff" * 100)), 60)

    def test_loopback(self):
        for h in ("localhost", "127.0.0.1", "::1"):
            self.assertEqual(rewrite_loopback(h), "host.docker.internal")
        self.assertEqual(rewrite_loopback("db.example.com"), "db.example.com")


@unittest.skipUnless(HAVE_SA, "sqlalchemy not installed")
class UrlTests(unittest.TestCase):
    def test_urls(self):
        from icsync.conn import source_url
        env = {"IC_SRC_HOST": "localhost", "IC_SRC_PORT": "1433", "IC_SRC_DB": "d",
               "IC_SRC_USER": "u", "IC_SRC_PASSWORD": "p@ss:/w"}
        u = source_url("mssql", env)
        self.assertEqual(u.drivername, "mssql+pyodbc")
        self.assertEqual(u.host, "host.docker.internal")
        self.assertEqual(u.password, "p@ss:/w")
        self.assertEqual(u.query["driver"], "ODBC Driver 18 for SQL Server")
        self.assertEqual(source_url("postgres", env).drivername, "postgresql+psycopg2")
        self.assertEqual(source_url("mysql", env).drivername, "mysql+pymysql")
        self.assertEqual(source_url("postgres", env).query["connect_timeout"], "15")


if __name__ == "__main__":
    unittest.main()
