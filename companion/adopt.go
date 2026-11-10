package main

import (
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"syscall"
)

// Collected is one running Compose project's files, read for adoption: its
// compose files and its .env. Argus only names the project; the companion finds
// the folder and files itself, from Docker's labels, and reads nothing else.
type Collected struct {
	Project string            `json:"project"`
	Dir     string            `json:"dir"`
	Files   map[string]string `json:"files,omitempty"` // base name -> base64
	Env     string            `json:"env,omitempty"`
	Error   string            `json:"error,omitempty"`
	// What this host's policy would refuse in it, checked here before Argus
	// commits it anywhere: the same check a deploy makes (Policy.Check).
	Policy []string `json:"policy,omitempty"`
}

const maxAdoptFile = 512 * 1024

// collect reads a project's files. Only regular files inside the project's own
// folder, never through a symlink, each at most 512 KB. moved, when set, names
// the folder a project's files went to since its containers started (an
// uninstall hands stacks back without restarting them, so Docker's labels keep
// the old folder); it returns "" when they did not move.
func collect(project string, containers []Container, moved func(dir string) string) Collected {
	out := Collected{Project: project}
	var dir string
	var files []string
	for _, c := range containers {
		if c.Project == project && c.WorkingDir != "" {
			dir, files = c.WorkingDir, c.ConfigFiles
			break
		}
	}
	if dir == "" {
		out.Error = "no running Compose project of that name here"
		return out
	}
	dir = filepath.Clean(dir)
	out.Dir = dir
	if !filepath.IsAbs(dir) {
		out.Error = "its folder is not an absolute path"
		return out
	}
	now := dir
	if moved != nil {
		if to := moved(dir); to != "" {
			now, out.Dir = to, to
		}
	}
	out.Files = map[string]string{}
	for _, f := range files {
		f = filepath.Clean(f)
		if filepath.Dir(f) != dir {
			out.Error = "a compose file of it lives outside its folder: " + f
			return out
		}
		b, err := readPlain(filepath.Join(now, filepath.Base(f)))
		if err != nil {
			out.Error = err.Error()
			return out
		}
		out.Files[filepath.Base(f)] = base64.StdEncoding.EncodeToString(b)
	}
	if len(out.Files) == 0 {
		out.Error = "Docker does not say which compose file started it"
		return out
	}
	if b, err := readPlain(filepath.Join(now, ".env")); err == nil {
		out.Env = string(b)
	} else if !errors.Is(err, os.ErrNotExist) {
		out.Error = err.Error()
	}
	return out
}

// precheck runs the policy check a deploy would, on files just collected, so
// Argus can say what to allow before it commits the stack (a stack it moved in
// was refused at its first deploy: a bind under /home, a capability). Nothing
// is checked when Compose cannot resolve the files here: the deploy says why.
func (d *Deployer) precheck(got Collected) []string {
	if got.Error != "" || len(got.Files) == 0 {
		return nil
	}
	p, err := loadPolicy(d.PolicyPath)
	if err != nil {
		return []string{"the policy file is unreadable: " + err.Error()}
	}
	if err := os.MkdirAll(d.StateDir, 0o700); err != nil {
		return nil
	}
	stage, err := os.MkdirTemp(d.StateDir, ".precheck-")
	if err != nil {
		return nil
	}
	defer os.RemoveAll(stage)
	files := map[string][]byte{}
	for name, b64 := range got.Files {
		b, err := base64.StdEncoding.DecodeString(b64)
		if err != nil || filepath.Base(name) != name {
			return nil
		}
		files[name] = b
	}
	if err := writeFiles(stage, files, got.Env); err != nil {
		return nil
	}
	out, err := d.compose(stage, nil, "-p", got.Project, "config", "--format", "json")
	if err != nil {
		return nil
	}
	return p.Check([]byte(out), stage, got.Dir)
}

// readPlain opens a regular file without following a symlink at its last step.
func readPlain(path string) ([]byte, error) {
	f, err := os.OpenFile(path, os.O_RDONLY|syscall.O_NOFOLLOW, 0)
	if err != nil {
		if errors.Is(err, syscall.ELOOP) {
			return nil, fmt.Errorf("%s is a symlink, and Argus reads only plain files; replace it with a copy of the file it points to", path)
		}
		return nil, err
	}
	defer f.Close()
	st, err := f.Stat()
	if err != nil || !st.Mode().IsRegular() {
		return nil, fmt.Errorf("%s is not a plain file; not read", path)
	}
	if st.Size() > maxAdoptFile {
		return nil, fmt.Errorf("%s is larger than 512 KB; not read", path)
	}
	return io.ReadAll(io.LimitReader(f, maxAdoptFile))
}

// handedBack is where a stack Argus created went when an earlier companion was
// uninstalled: dir is its old folder in StacksDir, now gone, and the answer is
// the /opt/stacks folder whose mark names it, or "" (see handBackScript).
func (d *Deployer) handedBack(dir string) string {
	if d.HandedBack == "" || filepath.Dir(dir) != filepath.Clean(d.StacksDir) {
		return ""
	}
	if _, err := os.Lstat(dir); !errors.Is(err, os.ErrNotExist) {
		return ""
	}
	name := filepath.Base(dir)
	for _, cand := range []string{name, name + "-argus"} {
		to := filepath.Join(d.HandedBack, cand)
		b, err := readPlain(filepath.Join(to, handedBackMark))
		if err == nil && strings.TrimSpace(string(b)) == name {
			return to
		}
	}
	return ""
}

// The folders this companion collected: the only ones it will later deploy
// into in place. Argus cannot add to this list; only a collect on this host can.
func (d *Deployer) adoptedPath() string { return filepath.Join(d.StateDir, "adopted.json") }

func (d *Deployer) Adopted() []string {
	var dirs []string
	if b, err := os.ReadFile(d.adoptedPath()); err == nil {
		_ = json.Unmarshal(b, &dirs)
	}
	return dirs
}

func (d *Deployer) markAdopted(dir string) {
	dirs := d.Adopted()
	if !slices.Contains(dirs, dir) {
		dirs = append(dirs, dir)
		b, _ := json.Marshal(dirs)
		_ = os.MkdirAll(d.StateDir, 0o700)
		_ = os.WriteFile(d.adoptedPath(), b, 0o600)
	}
}

// inPlace checks a stack's own folder: adopted here, and allowed by the policy.
func (d *Deployer) inPlace(dir string) error {
	dir = filepath.Clean(dir)
	if !filepath.IsAbs(dir) {
		return fmt.Errorf("%s is not an absolute path", dir)
	}
	if !slices.Contains(d.Adopted(), dir) {
		return fmt.Errorf("%s was never adopted on this host, so nothing is deployed there", dir)
	}
	if d.allowed(dir) {
		return nil
	}
	return fmt.Errorf("%s is outside the folders this host's policy allows (%s); add it to bind_roots in /etc/argus-companion/policy.json on the host",
		dir, strings.Join(d.Policy.BindRoots, ", "))
}
