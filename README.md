# titan-function-runtimes — runtimes of OpenViCloud's function service

[OVC Function](https://github.com/OpenViCloud) runs a customer's zip as a function: HTTP, or a cron schedule, in its own
microVM. This repository holds what runs *around* the customer's code: one runtime image per language, the adapter in it
that calls the function, and the Go library a Go function links. The service itself (control plane, worker) lives in the
other OpenViCloud repositories; the design is `titan-ops features/ovc-function/design.md#runtimes`.

| Runtime | Image (GHCR) | The zip holds | The adapter calls |
|---|---|---|---|
| `python3.15` | `ghcr.io/openvicloud/titan-function-runtimes/python3.15` | `main.py` and its libraries at the root (`pip install --target`, for `linux/amd64`) | `handler(event, context)` of `main` |
| `nodejs26` | `ghcr.io/openvicloud/titan-function-runtimes/nodejs26` | `index.mjs`/`index.js`/`index.cjs` and the `node_modules` it needs | `handler(event, context)` of `index`, sync or `async` |
| `go` | `ghcr.io/openvicloud/titan-function-runtimes/go` | one static `bootstrap` (`CGO_ENABLED=0 GOOS=linux GOARCH=amd64`) | `function.Start(handler)` in `bootstrap` |

Images are `linux/amd64`, built by CI on every push to `main` and tagged with the full commit sha (one manifest each). They
run as user 1000 on a read-only root file system; `/function` holds the customer's code, `/tmp` is the only writable path.

## The contract

The same for every runtime (`tests/contract/contract_test.py` runs each case against all three).

**HTTP** (`TITAN_TRIGGER=http`, the default): the adapter listens on `$PORT`, turns a request into an `event` and the return
value into the response.

```
event    {trigger: "http", method, path, query, headers, body, isBase64Encoded}
           headers: lower-case names; query: {name: "a,b"} (repeated names joined by ","); path: as sent, without the query;
           body: a string if it is UTF-8, else base64 with isBase64Encoded true; a body over 6 MiB is answered 413
return   {statusCode, headers, body[, isBase64Encoded]}   or a string (200, text/plain; charset=utf-8)
           anything else, a statusCode outside 100-599, or a header that is not a string without line breaks: 500
error    a thrown error is a 500 "Internal Server Error"; the stack goes to stderr, never to the caller
context  Python  context.function_id, .version_id, .request_id, .get_remaining_time_in_millis()
         Node.js context.functionId, .versionId, .requestId, .getRemainingTimeInMillis()
         Go      ctx deadline, function.FromContext(ctx) -> Info{FunctionID, VersionID, RequestID}
```

**Cron** (`TITAN_TRIGGER=cron`): the adapter calls the function once with `{trigger: "cron", schedule, scheduledTime}`, ignores
the result and exits: 0, or 1 when the function threw (the stack goes to stderr). No port is opened.

**Stopping**: the adapter is PID 1 in the pod. On SIGTERM it stops accepting, lets requests in flight finish for up to 2 s and
exits 0; a cron run in progress exits 143. A function that does not load (no module, no handler, `PORT` not a number) exits 1
at start.

**Environment** the adapter reads (set by the function service, not by the customer; the `TITAN_` prefix is reserved):

| Variable | Meaning |
|---|---|
| `PORT` | HTTP port (default 8080) |
| `TITAN_HANDLER` | `module.function`, e.g. `main.handler`; Node.js may use a path, `sub/index.handler` |
| `TITAN_TRIGGER` | `http` (default) or `cron` |
| `TITAN_FUNCTION_ID`, `TITAN_VERSION_ID` | for `context` |
| `TITAN_TIMEOUT_SECONDS` | the time the function has; `context` reports what is left (none when unset) |
| `TITAN_SCHEDULE`, `TITAN_SCHEDULED_TIME` | for the cron event; `TITAN_SCHEDULED_TIME` unset means the start minute, UTC |
| `TITAN_FUNCTION_DIR` | where the code is (default `/function`; tests set it) |

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
python3 tests/contract/contract_test.py        # the contract, against all three (needs python3, node and go)
```

CI runs the same and builds the three images; pushes to GHCR happen only from `main`.

## License

MIT, see [LICENSE](LICENSE).
