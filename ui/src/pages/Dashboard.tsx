import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { Badge, Bars, Card, Empty, ErrorBox, EventFeed, Field, Loading, Modal, NamespaceSelect, Page, Stat, ago, money, num, useAction, useApp, useData } from "../components";

export function NewObjective({ onClose, namespaceId }: { onClose: () => void; namespaceId?: string }) {
  const [form, setForm] = useState({ namespace_id: namespaceId ?? "ns_root", title: "", description: "" });
  const act = useAction();
  return (
    <Modal title="Give the workforce an objective" onClose={onClose} footer={<>
      <button className="btn" onClick={onClose}>Cancel</button>
      <button className="btn primary" disabled={act.busy || form.title.length < 3}
        onClick={() => act.run(() => api.post("/objectives", form)).then((r) => r && onClose())}>Start</button></>}>
      <div className="stack">
        <ErrorBox error={act.error} />
        <p className="muted">The organization's lead plans the work, assembles the agents it needs, runs tasks in parallel and reports back.</p>
        <Field label="Organization"><NamespaceSelect value={form.namespace_id} onChange={(v) => setForm({ ...form, namespace_id: v })} /></Field>
        <Field label="Objective"><input autoFocus value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} placeholder="e.g. Launch a landing page for the spring campaign" /></Field>
        <Field label="Details, constraints, definition of done"><textarea rows={6} value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} /></Field>
      </div>
    </Modal>
  );
}

