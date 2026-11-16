package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"sort"
	"strings"
	"syscall"
	"time"
)

// Old images (design decision 24). Every deploy that moves a tag leaves the image
// it replaced on the host, and nothing else removes it: deals-lab filled its
// 30 GB disk twice in two weeks with old builds of one app (RCA, 2026-11-16).
// After a healthy deploy the companion removes the images it moved on from:
//
//   - only images of the repositories the stack runs now (ghcr.io/you/app),
//     never anything else on the host (a locally built image may exist nowhere
//     else);
//   - never one any container uses, running or stopped (Docker refuses anyway:
//     nothing here forces);
//   - and never the images the stack ran just before this deploy, so going back
//     one version needs no download. An older one can be pulled again.

type dockerImage struct {
	ID          string   `json:"Id"`
	RepoTags    []string `json:"RepoTags"`
	RepoDigests []string `json:"RepoDigests"`
	Size        int64    `json:"Size"`
}

// repoOf is an image reference without its tag or digest:
// ghcr.io/a/b:sha-1 and ghcr.io/a/b@sha256:... are both ghcr.io/a/b.
func repoOf(ref string) string {
	if i := strings.Index(ref, "@"); i >= 0 {
		ref = ref[:i]
	}
	if i := strings.LastIndex(ref, ":"); i > strings.LastIndex(ref, "/") {
		ref = ref[:i]
	}
	return ref
}

func (img dockerImage) repos() []string {
	var out []string
	for _, r := range append(append([]string{}, img.RepoTags...), img.RepoDigests...) {
		if r == "<none>:<none>" || r == "<none>@<none>" {
			continue
		}
		out = append(out, repoOf(r))
	}
	return out
}

// staleImages picks what a healthy deploy of a stack may remove: images of the
// repositories it runs now (repos), that no container uses (used), other than
// the ones it ran before (keep). Biggest first.
func staleImages(images []dockerImage, repos, used, keep map[string]bool) []dockerImage {
	var out []dockerImage
	for _, img := range images {
		if used[img.ID] || keep[img.ID] {
			continue
		}
		for _, r := range img.repos() {
			if repos[r] {
				out = append(out, img)
				break
			}
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Size > out[j].Size })
	return out
}

func dockerClient(socket string, timeout time.Duration) *http.Client {
	return &http.Client{Timeout: timeout, Transport: &http.Transport{
		DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			return (&net.Dialer{}).DialContext(ctx, "unix", socket)
		},
	}}
}

func listImages(client *http.Client) ([]dockerImage, error) {
	resp, err := client.Get("http://docker/images/json")
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("docker said HTTP %d", resp.StatusCode)
	}
	var out []dockerImage
	err = json.NewDecoder(io.LimitReader(resp.Body, 16<<20)).Decode(&out)
	return out, err
}

// removeImage asks Docker to remove one image, never forcing: an image a
// container uses, or one that more than one repository names, stays.
func removeImage(client *http.Client, id string) bool {
	req, _ := http.NewRequest(http.MethodDelete, "http://docker/images/"+url.PathEscape(id), nil)
	resp, err := client.Do(req)
	if err != nil {
		return false
	}
	resp.Body.Close()
	return resp.StatusCode == http.StatusOK
}

// imagesOf is the set of image ids a Compose project's containers use.
func imagesOf(cs []Container, project string) map[string]bool {
	out := map[string]bool{}
	for _, c := range cs {
		if c.Project == project && c.imageID != "" {
			out[c.imageID] = true
		}
	}
	return out
}

// pruneAfter removes the images a project moved on from, keeping the ones it ran
// before (keep). It returns a sentence for the deploy's result, or "" when
// nothing went. Any error leaves everything as it was.
func pruneAfter(socket, project string, keep map[string]bool) string {
	cs, err := dockerContainers(socket)
	if err != nil {
		return ""
	}
	client := dockerClient(socket, 2*time.Minute)
	images, err := listImages(client)
	if err != nil {
		return ""
	}
	byID := map[string]dockerImage{}
	for _, img := range images {
		byID[img.ID] = img
	}
	repos, used := map[string]bool{}, map[string]bool{}
	for id := range imagesOf(cs, project) {
		for _, r := range byID[id].repos() {
			repos[r] = true
		}
	}
	for _, c := range cs {
		used[c.imageID] = true
	}
	// Layers shared between versions make each image's own size say far more
	// than removing it frees, so the disk says what went.
	before := freeBytes(dockerRoot)
	n := 0
	for _, img := range staleImages(images, repos, used, keep) {
		if removeImage(client, img.ID) {
			n++
		}
	}
	if n == 0 {
		return ""
	}
	what := fmt.Sprintf("removed %d old image%s", n, map[bool]string{true: "", false: "s"}[n == 1])
	if freed := freeBytes(dockerRoot) - before; before > 0 && freed > 1<<20 {
		what += ", freeing " + humanBytes(freed)
	}
	return what
}

const dockerRoot = "/var/lib/docker"

// freeBytes is the space free to use on the filesystem holding path, or 0.
func freeBytes(path string) int64 {
	var st syscall.Statfs_t
	if syscall.Statfs(path, &st) != nil {
		return 0
	}
	return int64(st.Bavail) * int64(st.Bsize)
}

func humanBytes(n int64) string {
	switch {
	case n >= 1<<30:
		return fmt.Sprintf("%.1f GB", float64(n)/(1<<30))
	case n >= 1<<20:
		return fmt.Sprintf("%d MB", n>>20)
	}
	return fmt.Sprintf("%d KB", n>>10)
}
