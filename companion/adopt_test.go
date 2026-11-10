package main

import (
	"encoding/base64"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestCollectReadsOnlyTheProjectsOwnPlainFiles(t *testing.T) {
	dir := t.TempDir()
	_ = os.WriteFile(filepath.Join(dir, "compose.yml"), []byte("services: {}\n"), 0o644)
	_ = os.WriteFile(filepath.Join(dir, ".env"), []byte("TOKEN=s3cret\n"), 0o600)
	cs := []Container{{Name: "web", Project: "web", WorkingDir: dir,
		ConfigFiles: []string{filepath.Join(dir, "compose.yml")}}}
	got := collect("web", cs)
	if got.Error != "" || got.Env != "TOKEN=s3cret\n" || got.Dir != dir {
		t.Fatalf("got %+v", got)
	}
	if b, _ := base64.StdEncoding.DecodeString(got.Files["compose.yml"]); string(b) != "services: {}\n" {
		t.Fatalf("compose %q", b)
	}
	// A label pointing outside the folder is refused, not read.
	cs[0].ConfigFiles = []string{"/etc/passwd"}
	if got := collect("web", cs); !strings.Contains(got.Error, "outside its folder") || len(got.Files) != 0 {
		t.Fatalf("got %+v", got)
	}
	// A symlink is never followed.
	_ = os.Remove(filepath.Join(dir, "compose.yml"))
	_ = os.Symlink("/etc/hostname", filepath.Join(dir, "compose.yml"))
	cs[0].ConfigFiles = []string{filepath.Join(dir, "compose.yml")}
	if got := collect("web", cs); !strings.Contains(got.Error, "symlink") {
		t.Fatalf("got %+v", got)
	}
	if got := collect("other", cs); got.Error == "" {
		t.Fatal("an unknown project must not be read")
	}
}

func TestInPlaceOnlyWhereThisHostAdoptedAndThePolicyAllows(t *testing.T) {
	d, f := newTestDeployer(t)
	root := t.TempDir()
	d.Policy = defaultPolicy()
	d.Policy.BindRoots = []string{root}
	_ = os.WriteFile(d.PolicyPath, []byte(`{"bind_roots":["`+root+`"]}`), 0o644)
	adopted := filepath.Join(root, "media")
	st := stack("d1", "v1")
	st.Dir = adopted
	d.Apply(&DeployPlan{Stacks: []StackSpec{st}})
	r := d.Results(&DeployPlan{Stacks: []StackSpec{st}})[0]
	if r.Result != "refused" || !strings.Contains(r.Detail, "never adopted") {
		t.Fatalf("a folder Argus names is not enough: %+v", r)
	}
	d.markAdopted(adopted)
	_ = os.MkdirAll(adopted, 0o755)
	_ = os.WriteFile(filepath.Join(adopted, "data.db"), []byte("precious"), 0o644)
	st.Digest = "d2"
	d.Apply(&DeployPlan{Stacks: []StackSpec{st}})
	if r := d.Results(&DeployPlan{Stacks: []StackSpec{st}})[0]; r.Result != "ok" {
		t.Fatalf("in place: %+v (calls %v)", r, f.calls)
	}
	if b, _ := os.ReadFile(filepath.Join(adopted, "compose.yml")); string(b) != "v1" {
		t.Fatal("compose.yml was not written in place")
	}
	if b, _ := os.ReadFile(filepath.Join(adopted, "data.db")); string(b) != "precious" {
		t.Fatal("the stack's data was touched")
	}
	// Adopted, but outside what the policy allows: refused with the fix.
	outside := t.TempDir()
	d.markAdopted(outside)
	st.Dir, st.Digest = outside, "d3"
	d.Apply(&DeployPlan{Stacks: []StackSpec{st}})
	if r := d.Results(&DeployPlan{Stacks: []StackSpec{st}})[0]; r.Result != "refused" ||
		!strings.Contains(r.Detail, "bind_roots") {
		t.Fatalf("got %+v", r)
	}
}
