import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api } from "../api";
import { Badge, Card, Empty, ErrorBox, Field, Loading, Modal, NamespaceSelect, Page, Tabs, ago, money, num, useAction, useApp, useData } from "../components";
import { NewObjective } from "./Dashboard";

function Transcript({ runId }: { runId: string }) {
  const run = useData(() => api.get(`/runs/${runId}`), [runId], ["tool.call", "agent.thought", "task."]);
  if (!run.data) return <Empty>Loading transcript…</Empty>;
  const r = run.data;
  return (
    <div className="stack">
      <div className="row small muted">
        <Badge value={r.status} /> <span className="mono">{r.model || "no model call yet"}</span>
        <span>{r.steps} steps · {num(r.input_tokens)} in / {num(r.output_tokens)} out · {money(r.cost_usd, 4)}</span>
        {r.pending?.reason && <Badge value={`waiting: ${r.pending.reason}`} tone="warn" />}
      </div>
      {r.error && <ErrorBox error={r.error} />}
      {r.messages.map((m: any, i: number) => {
        if (m.role === "user") return <div key={i} className="msg"><div className="msg-head">{i === 0 ? "Task brief" : "System → agent"}</div><div className="msg-body prose">{m.content}</div></div>;
        if (m.role === "assistant") return (
          <div key={i} className="msg assistant"><div className="msg-head">Agent</div><div className="msg-body stack">
            {m.content && <div className="prose">{m.content}</div>}
            {(m.tool_calls ?? []).map((c: any) => (
              <div key={c.id}><span className="badge purple">⚙ {c.name}</span><div className="codebox" style={{ marginTop: 6, maxHeight: 220 }}><pre>{JSON.stringify(c.arguments, null, 2)}</pre></div></div>))}
          </div></div>);
        return (m.results ?? []).map((res: any) => (
          <div key={i + res.tool_call_id} className={"msg tool" + (res.is_error ? " err" : "")}>
            <div className="msg-head">{res.name} → {res.is_error ? "error" : "result"}</div>
            <div className="msg-body"><div className="codebox" style={{ maxHeight: 220, border: "none", padding: 0, background: "none" }}><pre>{res.content}</pre></div></div>
          </div>));
      })}
    </div>
  );
}

export function TaskModal({ taskId, onClose, agentName }: { taskId: string; onClose: () => void; agentName: (id: string) => string }) {
  const { can } = useApp();
  const state = useData(() => api.get(`/tasks/${taskId}`), [taskId], ["task.", "review.", "approval."]);
  const act = useAction();
  const [runIdx, setRunIdx] = useState(-1);
  if (!state.data) return <Modal title="Task" onClose={onClose}><Loading state={state}>{() => null}</Loading></Modal>;
  const { task: t, runs, children } = state.data;
  const run = runs[runIdx < 0 ? runs.length - 1 : runIdx];
  return (
    <Modal wide title={<span>{t.title} <Badge value={t.status} live={t.status === "running"} /></span>} onClose={onClose}
      footer={can("tasks:run") && <>
        {["failed", "blocked", "cancelled"].includes(t.status) && <button className="btn primary" disabled={act.busy} onClick={() => act.run(() => api.post(`/tasks/${t.id}/retry`)).then(state.reload)}>Retry</button>}
        {!["done", "failed", "cancelled"].includes(t.status) && <button className="btn danger" disabled={act.busy} onClick={() => act.run(() => api.post(`/tasks/${t.id}/cancel`)).then(state.reload)}>Cancel task</button>}
      </>}>
      <div className="stack">
        <ErrorBox error={act.error} />
        <dl className="kv">
          <dt>Assigned to</dt><dd>{agentName(t.assigned_agent_id)}</dd>
          <dt>Kind</dt><dd><Badge value={t.kind} /> · attempt {t.attempts}/{t.max_attempts} · priority {t.priority}</dd>
          {t.meta?.review?.verdict && <><dt>Review</dt><dd><Badge value={t.meta.review.verdict} /> {t.meta.review.score != null && `score ${t.meta.review.score}`} — {t.meta.review.comments}</dd></>}
          {t.meta?.review_overridden && <><dt>Review note</dt><dd><Badge value={t.meta.review_overridden} tone="warn" /></dd></>}
        </dl>
        {t.error && <ErrorBox error={t.error} />}
        {t.result && <div><h3>Result</h3><div className="codebox prose" style={{ marginTop: 6 }}>{t.result}</div></div>}
        {children.length > 0 && <div><h3>Sub-tasks</h3><table><tbody>{children.map((c: any) => (
          <tr key={c.id}><td>{c.title}</td><td>{agentName(c.assigned_agent_id)}</td><td><Badge value={c.kind} /></td><td><Badge value={c.status} /></td></tr>))}</tbody></table></div>}
        <div>
          <div className="row"><h3>Execution transcript</h3><span className="spacer" />
            {runs.length > 1 && runs.map((r: any, i: number) => <button key={r.id} className={"btn sm" + (r.id === run?.id ? " primary" : "")} onClick={() => setRunIdx(i)}>Run {i + 1}</button>)}</div>
          <div style={{ marginTop: 8 }}>{run ? <Transcript runId={run.id} /> : <Empty>This task has not started yet.</Empty>}</div>
        </div>
      </div>
    </Modal>
  );
}

