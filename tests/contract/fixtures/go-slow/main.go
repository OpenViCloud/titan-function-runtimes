// A program whose initialisation takes 1.5 s, before main: SIGTERM arrives while it is still loading.
package main

import (
	"context"
	"time"

	"github.com/OpenViCloud/titan-function-runtimes/function"
)

var _ = func() int { time.Sleep(1500 * time.Millisecond); return 0 }()

func main() {
	function.Start(func(context.Context, function.Event) (function.Response, error) { return function.Text("slow"), nil })
}
