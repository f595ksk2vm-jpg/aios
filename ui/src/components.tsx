import { ReactNode, createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, LiveEvent, api, getToken, setToken, setUnauthorizedHandler, streamEvents } from "./api";

// ----------------------------------------------------------------------------- app context

type Ctx = {
  user: any | null;
  permissions: string[];
  can: (perm: string) => boolean;
  login: (token: string) => Promise<void>;
  logout: () => void;
  connected: boolean;
  subscribe: (fn: (e: LiveEvent) => void) => () => void;
  namespaces: any[];
  nsName: (id?: string | null) => string;
  reloadNamespaces: () => void;
};
const AppCtx = createContext<Ctx>(null as any);
export const useApp = () => useContext(AppCtx);

export function AppProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<any | null>(null);
  const [permissions, setPermissions] = useState<string[]>([]);
  const [ready, setReady] = useState(false);
  const [connected, setConnected] = useState(false);
  const [namespaces, setNamespaces] = useState<any[]>([]);
  const listeners = useRef(new Set<(e: LiveEvent) => void>());

  const logout = useCallback(() => {
    if (getToken()) api.post("/auth/logout").catch(() => {});
    setToken(null);
    setUser(null);
  }, []);

  const loadMe = useCallback(async () => {
    if (!getToken()) { setReady(true); return; }
    try {
      const me = await api.get("/auth/me");
      setUser(me.user);
      setPermissions(me.permissions);
    } catch { setToken(null); setUser(null); }
    setReady(true);
  }, []);

  const reloadNamespaces = useCallback(() => {
    api.get("/namespaces").then((d) => setNamespaces(d.namespaces)).catch(() => {});
  }, []);

  useEffect(() => { setUnauthorizedHandler(() => { setToken(null); setUser(null); }); loadMe(); }, [loadMe]);

  useEffect(() => {
    if (!user) return;
    reloadNamespaces();
    const stop = streamEvents((e) => {
      if (e.kind === "business.created" || e.kind === "project.created") reloadNamespaces();
      listeners.current.forEach((fn) => fn(e));
    }, setConnected);
    return stop;
  }, [user, reloadNamespaces]);

  const value = useMemo<Ctx>(() => ({
    user, permissions, connected, namespaces, reloadNamespaces,
    can: (perm) => permissions.includes(perm),
    login: async (token) => { setToken(token); await loadMe(); },
    logout,
    subscribe: (fn) => { listeners.current.add(fn); return () => { listeners.current.delete(fn); }; },
    nsName: (id) => (id === "ns_root" ? "AI-OS (global)" : namespaces.find((n) => n.id === id)?.name ?? id ?? "—"),
  }), [user, permissions, connected, namespaces, loadMe, logout, reloadNamespaces]);

  if (!ready) return <div className="login muted">Loading…</div>;
  return <AppCtx.Provider value={value}>{children}</AppCtx.Provider>;
}

/** Load data, and reload it (debounced) whenever a live event with one of the given kind prefixes arrives. */
export function useData(loader: () => Promise<any>, deps: any[], liveKinds: string[] = []) {
  const { subscribe } = useApp();
  const [data, setData] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const loaderRef = useRef(loader);
  loaderRef.current = loader;
  const seq = useRef(0);

  const reload = useCallback(async () => {
    const mine = ++seq.current;
    try {
      const d = await loaderRef.current();
      if (mine === seq.current) { setData(d); setError(null); }
    } catch (e: any) {
      if (mine === seq.current) setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      if (mine === seq.current) setLoading(false);
    }
  }, []);

  useEffect(() => { setLoading(true); reload(); /* eslint-disable-next-line */ }, deps);

  const kindsKey = liveKinds.join(",");
  useEffect(() => {
    if (!kindsKey) return;
    const kinds = kindsKey.split(",");
    let timer: any = null;
    const unsub = subscribe((e) => {
      if (!kinds.some((k) => e.kind.startsWith(k))) return;
      if (!timer) timer = setTimeout(() => { timer = null; reload(); }, 400);
    });
    return () => { unsub(); if (timer) clearTimeout(timer); };
  }, [kindsKey, subscribe, reload]);

  return { data, error, loading, reload };
}

/** Run an action with busy/error state — every button that mutates goes through this. */
export function useAction() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const run = useCallback(async (fn: () => Promise<any>) => {
    setBusy(true); setError(null);
    try { return await fn(); } catch (e: any) { setError(e?.message ?? String(e)); return undefined; } finally { setBusy(false); }
  }, []);
  return { busy, error, run, setError };
}

// ----------------------------------------------------------------------------- primitives

export function Page({ title, sub, actions, children }: { title: ReactNode; sub?: ReactNode; actions?: ReactNode; children: ReactNode }) {
  return (
    <div className="page">
      <div className="page-head">
        <div><h1>{title}</h1>{sub && <div className="sub">{sub}</div>}</div>
        {actions && <div className="actions">{actions}</div>}
      </div>
      {children}
    </div>
  );
}

