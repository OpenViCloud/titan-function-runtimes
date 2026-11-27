package function

import (
	"context"
	"crypto/rand"
	"encoding/base64"
	"errors"
	"fmt"
	"io"
	"math"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"regexp"
	"runtime/debug"
	"sort"
	"strconv"
	"strings"
	"syscall"
	"time"
	"unicode/utf8"
)

const (
	maxBody      = 6 * 1024 * 1024
	shutdownWait = 2 * time.Second
)

type config struct {
	port          string
	trigger       string
	functionID    string
	versionID     string
	timeout       time.Duration // 0: no deadline
	schedule      string
	scheduledTime string
}

func configFrom(getenv func(string) string, now time.Time) (config, error) {
	c := config{
		port: getenv("PORT"), trigger: getenv("TITAN_TRIGGER"), functionID: getenv("TITAN_FUNCTION_ID"),
		versionID: getenv("TITAN_VERSION_ID"), schedule: getenv("TITAN_SCHEDULE"), scheduledTime: getenv("TITAN_SCHEDULED_TIME"),
	}
	if c.trigger == "" {
		c.trigger = "http"
	}
	if c.trigger != "http" && c.trigger != "cron" {
		return c, fmt.Errorf("TITAN_TRIGGER %q is neither http nor cron", c.trigger)
	}
	if c.port == "" {
		c.port = "8080"
	}
	// a cron run opens no port, so it does not read PORT; plain digits only (not " 80", not "+80")
	if n, err := strconv.Atoi(c.port); c.trigger == "http" && (err != nil || strings.Trim(c.port, "0123456789") != "" || n > 65535) {
		return c, fmt.Errorf("PORT %q is not a number", c.port)
	}
	// anything but a positive finite number of seconds means no deadline
	if s, err := strconv.ParseFloat(getenv("TITAN_TIMEOUT_SECONDS"), 64); err == nil && s > 0 && !math.IsInf(s, 0) && !math.IsNaN(s) {
		c.timeout = time.Duration(s * float64(time.Second))
	}
	if c.scheduledTime == "" {
		c.scheduledTime = now.UTC().Truncate(time.Minute).Format("2006-01-02T15:04:05Z")
	}
	return c, nil
}

// Start runs the function until the process is told to stop, then exits: it never returns.
func Start(h Handler) {
	cfg, err := configFrom(os.Getenv, time.Now())
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	if cfg.trigger == "cron" {
		os.Exit(runCron(h, cfg))
	}
	os.Exit(runHTTP(h, cfg))
}

func newContext(parent context.Context, cfg config, requestID string) (context.Context, context.CancelFunc) {
	ctx := context.WithValue(parent, infoKey{}, Info{FunctionID: cfg.functionID, VersionID: cfg.versionID, RequestID: requestID})
	if cfg.timeout > 0 {
		return context.WithTimeout(ctx, cfg.timeout)
	}
	return ctx, func() {}
}

func runCron(h Handler, cfg config) int {
	term := make(chan os.Signal, 1)
	signal.Notify(term, syscall.SIGTERM)
	go func() {
		<-term
		os.Exit(143)
	}()
	ctx, cancel := newContext(context.Background(), cfg, newID())
	defer cancel()
	_, err := call(ctx, h, Event{Trigger: "cron", Schedule: cfg.schedule, ScheduledTime: cfg.scheduledTime})
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	return 0
}

func runHTTP(h Handler, cfg config) int {
	ln, err := net.Listen("tcp", ":"+cfg.port)
	if err != nil {
		fmt.Fprintln(os.Stderr, "cannot listen:", err)
		return 1
	}
	srv := &http.Server{Handler: &adapter{h: h, cfg: cfg}}
	term := make(chan os.Signal, 1)
	signal.Notify(term, syscall.SIGTERM)
	done := make(chan error, 1)
	go func() { done <- srv.Serve(ln) }()
	select {
	case err := <-done:
		fmt.Fprintln(os.Stderr, "server stopped:", err)
		return 1
	case <-term:
	}
	ctx, cancel := context.WithTimeout(context.Background(), shutdownWait)
	defer cancel()
	if err := srv.Shutdown(ctx); err != nil {
		srv.Close()
	}
	return 0
}

type adapter struct {
	h   Handler
	cfg config
}

func (a *adapter) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	body, ok := readBody(w, r)
	if !ok {
		return
	}
	headers := map[string]string{}
	for name, values := range r.Header {
		headers[strings.ToLower(name)] = strings.Join(values, ", ")
	}
	if r.Host != "" {
		headers["host"] = r.Host
	}
	if len(r.TransferEncoding) > 0 {
		headers["transfer-encoding"] = strings.Join(r.TransferEncoding, ", ") // Go moves it out of r.Header; the other runtimes pass it
	}
	path, rawQuery := splitTarget(r.RequestURI)
	query := parseQuery(rawQuery)
	text, isB64 := string(body), false
	if !utf8.Valid(body) {
		text, isB64 = base64.StdEncoding.EncodeToString(body), true
	}
	requestID := r.Header.Get("X-Request-Id")
	if requestID == "" {
		requestID = newID()
	}
	ctx, cancel := newContext(r.Context(), a.cfg, requestID)
	defer cancel()
	resp, err := call(ctx, a.h, Event{Trigger: "http", Method: r.Method, Path: path, Query: query, Headers: headers, Body: text, IsBase64Encoded: isB64})
	var payload []byte
	if err == nil {
		payload, err = check(&resp)
	}
	if err != nil {
		fmt.Fprintf(os.Stderr, "function error (%s): %v\n", requestID, err)
		resp = Response{StatusCode: 500, Headers: map[string]string{"content-type": "text/plain; charset=utf-8"}}
		payload = []byte("Internal Server Error")
	}
	write(w, r, resp, payload)
}

