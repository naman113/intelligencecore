import unittest

from icsync import reconcile


class Decide(unittest.TestCase):
    def test_rules(self):
        self.assertEqual(reconcile.decide(0, 10), (False, "reconcile skipped: no source keys"))
        self.assertEqual(reconcile.decide(4, 10),
                         (False, "reconcile skipped: key count dropped by more than half"))
        self.assertEqual(reconcile.decide(5, 10), (True, None))
        self.assertEqual(reconcile.decide(0, 0), (True, None))

    def test_delete_uses_identifiers(self):
        try:
            from psycopg2 import sql
        except ImportError:
            self.skipTest("psycopg2 missing")
        q = reconcile.build_delete('da"ta', "t", ["id", "b"])
        self.assertIsInstance(q, sql.Composed)
        txt = repr(q)
        self.assertIn("Identifier", txt)


if __name__ == "__main__":
    unittest.main()
