package main

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"runtime"
	"time"
)

// Updater replaces this program with the build Argus serves when they differ.
// The build's sha256 comes in Argus's signed reply and the file over the pinned
// connection, so nothing else can slip in a binary. The new file is installed
// outside the service's read-only sandbox (as patch runs are) and the program
// then exits; systemd or OpenRC starts the new one.
type Updater struct {
	Exe, Self, StateDir, URL string
	client                   *http.Client
	run                      func(timeout time.Duration, env []string, name string, args ...string) (string, error)
	tried                    map[string]time.Time
}

const retryUpdate = time.Hour

func newUpdater(client *http.Client, url, stateDir string) *Updater {
	u := &Updater{URL: url, StateDir: stateDir, client: client, run: runOutside,
		tried: map[string]time.Time{}}
	if exe, err := os.Executable(); err == nil {
		if exe, err = filepath.EvalSymlinks(exe); err == nil {
			u.Exe, u.Self = exe, fileSHA256(exe)
		}
	}
	return u
}

func fileSHA256(path string) string {
	f, err := os.Open(path)
	if err != nil {
		return ""
	}
	defer f.Close()
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return ""
	}
	return hex.EncodeToString(h.Sum(nil))
}

// Due: Argus serves another build, and it was not tried in the last hour.
func (u *Updater) Due(sha string) bool {
	return sha != "" && u.Self != "" && sha != u.Self && time.Since(u.tried[sha]) > retryUpdate
}

// Update fetches the build, checks its sha256 and installs it over this program.
func (u *Updater) Update(sha string) error {
	u.tried[sha] = time.Now()
	resp, err := u.client.Get(u.URL + "/companion/download/linux-" + runtime.GOARCH)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("download: HTTP %d", resp.StatusCode)
	}
	tmp, err := os.CreateTemp(u.StateDir, "argus-companion-*.new")
	if err != nil {
		return err
	}
	defer os.Remove(tmp.Name())
	h := sha256.New()
	_, err = io.Copy(io.MultiWriter(tmp, h), io.LimitReader(resp.Body, 200<<20))
	if cerr := tmp.Close(); err == nil {
		err = cerr
	}
	if err != nil {
		return err
	}
	if got := hex.EncodeToString(h.Sum(nil)); got != sha {
		return fmt.Errorf("the download's sha256 is %s, not the %s Argus signed", got[:12], sha[:12])
	}
	if out, err := u.run(time.Minute, nil, "install", "-m", "0755", tmp.Name(), u.Exe); err != nil {
		return fmt.Errorf("install: %v %s", err, out)
	}
	return nil
}
