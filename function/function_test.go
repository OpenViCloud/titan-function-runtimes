package function

import (
	"context"
	"encoding/json"
	"regexp"
	"strings"
	"testing"
	"time"
)

func env(m map[string]string) func(string) string { return func(k string) string { return m[k] } }

func TestConfigFrom(t *testing.T) {
	now := time.Date(2026, 11, 27, 10, 5, 42, 0, time.UTC)
	c, err := configFrom(env(nil), now)
	if err != nil || c.port != "8080" || c.trigger != "http" || c.timeout != 0 || c.scheduledTime != "2026-11-27T10:05:00Z" {
		t.Fatalf("defaults: %+v %v", c, err)
	}
	c, err = configFrom(env(map[string]string{"PORT": "9000", "TITAN_TRIGGER": "cron", "TITAN_TIMEOUT_SECONDS": "2.5", "TITAN_SCHEDULED_TIME": "2026-01-01T00:00:00Z"}), now)
	if err != nil || c.port != "9000" || c.trigger != "cron" || c.timeout != 2500*time.Millisecond || c.scheduledTime != "2026-01-01T00:00:00Z" {
		t.Fatalf("set: %+v %v", c, err)
	}
	for _, bad := range []map[string]string{{"PORT": "abc"}, {"PORT": "70000"}, {"PORT": "-1"}, {"TITAN_TRIGGER": "queue"}} {
		if _, err := configFrom(env(bad), now); err == nil {
			t.Errorf("%v should be refused", bad)
		}
	}
	if c, _ := configFrom(env(map[string]string{"TITAN_TIMEOUT_SECONDS": "x"}), now); c.timeout != 0 {
		t.Errorf("a timeout that is not a number means no deadline, got %v", c.timeout)
	}
}

func TestCheck(t *testing.T) {
	good := []Response{
		{StatusCode: 200}, {StatusCode: 100}, {StatusCode: 599}, {StatusCode: 200, Headers: map[string]string{"X-A": "b"}},
		{StatusCode: 200, IsBase64Encoded: true, Body: "AP8="},
	}
	for _, r := range good {
		if _, err := check(&r); err != nil {
			t.Errorf("%+v: %v", r, err)
		}
	}
	bad := []Response{
		{}, {StatusCode: 99}, {StatusCode: 600}, {StatusCode: 200, Headers: map[string]string{"a b": "x"}},
		{StatusCode: 200, Headers: map[string]string{"": "x"}}, {StatusCode: 200, Headers: map[string]string{"a": "x\ny"}},
		{StatusCode: 200, IsBase64Encoded: true, Body: "***"}, {StatusCode: 200, IsBase64Encoded: true, Body: "AP8"},
	}
	for _, r := range bad {
		if _, err := check(&r); err == nil {
			t.Errorf("%+v should be refused", r)
		}
	}
	b, _ := check(&Response{StatusCode: 200, IsBase64Encoded: true, Body: "AP8="})
	if string(b) != "\x00\xff" {
		t.Errorf("base64 body decoded to %q", b)
	}
}

func TestEventJSONHasTheKeysOfTheOtherRuntimes(t *testing.T) {
	keys := func(e Event) []string {
		var m map[string]any
		b, _ := json.Marshal(e)
		json.Unmarshal(b, &m)
		var out []string
		for k := range m {
			out = append(out, k)
		}
		return out
	}
	if got := len(keys(Event{Trigger: "http"})); got != 7 {
		t.Errorf("an http event has %d keys, want 7", got)
	}
	if got := len(keys(Event{Trigger: "cron", Method: "ignored"})); got != 3 {
		t.Errorf("a cron event has %d keys, want 3", got)
	}
}

func TestInfoRidesTheContextAndTheDeadlineIsTheTimeout(t *testing.T) {
	ctx, cancel := newContext(context.Background(), config{functionID: "f", versionID: "v", timeout: time.Minute}, "r")
	defer cancel()
	if i, ok := FromContext(ctx); !ok || i != (Info{"f", "v", "r"}) {
		t.Errorf("info %+v %v", i, ok)
	}
	if d, ok := ctx.Deadline(); !ok || time.Until(d) > time.Minute || time.Until(d) < 59*time.Second {
		t.Errorf("deadline %v %v", d, ok)
	}
	ctx, cancel = newContext(context.Background(), config{}, "r")
	defer cancel()
	if _, ok := ctx.Deadline(); ok {
		t.Error("no timeout set, but there is a deadline")
	}
	if _, ok := FromContext(context.Background()); ok {
		t.Error("a context that is not the function's has no Info")
	}
}

func TestPanicIsAnError(t *testing.T) {
	_, err := call(context.Background(), func(context.Context, Event) (Response, error) { panic("boom") }, Event{})
	if err == nil || !strings.Contains(err.Error(), "boom") {
		t.Fatalf("got %v", err)
	}
}

func TestNewIDIsAUUID(t *testing.T) {
	re := regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$`)
	a, b := newID(), newID()
	if !re.MatchString(a) || a == b {
		t.Errorf("%s %s", a, b)
	}
}
