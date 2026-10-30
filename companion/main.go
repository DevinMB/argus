// argus-companion reports one host to Argus and runs checks from inside it.
//
//	argus-companion enroll --url URL [--pin PIN] --code CODE [--read-only] [--config PATH]
//	argus-companion run [--config PATH] [--once]
//	argus-companion report [--config PATH]   print the report it would send
//	argus-companion version
//
// It only ever calls Argus (HTTPS, pinned); nothing listens on the host. In
// M2 it reads and reports; it changes nothing on the host.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"math/rand/v2"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"
)

var version = "dev" // set at build time

const defaultConfig = "/etc/argus-companion/companion.json"

func main() {
	if len(os.Args) < 2 {
		usage()
	}
	cmd, args := os.Args[1], os.Args[2:]
	fs := flag.NewFlagSet(cmd, flag.ExitOnError)
	config := fs.String("config", defaultConfig, "where the enrollment is kept")
	switch cmd {
	case "version":
		fmt.Println("argus-companion", version)
	case "enroll":
		url := fs.String("url", "", "Argus's address, e.g. https://argus.lan:8443")
		pin := fs.String("pin", "", "SHA-256 of Argus's TLS public key (from Add a host)")
		code := fs.String("code", "", "the single-use code from Add a host")
		readOnly := fs.Bool("read-only", false, "monitoring only: this host never deploys")
		_ = fs.Parse(args)
		if *url == "" || *code == "" {
			fail("enroll needs --url and --code (copy the command from Argus's Add a host)")
		}
		c, err := enroll(*url, *pin, *code, *readOnly)
		if err != nil {
			fail("enrolling: " + err.Error())
		}
		if err := c.save(*config); err != nil {
			fail("saving the enrollment: " + err.Error())
		}
		fmt.Printf("enrolled as %q; the enrollment is in %s\n", c.Name, *config)
	case "run":
		once := fs.Bool("once", false, "send one report and exit")
		_ = fs.Parse(args)
		c, err := loadConfig(*config)
		if err != nil {
			fail("not enrolled (" + err.Error() + "); run the command from Argus's Add a host")
		}
		if *once {
			reply, err := sendReport(pinnedClient(c.Pin), c, newCollector().Collect(c.ReadOnly))
			if err != nil {
				fail(err.Error())
			}
			fmt.Printf("reported as %q; %d checks to run here\n", reply.Name, len(reply.Checks))
			return
		}
		ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
		defer stop()
		run(ctx, c)
	case "report":
		_ = fs.Parse(args)
		readOnly := false
		if c, err := loadConfig(*config); err == nil {
			readOnly = c.ReadOnly
		}
		b, _ := json.MarshalIndent(newCollector().Collect(readOnly), "", "  ")
		fmt.Println(string(b))
	default:
		usage()
	}
}

func run(ctx context.Context, c *Config) {
	log := slog.New(slog.NewTextHandler(os.Stderr, nil))
	log.Info("starting", "version", version, "argus", c.URL, "name", c.Name,
		"read_only", c.ReadOnly)
	client, collector, checker := pinnedClient(c.Pin), newCollector(), newChecker()
	go checker.Loop(ctx)
	every, failing := 60*time.Second, false
	// A random start, so hosts that boot together do not report together.
	wait := time.Duration(rand.Int64N(int64(5 * time.Second)))
	for {
		select {
		case <-ctx.Done():
			log.Info("stopping")
			return
		case <-time.After(wait):
		}
		report := collector.Collect(c.ReadOnly)
		report.Checks = checker.Take()
		reply, err := sendReport(client, c, report)
		wait = every
		if err != nil {
			var he *HTTPError
			if errors.As(err, &he) && he.Status == http.StatusUnauthorized {
				// Removed in Argus, or a restored database: only a new code helps.
				log.Error("Argus no longer knows this host; enroll it again", "error", err)
				wait = 5 * time.Minute
			} else if !failing {
				log.Error("report failed; retrying", "error", err)
			}
			failing = true
			continue
		}
		if failing {
			log.Info("reporting again")
			failing = false
		}
		checker.Set(reply.Checks)
		if reply.ReportEvery >= 10 {
			every = time.Duration(reply.ReportEvery) * time.Second
			wait = every
		}
	}
}

func usage() {
	fmt.Fprintln(os.Stderr, "usage: argus-companion enroll|run|report|version [--config PATH]")
	os.Exit(2)
}

func fail(msg string) {
	fmt.Fprintln(os.Stderr, "argus-companion:", msg)
	os.Exit(1)
}
