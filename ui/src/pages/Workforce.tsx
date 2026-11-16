import { useState } from "react";
import { api } from "../api";
import { Avatar, Badge, Card, Empty, ErrorBox, Field, Loading, Modal, NamespaceSelect, Page, Tabs, ago, money, num, useAction, useApp, useData } from "../components";

const AUTONOMY: [string, string][] = [
  ["assisted", "Assisted — proposes every action for approval"],
  ["supervised", "Supervised — routine work alone, approval for external or important actions"],
  ["autonomous", "Autonomous — runs authorised workflows independently"],
  ["strategic", "Strategic — may also launch initiatives within its budget"],
];

function modelLabel(m: any) { return m?.mode === "pinned" ? m.model : `auto · ${m?.tier ?? "balanced"} · ${m?.prefer ?? "balanced"}`; }

function AgentModal({ agentId, onClose }: { agentId: string; onClose: () => void }) {
  const { can, nsName } = useApp();
  const state = useData(() => Promise.all([api.get(`/agents/${agentId}`), api.get("/tools"), api.get("/models")]), [agentId], ["task."]);
  const [tab, setTab] = useState("profile");
  const [form, setForm] = useState<any | null>(null);
  const act = useAction();
  if (!state.data) return <Modal title="Agent" onClose={onClose}><Loading state={state}>{() => null}</Loading></Modal>;
  const [{ agent: a, manager, reports, performance: p, recent_tasks, messages }, toolsData, modelsData] = state.data;
  const f = form ?? { ...a, permissions_text: a.permissions.join("\n") };
  const set = (patch: any) => setForm({ ...f, ...patch });
  const editable = can("workforce:manage");
  const providers: Record<string, string> = Object.fromEntries(modelsData.providers.map((x: any) => [x.id, x.name]));
  const save = () => act.run(async () => {
    await api.patch(`/agents/${a.id}`, {
      name: f.name, title: f.title, department: f.department, system_prompt: f.system_prompt, model: f.model, tools: f.tools,
      permissions: f.permissions_text.split("\n").map((s: string) => s.trim()).filter(Boolean), autonomy: f.autonomy, exec_env: f.exec_env,
    });
    setForm(null); state.reload();
  });
  const setStatus = (s: string) => act.run(() => api.post(`/agents/${a.id}/status/${s}`)).then(state.reload);

  return (
    <Modal wide onClose={onClose} title={<span className="row"><Avatar name={a.name} /> {a.name} <span className="muted">· {a.title}</span> <Badge value={a.status} /></span>}
      footer={editable && <>
        <button className="btn" disabled={act.busy} onClick={() => act.run(() => api.post(`/agents/${a.id}/duplicate`)).then((r) => r && onClose())}>Duplicate</button>
        {a.status === "active" ? <button className="btn" disabled={act.busy} onClick={() => setStatus("paused")}>Pause</button>
          : a.status !== "deactivated" && <button className="btn" disabled={act.busy} onClick={() => setStatus("active")}>Activate</button>}
        {a.status !== "deactivated" ? <button className="btn danger" disabled={act.busy} onClick={() => window.confirm(`Deactivate ${a.name}?`) && setStatus("deactivated")}>Deactivate</button>
          : <button className="btn" disabled={act.busy} onClick={() => setStatus("active")}>Reactivate</button>}
        {tab === "config" && <button className="btn primary" disabled={act.busy || !form} onClick={save}>Save configuration</button>}
      </>}>
      <Tabs tabs={[["profile", "Profile & performance"], ["config", "Configuration"], ["permissions", "Tools & permissions"], ["history", "Task history"], ["comms", "Messages"]]} value={tab} onChange={setTab} />
      <ErrorBox error={act.error} />
      {tab === "profile" && (
        <div className="stack">
          <dl className="kv">
            <dt>Organization</dt><dd>{nsName(a.namespace_id)}</dd>
            <dt>Department</dt><dd>{a.department || "—"}</dd>
            <dt>Reports to</dt><dd>{manager ? `${manager.name} · ${manager.title}` : "nobody (escalates to humans)"}</dd>
            <dt>Direct reports</dt><dd>{reports.length ? reports.map((r: any) => r.name).join(", ") : "—"}</dd>
            <dt>Autonomy</dt><dd><Badge value={a.autonomy} /></dd>
            <dt>Model</dt><dd className="mono">{modelLabel(a.model)}</dd>
            <dt>Runs on</dt><dd><Badge value={a.exec_env} /></dd>
            <dt>Created by</dt><dd className="mono">{a.created_by}</dd>
          </dl>
          <div className="grid cols-4">
            <div className="card stat"><div className="label">Tasks done</div><div className="value">{num(p.tasks_done)}</div><div className="hint">{p.tasks_failed} failed · {p.tasks_active} active</div></div>
            <div className="card stat"><div className="label">Success rate</div><div className="value">{p.success_rate == null ? "—" : `${Math.round(p.success_rate * 100)}%`}</div><div className="hint">avg {p.avg_duration_s ?? "—"}s per task</div></div>
            <div className="card stat"><div className="label">Review score</div><div className="value">{p.avg_review_score ?? "—"}</div><div className="hint">{p.approvals_requested} approvals requested</div></div>
            <div className="card stat"><div className="label">Model cost</div><div className="value">{money(p.cost_usd, 4)}</div><div className="hint">{num(p.llm_calls)} calls · {num(p.input_tokens + p.output_tokens)} tokens</div></div>
          </div>
        </div>)}
      {tab === "config" && (
        <div className="form-grid">
          <Field label="Name"><input disabled={!editable} value={f.name} onChange={(e) => set({ name: e.target.value })} /></Field>
          <Field label="Title"><input disabled={!editable} value={f.title} onChange={(e) => set({ title: e.target.value })} /></Field>
          <Field label="Department"><input disabled={!editable} value={f.department} onChange={(e) => set({ department: e.target.value })} /></Field>
          <Field label="Execution environment" hint="Where this agent's tasks may run in a hybrid deployment.">
            <select disabled={!editable} value={f.exec_env} onChange={(e) => set({ exec_env: e.target.value })}><option value="any">any</option><option value="local">local</option><option value="cloud">cloud</option></select></Field>
          <Field label="Autonomy level" full><select disabled={!editable} value={f.autonomy} onChange={(e) => set({ autonomy: e.target.value })}>{AUTONOMY.map(([k, l]) => <option key={k} value={k}>{l}</option>)}</select></Field>
          <Field label="Model selection"><select disabled={!editable} value={f.model?.mode ?? "auto"} onChange={(e) => set({ model: { ...f.model, mode: e.target.value } })}>
            <option value="auto">Automatic routing</option><option value="pinned">Pinned model (falls back if unavailable)</option></select></Field>
          {f.model?.mode === "pinned" ? (
            <Field label="Model"><select disabled={!editable} value={f.model.model ?? ""} onChange={(e) => set({ model: { ...f.model, model: e.target.value } })}>
              <option value="">Choose…</option>{modelsData.models.map((m: any) => { const ref = `${providers[m.provider_id]}/${m.model_name}`; return <option key={m.id} value={ref}>{ref}</option>; })}</select></Field>
          ) : (<>
            <Field label="Capability tier"><select disabled={!editable} value={f.model?.tier ?? "balanced"} onChange={(e) => set({ model: { ...f.model, tier: e.target.value } })}><option>fast</option><option>balanced</option><option>deep</option></select></Field>
            <Field label="Optimise for"><select disabled={!editable} value={f.model?.prefer ?? "balanced"} onChange={(e) => set({ model: { ...f.model, prefer: e.target.value } })}><option>balanced</option><option>quality</option><option>cost</option><option>latency</option></select></Field>
          </>)}
          <Field label="System instructions" full><textarea disabled={!editable} rows={9} value={f.system_prompt} onChange={(e) => set({ system_prompt: e.target.value })} /></Field>
        </div>)}
      {tab === "permissions" && (
        <div className="stack">
          <div className="notice">Tools are what the agent can see; permissions are what the gateway lets it use. Only people with workforce access can change either — agents cannot modify their own or each other's, and an agent that creates another can only pass on a subset of its own.</div>
          <Field label="Tools offered to this agent">
            <div className="checklist">{toolsData.tools.map((t: any) => (
              <label key={t.name} title={`${t.description}\nrequires ${t.permission} · ${t.category}`}>
                <input type="checkbox" disabled={!editable} checked={f.tools.includes(t.name)}
                  onChange={(e) => set({ tools: e.target.checked ? [...f.tools, t.name] : f.tools.filter((x: string) => x !== t.name) })} />
                <span className="mono small">{t.name}</span></label>))}</div></Field>
          <Field label="Permission patterns (one per line)" hint="e.g. fs.* · exec.code · net.read · integration.github · collab.delegate">
            <textarea className="mono" disabled={!editable} rows={7} value={f.permissions_text} onChange={(e) => set({ permissions_text: e.target.value })} /></Field>
          {editable && <div className="row"><span className="spacer" /><button className="btn primary" disabled={act.busy || !form} onClick={save}>Save tools & permissions</button></div>}
        </div>)}
      {tab === "history" && (recent_tasks.length ? (
        <table><thead><tr><th>Task</th><th>Kind</th><th>Status</th><th>When</th></tr></thead>
          <tbody>{recent_tasks.map((t: any) => <tr key={t.id}><td>{t.title}</td><td><Badge value={t.kind} /></td><td><Badge value={t.status} /></td><td className="muted nowrap">{ago(t.created_at)}</td></tr>)}</tbody></table>
      ) : <Empty>No tasks yet.</Empty>)}
      {tab === "comms" && (messages.length ? (
        <div className="stack">{messages.map((m: any) => (
          <div key={m.id} className="msg"><div className="msg-head">{m.kind} · {m.from_agent === a.id ? "sent" : "received"} · {ago(m.created_at)}</div>
            <div className="msg-body"><strong>{m.subject}</strong><div className="prose">{m.body}</div></div></div>))}</div>
      ) : <Empty>No messages.</Empty>)}
    </Modal>
  );
}