export function Card({ title, actions, children, flush }: { title?: ReactNode; actions?: ReactNode; children: ReactNode; flush?: boolean }) {
  return (
    <section className="card">
      {(title || actions) && <div className="card-head"><h2>{title}</h2>{actions && <div className="actions">{actions}</div>}</div>}
      <div className={"card-body" + (flush ? " flush" : "")}>{children}</div>
    </section>
  );
}

export function Stat({ label, value, hint, tone }: { label: string; value: ReactNode; hint?: ReactNode; tone?: string }) {
  return (
    <div className="card stat">
      <div className="label">{label}</div>
      <div className="value" style={tone ? { color: `var(--${tone})` } : undefined}>{value}</div>
      {hint && <div className="hint">{hint}</div>}
    </div>
  );
}

const TONES: Record<string, string> = {
  active: "ok", done: "ok", completed: "ok", approved: "ok", online: "ok", ok: "ok", customer: "ok", operating: "ok", approve: "ok",
  running: "info", planning: "info", finalizing: "info", ready: "info", qualified: "info", building: "info", info: "info",
  pending: "", queued: "", lead: "", idea: "",
  waiting: "purple", review: "purple", validation: "purple",
  awaiting_approval: "warn", paused: "warn", needs_attention: "warn", warn: "warn", suspended: "warn", blocked: "warn", request_changes: "warn",
  failed: "danger", rejected: "danger", denied: "danger", error: "danger", deactivated: "danger", cancelled: "danger", dead: "danger",
  archived: "danger", churned: "danger", offline: "danger",
  assisted: "", supervised: "accent", autonomous: "purple", strategic: "warn", local: "ok", cloud: "info", any: "",
};
export function Badge({ value, tone, live }: { value?: string | null; tone?: string; live?: boolean }) {
  if (!value) return null;
  const t = tone ?? TONES[value] ?? "";
  return <span className={`badge ${t}`}>{live && <span className="dot pulse" />}{String(value).replace(/_/g, " ")}</span>;
}

export function Empty({ children }: { children: ReactNode }) { return <div className="empty">{children}</div>; }
export function ErrorBox({ error }: { error?: string | null }) { return error ? <div className="error" role="alert">{error}</div> : null; }

export function Loading({ state, children }: { state: { data: any; error: string | null; loading: boolean }; children: () => ReactNode }) {
  if (state.error && !state.data) return <ErrorBox error={state.error} />;
  if (!state.data) return <Empty>Loading…</Empty>;
  return <>{children()}</>;
}

export function Tabs({ tabs, value, onChange }: { tabs: [string, string][]; value: string; onChange: (v: string) => void }) {
  return (
    <div className="tabs" role="tablist">
      {tabs.map(([key, label]) => (
        <button key={key} role="tab" aria-selected={value === key} className={"tab" + (value === key ? " active" : "")} onClick={() => onChange(key)}>{label}</button>
      ))}
    </div>
  );
}

export function Modal({ title, onClose, children, footer, wide }: { title: ReactNode; onClose: () => void; children: ReactNode; footer?: ReactNode; wide?: boolean }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="modal-backdrop" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className={"modal" + (wide ? " wide" : "")} role="dialog" aria-modal="true">
        <div className="modal-head"><h2>{title}</h2><span className="spacer" /><button className="btn ghost sm" onClick={onClose} aria-label="Close">✕</button></div>
        <div className="modal-body">{children}</div>
        <div className="modal-foot">{footer}</div>
      </div>
    </div>
  );
}

export function Field({ label, children, full, hint }: { label: string; children: ReactNode; full?: boolean; hint?: string }) {
  return <label className={"field" + (full ? " full" : "")}>{label}{children}{hint && <span className="small" style={{ fontWeight: 400 }}>{hint}</span>}</label>;
}

export function NamespaceSelect({ value, onChange, allowAll, kinds }: { value: string; onChange: (v: string) => void; allowAll?: boolean; kinds?: string[] }) {
  const { namespaces } = useApp();
  const byParent = (id: string | null) => namespaces.filter((n) => n.parent_id === id && n.status !== "archived");
  const options: [string, string][] = [];
  const walk = (parent: string | null, depth: number) => byParent(parent).forEach((n) => {
    if (!kinds || kinds.includes(n.kind)) options.push([n.id, `${"  ".repeat(depth)}${n.kind === "system" ? "AI-OS (global)" : n.name}${n.kind === "project" ? " · project" : ""}`]);
    walk(n.id, depth + 1);
  });
  walk(null, 0);
  return (
    <select value={value} onChange={(e) => onChange(e.target.value)}>
      {allowAll && <option value="">All organizations</option>}
      {options.map(([id, label]) => <option key={id} value={id}>{label}</option>)}
    </select>
  );
}

