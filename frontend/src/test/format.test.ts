import { describe, expect, it } from "vitest";

import { num, pct, signed } from "../format";

describe("pct", () => {
  it("renders a 0..1 ratio as a whole percentage", () => {
    expect(pct(0.867)).toBe("87%");
    expect(pct(1)).toBe("100%");
    expect(pct(0)).toBe("0%");
  });
});

describe("signed", () => {
  it("prefixes positives with + and leaves negatives alone", () => {
    expect(signed(120)).toBe("+120");
    expect(signed(-5)).toBe("-5");
    expect(signed(0)).toBe("0");
  });
});

describe("num", () => {
  it("groups thousands", () => {
    expect(num(76645)).toBe("76,645");
    expect(num(120)).toBe("120");
  });
});