function OrgNode({ node, onOpen }: { node: any; onOpen: (id: string) => void }) {
  return (
    <li>
      <span className="org-node" onClick={() => onOpen(node.id)}>
        <Avatar name={node.name} />
        <span><strong>{node.name}</strong><br /><span className="small muted">{node.title}</span></span>
        <Badge value={node.autonomy} />{node.status !== "active" && <Badge value={node.status} />}
      </span>
      {node.reports.length > 0 && <ul>{node.reports.map((r: any) => <OrgNode key={r.id} node={r} onOpen={onOpen} />)}</ul>}
    </li>
  );
}

function Hire({ ns, onClose }: { ns: string; onClose: () => void }) {
  const tpl = useData(() => api.get("/workforce/templates"), []);
  const [mode, setMode] = useState("agent");
  const [form, setForm] = useState<any>({ namespace_id: ns || "ns_root", template: "full_stack_developer", name: "", autonomy: "", department: "Development", preset: "lean" });
  const act = useAction();
  const submit = () => act.run(async () => {
    if (mode === "agent") await api.post("/workforce/agents", { namespace_id: form.namespace_id, template: form.template, name: form.name || null, autonomy: form.autonomy || null });
    else if (mode === "department") await api.post("/workforce/departments", { namespace_id: form.namespace_id, department: form.department, autonomy: form.autonomy || null });
    else await api.post("/workforce/org", { namespace_id: form.namespace_id, preset: form.preset, autonomy: form.autonomy || null });
    onClose();
  });
  return (
    <Modal title="Agent factory" onClose={onClose} footer={<><button className="btn" onClick={onClose}>Cancel</button><button className="btn primary" disabled={act.busy} onClick={submit}>Create</button></>}>
      <Tabs tabs={[["agent", "One agent"], ["department", "A department"], ["org", "A whole organization"]]} value={mode} onChange={setMode} />
      <Loading state={tpl}>{() => (
        <div className="stack">
          <ErrorBox error={act.error} />
          <Field label="Organization"><NamespaceSelect value={form.namespace_id} onChange={(v) => setForm({ ...form, namespace_id: v })} kinds={["system", "business"]} /></Field>
          {mode === "agent" && <>
            <Field label="Role template"><select value={form.template} onChange={(e) => setForm({ ...form, template: e.target.value })}>
              {tpl.data.departments.concat([""]).map((d: string) => {
                const items = tpl.data.templates.filter((t: any) => (t.department || "") === d || (d === "" && !tpl.data.departments.includes(t.department)));
                return items.length ? <optgroup key={d || "custom"} label={d || "Custom"}>{items.map((t: any) => <option key={t.key} value={t.key}>{t.title}</option>)}</optgroup> : null;
              })}</select></Field>
            <div className="muted small">{tpl.data.templates.find((t: any) => t.key === form.template)?.prompt}</div>
            <Field label="Name (optional)"><input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} /></Field></>}
          {mode === "department" && <Field label="Department" hint="Creates every role in the department with its reporting lines.">
            <select value={form.department} onChange={(e) => setForm({ ...form, department: e.target.value })}>{tpl.data.departments.map((d: string) => <option key={d}>{d}</option>)}</select></Field>}
          {mode === "org" && <Field label="Line-up"><select value={form.preset} onChange={(e) => setForm({ ...form, preset: e.target.value })}>
            {Object.entries(tpl.data.presets).map(([k, v]: any) => <option key={k} value={k}>{k} — {v.length} roles</option>)}</select></Field>}
          <Field label="Autonomy"><select value={form.autonomy} onChange={(e) => setForm({ ...form, autonomy: e.target.value })}>
            <option value="">Template default</option>{AUTONOMY.map(([k, l]) => <option key={k} value={k}>{l}</option>)}</select></Field>
        </div>)}</Loading>
    </Modal>
  );
}