export function ago(iso?: string | null): string {
  if (!iso) return "—";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return `${Math.floor(s)}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}
export const clock = (iso: string) => new Date(iso).toLocaleTimeString([], { hour12: false });
export const money = (n?: number | null, digits = 2) => n == null ? "—" : `$${n.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
export const num = (n?: number | null) => (n == null ? "—" : n.toLocaleString());

export function Progress({ value }: { value: number }) {
  return <div className="progress" role="progressbar" aria-valuenow={value} aria-valuemin={0} aria-valuemax={100}><div style={{ width: `${Math.min(100, Math.max(0, value))}%` }} /></div>;
}

export function Bars({ values, labels }: { values: number[]; labels?: string[] }) {
  const max = Math.max(...values, 1e-9);
  return <div className="bars">{values.map((v, i) => <div key={i} title={labels ? `${labels[i]}: ${v}` : String(v)} style={{ height: `${(v / max) * 100}%` }} />)}</div>;
}

export function Spark({ values, tone = "accent" }: { values: number[]; tone?: string }) {
  if (values.length < 2) return null;
  const min = Math.min(...values), max = Math.max(...values), span = max - min || 1;
  const pts = values.map((v, i) => `${(i / (values.length - 1)) * 100},${28 - ((v - min) / span) * 24 - 2}`).join(" ");
  return <svg viewBox="0 0 100 28" preserveAspectRatio="none" style={{ width: "100%", height: 28 }}><polyline points={pts} fill="none" stroke={`var(--${tone})`} strokeWidth="1.8" vectorEffect="non-scaling-stroke" /></svg>;
}

export function Avatar({ name }: { name: string }) { return <span className="avatar">{(name || "?").slice(0, 2).toUpperCase()}</span>; }

const ICONS: [string, string][] = [
  ["task.failed", "✕"], ["task.completed", "✓"], ["task", "▸"], ["tool.denied", "⛔"], ["tool", "⚙"], ["approval", "✋"], ["agent.created", "＋"],
  ["agent.thought", "💬"], ["agent.escalation", "⚠"], ["agent", "✉"], ["objective.completed", "★"], ["objective", "◎"], ["system", "⏻"],
  ["model", "⇄"], ["deliverable", "📄"], ["review", "🔍"], ["business", "🏢"], ["project", "📁"], ["grant", "🔗"],
];
export function eventIcon(kind: string) { return (ICONS.find(([p]) => kind.startsWith(p)) ?? ["", "•"])[1]; }

export function EventFeed({ events, empty = "No activity yet." }: { events: LiveEvent[]; empty?: string }) {
  const { nsName } = useApp();
  if (!events.length) return <Empty>{empty}</Empty>;
  return (
    <div className="feed">
      {events.map((e) => (
        <div key={e.id} className={`feed-item ${e.level}`}>
          <span className="time">{clock(e.ts)}</span>
          <span className="feed-icon" aria-hidden>{eventIcon(e.kind)}</span>
          <div style={{ minWidth: 0 }}>
            <div className="prose">{e.message || e.kind}</div>
            <div className="kind">{e.kind} · {nsName(e.namespace_id)}</div>
          </div>
        </div>
      ))}
    </div>
  );
}

/** Workspace file browser + viewer for a business or project namespace. */
export function Files({ namespaceId }: { namespaceId: string }) {
  const [path, setPath] = useState(".");
  const [open, setOpen] = useState<string | null>(null);
  const list = useData(() => api.get(`/workspaces/${namespaceId}/files`, { path }), [namespaceId, path], ["deliverable"]);
  const file = useData(() => (open ? api.get(`/workspaces/${namespaceId}/file`, { path: open }) : Promise.resolve(null)), [namespaceId, open]);
  const crumbs = path === "." ? [] : path.split("/");
  return (
    <div className="grid split-left">
      <Card flush title={<span className="mono">/{crumbs.join("/")}</span>} actions={path !== "." && <button className="btn sm" onClick={() => setPath(crumbs.slice(0, -1).join("/") || ".")}>↑ Up</button>}>
        <Loading state={list}>{() => list.data.entries.length ? (
          <table><tbody>{list.data.entries.map((f: any) => (
            <tr key={f.path} className={"clickable" + (open === f.path ? " selected" : "")} onClick={() => (f.dir ? setPath(f.path) : setOpen(f.path))}>
              <td>{f.dir ? "📁" : "📄"} {f.name}</td><td className="right muted small">{f.dir ? "" : `${f.size} B`}</td>
            </tr>))}</tbody></table>) : <Empty>Empty directory</Empty>}</Loading>
      </Card>
      <Card title={open ? <span className="mono">{open}</span> : "Select a file"}>
        {!open ? <Empty>Files the workforce produces appear here.</Empty> : !file.data ? <Empty>Loading…</Empty>
          : file.data.binary ? <Empty>Binary file ({file.data.size} bytes)</Empty>
          : <div className="codebox" style={{ maxHeight: 560 }}><pre>{file.data.content}</pre></div>}
      </Card>
    </div>
  );
}