function NewTask({ onClose }: { onClose: () => void }) {
  const [form, setForm] = useState({ namespace_id: "ns_root", title: "", description: "", agent_id: "", needs_review: false });
  const agents = useData(() => api.get("/agents", { namespace_id: form.namespace_id }), [form.namespace_id]);
  const act = useAction();
  return (
    <Modal title="Assign a task directly" onClose={onClose} footer={<>
      <button className="btn" onClick={onClose}>Cancel</button>
      <button className="btn primary" disabled={act.busy || form.title.length < 3} onClick={() => act.run(() => api.post("/tasks", { ...form, agent_id: form.agent_id || null })).then((r) => r && onClose())}>Assign</button></>}>
      <div className="stack">
        <ErrorBox error={act.error} />
        <Field label="Organization"><NamespaceSelect value={form.namespace_id} onChange={(v) => setForm({ ...form, namespace_id: v, agent_id: "" })} /></Field>
        <Field label="Agent"><select value={form.agent_id} onChange={(e) => setForm({ ...form, agent_id: e.target.value })}>
          <option value="">The organization's lead</option>
          {(agents.data?.agents ?? []).filter((a: any) => a.status === "active").map((a: any) => <option key={a.id} value={a.id}>{a.name} — {a.title}</option>)}</select></Field>
        <Field label="Task"><input value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} /></Field>
        <Field label="Instructions"><textarea rows={6} value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} /></Field>
        <label className="row"><input type="checkbox" checked={form.needs_review} onChange={(e) => setForm({ ...form, needs_review: e.target.checked })} /> Have another agent review the result</label>
      </div>
    </Modal>
  );
}

