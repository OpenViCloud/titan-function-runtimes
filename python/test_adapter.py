"""Unit tests of what is only Python's: how a handler is found, and which values are a response. The HTTP and cron
behaviour is in tests/contract/contract_test.py, which runs the same cases against every runtime."""
import errno
import os
import socket
import sys
import tempfile
import unittest
from unittest import mock

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
        with open(full, "w") as f:
            f.write(text)

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
                    {"statusCode": 99}, {"statusCode": 100}, {"statusCode": 199}, {"statusCode": 600},
                    {"statusCode": 200, "headers": {"a": "é"}}, {"statusCode": 200, "headers": {"a": "\x01"}},
                    {"statusCode": 200, "isBase64Encoded": True, "body": "QU\nJD"}, {"statusCode": 200, "headers": []},
                    {"statusCode": 200, "headers": {"a": 1}}, {"statusCode": 200, "headers": {"a b": "x"}},
                    {"statusCode": 200, "headers": {"a": "x\ny"}}, {"statusCode": 200, "body": b"bytes"},
                    {"statusCode": 200, "isBase64Encoded": True, "body": "***"}):
            with self.assertRaises(adapter.InvalidResponse, msg=repr(bad)):
                adapter.to_response(bad)

    def test_an_int_subclass_is_a_status_but_a_bool_is_not(self):
        import http
        self.assertEqual(adapter.to_response({"statusCode": http.HTTPStatus.OK})[0], 200)
        with self.assertRaises(adapter.InvalidResponse):
            adapter.to_response({"statusCode": True})

    def test_a_coroutine_is_refused_and_closed(self):
        async def handler():
            return "x"

        coroutine = handler()
        with self.assertRaises(adapter.InvalidResponse):
            adapter.to_response(coroutine)
        self.assertIsNone(coroutine.cr_frame, "the coroutine was closed, so it does not warn that it was never awaited")

    def test_one_header_per_name_the_last_wins(self):
        _, headers, _ = adapter.to_response({"statusCode": 200, "headers": {"X-A": "1", "x-a": "2", "Server": "mine"}})
        self.assertEqual(headers, [("x-a", "2")])

    def test_base64_only_when_the_flag_is_exactly_true(self):
        self.assertEqual(adapter.to_response({"statusCode": 200, "isBase64Encoded": True, "body": "AP8="})[2], b"\x00\xff")
        self.assertEqual(adapter.to_response({"statusCode": 200, "isBase64Encoded": "yes", "body": "AP8="})[2], b"AP8=")


class SplitTarget(unittest.TestCase):
    def test_targets(self):
        for target, want in {"/a/b?x=1": ("/a/b", "x=1"), "//x/y?q=1": ("/x/y", "q=1"), "http://h/abs": ("/abs", ""),
                             "http://h": ("/", ""), "//": ("/", ""), "/a%2Fb/../c?": ("/a%2Fb/../c", "")}.items():
            self.assertEqual(adapter.split_target(target), want, target)


class Listening(unittest.TestCase):
    def test_a_kernel_without_ipv6_gets_an_ipv4_socket(self):
        real = socket.socket

        def no_ipv6(family=-1, *args, **kwargs):
            if family == socket.AF_INET6:
                raise OSError(errno.EAFNOSUPPORT, "no IPv6")
            return real(family, *args, **kwargs)

        with mock.patch("socketserver.socket.socket", side_effect=no_ipv6):
            server = adapter.Server(0, lambda e, c: "x")
        self.addCleanup(server.server_close)
        self.assertEqual(server.socket.family, socket.AF_INET)

    def test_with_ipv6_one_socket_takes_both_families(self):
        server = adapter.Server(0, lambda e, c: "x")
        self.addCleanup(server.server_close)
        self.assertEqual(server.socket.family, socket.AF_INET6)
        self.assertEqual(server.socket.getsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY), 0)


class PortInUse(unittest.TestCase):
    def test_a_port_that_is_taken_is_an_error_not_a_reason_to_try_another_family(self):
        # v6-only: a dual-stack holder would make the IPv4 attempt fail too, and the test would pass with a fallback in the code
        holder = socket.socket(socket.AF_INET6)
        self.addCleanup(holder.close)
        holder.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        holder.bind(("::", 0))
        holder.listen()
        with self.assertRaises(OSError):
            adapter.Server(holder.getsockname()[1], lambda e, c: "x")


class Context(unittest.TestCase):
    def test_remaining_time_is_none_without_a_deadline_and_never_negative(self):
        self.assertIsNone(adapter.Context("f", "v", "r", None).get_remaining_time_in_millis())
        self.assertEqual(adapter.Context("f", "v", "r", 0.0).get_remaining_time_in_millis(), 0)


if __name__ == "__main__":
    unittest.main()
