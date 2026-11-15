// Sensors as draggable DOM markers: easy to grab, always above the data.

import { Marker } from "maplibre-gl";
import { useEffect, useRef } from "react";
import type { SensorSpec } from "../api";
import { useMap } from "./MapView";

interface SensorMarkersProps {
  sensors: SensorSpec[];
  selected: number | null;
  /** Called continuously while dragging, and with `done` on release. */
  onMove: (index: number, lat: number, lon: number, done: boolean) => void;
  onSelect: (index: number) => void;
  draggable?: boolean;
}

export function SensorMarkers({ sensors, selected, onMove, onSelect, draggable = true }: SensorMarkersProps) {
  const map = useMap();
  const markers = useRef<Marker[]>([]);
  const dragging = useRef<number | null>(null);
  const handlers = useRef({ onMove, onSelect });
  handlers.current = { onMove, onSelect };

  // One marker per sensor; rebuilt only when sensors are added or removed.
  const count = sensors.length;
  useEffect(() => {
    const made = Array.from({ length: count }, (_, i) => {
      const el = document.createElement("div");
      el.className = "sensor-marker";
      el.innerHTML = '<span class="dot"></span><span class="label"></span>';
      el.addEventListener("click", (e) => {
        e.stopPropagation();
        handlers.current.onSelect(i);
      });
      const marker = new Marker({ element: el, draggable, anchor: "top" }).setLngLat([0, 0]).addTo(map);
      marker.on("dragstart", () => {
        dragging.current = i;
        handlers.current.onSelect(i);
      });
      marker.on("drag", () => {
        const { lat, lng } = marker.getLngLat();
        handlers.current.onMove(i, lat, lng, false);
      });
      marker.on("dragend", () => {
        dragging.current = null;
        const { lat, lng } = marker.getLngLat();
        handlers.current.onMove(i, lat, lng, true);
      });
      return marker;
    });
    markers.current = made;
    return () => made.forEach((m) => m.remove());
  }, [map, count, draggable]);

  useEffect(() => {
    sensors.forEach((s, i) => {
      const marker = markers.current[i];
      if (!marker) return;
      if (dragging.current !== i) marker.setLngLat([s.position.lon, s.position.lat]);
      const el = marker.getElement();
      el.classList.toggle("selected", i === selected);
      el.classList.toggle("passive", s.type === "passive_bearing");
      el.title = `${s.id}: drag to move, click to edit`;
      const label = el.querySelector(".label");
      if (label) label.textContent = s.id;
    });
  }, [sensors, selected]);

  return null;
}
