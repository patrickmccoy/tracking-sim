// Two replays side by side, one clock and one camera, with OSPA below.

import type { Map as MlMap } from "maplibre-gl";
import { useMemo, useRef } from "react";
import type { SensorSpec } from "../api";
import { MapView } from "../map/MapView";
import { SensorMarkers } from "../map/SensorMarkers";
import { OspaChart, type OspaSeries } from "./OspaChart";
import { ReplayLayers, type LayerOptions, type Selection } from "./ReplayLayers";
import type { ReplayData } from "./useReplayData";

export interface Side {
  label: string;
  tracker: string;
  color: string;
  data: ReplayData;
}

interface CompareViewProps {
  mapStyle: string;
  center: [number, number];
  sides: [Side, Side];
  t: number;
  window: [number, number];
  options: LayerOptions;
  selection: Selection | null;
  onSelect: (selection: Selection) => void;
  onSeek: (t: number) => void;
}

function ospaSeries(side: Side): OspaSeries {
  const score = side.data.detail?.metrics.trackers.find((s) => s.tracker === side.tracker);
  const points = score?.over_time ?? [];
  return {
    label: side.label,
    color: side.color,
    times: points.map((p) => Date.parse(p.time) / 1000),
    ospa: points.map((p) => p.ospa_m),
  };
}

export function CompareView({ mapStyle, center, sides, t, window, options, selection, onSelect, onSeek }: CompareViewProps) {
  // Moving either map moves the other; the flag stops the echo.
  const maps = useRef<(MlMap | null)[]>([null, null]);
  const syncing = useRef(false);
  const link = (i: number) => (map: MlMap) => {
    maps.current[i] = map;
    map.on("move", () => {
      const other = maps.current[1 - i];
      if (!other || syncing.current) return;
      syncing.current = true;
      other.jumpTo({ center: map.getCenter(), zoom: map.getZoom(), bearing: map.getBearing(), pitch: map.getPitch() });
      syncing.current = false;
    });
  };

  const series = useMemo(() => sides.map(ospaSeries), [sides[0].data.detail, sides[1].data.detail, sides[0].tracker, sides[1].tracker]);
  const cutoff = Number((sides[0].data.detail?.manifest.scenario as { metrics?: { cutoff_m?: number } } | undefined)?.metrics?.cutoff_m ?? 1000);

  return (
    <div className="compare">
      <div className="compare-maps">
        {sides.map((side, i) => {
          const sensors = (side.data.detail?.manifest.scenario.sensors ?? []) as SensorSpec[];
          return (
            <div key={i} className="compare-pane">
              <MapView styleUrl={mapStyle} center={center} zoom={8} onReady={link(i)}>
                <ReplayLayers
                  truth={side.data.truth}
                  detections={side.data.detections}
                  tracks={[{ tracker: side.tracker, data: side.data.tracks[side.tracker], color: side.color }]}
                  t={t}
                  options={options}
                  selection={selection}
                  onSelect={onSelect}
                />
                <SensorMarkers sensors={sensors} selected={null} onMove={() => undefined} onSelect={() => undefined} draggable={false} />
              </MapView>
              <div className="pane-label">
                <span className="swatch line" style={{ background: side.color }} />
                {side.label}
                {side.data.loading && <span className="muted small"> · loading {side.data.loading}</span>}
              </div>
            </div>
          );
        })}
      </div>
      <OspaChart series={series} window={window} t={t} cutoffM={cutoff} onSeek={onSeek} />
    </div>
  );
}
