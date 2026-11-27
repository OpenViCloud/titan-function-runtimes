// Package function is the Go runtime of OVC Function: a function is a static binary named bootstrap that calls Start
// with its handler, and the same contract as the Python and Node.js adapters applies (titan-ops
// features/ovc-function/design.md#runtimes).
//
//	func main() {
//		function.Start(func(ctx context.Context, e function.Event) (function.Response, error) {
//			return function.Text("hello " + e.Path), nil
//		})
//	}
//
// Build it with CGO_ENABLED=0 GOOS=linux GOARCH=amd64 and zip it as bootstrap at the root of the zip. The process is
// PID 1 in the pod: Start handles SIGTERM itself.
//
// Environment: PORT, TITAN_TRIGGER (http or cron), TITAN_FUNCTION_ID, TITAN_VERSION_ID, TITAN_TIMEOUT_SECONDS,
// TITAN_SCHEDULE, TITAN_SCHEDULED_TIME.
package function

import (
	"context"
	"encoding/json"
)

// Event is what the function is called with: an HTTP request (Trigger "http") or a cron run (Trigger "cron").
type Event struct {
	Trigger         string
	Method          string
	Path            string
	Query           map[string]string
	Headers         map[string]string
	Body            string
	IsBase64Encoded bool
	Schedule        string
	ScheduledTime   string
}

// MarshalJSON writes the event as the Python and Node.js adapters pass it: seven keys for http, three for cron.
func (e Event) MarshalJSON() ([]byte, error) {
	if e.Trigger == "cron" {
		return json.Marshal(map[string]string{"trigger": e.Trigger, "schedule": e.Schedule, "scheduledTime": e.ScheduledTime})
	}
	return json.Marshal(map[string]any{
		"trigger": e.Trigger, "method": e.Method, "path": e.Path, "query": e.Query, "headers": e.Headers,
		"body": e.Body, "isBase64Encoded": e.IsBase64Encoded,
	})
}

// Response is what an HTTP function returns. A cron run ignores it.
type Response struct {
	StatusCode      int
	Headers         map[string]string
	Body            string
	IsBase64Encoded bool
}

// Text is a 200 text/plain response.
func Text(body string) Response {
	return Response{StatusCode: 200, Headers: map[string]string{"content-type": "text/plain; charset=utf-8"}, Body: body}
}

// Handler is the function. Its context carries the deadline (when TITAN_TIMEOUT_SECONDS is set) and Info. A returned
// error is a 500 (HTTP) or exit code 1 (cron); the error text goes to stderr, never to the caller.
type Handler func(ctx context.Context, event Event) (Response, error)

// Info identifies the call.
type Info struct {
	FunctionID string
	VersionID  string
	RequestID  string
}

type infoKey struct{}

// FromContext returns the Info of the call that ctx belongs to.
func FromContext(ctx context.Context) (Info, bool) {
	i, ok := ctx.Value(infoKey{}).(Info)
	return i, ok
}
