import { useEffect, useState } from "react";
import { api, type Job, type RunRow, type ScenarioDoc } from "../api";

interface RunPanelProps {
  draft: ScenarioDoc;
  canRun: boolean;
  onOpen: (runId: string) => void;
}

const fmtTime = (iso: string) => iso.slice(0, 16).replace("T", " ");

/** Run the edited scenario, follow the job, and list stored runs. */
export function RunPanel({ draft, canRun, onOpen }: RunPanelProps) {
  const [job, setJob] = useState<Job | null>(null);
  const [runs, setRuns] = useState<RunRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [now, setNow] = useState(Date.now());

  const refresh = () => api.runs().then((r) => setRuns(r.reverse())).catch(() => undefined);
  useEffect(() => {
    void refresh();
  }, []);

  // Poll the job until it finishes; open its run when it does.
  useEffect(() => {
    if (!job || job.state === "done" || job.state === "failed") return;
    const timer = setInterval(async () => {
      setNow(Date.now());
      try {
        const next = await api.job(job.id);
        setJob(next);
        if (next.state === "done") {
          void refresh();
          onOpen(next.run_id);
        }
      } catch (e) {
        setError((e as Error).message);
      }
    }, 1000);
    return () => clearInterval(timer);
  }, [job?.id, job?.state]);

  const start = async () => {
    setError(null);
    try {
      const started = await api.startRun(draft);
      setJob(started);
      setNow(Date.now());
      if (started.state === "done") {
        void refresh();
        onOpen(started.run_id);
      }
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const active = job && (job.state === "queued" || job.state === "running");
  const elapsed = job?.started ? Math.max(0, Math.round((now - Date.parse(job.started)) / 1000)) : 0;

  return (
    <section>
      <button className="primary wide" onClick={start} disabled={!canRun || !!active}>
        {active ? "Running…" : "Run this scenario"}
      </button>
      {active && (
        <p className="small status" role="status">
          {job.state === "queued" ? "Waiting for the run before it" : `${job.stage ?? "starting"} · ${elapsed} s`}
        </p>
      )}
      {job?.state === "failed" && <p className="error small">Run failed: {job.error}</p>}
      {job?.state === "done" && job.reused && <p className="muted small">Same as a stored run: opened it.</p>}
      {error && <p className="error small">{error}</p>}
      <h3>Stored runs</h3>
      {runs.length === 0 && <p className="muted small">None yet.</p>}
      <ul className="plain runs">
        {runs.slice(0, 12).map((r) => {
          const best = [...r.trackers].sort((a, b) => a.ospa_m - b.ospa_m)[0];
          return (
            <li key={r.id}>
              <button className="list-item" onClick={() => onOpen(r.id)} title={r.id}>
                <span>
                  {r.scenario} <span className="muted small">seed {r.seed}</span>
                </span>
                <span className="muted small">{fmtTime(r.created)}</span>
              </button>
              {best && (
                <span className="muted small run-best">
                  {r.sensors.join(", ")} · best {best.tracker} {Math.round(best.ospa_m)} m
                </span>
              )}
            </li>
          );
        })}
      </ul>
    </section>
  );
}
