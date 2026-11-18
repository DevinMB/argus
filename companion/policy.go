package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strings"
)

// Policy is what this host will never run, whatever Argus or the deploy repo
// says. It lives on the host (/etc/argus-companion/policy.json) and changing it
// needs someone on the host: a compromised Argus cannot loosen it.
type Policy struct {
	AllowPrivileged     bool     `json:"allow_privileged"`
	AllowHostNamespaces bool     `json:"allow_host_namespaces"` // network, pid, ipc, userns: host
	AllowDockerSocket   bool     `json:"allow_docker_socket"`
	AllowDevices        bool     `json:"allow_devices"`
	AllowedCaps         []string `json:"allowed_caps"`
	// Host folders a bind mount may come from, besides the stack's own folder.
	BindRoots []string `json:"bind_roots"`
	// Host files any stack may mount read-only.
	ReadOnlyBinds []string `json:"read_only_binds"`
	// True: this program is updated only by re-running the install command, never
	// by itself when Argus serves a newer build.
	ManualUpdates bool `json:"manual_updates"`
	// True: on a Proxmox server, never add the companion to a guest when Argus
	// asks (Add Argus on a guest row).
	NoGuestInstalls bool `json:"no_guest_installs"`
	// True: never update Argus when it asks (Settings, Update Argus).
	NoArgusUpdates bool `json:"no_argus_updates"`
	// Compose keys this host also accepts, beyond the ones it knows (policy.go).
	AllowedKeys []string `json:"allowed_keys"`
}

func defaultPolicy() *Policy {
	return &Policy{
		AllowedCaps:   []string{"CHOWN", "DAC_OVERRIDE", "FOWNER", "KILL", "NET_BIND_SERVICE", "NET_RAW", "SETGID", "SETUID"},
		BindRoots:     []string{"/srv", "/opt", "/mnt", "/media"},
		ReadOnlyBinds: []string{"/etc/localtime", "/etc/timezone", "/etc/ssl/certs"},
	}
}

// loadPolicy reads the policy file; a missing file means the defaults. A file
// that does not parse is an error, and the deployer then refuses everything.
func loadPolicy(path string) (*Policy, error) {
	b, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return defaultPolicy(), nil
	}
	if err != nil {
		return nil, err
	}
	p := &Policy{}
	dec := json.NewDecoder(strings.NewReader(string(b)))
	dec.DisallowUnknownFields()
	if err := dec.Decode(p); err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	return p, nil
}

// Compose keys a stack may use, as `docker compose config` resolves it. A key
// not named here is refused until the host's policy lists it in allowed_keys:
// Compose gains keys that reach the host (use_api_socket, provider) faster than
// a list of forbidden ones could follow. The ones that can reach the host are
// judged below by the policy's own rules.
var topKeys = keySet("name", "services", "networks", "volumes", "secrets", "configs")

var serviceKeys = keySet(
	"annotations", "attach", "blkio_config", "cap_add", "cap_drop", "cgroup", "command",
	"configs", "container_name", "cpu_count", "cpu_percent", "cpu_period", "cpu_quota",
	"cpu_rt_period", "cpu_rt_runtime", "cpu_shares", "cpus", "cpuset", "depends_on", "deploy",
	"develop", "devices", "dns", "dns_opt", "dns_search", "domainname", "entrypoint",
	"environment", "expose", "external_links", "extra_hosts", "gpus", "group_add",
	"healthcheck", "hostname", "image", "init", "ipc", "labels", "links", "logging",
	"mac_address", "mem_limit", "mem_reservation", "mem_swappiness", "memswap_limit",
	"network_mode", "networks", "oom_kill_disable", "oom_score_adj", "pid", "pids_limit",
	"platform", "ports", "post_start", "pre_stop", "privileged", "profiles", "pull_policy",
	"read_only", "restart", "scale", "secrets", "security_opt", "shm_size", "stdin_open",
	"stop_grace_period", "stop_signal", "storage_opt", "sysctls", "tmpfs", "tty", "ulimits",
	"user", "userns_mode", "uts", "volumes", "working_dir")

