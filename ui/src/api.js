import { useEffect, useRef, useState } from "react";

const REFRESH_MS = 10_000;

async function getJSON(path, signal) {
  const r = await fetch(path, { signal, headers: { Accept: "application/json" } });
  if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
  return r.json();
}

// Polls the read-only API. Keeps the last good data when a refresh fails, so a
// blip shows as "stale" rather than an empty page.
export function useArgus() {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [fetchedAt, setFetchedAt] = useState(null);
  const timer = useRef(null);

  useEffect(() => {
    let ctrl;
    const load = async () => {
      ctrl = new AbortController();
      try {
        const [info, checks, heartbeats, updates, jobs] = await Promise.all([
          getJSON("/api/info", ctrl.signal),
          getJSON("/api/checks", ctrl.signal),
          getJSON("/api/heartbeats", ctrl.signal),
          getJSON("/api/updates", ctrl.signal),
          getJSON("/api/jobs", ctrl.signal),
        ]);
        setData({ info, checks, heartbeats, updates, jobs });
        setError(null);
        setFetchedAt(Date.now());
      } catch (e) {
        if (e.name !== "AbortError") setError(e.message);
      }
      timer.current = setTimeout(load, REFRESH_MS);
    };
    load();
    return () => {
      clearTimeout(timer.current);
      ctrl?.abort();
    };
  }, []);

  return { data, error, fetchedAt };
}

export async function getCheck(name, signal) {
  return getJSON(`/api/checks/${encodeURIComponent(name)}`, signal);
}

export async function getJob(name, signal) {
  return getJSON(`/api/jobs/${encodeURIComponent(name)}`, signal);
}
