import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import { Badge, Card, Empty, ErrorBox, Field, Files, Loading, Modal, Page, Progress, Stat, Tabs, ago, num, useAction, useApp, useData } from "../components";
import { NewObjective } from "./Dashboard";
import { TaskModal } from "./Work";

const KIND_LABELS: Record<string, string> = {
  website: "Website", saas: "SaaS application", mobile_app: "Mobile application", api: "API", platform: "Software platform", ai_app: "AI application",
  automation: "Automation system", internal_tool: "Internal business tool", digital_product: "Digital product", research: "Research project", software: "Other software",
};

export function NewProject({ onClose, businessId }: { onClose: () => void; businessId?: string }) {
  const biz = useData(() => api.get("/businesses"), []);
  const [f, setF] = useState<any>({ name: "", kind: "saas", description: "", requirements: "", business_id: businessId ?? "", kickoff: true });
  const act = useAction();
  return (
    <Modal wide title="New project" onClose={onClose} footer={<>
      <button className="btn" onClick={onClose}>Cancel</button>
      <button className="btn primary" disabled={act.busy || f.name.length < 2} onClick={() => act.run(() => api.post("/projects", { ...f, business_id: f.business_id || null })).then((r) => r && onClose())}>Create project</button></>}>
      <div className="form-grid">
        <div className="full"><ErrorBox error={act.error} /></div>
        <Field label="Project name"><input autoFocus value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} /></Field>
        <Field label="Type"><select value={f.kind} onChange={(e) => setF({ ...f, kind: e.target.value })}>{Object.entries(KIND_LABELS).map(([k, l]) => <option key={k} value={k}>{l}</option>)}</select></Field>
        <Field label="Business" full hint="A project inside a business uses that business's agents, knowledge, budget and placement rules."><select value={f.business_id} onChange={(e) => setF({ ...f, business_id: e.target.value })}>
          <option value="">Standalone (global AI-OS agents)</option>{(biz.data?.businesses ?? []).map((b: any) => <option key={b.id} value={b.id}>{b.name}</option>)}</select></Field>
        <Field label="What should be built" full><textarea rows={3} value={f.description} onChange={(e) => setF({ ...f, description: e.target.value })} /></Field>
        <Field label="Requirements" full hint="Saved to docs/REQUIREMENTS.md in the project repository."><textarea rows={6} value={f.requirements} onChange={(e) => setF({ ...f, requirements: e.target.value })} /></Field>
        <label className="row full"><input type="checkbox" checked={f.kickoff} onChange={(e) => setF({ ...f, kickoff: e.target.checked })} /> Start delivery immediately</label>
      </div>
    </Modal>
  );
}

export function Projects() {
  const { can } = useApp();
  const state = useData(() => api.get("/projects"), [], ["project.", "task."]);
  const [creating, setCreating] = useState(false);
  return (
    <Page title="Project portfolio" sub="Every project has its own workspace and git repository, requirements, tasks with dependencies, assigned agents, deliverables and version history."
      actions={can("business:manage") && <button className="btn primary" onClick={() => setCreating(true)}>＋ New project</button>}>
      {creating && <NewProject onClose={() => { setCreating(false); state.reload(); }} />}
      <Card flush><Loading state={state}>{() => state.data.projects.length ? (
        <div className="table-wrap"><table>
          <thead><tr><th>Project</th><th>Type</th><th>Business</th><th>Progress</th><th>Agents</th><th>Status</th><th>Created</th></tr></thead>
          <tbody>{state.data.projects.map((p: any) => (
            <tr key={p.id}>
              <td><Link to={`/projects/${p.id}`}><strong>{p.name}</strong></Link><div className="small muted">{p.description.slice(0, 110)}</div></td>
              <td><Badge value={KIND_LABELS[p.kind] ?? p.kind} /></td><td>{p.business_name ?? <span className="muted">standalone</span>}</td>
              <td style={{ width: 220 }}><div className="row" style={{ flexWrap: "nowrap" }}><Progress value={p.progress.percent} /><span className="small muted nowrap">{p.progress.done}/{p.progress.total}</span></div></td>
              <td>{p.progress.assigned_agents.length}</td><td><Badge value={p.status} /></td><td className="muted nowrap">{ago(p.created_at)}</td>
            </tr>))}</tbody>
        </table></div>) : <Empty>No projects yet.</Empty>}</Loading></Card>
    </Page>
  );
}

