import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import { Badge, Bars, Card, Empty, ErrorBox, Field, Files, Loading, Modal, Page, Progress, Spark, Stat, Tabs, ago, money, num, useAction, useApp, useData } from "../components";
import { NewObjective } from "./Dashboard";
import { NewProject } from "./Projects";

function NewBusiness({ onClose }: { onClose: (created?: any) => void }) {
  const [f, setF] = useState<any>({ name: "", idea: "", org_preset: "lean", autonomy: "supervised", compute: "any", models: "any", monthly_budget_usd: "", auto_provision: true, kickoff: true });
  const act = useAction();
  const submit = () => act.run(() => api.post("/businesses", { ...f, monthly_budget_usd: f.monthly_budget_usd === "" ? null : Number(f.monthly_budget_usd) })).then((b) => b && onClose(b));
  return (
    <Modal wide title="Create a business from an idea" onClose={() => onClose()} footer={<>
      <button className="btn" onClick={() => onClose()}>Cancel</button>
      <button className="btn primary" disabled={act.busy || f.name.length < 2 || f.idea.length < 5} onClick={submit}>{act.busy ? "Assembling…" : "Create business"}</button></>}>
      <div className="form-grid">
        <div className="full"><ErrorBox error={act.error} /></div>
        <Field label="Business name"><input autoFocus value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} /></Field>
        <Field label="Starting team"><select value={f.org_preset} onChange={(e) => setF({ ...f, org_preset: e.target.value })}>
          <option value="minimal">Minimal — a CEO who hires as needed</option><option value="lean">Lean — 7 core roles</option><option value="full">Full — all 31 roles</option></select></Field>
        <Field label="Idea or objective" full hint="Describe what the business should do, for whom, and anything you already know."><textarea rows={5} value={f.idea} onChange={(e) => setF({ ...f, idea: e.target.value })} /></Field>
        <Field label="Autonomy of its agents"><select value={f.autonomy} onChange={(e) => setF({ ...f, autonomy: e.target.value })}>
          <option value="assisted">Assisted</option><option value="supervised">Supervised</option><option value="autonomous">Autonomous</option><option value="strategic">Strategic</option></select></Field>
        <Field label="Monthly AI budget (USD)" hint="Hard limit across the business and its projects. Empty = no limit."><input type="number" min={0} value={f.monthly_budget_usd} onChange={(e) => setF({ ...f, monthly_budget_usd: e.target.value })} /></Field>
        <Field label="Where workloads run" hint="Which workers may execute this business's tasks."><select value={f.compute} onChange={(e) => setF({ ...f, compute: e.target.value })}>
          <option value="any">Anywhere</option><option value="local">Local infrastructure only</option><option value="cloud">Cloud only</option></select></Field>
        <Field label="Which models may see its data" hint="'Local only' keeps every prompt on your own hardware."><select value={f.models} onChange={(e) => setF({ ...f, models: e.target.value })}>
          <option value="any">Any enabled model</option><option value="local">Locally hosted models only</option><option value="cloud">Cloud models only</option></select></Field>
        <label className="row full"><input type="checkbox" checked={f.auto_provision} onChange={(e) => setF({ ...f, auto_provision: e.target.checked })} /> Let the agent factory add agents when a plan needs a role nobody has</label>
        <label className="row full"><input type="checkbox" checked={f.kickoff} onChange={(e) => setF({ ...f, kickoff: e.target.checked })} /> Start immediately: market research, business model, strategy and org design</label>
      </div>
    </Modal>
  );
}