export default function Workforce() {
  const { can, nsName } = useApp();
  const [ns, setNs] = useState("");
  const [tab, setTab] = useState("directory");
  const [open, setOpen] = useState<string | null>(null);
  const [hiring, setHiring] = useState(false);
  const [dept, setDept] = useState("");
  const agents = useData(() => api.get("/agents", { namespace_id: ns }), [ns], ["agent.created", "agent.status"]);
  const chart = useData(() => api.get("/workforce/org-chart", { namespace_id: ns || "ns_root" }), [ns], ["agent.created", "agent.status"]);
  const perf = useData(() => (tab === "performance" ? api.get("/workforce/performance", { namespace_id: ns }) : Promise.resolve(null)), [ns, tab], ["task.completed", "task.failed"]);
  const tpl = useData(() => (tab === "templates" ? api.get("/workforce/templates") : Promise.resolve(null)), [tab]);
  const msgs = useData(() => (tab === "comms" ? Promise.all([api.get("/messages", { namespace_id: ns, limit: 150 }), api.get("/agents", { include_deactivated: true })]) : Promise.resolve(null)), [ns, tab], ["agent."]);
  const rows = (agents.data?.agents ?? []).filter((a: any) => !dept || a.department === dept);
  const depts = [...new Set((agents.data?.agents ?? []).map((a: any) => a.department).filter(Boolean))] as string[];

  return (
    <Page title="AI workforce" sub="Agents, departments and reporting lines. Templates are starting points — every agent is individually configurable, and the factory creates more on demand."
      actions={<>
        <div style={{ width: 220 }}><NamespaceSelect value={ns} onChange={setNs} allowAll /></div>
        {can("workforce:manage") && <button className="btn primary" onClick={() => setHiring(true)}>＋ Create agents</button>}
      </>}>
      {open && <AgentModal agentId={open} onClose={() => { setOpen(null); agents.reload(); chart.reload(); }} />}
      {hiring && <Hire ns={ns} onClose={() => { setHiring(false); agents.reload(); chart.reload(); }} />}
      <Tabs tabs={[["directory", "Directory"], ["org", "Org chart"], ["performance", "Performance"], ["comms", "Communication"], ["templates", "Role templates"]]} value={tab} onChange={setTab} />
      {tab === "directory" && (
        <Card flush title={`${rows.length} agents`} actions={<select value={dept} onChange={(e) => setDept(e.target.value)} style={{ width: 200 }}>
          <option value="">All departments</option>{depts.map((d) => <option key={d}>{d}</option>)}</select>}>
          <Loading state={agents}>{() => rows.length ? (
            <div className="table-wrap"><table>
              <thead><tr><th>Agent</th><th>Department</th><th>Organization</th><th>Autonomy</th><th>Model</th><th>Runs on</th><th>Status</th></tr></thead>
              <tbody>{rows.map((a: any) => (
                <tr key={a.id} className="clickable" onClick={() => setOpen(a.id)}>
                  <td><div className="row" style={{ flexWrap: "nowrap" }}><Avatar name={a.name} /><div><strong>{a.name}</strong><div className="small muted">{a.title}</div></div></div></td>
                  <td>{a.department || "—"}</td><td className="muted">{nsName(a.namespace_id)}</td><td><Badge value={a.autonomy} /></td>
                  <td className="mono small">{modelLabel(a.model)}</td><td><Badge value={a.exec_env} /></td><td><Badge value={a.status} /></td>
                </tr>))}</tbody>
            </table></div>) : <Empty>No agents here yet.</Empty>}</Loading>
        </Card>)}
      {tab === "org" && (
        <Card title={`Organization chart — ${nsName(ns || "ns_root")}`} actions={chart.data && <span className="muted small">{chart.data.headcount} agents</span>}>
          {!ns && <div className="notice" style={{ marginBottom: 12 }}>Showing the global AI-OS agents. Choose a business above to see its hierarchy.</div>}
          <Loading state={chart}>{() => chart.data.roots.length ? (<>
            <div className="org"><ul>{chart.data.roots.map((r: any) => <OrgNode key={r.id} node={r} onOpen={setOpen} />)}</ul></div>
            <div className="chips" style={{ marginTop: 16 }}>{Object.entries(chart.data.departments).map(([d, ids]: any) => <span key={d} className="badge accent">{d}: {ids.length}</span>)}</div>
          </>) : <Empty>No agents in this organization.</Empty>}</Loading>
        </Card>)}
      {tab === "performance" && (
        <Card flush><Loading state={perf}>{() => perf.data?.agents.length ? (
          <div className="table-wrap"><table>
            <thead><tr><th>Agent</th><th className="right">Done</th><th className="right">Failed</th><th className="right">Success</th><th className="right">Avg time</th><th className="right">Review</th><th className="right">Calls</th><th className="right">Tokens</th><th className="right">Cost</th></tr></thead>
            <tbody>{[...perf.data.agents].sort((a: any, b: any) => b.tasks_done - a.tasks_done).map((p: any) => (
              <tr key={p.agent_id} className="clickable" onClick={() => setOpen(p.agent_id)}>
                <td><strong>{p.name}</strong> <span className="muted small">{p.title}</span></td>
                <td className="right">{p.tasks_done}</td><td className="right">{p.tasks_failed}</td>
                <td className="right">{p.success_rate == null ? "—" : `${Math.round(p.success_rate * 100)}%`}</td>
                <td className="right">{p.avg_duration_s == null ? "—" : `${p.avg_duration_s}s`}</td><td className="right">{p.avg_review_score ?? "—"}</td>
                <td className="right">{num(p.llm_calls)}</td><td className="right">{num(p.input_tokens + p.output_tokens)}</td><td className="right">{money(p.cost_usd, 4)}</td>
              </tr>))}</tbody>
          </table></div>) : <Empty>No agents.</Empty>}</Loading></Card>)}
      {tab === "comms" && (
        <Card flush title="Inter-agent communication"><Loading state={msgs}>{() => {
          const [{ messages }, { agents: all }] = msgs.data;
          const name = (id: string | null) => (id ? all.find((a: any) => a.id === id)?.name ?? "?" : "humans / system");
          return messages.length ? (
            <div className="table-wrap"><table>
              <thead><tr><th>When</th><th>Type</th><th>From → To</th><th>Message</th></tr></thead>
              <tbody>{messages.map((m: any) => (
                <tr key={m.id}><td className="muted nowrap">{ago(m.created_at)}</td><td><Badge value={m.kind} tone={m.kind === "escalation" ? "warn" : undefined} /></td>
                  <td className="nowrap">{name(m.from_agent)} → {name(m.to_agent)}</td>
                  <td><strong>{m.subject}</strong><div className="prose small muted">{m.body.slice(0, 400)}{m.body.length > 400 ? "…" : ""}</div></td></tr>))}</tbody>
            </table></div>) : <Empty>Agents have not exchanged messages yet.</Empty>;
        }}</Loading></Card>)}
      {tab === "templates" && (
        <Loading state={tpl}>{() => tpl.data && (
          <div className="stack" style={{ gap: 16 }}>{tpl.data.departments.concat(["Custom"]).map((d: string) => {
            const items = tpl.data.templates.filter((t: any) => (d === "Custom" ? !tpl.data.departments.includes(t.department) : t.department === d));
            return items.length ? (
              <Card key={d} flush title={d}><div className="table-wrap"><table>
                <thead><tr><th style={{ width: 220 }}>Role</th><th>Reports to</th><th>Tier</th><th>Capability kits</th><th>Instructions</th></tr></thead>
                <tbody>{items.map((t: any) => (
                  <tr key={t.key}><td><strong>{t.title}</strong><div className="mono small muted">{t.key}</div></td><td className="mono small">{t.reports_to ?? "—"}</td>
                    <td><Badge value={t.tier} /></td><td><div className="chips">{t.kits.map((kit: string) => <span key={kit} className="badge">{kit}</span>)}</div></td>
                    <td className="small muted">{t.prompt}</td></tr>))}</tbody>
              </table></div></Card>) : null;
          })}</div>)}</Loading>)}
    </Page>
  );
}
