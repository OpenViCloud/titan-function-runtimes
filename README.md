# titan-function-runtimes — runtimes of OpenViCloud's function service

[OVC Function](https://github.com/OpenViCloud) runs a customer's zip as a function: HTTP, or a cron schedule, in its own
microVM. This repository holds what runs *around* the customer's code: one runtime image per language, the adapter in it
that calls the function, and the Go library a Go function links. The service itself (control plane, worker) lives in the
other OpenViCloud repositories; the design is `titan-ops features/ovc-function/design.md#runtimes`.

| Runtime | Image (GHCR) | The zip holds | The adapter calls |
|---|---|---|---|
| `python3.15` | `ghcr.io/openvicloud/titan-function-runtimes/python3.15` | the module of the handler (`main.py`) and its libraries at the root (`pip install --target`, for `linux/amd64`) | the function named by `TITAN_HANDLER`, e.g. `main.handler`: a plain function `handler(event, context)` (not `async`: that is an error) |
| `nodejs26` | `ghcr.io/openvicloud/titan-function-runtimes/nodejs26` | the module of the handler (`index.mjs`, `.js` or `.cjs`) and the `node_modules` it needs | the export named by `TITAN_HANDLER`, e.g. `index.handler`: `handler(event, context)`, sync or `async` |
| `go` | `ghcr.io/openvicloud/titan-function-runtimes/go` | one static `bootstrap` (`CGO_ENABLED=0 GOOS=linux GOARCH=amd64`) | `function.Start(handler)` in `bootstrap` |

Images are `linux/amd64`, built by CI on every push to `main` and tagged with the full commit sha (one manifest each). They
run as user 1000. The function service runs a pod from them with a read-only root file system (its pod spec, not this
repository, sets that): `/function` holds the customer's code and is read-only, `/tmp` is the only writable path.

## The contract

The same for every runtime, with the differences named. `tests/contract/contract_test.py` runs its cases against all three; the
cases that need a handler of the wrong shape (a dynamic language can return one, a typed Go handler cannot) and the ones about
loading a module (Go links its handler at build time) run against Python and Node.js only.

**HTTP** (`TITAN_TRIGGER=http`, the default): the adapter listens on `$PORT` and turns a request into an `event` and the
return value into the response.

```
event    {trigger: "http", method, path, query, headers, body, isBase64Encoded}
           path: the path of the request target without the query, not decoded; an absolute-form target loses its scheme and
                 host, and a leading run of slashes is one
           query: {name: "a,b"}: split on "&" only, a malformed escape stays as written, a repeated name is joined by ","
           headers: lower-case names; a repeated header is joined by ", "
           body: a string if it is UTF-8 (a leading byte order mark stays), else base64 with isBase64Encoded true;
                 a body over 6 MiB is answered 413 and the function is not called
return   {statusCode, headers, body[, isBase64Encoded]}   or a string (200, text/plain; charset=utf-8)
           statusCode: an integer from 200 to 599; headers: names are RFC tokens, values visible ASCII (and tab) strings;
           Content-Length, Transfer-Encoding, Connection and Server are the adapter's and are dropped; a header named
           twice (in any case) is sent once; a Date the function sets replaces the adapter's;
           body: a string, or base64 (no line breaks) when isBase64Encoded is true
           no content-type given: none is sent. A 204 has no Content-Length; 204, 304 and HEAD responses carry no body
error    a thrown error (Python: any exception, SystemExit included), a returned value that is not one of the two shapes, an
         invalid status or header, or an async Python handler: a 500 "Internal Server Error". The error goes to stderr (an
         exception or panic with its stack; a Go handler's returned error as its text), never to the caller
context  Python  context.function_id, .version_id, .request_id, .get_remaining_time_in_millis()
         Node.js context.functionId, .versionId, .requestId, .getRemainingTimeInMillis()
         Go      ctx deadline, function.FromContext(ctx) -> Info{FunctionID, VersionID, RequestID}
           request id: the X-Request-Id header, else a random UUID; time left: None / null / no deadline when
           TITAN_TIMEOUT_SECONDS is not a positive finite number
```

A promise the Node.js function leaves unhandled, or an exception thrown in its timer, is logged to stderr and the process
carries on. A panic in a goroutine of a Go function ends the process (Go gives no way to carry on).

**Cron** (`TITAN_TRIGGER=cron`): the adapter calls the function once with `{trigger: "cron", schedule, scheduledTime}`, ignores
the result, does not read `PORT` and opens no port. It exits 0 when the function returned, and non-zero when it threw or was
not a valid function: 1, with the stack on stderr (Node.js exits 13 when the function's promise never settles).

**Stopping**: the adapter is PID 1 in the pod. On SIGTERM it stops accepting, lets requests in flight finish for up to 2 s and
exits 0; a cron run in progress exits 143, and so does a SIGTERM during start-up. A function that does not load (no module, no
handler, `PORT` not plain digits) exits 1 at start.

**Environment** the function service sets (the `TITAN_` prefix is reserved; an empty value is the same as unset):

| Variable | Read by | Meaning |
|---|---|---|
| `PORT` | all (HTTP only) | HTTP port, plain digits (default 8080) |
| `TITAN_TRIGGER` | all | `http` (default) or `cron` |
| `TITAN_HANDLER` | Python, Node.js | `module.function`, no default; Node.js may use a path inside the code directory, `sub/index.handler` |
| `TITAN_FUNCTION_DIR` | Python, Node.js | where the code is (default `/function`; the tests set it) |
| `TITAN_FUNCTION_ID`, `TITAN_VERSION_ID` | all | for `context` |
| `TITAN_TIMEOUT_SECONDS` | all | the time the function has; `context` reports what is left |
| `TITAN_SCHEDULE`, `TITAN_SCHEDULED_TIME` | all | for the cron event; `TITAN_SCHEDULED_TIME` unset means the start minute, UTC |

## A Go function

```go
package main

import (
	"context"

	"github.com/OpenViCloud/titan-function-runtimes/function"
)

func main() {
	function.Start(func(ctx context.Context, e function.Event) (function.Response, error) {
		return function.Text("hello " + e.Path), nil
	})
}
```

## Tests

```sh
python3 scripts/test_images.py                 # the images and the workflow
python3 python/test_adapter.py                 # Python adapter
node --test node/adapter.test.mjs              # Node.js adapter (Node 22 or later)
go vet ./... && go test ./function/            # Go library
python3 tests/contract/contract_test.py        # the contract, against all three (needs python3, node 22 or later, and go)
```

CI runs the same and builds the three images; pushes to GHCR happen only from `main`.

## License

MIT, see [LICENSE](LICENSE).
