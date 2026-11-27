"""Python runtime adapter: loads the customer's handler from /function and serves it over HTTP or runs it once as cron.

The contract is the one in titan-ops features/ovc-function/design.md#runtimes. The adapter is PID 1 in the pod, and the
kernel gives PID 1 no default action for SIGTERM, so it handles SIGTERM itself. Standard library only.

Environment: PORT, TITAN_HANDLER (`module.function`), TITAN_TRIGGER (`http` or `cron`), TITAN_FUNCTION_ID,
TITAN_VERSION_ID, TITAN_TIMEOUT_SECONDS, TITAN_SCHEDULE, TITAN_SCHEDULED_TIME, TITAN_FUNCTION_DIR (default /function).
"""
import base64
import http.server
import importlib
import inspect
import math
import os
import re
import signal
import socket
import sys
import threading
import time
import traceback
import urllib.parse
import uuid

MAX_BODY = 6 * 1024 * 1024
SHUTDOWN_WAIT = 2.0
FRAMING_HEADERS = {"content-length", "transfer-encoding", "connection", "server"}
HEADER_VALUE = re.compile(r"[\t\x20-\x7e]*")  # visible ASCII: what every runtime sends as a header value
CHUNK_SIZE = re.compile(r"[0-9a-fA-F]{1,8}")
TARGET = re.compile(r"^(?:[A-Za-z][A-Za-z0-9+.-]*://[^/?]*)?([^?]*)(?:\?(.*))?$", re.S)
TOKEN_CHARS = frozenset("!#$%&'*+-.^_`|~0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")


class Context:
    def __init__(self, function_id, version_id, request_id, deadline):
        self.function_id = function_id
        self.version_id = version_id
        self.request_id = request_id
        self._deadline = deadline

    def get_remaining_time_in_millis(self):
        """Milliseconds until the function's timeout, or None when the timeout is not known."""
        if self._deadline is None:
            return None
        return max(0, int((self._deadline - time.monotonic()) * 1000))


class InvalidResponse(Exception):
    pass


class BadRequest(Exception):
    pass


def load_handler(spec, code_dir):
    module_name, sep, attr = spec.rpartition(".")
    if not sep or not module_name or not attr:
        raise ValueError(f"TITAN_HANDLER {spec!r} is not `module.function`")
    if code_dir not in sys.path:
        sys.path.insert(0, code_dir)
    handler = getattr(importlib.import_module(module_name), attr, None)
    if handler is None:
        raise AttributeError(f"module {module_name!r} has no {attr!r}")
    if not callable(handler):
        raise TypeError(f"{spec} is not callable")
    return handler


def _context(headers, timeout):
    request_id = headers.get("x-request-id") or str(uuid.uuid4())
    deadline = None if timeout is None else time.monotonic() + timeout
    return Context(os.environ.get("TITAN_FUNCTION_ID", ""), os.environ.get("TITAN_VERSION_ID", ""), request_id, deadline)


def _timeout():
    """TITAN_TIMEOUT_SECONDS as a number of seconds; anything but a positive finite number means no deadline."""
    try:
        seconds = float(os.environ.get("TITAN_TIMEOUT_SECONDS", ""))
    except ValueError:
        return None
    return seconds if math.isfinite(seconds) and seconds > 0 else None


def split_target(target):
    """(path, raw query) of a request target: an absolute-form target loses its scheme and host, a leading run of slashes is one."""
    path, query = TARGET.match(target).groups()
    return "/" + path.lstrip("/"), query or ""


