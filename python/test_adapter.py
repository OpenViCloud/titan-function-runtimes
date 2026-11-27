"""Unit tests of what is only Python's: how a handler is found, and which values are a response. The HTTP and cron
behaviour is in tests/contract/contract_test.py, which runs the same cases against every runtime."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import adapter  # noqa: E402


class LoadHandler(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.addCleanup(lambda: [sys.modules.pop(m, None) for m in ("mod_a", "pkg", "pkg.inner")])

    def write(self, rel, text):
        full = os.path.join(self.dir.name, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        open(full, "w").write(text)

    def test_a_module_in_the_code_dir(self):
        self.write("mod_a.py", "def handler(e, c):\n    return 'a'\n")
        self.assertEqual(adapter.load_handler("mod_a.handler", self.dir.name)({}, None), "a")

    def test_a_module_in_a_package_splits_on_the_last_dot(self):
        self.write("pkg/__init__.py", "")
        self.write("pkg/inner.py", "def go(e, c):\n    return 'inner'\n")
        self.assertEqual(adapter.load_handler("pkg.inner.go", self.dir.name)({}, None), "inner")

    def test_what_is_not_found_raises(self):
        self.write("mod_a.py", "VALUE = 1\n")
        for spec in ("nomodule.handler", "mod_a.nohandler", "mod_a.VALUE", "handler", ".handler", "mod_a."):
            with self.assertRaises(Exception, msg=spec):
                adapter.load_handler(spec, self.dir.name)


class ToResponse(unittest.TestCase):
    def test_a_string(self):
        self.assertEqual(adapter.to_response("é"), (200, [("Content-Type", "text/plain; charset=utf-8")], "é".encode()))

    def test_a_dict_without_headers_or_body(self):
        self.assertEqual(adapter.to_response({"statusCode": 204}), (204, [], b""))

    def test_framing_headers_are_dropped_in_any_case(self):
        _, headers, _ = adapter.to_response({"statusCode": 200, "headers": {"Content-Length": "9", "TRANSFER-ENCODING": "x", "X-Ok": "1"}})
        self.assertEqual(headers, [("X-Ok", "1")])

    def test_bad_values(self):
        for bad in (None, 5, [], b"bytes", {}, {"statusCode": "200"}, {"statusCode": 200.0}, {"statusCode": True},
                    {"statusCode": 99}, {"statusCode": 600}, {"statusCode": 200, "headers": []},
                    {"statusCode": 200, "headers": {"a": 1}}, {"statusCode": 200, "headers": {"a b": "x"}},
                    {"statusCode": 200, "headers": {"a": "x\ny"}}, {"statusCode": 200, "body": b"bytes"},
                    {"statusCode": 200, "isBase64Encoded": True, "body": "***"}):
            with self.assertRaises(adapter.InvalidResponse, msg=repr(bad)):
                adapter.to_response(bad)

    def test_base64_only_when_the_flag_is_exactly_true(self):
        self.assertEqual(adapter.to_response({"statusCode": 200, "isBase64Encoded": True, "body": "AP8="})[2], b"\x00\xff")
        self.assertEqual(adapter.to_response({"statusCode": 200, "isBase64Encoded": "yes", "body": "AP8="})[2], b"AP8=")


class Context(unittest.TestCase):
    def test_remaining_time_is_none_without_a_deadline_and_never_negative(self):
        self.assertIsNone(adapter.Context("f", "v", "r", None).get_remaining_time_in_millis())
        self.assertEqual(adapter.Context("f", "v", "r", 0.0).get_remaining_time_in_millis(), 0)


if __name__ == "__main__":
    unittest.main()