export default function Dashboard() {
  const { can, nsName } = useApp();
  const status = useData(() => api.get("/system/status"), [], ["task.", "objective.", "approval.", "agent.created", "system.", "business.", "project."]);
  const portfolio = useData(() => Promise.all([api.get("/businesses"), api.get("/projects")]), [], ["business.", "project.", "task.completed"]);
  const active = useData(() => api.get("/tasks", { status: "running,waiting,awaiting_approval,review,paused,ready", limit: 25 }), [], ["task."]);
  const usage = useData(() => api.get("/usage", { days: 14 }), [], ["task.completed"]);
  const feed = useData(() => api.get("/events", { limit: 14 }), [], ["task.", "objective.", "agent.", "approval.", "system.", "business.", "project.", "deliverable."]);
  const agents = useData(() => api.get("/agents"), [], ["agent.created"]);
  const [creating, setCreating] = useState(false);
  const stop = useAction();
  const agentName = (id: string) => agents.data?.agents.find((a: any) => a.id === id)?.name ?? "—";

  return (
    <Page title="Command center" sub="One view across the AI-OS, the workforce running on it, and everything that workforce is building."
      actions={<>
        {can("tasks:run") && <button className="btn primary" onClick={() => setCreating(true)}>＋ New objective</button>}
        {can("system:control") && !status.data?.emergency_stop.active && (
          <button className="btn danger" disabled={stop.busy} onClick={() => {
            const reason = window.prompt("Halt ALL agent execution immediately?\nOptional reason:");
            if (reason !== null) stop.run(() => api.post("/system/emergency-stop", { reason })).then(status.reload);
          }}>⏻ Emergency stop</button>)}
      </>}>
      {creating && <NewObjective onClose={() => setCreating(false)} />}
      <ErrorBox error={stop.error} />
      <Loading state={status}>{() => {
        const s = status.data, c = s.counts;
        const [biz, proj] = portfolio.data ?? [{ businesses: [] }, { projects: [] }];
        return (
          <div className="stack" style={{ gap: 16 }}>
            <div className="grid cols-6">
              <Stat label="Active agents" value={num(c.agents_active)} hint={`${num(c.agents_total)} total`} />
              <Stat label="Businesses" value={num(biz.businesses.length)} hint={`${num(proj.projects.length)} projects`} />
              <Stat label="Running tasks" value={num(c.tasks_running)} hint={`${num(c.tasks_queued)} queued · ${num(c.tasks_waiting)} waiting`} />
              <Stat label="Objectives in flight" value={num(c.objectives_active)} hint={c.objectives_attention ? `${c.objectives_attention} need attention` : "none need attention"} tone={c.objectives_attention ? "warn" : undefined} />
              <Stat label="Pending approvals" value={<Link to="/approvals">{num(c.approvals_pending)}</Link>} hint="human decisions waiting" tone={c.approvals_pending ? "warn" : undefined} />
              <Stat label="AI spend today" value={money(s.usage.cost_today)} hint={`${money(s.usage.cost_month)} this month`} />
            </div>

            <div className="grid split">
              <Card flush title="Work in progress" actions={<Link to="/work" className="small">All objectives →</Link>}>
                {active.data?.tasks.length ? (
                  <div className="table-wrap"><table>
                    <thead><tr><th>Task</th><th>Agent</th><th>Organization</th><th>Status</th><th>Started</th></tr></thead>
                    <tbody>{active.data.tasks.map((t: any) => (
                      <tr key={t.id}>
                        <td><Link to={`/work?task=${t.id}`}>{t.title}</Link></td>
                        <td>{agentName(t.assigned_agent_id)}</td><td className="muted">{nsName(t.namespace_id)}</td>
                        <td><Badge value={t.status} live={t.status === "running"} /></td><td className="muted nowrap">{ago(t.started_at)}</td>
                      </tr>))}</tbody>
                  </table></div>) : <Empty>Nothing is running. Create a business, a project or an objective to put the workforce to work.</Empty>}
              </Card>
              <Card title="System">
                <dl className="kv">
                  <dt>Deployment</dt><dd><Badge value={s.config.deployment_mode} tone="accent" /> on <span className="mono">{s.config.node_name}</span></dd>
                  <dt>Database</dt><dd>{s.config.database_backend}</dd>
                  <dt>Sandbox</dt><dd>{s.sandbox.driver}{s.sandbox.network_isolated ? " · network isolated" : ""}</dd>
                  <dt>Workers</dt><dd>{s.workers.length ? s.workers.map((w: any) => (
                    <div key={w.id}><Badge value={w.online ? "online" : "offline"} /> <Badge value={w.location} /> <span className="small muted">{w.active_jobs}/{w.concurrency} busy</span></div>
                  )) : <span className="muted">none connected</span>}</dd>
                  <dt>Queue</dt><dd className="small">{Object.entries(s.queue).map(([k, v]) => `${k}: ${v}`).join(" · ") || "empty"}</dd>
                  <dt>Today</dt><dd className="small">{num(s.usage.llm_calls_today)} model calls · {num(s.usage.exec_runs_today)} sandbox runs · {num(c.tasks_done_today)} tasks done{c.tasks_failed_today ? ` · ${c.tasks_failed_today} failed` : ""}</dd>
                </dl>
              </Card>
            </div>

            <div className="grid cols-3">
              <Card flush title="Notifications">
                {s.notifications.length ? <EventFeed events={s.notifications.slice(0, 7)} /> : <Empty>No warnings or errors.</Empty>}
              </Card>
              <Card flush title="Recent activity" actions={<Link to="/activity" className="small">Live view →</Link>}>
                <EventFeed events={(feed.data?.events ?? []).slice(0, 7)} />
              </Card>
              <Card title="Resource consumption · 14 days">
                {usage.data && usage.data.calls ? (<div className="stack">
                  <Bars values={usage.data.by_day.map((d: any) => d.input_tokens + d.output_tokens)} labels={usage.data.by_day.map((d: any) => d.key)} />
                  <div className="small muted">Tokens per day · total {num(usage.data.input_tokens + usage.data.output_tokens)} tokens · {num(usage.data.calls)} calls · {money(usage.data.cost_usd, 4)}</div>
                  <table><tbody>{usage.data.by_model.filter((m: any) => m.cost_usd > 0 || m.input_tokens > 0).slice(0, 4).map((m: any) => (
                    <tr key={m.key}><td className="mono small">{m.key}</td><td className="right">{money(m.cost_usd, 4)}</td></tr>))}</tbody></table>
                </div>) : <Empty>No model usage yet.</Empty>}
              </Card>
            </div>

            <Card flush title="Business portfolio" actions={<Link to="/businesses" className="small">Manage →</Link>}>
              {biz.businesses.length ? (
                <div className="table-wrap"><table>
                  <thead><tr><th>Business</th><th>Stage</th><th>Agents</th><th>Projects</th><th>Tasks done</th><th className="right">Net</th><th className="right">AI spend</th><th>Placement</th></tr></thead>
                  <tbody>{biz.businesses.map((b: any) => (
                    <tr key={b.id}>
                      <td><Link to={`/businesses/${b.id}`}><strong>{b.name}</strong></Link> {b.status !== "active" && <Badge value={b.status} />}</td>
                      <td><Badge value={b.stage} /></td><td>{b.agents}</td><td>{b.projects}</td><td>{b.tasks.done ?? 0}</td>
                      <td className="right">{money(b.finance.net)}</td><td className="right">{money(b.ai_cost_usd, 4)}</td>
                      <td><Badge value={b.compute} /> <span className="small muted">compute</span> <Badge value={b.models} /> <span className="small muted">models</span></td>
                    </tr>))}</tbody>
                </table></div>) : <Empty>No businesses yet. <Link to="/businesses">Describe an idea</Link> and AI-OS will staff it.</Empty>}
            </Card>
          </div>);
      }}</Loading>
    </Page>
  );
}
