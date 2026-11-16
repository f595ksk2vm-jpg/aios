import { useState } from "react";
import { api } from "../api";
import { Badge, Card, Empty, ErrorBox, Field, Loading, NamespaceSelect, Page, Tabs, ago, money, num, useAction, useApp, useData } from "../components";

function Deployment() {
  const { can, reloadNamespaces } = useApp();
  const status = useData(() => api.get("/system/status"), [], ["system."]);
  const ns = useData(() => api.get("/namespaces"), [], ["business.", "project."]);
  const act = useAction();
  const place = (id: string, patch: any) => act.run(() => api.patch(`/namespaces/${id}`, patch)).then(() => { ns.reload(); reloadNamespaces(); });
  const sel = (n: any, key: "compute" | "models") => (
    <select style={{ width: 110 }} disabled={!can("system:control")} value={n[key]} onChange={(e) => place(n.id, { [key]: e.target.value })}>
      <option value="any">any</option><option value="local">local</option><option value="cloud">cloud</option></select>);
  return (
    <Loading state={status}>{() => {
      const s = status.data;
      return (
        <div className="stack" style={{ gap: 16 }}>
          <ErrorBox error={act.error} />
          <div className="grid cols-2">
            <Card title="This control plane">
              <dl className="kv">
                <dt>Deployment mode</dt><dd><Badge value={s.config.deployment_mode} tone="accent" /></dd>
                <dt>Node</dt><dd className="mono">{s.config.node_name} <Badge value={s.config.worker_location} /></dd>
                <dt>Database</dt><dd>{s.config.database_backend}</dd>
                <dt>Data directory</dt><dd className="mono">{s.config.data_dir}</dd>
                <dt>Embedded worker</dt><dd>{s.config.embedded_worker ? `yes · concurrency ${s.config.worker_concurrency}` : "no — workers run separately"}</dd>
                <dt>Code sandbox</dt><dd>{s.sandbox.driver} · {s.sandbox.network_isolated ? "no network by default" : "network NOT isolated"} · {s.sandbox.memory_mb} MB</dd>
                <dt>Embeddings</dt><dd className="mono">{s.config.embedder}</dd>
                <dt>Agent egress</dt><dd>{s.config.http_allowed_hosts.length ? s.config.http_allowed_hosts.join(", ") : "any public host"}{s.config.http_allow_private ? " + private networks" : ""}</dd>
                <dt>Version</dt><dd>{s.version}</dd>
              </dl>
              <p className="muted small" style={{ marginTop: 12 }}>These are set with <span className="mono">AIOS_*</span> environment variables when the process starts. The same build runs in local, cloud and hybrid mode.</p>
            </Card>
            <Card flush title="Workers" actions={<span className="small muted">Stateless — add one on any machine with <span className="mono">aios worker --location …</span></span>}>
              {s.workers.length ? <table><thead><tr><th>Worker</th><th>Location</th><th>Load</th><th>Sandbox</th><th>Heartbeat</th></tr></thead><tbody>{s.workers.map((w: any) => (
                <tr key={w.id}><td className="mono small">{w.id}</td><td><Badge value={w.location} /></td><td>{w.active_jobs}/{w.concurrency}</td>
                  <td className="small">{w.meta?.sandbox?.driver}</td><td><Badge value={w.online ? "online" : "offline"} /> <span className="small muted">{ago(w.heartbeat_at)}</span></td></tr>))}</tbody></table>
                : <Empty>No workers are connected, so queued work will wait.</Empty>}
            </Card>
          </div>
          <Card flush title="Placement policy" actions={<span className="small muted">Where each organization's workloads run, and which models may see its data</span>}>
            <Loading state={ns}>{() => (
              <table><thead><tr><th>Organization</th><th>Kind</th><th>Compute</th><th>Models</th><th>Status</th></tr></thead><tbody>{ns.data.namespaces.map((n: any) => (
                <tr key={n.id}><td>{n.parent_id && n.parent_id !== "ns_root" ? "↳ " : ""}<strong>{n.kind === "system" ? "AI-OS (global default)" : n.name}</strong></td><td><Badge value={n.kind} /></td>
                  <td>{sel(n, "compute")}</td><td>{sel(n, "models")}</td><td><Badge value={n.status} /></td></tr>))}</tbody></table>)}</Loading>
          </Card>
        </div>);
    }}</Loading>
  );
}

function Models() {
  const { can } = useApp();
  const state = useData(() => api.get("/models"), []);
  const act = useAction();
  const [health, setHealth] = useState<Record<string, any>>({});
  const [prov, setProv] = useState<any>({ name: "", kind: "openai_compat", location: "local", base_url: "http://localhost:11434/v1", secret_name: "" });
  const [model, setModel] = useState<any>({ provider_id: "", model_name: "", tier: "balanced", price_in: 0, price_out: 0 });
  const [route, setRoute] = useState<any>({ ns: "ns_root", tier: "balanced", prefer: "balanced", result: null });
  const manage = can("models:manage");
  return (
    <Loading state={state}>{() => {
      const { providers, models } = state.data;
      const pname = (id: string) => providers.find((p: any) => p.id === id)?.name ?? id;
      const toggle = (p: any) => act.run(() => api.put("/models/providers", { name: p.name, kind: p.kind, location: p.location, base_url: p.base_url, secret_name: p.secret_name, enabled: !p.enabled, meta: p.meta })).then(state.reload);
      return (
        <div className="stack" style={{ gap: 16 }}>
          <ErrorBox error={act.error} />
          <Card flush title="Providers" actions={<span className="small muted">Cloud APIs and locally hosted endpoints side by side</span>}>
            <div className="table-wrap"><table><thead><tr><th>Provider</th><th>Protocol</th><th>Location</th><th>Endpoint</th><th>Credential</th><th>Status</th><th></th></tr></thead><tbody>{providers.map((p: any) => (
              <tr key={p.id}><td><strong>{p.name}</strong></td><td className="mono small">{p.kind}</td><td><Badge value={p.location} /></td><td className="mono small">{p.base_url ?? "default"}</td>
                <td>{p.secret_name ? <><span className="mono small">{p.secret_name}</span> <Badge value={p.credential_set ? "set" : "missing"} tone={p.credential_set ? "ok" : "warn"} /></> : <span className="muted">none needed</span>}</td>
                <td><Badge value={p.enabled ? "enabled" : "disabled"} tone={p.enabled ? "ok" : ""} />{health[p.id] && <div className="small" style={{ color: `var(--${health[p.id].ok ? "ok" : "danger"})` }}>{health[p.id].detail}</div>}</td>
                <td className="right nowrap">{manage && <>
                  <button className="btn sm" onClick={() => act.run(() => api.post(`/models/providers/${p.id}/health`)).then((h) => h && setHealth({ ...health, [p.id]: h }))}>Test</button>{" "}
                  <button className="btn sm" title="List the models this endpoint serves and add them to the catalog" onClick={() => act.run(async () => {
                    const found = (await api.post(`/models/providers/${p.id}/discover`)).models;
                    for (const m of found.slice(0, 40)) await api.put("/models/catalog", { provider_id: p.id, model_name: m.model_name, display_name: m.display_name });
                    state.reload();
                  })}>Discover</button>{" "}
                  <button className="btn sm" onClick={() => toggle(p)}>{p.enabled ? "Disable" : "Enable"}</button></>}</td></tr>))}</tbody></table></div>
          </Card>
          <Card flush title="Model catalog" actions={<span className="small muted">Prices in USD per million tokens — used for routing and cost tracking</span>}>
            {models.length ? <div className="table-wrap"><table><thead><tr><th>Model</th><th>Provider</th><th>Tier</th><th className="right">Context</th><th className="right">Input</th><th className="right">Output</th><th>Latency</th><th>Tools</th><th></th></tr></thead><tbody>{models.map((m: any) => (
              <tr key={m.id}><td><strong>{m.display_name}</strong><div className="mono small muted">{m.model_name}</div></td><td>{pname(m.provider_id)}</td><td><Badge value={m.tier} /></td>
                <td className="right">{num(m.context_window)}</td><td className="right">{money(m.price_in)}</td><td className="right">{money(m.price_out)}</td><td>{["", "fast", "medium", "slow"][m.latency_class]}</td><td>{m.supports_tools ? "yes" : "no"}</td>
                <td className="right">{manage && <button className="btn sm ghost" onClick={() => act.run(() => api.del(`/models/catalog/${m.id}`)).then(state.reload)}>Remove</button>}</td></tr>))}</tbody></table></div> : <Empty>No models.</Empty>}
          </Card>
          <div className="grid cols-3">
            <Card title="Routing preview">
              <div className="stack">
                <p className="muted small">Agents on automatic routing get the best model for their tier and preference that the organization's placement allows; the rest of the list is the fallback order.</p>
                <Field label="Organization"><NamespaceSelect value={route.ns} onChange={(v) => setRoute({ ...route, ns: v })} /></Field>
                <div className="form-grid">
                  <Field label="Tier"><select value={route.tier} onChange={(e) => setRoute({ ...route, tier: e.target.value })}><option>fast</option><option>balanced</option><option>deep</option></select></Field>
                  <Field label="Optimise for"><select value={route.prefer} onChange={(e) => setRoute({ ...route, prefer: e.target.value })}><option>balanced</option><option>quality</option><option>cost</option><option>latency</option></select></Field>
                </div>
                <button className="btn" onClick={() => act.run(() => api.post("/models/route", { namespace_id: route.ns, model: { mode: "auto", tier: route.tier, prefer: route.prefer } })).then((r) => r && setRoute({ ...route, result: r }))}>Preview</button>
                {route.result && (route.result.candidates.length ? <ol style={{ margin: 0, paddingLeft: 18 }}>{route.result.candidates.map((c: any) => <li key={c.ref}><span className="mono small">{c.ref}</span> <Badge value={c.location} /></li>)}</ol>
                  : <div className="error">No enabled model satisfies placement “{route.result.models_placement}”.</div>)}
              </div>
            </Card>
            {manage && <Card title="Add a provider">
              <div className="stack">
                <Field label="Name"><input value={prov.name} onChange={(e) => setProv({ ...prov, name: e.target.value })} placeholder="ollama" /></Field>
                <div className="form-grid">
                  <Field label="Protocol"><select value={prov.kind} onChange={(e) => setProv({ ...prov, kind: e.target.value })}><option value="openai_compat">OpenAI-compatible</option><option value="anthropic">Anthropic</option></select></Field>
                  <Field label="Location"><select value={prov.location} onChange={(e) => setProv({ ...prov, location: e.target.value })}><option>local</option><option>cloud</option></select></Field>
                </div>
                <Field label="Base URL" hint="Ollama, vLLM, llama.cpp, LM Studio, or any hosted OpenAI-compatible API."><input value={prov.base_url} onChange={(e) => setProv({ ...prov, base_url: e.target.value })} /></Field>
                <Field label="Secret holding the API key (optional)"><input value={prov.secret_name} onChange={(e) => setProv({ ...prov, secret_name: e.target.value })} placeholder="MY_PROVIDER_KEY" /></Field>
                <button className="btn primary" disabled={!prov.name} onClick={() => act.run(() => api.put("/models/providers", { ...prov, base_url: prov.base_url || null, secret_name: prov.secret_name || null })).then(state.reload)}>Save provider</button>
              </div>
            </Card>}
            {manage && <Card title="Add a model">
              <div className="stack">
                <Field label="Provider"><select value={model.provider_id} onChange={(e) => setModel({ ...model, provider_id: e.target.value })}><option value="">Choose…</option>{providers.map((p: any) => <option key={p.id} value={p.id}>{p.name}</option>)}</select></Field>
                <Field label="Model name (as the endpoint expects it)"><input value={model.model_name} onChange={(e) => setModel({ ...model, model_name: e.target.value })} placeholder="qwen2.5-coder:14b" /></Field>
                <div className="form-grid">
                  <Field label="Tier"><select value={model.tier} onChange={(e) => setModel({ ...model, tier: e.target.value })}><option>fast</option><option>balanced</option><option>deep</option></select></Field>
                  <Field label="$ in / out per MTok"><div className="row" style={{ flexWrap: "nowrap" }}><input type="number" min={0} value={model.price_in} onChange={(e) => setModel({ ...model, price_in: Number(e.target.value) })} /><input type="number" min={0} value={model.price_out} onChange={(e) => setModel({ ...model, price_out: Number(e.target.value) })} /></div></Field>
                </div>
                <button className="btn primary" disabled={!model.provider_id || !model.model_name} onClick={() => act.run(() => api.put("/models/catalog", model)).then(state.reload)}>Save model</button>
              </div>
            </Card>}
          </div>
        </div>);
    }}</Loading>
  );
}

function Integrations() {
  const { can } = useApp();
  const state = useData(() => api.get("/tools"), []);
  const act = useAction();
  const [c, setC] = useState<any>({ name: "", base_url: "https://", secret_name: "", description: "" });
  return (
    <Loading state={state}>{() => {
      const { tools, integrations, policy, categories, autonomy_levels } = state.data;
      return (
        <div className="stack" style={{ gap: 16 }}>
          <ErrorBox error={act.error} />
          <Card flush title="Integrations" actions={<span className="small muted">Credentials live in the secret store; agents only ever see the connector's name</span>}>
            <div className="table-wrap"><table><thead><tr><th>Integration</th><th>Category</th><th>Endpoint</th><th>Needs secret</th><th>Status</th><th>Tool</th><th></th></tr></thead><tbody>{integrations.map((i: any) => (
              <tr key={i.name}><td><strong>{i.name}</strong><div className="small muted">{i.description}</div></td><td><Badge value={i.group} /></td><td className="mono small">{i.base_url}</td><td className="mono small">{i.secret_name ?? "—"}</td>
                <td><Badge value={i.configured ? "configured" : "not configured"} tone={i.configured ? "ok" : ""} /></td><td className="mono small">{i.tool}</td>
                <td>{i.custom && can("system:control") && <button className="btn sm ghost" onClick={() => act.run(() => api.del(`/integrations/${i.name}`)).then(state.reload)}>Remove</button>}</td></tr>))}</tbody></table></div>
            {can("system:control") && <div className="row" style={{ padding: 12, borderTop: "1px solid var(--border)" }}>
              <input style={{ width: 140 }} placeholder="name" value={c.name} onChange={(e) => setC({ ...c, name: e.target.value })} />
              <input style={{ flex: 1, minWidth: 200 }} placeholder="https://api.example.com" value={c.base_url} onChange={(e) => setC({ ...c, base_url: e.target.value })} />
              <input style={{ width: 190 }} placeholder="SECRET_NAME (optional)" value={c.secret_name} onChange={(e) => setC({ ...c, secret_name: e.target.value })} />
              <input style={{ flex: 1, minWidth: 160 }} placeholder="What this API is for" value={c.description} onChange={(e) => setC({ ...c, description: e.target.value })} />
              <button className="btn primary" disabled={!c.name || c.base_url.length < 9} onClick={() => act.run(() => api.post("/integrations", { ...c, secret_name: c.secret_name || null })).then(state.reload)}>Add REST connector</button>
            </div>}
          </Card>
          <Card flush title="Approval policy" actions={<span className="small muted">What each autonomy level may do without a human</span>}>
            <div className="table-wrap"><table><thead><tr><th>Action category</th>{autonomy_levels.map((l: string) => <th key={l}>{l}</th>)}</tr></thead><tbody>{categories.map((cat: string) => (
              <tr key={cat}><td><strong>{cat.replace(/_/g, " ")}</strong></td>{autonomy_levels.map((l: string) => (
                <td key={l}><Badge value={policy[l][cat] === "allow" ? "runs" : "needs approval"} tone={policy[l][cat] === "allow" ? "ok" : "warn"} /></td>))}</tr>))}</tbody></table></div>
          </Card>
          <Card flush title={`Tool registry · ${tools.length} tools`}>
            <div className="table-wrap"><table><thead><tr><th>Tool</th><th>Layer</th><th>Group</th><th>Category</th><th>Permission required</th><th>Description</th></tr></thead><tbody>{tools.map((t: any) => (
              <tr key={t.name}><td className="mono">{t.name}</td><td><Badge value={t.layer} tone={t.layer === "kernel" ? "info" : t.layer === "workforce" ? "purple" : "accent"} /></td><td>{t.group}</td>
                <td><Badge value={t.category} /></td><td className="mono small">{t.permission}</td><td className="small muted">{t.description}</td></tr>))}</tbody></table></div>
          </Card>
        </div>);
    }}</Loading>
  );
}

function Memory() {
  const { can, nsName } = useApp();
  const [ns, setNs] = useState("ns_root");
  const [q, setQ] = useState("");
  const [results, setResults] = useState<any[] | null>(null);
  const recent = useData(() => api.get("/memory", { namespace_id: ns === "ns_root" ? "" : ns }), [ns], ["task.completed", "objective.completed"]);
  const [note, setNote] = useState({ title: "", content: "", document: false });
  const act = useAction();
  const search = () => act.run(() => api.get("/memory/search", { q, namespace_id: ns })).then((r) => r && setResults(r.results));
  return (
    <div className="stack" style={{ gap: 16 }}>
      <ErrorBox error={act.error} />
      <Card title="Semantic search" actions={<span className="small muted">embedder: <span className="mono">{recent.data?.embedder}</span></span>}>
        <div className="stack">
          <p className="muted small">Search as an organization would: its own knowledge, what its parent shares downward, its projects' knowledge, and anything explicitly granted to it. Other organizations' memory is not reachable.</p>
          <div className="row">
            <div style={{ width: 240 }}><NamespaceSelect value={ns} onChange={(v) => { setNs(v); setResults(null); }} /></div>
            <input style={{ flex: 1, minWidth: 200 }} placeholder="What do we know about…" value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => e.key === "Enter" && q && search()} />
            <button className="btn primary" disabled={!q || act.busy} onClick={search}>Search</button>
          </div>
          {results && (results.length ? results.map((r) => (
            <div key={r.id} className="msg"><div className="msg-head">{r.score} · {r.kind} · {nsName(r.namespace_id)} · {r.created_at.slice(0, 10)}</div>
              <div className="msg-body"><strong>{r.title}</strong><div className="prose">{r.content}</div></div></div>)) : <Empty>Nothing relevant found.</Empty>)}
        </div>
      </Card>
      <div className="grid split">
        <Card flush title="Recent memory">
          <Loading state={recent}>{() => recent.data.items.length ? (
            <div className="table-wrap"><table><thead><tr><th>Title</th><th>Kind</th><th>Scope</th><th>Organization</th><th>When</th><th></th></tr></thead><tbody>{recent.data.items.slice(0, 60).map((m: any) => (
              <tr key={m.id}><td><strong>{m.title || "(untitled)"}</strong><div className="small muted">{m.content.slice(0, 140)}</div></td><td><Badge value={m.kind} /></td><td><Badge value={m.scope === "agent" ? "private" : "shared"} /></td>
                <td className="muted">{nsName(m.namespace_id)}</td><td className="muted nowrap">{ago(m.created_at)}</td>
                <td>{can("memory:write") && <button className="btn sm ghost" onClick={() => act.run(() => api.del(`/memory/${m.id}`)).then(recent.reload)}>Delete</button>}</td></tr>))}</tbody></table></div>
          ) : <Empty>Nothing stored yet. Task results, objective outcomes and lessons are written here automatically.</Empty>}</Loading>
        </Card>
        <div className="stack" style={{ gap: 16 }}>
          {can("memory:write") && <Card title="Add knowledge">
            <div className="stack">
              <Field label="Title"><input value={note.title} onChange={(e) => setNote({ ...note, title: e.target.value })} /></Field>
              <Field label="Content"><textarea rows={6} value={note.content} onChange={(e) => setNote({ ...note, content: e.target.value })} /></Field>
              <label className="row"><input type="checkbox" checked={note.document} onChange={(e) => setNote({ ...note, document: e.target.checked })} /> Store as a document (kept whole, chunked for retrieval)</label>
              <button className="btn primary" disabled={!note.content || (note.document && !note.title)} onClick={() => act.run(() => api.post(note.document ? "/memory/documents" : "/memory", { namespace_id: ns, title: note.title, content: note.content })).then(() => { recent.reload(); setNote({ title: "", content: "", document: false }); })}>Save to {nsName(ns)}</button>
            </div>
          </Card>}
          <Card flush title="Documents">
            {recent.data?.documents.length ? <table><tbody>{recent.data.documents.map((d: any) => (
              <tr key={d.id}><td><strong>{d.title}</strong><div className="small muted">{nsName(d.namespace_id)} · {ago(d.created_at)}</div></td>
                <td className="right">{can("memory:write") && <button className="btn sm ghost" onClick={() => act.run(() => api.del(`/memory/documents/${d.id}`)).then(recent.reload)}>Delete</button>}</td></tr>))}</tbody></table> : <Empty>No documents.</Empty>}
          </Card>
        </div>
      </div>
    </div>
  );
}

function Security() {
  const { can, nsName, user } = useApp();
  const admin = can("system:control");
  const secrets = useData(() => (can("secrets:manage") ? api.get("/secrets") : Promise.resolve({ secrets: [] })), []);
  const budgets = useData(() => api.get("/budgets"), [], ["task.completed"]);
  const audit = useData(() => (admin ? Promise.all([api.get("/audit", { limit: 120 }), api.get("/audit/verify")]) : Promise.resolve(null)), []);
  const backups = useData(() => (admin ? api.get("/system/backups") : Promise.resolve({ backups: [] })), []);
  const users = useData(() => (can("users:manage") ? api.get("/users") : Promise.resolve(null)), []);
  const agents = useData(() => api.get("/agents"), []);
  const act = useAction();
  const [sec, setSec] = useState({ name: "", value: "", namespace_id: "ns_root" });
  const [bud, setBud] = useState<any>({ scope_kind: "global", scope_id: "ns_root", period: "month", limit_usd: "" });
  const [nu, setNu] = useState({ email: "", password: "", name: "", role: "operator" });
  const scopeLabel = (b: any) => b.scope_kind === "global" ? "Everything" : b.scope_kind === "namespace" ? nsName(b.scope_id) : (agents.data?.agents.find((a: any) => a.id === b.scope_id)?.name ?? b.scope_id);
  return (
    <div className="stack" style={{ gap: 16 }}>
      <ErrorBox error={act.error} />
      <div className="grid cols-2">
        <Card flush title="Spending limits" actions={<span className="small muted">Hard stops, checked before every model call</span>}>
          <Loading state={budgets}>{() => (<>
            {budgets.data.budgets.length ? <table><thead><tr><th>Scope</th><th>Period</th><th className="right">Spent</th><th className="right">Limit</th><th></th></tr></thead><tbody>{budgets.data.budgets.map((b: any) => (
              <tr key={b.id}><td><Badge value={b.scope_kind} /> {scopeLabel(b)}</td><td>{b.period}</td>
                <td className="right" style={{ color: b.spent_usd >= b.limit_usd ? "var(--danger)" : undefined }}>{money(b.spent_usd, 4)}</td><td className="right">{money(b.limit_usd)}</td>
                <td className="right">{admin && <button className="btn sm ghost" onClick={() => act.run(() => api.del(`/budgets/${b.id}`)).then(budgets.reload)}>Remove</button>}</td></tr>))}</tbody></table> : <Empty>No limits set: spending is unlimited.</Empty>}
            {admin && <div className="row" style={{ padding: 12, borderTop: "1px solid var(--border)" }}>
              <select style={{ width: 130 }} value={bud.scope_kind} onChange={(e) => setBud({ ...bud, scope_kind: e.target.value, scope_id: e.target.value === "agent" ? "" : "ns_root" })}><option value="global">Everything</option><option value="namespace">Organization</option><option value="agent">Agent</option></select>
              {bud.scope_kind === "namespace" && <div style={{ width: 200 }}><NamespaceSelect value={bud.scope_id} onChange={(v) => setBud({ ...bud, scope_id: v })} /></div>}
              {bud.scope_kind === "agent" && <select style={{ width: 200 }} value={bud.scope_id} onChange={(e) => setBud({ ...bud, scope_id: e.target.value })}><option value="">Choose agent…</option>{(agents.data?.agents ?? []).map((a: any) => <option key={a.id} value={a.id}>{a.name} — {a.title}</option>)}</select>}
              <select style={{ width: 100 }} value={bud.period} onChange={(e) => setBud({ ...bud, period: e.target.value })}><option>day</option><option>month</option><option>total</option></select>
              <input style={{ width: 110 }} type="number" min={0} placeholder="USD" value={bud.limit_usd} onChange={(e) => setBud({ ...bud, limit_usd: e.target.value })} />
              <button className="btn primary" disabled={bud.limit_usd === "" || (bud.scope_kind === "agent" && !bud.scope_id)} onClick={() => act.run(() => api.put("/budgets", { ...bud, limit_usd: Number(bud.limit_usd) })).then(budgets.reload)}>Set limit</button>
            </div>}</>)}</Loading>
        </Card>
        <Card flush title="Secrets" actions={<span className="small muted">Encrypted at rest · write-only · never shown to agents</span>}>
          {!can("secrets:manage") ? <Empty>Your role cannot manage secrets.</Empty> : <Loading state={secrets}>{() => (<>
            {secrets.data.secrets.length ? <table><thead><tr><th>Name</th><th>Scope</th><th>Set</th><th></th></tr></thead><tbody>{secrets.data.secrets.map((s: any) => (
              <tr key={s.id}><td className="mono">{s.name}</td><td>{nsName(s.namespace_id)}</td><td className="muted">{ago(s.rotated_at ?? s.created_at)}</td>
                <td className="right"><button className="btn sm ghost" onClick={() => window.confirm(`Delete secret ${s.name}?`) && act.run(() => api.del(`/secrets/${s.name}`, { namespace_id: s.namespace_id })).then(secrets.reload)}>Delete</button></td></tr>))}</tbody></table> : <Empty>No secrets stored.</Empty>}
            <div className="row" style={{ padding: 12, borderTop: "1px solid var(--border)" }}>
              <input style={{ width: 190 }} placeholder="ANTHROPIC_API_KEY" value={sec.name} onChange={(e) => setSec({ ...sec, name: e.target.value })} />
              <input style={{ flex: 1, minWidth: 140 }} type="password" placeholder="value" autoComplete="off" value={sec.value} onChange={(e) => setSec({ ...sec, value: e.target.value })} />
              <div style={{ width: 170 }}><NamespaceSelect value={sec.namespace_id} onChange={(v) => setSec({ ...sec, namespace_id: v })} kinds={["system", "business"]} /></div>
              <button className="btn primary" disabled={!sec.name || !sec.value} onClick={() => act.run(() => api.put("/secrets", sec)).then(() => { secrets.reload(); setSec({ ...sec, name: "", value: "" }); })}>Store</button>
            </div></>)}</Loading>}
        </Card>
      </div>
      <div className="grid cols-2">
        {users.data && <Card flush title="People & roles">
          <table><thead><tr><th>User</th><th>Role</th><th>Status</th></tr></thead><tbody>{users.data.users.map((u: any) => (
            <tr key={u.id}><td><strong>{u.name}</strong><div className="small muted">{u.email}</div></td>
              <td><select style={{ width: 130 }} disabled={u.id === user.id} value={u.role} onChange={(e) => act.run(() => api.patch(`/users/${u.id}`, { role: e.target.value })).then(users.reload)}>{users.data.roles.map((r: string) => <option key={r}>{r}</option>)}</select></td>
              <td>{u.id === user.id ? <Badge value="you" tone="accent" /> : <button className="btn sm" onClick={() => act.run(() => api.patch(`/users/${u.id}`, { is_active: !u.is_active })).then(users.reload)}>{u.is_active ? "Deactivate" : "Activate"}</button>}</td></tr>))}</tbody></table>
          <div className="row" style={{ padding: 12, borderTop: "1px solid var(--border)" }}>
            <input style={{ flex: 1, minWidth: 110 }} placeholder="Name" value={nu.name} onChange={(e) => setNu({ ...nu, name: e.target.value })} />
            <input style={{ flex: 1, minWidth: 150 }} placeholder="Email" value={nu.email} onChange={(e) => setNu({ ...nu, email: e.target.value })} />
            <input style={{ flex: 1, minWidth: 120 }} type="password" placeholder="Password (8+)" autoComplete="new-password" value={nu.password} onChange={(e) => setNu({ ...nu, password: e.target.value })} />
            <select style={{ width: 110 }} value={nu.role} onChange={(e) => setNu({ ...nu, role: e.target.value })}><option>viewer</option><option>operator</option><option>admin</option></select>
            <button className="btn primary" disabled={!nu.email || nu.password.length < 8} onClick={() => act.run(() => api.post("/users", nu)).then(() => { users.reload(); setNu({ email: "", password: "", name: "", role: "operator" }); })}>Add</button>
          </div>
        </Card>}
        {admin && <Card flush title="Backup & recovery" actions={<button className="btn sm primary" disabled={act.busy} onClick={() => act.run(() => api.post("/system/backups", {})).then(backups.reload)}>Create backup now</button>}>
          {backups.data?.backups.length ? <table><thead><tr><th>Archive</th><th className="right">Size</th></tr></thead><tbody>{backups.data.backups.map((b: any) => (
            <tr key={b.file}><td className="mono small">{b.file}</td><td className="right">{num(Math.round(b.size_bytes / 1024))} KB</td></tr>))}</tbody></table> : <Empty>No backups yet.</Empty>}
          <p className="muted small" style={{ padding: "0 14px 12px" }}>A backup is a consistent database snapshot plus every workspace and its git history, written to the data directory. Restore with <span className="mono">aios restore &lt;archive&gt;</span> while the system is stopped. The secret-store master key is excluded unless you ask for it on the command line.</p>
        </Card>}
      </div>
      {audit.data && <Card flush title="Audit trail" actions={<Badge value={audit.data[1].ok ? `hash chain intact · ${audit.data[1].entries} entries` : `TAMPERING DETECTED at entry ${audit.data[1].broken_at}`} tone={audit.data[1].ok ? "ok" : "danger"} />}>
        <div className="table-wrap" style={{ maxHeight: 460, overflowY: "auto" }}><table><thead><tr><th>When</th><th>Actor</th><th>Action</th><th>Resource</th><th>Outcome</th><th>Detail</th></tr></thead><tbody>{audit.data[0].entries.map((e: any) => (
          <tr key={e.id}><td className="muted nowrap">{ago(e.ts)}</td><td className="mono small">{e.actor}</td><td className="mono small">{e.action}</td><td className="mono small">{e.resource}</td>
            <td><Badge value={e.outcome} /></td><td className="small muted" style={{ maxWidth: 420 }}><div className="truncate" title={JSON.stringify(e.detail)}>{Object.keys(e.detail).length ? JSON.stringify(e.detail) : ""}</div></td></tr>))}</tbody></table></div>
      </Card>}
    </div>
  );
}

export default function System() {
  const [tab, setTab] = useState("deployment");
  return (
    <Page title="AI-OS infrastructure" sub="Layer 1: the business-agnostic foundation. Deployment and placement, model providers and routing, integrations, memory, and the security controls every agent action passes through.">
      <Tabs value={tab} onChange={setTab} tabs={[["deployment", "Deployment & resources"], ["models", "Models"], ["integrations", "Integrations & tools"], ["memory", "Memory"], ["security", "Security"]]} />
      {tab === "deployment" && <Deployment />}
      {tab === "models" && <Models />}
      {tab === "integrations" && <Integrations />}
      {tab === "memory" && <Memory />}
      {tab === "security" && <Security />}
    </Page>
  );
}