export function Businesses() {
  const { can } = useApp();
  const state = useData(() => api.get("/businesses"), [], ["business.", "task.completed", "project.", "agent.created"]);
  const [creating, setCreating] = useState(false);
  return (
    <Page title="Business portfolio" sub="Each business is an isolated organization with its own agents, projects, customers, documents, finances and KPIs. Nothing is shared between businesses unless you grant it."
      actions={can("business:manage") && <button className="btn primary" onClick={() => setCreating(true)}>＋ New business</button>}>
      {creating && <NewBusiness onClose={() => { setCreating(false); state.reload(); }} />}
      <Loading state={state}>{() => state.data.businesses.length ? (
        <div className="grid cols-3">{state.data.businesses.map((b: any) => {
          const total = Object.values(b.tasks).reduce((a: number, n: any) => a + n, 0) as number;
          return (
            <Link key={b.id} to={`/businesses/${b.id}`} className="card" style={{ color: "inherit", textDecoration: "none" }}>
              <div className="card-head"><h2>{b.name}</h2><div className="actions"><Badge value={b.stage} />{b.status !== "active" && <Badge value={b.status} />}</div></div>
              <div className="card-body stack">
                <div className="muted" style={{ minHeight: 42 }}>{(b.mission || b.idea).slice(0, 150)}</div>
                <div className="grid cols-3" style={{ gap: 8 }}>
                  <div><div className="small muted">Agents</div><strong>{b.agents}</strong></div>
                  <div><div className="small muted">Projects</div><strong>{b.projects}</strong></div>
                  <div><div className="small muted">Net</div><strong>{money(b.finance.net, 0)}</strong></div>
                </div>
                <div className="row small muted" style={{ flexWrap: "nowrap" }}><Progress value={total ? ((b.tasks.done ?? 0) / total) * 100 : 0} /><span className="nowrap">{b.tasks.done ?? 0}/{total} tasks</span></div>
                <div className="row small muted"><Badge value={b.compute} /> compute <Badge value={b.models} /> models <span className="spacer" />AI {money(b.ai_cost_usd, 4)}</div>
              </div>
            </Link>);
        })}</div>) : <Card><Empty>No businesses yet. Describe an idea and AI-OS creates the organization, staffs it and starts the founding work.</Empty></Card>}</Loading>
    </Page>
  );
}

function Sharing({ business }: { business: any }) {
  const { can, namespaces, nsName, reloadNamespaces } = useApp();
  const state = useData(() => api.get("/namespaces"), [business.id], ["grant."]);
  const [f, setF] = useState({ to_ns: "", resource_kind: "knowledge" });
  const act = useAction();
  const others = namespaces.filter((n) => n.kind === "business" && n.id !== business.namespace_id);
  const grants = (state.data?.grants ?? []).filter((g: any) => g.from_ns === business.namespace_id || g.to_ns === business.namespace_id);
  return (
    <Card title="Cross-business sharing">
      <div className="stack">
        <p className="muted">By default no other business can see this one's knowledge, use its agents or run its workflows. A grant shares one kind of resource with one other organization, is audited, and can be revoked at any time.</p>
        <ErrorBox error={act.error} />
        {grants.length ? <table><thead><tr><th>Resource</th><th>From</th><th>To</th><th>Since</th><th></th></tr></thead><tbody>{grants.map((g: any) => (
          <tr key={g.id}><td><Badge value={g.resource_kind} tone="accent" />{g.resource_id && <span className="mono small"> {g.resource_id}</span>}</td><td>{nsName(g.from_ns)}</td><td>{nsName(g.to_ns)}</td><td className="muted">{ago(g.created_at)}</td>
            <td className="right">{can("system:control") && <button className="btn sm" onClick={() => act.run(() => api.del(`/grants/${g.id}`)).then(state.reload)}>Revoke</button>}</td></tr>))}</tbody></table>
          : <Empty>Nothing is shared.</Empty>}
        {can("system:control") && others.length > 0 && (
          <div className="row">
            <span>Share this business's</span>
            <select style={{ width: 150 }} value={f.resource_kind} onChange={(e) => setF({ ...f, resource_kind: e.target.value })}><option value="knowledge">knowledge</option><option value="agent">agents</option><option value="workflow">workflows</option></select>
            <span>with</span>
            <select style={{ width: 220 }} value={f.to_ns} onChange={(e) => setF({ ...f, to_ns: e.target.value })}><option value="">Choose a business…</option>{others.map((n) => <option key={n.id} value={n.id}>{n.name}</option>)}</select>
            <button className="btn primary" disabled={!f.to_ns || act.busy} onClick={() => act.run(() => api.post("/grants", { from_ns: business.namespace_id, ...f })).then(() => { state.reload(); reloadNamespaces(); })}>Grant</button>
          </div>)}
      </div>
    </Card>
  );
}

