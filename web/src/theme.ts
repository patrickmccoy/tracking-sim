// Map colours, from the dataviz reference palette (light steps: the base
// map is light). Identity follows the thing, never its rank: tracker A is
// always slot 2 (orange) and tracker B slot 3 (aqua), whatever they are.

export const colors = {
  truth: "#52514e",
  truthLabel: "#0b0b0b",
  detection: "#2a78d6", // categorical slot 1, blue
  clutter: "#a3a29c",
  trackA: "#eb6834", // slot 2, orange
  trackB: "#1baf7a", // slot 3, aqua
  sensor: "#0b0b0b",
  sensorSelected: "#6250d6", // slot 7, violet
  halo: "#fcfcfb",
};

// Coverage heights, low to high: an ordinal blue ramp, darkest for the
// lowest (smallest) area, drawn on top.
export const coverageSteps = [
  { height_m: 300, label: "1,000 ft", color: "#1c5cab" },
  { height_m: 1500, label: "5,000 ft", color: "#3987e5" },
  { height_m: 3000, label: "10,000 ft", color: "#86b6ef" },
];
