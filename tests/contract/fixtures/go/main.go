// Fixture handler of the contract suite: the same behaviours as fixtures/python/main.py and fixtures/node/index.mjs.
// A Go handler returns a typed Response, so the cases that return a value of the wrong shape do not exist here.
package main

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/OpenViCloud/titan-function-runtimes/function"
)

func remaining(ctx context.Context) any {
	d, ok := ctx.Deadline()
	if !ok {
		return nil
	}
	return max(time.Until(d).Milliseconds(), 0)
}

func reply(v any) function.Response {
	b, _ := json.Marshal(v)
	return function.Response{StatusCode: 200, Headers: map[string]string{"content-type": "application/json"}, Body: string(b)}
}

func handler(ctx context.Context, e function.Event) (function.Response, error) {
	if e.Trigger == "cron" {
		b, _ := json.Marshal(e)
		fmt.Println("CRON-EVENT " + string(b))
		switch os.Getenv("FIXTURE_CRON") {
		case "fail":
			return function.Response{}, errors.New("cron-boom")
		case "bigout":
			fmt.Println(strings.Repeat("x", 1_048_576))
		case "sleep":
			time.Sleep(30 * time.Second)
		}
		return function.Response{}, nil
	}
	switch e.Path {
	case "/echo":
		info, _ := function.FromContext(ctx)
		return reply(map[string]any{
			"event":   e,
			"context": map[string]any{"functionId": info.FunctionID, "versionId": info.VersionID, "requestId": info.RequestID, "remainingMs": remaining(ctx)},
		}), nil
	case "/big":
		return function.Text(strings.Repeat("a", 8_000_000)), nil
	case "/status":
		code, _ := strconv.Atoi(e.Query["code"])
		return function.Response{StatusCode: code, Headers: map[string]string{"x-a": "b"}, Body: "ok"}, nil
	case "/string":
		return function.Text("hello"), nil
	case "/throw":
		return function.Response{}, errors.New("boom-secret")
	case "/bad":
		// the typed Response cannot be a wrong shape; its invalid values are a status outside 200-599
		if e.Query["n"] == "1" {
			return function.Response{StatusCode: 199, Body: "x"}, nil
		}
		return function.Response{StatusCode: 600, Body: "x"}, nil
	case "/crlf":
		return function.Response{StatusCode: 200, Headers: map[string]string{"x-a": "b\r\nx-evil: 1"}, Body: "x"}, nil
	case "/framing":
		return function.Response{StatusCode: 200, Headers: map[string]string{"Content-Length": "1", "Connection": "close", "Server": "evil", "X-Ok": "1"}, Body: "hello"}, nil
	case "/b64":
		return function.Response{StatusCode: 200, IsBase64Encoded: true, Body: base64.StdEncoding.EncodeToString([]byte{0, 255, 'A', 'B'})}, nil
	case "/nonascii":
		return function.Response{StatusCode: 200, Headers: map[string]string{"x-a": "é€"}, Body: "x"}, nil
	case "/b64nl":
		return function.Response{StatusCode: 200, IsBase64Encoded: true, Body: "QU\nJD"}, nil
	case "/date":
		return function.Response{StatusCode: 200, Headers: map[string]string{"Date": "Thu, 01 Jan 1970 00:00:00 GMT"}, Body: "x"}, nil
	case "/badhead":
		return function.Response{StatusCode: 200, Headers: map[string]string{"bad name": "x"}, Body: "x"}, nil
	case "/len":
		fmt.Println("LEN-CALLED")
		if e.IsBase64Encoded {
			raw, _ := base64.StdEncoding.DecodeString(e.Body)
			return function.Text(strconv.Itoa(len(raw))), nil
		}
		return function.Text(strconv.Itoa(len(e.Body))), nil
	case "/sleep":
		ms, _ := strconv.Atoi(e.Query["ms"])
		time.Sleep(time.Duration(ms) * time.Millisecond)
		return function.Text("slept"), nil
	case "/remaining":
		first := remaining(ctx)
		time.Sleep(300 * time.Millisecond)
		return reply([]any{first, remaining(ctx)}), nil
	}
	return function.Response{StatusCode: 404, Body: "path=" + e.Path}, nil
}

func main() { function.Start(handler) }