export function ProjectDetail() {
  const { id } = useParams();
  const { can } = useApp();
  const state = useData(() => api.get(`/projects/${id}`), [id], ["task.", "deliverable.", "objective.", "review."]);
  const agents = useData(() => api.get("/agents", { include_deactivated: true }), []);
  const [tab, setTab] = useState("tasks");
  const [task, setTask] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const act = useAction();
  const agentName = (aid: string) => { const a = agents.data?.agents.find((x: any) => x.id === aid); return a ? `${a.name} · ${a.title}` : "—"; };
  return (
    <Loading state={state}>{() => {
      const { project: p, progress, tasks, deliverables, history } = state.data;
      const titleOf = (tid: string) => tasks.find((t: any) => t.id === tid)?.title ?? tid;
      const setStatus = (s: string) => act.run(() => api.patch(`/projects/${p.id}`, { status: s })).then(state.reload);
      return (
        <Page title={<span>{p.name} <Badge value={KIND_LABELS[p.kind] ?? p.kind} /> <Badge value={p.status} /></span>}
          sub={<><Link to="/projects">← Projects</Link>{p.business_id && <> · <Link to={`/businesses/${p.business_id}`}>Business</Link></>} · {p.description}</>}
          actions={can("business:manage") && <>
            <button className="btn" onClick={() => setAdding(true)}>＋ Objective</button>
            {p.status === "active" ? <button className="btn" onClick={() => setStatus("paused")}>⏸ Pause</button> : <button className="btn primary" onClick={() => setStatus("active")}>▶ Resume</button>}
            {p.status !== "completed" && <button className="btn" onClick={() => setStatus("completed")}>Mark complete</button>}</>}>
          {task && <TaskModal taskId={task} onClose={() => setTask(null)} agentName={agentName} />}
          {adding && <NewObjective namespaceId={p.namespace_id} onClose={() => { setAdding(false); state.reload(); }} />}
          <ErrorBox error={act.error} />
          <div className="grid cols-4" style={{ marginBottom: 16 }}>
            <div className="card stat"><div className="label">Progress</div><div className="value">{progress.percent}%</div><Progress value={progress.percent} /></div>
            <Stat label="Tasks" value={`${progress.done}/${progress.total}`} hint={Object.entries(progress.tasks).filter(([k]) => k !== "done").map(([k, v]) => `${v} ${k.replace(/_/g, " ")}`).join(" · ") || "all done"} />
            <Stat label="Assigned agents" value={num(progress.assigned_agents.length)} />
            <Stat label="Deliverables" value={num(deliverables.length)} hint={`${history.length} commits`} />
          </div>
          <Tabs value={tab} onChange={setTab} tabs={[["tasks", "Tasks & dependencies"], ["deliverables", "Deliverables"], ["files", "Workspace"], ["history", "Version history"], ["requirements", "Requirements"]]} />
          {tab === "tasks" && (<div className="stack" style={{ gap: 16 }}>
            {progress.objectives.map((o: any) => o.result && (
              <Card key={o.id} title={<span>Outcome: {o.title} <Badge value={o.status} /></span>}><div className="prose">{o.result}</div></Card>))}
            <Card flush>{tasks.length ? (
              <div className="table-wrap"><table><thead><tr><th>Task</th><th>Agent</th><th>Depends on</th><th>Status</th><th>Finished</th></tr></thead><tbody>{tasks.map((t: any) => (
                <tr key={t.id} className="clickable" onClick={() => setTask(t.id)}>
                  <td>{t.parent_task_id && <span className="muted">↳ </span>}{t.title} {t.kind !== "work" && <Badge value={t.kind} />}</td><td>{agentName(t.assigned_agent_id)}</td>
                  <td className="small muted">{(t.depends_on ?? []).map(titleOf).join(", ") || "—"}</td><td><Badge value={t.status} live={t.status === "running"} /></td><td className="muted nowrap">{ago(t.finished_at)}</td>
                </tr>))}</tbody></table></div>) : <Empty>No tasks yet. Add an objective to start work.</Empty>}</Card></div>)}
          {tab === "deliverables" && (
            <Card flush>{deliverables.length ? (
              <table><thead><tr><th>File</th><th>Type</th><th>Version</th><th>Produced by</th><th>Commit</th><th className="right">Size</th><th>Updated</th></tr></thead><tbody>{deliverables.map((d: any) => (
                <tr key={d.id}><td className="mono">{d.path}</td><td><Badge value={d.kind} /></td><td>v{d.version}</td><td>{agentName(d.agent_id)}</td><td className="mono small">{d.commit.slice(0, 8)}</td>
                  <td className="right">{num(d.size_bytes)} B</td><td className="muted nowrap">{ago(d.updated_at)}</td></tr>))}</tbody></table>) : <Empty>Nothing delivered yet.</Empty>}</Card>)}
          {tab === "files" && <Files namespaceId={p.namespace_id} />}
          {tab === "history" && (
            <Card flush>{history.length ? (
              <table><thead><tr><th>Commit</th><th>Author</th><th>Message</th><th>Date</th></tr></thead><tbody>{history.map((c: any) => (
                <tr key={c.commit}><td className="mono small">{c.commit.slice(0, 8)}</td><td>{c.author}</td><td>{c.message}</td><td className="muted nowrap">{ago(c.date)}</td></tr>))}</tbody></table>) : <Empty>No commits.</Empty>}</Card>)}
          {tab === "requirements" && <Card title="Requirements"><div className="prose">{p.requirements || "No requirements recorded."}</div></Card>}
        </Page>);
    }}</Loading>
  );
}
