import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { Badge, Card, Empty, ErrorBox, Loading, Page, ago, useAction, useApp, useData } from "../components";

export default function Approvals() {
  const { can, nsName } = useApp();
  const state = useData(() => Promise.all([api.get("/approvals"), api.get("/agents", { include_deactivated: true })]), [], ["approval."]);
  const [reasons, setReasons] = useState<Record<string, string>>({});
  const act = useAction();
  const decide = (id: string, approve: boolean) =>
    act.run(() => api.post(`/approvals/${id}/decide`, { approve, reason: reasons[id] ?? "" })).then(state.reload);

  return (
    <Page title="Approvals" sub="Actions that wait for a human: anything financial, legal or touching sensitive data — always — plus whatever exceeds an agent's autonomy level. The agent's run is suspended until you decide.">
      <ErrorBox error={act.error} />
      <Loading state={state}>{() => {
        const [{ approvals }, { agents }] = state.data;
        const name = (id: string) => { const a = agents.find((x: any) => x.id === id); return a ? `${a.name} · ${a.title}` : "—"; };
        const pending = approvals.filter((a: any) => a.status === "pending");
        const done = approvals.filter((a: any) => a.status !== "pending");
        return (
          <div className="stack" style={{ gap: 16 }}>
            {pending.length === 0 && <Card><Empty>No decisions are waiting.</Empty></Card>}
            {pending.map((a: any) => (
              <Card key={a.id} title={<span>{a.action} <Badge value={a.category} tone={["financial", "legal", "sensitive_data"].includes(a.category) ? "danger" : "warn"} /></span>}
                actions={<span className="muted small">{ago(a.created_at)}</span>}>
                <div className="stack">
                  <dl className="kv">
                    <dt>Requested by</dt><dd>{name(a.agent_id)}</dd>
                    <dt>Organization</dt><dd>{nsName(a.namespace_id)}</dd>
                    {a.task_id && <><dt>Task</dt><dd><Link to={`/work?task=${a.task_id}`}>Open task and transcript</Link></dd></>}
                  </dl>
                  <div className="codebox"><pre>{JSON.stringify(a.payload.arguments ?? a.payload, null, 2)}</pre></div>
                  {can("approvals:decide") ? (
                    <div className="row">
                      <input style={{ flex: 1, minWidth: 220 }} placeholder="Reason (shown to the agent if you reject)" value={reasons[a.id] ?? ""}
                        onChange={(e) => setReasons({ ...reasons, [a.id]: e.target.value })} />
                      <button className="btn danger" disabled={act.busy} onClick={() => decide(a.id, false)}>Reject</button>
                      <button className="btn primary" disabled={act.busy} onClick={() => decide(a.id, true)}>Approve</button>
                    </div>) : <div className="notice">Your role cannot decide approvals.</div>}
                </div>
              </Card>))}
            <Card flush title="History">
              {done.length ? (
                <div className="table-wrap"><table>
                  <thead><tr><th>Action</th><th>Category</th><th>Agent</th><th>Decision</th><th>Reason</th><th>When</th></tr></thead>
                  <tbody>{done.map((a: any) => (
                    <tr key={a.id}><td>{a.action}</td><td><Badge value={a.category} /></td><td>{name(a.agent_id)}</td><td><Badge value={a.status} /></td>
                      <td className="muted">{a.reason || "—"}</td><td className="muted nowrap">{ago(a.decided_at)}</td></tr>))}</tbody>
                </table></div>) : <Empty>No past decisions.</Empty>}
            </Card>
          </div>);
      }}</Loading>
    </Page>
  );
}
