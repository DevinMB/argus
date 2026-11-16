package main

import (
	"reflect"
	"strings"
	"testing"
)

func TestRepoOfDropsTagAndDigest(t *testing.T) {
	for in, want := range map[string]string{
		"ghcr.io/you/app:sha-1":          "ghcr.io/you/app",
		"ghcr.io/you/app@sha256:abc":     "ghcr.io/you/app",
		"localhost:5000/app:1":           "localhost:5000/app",
		"localhost:5000/app":             "localhost:5000/app",
		"postgres:16":                    "postgres",
		"ghcr.io/you/app:1@sha256:abc12": "ghcr.io/you/app",
	} {
		if got := repoOf(in); got != want {
			t.Errorf("repoOf(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestADeployRemovesOnlyTheImagesItMovedOnFrom(t *testing.T) {
	// deals-lab: 14 images of one app, about 20 GB, one in use (RCA 2026-11-16).
	images := []dockerImage{
		{ID: "now", RepoTags: []string{"ghcr.io/you/deals:sha-3"}},
		{ID: "before", RepoDigests: []string{"ghcr.io/you/deals@sha256:2"}},         // what it ran until now
		{ID: "older", RepoDigests: []string{"ghcr.io/you/deals@sha256:1"}, Size: 5}, // goes
		{ID: "oldest", RepoTags: []string{"ghcr.io/you/deals:sha-0"}, Size: 9},      // goes, first
		{ID: "stopped", RepoTags: []string{"ghcr.io/you/deals:beta"}},               // a stopped container uses it
		{ID: "kafka", RepoTags: []string{"local/kafka:custom"}},                     // another repo: never
		{ID: "built", RepoTags: []string{"<none>:<none>"}},                          // no repo: never
		{ID: "pg", RepoTags: []string{"postgres:15"}},                               // not this stack's repo
	}
	repos := map[string]bool{"ghcr.io/you/deals": true}
	used := map[string]bool{"now": true, "stopped": true}
	keep := map[string]bool{"before": true}
	var ids []string
	for _, img := range staleImages(images, repos, used, keep) {
		ids = append(ids, img.ID)
	}
	if !reflect.DeepEqual(ids, []string{"oldest", "older"}) {
		t.Fatalf("would remove %v", ids)
	}
}

func TestAHealthyDeploySaysWhatItCleanedUp(t *testing.T) {
	d, f := newTestDeployer(t)
	var asked []string
	d.prune = func(project string, keep map[string]bool) string {
		asked = append(asked, project)
		return "removed 2 old images, freeing 3.1 GB"
	}
	plan := &DeployPlan{Stacks: []StackSpec{stack("d1", "v1")}}
	d.Apply(plan)
	r := d.Results(plan)[0]
	if r.Result != "ok" || !reflect.DeepEqual(asked, []string{"web"}) || !strings.Contains(r.Detail, "removed 2 old images") {
		t.Fatalf("got %+v, pruned %v", r, asked)
	}
	// A deploy that is not healthy removes nothing: its old image is the way back.
	f.failUp = true
	plan = &DeployPlan{Stacks: []StackSpec{stack("d2", "v2")}}
	d.Apply(plan)
	if len(asked) != 1 {
		t.Fatalf("an unhealthy deploy pruned: %v", asked)
	}
}

func TestOnStartEveryDeployedStackIsCleanedOnce(t *testing.T) {
	d, _ := newTestDeployer(t)
	d.Apply(&DeployPlan{Stacks: []StackSpec{stack("d1", "v1")}})
	var asked []string
	d.prune = func(project string, keep map[string]bool) string {
		asked = append(asked, project)
		if keep != nil {
			t.Fatalf("nothing to keep on start, got %v", keep)
		}
		return "removed 12 old images, freeing 13.0 GB"
	}
	if got := d.PruneAll(); !reflect.DeepEqual(got, []string{"web: removed 12 old images, freeing 13.0 GB"}) || !reflect.DeepEqual(asked, []string{"web"}) {
		t.Fatalf("got %v, asked %v", got, asked)
	}
}