export function BusinessDetail() {
  const { id } = useParams();
  const { can } = useApp();
  const state = useData(() => api.get(`/businesses/${id}`), [id], ["task.", "objective.", "project.", "deliverable.", "business.", "agent.created"]);
  const [tab, setTab] = useState("overview");
  const [modal, setModal] = useState<string | null>(null);
  const ledger = useData(() => (tab === "finance" ? api.get(`/businesses/${id}/ledger`) : Promise.resolve(null)), [id, tab]);
  const customers = useData(() => (tab === "customers" ? api.get(`/businesses/${id}/customers`) : Promise.resolve(null)), [id, tab]);
  const objectives = useData(() => api.get("/objectives", { namespace_id: state.data?.business.namespace_id ?? "none" }), [state.data?.business.namespace_id], ["objective."]);
  const act = useAction();
  const [entry, setEntry] = useState<any>({ kind: "revenue", amount: "", category: "general", memo: "" });
  const [cust, setCust] = useState<any>({ name: "", email: "", company: "", stage: "lead" });
  const [kpi, setKpi] = useState<any>({ key: "", name: "", unit: "", target: "" });
  const [wf, setWf] = useState<any>({ name: "", objective_title: "", objective_description: "", interval_minutes: "" });

  return (
    <Loading state={state}>{() => {
      const o = state.data, b = o.business, manage = can("business:manage");
      const setStatus = (s: string) => act.run(() => api.post(`/businesses/${b.id}/status/${s}`)).then(state.reload);
      const totalTasks = Object.values(o.tasks).reduce((a: number, n: any) => a + n, 0) as number;
      return (
        <Page title={<span>{b.name} <Badge value={b.stage} /> {b.status !== "active" && <Badge value={b.status} />}</span>}
          sub={<><Link to="/businesses">← Portfolio</Link> · {b.mission || b.idea}</>}
          actions={manage && <>
            <button className="btn" onClick={() => setModal("objective")}>＋ Objective</button>
            <button className="btn" onClick={() => setModal("project")}>＋ Project</button>
            {b.status === "active" ? <button className="btn" disabled={act.busy} onClick={() => setStatus("paused")}>⏸ Pause business</button>
              : <button className="btn primary" disabled={act.busy} onClick={() => setStatus("active")}>▶ Resume business</button>}</>}>
          {modal === "objective" && <NewObjective namespaceId={b.namespace_id} onClose={() => { setModal(null); objectives.reload(); }} />}
          {modal === "project" && <NewProject businessId={b.id} onClose={() => { setModal(null); state.reload(); }} />}
          <ErrorBox error={act.error} />
          <div className="grid cols-6" style={{ marginBottom: 16 }}>
            <Stat label="Agents" value={num(o.agents)} />
            <Stat label="Projects" value={num(o.projects)} />
            <Stat label="Tasks done" value={`${o.tasks.done ?? 0}/${totalTasks}`} hint={o.tasks.failed ? `${o.tasks.failed} failed` : undefined} />
            <Stat label="Revenue" value={money(o.finance.revenue, 0)} hint={`expenses ${money(o.finance.expense, 0)}`} />
            <Stat label="Net" value={money(o.finance.net, 0)} tone={o.finance.net < 0 ? "danger" : undefined} />
            <Stat label="AI spend" value={money(o.ai_cost_usd, 4)} hint={o.budgets[0] ? `limit ${money(o.budgets[0].limit_usd, 0)}/${o.budgets[0].period}` : "no limit set"} />
          </div>
          <Tabs value={tab} onChange={setTab} tabs={[["overview", "Overview"], ["projects", "Projects"], ["finance", "Finance"], ["customers", "Customers"], ["deliverables", "Deliverables"], ["files", "Documents & files"], ["workflows", "Workflows"], ["sharing", "Isolation & sharing"]]} />

          {tab === "overview" && (
            <div className="grid split">
              <div className="stack" style={{ gap: 16 }}>
                <Card title="Profile">
                  <dl className="kv">
                    <dt>Idea</dt><dd className="prose">{b.idea}</dd>
                    <dt>Mission</dt><dd className="prose">{b.mission || <span className="muted">not yet defined by the workforce</span>}</dd>
                    <dt>Business model</dt><dd className="prose">{b.business_model || <span className="muted">—</span>}</dd>
                    <dt>Strategy</dt><dd className="prose">{b.strategy || <span className="muted">—</span>}</dd>
                    <dt>Placement</dt><dd><Badge value={o.namespace.compute} /> compute · <Badge value={o.namespace.models} /> models</dd>
                    <dt>Agent factory</dt><dd>{o.namespace.meta.auto_provision ? "adds agents on demand" : "fixed team"} · default autonomy <Badge value={o.namespace.meta.default_autonomy} /></dd>
                  </dl>
                </Card>
                <Card flush title="Objectives">
                  {objectives.data?.objectives.length ? <table><tbody>{objectives.data.objectives.map((ob: any) => (
                    <tr key={ob.id}><td><Link to="/work">{ob.title}</Link></td><td className="right"><Badge value={ob.status} live={["planning", "running"].includes(ob.status)} /></td></tr>))}</tbody></table>
                    : <Empty>No objectives yet.</Empty>}
                </Card>
              </div>
              <Card title="KPIs">
                <div className="stack">
                  {o.kpis.length ? o.kpis.map((k: any) => (
                    <div key={k.id}>
                      <div className="row"><strong>{k.name}</strong><span className="spacer" /><span style={{ fontSize: 18, fontWeight: 650 }}>{k.latest ?? "—"} <span className="small muted">{k.unit}</span></span></div>
                      <Spark values={k.series.map((p: any) => p.value)} />
                      <div className="small muted">{k.target != null ? `target ${k.target} ${k.unit}` : "no target"} · {k.series.length} readings</div>
                    </div>)) : <Empty>No KPIs defined yet — the leadership team defines them during the founding objective.</Empty>}
                  {manage && <details><summary className="clickable muted">Define a KPI or record a value</summary>
                    <div className="form-grid" style={{ marginTop: 8 }}>
                      <Field label="Key"><input value={kpi.key} onChange={(e) => setKpi({ ...kpi, key: e.target.value })} placeholder="mrr" /></Field>
                      <Field label="Name"><input value={kpi.name} onChange={(e) => setKpi({ ...kpi, name: e.target.value })} /></Field>
                      <Field label="Unit"><input value={kpi.unit} onChange={(e) => setKpi({ ...kpi, unit: e.target.value })} /></Field>
                      <Field label="Target / value"><input type="number" value={kpi.target} onChange={(e) => setKpi({ ...kpi, target: e.target.value })} /></Field>
                      <button className="btn" disabled={!kpi.key || !kpi.name} onClick={() => act.run(() => api.put(`/businesses/${b.id}/kpis`, { key: kpi.key, name: kpi.name, unit: kpi.unit, target: kpi.target === "" ? null : Number(kpi.target) })).then(state.reload)}>Define</button>
                      <button className="btn" disabled={!kpi.key || kpi.target === ""} onClick={() => act.run(() => api.post(`/businesses/${b.id}/kpis/${kpi.key}`, { value: Number(kpi.target) })).then(state.reload)}>Record value</button>
                    </div></details>}
                </div>
              </Card>
            </div>)}

          {tab === "projects" && (
            <Card flush>{o.projects_list.length ? (
              <table><thead><tr><th>Project</th><th>Kind</th><th>Progress</th><th>Status</th></tr></thead><tbody>{o.projects_list.map((p: any) => (
                <tr key={p.id}><td><Link to={`/projects/${p.id}`}><strong>{p.name}</strong></Link><div className="small muted">{p.description.slice(0, 120)}</div></td><td><Badge value={p.kind} /></td>
                  <td style={{ width: 220 }}><div className="row" style={{ flexWrap: "nowrap" }}><Progress value={p.progress.percent} /><span className="small muted nowrap">{p.progress.done}/{p.progress.total}</span></div></td>
                  <td><Badge value={p.status} /></td></tr>))}</tbody></table>) : <Empty>No projects yet.</Empty>}</Card>)}

          {tab === "finance" && (
            <div className="grid split">
              <Card flush title="Ledger">
                <Loading state={ledger}>{() => ledger.data?.entries.length ? (
                  <table><thead><tr><th>Date</th><th>Type</th><th>Category</th><th>Memo</th><th>Recorded by</th><th className="right">Amount</th></tr></thead><tbody>{ledger.data.entries.map((e: any) => (
                    <tr key={e.id}><td className="nowrap">{e.ts.slice(0, 10)}</td><td><Badge value={e.kind} tone={e.kind === "revenue" ? "ok" : "warn"} /></td><td>{e.category}</td><td className="muted">{e.memo}</td>
                      <td className="mono small">{e.recorded_by}</td><td className="right nowrap">{e.kind === "expense" ? "−" : ""}{money(e.amount)}</td></tr>))}</tbody></table>) : <Empty>No ledger entries. Agents record revenue freely; recording an expense always needs your approval.</Empty>}</Loading>
              </Card>
              <div className="stack" style={{ gap: 16 }}>
                <Card title="By month">{o.finance.by_month.length ? (<>
                  <Bars values={o.finance.by_month.map((m: any) => m.revenue - m.expense)} labels={o.finance.by_month.map((m: any) => m.month)} />
                  <div className="small muted">Net per month · {o.finance.by_month[0].month} → {o.finance.by_month[o.finance.by_month.length - 1].month}</div></>) : <Empty>No data.</Empty>}</Card>
                {manage && <Card title="Add an entry"><div className="form-grid">
                  <Field label="Type"><select value={entry.kind} onChange={(e) => setEntry({ ...entry, kind: e.target.value })}><option>revenue</option><option>expense</option></select></Field>
                  <Field label="Amount"><input type="number" min={0} value={entry.amount} onChange={(e) => setEntry({ ...entry, amount: e.target.value })} /></Field>
                  <Field label="Category"><input value={entry.category} onChange={(e) => setEntry({ ...entry, category: e.target.value })} /></Field>
                  <Field label="Memo"><input value={entry.memo} onChange={(e) => setEntry({ ...entry, memo: e.target.value })} /></Field>
                  <button className="btn primary full" disabled={!entry.amount || act.busy} onClick={() => act.run(() => api.post(`/businesses/${b.id}/ledger`, { ...entry, amount: Number(entry.amount) })).then(() => { ledger.reload(); state.reload(); setEntry({ ...entry, amount: "", memo: "" }); })}>Record</button>
                </div></Card>}
              </div>
            </div>)}

          {tab === "customers" && (
            <Card flush title="Customers & leads" actions={<span className="small muted">{Object.entries(o.customers).map(([k, v]) => `${v} ${k}`).join(" · ")}</span>}>
              <Loading state={customers}>{() => (<>
                {customers.data?.customers.length ? <table><thead><tr><th>Name</th><th>Email</th><th>Company</th><th>Stage</th><th>Notes</th><th>Updated</th><th></th></tr></thead><tbody>{customers.data.customers.map((c: any) => (
                  <tr key={c.id}><td><strong>{c.name}</strong></td><td>{c.email}</td><td>{c.company}</td><td><Badge value={c.stage} /></td><td className="muted small">{c.notes}</td><td className="muted nowrap">{ago(c.updated_at)}</td>
                    <td>{manage && <button className="btn sm ghost" onClick={() => window.confirm(`Delete ${c.name}?`) && act.run(() => api.del(`/businesses/${b.id}/customers/${c.id}`)).then(customers.reload)}>Delete</button>}</td></tr>))}</tbody></table>
                  : <Empty>No customers yet.</Empty>}
                {manage && <div className="row" style={{ padding: 12, borderTop: "1px solid var(--border)" }}>
                  <input style={{ flex: 1, minWidth: 120 }} placeholder="Name" value={cust.name} onChange={(e) => setCust({ ...cust, name: e.target.value })} />
                  <input style={{ flex: 1, minWidth: 120 }} placeholder="Email" value={cust.email} onChange={(e) => setCust({ ...cust, email: e.target.value })} />
                  <input style={{ flex: 1, minWidth: 120 }} placeholder="Company" value={cust.company} onChange={(e) => setCust({ ...cust, company: e.target.value })} />
                  <select style={{ width: 130 }} value={cust.stage} onChange={(e) => setCust({ ...cust, stage: e.target.value })}><option>lead</option><option>qualified</option><option>customer</option><option>churned</option></select>
                  <button className="btn primary" disabled={!cust.name} onClick={() => act.run(() => api.put(`/businesses/${b.id}/customers`, cust)).then(() => { customers.reload(); state.reload(); setCust({ name: "", email: "", company: "", stage: "lead" }); })}>Add</button>
                </div>}</>)}</Loading>
            </Card>)}

          {tab === "deliverables" && (
            <Card flush>{o.deliverables_list.length ? (
              <table><thead><tr><th>File</th><th>Type</th><th>Version</th><th>Commit</th><th className="right">Size</th><th>Updated</th></tr></thead><tbody>{o.deliverables_list.map((d: any) => (
                <tr key={d.id}><td className="mono">{d.path}</td><td><Badge value={d.kind} /></td><td>v{d.version}</td><td className="mono small">{d.commit.slice(0, 8)}</td><td className="right">{num(d.size_bytes)} B</td><td className="muted nowrap">{ago(d.updated_at)}</td></tr>))}</tbody></table>
            ) : <Empty>Nothing delivered yet. Every file an agent produces is committed to the workspace repository and listed here.</Empty>}</Card>)}

          {tab === "files" && <Files namespaceId={b.namespace_id} />}

          {tab === "workflows" && (
            <div className="stack" style={{ gap: 16 }}>
              <Card flush title="Workflows" actions={<span className="small muted">Reusable objectives, run on demand or on a schedule</span>}>
                {o.workflows.length ? <table><thead><tr><th>Workflow</th><th>Objective</th><th>Schedule</th><th>Runs</th><th>Last run</th><th></th></tr></thead><tbody>{o.workflows.map((w: any) => (
                  <tr key={w.id}><td><strong>{w.name}</strong>{w.namespace_id !== b.namespace_id && <Badge value="shared" tone="accent" />}</td><td>{w.objective_title}</td>
                    <td>{w.interval_minutes ? `every ${w.interval_minutes} min` : "manual"} {!w.enabled && <Badge value="disabled" />}</td><td>{w.run_count}</td><td className="muted">{ago(w.last_run_at)}</td>
                    <td className="right nowrap">{can("tasks:run") && <button className="btn sm" onClick={() => act.run(() => api.post(`/workflows/${w.id}/run`, { namespace_id: b.namespace_id })).then(() => { state.reload(); objectives.reload(); })}>Run now</button>}
                      {manage && w.namespace_id === b.namespace_id && <button className="btn sm ghost" onClick={() => act.run(() => api.del(`/workflows/${w.id}`)).then(state.reload)}>Delete</button>}</td></tr>))}</tbody></table>
                  : <Empty>No workflows yet.</Empty>}
              </Card>
              {manage && <Card title="New workflow"><div className="form-grid">
                <Field label="Name"><input value={wf.name} onChange={(e) => setWf({ ...wf, name: e.target.value })} placeholder="Weekly competitor scan" /></Field>
                <Field label="Repeat every (minutes)" hint="Leave empty to run manually."><input type="number" min={5} value={wf.interval_minutes} onChange={(e) => setWf({ ...wf, interval_minutes: e.target.value })} /></Field>
                <Field label="Objective title" full><input value={wf.objective_title} onChange={(e) => setWf({ ...wf, objective_title: e.target.value })} /></Field>
                <Field label="Objective details" full><textarea value={wf.objective_description} onChange={(e) => setWf({ ...wf, objective_description: e.target.value })} /></Field>
                <button className="btn primary" disabled={!wf.name || wf.objective_title.length < 3} onClick={() => act.run(() => api.post("/workflows", { ...wf, namespace_id: b.namespace_id, interval_minutes: wf.interval_minutes === "" ? null : Number(wf.interval_minutes) })).then(() => { state.reload(); setWf({ name: "", objective_title: "", objective_description: "", interval_minutes: "" }); })}>Create workflow</button>
              </div></Card>}
            </div>)}

          {tab === "sharing" && <Sharing business={b} />}
        </Page>);
    }}</Loading>
  );
}
