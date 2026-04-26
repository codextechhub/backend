import { useState } from "react";
import { useNavigate } from "react-router-dom";
import axios from "axios";
import { getBaseUrl } from "../api";
import { EP } from "../endpoints";

export default function Login() {
  const [mode, setMode] = useState("login"); // "login" | "forgot" | "sent"
  const [form, setForm] = useState({ email: "", password: "" });
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [fpEmail, setFpEmail] = useState("");
  const [fpLoading, setFpLoading] = useState(false);
  const [fpError, setFpError] = useState("");
  const navigate = useNavigate();

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (!form.email || !form.password) {
      setError("Please enter your email and password.");
      return;
    }
    setLoading(true);
    setError("");
    try {
      const { data } = await axios.post(
        `${getBaseUrl()}${EP.AUTH_LOGIN}`,
        { email: form.email, password: form.password }
      );
      localStorage.setItem("access_token", data.data.access);
      localStorage.setItem("refresh_token", data.data.refresh);
      navigate("/dashboard");
    } catch (err) {
      const d = err.response?.data;
      setError(
        d?.detail ||
        d?.non_field_errors?.[0] ||
        (typeof d === "string" ? d : null) ||
        "Login failed. Check your credentials."
      );
    } finally {
      setLoading(false);
    }
  };

  const handleForgotPassword = async (e) => {
    e.preventDefault();
    if (!fpEmail) { setFpError("Please enter your email address."); return; }
    setFpLoading(true);
    setFpError("");
    try {
      await axios.post(
        `${getBaseUrl()}${EP.AUTH_PASSWORD_RESET}`,
        { email: fpEmail }
      );
      setMode("sent");
    } catch (err) {
      const d = err.response?.data;
      setFpError(d?.detail || d?.email?.[0] || "Could not send reset link. Try again.");
    } finally {
      setFpLoading(false);
    }
  };

  const leftPanel = (
    <div className="login-left">
      <div className="ll-logo">
        <div className="ll-gem">XV</div>
        <div className="ll-brand">X <span>Vision Systems</span></div>
      </div>
      <div>
        <h1 className="ll-tagline">
          Internal<br /><em>Staff</em><br />Console.
        </h1>
        <p className="ll-desc">
          Vision operations dashboard for<br />
          internal team use. Manage schools,<br />
          RBAC, users, and audit trails.
        </p>
      </div>
    </div>
  );

  if (mode === "sent") return (
    <div className="login-screen">
      <div className="login-wrap">
        {leftPanel}
        <div className="login-right">
          <div className="lr-head">
            <h2>Check your <em>email</em></h2>
            <p>A password reset link was sent to</p>
          </div>
          <div style={{ fontFamily: "var(--fm)", fontSize: 13, padding: "10px 14px", background: "var(--v-l)", borderRadius: "var(--r8)", color: "var(--v)", marginBottom: 20, wordBreak: "break-all" }}>
            {fpEmail}
          </div>
          <p style={{ fontSize: 13, color: "var(--ink3)", marginBottom: 20 }}>
            Didn't receive it? Check your spam folder or try again in a few minutes.
          </p>
          <button className="btn btn-secondary btn-full" onClick={() => { setMode("forgot"); setFpError(""); }}>
            Resend link
          </button>
          <button
            className="btn btn-ghost btn-full"
            style={{ marginTop: 10 }}
            onClick={() => { setMode("login"); setFpEmail(""); setFpError(""); }}
          >
            ← Back to sign in
          </button>
        </div>
      </div>
    </div>
  );

  if (mode === "forgot") return (
    <div className="login-screen">
      <div className="login-wrap">
        {leftPanel}
        <div className="login-right">
          <div className="lr-head">
            <h2>Reset <em>password</em></h2>
            <p>Enter your email and we'll send you a reset link</p>
          </div>
          <form onSubmit={handleForgotPassword}>
            <div className="f-group">
              <label htmlFor="fp-email">Email address</label>
              <input
                id="fp-email"
                type="email"
                placeholder="you@codexng.com"
                value={fpEmail}
                onChange={(e) => setFpEmail(e.target.value)}
                autoFocus
              />
            </div>
            {fpError && <div className="f-err">{fpError}</div>}
            <button
              type="submit"
              className="btn btn-primary btn-full"
              style={{ marginTop: 16 }}
              disabled={fpLoading}
            >
              {fpLoading ? <span className="spin" /> : null}
              <span>{fpLoading ? "Sending…" : "Send reset link"}</span>
            </button>
            <div style={{ textAlign: "center", marginTop: 16 }}>
              <button
                type="button"
                style={{ background: "none", border: "none", cursor: "pointer", color: "var(--ink3)", fontSize: 13 }}
                onClick={() => { setMode("login"); setFpError(""); }}
              >
                ← Back to sign in
              </button>
            </div>
          </form>
        </div>
      </div>
    </div>
  );

  return (
    <div className="login-screen">
      <div className="login-wrap">
        {leftPanel}
        <div className="login-right">
          <div className="lr-head">
            <h2>Welcome <em>back</em></h2>
            <p>Sign in with your Vision Staff credentials</p>
          </div>
          <form onSubmit={handleSubmit}>
            <div className="f-group">
              <label htmlFor="email">Email address</label>
              <input
                id="email"
                type="email"
                placeholder="you@codexng.com"
                value={form.email}
                onChange={(e) => setForm({ ...form, email: e.target.value })}
                autoFocus
              />
            </div>
            <div className="f-group">
              <label htmlFor="password">Password</label>
              <input
                id="password"
                type="password"
                placeholder="••••••••"
                value={form.password}
                onChange={(e) => setForm({ ...form, password: e.target.value })}
              />
            </div>
            <div style={{ textAlign: "right", marginTop: -4, marginBottom: 4 }}>
              <button
                type="button"
                style={{ background: "none", border: "none", cursor: "pointer", color: "var(--v)", fontSize: 12, padding: "4px 0" }}
                onClick={() => { setMode("forgot"); setError(""); }}
              >
                Forgot password?
              </button>
            </div>
            {error && <div className="f-err">{error}</div>}
            <button
              type="submit"
              className="btn btn-primary btn-full"
              style={{ marginTop: 16 }}
              disabled={loading}
            >
              {loading ? <span className="spin" /> : null}
              <span>{loading ? "Signing in…" : "Sign in to console"}</span>
            </button>
          </form>
        </div>
      </div>
    </div>
  );
}