def to_response(result):
    """(status, [(name, value)], body bytes) from what the handler returned; InvalidResponse when it is none of the two shapes."""
    if inspect.isawaitable(result):
        if inspect.iscoroutine(result):
            result.close()
        raise InvalidResponse("the handler is async: the Python runtime calls a plain function")
    if isinstance(result, str):
        return 200, [("Content-Type", "text/plain; charset=utf-8")], result.encode()
    if not isinstance(result, dict):
        raise InvalidResponse(f"the function returned {type(result).__name__}, not a string or a dict with statusCode")
    status = result.get("statusCode")
    if type(status) is not int or not 200 <= status <= 599:
        raise InvalidResponse(f"statusCode {status!r} is not an integer from 200 to 599")
    headers = result.get("headers", {})
    if headers is None:
        headers = {}
    if not isinstance(headers, dict):
        raise InvalidResponse("headers is not an object")
    out = {}
    for name, value in headers.items():
        if not isinstance(name, str) or not name or not set(name) <= TOKEN_CHARS:
            raise InvalidResponse(f"header name {name!r} is not valid")
        if not isinstance(value, str):
            raise InvalidResponse(f"header {name}: the value is not a string")
        if not HEADER_VALUE.fullmatch(value):
            raise InvalidResponse(f"header {name}: the value is not visible ASCII")
        if name.lower() not in FRAMING_HEADERS:
            out[name.lower()] = (name, value)  # one header per name: the last one given wins
    out = list(out.values())
    body = result.get("body", "")
    if body is None:
        body = ""
    if not isinstance(body, str):
        raise InvalidResponse("body is not a string")
    if result.get("isBase64Encoded") is True:
        try:
            return status, out, base64.b64decode(body, validate=True)  # validate=True refuses a line break
        except ValueError:
            raise InvalidResponse("body is not valid base64") from None
    return status, out, body.encode()


class Inflight:
    def __init__(self):
        self._n = 0
        self._cv = threading.Condition()

    def enter(self):
        with self._cv:
            self._n += 1

    def leave(self):
        with self._cv:
            self._n -= 1
            self._cv.notify_all()

    def wait_idle(self, timeout):
        end = time.monotonic() + timeout
        with self._cv:
            while self._n > 0:
                left = end - time.monotonic()
                if left <= 0:
                    return
                self._cv.wait(left)


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128

    def __init__(self, port, handler):
        self.function = handler
        self.inflight = Inflight()
        self.stopping = False
        self.timeout_seconds = _timeout()
        # dual-stack when the kernel has IPv6, IPv4 only when it does not (the socket call itself fails then)
        for family, address in ((socket.AF_INET6, "::"), (socket.AF_INET, "0.0.0.0")):
            self.address_family = family
            try:
                super().__init__((address, port), RequestHandler, bind_and_activate=False)
            except OSError:
                if family == socket.AF_INET:
                    raise
                continue
            try:
                if family == socket.AF_INET6:
                    self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
                self.server_bind()
                self.server_activate()
                return
            except OSError:
                self.socket.close()
                if family == socket.AF_INET:
                    raise


class RequestHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "titan-function-python"
    sys_version = ""

    def log_message(self, *args):
        pass

    def _read_body(self):
        """The request body as bytes, or None after answering 413."""
        te = self.headers.get("Transfer-Encoding", "").lower()
        if "chunked" in te:
            data = bytearray()
            while True:
                line = self.rfile.readline(1024)
                digits = line.split(b";")[0].strip().decode("latin-1")
                if not CHUNK_SIZE.fullmatch(digits):  # not int(..., 16): it takes "-1", "0x5" and "1_0"
                    raise BadRequest()
                size = int(digits, 16)
                if size == 0:
                    while self.rfile.readline(1024) not in (b"\r\n", b"\n", b""):
                        pass
                    return bytes(data)
                if len(data) + size > MAX_BODY:
                    self._too_large()
                    return None
                data += self.rfile.read(size)
                self.rfile.readline(8)
        raw = self.headers.get("Content-Length") or "0"
        if not (raw.isascii() and raw.isdigit()):
            raise BadRequest()
        length = int(raw)
        if length > MAX_BODY:
            self._too_large()
            return None
        data = self.rfile.read(length) if length else b""
        if len(data) < length:  # the client hung up mid-body
            self.close_connection = True
            return None
        return data

    def _too_large(self):
        self._send(413, [("Content-Type", "text/plain; charset=utf-8")], b"Request Entity Too Large", close=True)
        # lingering close: read what the client is still sending, so the answer is not lost to a reset
        try:
            self.connection.shutdown(socket.SHUT_WR)
            self.connection.settimeout(1.0)
            drained = 0
            while drained < 16 * MAX_BODY:
                chunk = self.connection.recv(1 << 16)
                if not chunk:
                    break
                drained += len(chunk)
        except OSError:
            pass

    def _send(self, status, headers, body, close=False):
        close = close or self.server.stopping
        self.send_response_only(status)
        names = {name.lower() for name, _ in headers}
        if "date" not in names:
            self.send_header("Date", self.date_time_string())
        for name, value in headers:
            self.send_header(name, value)
        if status != 204:  # RFC 9110: a 204 has no Content-Length
            self.send_header("Content-Length", str(len(body)))
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        if self.command != "HEAD" and status not in (204, 304):
            self.wfile.write(body)

    def _serve(self):
        self.server.inflight.enter()
        try:
            try:
                body = self._read_body()
            except BadRequest:
                self._send(400, [("Content-Type", "text/plain; charset=utf-8")], b"Bad Request", close=True)
                return
            if body is None:
                return
            path, raw_query = split_target(self.path)
            query = {}
            for key, value in urllib.parse.parse_qsl(raw_query, keep_blank_values=True):
                query[key] = query[key] + "," + value if key in query else value
            headers = {}
            for key in self.headers:
                headers[key.lower()] = ", ".join(self.headers.get_all(key))
            try:
                text, is_b64 = body.decode("utf-8"), False
            except UnicodeDecodeError:
                text, is_b64 = base64.b64encode(body).decode(), True
            event = {"trigger": "http", "method": self.command, "path": path, "query": query,
                     "headers": headers, "body": text, "isBase64Encoded": is_b64}
            context = _context(headers, self.server.timeout_seconds)
            try:
                status, out, payload = to_response(self.server.function(event, context))
            except InvalidResponse as e:
                print(f"invalid response ({context.request_id}): {e}", file=sys.stderr, flush=True)
                status, out, payload = 500, [("Content-Type", "text/plain; charset=utf-8")], b"Internal Server Error"
            except BaseException:  # SystemExit from the function is its error too
                print(f"function error ({context.request_id}):\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                status, out, payload = 500, [("Content-Type", "text/plain; charset=utf-8")], b"Internal Server Error"
            self._send(status, out, payload)
        finally:
            self.server.inflight.leave()

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _serve


def run_http(handler):
    raw = os.environ.get("PORT") or "8080"
    if not (raw.isascii() and raw.isdigit() and int(raw) <= 65535):
        sys.exit(f"PORT {raw!r} is not a number")
    try:
        server = Server(int(raw), handler)
    except OSError as e:
        sys.exit(f"cannot listen on {raw}: {e}")

    def stop():
        server.stopping = True
        server.shutdown()
        server.server_close()
        server.inflight.wait_idle(SHUTDOWN_WAIT)
        os._exit(0)

    # serve_forever runs on this thread, and shutdown() waits for it to return: stop on another thread
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=stop, daemon=True).start())
    server.serve_forever()
    # serve_forever returns on SIGTERM; the thread above exits the process once the requests in flight are done
    threading.Event().wait()


def run_cron(handler):
    schedule = os.environ.get("TITAN_SCHEDULE", "")
    scheduled = os.environ.get("TITAN_SCHEDULED_TIME") or time.strftime("%Y-%m-%dT%H:%M:00Z", time.gmtime())
    event = {"trigger": "cron", "schedule": schedule, "scheduledTime": scheduled}
    signal.signal(signal.SIGTERM, lambda *_: os._exit(143))
    context = _context({}, _timeout())
    code = 0
    try:
        result = handler(event, context)
        if inspect.isawaitable(result):
            if inspect.iscoroutine(result):
                result.close()
            raise InvalidResponse("the handler is async: the Python runtime calls a plain function")
    except BaseException:
        traceback.print_exc()
        code = 1
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


def main():
    # PID 1 has no default action for SIGTERM: until the real handler is in, ending is all there is to do
    signal.signal(signal.SIGTERM, lambda *_: os._exit(143))
    trigger = os.environ.get("TITAN_TRIGGER") or "http"
    if trigger not in ("http", "cron"):
        sys.exit(f"TITAN_TRIGGER {trigger!r} is neither http nor cron")
    try:
        handler = load_handler(os.environ.get("TITAN_HANDLER", ""), os.environ.get("TITAN_FUNCTION_DIR", "/function"))
    except BaseException:
        traceback.print_exc()
        sys.exit(1)
    (run_cron if trigger == "cron" else run_http)(handler)


if __name__ == "__main__":
    main()
