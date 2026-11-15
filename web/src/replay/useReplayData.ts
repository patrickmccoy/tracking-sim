// A stored run's replay data, fetched once per run (tracks once per tracker).

import { useEffect, useState } from "react";
import { api, type DetectionData, type RunDetail, type TrackData, type TruthData } from "../api";

export interface ReplayData {
  detail: RunDetail | null;
  truth: TruthData | null;
  detections: DetectionData | null;
  tracks: Record<string, TrackData>;
  loading: string | null;
  error: string | null;
}

export function useReplayData(runId: string | null, trackers: string[]): ReplayData {
  const [detail, setDetail] = useState<RunDetail | null>(null);
  const [truth, setTruth] = useState<TruthData | null>(null);
  const [detections, setDetections] = useState<DetectionData | null>(null);
  const [tracks, setTracks] = useState<Record<string, TrackData>>({});
  const [loading, setLoading] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setDetail(null);
    setTruth(null);
    setDetections(null);
    setTracks({});
    setError(null);
    if (!runId) return;
    let live = true;
    setLoading("run");
    (async () => {
      try {
        const d = await api.run(runId);
        if (!live) return;
        setDetail(d);
        setLoading("truth (rebuilt from the truth file)");
        const [t, det] = await Promise.all([api.truth(runId), api.detections(runId)]);
        if (!live) return;
        setTruth(t);
        setDetections(det);
        setLoading(null);
      } catch (e) {
        if (live) {
          setError((e as Error).message);
          setLoading(null);
        }
      }
    })();
    return () => {
      live = false;
    };
  }, [runId]);

  const wanted = trackers.join("\n");
  useEffect(() => {
    if (!runId) return;
    let live = true;
    for (const tracker of trackers) {
      if (tracks[tracker]) continue;
      api
        .tracks(runId, tracker)
        .then((data) => live && setTracks((all) => ({ ...all, [tracker]: data })))
        .catch((e: Error) => live && setError(e.message));
    }
    return () => {
      live = false;
    };
  }, [runId, wanted]);

  return { detail, truth, detections, tracks, loading, error };
}