func write(w http.ResponseWriter, r *http.Request, resp Response, payload []byte) {
	h := w.Header()
	names := make([]string, 0, len(resp.Headers))
	for name := range resp.Headers {
		names = append(names, name)
	}
	sort.Strings(names) // one header per name, however it is cased: the last in byte order wins
	for _, name := range names {
		if !framing(name) {
			h.Del(name)
			h.Set(name, resp.Headers[name])
		}
	}
	if h.Get("Content-Type") == "" {
		h["Content-Type"] = nil // net/http would sniff one from the body: the other runtimes send none
	}
	if resp.StatusCode != 204 { // RFC 9110: a 204 has no Content-Length
		h.Set("Content-Length", strconv.Itoa(len(payload)))
	}
	w.WriteHeader(resp.StatusCode)
	if r.Method != http.MethodHead && resp.StatusCode != 204 && resp.StatusCode != 304 {
		w.Write(payload)
	}
}

var targetRE = regexp.MustCompile(`^(?:[A-Za-z][A-Za-z0-9+.-]*://[^/?]*)?([^?]*)(?:\?(.*))?$`)

// splitTarget returns the path and the raw query of a request target: an absolute-form target loses its scheme and host,
// a leading run of slashes is one.
func splitTarget(target string) (string, string) {
	m := targetRE.FindStringSubmatch(target)
	if m == nil {
		return "/", ""
	}
	return "/" + strings.TrimLeft(m[1], "/"), m[2]
}

// parseQuery reads a raw query the way the Python and Node.js adapters do: pairs split on "&" only, a name or value that is
// not a valid escape stays as written, repeated names are joined with ",". (r.URL.Query drops such pairs.)
func parseQuery(raw string) map[string]string {
	query := map[string]string{}
	if raw == "" {
		return query
	}
	for _, pair := range strings.Split(raw, "&") {
		if pair == "" {
			continue
		}
		name, value, _ := strings.Cut(pair, "=")
		if n, err := url.QueryUnescape(name); err == nil {
			name = n
		}
		if v, err := url.QueryUnescape(value); err == nil {
			value = v
		}
		if old, ok := query[name]; ok {
			value = old + "," + value
		}
		query[name] = value
	}
	return query
}

// readBody reads the request body up to the cap; over it, it answers 413 and reports false.
func readBody(w http.ResponseWriter, r *http.Request) ([]byte, bool) {
	tooLarge := func() ([]byte, bool) {
		w.Header().Set("Connection", "close")
		w.WriteHeader(http.StatusRequestEntityTooLarge)
		w.Write([]byte("Request Entity Too Large"))
		// send the answer, then read what the client is still sending, so the answer is not lost to a reset
		rc := http.NewResponseController(w)
		rc.Flush()
		rc.SetReadDeadline(time.Now().Add(2 * time.Second))
		io.Copy(io.Discard, io.LimitReader(r.Body, 16*maxBody))
		return nil, false
	}
	if r.ContentLength > maxBody {
		return tooLarge()
	}
	body, err := io.ReadAll(io.LimitReader(r.Body, maxBody+1))
	if err != nil {
		http.Error(w, "bad request", http.StatusBadRequest)
		return nil, false
	}
	if len(body) > maxBody {
		return tooLarge()
	}
	return body, true
}

// call runs the handler; a panic is an error like any other.
func call(ctx context.Context, h Handler, e Event) (resp Response, err error) {
	defer func() {
		if p := recover(); p != nil {
			err = fmt.Errorf("panic: %v\n%s", p, debug.Stack())
		}
	}()
	return h(ctx, e)
}

func framing(name string) bool {
	switch strings.ToLower(name) {
	case "content-length", "transfer-encoding", "connection":
		return true
	}
	return false
}

const tokenChars = "!#$%&'*+-.^_`|~0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"

// check validates a response and returns the bytes of its body.
func check(resp *Response) ([]byte, error) {
	if resp.StatusCode < 200 || resp.StatusCode > 599 {
		return nil, fmt.Errorf("statusCode %d is not from 200 to 599", resp.StatusCode)
	}
	for name, value := range resp.Headers {
		if name == "" || strings.Trim(name, tokenChars) != "" {
			return nil, fmt.Errorf("header name %q is not valid", name)
		}
		for _, c := range []byte(value) {
			if c != '\t' && (c < 0x20 || c > 0x7e) {
				return nil, fmt.Errorf("header %s: the value is not visible ASCII", name)
			}
		}
	}
	if resp.IsBase64Encoded {
		if strings.ContainsAny(resp.Body, "\r\n") { // the decoder would skip them
			return nil, errors.New("body is not valid base64")
		}
		b, err := base64.StdEncoding.Strict().DecodeString(resp.Body)
		if err != nil {
			return nil, errors.New("body is not valid base64")
		}
		return b, nil
	}
	return []byte(resp.Body), nil
}

func newID() string {
	b := make([]byte, 16)
	rand.Read(b)
	b[6], b[8] = b[6]&0x0f|0x40, b[8]&0x3f|0x80
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[0:4], b[4:6], b[6:8], b[8:10], b[10:])
}
