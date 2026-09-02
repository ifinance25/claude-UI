import { describe, it, expect } from "vitest";
import { contextZone, CTX_AMBER_PCT, CTX_RED_PCT } from "@/lib/contextGauge";

describe("contextZone", () => {
  it("зелёный ниже 70%", () => {
    expect(contextZone(0)).toBe("green");
    expect(contextZone(69)).toBe("green");
  });
  it("жёлтый 70–84%", () => {
    expect(contextZone(70)).toBe("amber");
    expect(contextZone(84)).toBe("amber");
  });
  it("красный от 85%", () => {
    expect(contextZone(85)).toBe("red");
    expect(contextZone(100)).toBe("red");
  });
  it("пороги-константы", () => {
    expect(CTX_AMBER_PCT).toBe(70);
    expect(CTX_RED_PCT).toBe(85);
  });
});