export default function Work() {
  const { can, nsName } = useApp();
  const [params, setParams] = useSearchParams();
  const [ns, setNs] = useState("");
  const [tab, setTab] = useState("objectives");
  const [selected, setSelected] = useState<string | null>(null);
  const [modal, setModal] = useState<"objective" | "task" | null>(null);
  const objectives = useData(() => api.get("/objectives", { namespace_id: ns }), [ns], ["objective.", "task."]);
  const tasks = useData(() => api.get("/tasks", { namespace_id: ns, limit: 300 }), [ns], ["task."]);
  const agents = useData(() => api.get("/agents", { include_deactivated: true }), [], ["agent.created"]);
  const detail = useData(() => (selected ? api.get(`/objectives/${selected}`) : Promise.resolve(null)), [selected], ["task.", "objective.", "review."]);
  const act = useAction();
  const agentName = (id: string) => { const a = agents.data?.agents.find((x: any) => x.id === id); return a ? `${a.name} · ${a.title}` : "—"; };
  const openTask = params.get("task");
  const setTask = (id: string | null) => { const p = new URLSearchParams(params); id ? p.set("task", id) : p.delete("task"); setParams(p, { replace: true }); };

  useEffect(() => { if (!selected && objectives.data?.objectives.length) setSelected(objectives.data.objectives[0].id); }, [objectives.data, selected]);

  return (
    <Page title="Objectives & tasks" sub="An objective is planned into a task graph, executed in parallel by agents, reviewed, then evaluated against what was asked."
      actions={<>
        <div style={{ width: 220 }}><NamespaceSelect value={ns} onChange={(v) => { setNs(v); setSelected(null); }} allowAll /></div>
        {can("tasks:run") && <><button className="btn" onClick={() => setModal("task")}>＋ Task</button><button className="btn primary" onClick={() => setModal("objective")}>＋ Objective</button></>}
      </>}>
      {modal === "objective" && <NewObjective onClose={() => { setModal(null); objectives.reload(); }} namespaceId={ns || undefined} />}
      {modal === "task" && <NewTask onClose={() => { setModal(null); tasks.reload(); }} />}
      {openTask && <TaskModal taskId={openTask} onClose={() => setTask(null)} agentName={agentName} />}
      <Tabs tabs={[["objectives", "Objectives"], ["tasks", "All tasks"]]} value={tab} onChange={setTab} />
      <ErrorBox error={act.error} />
      {tab === "objectives" && (
        <div className="grid split-left">
          <Card flush title="Objectives">
            <Loading state={objectives}>{() => objectives.data.objectives.length ? (
              <table><tbody>{objectives.data.objectives.map((o: any) => (
                <tr key={o.id} className={"clickable" + (selected === o.id ? " selected" : "")} onClick={() => setSelected(o.id)}>
                  <td><div><strong>{o.title}</strong></div><div className="small muted">{nsName(o.namespace_id)} · {ago(o.created_at)}</div></td>
                  <td className="right"><Badge value={o.status} live={["planning", "running", "finalizing"].includes(o.status)} /></td>
                </tr>))}</tbody></table>) : <Empty>No objectives yet.</Empty>}</Loading>
          </Card>
          <div className="stack">
            {!detail.data ? <Card><Empty>Select an objective.</Empty></Card> : (() => {
              const { objective: o, tasks: ts } = detail.data;
              const work = ts.filter((t: any) => t.kind === "work" && !t.parent_task_id);
              const done = work.filter((t: any) => t.status === "done").length;
              const titleOf = (id: string) => ts.find((t: any) => t.id === id)?.title ?? id;
              return (<>
                <Card title={o.title} actions={<>
                  <Badge value={o.status} live={["planning", "running", "finalizing"].includes(o.status)} />
                  {can("tasks:run") && !["completed", "cancelled"].includes(o.status) && <button className="btn sm" disabled={act.busy}
                    onClick={() => window.confirm("Cancel this objective and its open tasks?") && act.run(() => api.post(`/objectives/${o.id}/cancel`)).then(detail.reload)}>Cancel</button>}</>}>
                  <div className="stack">
                    <dl className="kv">
                      <dt>Organization</dt><dd>{nsName(o.namespace_id)}</dd>
                      <dt>Owner</dt><dd>{agentName(o.owner_agent_id)}</dd>
                      <dt>Progress</dt><dd>{done}/{work.length} planned tasks done</dd>
                      {o.evaluation?.score != null && <><dt>Self-evaluation</dt><dd><Badge value={o.evaluation.achieved ? "achieved" : "not achieved"} tone={o.evaluation.achieved ? "ok" : "warn"} /> score {o.evaluation.score}</dd></>}
                    </dl>
                    {o.description && <details><summary className="muted clickable">Brief</summary><div className="prose" style={{ marginTop: 6 }}>{o.description}</div></details>}
                    {o.result && <div><h3>Outcome</h3><div className="codebox prose" style={{ marginTop: 6 }}>{o.result}</div></div>}
                    {o.evaluation?.lessons?.length > 0 && <div><h3>Lessons recorded to memory</h3><ul>{o.evaluation.lessons.map((l: string, i: number) => <li key={i}>{l}</li>)}</ul></div>}
                    {o.evaluation?.follow_ups?.length > 0 && <div><h3>Recommended follow-ups</h3><ul>{o.evaluation.follow_ups.map((l: string, i: number) => <li key={i}>{l}</li>)}</ul></div>}
                  </div>
                </Card>
                <Card flush title="Task graph">
                  <div className="table-wrap"><table>
                    <thead><tr><th>Task</th><th>Agent</th><th>Depends on</th><th>Status</th><th>Finished</th></tr></thead>
                    <tbody>{ts.map((t: any) => (
                      <tr key={t.id} className="clickable" onClick={() => setTask(t.id)}>
                        <td>{t.parent_task_id && <span className="muted">↳ </span>}{t.title} {t.kind !== "work" && <Badge value={t.kind} />}{t.meta?.needs_review && <Badge value="reviewed" tone="purple" />}</td>
                        <td>{agentName(t.assigned_agent_id)}</td>
                        <td className="small muted">{(t.depends_on ?? []).map(titleOf).join(", ") || "—"}</td>
                        <td><Badge value={t.status} live={t.status === "running"} /></td><td className="muted nowrap">{ago(t.finished_at)}</td>
                      </tr>))}</tbody>
                  </table></div>
                </Card></>);
            })()}
          </div>
        </div>)}
      {tab === "tasks" && (
        <Card flush>
          <Loading state={tasks}>{() => tasks.data.tasks.length ? (
            <div className="table-wrap"><table>
              <thead><tr><th>Task</th><th>Kind</th><th>Agent</th><th>Organization</th><th>Status</th><th>Created</th></tr></thead>
              <tbody>{tasks.data.tasks.map((t: any) => (
                <tr key={t.id} className="clickable" onClick={() => setTask(t.id)}>
                  <td>{t.title}</td><td><Badge value={t.kind} /></td><td>{agentName(t.assigned_agent_id)}</td><td className="muted">{nsName(t.namespace_id)}</td>
                  <td><Badge value={t.status} live={t.status === "running"} /></td><td className="muted nowrap">{ago(t.created_at)}</td>
                </tr>))}</tbody>
            </table></div>) : <Empty>No tasks yet.</Empty>}</Loading>
        </Card>)}
    </Page>
  );
}
