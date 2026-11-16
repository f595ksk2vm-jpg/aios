import React, { useEffect, useState } from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter, NavLink, Navigate, Route, Routes } from "react-router-dom";
import { api } from "./api";
import { AppProvider, useAction, useApp, useData } from "./components";
import Activity from "./pages/Activity";
import Approvals from "./pages/Approvals";
import { BusinessDetail, Businesses } from "./pages/Businesses";
import Dashboard from "./pages/Dashboard";
import Login from "./pages/Login";
import { ProjectDetail, Projects } from "./pages/Projects";
import System from "./pages/System";
import Work from "./pages/Work";
import Workforce from "./pages/Workforce";
import "./styles.css";

function applyTheme(theme: string) {
  if (theme === "auto") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", theme);
}

function Shell() {
  const { user, logout, connected, can } = useApp();
  const status = useData(() => api.get("/system/status"), [], ["system.", "approval."]);
  const resume = useAction();
  const [theme, setTheme] = useState(() => { try { return localStorage.getItem("aios.theme") ?? "auto"; } catch { return "auto"; } });
  useEffect(() => { applyTheme(theme); try { localStorage.setItem("aios.theme", theme); } catch { /* ignore */ } }, [theme]);
  const stop = status.data?.emergency_stop;
  const pending = status.data?.counts.approvals_pending ?? 0;
  const link = (to: string, label: string, count?: number) => (
    <NavLink to={to} end={to === "/"} className={({ isActive }) => "nav-link" + (isActive ? " active" : "")}>
      {label}{count ? <span className="count">{count}</span> : null}
    </NavLink>
  );
  return (
    <div className="shell">
      <nav className="sidebar" aria-label="Main">
        <div className="brand"><span className="brand-mark">◉</span>AI-OS</div>
        {link("/", "Dashboard")}
        {link("/activity", "Live activity")}
        {link("/approvals", "Approvals", pending)}
        <div className="nav-group">Layer 3 · Ecosystem</div>
        {link("/businesses", "Businesses")}
        {link("/projects", "Projects")}
        {link("/work", "Objectives & tasks")}
        <div className="nav-group">Layer 2 · Workforce</div>
        {link("/workforce", "Agents & org")}
        <div className="nav-group">Layer 1 · AI-OS</div>
        {link("/system", "Infrastructure")}
        <div className="sidebar-foot">
          <div><span className="dot" style={{ color: connected ? "#4ccf8a" : "#e5484d" }} /> {connected ? "Live" : "Reconnecting…"}</div>
          <div className="truncate" title={user.email}>{user.name} · {user.role}</div>
          <div className="row">
            <button onClick={() => setTheme(theme === "dark" ? "light" : theme === "light" ? "auto" : "dark")}>Theme: {theme}</button>
            <button onClick={logout}>Sign out</button>
          </div>
        </div>
      </nav>
      <main className="main">
        {stop?.active && (
          <div className="stop-banner" role="alert">
            ⏻ EMERGENCY STOP ACTIVE — all agent execution is halted{stop.reason ? ` (${stop.reason})` : ""}.
            {can("system:control") && <button className="btn" disabled={resume.busy} onClick={() => resume.run(() => api.post("/system/resume")).then(status.reload)}>Resume operations</button>}
          </div>
        )}
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/activity" element={<Activity />} />
          <Route path="/approvals" element={<Approvals />} />
          <Route path="/businesses" element={<Businesses />} />
          <Route path="/businesses/:id" element={<BusinessDetail />} />
          <Route path="/projects" element={<Projects />} />
          <Route path="/projects/:id" element={<ProjectDetail />} />
          <Route path="/work" element={<Work />} />
          <Route path="/workforce" element={<Workforce />} />
          <Route path="/system" element={<System />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </div>
  );
}

function Root() {
  const { user } = useApp();
  return user ? <Shell /> : <Login />;
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <BrowserRouter>
      <AppProvider><Root /></AppProvider>
    </BrowserRouter>
  </React.StrictMode>,
);