func keySet(keys ...string) map[string]bool {
	m := make(map[string]bool, len(keys))
	for _, k := range keys {
		m[k] = true
	}
	return m
}

type composeService struct {
	Privileged  bool     `json:"privileged"`
	NetworkMode string   `json:"network_mode"`
	Pid         string   `json:"pid"`
	Ipc         string   `json:"ipc"`
	Uts         string   `json:"uts"`
	Cgroup      string   `json:"cgroup"`
	UsernsMode  string   `json:"userns_mode"`
	CapAdd      []string `json:"cap_add"`
	Devices     []any    `json:"devices"`
	Gpus        any      `json:"gpus"`
	SecurityOpt []string `json:"security_opt"`
	Volumes     []struct {
		Type     string `json:"type"`
		Source   string `json:"source"`
		ReadOnly bool   `json:"read_only"`
	} `json:"volumes"`
	Deploy struct {
		Resources struct {
			Reservations struct {
				Devices []any `json:"devices"`
			} `json:"reservations"`
		} `json:"resources"`
	} `json:"deploy"`
}

type composeConfig struct {
	Services map[string]composeService `json:"services"`
	Volumes  map[string]struct {
		Driver     string            `json:"driver"`
		DriverOpts map[string]string `json:"driver_opts"`
	} `json:"volumes"`
	Secrets map[string]fileSource `json:"secrets"`
	Configs map[string]fileSource `json:"configs"`
}

type fileSource struct {
	File string `json:"file"`
}

// Check returns what the stack does that the policy forbids. config is
// `docker compose config --format json` run in stage; relative paths there
// point into stage and are judged as the stack folder `dir` they will become.
func (p *Policy) Check(config []byte, stage, dir string) []string {
	var c composeConfig
	var keys struct {
		Top      map[string]json.RawMessage
		Services map[string]map[string]json.RawMessage
	}
	if err := json.Unmarshal(config, &c); err != nil {
		return []string{"the compose file did not resolve: " + err.Error()}
	}
	_ = json.Unmarshal(config, &keys.Top)
	if raw, ok := keys.Top["services"]; ok {
		_ = json.Unmarshal(raw, &keys.Services)
	}
	var bad []string
	unknown := func(where, k string) {
		bad = append(bad, fmt.Sprintf("%s`%s`, which this host's policy does not know: if it is safe, add %q to allowed_keys in %s", where, k, k, policyFile))
	}
	for _, k := range sortedKeys(keys.Top) {
		if !topKeys[k] && !strings.HasPrefix(k, "x-") && !slices.Contains(p.AllowedKeys, k) {
			unknown("", k)
		}
	}
	for _, n := range sortedKeys(c.Services) {
		s := c.Services[n]
		no := func(why string) { bad = append(bad, n+": "+why) }
		for _, k := range sortedKeys(keys.Services[n]) {
			if !serviceKeys[k] && !strings.HasPrefix(k, "x-") && !slices.Contains(p.AllowedKeys, k) {
				unknown(n+": ", k)
			}
		}
		if s.Privileged && !p.AllowPrivileged {
			no("privileged")
		}
		if !p.AllowHostNamespaces {
			for _, ns := range [][2]string{{"network", s.NetworkMode}, {"pid", s.Pid}, {"ipc", s.Ipc},
				{"uts", s.Uts}, {"cgroup", s.Cgroup}, {"userns", s.UsernsMode}} {
				if ns[1] == "host" {
					no(ns[0] + " of the host")
				}
			}
		}
		if !p.AllowDevices && (len(s.Devices) > 0 || s.Gpus != nil || len(s.Deploy.Resources.Reservations.Devices) > 0) {
			no("devices")
		}
		for _, cp := range s.CapAdd {
			cp = strings.TrimPrefix(strings.ToUpper(cp), "CAP_")
			if !slices.Contains(p.AllowedCaps, cp) {
				no("capability " + cp)
			}
		}
		for _, o := range s.SecurityOpt {
			flat := strings.ReplaceAll(strings.ToLower(o), ":", "=")
			if strings.Contains(flat, "unconfined") || flat == "label=disable" {
				no("security_opt " + o)
			}
		}
		for _, v := range s.Volumes {
			if v.Type == "bind" {
				if why := p.bind(v.Source, v.ReadOnly, stage); why != "" {
					no(why)
				}
			}
		}
	}
	// A named volume can be a bind in disguise (driver_opts o=bind, device=/).
	// Network shares (type nfs, cifs) are not host folders and stay allowed.
	for _, n := range sortedKeys(c.Volumes) {
		v := c.Volumes[n]
		if v.Driver != "" && v.Driver != "local" {
			continue
		}
		isBind := v.DriverOpts["type"] == "none" || v.DriverOpts["type"] == "bind"
		for _, o := range strings.Split(v.DriverOpts["o"], ",") {
			isBind = isBind || o == "bind" || o == "rbind"
		}
		if isBind && v.DriverOpts["device"] != "" {
			if why := p.bind(v.DriverOpts["device"], false, stage); why != "" {
				bad = append(bad, "volume "+n+": "+why)
			}
		}
	}
	// A secret or config read from a file is that file mounted read-only.
	for kind, files := range map[string]map[string]string{"secret": fileSources(c.Secrets), "config": fileSources(c.Configs)} {
		for _, n := range sortedKeys(files) {
			if why := p.bind(files[n], true, stage); why != "" {
				bad = append(bad, kind+" "+n+": "+why)
			}
		}
	}
	_ = dir
	return bad
}

