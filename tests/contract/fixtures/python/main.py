"""Fixture handler of the contract suite: the same behaviours as fixtures/node/index.mjs and fixtures/go/main.go."""
import base64
import json
import os
import time


def _context(c):
    return {"functionId": c.function_id, "versionId": c.version_id, "requestId": c.request_id,
            "remainingMs": c.get_remaining_time_in_millis()}


def _json(obj, status=200):
    return {"statusCode": status, "headers": {"content-type": "application/json"}, "body": json.dumps(obj)}


NOT_CALLABLE = 5  # TITAN_HANDLER=main.NOT_CALLABLE

INVALID = [
    None,
    None,
    {},
    {"statusCode": "abc"},
    {"statusCode": 99},
    {"statusCode": 600},
    {"statusCode": 200, "headers": ["x"]},
    {"statusCode": 200, "headers": {"x-n": 5}},
    {"statusCode": 200, "body": 5},
    {"statusCode": 200, "isBase64Encoded": True, "body": "not base64!"},
    {"statusCode": True},
    {"statusCode": 100},
    {"statusCode": 199},
]


def handler(event, context):
    if event["trigger"] == "cron":
        print("CRON-EVENT " + json.dumps(event), flush=True)
        mode = os.environ.get("FIXTURE_CRON", "")
        if mode == "fail":
            raise RuntimeError("cron-boom")
        if mode == "bigout":
            print("x" * 1_048_576, flush=True)
        if mode == "sleep":
            time.sleep(30)
        return None
    path, query = event["path"], event["query"]
    if path == "/echo":
        return _json({"event": event, "context": _context(context)})
    if path == "/status":
        return {"statusCode": int(query["code"]), "headers": {"x-a": "b"}, "body": "ok"}
    if path == "/string":
        return "hello"
    if path == "/big":
        return "a" * 8_000_000
    if path == "/exit":
        raise SystemExit(3)
    if path == "/throw":
        raise RuntimeError("boom-secret")
    if path == "/bad":
        return INVALID[int(query["n"])]
    if path == "/crlf":
        return {"statusCode": 200, "headers": {"x-a": "b\r\nx-evil: 1"}, "body": "x"}
    if path == "/framing":
        return {"statusCode": 200, "headers": {"Content-Length": "1", "Connection": "close", "Server": "evil", "X-Ok": "1"}, "body": "hello"}
    if path == "/b64":
        return {"statusCode": 200, "isBase64Encoded": True, "body": base64.b64encode(b"\x00\xffAB").decode()}
    if path == "/nonascii":
        return {"statusCode": 200, "headers": {"x-a": "é€"}, "body": "x"}
    if path == "/b64nl":
        return {"statusCode": 200, "isBase64Encoded": True, "body": "QU\nJD"}
    if path == "/date":
        return {"statusCode": 200, "headers": {"Date": "Thu, 01 Jan 1970 00:00:00 GMT"}, "body": "x"}
    if path == "/badhead":
        return {"statusCode": 200, "headers": {"bad name": "x"}, "body": "x"}
    if path == "/len":
        print("LEN-CALLED", flush=True)
        raw = base64.b64decode(event["body"]) if event["isBase64Encoded"] else event["body"].encode()
        return str(len(raw))
    if path == "/sleep":
        time.sleep(int(query["ms"]) / 1000)
        return "slept"
    if path == "/remaining":
        first = context.get_remaining_time_in_millis()
        time.sleep(0.3)
        return _json([first, context.get_remaining_time_in_millis()])
    return {"statusCode": 404, "body": "path=" + path}
