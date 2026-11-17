import io
import unittest

try:
    import pyarrow as pa
except ImportError:
    pa = None

from icsync import csvload


@unittest.skipIf(pa is None, "pyarrow missing")
class Csv(unittest.TestCase):
    def test_arrow_to_pg(self):
        c = csvload.arrow_to_pg
        self.assertEqual(c(pa.int64()), "bigint")
        self.assertEqual(c(pa.float64()), "double precision")
        self.assertEqual(c(pa.string()), "text")
        self.assertEqual(c(pa.large_string()), "text")
        self.assertEqual(c(pa.bool_()), "boolean")
        self.assertEqual(c(pa.date32()), "date")
        self.assertEqual(c(pa.timestamp("s")), "timestamp without time zone")
        self.assertEqual(c(pa.timestamp("s", tz="UTC")), "timestamp with time zone")
        self.assertEqual(c(pa.decimal128(12, 2)), "numeric(12,2)")
        self.assertEqual(c(pa.binary()), "text")

    def test_type_roundtrip(self):
        for t in (pa.int64(), pa.float64(), pa.string(), pa.bool_(), pa.date32(), pa.timestamp("s"),
                  pa.timestamp("us", tz="UTC"), pa.decimal128(12, 2)):
            self.assertEqual(csvload.type_from_name(csvload.type_name(t)), t)

    def test_header(self):
        csvload.header_ok(["a", "b"])
        for bad in ([""], ["a", "a"], ["x" * 64], ["a\x00"], ["c%d" % i for i in range(1001)]):
            with self.assertRaises(ValueError):
                csvload.header_ok(bad)

    def test_validate(self):
        with self.assertRaises(ValueError):
            csvload._validate({"delimiter": "x"})
        with self.assertRaises(ValueError):
            csvload._validate({"encoding": "latin-1"})
        csvload._validate({"delimiter": ";", "encoding": "utf-8"})

    def test_batches_cap_and_bom(self):
        import pyarrow.csv as pacsv
        data = "\ufeffa,b\n" + "".join(f"{i},x\n" for i in range(50))
        r = pacsv.open_csv(io.BytesIO(data.encode("utf-8")))
        self.assertEqual(r.schema.names, ["a", "b"])
        n = sum(t.num_rows for t in csvload._batches(r, 10))
        self.assertEqual(n, 10)


if __name__ == "__main__":
    unittest.main()
