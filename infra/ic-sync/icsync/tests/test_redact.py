import io
import json
import unittest
from contextlib import redirect_stdout

from icsync import __main__ as m
from icsync.redact import clip, env_secrets, error_text, redact

ENV = {"IC_SYNC_JOB": json.dumps({"v": 1, "kind": "postgres"}), "IC_SRC_PASSWORD": "hunter2hunter2"}


def run(argv, env=ENV):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = m.main(argv, env)
    return code, buf.getvalue().splitlines()


class RedactTests(unittest.TestCase):
    def test_literal_and_patterns(self):
        t = "failed password=hunter2hunter2 at mysql://u:hunter2hunter2@h/db"
        self.assertNotIn("hunter2hunter2", redact(t, ["hunter2hunter2"]))
        self.assertNotIn("abc12345", redact("Password=abc12345;Server=x", []))
        self.assertNotIn("abc12345", redact("Server=x;PWD=abc12345;", []))
        self.assertNotIn("zz99zz99", redact("postgres://u:zz99zz99@h/db", []))

    def test_short_secret_ignored(self):
        self.assertEqual(redact("a bc d", ["bc"]), "a bc d")

    def test_clip(self):
        self.assertEqual(len(clip("x" * 1000)), 300)

    def test_error_text(self):
        e = error_text(ValueError("boom hunter2hunter2\nsecond"), ["hunter2hunter2"])
        self.assertTrue(e.startswith("ValueError: boom"))
        self.assertNotIn("hunter2", e)
        self.assertNotIn("second", e)

    def test_env_secrets(self):
        s = env_secrets({"IC_SRC_PASSWORD": "a1b2c3", "IC_WH_PASSWORD": "d4e5f6",
                         "IC_SRC_OPTIONS": json.dumps({"password": "g7h8i9"})})
        self.assertEqual(set(s), {"a1b2c3", "d4e5f6", "g7h8i9"})


class MainTests(unittest.TestCase):
    def setUp(self):
        self._saved = dict(m.HANDLERS)

    def tearDown(self):
        m.HANDLERS.clear()
        m.HANDLERS.update(self._saved)

    def test_ok(self):
        m.HANDLERS["check"] = lambda job, env: {"status": "ok", "read_only": True}
        code, lines = run(["check"])
        self.assertEqual(code, 0)
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("IC_SYNC_RESULT "))
        r = json.loads(lines[0][len("IC_SYNC_RESULT "):])
        for k in ("status", "error", "duration_ms", "peak_memory_mb"):
            self.assertIn(k, r)

    def test_exception_redacted(self):
        def boom(job, env):
            raise RuntimeError("cannot login hunter2hunter2")
        m.HANDLERS["check"] = boom
        code, lines = run(["check"])
        self.assertEqual(code, 1)
        r = json.loads(lines[0][len("IC_SYNC_RESULT "):])
        self.assertEqual(r["status"], "failed")
        self.assertTrue(r["error"].startswith("RuntimeError: "))
        self.assertNotIn("hunter2hunter2", lines[0])

    def test_partial(self):
        m.HANDLERS["sync"] = lambda job, env: {"status": "partial"}
        self.assertEqual(run(["sync"])[0], 2)

    def test_unknown(self):
        code, lines = run(["nope"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(lines[0][len("IC_SYNC_RESULT "):])["error"], "unknown command")

    def test_bad_job_fails_before_handler(self):
        called = []
        m.HANDLERS["check"] = lambda job, env: called.append(1) or {"status": "ok"}
        code, _ = run(["check"], {"IC_SYNC_JOB": "{bad"})
        self.assertEqual(code, 1)
        self.assertFalse(called)

    def test_peak_memory_type(self):
        v = m.peak_memory_mb()
        self.assertTrue(v is None or isinstance(v, int))


if __name__ == "__main__":
    unittest.main()
