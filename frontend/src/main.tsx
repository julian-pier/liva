import { StrictMode } from "react"
import { createRoot } from "react-dom/client"

import App from "./App"
import DashboardApp from "./DashboardApp"
import "./index.css"

const mountNode = document.getElementById("liva-dashboard-root")
  ?? document.getElementById("liva-ui-root")
  ?? document.getElementById("root")

if (!mountNode) {
  throw new Error("LIVA UI mount point is missing")
}

const surface = mountNode.dataset.livaSurface || "ui-lab"

createRoot(mountNode).render(
  <StrictMode>
    {surface === "dashboard" ? <DashboardApp /> : <App />}
  </StrictMode>,
)
