import json
import unittest

from icsync.spec import SpecError, parse_job


def J(**kw):
    d = {"v": 1, "kind": "mysql", "source": "probe"}
    d.update(kw)
    return json.dumps(d)


class SpecTests(unittest.TestCase):
    def test_defaults(self):
        j = parse_job("check", J())
        self.assertEqual(j["kind"], "mysql")
        self.assertEqual(j["tables"], [])
        self.assertEqual(j["caps"]["chunk_size"], 50_000)

    def test_invalid_json(self):
        with self.assertRaises(SpecError):
            parse_job("check", "{nope")

    def test_bad_version_and_kind(self):
        with self.assertRaises(SpecError):
            parse_job("check", json.dumps({"v": 2, "kind": "mysql"}))
        with self.assertRaises(SpecError):
            parse_job("check", J(kind="oracle"))

    def test_too_many_tables(self):
        t = [{"remote_table": "a", "warehouse_table": "a"}] * 501
        with self.assertRaises(SpecError):
            parse_job("sync", J(tables=t))

    def test_identifier_limits(self):
        for bad in ("x" * 64, "a\x00b", "é" * 40):
            with self.assertRaises(SpecError):
                parse_job("sync", J(tables=[{"remote_table": bad, "warehouse_table": "t"}]))

    def test_caps(self):
        for caps in ({"max_rows": 0}, {"max_rows": -1}, {"max_rows": 50_000_001},
                     {"max_seconds": 21_601}, {"chunk_size": 500_001}):
            with self.assertRaises(SpecError):
                parse_job("sync", J(caps=caps))
        ok = parse_job("sync", J(caps={"max_rows": 50_000_000, "max_seconds": 21_600, "chunk_size": 500_000}))
        self.assertEqual(ok["caps"]["max_rows"], 50_000_000)

    def test_row_limit(self):
        with self.assertRaises(SpecError):
            parse_job("test", J(row_limit=1001))
        self.assertEqual(parse_job("test", J(row_limit=1000))["row_limit"], 1000)

    def test_message_has_no_input_value(self):
        try:
            parse_job("check", J(kind="SECRETKIND"))
        except SpecError as e:
            self.assertNotIn("SECRETKIND", str(e))


if __name__ == "__main__":
    unittest.main()

    def test_reset_flag(self):
        t = {"remote_table": "a", "warehouse_table": "a"}
        j = parse_job("sync", J(tables=[t, dict(t, reset=True)]))
        self.assertFalse(j["tables"][0]["reset"])
        self.assertTrue(j["tables"][1]["reset"])
        with self.assertRaises(SpecError):
            parse_job("sync", J(tables=[dict(t, reset="yes")]))
