#!/usr/bin/env python3
"""The contract of a runtime (titan-ops features/ovc-function/design.md#runtimes), run against every adapter.

One list of cases, three runtimes: each class below starts the adapter of one language on a fixture handler with the
same behaviours (fixtures/python/main.py, fixtures/node/index.mjs, fixtures/go/main.go), talks to it over HTTP or as a
cron process, and compares what comes back. A language that drifts from the contract fails the case it drifts on.

Run:  python3 tests/contract/contract_test.py [-k Python]      (needs python3, node 22+ and go)
"""
import base64
import http.client
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
FIXTURES = os.path.join(HERE, "fixtures")
MAX_BODY = 6 * 1024 * 1024
INVALID_RETURNS = 13  # entries of INVALID in fixtures/python/main.py and fixtures/node/index.mjs


class Process:
    """An adapter process: its output is collected, its port is the one we picked."""

    def __init__(self, argv, env, cwd):
        self.port = _free_port()
        full = {"PATH": os.environ["PATH"], "PORT": str(self.port), **env}
        self.out, self.err = [], []
        self.proc = subprocess.Popen(argv, env=full, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self._threads = [threading.Thread(target=_pump, args=(self.proc.stdout, self.out), daemon=True),
                         threading.Thread(target=_pump, args=(self.proc.stderr, self.err), daemon=True)]
        for t in self._threads:
            t.start()

    def wait_port(self, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            if self.proc.poll() is not None:
                raise AssertionError(f"adapter exited {self.proc.returncode} before listening; stderr: {self.stderr()}")
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.2).close()
                return
            except OSError:
                time.sleep(0.05)
        raise AssertionError("adapter did not listen in time")

    def listening(self):
        try:
            socket.create_connection(("127.0.0.1", self.port), timeout=0.2).close()
            return True
        except OSError:
            return False

    def wait(self, timeout):
        try:
            code = self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            raise AssertionError(f"adapter still running after {timeout} s; stderr: {self.stderr()}")
        for t in self._threads:
            t.join(2)
        return code

    def stdout(self):
        return "".join(self.out)

    def stderr(self):
        return "".join(self.err)

    def kill(self):
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        for t in self._threads:
            t.join(2)
        self.proc.stdout.close()
        self.proc.stderr.close()


def _pump(stream, sink):
    for line in stream:
        sink.append(line)


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Response:
    def __init__(self, status, headers, body):
        self.status, self.headers, self.body = status, headers, body

    def json(self):
        return json.loads(self.body)


def request(port, method, path, body=None, headers=None, chunked=False, timeout=15):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request(method, path, body=body, headers=headers or {}, encode_chunked=chunked)
        r = conn.getresponse()
        return Response(r.status, {k.lower(): v for k, v in r.getheaders()}, r.read())
    finally:
        conn.close()


class Contract:
    """Cases every runtime passes. A subclass names the runtime and how to start it."""

    runtime = ""
    dynamic = True  # a handler can return a value of the wrong shape (Python, Node.js); a typed Go one cannot

    def argv(self):
        raise NotImplementedError

    def code_dir(self):
        return os.path.join(FIXTURES, self.runtime)

    def start(self, **env):
        p = Process(self.argv(), {"TITAN_FUNCTION_DIR": self.code_dir(), "TITAN_HANDLER": self.handler_name(), **env},
                    cwd=self.code_dir())
        self.addCleanup(p.kill)
        return p

    def handler_name(self):
        return "main.handler" if self.runtime == "python" else "index.handler"

    def serve(self, **env):
        p = self.start(TITAN_TRIGGER="http", **env)
        p.wait_port()
        return p

    # --- what the function returns -------------------------------------------------------------

    def test_status_headers_body_come_back_as_returned(self):
        p = self.serve()
        r = request(p.port, "GET", "/status?code=201")
        self.assertEqual((r.status, r.headers.get("x-a"), r.body), (201, "b", b"ok"))
        self.assertNotIn("content-type", r.headers, "a response without a content type is sent without one, in every runtime")

    def test_204_and_304_carry_no_body_and_head_none(self):
        p = self.serve()
        for code in (204, 304):
            r = request(p.port, "GET", f"/status?code={code}")
            self.assertEqual((r.status, r.body), (code, b""))
            if code == 204:
                self.assertNotIn("content-length", r.headers)
        r = request(p.port, "HEAD", "/status?code=200")
        self.assertEqual((r.status, r.body), (200, b""))

    def test_a_base64_response_is_decoded(self):
        p = self.serve()
        r = request(p.port, "GET", "/b64")
        self.assertEqual((r.status, r.body), (200, b"\x00\xffAB"))

    def test_a_string_is_a_200_text_plain(self):
        p = self.serve()
        r = request(p.port, "GET", "/string")
        self.assertEqual((r.status, r.headers["content-type"], r.body), (200, "text/plain; charset=utf-8", b"hello"))

    def test_a_thrown_error_is_a_generic_500_and_its_stack_goes_to_stderr(self):
        p = self.serve()
        r = request(p.port, "GET", "/throw")
        self.assertEqual(r.status, 500)
        self.assertNotIn(b"boom-secret", r.body)
        self.assertEqual(request(p.port, "GET", "/string").status, 200, "the adapter must survive a throwing handler")
        self.assertIn("boom-secret", _wait_for(lambda: p.stderr(), "boom-secret"))

    def test_invalid_returns_are_500_and_the_adapter_lives_on(self):
        p = self.serve()
        for n in range(INVALID_RETURNS if self.dynamic else 2):
            r = request(p.port, "GET", f"/bad?n={n}")
            self.assertEqual(r.status, 500, f"case {n}: {r.body!r}")
            self.assertNotIn(b"x-evil", r.body)
        self.assertEqual(request(p.port, "GET", "/string").body, b"hello")

    def test_a_header_that_is_not_visible_ascii_or_has_a_bad_name_is_a_500_not_a_split_response(self):
        p = self.serve()
        for path in ("/crlf", "/badhead", "/nonascii"):
            r = request(p.port, "GET", path)
            self.assertEqual(r.status, 500, path)
            self.assertNotIn("x-evil", r.headers)

    def test_base64_with_a_line_break_is_a_500(self):
        p = self.serve()
        self.assertEqual(request(p.port, "GET", "/b64nl").status, 500)

    def test_a_date_set_by_the_function_is_sent_once(self):
        p = self.serve()
        conn = http.client.HTTPConnection("127.0.0.1", p.port, timeout=10)
        conn.request("GET", "/date")
        r = conn.getresponse()
        dates = [v for k, v in r.getheaders() if k.lower() == "date"]
        r.read()
        conn.close()
        self.assertEqual(dates, ["Thu, 01 Jan 1970 00:00:00 GMT"])

    def test_the_adapter_owns_the_framing_headers(self):
        p = self.serve()
        r = request(p.port, "GET", "/framing")
        self.assertEqual((r.status, r.body, r.headers.get("x-ok")), (200, b"hello", "1"))
        self.assertEqual(r.headers["content-length"], "5")
        self.assertNotEqual(r.headers.get("connection"), "close", "a customer cannot close the connection for the adapter")
        self.assertNotIn("server", r.headers, "the Server header is the adapter's, and the adapter sends none")

    # --- the event --------------------------------------------------------------------------------

    def test_the_event_carries_method_path_query_headers_and_body(self):
        p = self.serve()
        r = request(p.port, "POST", "/echo?a=1&a=2&b=", body="héllo".encode(), headers={"X-Custom": "V", "Content-Type": "text/x"})
        got = r.json()["event"]
        self.assertEqual(got["trigger"], "http")
        self.assertEqual((got["method"], got["path"]), ("POST", "/echo"))
        self.assertEqual(got["query"], {"a": "1,2", "b": ""})
        self.assertEqual((got["headers"]["x-custom"], got["headers"]["content-type"]), ("V", "text/x"))
        self.assertEqual((got["body"], got["isBase64Encoded"]), ("héllo", False))

    def test_an_empty_request_has_an_empty_body_and_query(self):
        p = self.serve()
        got = request(p.port, "GET", "/echo").json()["event"]
        self.assertEqual((got["body"], got["isBase64Encoded"], got["query"]), ("", False, {}))

    def test_the_path_is_the_raw_path_without_the_query(self):
        p = self.serve()
        got = request(p.port, "GET", "/echo?x=%41/..%2F").json()["event"]
        self.assertEqual(got["path"], "/echo")
        self.assertEqual(got["query"], {"x": "A/../"})

    def test_the_target_is_reduced_to_its_path(self):
        p = self.serve()
        for target, want in (("//x/y?q=1", "/x/y"), ("http://h/abs?q=1", "/abs"), ("//", "/"), ("/a%2Fb/../c", "/a%2Fb/../c")):
            s = socket.create_connection(("127.0.0.1", p.port), timeout=10)
            s.sendall(f"GET {target} HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n".encode())
            raw = b""
            while chunk := s.recv(4096):
                raw += chunk
            s.close()
            self.assertTrue(raw.startswith(b"HTTP/1.1 404"), (target, raw))
            self.assertTrue(raw.endswith(("path=" + want).encode()), (target, raw))
        self.assertEqual(request(p.port, "GET", "/string").status, 200, "the adapter must survive any target")

    def test_a_target_that_does_not_parse_is_answered_not_dropped(self):
        p = self.serve()
        s = socket.create_connection(("127.0.0.1", p.port), timeout=10)
        s.sendall(b"GET http://[ HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        self.assertRegex(s.recv(4096), rb"^HTTP/1\.1 4\d\d ")
        s.close()
        self.assertEqual(request(p.port, "GET", "/string").status, 200)

    def test_a_question_mark_after_the_first_belongs_to_the_first_name(self):
        p = self.serve()
        self.assertEqual(request(p.port, "GET", "/echo??x=1").json()["event"]["query"], {"?x": "1"})

    def test_a_content_length_with_leading_zeros_is_its_value(self):
        p = self.serve()
        s = socket.create_connection(("127.0.0.1", p.port), timeout=10)
        s.sendall(b"POST /len HTTP/1.1\r\nHost: x\r\nConnection: close\r\nContent-Length: 00000000000003\r\n\r\nabc")
        raw = b""
        while chunk := s.recv(4096):
            raw += chunk
        s.close()
        self.assertTrue(raw.startswith(b"HTTP/1.1 200") and raw.endswith(b"\r\n\r\n3"), raw)

    def test_a_content_length_of_thousands_of_digits_is_refused_not_dropped(self):
        p = self.serve()
        s = socket.create_connection(("127.0.0.1", p.port), timeout=10)
        s.sendall(b"POST /len HTTP/1.1\r\nHost: x\r\nContent-Length: " + b"1" * 5000 + b"\r\n\r\n")
        self.assertRegex(s.recv(4096), rb"^HTTP/1\.1 4(00|13) ")
        s.close()
        self.assertEqual(request(p.port, "GET", "/string").status, 200)

    def test_the_query_keeps_odd_names_and_odd_escapes(self):
        p = self.serve()
        got = request(p.port, "GET", "/echo?constructor=1&toString=2&__proto__=3&a=1;b=2&c=%zz&d=a+b&e=%zz%41&f=%zz+c&g=%ff%fe").json()["event"]["query"]
        self.assertEqual(got, {"constructor": "1", "toString": "2", "__proto__": "3", "a": "1;b=2", "c": "%zz", "d": "a b", "e": "%zzA", "f": "%zz c", "g": "\ufffd\ufffd"})

    def test_a_utf8_body_keeps_its_byte_order_mark(self):
        p = self.serve()
        got = request(p.port, "POST", "/echo", body=b"\xef\xbb\xbfhi").json()["event"]
        self.assertEqual((got["body"], got["isBase64Encoded"]), ("\ufeffhi", False))

    def test_a_body_that_is_not_utf8_is_base64(self):
        p = self.serve()
        got = request(p.port, "POST", "/echo", body=b"\xff\xfe").json()["event"]
        self.assertEqual((got["body"], got["isBase64Encoded"]), (base64.b64encode(b"\xff\xfe").decode(), True))

    def test_a_chunked_body_arrives_whole(self):
        p = self.serve()
        chunks = [b"ab", b"cde", b"f" * 70000]
        r = request(p.port, "POST", "/len", body=iter(chunks), chunked=True, headers={"Transfer-Encoding": "chunked"})
        self.assertEqual(r.body, str(sum(len(c) for c in chunks)).encode())

    def test_a_body_of_exactly_the_cap_passes(self):
        p = self.serve()
        r = request(p.port, "POST", "/len", body=b"a" * MAX_BODY, timeout=30)
        self.assertEqual((r.status, r.body), (200, str(MAX_BODY).encode()))

    def test_an_idle_keep_alive_connection_stays_open_for_the_peer_to_close(self):
        # queue-proxy reuses connections: one the adapter closed on its own after a few seconds is the classic sporadic 502
        p = self.serve()
        conn = http.client.HTTPConnection("127.0.0.1", p.port, timeout=15)
        self.addCleanup(conn.close)
        conn.request("GET", "/string")
        conn.getresponse().read()
        time.sleep(6.5)
        conn.request("GET", "/string")
        self.assertEqual(conn.getresponse().read(), b"hello")

    def test_a_declared_length_over_the_cap_is_413_without_calling_the_function(self):
        p = self.serve()
        s = socket.create_connection(("127.0.0.1", p.port), timeout=10)
        s.sendall(f"POST /len HTTP/1.1\r\nHost: x\r\nContent-Length: {MAX_BODY + 1}\r\n\r\n".encode())
        self.assertTrue(s.recv(4096).startswith(b"HTTP/1.1 413"))
        s.close()
        self.assertEqual(request(p.port, "GET", "/string").status, 200)
        self.assertNotIn("LEN-CALLED", p.stdout())

    def test_a_chunked_body_over_the_cap_is_413(self):
        p = self.serve()
        chunks = (b"a" * (1 << 20) for _ in range(7))
        r = request(p.port, "POST", "/len", body=chunks, chunked=True, headers={"Transfer-Encoding": "chunked"}, timeout=30)
        self.assertEqual(r.status, 413)
        self.assertEqual(request(p.port, "GET", "/string").status, 200)
        self.assertNotIn("LEN-CALLED", p.stdout())

    def test_a_chunked_request_with_many_trailers_does_not_poison_the_connection(self):
        p = self.serve()
        s = socket.create_connection(("127.0.0.1", p.port), timeout=5)
        trailers = "".join(f"X-T{i}: v\r\n" for i in range(70))
        s.sendall(("POST /len HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n3\r\nabc\r\n0\r\n" + trailers +
                   "\r\nGET /string HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n").encode())
        raw = b""
        try:
            while chunk := s.recv(4096):
                raw += chunk
        except socket.timeout:
            pass
        s.close()
        self.assertTrue(raw.startswith(b"HTTP/1.1 200"), raw)
        self.assertNotIn(b"HTTP/1.1 400", raw, "what follows the trailers is a request, or nothing, never garbage parsed as one")

    def test_a_malformed_request_is_400_and_the_adapter_lives_on(self):
        p = self.serve()
        for head in ("Content-Length: abc", "Content-Length: -5", "Content-Length: \u00b2",
                     "Transfer-Encoding: chunked\r\n\r\nzz\r\nab", "Transfer-Encoding: chunked\r\n\r\n-1\r\nab\r\n0\r\n\r\n",
                     "Transfer-Encoding: chunked\r\n\r\n0x5\r\nhello\r\n0\r\n\r\n", "Transfer-Encoding: chunked\r\n\r\n1_0\r\nabcdefghijklmnop\r\n0\r\n\r\n"):
            s = socket.create_connection(("127.0.0.1", p.port), timeout=10)
            s.sendall(f"POST /len HTTP/1.1\r\nHost: x\r\n{head}\r\n\r\n".encode() if "\r\n\r\n" not in head else
                      f"POST /len HTTP/1.1\r\nHost: x\r\n{head}".encode())
            self.assertTrue(s.recv(4096).startswith(b"HTTP/1.1 400"), head)
            s.close()
        self.assertEqual(request(p.port, "GET", "/string").status, 200)

    def test_a_client_that_hangs_up_mid_body_does_not_reach_the_function_or_stop_the_adapter(self):
        p = self.serve()
        s = socket.create_connection(("127.0.0.1", p.port), timeout=10)
        s.sendall(b"POST /len HTTP/1.1\r\nHost: x\r\nContent-Length: 100\r\n\r\nonly ten b")
        s.close()
        time.sleep(0.3)
        self.assertEqual(request(p.port, "GET", "/string").status, 200)
        self.assertIsNone(p.proc.poll(), "the adapter must still run")
        self.assertNotIn("LEN-CALLED", p.stdout(), "the function must not see a body that never arrived")

    # --- the context ----------------------------------------------------------------------------

    def test_the_context_carries_the_ids_and_the_time_left(self):
        p = self.serve(TITAN_FUNCTION_ID="f-1", TITAN_VERSION_ID="v-1", TITAN_TIMEOUT_SECONDS="10")
        c = request(p.port, "GET", "/echo", headers={"X-Request-Id": "r-1"}).json()["context"]
        self.assertEqual((c["functionId"], c["versionId"], c["requestId"]), ("f-1", "v-1", "r-1"))
        self.assertTrue(0 < c["remainingMs"] <= 10000, c)

    def test_a_request_without_an_id_gets_one_and_each_request_gets_its_own(self):
        p = self.serve()
        a = request(p.port, "GET", "/echo").json()["context"]
        b = request(p.port, "GET", "/echo").json()["context"]
        self.assertTrue(a["requestId"] and b["requestId"])
        self.assertNotEqual(a["requestId"], b["requestId"])
        self.assertIsNone(a["remainingMs"], "without TITAN_TIMEOUT_SECONDS there is no deadline to report")

    def test_a_timeout_that_is_not_a_plain_decimal_means_no_deadline(self):
        for value in ("0", "-5", "abc", "inf", "nan", "", " 5 ", "5_0", "0x10", "1e10", "1e308", ".5", "5.", "+5"):
            p = self.serve(TITAN_TIMEOUT_SECONDS=value)
            self.assertIsNone(request(p.port, "GET", "/echo").json()["context"]["remainingMs"], value)
            p.proc.send_signal(signal.SIGTERM)
            p.wait(5)

    def test_a_very_long_timeout_is_a_long_deadline_not_an_error(self):
        for digits in (30, 400):  # 400 digits is beyond float64
            p = self.serve(TITAN_TIMEOUT_SECONDS="9" * digits)
            left = request(p.port, "GET", "/echo").json()["context"]["remainingMs"]
            self.assertTrue(10**11 < left <= 10**12, (digits, left))
            p.proc.send_signal(signal.SIGTERM)
            p.wait(5)

    def test_a_timeout_below_a_nanosecond_is_a_deadline_that_has_passed(self):
        p = self.serve(TITAN_TIMEOUT_SECONDS="0.0000000001")
        self.assertEqual(request(p.port, "GET", "/echo").json()["context"]["remainingMs"], 0)

    def test_the_time_left_runs_down_while_the_function_runs(self):
        p = self.serve(TITAN_TIMEOUT_SECONDS="10")
        first, second = request(p.port, "GET", "/remaining").json()
        self.assertTrue(first - second >= 250, (first, second))
        self.assertTrue(first <= 10000)

    # --- cron ----------------------------------------------------------------------------------

    def cron(self, **env):
        return self.start(TITAN_TRIGGER="cron", **env)

    def test_cron_calls_the_function_once_with_the_cron_event_and_exits_0(self):
        p = self.cron(TITAN_SCHEDULE="*/5 * * * *", TITAN_SCHEDULED_TIME="2026-11-27T10:05:00Z")
        self.assertEqual(p.wait(15), 0, p.stderr())
        lines = [l for l in p.stdout().splitlines() if l.startswith("CRON-EVENT ")]
        self.assertEqual(len(lines), 1)
        event = json.loads(lines[0][len("CRON-EVENT "):])
        self.assertEqual((event["trigger"], event["schedule"], event["scheduledTime"]),
                         ("cron", "*/5 * * * *", "2026-11-27T10:05:00Z"))
        self.assertEqual(sorted(event), ["schedule", "scheduledTime", "trigger"])
        self.assertFalse(p.listening(), "a cron run opens no port")

    def test_a_method_the_adapter_has_never_heard_of_reaches_the_function(self):
        p = self.serve()
        s = socket.create_connection(("127.0.0.1", p.port), timeout=10)
        s.sendall(b"PROPFIND /echo HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        raw = b""
        while chunk := s.recv(4096):
            raw += chunk
        s.close()
        self.assertTrue(raw.startswith(b"HTTP/1.1 200"), raw)
        self.assertEqual(json.loads(raw.split(b"\r\n\r\n", 1)[1])["event"]["method"], "PROPFIND")

    def test_cron_does_not_read_the_port(self):
        p = self.cron(PORT="abc")
        self.assertEqual(p.wait(15), 0, p.stderr())

    def test_cron_without_a_scheduled_time_uses_the_start_minute_in_utc(self):
        before = time.strftime("%Y-%m-%dT%H:%M:00Z", time.gmtime())
        p = self.cron(TITAN_SCHEDULE="* * * * *")
        self.assertEqual(p.wait(15), 0, p.stderr())
        after = time.strftime("%Y-%m-%dT%H:%M:00Z", time.gmtime())
        event = json.loads(p.stdout().split("CRON-EVENT ", 1)[1])
        self.assertIn(event["scheduledTime"], (before, after))

    def test_cron_output_is_not_cut_when_the_process_ends(self):
        p = self.cron(FIXTURE_CRON="bigout")
        self.assertEqual(p.wait(15), 0, p.stderr())
        self.assertGreaterEqual(len(p.stdout()), 1_048_576)

    def test_cron_exits_1_with_the_stack_on_stderr_when_the_function_throws(self):
        p = self.cron(FIXTURE_CRON="fail")
        self.assertEqual(p.wait(15), 1)
        self.assertIn("cron-boom", p.stderr())

    # --- SIGTERM ---------------------------------------------------------------------------------

    def test_sigterm_when_idle_exits_0_at_once(self):
        p = self.serve()
        t0 = time.time()
        p.proc.send_signal(signal.SIGTERM)
        self.assertEqual(p.wait(5), 0, p.stderr())
        self.assertLess(time.time() - t0, 1.0)

    def test_sigterm_lets_a_running_request_finish_then_exits_0(self):
        p = self.serve()
        out = {}
        t = threading.Thread(target=lambda: out.update(r=request(p.port, "GET", "/sleep?ms=1000")))
        t.start()
        time.sleep(0.3)
        p.proc.send_signal(signal.SIGTERM)
        t.join(10)
        self.assertEqual((out["r"].status, out["r"].body), (200, b"slept"))
        self.assertEqual(p.wait(5), 0, p.stderr())

    def test_sigterm_lets_a_large_response_reach_a_slow_reader(self):
        p = self.serve()
        s = socket.create_connection(("127.0.0.1", p.port), timeout=15)
        s.sendall(b"GET /big HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        time.sleep(0.5)  # the kernel buffers fill; the adapter is still writing
        p.proc.send_signal(signal.SIGTERM)
        raw = b""
        while chunk := s.recv(1 << 16):
            raw += chunk
        s.close()
        self.assertEqual(len(raw.split(b"\r\n\r\n", 1)[1]), 8_000_000)
        self.assertEqual(p.wait(5), 0, p.stderr())

    def test_a_second_sigterm_while_draining_changes_nothing(self):
        p = self.serve()
        out = {}
        t = threading.Thread(target=lambda: out.update(r=request(p.port, "GET", "/sleep?ms=800")))
        t.start()
        time.sleep(0.3)
        p.proc.send_signal(signal.SIGTERM)
        time.sleep(0.1)
        p.proc.send_signal(signal.SIGTERM)
        t.join(10)
        self.assertEqual((out["r"].status, out["r"].body), (200, b"slept"))
        self.assertEqual(p.wait(5), 0, p.stderr())

    def test_sigterm_does_not_wait_for_a_long_request(self):
        p = self.serve()
        threading.Thread(target=lambda: _ignore(lambda: request(p.port, "GET", "/sleep?ms=10000")), daemon=True).start()
        time.sleep(0.3)
        t0 = time.time()
        p.proc.send_signal(signal.SIGTERM)
        self.assertEqual(p.wait(6), 0, p.stderr())
        self.assertLess(time.time() - t0, 3.5)

    def test_sigterm_stops_accepting_new_connections(self):
        p = self.serve()
        threading.Thread(target=lambda: _ignore(lambda: request(p.port, "GET", "/sleep?ms=1500")), daemon=True).start()
        time.sleep(0.3)
        p.proc.send_signal(signal.SIGTERM)
        time.sleep(0.4)
        self.assertFalse(p.listening())
        p.wait(6)

    def test_sigterm_in_the_middle_of_a_cron_run_exits_143(self):
        p = self.cron(FIXTURE_CRON="sleep")
        _wait_for(lambda: p.stdout(), "CRON-EVENT")
        t0 = time.time()
        p.proc.send_signal(signal.SIGTERM)
        self.assertEqual(p.wait(6), 143, p.stderr())
        self.assertLess(time.time() - t0, 1.0)

    # --- a function that does not load ----------------------------------------------------------

    def assert_does_not_start(self, **env):
        p = self.start(TITAN_TRIGGER="http", **env)
        self.assertEqual(p.wait(10), 1)
        self.assertFalse(p.listening())
        self.assertTrue(p.stderr().strip(), "the reason must be on stderr")

    def test_a_port_that_is_not_a_number_does_not_start(self):
        self.assert_does_not_start(PORT="abc")

    def test_a_port_that_is_not_plain_digits_does_not_start(self):
        for port in (" 80", "+80", "8 0", "99999", "-1"):
            self.assert_does_not_start(PORT=port)

    def test_an_empty_trigger_is_http(self):
        p = self.start(TITAN_TRIGGER="")
        p.wait_port()
        self.assertEqual(request(p.port, "GET", "/string").body, b"hello")


class DynamicLoadContract(Contract):
    """Load failures only a dynamic language has: Go links its handler at build time."""

    def test_an_empty_function_dir_is_the_default_one(self):
        # the default is /function, which does not exist here: an empty value must not mean "the current directory"
        self.assert_does_not_start(TITAN_FUNCTION_DIR="")

    def test_sigterm_during_a_slow_import_exits_143(self):
        slow = os.path.join(FIXTURES, self.runtime + "-slow")
        for trigger in ("http", "cron"):
            p = self.start(TITAN_TRIGGER=trigger, TITAN_FUNCTION_DIR=slow)
            time.sleep(0.5)
            t0 = time.time()
            p.proc.send_signal(signal.SIGTERM)
            code = p.wait(6)
            # Node.js deals with a signal that came in while a synchronous import blocked it only after that import: an HTTP
            # adapter is by then listening (0), a cron run may have finished (0). Either way the process ends at once
            self.assertIn(code, (0, 143), trigger + ": " + p.stderr())
            self.assertLess(time.time() - t0, 3.5, trigger)

    def test_a_missing_module_does_not_start(self):
        self.assert_does_not_start(TITAN_HANDLER="nomodule.handler")

    def test_a_missing_handler_does_not_start(self):
        self.assert_does_not_start(TITAN_HANDLER=self.handler_name().split(".")[0] + ".nohandler")

    def test_a_handler_that_is_not_callable_does_not_start(self):
        self.assert_does_not_start(TITAN_HANDLER=self.handler_name().split(".")[0] + ".NOT_CALLABLE")

    def test_a_handler_without_a_module_part_does_not_start(self):
        self.assert_does_not_start(TITAN_HANDLER="handler")

    def test_cron_with_a_missing_handler_exits_1(self):
        p = self.start(TITAN_TRIGGER="cron", TITAN_HANDLER="nomodule.handler")
        self.assertEqual(p.wait(10), 1)
        self.assertTrue(p.stderr().strip())


def _ignore(fn):
    try:
        fn()
    except Exception:
        pass


def _wait_for(read, needle, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        got = read()
        if needle in got:
            return got
        time.sleep(0.05)
    raise AssertionError(f"{needle!r} never appeared; saw {read()!r}")


class PythonContract(DynamicLoadContract, unittest.TestCase):
    runtime = "python"

    def test_sys_exit_in_a_handler_is_a_500_and_the_adapter_lives_on(self):
        p = self.serve()
        self.assertEqual(request(p.port, "GET", "/exit").status, 500)
        self.assertEqual(request(p.port, "GET", "/string").status, 200)

    def argv(self):
        return [sys.executable, os.path.join(ROOT, "python", "adapter.py")]


class NodeContract(DynamicLoadContract, unittest.TestCase):
    runtime = "node"

    def test_cron_survives_a_promise_left_unhandled(self):
        p = self.cron(FIXTURE_CRON="dangling")
        self.assertEqual(p.wait(15), 0, p.stderr())
        self.assertIn("dangling", p.stderr())

    def test_a_promise_left_unhandled_does_not_end_the_adapter(self):
        p = self.serve()
        self.assertEqual(request(p.port, "GET", "/dangling").body, b"ok")
        time.sleep(0.3)
        self.assertEqual(request(p.port, "GET", "/string").status, 200)
        self.assertIn("dangling", p.stderr())

    def argv(self):
        return ["node", os.path.join(ROOT, "node", "adapter.mjs")]


class GoContract(Contract, unittest.TestCase):
    runtime = "go"
    dynamic = False

    @classmethod
    def setUpClass(cls):
        cls.bin_dir = tempfile.mkdtemp(prefix="titan-contract-go-")
        cls.addClassCleanup(shutil.rmtree, cls.bin_dir, True)
        for name, package in (("bootstrap", "go"), ("slow", "go-slow")):
            out = subprocess.run(["go", "build", "-o", os.path.join(cls.bin_dir, name), f"./tests/contract/fixtures/{package}"],
                                 cwd=ROOT, env={**os.environ, "CGO_ENABLED": "0"}, capture_output=True, text=True)
            if out.returncode != 0:
                raise RuntimeError("go build of the fixture failed: " + out.stderr)

    def test_sigterm_during_the_programs_own_initialisation_exits_143(self):
        for trigger in ("http", "cron"):
            p = Process([os.path.join(self.bin_dir, "slow")], {"TITAN_TRIGGER": trigger}, cwd=ROOT)
            self.addCleanup(p.kill)
            time.sleep(0.5)
            p.proc.send_signal(signal.SIGTERM)
            self.assertEqual(p.wait(6), 143, trigger + ": " + p.stderr())

    def argv(self):
        return [os.path.join(self.bin_dir, "bootstrap")]

    def handler_name(self):
        return ""


if __name__ == "__main__":
    unittest.main()
