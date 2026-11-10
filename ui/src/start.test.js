import { describe, expect, it } from "vitest";
import { coverageLine } from "./components/GetStarted.jsx";

describe("coverage", () => {
  it("is quiet until there is a host", () => {
    expect(coverageLine({ hosts: 0 })).toBe(null);
  });
  it("says what is covered in one line", () => {
    expect(coverageLine({ services: 15, watched: 12, hosts: 4, reporting: 4, stacks: 10, deployed: 9 }))
      .toBe("12 of 15 published services watched · 4 of 4 hosts reporting · 9 of 10 stacks deployed from git");
    expect(coverageLine({ services: 1, watched: 0, hosts: 1, reporting: 1, stacks: 0, deployed: 0 }))
      .toBe("0 of 1 published service watched · 1 of 1 host reporting");
  });
});
