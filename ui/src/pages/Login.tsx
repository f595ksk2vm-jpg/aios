import { FormEvent, useEffect, useState } from "react";
import { api } from "../api";
import { ErrorBox, Field, useAction, useApp } from "../components";

export default function Login() {
  const { login } = useApp();
  const [setup, setSetup] = useState<boolean | null>(null);
  const [form, setForm] = useState({ email: "", password: "", name: "" });
  const act = useAction();
  useEffect(() => { api.get("/auth/state").then((s) => setSetup(s.needs_setup)).catch(() => setSetup(false)); }, []);

  const submit = (e: FormEvent) => {
    e.preventDefault();
    act.run(async () => {
      const res = await api.post(setup ? "/auth/setup" : "/auth/login", form);
      await login(res.token);
    });
  };
  if (setup === null) return <div className="login muted">Connecting…</div>;
  return (
    <div className="login">
      <form className="card stack" onSubmit={submit}>
        <div className="row"><span className="brand-mark" style={{ color: "#fff" }}>◉</span><h1>AI-OS</h1></div>
        <p className="muted">{setup ? "Create the owner account for this installation. This is only possible once."
          : "Sign in to the command center."}</p>
        <ErrorBox error={act.error} />
        {setup && <Field label="Your name"><input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} autoComplete="name" /></Field>}
        <Field label="Email"><input type="email" required value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} autoComplete="username" /></Field>
        <Field label="Password" hint={setup ? "At least 8 characters." : undefined}>
          <input type="password" required minLength={8} value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })}
            autoComplete={setup ? "new-password" : "current-password"} />
        </Field>
        <button className="btn primary" disabled={act.busy}>{setup ? "Create owner account" : "Sign in"}</button>
      </form>
    </div>
  );
}
