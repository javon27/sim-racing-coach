import { describe, it, expect, afterEach } from "vitest";
import { render, screen, cleanup } from "@testing-library/svelte";
import InfoTip from "./InfoTip.svelte";

describe("InfoTip", () => {
  afterEach(cleanup);

  it("renders a marker with the tip text as its accessible name", () => {
    render(InfoTip, { text: "Metric or imperial units." });

    const marker = screen.getByRole("button", { name: "Metric or imperial units." });
    expect(marker).toBeTruthy();
    expect(marker.getAttribute("title")).toBe("Metric or imperial units.");
  });

  it("shows the tip text in its bubble", () => {
    render(InfoTip, { text: "Light, dark, or follow your system setting." });

    expect(screen.getByText("Light, dark, or follow your system setting.")).toBeTruthy();
  });
});
