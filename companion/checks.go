package main

import (
	"context"
	"crypto/tls"
	"fmt"
	"io"
	"net"
	"net/http"
	"os/exec"
	"slices"
	"strconv"
	"sync"
	"time"
)

// CheckSpec is a check Argus asks this host to run, from inside it.
type CheckSpec struct {
	Name         string  `json:"name"`
	Kind         string  `json:"kind"` // http, tcp or ping
	Target       string  `json:"target"`
	Interval     int     `json:"interval"`
	Timeout      float64 `json:"timeout"`
	ExpectStatus []int   `json:"expect_status"`
	VerifyTLS    bool    `json:"verify_tls"`
}

type CheckResult struct {
	Name     string  `json:"name"`
	OK       bool    `json:"ok"`
	Duration float64 `json:"duration"`
	Error    string  `json:"error,omitempty"`
}

// maxQueued results wait for the next report; beyond that the oldest go.
const maxQueued = 1000

// Checker runs the checks on their intervals and queues results for the report.
type Checker struct {
	mu      sync.Mutex
	specs   []CheckSpec
	next    map[string]time.Time
	results []CheckResult
}

func newChecker() *Checker { return &Checker{next: map[string]time.Time{}} }

func (k *Checker) Set(specs []CheckSpec) {
	k.mu.Lock()
	defer k.mu.Unlock()
	k.specs = specs
	for name := range k.next {
		if !slices.ContainsFunc(specs, func(s CheckSpec) bool { return s.Name == name }) {
			delete(k.next, name)
		}
	}
}

// Take returns the queued results and empties the queue.
func (k *Checker) Take() []CheckResult {
	k.mu.Lock()
	defer k.mu.Unlock()
	out := k.results
	k.results = nil
	if out == nil {
		out = []CheckResult{}
	}
	return out
}

func (k *Checker) Loop(ctx context.Context) {
	t := time.NewTicker(time.Second)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case now := <-t.C:
			k.mu.Lock()
			var due []CheckSpec
			for _, s := range k.specs {
				if k.next[s.Name].After(now) {
					continue
				}
				k.next[s.Name] = now.Add(time.Duration(max(s.Interval, 10)) * time.Second)
				due = append(due, s)
			}
			k.mu.Unlock()
			for _, s := range due {
				go func() {
					r := runCheck(ctx, s)
					k.mu.Lock()
					k.results = append(k.results, r)
					if len(k.results) > maxQueued {
						k.results = k.results[len(k.results)-maxQueued:]
					}
					k.mu.Unlock()
				}()
			}
		}
	}
}

func runCheck(ctx context.Context, s CheckSpec) CheckResult {
	timeout := time.Duration(s.Timeout * float64(time.Second))
	if timeout <= 0 {
		timeout = 5 * time.Second
	}
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	start := time.Now()
	err := probe(ctx, s, timeout)
	r := CheckResult{Name: s.Name, OK: err == nil, Duration: time.Since(start).Seconds()}
	if err != nil {
		r.Error = err.Error()
		if len(r.Error) > 300 {
			r.Error = r.Error[:300]
		}
	}
	return r
}

func probe(ctx context.Context, s CheckSpec, timeout time.Duration) error {
	switch s.Kind {
	case "http":
		t := http.DefaultTransport.(*http.Transport).Clone()
		t.TLSClientConfig = &tls.Config{InsecureSkipVerify: !s.VerifyTLS} //nolint:gosec // the check's own choice
		client := &http.Client{Transport: t, Timeout: timeout,
			CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, s.Target, nil)
		if err != nil {
			return err
		}
		req.Header.Set("User-Agent", "argus-companion")
		resp, err := client.Do(req)
		if err != nil {
			return err
		}
		_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 64<<10))
		resp.Body.Close()
		expect := s.ExpectStatus
		if len(expect) == 0 {
			expect = []int{200}
		}
		if !slices.Contains(expect, resp.StatusCode) {
			return fmt.Errorf("HTTP %d, expected %s", resp.StatusCode, joinInts(expect))
		}
		return nil
	case "tcp":
		conn, err := (&net.Dialer{}).DialContext(ctx, "tcp", s.Target)
		if err != nil {
			return err
		}
		return conn.Close()
	case "ping":
		secs := strconv.Itoa(max(1, int(timeout.Seconds())))
		out, err := exec.CommandContext(ctx, "ping", "-c", "1", "-W", secs, "--", s.Target).CombinedOutput()
		if err != nil {
			return fmt.Errorf("no reply (%s)", lastLine(string(out)))
		}
		return nil
	}
	return fmt.Errorf("unknown check kind %q", s.Kind)
}

func joinInts(xs []int) string {
	s := ""
	for i, x := range xs {
		if i > 0 {
			s += " or "
		}
		s += strconv.Itoa(x)
	}
	return s
}

func lastLine(s string) string {
	lines := nonEmptyLines(s)
	if len(lines) == 0 {
		return "ping failed"
	}
	return lines[len(lines)-1]
}
