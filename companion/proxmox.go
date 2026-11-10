package main

import (
	"bufio"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"
)

// On a Proxmox server: its guests, and the newest vzdump backup of each that
// this server can see. Argus uses a guest's backup as the safety net for
// patching it. Read from files only (guest configs, storage.cfg, the dump
// folders), so it works inside the companion's read-only sandbox.
type Proxmox struct {
	Node   string     `json:"node"`
	Guests []PveGuest `json:"guests"`
	Error  string     `json:"error,omitempty"`
}

type PveGuest struct {
	VMID   int        `json:"vmid"`
	Name   string     `json:"name"`
	Type   string     `json:"type"` // qemu or lxc
	Status string     `json:"status"`
	Backup *PveBackup `json:"backup,omitempty"`
}

type PveBackup struct {
	At      float64 `json:"at"`
	Storage string  `json:"storage"`
	Size    int64   `json:"size"`
}

type pveStorage struct {
	ID, Type, Path string
	Content        []string
	Nodes          []string
	Disabled       bool
}

const proxmoxEvery = 15 * time.Minute

var dumpName = regexp.MustCompile(`^vzdump-(qemu|lxc)-(\d+)-\d{4}_\d\d_\d\d-\d\d_\d\d_\d\d\.(vma|tar)(\.(zst|gz|lzo))?$`)

// PVEDir is /etc/pve; RunDir and CgroupDir tell which guests are running.
type proxmoxPaths struct {
	PVEDir, RunDir, CgroupDir, Proc string
}

var defaultProxmoxPaths = proxmoxPaths{"/etc/pve", "/var/run/qemu-server", "/sys/fs/cgroup/lxc", "/proc"}

func isProxmox(p proxmoxPaths) bool {
	_, err := os.Stat(filepath.Join(p.PVEDir, "storage.cfg"))
	return err == nil
}

func collectProxmox(p proxmoxPaths, node string) *Proxmox {
	out := &Proxmox{Node: node, Guests: []PveGuest{}}
	byKey := map[string]int{}
	for _, kind := range []struct{ typ, dir, nameKey string }{
		{"qemu", "qemu-server", "name"}, {"lxc", "lxc", "hostname"},
	} {
		files, _ := filepath.Glob(filepath.Join(p.PVEDir, "nodes", node, kind.dir, "*.conf"))
		for _, f := range files {
			id, err := strconv.Atoi(strings.TrimSuffix(filepath.Base(f), ".conf"))
			if err != nil {
				continue
			}
			g := PveGuest{VMID: id, Type: kind.typ, Name: confValue(f, kind.nameKey),
				Status: guestStatus(p, kind.typ, id)}
			out.Guests = append(out.Guests, g)
		}
	}
	sort.Slice(out.Guests, func(i, j int) bool { return out.Guests[i].VMID < out.Guests[j].VMID })
	for i, g := range out.Guests {
		byKey[g.Type+"/"+strconv.Itoa(g.VMID)] = i
	}
	cfg, err := os.ReadFile(filepath.Join(p.PVEDir, "storage.cfg"))
	if err != nil {
		out.Error = "cannot read storage.cfg: " + err.Error()
		return out
	}
	for _, s := range parseStorageCfg(string(cfg)) {
		if s.Disabled || s.Path == "" || !contains(s.Content, "backup") ||
			(len(s.Nodes) > 0 && !contains(s.Nodes, node)) {
			continue
		}
		entries, err := os.ReadDir(filepath.Join(s.Path, "dump"))
		if err != nil {
			if out.Error == "" {
				out.Error = "backup storage " + s.ID + ": " + err.Error()
			}
			continue
		}
		for _, e := range entries {
			m := dumpName.FindStringSubmatch(e.Name())
			if m == nil || !e.Type().IsRegular() {
				continue
			}
			i, ok := byKey[m[1]+"/"+m[2]]
			if !ok {
				continue
			}
			g := &out.Guests[i]
			// A storage shared with another Proxmox server can hold its guests
			// under the same number. vzdump's notes name the guest by default.
			path := filepath.Join(s.Path, "dump", e.Name())
			if notes := firstLine(path + ".notes"); notes != "" && g.Name != "" &&
				!strings.Contains(strings.ToLower(notes), strings.ToLower(g.Name)) {
				continue
			}
			info, err := e.Info()
			if err != nil {
				continue
			}
			at := float64(info.ModTime().Unix())
			if g.Backup == nil || at > g.Backup.At {
				g.Backup = &PveBackup{At: at, Storage: s.ID, Size: info.Size()}
			}
		}
	}
	return out
}

// confValue reads "key: value" from the main section of a guest config.
func confValue(path, key string) string {
	f, err := os.Open(path)
	if err != nil {
		return ""
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		line := sc.Text()
		if strings.HasPrefix(line, "[") { // snapshots follow the main section
			break
		}
		if v, ok := strings.CutPrefix(line, key+":"); ok {
			return strings.TrimSpace(v)
		}
	}
	return ""
}

func guestStatus(p proxmoxPaths, typ string, id int) string {
	sid := strconv.Itoa(id)
	if typ == "lxc" {
		if _, err := os.Stat(filepath.Join(p.CgroupDir, sid)); err == nil {
			return "running"
		}
		return "stopped"
	}
	pid := firstLine(filepath.Join(p.RunDir, sid+".pid"))
	if pid != "" {
		if _, err := os.Stat(filepath.Join(p.Proc, pid)); err == nil {
			return "running"
		}
	}
	return "stopped"
}

// parseStorageCfg reads Proxmox's storage.cfg: "type: id" then indented "key value" lines.
func parseStorageCfg(text string) []pveStorage {
	var out []pveStorage
	var cur *pveStorage
	for _, line := range strings.Split(text, "\n") {
		if strings.TrimSpace(line) == "" || strings.HasPrefix(strings.TrimSpace(line), "#") {
			continue
		}
		if line[0] != ' ' && line[0] != '\t' {
			typ, id, ok := strings.Cut(line, ":")
			if !ok {
				cur = nil
				continue
			}
			out = append(out, pveStorage{ID: strings.TrimSpace(id), Type: strings.TrimSpace(typ)})
			cur = &out[len(out)-1]
			if cur.Type == "nfs" || cur.Type == "cifs" || cur.Type == "cephfs" {
				cur.Path = "/mnt/pve/" + cur.ID // unless a path line says otherwise
			}
			continue
		}
		if cur == nil {
			continue
		}
		k, v, _ := strings.Cut(strings.TrimSpace(line), " ")
		v = strings.TrimSpace(v)
		switch k {
		case "path":
			cur.Path = v
		case "content":
			cur.Content = splitNonEmpty(v, ",")
		case "nodes":
			cur.Nodes = splitNonEmpty(v, ",")
		case "disable":
			cur.Disabled = v == "" || v == "1"
		}
	}
	// Built in: "local" is a dir storage at /var/lib/vz when storage.cfg names it.
	for i := range out {
		if out[i].Type == "dir" && out[i].Path == "" && out[i].ID == "local" {
			out[i].Path = "/var/lib/vz"
		}
	}
	return out
}

func contains(list []string, s string) bool {
	for _, x := range list {
		if strings.TrimSpace(x) == s {
			return true
		}
	}
	return false
}
