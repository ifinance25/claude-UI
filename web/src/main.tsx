import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "@/App";
import "@/index.css";
// Side-effect: applies the persisted theme class to <html> before render.
import "@/lib/theme";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>
);