// CheckFiles judges the host files Compose itself reads into a stack: env_file
// and label_file. The resolved config has already merged them away, so raw is
// `docker compose config --format json --no-interpolate`, which keeps them.
func (p *Policy) CheckFiles(raw []byte, stage string) []string {
	var c struct {
		Services map[string]struct {
			EnvFile   []struct{ Path string } `json:"env_file"`
			LabelFile any                     `json:"label_file"`
		} `json:"services"`
	}
	if err := json.Unmarshal(raw, &c); err != nil {
		return []string{"the compose file did not resolve: " + err.Error()}
	}
	var bad []string
	for _, n := range sortedKeys(c.Services) {
		s := c.Services[n]
		paths := []string{}
		for _, e := range s.EnvFile {
			paths = append(paths, e.Path)
		}
		switch l := s.LabelFile.(type) {
		case string:
			paths = append(paths, l)
		case []any:
			for _, x := range l {
				if str, ok := x.(string); ok {
					paths = append(paths, str)
				}
			}
		}
		for _, path := range paths {
			if strings.Contains(path, "$") {
				bad = append(bad, n+": a file path made from a variable ("+path+")")
			} else if why := p.bind(path, true, stage); why != "" {
				bad = append(bad, n+": "+strings.Replace(why, "a bind from", "a file read from", 1))
			}
		}
	}
	return bad
}

// bind says why a host path may not reach a container, or "" when it may: the
// stack's own folder, a folder in bind_roots, or (read-only) a read_only_binds file.
func (p *Policy) bind(source string, readOnly bool, stage string) string {
	src := filepath.Clean(source)
	if src == stage || strings.HasPrefix(src, stage+"/") {
		return ""
	}
	if strings.HasSuffix(src, "docker.sock") {
		if p.AllowDockerSocket {
			return ""
		}
		return "the Docker socket"
	}
	if readOnly && slices.Contains(p.ReadOnlyBinds, src) {
		return ""
	}
	if slices.ContainsFunc(p.BindRoots, func(root string) bool {
		root = filepath.Clean(root)
		return src == root || strings.HasPrefix(src, root+"/")
	}) {
		return ""
	}
	return "a bind from " + src
}

func fileSources(m map[string]fileSource) map[string]string {
	out := map[string]string{}
	for n, v := range m {
		if v.File != "" {
			out[n] = v.File
		}
	}
	return out
}

func sortedKeys[V any](m map[string]V) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	slices.Sort(out)
	return out
}
