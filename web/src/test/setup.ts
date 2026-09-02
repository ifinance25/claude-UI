// Vitest setup: подключает jest-dom матчеры (toBeInTheDocument, toHaveClass и т.д.)
// и чистит DOM между тестами.
import "@testing-library/jest-dom/vitest";
import { afterEach } from "vitest";
import { cleanup } from "@testing-library/react";

afterEach(() => {
  cleanup();
});
