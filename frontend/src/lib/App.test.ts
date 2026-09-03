import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/svelte";
import App from "./App.svelte";

describe("App shell", () => {
  it("renders the three nav items and a theme toggle", () => {
    render(App);

    expect(screen.getByText("Coach")).toBeTruthy();
    expect(screen.getByText("Sessions")).toBeTruthy();
    expect(screen.getByText("Pit Wall")).toBeTruthy();
    expect(screen.getByText(/^Theme ·/)).toBeTruthy();
  });
});
