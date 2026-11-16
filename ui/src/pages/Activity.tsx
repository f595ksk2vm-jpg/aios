import { useEffect, useRef, useState } from "react";
import { LiveEvent, api } from "../api";
import { Card, EventFeed, NamespaceSelect, Page, useApp } from "../components";

const FILTERS: [string, string, string[]][] = [
  ["all", "Everything", [""]],
  ["agents", "Agent activity", ["agent.thought", "agent.created", "agent.status", "tool."]],
  ["comms", "Inter-agent communication", ["agent.message", "agent.delegation", "agent.question", "agent.answer", "agent.report", "agent.escalation", "review."]],
  ["tasks", "Task execution", ["task.", "objective.", "deliverable."]],
  ["approvals", "Approval requests", ["approval."]],
  ["errors", "Errors & warnings", []],
  ["system", "System events", ["system.", "model.", "grant.", "business.", "project."]],
];

export default function Activity() {
  const { subscribe, namespaces } = useApp();
  const [events, setEvents] = useState<LiveEvent[]>([]);
  const [filter, setFilter] = useState("all");
  const [ns, setNs] = useState("");
  const [paused, setPaused] = useState(false);
  const pausedRef = useRef(paused);
  pausedRef.current = paused;

  useEffect(() => { api.get("/events", { limit: 200 }).then((d) => setEvents(d.events)).catch(() => {}); }, []);
  useEffect(() => subscribe((e) => {
    if (!pausedRef.current) setEvents((prev) => (prev.some((p) => p.id === e.id) ? prev : [e, ...prev].slice(0, 600)));
  }), [subscribe]);

  const tree = new Set<string>();
  if (ns) { const add = (id: string) => { tree.add(id); namespaces.filter((n) => n.parent_id === id).forEach((n) => add(n.id)); }; add(ns); }
  const prefixes = FILTERS.find((f) => f[0] === filter)![2];
  const shown = events.filter((e) => (!ns || tree.has(e.namespace_id))
    && (filter === "errors" ? e.level !== "info" : prefixes.some((p) => e.kind.startsWith(p))));

  return (
    <Page title="Live activity center" sub="Every agent action, message, task transition, approval request and system event, as it happens — across all workers and locations."
      actions={<>
        <div style={{ width: 220 }}><NamespaceSelect value={ns} onChange={setNs} allowAll /></div>
        <button className="btn" onClick={() => setPaused(!paused)}>{paused ? "▶ Resume" : "⏸ Pause"}</button>
      </>}>
      <div className="row" style={{ marginBottom: 12 }}>
        {FILTERS.map(([key, label]) => (
          <button key={key} className={"btn sm" + (filter === key ? " primary" : "")} onClick={() => setFilter(key)}>{label}</button>))}
        <span className="spacer" /><span className="muted small">{shown.length} events{paused ? " · paused" : ""}</span>
      </div>
      <Card flush><EventFeed events={shown.slice(0, 300)} empty="Nothing matches this filter yet." /></Card>
    </Page>
  );
}
