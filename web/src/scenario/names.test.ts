import { describe, expect, it } from "vitest";
import type { Capture } from "../api";
import { nameFor } from "./names";

const capture = { id: "roof-2026-11-12-cb66462f2a" } as Capture;

describe("nameFor", () => {
  it("names a scenario after the capture it runs on", () => {
    expect(nameFor("kbjc", "kbjc", capture)).toBe("kbjc-on-roof-2026-11-12");
    expect(nameFor("kbjc-on-other", "kbjc", capture)).toBe("kbjc-on-roof-2026-11-12");
  });
  it("goes back to the file's name with its own traffic", () => {
    expect(nameFor("kbjc-on-roof-2026-11-12", "kbjc", null)).toBe("kbjc");
  });
  it("keeps a name the user chose", () => {
    expect(nameFor("my test", "kbjc", capture)).toBe("my test");
    expect(nameFor("my test", "kbjc", undefined)).toBe("my test");
  });
});
