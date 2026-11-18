package main

import (
	"strings"
	"testing"
)

const stage = "/var/lib/argus-companion/.stage-1"

func refusals(t *testing.T, p *Policy, config string) []string {
	t.Helper()
	return p.Check([]byte(config), stage, "/opt/stacks/web")
}

// Each of these reaches the host in a way the policy's rules are there to stop,
// without using the service keys the first version of the check looked at.
func TestThePolicyRefusesEveryWayToTheHost(t *testing.T) {
	cases := map[string]struct{ config, want string }{
		"a named volume that binds /": {`{"services":{"web":{"image":"a","volumes":[{"type":"volume","source":"root","target":"/h"}]}},
			"volumes":{"root":{"driver":"local","driver_opts":{"type":"none","o":"bind","device":"/"}}}}`, "volume root: a bind from /"},
		"a named volume with o=rbind": {`{"services":{"web":{"image":"a"}},
			"volumes":{"etc":{"driver_opts":{"o":"rbind,ro","device":"/etc"}}}}`, "volume etc: a bind from /etc"},
		"a secret from a host file": {`{"services":{"web":{"image":"a","secrets":[{"source":"s"}]}},
			"secrets":{"s":{"file":"/etc/shadow"}}}`, "secret s: a bind from /etc/shadow"},
		"a config from a host file": {`{"services":{"web":{"image":"a"}},
			"configs":{"k":{"file":"/root/.ssh/authorized_keys"}}}`, "config k: a bind from /root/.ssh/authorized_keys"},
		"a build from a host folder":  {`{"services":{"web":{"build":{"context":"/etc"}}}}`, "web: `build`"},
		"the host's cgroup":           {`{"services":{"web":{"image":"a","cgroup":"host"}}}`, "web: cgroup of the host"},
		"the host's uts":              {`{"services":{"web":{"image":"a","uts":"host"}}}`, "web: uts of the host"},
		"device cgroup rules":         {`{"services":{"web":{"image":"a","device_cgroup_rules":["a *:* rwm"]}}}`, "web: `device_cgroup_rules`"},
		"another container's volumes": {`{"services":{"web":{"image":"a","volumes_from":["container:x"]}}}`, "web: `volumes_from`"},
		"SELinux labels off":          {`{"services":{"web":{"image":"a","security_opt":["label:disable"]}}}`, "web: security_opt label:disable"},
		"seccomp off":                 {`{"services":{"web":{"image":"a","security_opt":["seccomp=unconfined"]}}}`, "web: security_opt seccomp=unconfined"},
		"the Docker API socket":       {`{"services":{"web":{"image":"a","use_api_socket":true}}}`, "web: `use_api_socket`"},
		"a provider service":          {`{"services":{"web":{"provider":{"type":"x"}}}}`, "web: `provider`"},
		"GPUs":                        {`{"services":{"web":{"image":"a","gpus":"all"}}}`, "web: devices"},
		"reserved devices": {`{"services":{"web":{"image":"a","deploy":{"resources":{"reservations":{"devices":[{"capabilities":["gpu"]}]}}}}}}`,
			"web: devices"},
		"an unknown top-level key": {`{"services":{"web":{"image":"a"}},"models":{"m":{}}}`, "`models`"},
	}
	for name, c := range cases {
		bad := refusals(t, defaultPolicy(), c.config)
		if !strings.Contains(strings.Join(bad, "\n"), c.want) {
			t.Errorf("%s: got %q, want one containing %q", name, bad, c.want)
		}
	}
}

// What real stacks use stays allowed: named volumes, a network share, a config
// written inline or kept beside the compose file, a seccomp profile, resource
// limits, and extension keys.
func TestThePolicyLetsOrdinaryStacksThrough(t *testing.T) {
	config := `{"name":"web","x-common":{},"networks":{"default":{"name":"web_default"}},
	  "services":{"web":{"image":"a","restart":"unless-stopped","mem_limit":"1073741824","shm_size":"268435456",
	    "cap_drop":["ALL"],"cap_add":["CHOWN"],"read_only":true,"tmpfs":["/tmp"],"user":"1000:1000",
	    "security_opt":["seccomp=` + stage + `/seccomp.json","no-new-privileges:true"],
	    "healthcheck":{"test":["CMD","true"]},"logging":{"driver":"json-file"},"x-note":"hi",
	    "volumes":[{"type":"volume","source":"data","target":"/data"},
	               {"type":"bind","source":"` + stage + `/conf","target":"/conf","read_only":true},
	               {"type":"bind","source":"/srv/web/data","target":"/d"}],
	    "configs":[{"source":"inline"},{"source":"beside"}]}},
	  "volumes":{"data":{"name":"web_data"},
	             "share":{"driver":"local","driver_opts":{"type":"nfs","o":"addr=192.0.2.10,nfsvers=3,rw","device":":/export/web"}}},
	  "configs":{"inline":{"content":"a: 1"},"beside":{"file":"` + stage + `/app.conf"}}}`
	if bad := refusals(t, defaultPolicy(), config); len(bad) != 0 {
		t.Fatalf("an ordinary stack was refused: %q", bad)
	}
}

// A key the companion does not know is refused with the fix, and the host's
// owner can allow it.
func TestAKeyThePolicyDoesNotKnowCanBeAllowedOnTheHost(t *testing.T) {
	config := `{"services":{"web":{"image":"a","runtime":"runsc"}}}`
	bad := refusals(t, defaultPolicy(), config)
	if len(bad) != 1 || !strings.Contains(bad[0], `add "runtime" to allowed_keys in /etc/argus-companion/policy.json`) {
		t.Fatalf("got %q", bad)
	}
	p := defaultPolicy()
	p.AllowedKeys = []string{"runtime"}
	if bad := refusals(t, p, config); len(bad) != 0 {
		t.Fatalf("allowed_keys did not allow it: %q", bad)
	}
}

// Files Compose reads into a stack itself (env_file, label_file) follow the
// same folders as binds.
func TestFilesComposeReadsFollowTheBindRules(t *testing.T) {
	raw := `{"services":{"web":{"image":"a",
	  "env_file":[{"path":"/srv/stacks/web/app.env","required":true},{"path":"` + stage + `/.env.local","required":false},
	              {"path":"/etc/shadow","required":true},{"path":"${HOME}/x.env","required":true}],
	  "label_file":"/root/labels"}}}`
	bad := defaultPolicy().CheckFiles([]byte(raw), stage)
	want := []string{"web: a file read from /etc/shadow", "web: a file path made from a variable (${HOME}/x.env)",
		"web: a file read from /root/labels"}
	if strings.Join(bad, "\n") != strings.Join(want, "\n") {
		t.Fatalf("got %q\nwant %q", bad, want)
	}
}
