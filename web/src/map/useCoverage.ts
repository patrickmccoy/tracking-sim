// Coverage for every sensor, fetched when a sensor's position or coverage
// changes (debounced, so dragging a sensor redraws it a few times a second),
// cached by what it depends on.

import { useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError, type CoverageData, type SensorSpec } from "../api";
import { coverageSteps } from "../theme";

type Result = { data: CoverageData } | { errors: Record<string, string> };

/** Validation errors by dotted path inside the sensor ("scan.period_s"). */
function fieldErrors(error: unknown): Record<string, string> {
  if (error instanceof ApiError && Array.isArray(error.detail)) {
    const out: Record<string, string> = {};
    for (const e of error.detail as { loc: (string | number)[]; msg: string }[]) {
      // The first entry is the sensor type Pydantic picked.
      out[e.loc.slice(1).join(".")] = e.msg.replace(/^Value error, /, "");
    }
    return out;
  }
  return { "": error instanceof Error ? error.message : String(error) };
}

export interface CoverageState {
  collection: GeoJSON.FeatureCollection;
  errors: Record<string, Record<string, string>>; // by sensor id
  busy: boolean;
}

export function useCoverage(sensors: SensorSpec[], terrain: Record<string, unknown> | null): CoverageState {
  const cache = useRef(new Map<string, Result>());
  const [version, setVersion] = useState(0);
  const [busy, setBusy] = useState(false);

  const keys = sensors.map((s) =>
    JSON.stringify({ t: s.type, p: s.position, c: s.coverage, terrain }),
  );
  const keyList = keys.join("\n");

  useEffect(() => {
    const missing = sensors.map((s, i) => [s, keys[i]!] as const).filter(([, k]) => !cache.current.has(k));
    if (!missing.length) return;
    const controller = new AbortController();
    setBusy(true);
    const timer = setTimeout(async () => {
      await Promise.all(
        missing.map(async ([sensor, key]) => {
          try {
            const data = await api.coverage(sensor, terrain, coverageSteps.map((h) => h.height_m), controller.signal);
            cache.current.set(key, { data });
          } catch (error) {
            if (controller.signal.aborted) return;
            cache.current.set(key, { errors: fieldErrors(error) });
          }
        }),
      );
      if (cache.current.size > 200) cache.current.clear(); // a long drag leaves many
      if (!controller.signal.aborted) {
        setBusy(false);
        setVersion((v) => v + 1);
      }
    }, 150);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [keyList]);

  const ids = sensors.map((s) => s.id).join("\n");
  const { collection, errors } = useMemo(() => {
    const features: GeoJSON.Feature[] = [];
    const errors: Record<string, Record<string, string>> = {};
    sensors.forEach((s, i) => {
      const result = cache.current.get(keys[i]!);
      if (!result) return;
      if ("errors" in result) errors[s.id] = result.errors;
      else
        for (const f of result.data.features) {
          features.push({ ...f, properties: { ...f.properties, sensor: s.id } } as GeoJSON.Feature);
        }
    });
    // Highest first, so the lowest (smallest, darkest) draws on top.
    features.sort((a, b) => (b.properties?.height_m ?? 0) - (a.properties?.height_m ?? 0));
    const collection: GeoJSON.FeatureCollection = { type: "FeatureCollection", features };
    return { collection, errors };
  }, [keyList, ids, version]);
  return { collection, errors, busy };
}
