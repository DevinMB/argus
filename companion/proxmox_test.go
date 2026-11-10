package main

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

func write(t *testing.T, path, text string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(text), 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestProxmoxGuestsAndTheirNewestBackup(t *testing.T) {
	root := t.TempDir()
	p := proxmoxPaths{filepath.Join(root, "pve"), filepath.Join(root, "run"),
		filepath.Join(root, "cg"), filepath.Join(root, "proc")}
	nas, other := filepath.Join(root, "mnt", "nas"), filepath.Join(root, "mnt", "other")
	write(t, filepath.Join(p.PVEDir, "storage.cfg"), `dir: local
	path `+filepath.Join(root, "vz")+`
	content iso,vztmpl

nfs: nas
	export /backups
	path `+nas+`
	server 10.0.0.2
	content backup

dir: other
	path `+other+`
	content backup
	nodes elsewhere
`)
	node := filepath.Join(p.PVEDir, "nodes", "pve1")
	write(t, filepath.Join(node, "qemu-server", "101.conf"), "boot: order=scsi0\nname: shop\nmemory: 2048\n\n[snap1]\nname: old\n")
	write(t, filepath.Join(node, "lxc", "105.conf"), "arch: amd64\nhostname: notes\n")
	write(t, filepath.Join(node, "qemu-server", "107.conf"), "name: never-backed-up\n")
	write(t, filepath.Join(p.RunDir, "101.pid"), "4242\n")
	write(t, filepath.Join(p.Proc, "4242", "stat"), "")

	dump := filepath.Join(nas, "dump")
	week := time.Now().Add(-7 * 24 * time.Hour)
	day := time.Now().Add(-24 * time.Hour)
	for name, at := range map[string]time.Time{
		"vzdump-qemu-101-2027_01_10-02_00_00.vma.zst": week,
		"vzdump-qemu-101-2027_01_16-02_00_00.vma.zst": day,
		"vzdump-lxc-105-2027_01_16-02_10_00.tar.zst":  day,
		// Another server's guest 105 on the same storage: its notes name it.
		"vzdump-lxc-105-2027_01_17-02_10_00.tar.zst": time.Now(),
		"vzdump-qemu-101-2027_01_17-02_00_00.log":    time.Now(),
	} {
		write(t, filepath.Join(dump, name), "x")
		os.Chtimes(filepath.Join(dump, name), at, at)
	}
	write(t, filepath.Join(dump, "vzdump-lxc-105-2027_01_16-02_10_00.tar.zst.notes"), "notes\n")
	write(t, filepath.Join(dump, "vzdump-lxc-105-2027_01_17-02_10_00.tar.zst.notes"), "someone-else\n")
	// A storage limited to another node is not read.
	write(t, filepath.Join(other, "dump", "vzdump-qemu-107-2027_01_17-02_00_00.vma"), "x")

	got := collectProxmox(p, "pve1")
	if got.Error != "" || len(got.Guests) != 3 {
		t.Fatalf("got %+v", got)
	}
	shop, notes, never := got.Guests[0], got.Guests[1], got.Guests[2]
	if shop.VMID != 101 || shop.Name != "shop" || shop.Type != "qemu" || shop.Status != "running" {
		t.Errorf("shop: %+v", shop)
	}
	if shop.Backup == nil || shop.Backup.Storage != "nas" || int64(shop.Backup.At) != day.Unix() {
		t.Errorf("shop's newest backup: %+v", shop.Backup)
	}
	if notes.Name != "notes" || notes.Status != "stopped" || notes.Backup == nil ||
		int64(notes.Backup.At) != day.Unix() {
		t.Errorf("notes: %+v %+v", notes, notes.Backup)
	}
	if never.VMID != 107 || never.Backup != nil {
		t.Errorf("never: %+v", never)
	}
}

func TestProxmoxMissingDumpFolderIsSaid(t *testing.T) {
	root := t.TempDir()
	p := proxmoxPaths{PVEDir: root}
	write(t, filepath.Join(root, "storage.cfg"), "nfs: unas\n\tpath "+filepath.Join(root, "gone")+"\n\tcontent backup\n")
	got := collectProxmox(p, "pve1")
	if got.Error == "" || len(got.Guests) != 0 {
		t.Errorf("got %+v", got)
	}
	if !isProxmox(p) || isProxmox(proxmoxPaths{PVEDir: filepath.Join(root, "nope")}) {
		t.Error("isProxmox")
	}
}

func TestProxmoxIsReadInTheBackground(t *testing.T) {
	root := t.TempDir()
	write(t, filepath.Join(root, "storage.cfg"), "")
	write(t, filepath.Join(root, "nodes", "pve1", "lxc", "105.conf"), "hostname: notes\n")
	c := &Collector{Proxmox: proxmoxPaths{PVEDir: root}}
	first := c.proxmox("pve1") // the read has only started
	deadline := time.Now().Add(5 * time.Second)
	got := first
	for got == nil && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
		got = c.proxmox("pve1")
	}
	if first != nil || got == nil || len(got.Guests) != 1 || got.Guests[0].Name != "notes" {
		t.Fatalf("first %+v, then %+v", first, got)
	}
	// A read that hangs: the last result goes out, saying so after ten minutes.
	c.pveMu.Lock()
	c.pveSince = time.Now().Add(-11 * time.Minute)
	c.pveMu.Unlock()
	if s := c.proxmox("pve1"); s.Error == "" || len(s.Guests) != 1 {
		t.Errorf("stale: %+v", s)
	}
}
