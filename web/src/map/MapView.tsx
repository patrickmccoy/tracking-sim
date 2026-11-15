// A MapLibre map whose children declare its layers.
//
//   <MapView style={url} center={[lon, lat]}>
//     <GeoJsonLayer id="sensors" data={collection} layers={[...]} />
//   </MapView>
//
// Children render only once the style has loaded, so each layer component
// can add its source and layers straight away and remove them on unmount.

import { Map as MlMap, NavigationControl, ScaleControl, setWorkerUrl } from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import workerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url";
import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";

// MapLibre 6 runs tile parsing in a module worker shipped as its own file;
// Vite bundles it and hands back its URL.
setWorkerUrl(workerUrl);

const MapContext = createContext<MlMap | null>(null);

export function useMap(): MlMap {
  const map = useContext(MapContext);
  if (!map) throw new Error("useMap must be used inside <MapView>");
  return map;
}

interface MapViewProps {
  styleUrl: string;
  center: [number, number];
  zoom?: number;
  children?: ReactNode;
  /** Called with the map once it has loaded (for camera syncing). */
  onReady?: (map: MlMap) => void;
  className?: string;
}

export function MapView({ styleUrl, center, zoom = 8, children, onReady, className }: MapViewProps) {
  const container = useRef<HTMLDivElement>(null);
  const [map, setMap] = useState<MlMap | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!container.current) return;
    const m = new MlMap({
      container: container.current,
      style: styleUrl,
      center,
      zoom,
      attributionControl: { compact: true },
    });
    m.addControl(new NavigationControl({ showCompass: true }), "top-right");
    m.addControl(new ScaleControl({ unit: "metric" }), "bottom-right");
    m.on("load", () => {
      setMap(m);
      onReady?.(m);
    });
    m.on("error", (e) => {
      // A missing tile is routine; a style that fails to load is not.
      if (!m.isStyleLoaded()) setError((e.error as Error | undefined)?.message ?? "map error");
    });
    return () => {
      setMap(null);
      m.remove();
    };
    // The map is built once per style; recentring is done below.
  }, [styleUrl]);

  const [lon, lat] = center;
  useEffect(() => {
    map?.jumpTo({ center: [lon, lat] });
  }, [map, lon, lat]);

  return (
    <div className={`map ${className ?? ""}`}>
      <div ref={container} className="map-canvas" />
      {error && <div className="map-error">Base map: {error}</div>}
      {map && <MapContext.Provider value={map}>{children}</MapContext.Provider>}
    </div>
  );
}
