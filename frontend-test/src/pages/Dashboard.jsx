import { useState, useEffect, useCallback } from "react";
import { useNavigate } from "react-router-dom";
import axios from "axios";
import api, { logout, getBaseUrl } from "../api";
import { EP } from "../endpoints";

// ─── helpers ──────────────────────────────────────────────────────────────────

function decodeJWT(token) {
  try { return JSON.parse(atob(token.split(".")[1])); } catch { return {}; }
}
function initials(name) {
  if (!name) return "??";
  const p = name.trim().split(" ");
  return p.length >= 2
    ? p[0][0].toUpperCase() + p[p.length - 1][0].toUpperCase()
    : name.slice(0, 2).toUpperCase();
}
function fmtDate(d) {
  if (!d) return "";
  try { return new Date(d).toLocaleDateString("en-NG", { day: "numeric", month: "short", year: "numeric" }); }
  catch { return String(d); }
}
function esc(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}
function slugify(str) {
  return str.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
}
function statusPill(s) {
  if (!s) return "pill-gray";
  const u = s.toUpperCase();
  if (["ACTIVE", "LIVE", "APPROVED", "COMPLETED"].includes(u)) return "pill-green";
  if (["PENDING", "CONFIGURING", "DRAFT"].includes(u)) return "pill-amber";
  if (["SUSPENDED", "INACTIVE", "DEACTIVATED", "REJECTED"].includes(u)) return "pill-red";
  if (["LOCKED"].includes(u)) return "pill-blue";
  return "pill-gray";
}
function flattenErrors(data) {
  if (!data || typeof data !== "object") return String(data || "Unknown error");
  if (data.detail) return String(data.detail);
  const msgs = [];
  const walk = (obj, prefix = "") => {
    Object.entries(obj).forEach(([k, v]) => {
      if (Array.isArray(v)) v.forEach((m) => msgs.push(prefix ? `${prefix}.${k}: ${m}` : `${k}: ${m}`));
      else if (typeof v === "object" && v) walk(v, prefix ? `${prefix}.${k}` : k);
      else msgs.push(prefix ? `${prefix}.${k}: ${v}` : `${k}: ${v}`);
    });
  };
  walk(data);
  return msgs.slice(0, 4).join(" · ") || "Unknown error";
}
function highlight(json) {
  if (typeof json !== "string") return String(json);
  return json.replace(
    /("(\\u[a-zA-Z0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d*)?(?:[eE][+\-]?\d+)?)/g,
    (m) => {
      let c = "jn";
      if (/^"/.test(m)) { c = /:$/.test(m) ? "jk" : "js"; }
      else if (/true|false/.test(m)) c = "jb";
      else if (/null/.test(m)) c = "jnull";
      return `<span class="${c}">${m}</span>`;
    }
  );
}
function auditIcon(action) {
  const a = (action || "").toLowerCase();
  if (a.includes("login") || a.includes("auth")) return { i: "🔐", bg: "#F0EEFF" };
  if (a.includes("create") || a.includes("enrol")) return { i: "✚", bg: "#ECFDF5" };
  if (a.includes("delete") || a.includes("deactivat")) return { i: "✕", bg: "#FEF2F2" };
  if (a.includes("update") || a.includes("edit")) return { i: "✎", bg: "#FFFBEB" };
  if (a.includes("role") || a.includes("rbac")) return { i: "🛡", bg: "#F0EEFF" };
  return { i: "○", bg: "#F7F6FA" };
}

// ─── ResultCard ───────────────────────────────────────────────────────────────

function ResultCard({ data, type, extra }) {
  if (!data || typeof data !== "object") return (
    <div className="notice error">
      <div className="notice-icon">!</div>
      <div className="notice-body"><h4>No data</h4><p>The API returned an empty response.</p></div>
    </div>
  );
  const skip = ["password", "access", "refresh", "token"];
  let name = "", sub = "", badge = "";
  if (type === "school") { name = data.name || "School"; sub = data.slug || ""; badge = data.status || "CREATED"; }
  else if (type === "user") { name = data.full_name || `${data.first_name || ""} ${data.last_name || ""}`.trim() || data.email || "User"; sub = data.email || ""; badge = data.status || "PENDING"; }
  else if (type === "role") { name = data.name || "Role"; sub = `${data.permissions_count ?? data.permission_keys?.length ?? 0} permissions`; badge = data.scope || "—"; }
  else { name = data.name || data.title || "Record"; sub = data.id || ""; badge = data.status || ""; }
  const fields = Object.entries(data).filter(([k, v]) => !skip.includes(k) && typeof v !== "object" && v !== null && v !== undefined && v !== "");
  return (
    <div className="result-card">
      <div className="rc-hero">
        <div className="rc-av">{initials(name) || "?"}</div>
        <div className="rc-hero-info">
          <div className="rc-name">{name}</div>
          <div className="rc-sub">{sub}</div>
          {badge && <div style={{ marginTop: 8 }}><span className={`pill ${statusPill(badge)}`}>{badge}</span></div>}
        </div>
      </div>
      <div className="rc-body">
        {fields.slice(0, 10).map(([k, v]) => (
          <div className="rc-row" key={k}>
            <span className="rc-key">{k.replace(/_/g, " ")}</span>
            <span className="rc-val">{String(v)}</span>
          </div>
        ))}
        {extra && <div className="rc-actions">{extra}</div>}
      </div>
    </div>
  );
}

// ─── Notice ───────────────────────────────────────────────────────────────────

function Notice({ type, title, msg, onClear }) {
  if (!msg) return null;
  return (
    <div className={`notice ${type}`} style={{ position: "relative" }}>
      <div className="notice-icon">{type === "success" ? "✓" : "✕"}</div>
      <div className="notice-body"><h4>{title}</h4><p>{msg}</p></div>
      {onClear && (
        <button onClick={onClear} style={{ position: "absolute", top: 10, right: 10, background: "none", border: "none", cursor: "pointer", color: "inherit", fontSize: 14 }}>✕</button>
      )}
    </div>
  );
}

// ─── SettingsModal ────────────────────────────────────────────────────────────

function SettingsModal({ open, onClose, onSave }) {
  const current = getBaseUrl();
  const [url, setUrl] = useState(current);
  const [selected, setSelected] = useState(current);

  const presets = [
    { name: "Staging", url: "https://api.codexng.com", sub: "api.codexng.com" },
    { name: "Local", url: "http://127.0.0.1:8000", sub: "localhost:8000" },
  ];

  function pick(u) { setSelected(u); setUrl(u); }
  function save() {
    if (!url.trim()) return;
    localStorage.setItem("vs_base_url", url.trim());
    onSave();
  }

  return (
    <div id="settings-modal" className={open ? "open" : ""} onClick={onClose}>
      <div className="sm-card" onClick={(e) => e.stopPropagation()}>
        <h3>API Settings</h3>
        <p>Configure which backend environment this console connects to</p>
        <div className="env-tabs">
          {presets.map((p) => (
            <div key={p.url} className={`env-tab${selected === p.url ? " on" : ""}`} onClick={() => pick(p.url)}>
              <div className="env-tab-name">{p.name}</div>
              <div className="env-tab-url">{p.sub}</div>
            </div>
          ))}
        </div>
        <div className="form-row">
          <label>Base URL (custom)</label>
          <input type="text" value={url} onChange={(e) => { setUrl(e.target.value); setSelected(""); }} placeholder="https://…" />
        </div>
        <div style={{ display: "flex", gap: 10, justifyContent: "flex-end", paddingTop: 14, borderTop: "1px solid var(--line)" }}>
          <button className="btn btn-ghost" onClick={onClose}>Cancel</button>
          <button className="btn btn-primary" onClick={save}>Save settings</button>
        </div>
      </div>
    </div>
  );
}

// ─── SVG chart primitives ─────────────────────────────────────────────────────

function Sparkline({ data, color = "var(--v)", width = 100, height = 36 }) {
  if (!data || data.length < 2) return null;
  const min = Math.min(...data);
  const max = Math.max(...data);
  const rng = Math.max(max - min, 1);
  const W = width, H = height;
  const pts = data.map((v, i) => [
    (i / (data.length - 1)) * W,
    H - 4 - ((v - min) / rng) * (H - 8),
  ]);
  const line = pts.map((p, i) => `${i === 0 ? "M" : "L"}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(" ");
  const area = `${line} L${W},${H} L0,${H} Z`;
  const uid = `spk${color.replace(/[^a-z0-9]/gi, "")}`;
  return (
    <svg width={W} height={H} style={{ display: "block", overflow: "visible" }}>
      <defs>
        <linearGradient id={uid} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={color} stopOpacity="0.25" />
          <stop offset="100%" stopColor={color} stopOpacity="0" />
        </linearGradient>
      </defs>
      <path d={area} fill={`url(#${uid})`} />
      <path d={line} fill="none" stroke={color} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
      <circle cx={pts[pts.length - 1][0]} cy={pts[pts.length - 1][1]} r="3" fill={color} />
    </svg>
  );
}

function AreaChart({ series = [], labels = [], height = 200 }) {
  const VW = 620, VH = height;
  const pad = { t: 8, r: 12, b: 34, l: 40 };
  const cW = VW - pad.l - pad.r;
  const cH = VH - pad.t - pad.b;
  const n = labels.length;
  if (!n || !series.length) return null;
  const allVals = series.flatMap((s) => s.data);
  const vMax = Math.ceil(Math.max(...allVals) * 1.2) || 10;
  const xAt = (i) => pad.l + (i / Math.max(n - 1, 1)) * cW;
  const yAt = (v) => pad.t + cH - (v / vMax) * cH;
  const gridVals = [0, 0.25, 0.5, 0.75, 1].map((f) => Math.round(vMax * f));
  const step = n <= 7 ? 1 : n <= 30 ? 4 : 12;
  return (
    <svg viewBox={`0 0 ${VW} ${VH}`} style={{ width: "100%", height, display: "block" }}>
      <defs>
        {series.map((s) => (
          <linearGradient key={s.key} id={`ag${s.key}`} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={s.color} stopOpacity="0.2" />
            <stop offset="100%" stopColor={s.color} stopOpacity="0.02" />
          </linearGradient>
        ))}
      </defs>
      {gridVals.map((v) => {
        const y = yAt(v);
        return (
          <g key={v}>
            <line x1={pad.l} y1={y} x2={VW - pad.r} y2={y} stroke="var(--line)" strokeWidth="1" />
            <text x={pad.l - 6} y={y + 4} fontSize="9.5" fill="var(--ink3)" textAnchor="end" fontFamily="DM Mono,monospace">{v}</text>
          </g>
        );
      })}
      {labels.map((lb, i) => {
        if (i % step !== 0 && i !== n - 1) return null;
        return (
          <text key={i} x={xAt(i)} y={VH - 6} fontSize="9.5" fill="var(--ink3)" textAnchor="middle" fontFamily="DM Mono,monospace">{lb}</text>
        );
      })}
      {series.map((s) => {
        const pts = s.data.map((v, i) => [xAt(i), yAt(v)]);
        const linePath = pts.map((p, i) => `${i === 0 ? "M" : "L"}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(" ");
        const areaPath = `${linePath} L${xAt(s.data.length - 1)},${pad.t + cH} L${xAt(0)},${pad.t + cH} Z`;
        return (
          <g key={s.key}>
            <path d={areaPath} fill={`url(#ag${s.key})`} />
            <path d={linePath} fill="none" stroke={s.color} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
            <circle cx={xAt(s.data.length - 1)} cy={yAt(s.data[s.data.length - 1])} r="3.5" fill={s.color} />
          </g>
        );
      })}
    </svg>
  );
}

function DonutChart({ slices = [], size = 136 }) {
  const R = size / 2 - 3;
  const r = R * 0.62;
  const cx = size / 2, cy = size / 2;
  const total = slices.reduce((s, sl) => s + sl.value, 0) || 1;
  let angle = -Math.PI / 2;
  const gap = 0.025;
  const paths = slices.map((sl) => {
    const sweep = (sl.value / total) * Math.PI * 2;
    const a0 = angle + gap / 2;
    const a1 = angle + sweep - gap / 2;
    angle += sweep;
    const large = sweep > Math.PI ? 1 : 0;
    const d = [
      `M${(cx + R * Math.cos(a0)).toFixed(2)} ${(cy + R * Math.sin(a0)).toFixed(2)}`,
      `A${R} ${R} 0 ${large} 1 ${(cx + R * Math.cos(a1)).toFixed(2)} ${(cy + R * Math.sin(a1)).toFixed(2)}`,
      `L${(cx + r * Math.cos(a1)).toFixed(2)} ${(cy + r * Math.sin(a1)).toFixed(2)}`,
      `A${r} ${r} 0 ${large} 0 ${(cx + r * Math.cos(a0)).toFixed(2)} ${(cy + r * Math.sin(a0)).toFixed(2)}Z`,
    ].join(" ");
    return { ...sl, d };
  });
  return (
    <svg width={size} height={size} style={{ display: "block", flexShrink: 0 }}>
      {paths.map((p, i) => <path key={i} d={p.d} fill={p.color} />)}
      <circle cx={cx} cy={cy} r={r - 1} fill="var(--card)" />
    </svg>
  );
}

// ─── HomePage ─────────────────────────────────────────────────────────────────

function pseudo(seed, i) {
  return Math.abs(Math.sin(seed * 9301 + i * 49297 + 233720) * 1e5) % 1;
}
function genSparkData(seed, n = 8) {
  const base = Math.max(seed || 5, 2);
  return Array.from({ length: n }, (_, i) => Math.max(1, Math.round(base * (0.65 + pseudo(base, i) * 0.7))));
}
function genSeries(seed, n, base) {
  return Array.from({ length: n }, (_, i) =>
    Math.round(Math.max(1, base * (0.55 + (i / n) * 0.55 + pseudo(seed, i) * 0.35)))
  );
}

function HomePage({ call, showToast, addActivity, onSubPageNav, profile, activity }) {
  const [period, setPeriod] = useState("30d");
  const [counts, setCounts] = useState({ schools: null, users: null, roles: null, sessions: null, invitations: null });
  const [refreshKey, setRefreshKey] = useState(0);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      const [sr, ur, rr, sesr, ir] = await Promise.allSettled([
        call("GET", EP.SCHOOLS),
        call("GET", EP.USERS),
        call("GET", EP.PLATFORM_ROLES),
        call("GET", EP.SESSIONS),
        call("GET", EP.INVITATIONS),
      ]);
      if (cancelled) return;
      const getCount = (r) => {
        if (r.status !== "fulfilled" || !r.value.ok) return null;
        const v = r.value;
        return v.pagination?.totalItems ?? (Array.isArray(v.data) ? v.data.length : null);
      };
      setCounts({ schools: getCount(sr), users: getCount(ur), roles: getCount(rr), sessions: getCount(sesr), invitations: getCount(ir) });
    }
    load();
    return () => { cancelled = true; };
  }, [call, refreshKey]); // eslint-disable-line react-hooks/exhaustive-deps

  const periodDays = period === "7d" ? 7 : period === "30d" ? 30 : 90;
  const labels = Array.from({ length: periodDays }, (_, i) => {
    const d = new Date();
    d.setDate(d.getDate() - (periodDays - 1 - i));
    return d.toLocaleDateString("en", { month: "short", day: "numeric" });
  });

  const usersBase = Math.max(counts.users ?? 20, 4);
  const schoolsBase = Math.max(counts.schools ?? 5, 1);

  const chartSeries = [
    { key: "logins",  label: "Login events", color: "var(--v)",     data: genSeries(7,  periodDays, Math.round(usersBase * 0.45)) },
    { key: "newusers",label: "New users",     color: "var(--green)", data: genSeries(13, periodDays, Math.round(usersBase * 0.09 + 1)) },
    { key: "schools", label: "Schools",       color: "#F59E0B",      data: genSeries(31, periodDays, Math.max(Math.round(schoolsBase * 0.12 + 1), 1)) },
  ];

  const STAT_CARDS = [
    { key: "schools",     label: "Schools",         icon: "🏫", color: "var(--v)",     sub: "active tenants",   trendSeed: 1 },
    { key: "users",       label: "Total Users",      icon: "👥", color: "var(--green)", sub: "across all schools",trendSeed: 2 },
    { key: "roles",       label: "Role Templates",   icon: "🛡", color: "#F59E0B",      sub: "platform-wide",    trendSeed: 3 },
    { key: "sessions",    label: "Active Sessions",  icon: "🔑", color: "#10B981",      sub: "live right now",   trendSeed: 4 },
    { key: "invitations", label: "Invitations",      icon: "✉",  color: "#6366F1",      sub: "pending & sent",   trendSeed: 5 },
  ];

  const firstName = profile?.first_name || "Admin";
  const hour = new Date().getHours();
  const greeting = hour < 12 ? "Good morning" : hour < 17 ? "Good afternoon" : "Good evening";
  const todayStr = new Date().toLocaleDateString("en-NG", { weekday: "long", day: "numeric", month: "long", year: "numeric" });

  const donutSlices = STAT_CARDS.map((s) => ({ label: s.label, value: counts[s.key] || 1, color: s.color }));
  const donutTotal = donutSlices.reduce((s, d) => s + d.value, 0) || 1;

  return (
    <div className="page active" id="page-home">
      {/* ── Header ── */}
      <div className="page-hd">
        <div className="page-hd-left">
          <h1>{greeting}, <em>{firstName}</em></h1>
          <p>{todayStr}</p>
        </div>
        <div className="page-hd-right" style={{ alignItems: "center", gap: 8 }}>
          <div className="period-tabs">
            {[["7d", "7 days"], ["30d", "30 days"], ["90d", "90 days"]].map(([k, label]) => (
              <button key={k} className={`period-tab${period === k ? " on" : ""}`} onClick={() => setPeriod(k)}>{label}</button>
            ))}
          </div>
          <button className="btn btn-ghost btn-sm" onClick={() => setRefreshKey((n) => n + 1)}>↻ Refresh</button>
        </div>
      </div>

      {/* ── Stat cards ── */}
      <div className="dash-cards">
        {STAT_CARDS.map((s) => {
          const val = counts[s.key];
          const trendPct = val != null ? (pseudo(val + s.trendSeed, s.trendSeed) * 14 + 0.5).toFixed(1) : null;
          const trendUp = val != null ? pseudo(val, s.trendSeed + 7) > 0.3 : true;
          return (
            <div className="dash-card" key={s.key}>
              <div className="dc-top">
                <span className="dc-label">{s.label}</span>
                <span className="dc-icon">{s.icon}</span>
              </div>
              <div className="dc-val">{val ?? "—"}</div>
              <div className="dc-bottom">
                <span className="dc-sub">{s.sub}</span>
                {trendPct && (
                  <span className={`dc-trend ${trendUp ? "up" : "dn"}`}>
                    {trendUp ? "↑" : "↓"} {trendPct}%
                  </span>
                )}
              </div>
              <Sparkline data={genSparkData(val ?? 5, 8)} color={s.color} width={100} height={34} />
            </div>
          );
        })}
      </div>

      {/* ── Activity chart ── */}
      <div className="card" style={{ marginBottom: 20 }}>
        <div className="card-head">
          <div>
            <h3>Platform Activity</h3>
            <p>Simulated trend over {period === "7d" ? "7 days" : period === "30d" ? "30 days" : "90 days"}</p>
          </div>
          <div style={{ display: "flex", gap: 16 }}>
            {chartSeries.map((s) => (
              <span key={s.key} style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12, color: "var(--ink2)" }}>
                <span style={{ width: 9, height: 9, borderRadius: "50%", background: s.color, display: "inline-block", flexShrink: 0 }} />
                {s.label}
              </span>
            ))}
          </div>
        </div>
        <div style={{ padding: "4px 16px 14px" }}>
          <AreaChart series={chartSeries} labels={labels} height={200} />
        </div>
      </div>

      {/* ── Bottom row ── */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 20 }}>
        {/* Donut breakdown */}
        <div className="card">
          <div className="card-head"><div><h3>Platform breakdown</h3><p>Entities by category</p></div></div>
          <div className="card-body" style={{ display: "flex", alignItems: "center", gap: 22 }}>
            <DonutChart slices={donutSlices} size={136} />
            <div style={{ flex: 1, minWidth: 0 }}>
              {donutSlices.map((sl, i) => {
                const pct = Math.round((sl.value / donutTotal) * 100);
                return (
                  <div key={i} style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 10 }}>
                    <span style={{ width: 9, height: 9, borderRadius: 2, background: sl.color, flexShrink: 0 }} />
                    <span style={{ fontSize: 12, color: "var(--ink2)", flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{sl.label}</span>
                    <span style={{ fontSize: 11, color: "var(--ink3)", fontFamily: "var(--fm)", flexShrink: 0 }}>{pct}%</span>
                  </div>
                );
              })}
            </div>
          </div>
        </div>

        {/* Quick actions */}
        <div className="card">
          <div className="card-head"><div><h3>Quick actions</h3><p>Common platform tasks</p></div></div>
          <div className="card-body" style={{ display: "flex", flexDirection: "column", gap: 7 }}>
            {[
              { icon: "🏫", label: "Create school",        page: "schools",  sub: "create" },
              { icon: "🌿", label: "Create branch",        page: "branches", sub: "create" },
              { icon: "✉",  label: "Invite user",          page: "users",    sub: "invite" },
              { icon: "🛡", label: "Create role template", page: "rbac",     sub: "create" },
              { icon: "📋", label: "View audit logs",      page: "audit",    sub: null     },
            ].map((a) => (
              <button
                key={a.label}
                className="btn btn-secondary"
                style={{ justifyContent: "flex-start", gap: 10, width: "100%", height: 36, fontSize: 13 }}
                onClick={() => onSubPageNav(a.page, a.sub)}
              >
                <span>{a.icon}</span>{a.label}
              </button>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

// ─── SchoolsPage — Nigerian test data profiles ────────────────────────────────

const _NG_EXPIRY = new Date(Date.now() + 365 * 24 * 3600 * 1000).toISOString().slice(0, 10);
const NG_TEST_SCHOOLS = [
  {
    label: "Greenfield Academy Ikeja",
    form: { name: "Greenfield Academy Ikeja", slug: "greenfield-academy-ikeja", type: "SCHOOL", country: "NG", timezone: "Africa/Lagos", contact_email: "info@greenfieldacademy.edu.ng", address: "14 Allen Avenue, Ikeja, Lagos State", ownership_type: "PRIVATE", website: "https://greenfieldacademy.edu.ng", motto: "Excellence Through Knowledge", term_structure: "THREE_TERMS", currency: "NGN", registration_id: "CAC/IT/2019/0042" },
    bf:   { name: "Main Campus", address: "14 Allen Avenue, Ikeja, Lagos", contact_email: "main@greenfieldacademy.edu.ng", branch_type: "Primary", country: "NG", state: "Lagos", is_main_branch: true, admin_first_name: "Emeka", admin_last_name: "Nwosu", admin_email: "emeka.nwosu@greenfieldacademy.edu.ng", admin_phone: "+2348012345678", admin_role: "Head Teacher" },
    af:   { first_name: "Adaeze", last_name: "Okonkwo", email: "adaeze.okonkwo@greenfieldacademy.edu.ng", phone_number: "+2348034567890", admin_role: "Principal" },
    pf:   { package_plan: "", student_capacity: "800", teacher_capacity: "60", admin_capacity: "12", subscription_expires: _NG_EXPIRY, enabled_modules: [] },
  },
  {
    label: "Heritage International School Abuja",
    form: { name: "Heritage International School Abuja", slug: "heritage-international-school-abuja", type: "SCHOOL", country: "NG", timezone: "Africa/Lagos", contact_email: "info@heritageschool.edu.ng", address: "Plot 45 Maitama District, Abuja FCT", ownership_type: "FAITH_BASED", website: "https://heritageschool.edu.ng", motto: "Faith, Excellence, Service", term_structure: "THREE_TERMS", currency: "NGN", registration_id: "FCT/EDU/2017/0098" },
    bf:   { name: "Maitama Campus", address: "Plot 45 Maitama District, Abuja FCT", contact_email: "maitama@heritageschool.edu.ng", branch_type: "Primary", country: "NG", state: "FCT", is_main_branch: true, admin_first_name: "Fatima", admin_last_name: "Hassan", admin_email: "fatima.hassan@heritageschool.edu.ng", admin_phone: "+2348098765432", admin_role: "Head of School" },
    af:   { first_name: "Yusuf", last_name: "Abdullahi", email: "yusuf.abdullahi@heritageschool.edu.ng", phone_number: "+2348023456789", admin_role: "Director" },
    pf:   { package_plan: "", student_capacity: "1200", teacher_capacity: "80", admin_capacity: "15", subscription_expires: _NG_EXPIRY, enabled_modules: [] },
  },
  {
    label: "Covenant Crown College Ibadan",
    form: { name: "Covenant Crown College Ibadan", slug: "covenant-crown-college-ibadan", type: "COLLEGE", country: "NG", timezone: "Africa/Lagos", contact_email: "info@covenantcrown.edu.ng", address: "Bodija Estate, Ibadan, Oyo State", ownership_type: "FAITH_BASED", website: "https://covenantcrown.edu.ng", motto: "Raising Pillars for the Nation", term_structure: "THREE_TERMS", currency: "NGN", registration_id: "OYO/MEB/2015/0203" },
    bf:   { name: "Bodija Campus", address: "Bodija Estate, Ibadan, Oyo State", contact_email: "bodija@covenantcrown.edu.ng", branch_type: "Primary", country: "NG", state: "Oyo", is_main_branch: true, admin_first_name: "Grace", admin_last_name: "Oluwaseun", admin_email: "grace.oluwaseun@covenantcrown.edu.ng", admin_phone: "+2348056789012", admin_role: "Head of Academics" },
    af:   { first_name: "Samuel", last_name: "Adeyemi", email: "samuel.adeyemi@covenantcrown.edu.ng", phone_number: "+2348045678901", admin_role: "Principal" },
    pf:   { package_plan: "", student_capacity: "600", teacher_capacity: "45", admin_capacity: "10", subscription_expires: _NG_EXPIRY, enabled_modules: [] },
  },
  {
    label: "Lagos City Polytechnic Surulere",
    form: { name: "Lagos City Polytechnic Surulere", slug: "lagos-city-polytechnic-surulere", type: "POLYTECHNIC", country: "NG", timezone: "Africa/Lagos", contact_email: "info@lagoscitypoly.edu.ng", address: "12 Bode Thomas Street, Surulere, Lagos", ownership_type: "PUBLIC", website: "https://lagoscitypoly.edu.ng", motto: "Technology for National Development", term_structure: "TWO_SEMESTERS", currency: "NGN", registration_id: "NBTE/POLY/2012/0067" },
    bf:   { name: "Surulere Main Campus", address: "12 Bode Thomas Street, Surulere, Lagos", contact_email: "main@lagoscitypoly.edu.ng", branch_type: "Main", country: "NG", state: "Lagos", is_main_branch: true, admin_first_name: "Ngozi", admin_last_name: "Eze", admin_email: "ngozi.eze@lagoscitypoly.edu.ng", admin_phone: "+2348067890123", admin_role: "Campus Director" },
    af:   { first_name: "Chukwuemeka", last_name: "Obi", email: "chukwuemeka.obi@lagoscitypoly.edu.ng", phone_number: "+2348078901234", admin_role: "Registrar" },
    pf:   { package_plan: "", student_capacity: "2000", teacher_capacity: "120", admin_capacity: "20", subscription_expires: _NG_EXPIRY, enabled_modules: [] },
  },
  {
    label: "Sunrise Nursery & Primary School Port Harcourt",
    form: { name: "Sunrise Nursery and Primary School Port Harcourt", slug: "sunrise-nursery-primary-ph", type: "SCHOOL", country: "NG", timezone: "Africa/Lagos", contact_email: "info@sunriseprimaryph.edu.ng", address: "24 GRA Phase 2, Port Harcourt, Rivers State", ownership_type: "PRIVATE", website: "https://sunriseprimaryph.edu.ng", motto: "Nurturing Tomorrow's Leaders Today", term_structure: "THREE_TERMS", currency: "NGN", registration_id: "RVS/EDU/2020/0114" },
    bf:   { name: "GRA Branch", address: "24 GRA Phase 2, Port Harcourt, Rivers State", contact_email: "gra@sunriseprimaryph.edu.ng", branch_type: "Primary", country: "NG", state: "Rivers", is_main_branch: true, admin_first_name: "Chisom", admin_last_name: "Amadi", admin_email: "chisom.amadi@sunriseprimaryph.edu.ng", admin_phone: "+2348089012345", admin_role: "Head Teacher" },
    af:   { first_name: "Blessing", last_name: "Nwofor", email: "blessing.nwofor@sunriseprimaryph.edu.ng", phone_number: "+2348090123456", admin_role: "Proprietress" },
    pf:   { package_plan: "", student_capacity: "300", teacher_capacity: "25", admin_capacity: "5", subscription_expires: _NG_EXPIRY, enabled_modules: [] },
  },
];

// ─── SchoolsPage ─────────────────────────────────────────────────────────

function WizStepBar({ step, total, labels }) {
  return (
    <div style={{ display: "flex", alignItems: "center", marginBottom: 28, maxWidth: 640 }}>
      {labels.map((label, i) => {
        const n = i + 1;
        const done = n < step;
        const active = n === step;
        const items = [
          <div key={`s${i}`} style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 5, flexShrink: 0 }}>
            <div style={{ width: 30, height: 30, borderRadius: "50%", background: done ? "#059669" : active ? "var(--v)" : "var(--line2)", color: (done || active) ? "#fff" : "var(--ink3)", display: "flex", alignItems: "center", justifyContent: "center", fontSize: 12, fontWeight: 700 }}>
              {done ? "✓" : n}
            </div>
            <div style={{ fontSize: 11, whiteSpace: "nowrap", color: active ? "var(--v)" : done ? "#059669" : "var(--ink3)", fontWeight: active ? 600 : 400 }}>{label}</div>
          </div>,
        ];
        if (i < labels.length - 1) items.push(
          <div key={`l${i}`} style={{ flex: 1, height: 2, background: done ? "#059669" : "var(--line2)", margin: "0 6px", marginBottom: 18 }} />
        );
        return items;
      })}
    </div>
  );
}

function SchoolsPage({ call, showToast, openDetail, addActivity, subPage, onSubPage }) {
  const [rows, setRows]       = useState([]);
  const [loading, setLoading] = useState(false);
  const [q, setQ]             = useState("");

  // Wizard
  const [wizStep, setWizStep]         = useState(1);
  const [schoolResult, setSchoolResult] = useState(null);
  const [notice, setNotice]           = useState(null);
  const [saving, setSaving]           = useState(false);
  const [packages, setPackages]       = useState([]);
  const [modules, setModules]         = useState([]);
  const [prefillIdx, setPrefillIdx]   = useState(0);

  const INIT_FORM = { name: "", slug: "", type: "SCHOOL", country: "NG", timezone: "Africa/Lagos", contact_email: "", address: "", ownership_type: "", website: "", motto: "", term_structure: "", currency: "NGN", registration_id: "" };
  const INIT_BF   = { name: "", address: "", contact_email: "", branch_type: "Primary", country: "NG", state: "", is_main_branch: true, admin_first_name: "", admin_last_name: "", admin_email: "", admin_phone: "", admin_role: "Head Teacher" };
  const INIT_AF   = { first_name: "", last_name: "", email: "", phone_number: "", admin_role: "Principal" };
  const INIT_PF   = { package_plan: "", student_capacity: "", teacher_capacity: "", admin_capacity: "", subscription_expires: "", enabled_modules: [] };

  const [form, setForm] = useState(INIT_FORM);
  const [bf,   setBf]   = useState(INIT_BF);
  const [af,   setAf]   = useState(INIT_AF);
  const [pf,   setPf]   = useState(INIT_PF);

  const load = useCallback(async () => {
    setLoading(true);
    const r = await call("GET", EP.SCHOOLS);
    setLoading(false);
    if (r.ok) setRows(r.data || []);
  }, [call]);

  useEffect(() => {
    if (subPage === "list") load();
    if (subPage === "create") {
      setWizStep(1); setSchoolResult(null); setNotice(null);
      setForm(INIT_FORM); setBf(INIT_BF); setAf(INIT_AF); setPf(INIT_PF);
      setPackages([]); setModules([]);
    }
  }, [subPage]); // eslint-disable-line react-hooks/exhaustive-deps

  // Load package plans + modules when reaching step 4
  useEffect(() => {
    if (wizStep !== 4 || packages.length > 0) return;
    call("GET", EP.PACKAGE_PLANS).then((r) => { if (r.ok) setPackages(Array.isArray(r.data) ? r.data : []); });
    call("GET", EP.MODULES).then((r)        => { if (r.ok) setModules(Array.isArray(r.data) ? r.data : []); });
  }, [wizStep]); // eslint-disable-line react-hooks/exhaustive-deps

  function setF(k) { return (e) => setForm((f) => ({ ...f, [k]: e.target.value })); }

  // ── Test data prefill ────────────────────────────────────────────────────────
  function fillTestData() {
    const d = NG_TEST_SCHOOLS[prefillIdx % NG_TEST_SCHOOLS.length];
    setForm(d.form);
    setBf(d.bf);
    setAf(d.af);
    setPf((p) => ({ ...p, ...d.pf }));
    setPrefillIdx((i) => (i + 1) % NG_TEST_SCHOOLS.length);
    setNotice(null);
    showToast(`Prefilled: ${d.label}`, "ok");
  }

  // ── Step navigation (steps 1-3 are local only, no API calls) ────────────────
  function step1Next() {
    setNotice(null);
    if (!form.name.trim()) { setNotice({ type: "error", title: "Required", msg: "School name is required." }); return; }
    if (!form.ownership_type) { setNotice({ type: "error", title: "Required", msg: "Ownership type is required." }); return; }
    setForm((f) => ({ ...f, slug: f.slug || slugify(f.name) }));
    setWizStep(2);
  }

  function step2Next() {
    setNotice(null);
    if (!bf.name.trim()) { setNotice({ type: "error", title: "Required", msg: "Branch name is required — a school must have at least one branch." }); return; }
    if (!bf.branch_type.trim()) { setNotice({ type: "error", title: "Required", msg: "Branch type is required." }); return; }
    setWizStep(3);
  }

  function step3Next() {
    setNotice(null);
    if (af.email && !af.first_name.trim()) { setNotice({ type: "error", title: "Required", msg: "Provide the admin's first name when an email is entered." }); return; }
    setWizStep(4);
  }

  // ── Final submission — single POST with full payload ─────────────────────────
  async function step4Submit() {
    setNotice(null);
    if (!pf.package_plan) { setNotice({ type: "error", title: "Required", msg: "Select a package plan." }); return; }
    if (!pf.student_capacity || !pf.teacher_capacity || !pf.admin_capacity) {
      setNotice({ type: "error", title: "Required", msg: "Student, teacher and admin capacities are all required." }); return;
    }

    // Branch object (backend field names)
    const branchAdminName = [bf.admin_first_name, bf.admin_last_name].filter(Boolean).join(" ");
    const branchObj = {
      name:    bf.name,
      _type:   bf.branch_type,
      is_main: bf.is_main_branch,
      ...(bf.address        ? { address: bf.address }                                         : {}),
      ...(bf.contact_email  ? { email: bf.contact_email }                                     : {}),
      ...(bf.country        ? { country: bf.country === "NG" ? "Nigeria" : bf.country }       : {}),
      ...(bf.state          ? { state: bf.state }                                              : {}),
    };
    if (branchAdminName && bf.admin_email) {
      branchObj.primary_admin_data = {
        full_name:   branchAdminName,
        email:       bf.admin_email,
        ...(bf.admin_phone ? { phone:       bf.admin_phone } : {}),
        ...(bf.admin_role  ? { branch_role: bf.admin_role  } : {}),
      };
    }

    // School admin
    const adminName = [af.first_name, af.last_name].filter(Boolean).join(" ");
    const primaryAdminData = (adminName && af.email) ? {
      full_name:   adminName,
      email:       af.email,
      ...(af.phone_number ? { phone:       af.phone_number } : {}),
      ...(af.admin_role   ? { school_role: af.admin_role   } : {}),
    } : undefined;

    // Package setup
    const packageData = {
      package_plan:      pf.package_plan,
      student_capacity:  parseInt(pf.student_capacity),
      teacher_capacity:  parseInt(pf.teacher_capacity),
      admin_capacity:    parseInt(pf.admin_capacity),
      ...(pf.subscription_expires          ? { subscription_expires_at: pf.subscription_expires }   : {}),
      ...(pf.enabled_modules?.length       ? { enabled_modules: pf.enabled_modules }                : {}),
    };

    // Full payload
    const payload = {
      name:     form.name,
      slug:     form.slug || slugify(form.name),
      currency: form.currency || "NGN",
      ...(form.ownership_type   ? { ownership_type:  form.ownership_type  } : {}),
      ...(form.address          ? { address:          form.address          } : {}),
      ...(form.website          ? { website:          form.website          } : {}),
      ...(form.motto            ? { motto:            form.motto            } : {}),
      ...(form.term_structure   ? { term_structure:   form.term_structure   } : {}),
      ...(form.registration_id  ? { registration_id:  form.registration_id  } : {}),
      branches:           [branchObj],
      package_setup_data: packageData,
      ...(primaryAdminData ? { primary_admin_data: primaryAdminData } : {}),
    };

    setSaving(true);
    const r = await call("POST", EP.SCHOOLS_CREATE, payload);
    setSaving(false);

    if (r.ok || r.status === 201) {
      setSchoolResult(r.data);
      addActivity("🏫", `School created: ${form.name}`);
      showToast(`${form.name} onboarded successfully`, "ok");
      onSubPage("result");
    } else {
      setNotice({ type: "error", title: "Onboarding failed", msg: flattenErrors(r.data) });
    }
  }

  function openSchDetail(sch) {
    call("GET", EP.SCHOOL(sch.slug || sch.id)).then((r) => {
      if (!r.ok) return;
      openDetail("School details", <ResultCard data={r.data} type="school" />);
    });
  }

  const visible = rows.filter((r) =>
    [r.name, r.slug, r.ownership_type, r.status].join(" ").toLowerCase().includes(q.toLowerCase())
  );
  const statCounts = {
    all:      rows.length,
    active:   rows.filter((r) => (r.status || "").toUpperCase() === "ACTIVE").length,
    pending:  rows.filter((r) => (r.status || "").toUpperCase() === "PENDING").length,
    inactive: rows.filter((r) => ["INACTIVE", "DEACTIVATED", "SUSPENDED"].includes((r.status || "").toUpperCase())).length,
  };

  const WLABELS = ["School Info", "Branch", "Admin", "Package & Submit"];

  // ── CREATE WIZARD ─────────────────────────────────────────────────────────────
  if (subPage === "create") return (
    <div className="page active" id="page-schools-create">
      <div className="page-hd">
        <div className="page-hd-left">
          <button className="btn btn-ghost btn-sm" onClick={() => { onSubPage("list"); load(); }}>← Schools</button>
          <h1>Onboard a <em>new school</em></h1>
          <p>All steps are collected first — the school is created in one shot at the end</p>
        </div>
        <div className="page-hd-right">
          <button className="btn btn-secondary btn-sm" style={{ display: "flex", alignItems: "center", gap: 6 }} onClick={fillTestData}>
            🇳🇬 Prefill test data
          </button>
        </div>
      </div>

      <WizStepBar step={wizStep} total={4} labels={WLABELS} />

      {/* ── Step 1: School info ── */}
      {wizStep === 1 && (
        <div className="card" style={{ maxWidth: 700 }}>
          <div className="card-head"><h3>School information</h3><span style={{ fontSize: 11, color: "var(--ink3)" }}>Step 1 of 4 — no API call yet</span></div>
          <div className="card-body">
            {notice && <Notice {...notice} onClear={() => setNotice(null)} />}
            <div className="form-row">
              <label>School name <span className="required">*</span></label>
              <input type="text" placeholder="e.g. Greenfield Academy Ikeja" value={form.name}
                onChange={(e) => setForm((f) => ({ ...f, name: e.target.value, slug: f.slug || slugify(e.target.value) }))} />
            </div>
            <div className="form-row-2">
              <div className="form-row">
                <label>School address</label>
                <input type="text" placeholder="Full street address" value={form.address} onChange={setF("address")} />
              </div>
              <div className="form-row">
                <label>Contact email</label>
                <input type="email" placeholder="info@school.edu.ng" value={form.contact_email} onChange={setF("contact_email")} />
              </div>
            </div>
            <div className="form-row-2">
              <div className="form-row">
                <label>School type</label>
                <select value={form.type} onChange={setF("type")}>
                  <option value="SCHOOL">School</option>
                  <option value="COLLEGE">College</option>
                  <option value="POLYTECHNIC">Polytechnic</option>
                  <option value="UNIVERSITY">University</option>
                  <option value="TRAINING_CENTER">Training Centre</option>
                  <option value="VOCATIONAL">Vocational</option>
                </select>
              </div>
              <div className="form-row">
                <label>Ownership type <span className="required">*</span></label>
                <select value={form.ownership_type} onChange={setF("ownership_type")}>
                  <option value="">Select…</option>
                  <option value="PRIVATE">Private</option>
                  <option value="PUBLIC">Public / Government</option>
                  <option value="FAITH_BASED">Faith-based</option>
                  <option value="NGO">NGO</option>
                </select>
              </div>
            </div>
            <div className="form-row-2">
              <div className="form-row">
                <label>Term structure</label>
                <select value={form.term_structure} onChange={setF("term_structure")}>
                  <option value="">Select…</option>
                  <option value="THREE_TERMS">Three Terms (Jan–Dec)</option>
                  <option value="TWO_SEMESTERS">Two Semesters</option>
                </select>
              </div>
              <div className="form-row">
                <label>Currency</label>
                <select value={form.currency} onChange={setF("currency")}>
                  <option value="NGN">NGN — Nigerian Naira</option>
                  <option value="GHS">GHS — Ghanaian Cedi</option>
                  <option value="KES">KES — Kenyan Shilling</option>
                  <option value="USD">USD — US Dollar</option>
                </select>
              </div>
            </div>
            <div className="form-row-2">
              <div className="form-row">
                <label>Website</label>
                <input type="url" placeholder="https://school.edu.ng" value={form.website} onChange={setF("website")} />
              </div>
              <div className="form-row">
                <label>Registration ID</label>
                <input type="text" placeholder="e.g. CAC/IT/2019/0042" value={form.registration_id} onChange={setF("registration_id")} />
              </div>
            </div>
            <div className="form-row">
              <label>School motto</label>
              <input type="text" placeholder="e.g. Excellence Through Knowledge" value={form.motto} onChange={setF("motto")} />
            </div>
            <div className="form-row">
              <label>URL slug <span className="required">*</span></label>
              <input type="text" placeholder="greenfield-academy-ikeja" value={form.slug} onChange={setF("slug")} style={{ fontFamily: "var(--fm)" }} />
              <div className="form-hint">Auto-generated from name. Must be globally unique on the platform.</div>
            </div>
            <div style={{ display: "flex", gap: 10, paddingTop: 8 }}>
              <button className="btn btn-primary" onClick={step1Next}>Continue →</button>
              <button className="btn btn-ghost" onClick={() => onSubPage("list")}>Cancel</button>
            </div>
          </div>
        </div>
      )}

      {/* ── Step 2: First branch ── */}
      {wizStep === 2 && (
        <div className="card" style={{ maxWidth: 700 }}>
          <div className="card-head">
            <h3>First branch</h3>
            <span style={{ fontSize: 11, color: "var(--ink3)" }}>Step 2 of 4 — required, a school must have at least one branch</span>
          </div>
          <div className="card-body">
            {notice && <Notice {...notice} onClear={() => setNotice(null)} />}
            <div className="form-row-2">
              <div className="form-row">
                <label>Branch name <span className="required">*</span></label>
                <input type="text" placeholder="e.g. Main Campus" value={bf.name} onChange={(e) => setBf((f) => ({ ...f, name: e.target.value }))} />
              </div>
              <div className="form-row">
                <label>Branch type <span className="required">*</span></label>
                <select value={bf.branch_type} onChange={(e) => setBf((f) => ({ ...f, branch_type: e.target.value }))}>
                  <option value="Primary">Primary</option>
                  <option value="Secondary">Secondary</option>
                  <option value="Main">Main</option>
                  <option value="Satellite">Satellite</option>
                  <option value="Campus">Campus</option>
                  <option value="Annex">Annex</option>
                  <option value="Combined">Combined (Nursery–Secondary)</option>
                </select>
              </div>
            </div>
            <div className="form-row-2">
              <div className="form-row">
                <label>Branch address</label>
                <input type="text" placeholder="Full street address" value={bf.address} onChange={(e) => setBf((f) => ({ ...f, address: e.target.value }))} />
              </div>
              <div className="form-row">
                <label>Branch email</label>
                <input type="email" placeholder="branch@school.edu.ng" value={bf.contact_email} onChange={(e) => setBf((f) => ({ ...f, contact_email: e.target.value }))} />
              </div>
            </div>
            <div className="form-row-2">
              <div className="form-row">
                <label>State / Province</label>
                <input type="text" placeholder="e.g. Lagos" value={bf.state} onChange={(e) => setBf((f) => ({ ...f, state: e.target.value }))} />
              </div>
              <div className="form-row">
                <label>Country</label>
                <select value={bf.country} onChange={(e) => setBf((f) => ({ ...f, country: e.target.value }))}>
                  <option value="NG">Nigeria 🇳🇬</option>
                  <option value="GH">Ghana 🇬🇭</option>
                  <option value="KE">Kenya 🇰🇪</option>
                  <option value="ZA">South Africa 🇿🇦</option>
                </select>
              </div>
            </div>
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "10px 0", borderBottom: "1px solid var(--line)", marginBottom: 14 }}>
              <div>
                <div style={{ fontWeight: 600, fontSize: 13 }}>Set as main branch</div>
                <div style={{ fontSize: 12, color: "var(--ink3)" }}>Every school must have exactly one main branch</div>
              </div>
              <div onClick={() => setBf((f) => ({ ...f, is_main_branch: !f.is_main_branch }))}
                style={{ width: 44, height: 24, borderRadius: 12, background: bf.is_main_branch ? "var(--v)" : "var(--line2)", cursor: "pointer", position: "relative", transition: "background .2s", flexShrink: 0 }}>
                <div style={{ width: 18, height: 18, borderRadius: "50%", background: "#fff", position: "absolute", top: 3, left: bf.is_main_branch ? 23 : 3, transition: "left .2s", boxShadow: "0 1px 3px rgba(0,0,0,.2)" }} />
              </div>
            </div>
            <div className="section-div"><span>Branch admin (optional)</span></div>
            <div className="form-row-2">
              <div className="form-row"><label>First name</label><input type="text" placeholder="e.g. Emeka" value={bf.admin_first_name} onChange={(e) => setBf((f) => ({ ...f, admin_first_name: e.target.value }))} /></div>
              <div className="form-row"><label>Last name</label><input type="text" placeholder="e.g. Nwosu" value={bf.admin_last_name} onChange={(e) => setBf((f) => ({ ...f, admin_last_name: e.target.value }))} /></div>
            </div>
            <div className="form-row-2">
              <div className="form-row"><label>Email</label><input type="email" placeholder="branch.admin@school.edu.ng" value={bf.admin_email} onChange={(e) => setBf((f) => ({ ...f, admin_email: e.target.value }))} /></div>
              <div className="form-row"><label>Phone</label><input type="tel" placeholder="+234 801 234 5678" value={bf.admin_phone} onChange={(e) => setBf((f) => ({ ...f, admin_phone: e.target.value }))} /></div>
            </div>
            <div className="form-row"><label>Role title</label><input type="text" placeholder="e.g. Head Teacher" value={bf.admin_role} onChange={(e) => setBf((f) => ({ ...f, admin_role: e.target.value }))} /></div>
            <div style={{ display: "flex", gap: 10, paddingTop: 8 }}>
              <button className="btn btn-ghost btn-sm" onClick={() => setWizStep(1)}>← Back</button>
              <button className="btn btn-primary" onClick={step2Next}>Continue →</button>
            </div>
          </div>
        </div>
      )}

      {/* ── Step 3: School admin ── */}
      {wizStep === 3 && (
        <div className="card" style={{ maxWidth: 700 }}>
          <div className="card-head"><h3>School administrator</h3><span style={{ fontSize: 11, color: "var(--ink3)" }}>Step 3 of 4 — optional but recommended</span></div>
          <div className="card-body">
            {notice && <Notice {...notice} onClear={() => setNotice(null)} />}
            <p style={{ fontSize: 12, color: "var(--ink3)", marginBottom: 16, lineHeight: 1.6 }}>
              The school admin will receive an invitation to manage the institution. Leave blank to skip — you can add one later.
            </p>
            <div className="form-row-2">
              <div className="form-row"><label>First name</label><input type="text" placeholder="e.g. Adaeze" value={af.first_name} onChange={(e) => setAf((f) => ({ ...f, first_name: e.target.value }))} /></div>
              <div className="form-row"><label>Last name</label><input type="text" placeholder="e.g. Okonkwo" value={af.last_name} onChange={(e) => setAf((f) => ({ ...f, last_name: e.target.value }))} /></div>
            </div>
            <div className="form-row-2">
              <div className="form-row"><label>Email address</label><input type="email" placeholder="admin@school.edu.ng" value={af.email} onChange={(e) => setAf((f) => ({ ...f, email: e.target.value }))} /></div>
              <div className="form-row"><label>Phone number</label><input type="tel" placeholder="+234 803 456 7890" value={af.phone_number} onChange={(e) => setAf((f) => ({ ...f, phone_number: e.target.value }))} /></div>
            </div>
            <div className="form-row"><label>Role title</label><input type="text" placeholder="e.g. Principal" value={af.admin_role} onChange={(e) => setAf((f) => ({ ...f, admin_role: e.target.value }))} /></div>
            <div style={{ display: "flex", gap: 10, paddingTop: 8 }}>
              <button className="btn btn-ghost btn-sm" onClick={() => setWizStep(2)}>← Back</button>
              <button className="btn btn-primary" onClick={step3Next}>Continue →</button>
              <button className="btn btn-ghost" onClick={() => { setAf(INIT_AF); setWizStep(4); }}>Skip admin</button>
            </div>
          </div>
        </div>
      )}

      {/* ── Step 4: Package + final submit ── */}
      {wizStep === 4 && (
        <div className="card" style={{ maxWidth: 700 }}>
          <div className="card-head"><h3>Package setup</h3><span style={{ fontSize: 11, color: "var(--ink3)" }}>Step 4 of 4 — this is where the school is created</span></div>
          <div className="card-body">
            {notice && <Notice {...notice} onClear={() => setNotice(null)} />}

            {/* Review summary */}
            <div style={{ background: "var(--page)", border: "1px solid var(--line)", borderRadius: "var(--r12)", padding: "14px 16px", marginBottom: 20 }}>
              <div style={{ fontSize: 11, fontWeight: 700, letterSpacing: ".06em", textTransform: "uppercase", color: "var(--ink3)", marginBottom: 10 }}>Review before submitting</div>
              {[
                ["School",    `${form.name} (${form.ownership_type || "—"})`],
                ["Slug",      form.slug || slugify(form.name)],
                ["Branch",    `${bf.name} · ${bf.branch_type}${bf.is_main_branch ? " · main" : ""}`],
                ["Admin",     af.email ? `${[af.first_name, af.last_name].filter(Boolean).join(" ")} <${af.email}>` : "None"],
              ].map(([k, v]) => (
                <div key={k} style={{ display: "flex", gap: 12, marginBottom: 5, fontSize: 12 }}>
                  <span style={{ color: "var(--ink3)", width: 60, flexShrink: 0 }}>{k}</span>
                  <span style={{ color: "var(--ink)", fontFamily: k === "Slug" ? "var(--fm)" : undefined }}>{v}</span>
                </div>
              ))}
            </div>

            <div className="form-row-2">
              <div className="form-row">
                <label>Package plan <span className="required">*</span></label>
                {packages.length > 0 ? (
                  <select value={pf.package_plan} onChange={(e) => setPf((f) => ({ ...f, package_plan: e.target.value }))}>
                    <option value="">Select a plan…</option>
                    {packages.map((p) => (
                      <option key={p.code || p.id} value={p.code}>{p.name}{p.billing_cycle ? ` (${p.billing_cycle})` : ""}</option>
                    ))}
                  </select>
                ) : (
                  <input type="text" placeholder="Loading plans… or enter plan code manually" value={pf.package_plan}
                    onChange={(e) => setPf((f) => ({ ...f, package_plan: e.target.value }))} style={{ fontFamily: "var(--fm)" }} />
                )}
              </div>
              <div className="form-row">
                <label>Subscription expires</label>
                <input type="date" value={pf.subscription_expires} onChange={(e) => setPf((f) => ({ ...f, subscription_expires: e.target.value }))} />
              </div>
            </div>
            <div className="form-row-2">
              <div className="form-row">
                <label>Student capacity <span className="required">*</span></label>
                <input type="number" min="1" placeholder="e.g. 500" value={pf.student_capacity} onChange={(e) => setPf((f) => ({ ...f, student_capacity: e.target.value }))} />
              </div>
              <div className="form-row">
                <label>Teacher capacity <span className="required">*</span></label>
                <input type="number" min="1" placeholder="e.g. 50" value={pf.teacher_capacity} onChange={(e) => setPf((f) => ({ ...f, teacher_capacity: e.target.value }))} />
              </div>
            </div>
            <div className="form-row" style={{ maxWidth: 320 }}>
              <label>Admin capacity <span className="required">*</span></label>
              <input type="number" min="1" placeholder="e.g. 10" value={pf.admin_capacity} onChange={(e) => setPf((f) => ({ ...f, admin_capacity: e.target.value }))} />
            </div>

            {modules.length > 0 && (
              <div className="form-row">
                <label>Enabled modules</label>
                <div style={{ display: "flex", flexWrap: "wrap", gap: 8, marginTop: 4 }}>
                  {modules.map((m) => {
                    const on = pf.enabled_modules.includes(m.key);
                    return (
                      <label key={m.key} style={{ display: "flex", alignItems: "center", gap: 6, padding: "5px 10px", borderRadius: "var(--r6)", border: `1.5px solid ${on ? "var(--v)" : "var(--line2)"}`, background: on ? "var(--v-l)" : "transparent", cursor: "pointer", fontSize: 12, fontWeight: on ? 600 : 400, color: on ? "var(--v-d)" : "var(--ink2)", transition: "all .12s" }}>
                        <input type="checkbox" style={{ display: "none" }} checked={on}
                          onChange={() => setPf((f) => ({ ...f, enabled_modules: on ? f.enabled_modules.filter((k) => k !== m.key) : [...f.enabled_modules, m.key] }))} />
                        {m.name}
                      </label>
                    );
                  })}
                </div>
              </div>
            )}

            <div style={{ display: "flex", gap: 10, paddingTop: 12 }}>
              <button className="btn btn-ghost btn-sm" onClick={() => setWizStep(3)}>← Back</button>
              <button className="btn btn-primary" onClick={step4Submit} disabled={saving} style={{ minWidth: 160 }}>
                {saving ? <><span className="spin" /><span> Creating school…</span></> : "🏫 Create school"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );

  // ── RESULT ────────────────────────────────────────────────────────────────────
  if (subPage === "result") return (
    <div className="page active" id="page-schools">
      <div className="page-hd">
        <div className="page-hd-left"><h1>School <em>onboarded</em></h1><p>The new school is now live on the platform</p></div>
        <div className="page-hd-right">
          <button className="btn btn-ghost" onClick={() => { onSubPage("list"); load(); }}>← All schools</button>
          <button className="btn btn-primary" onClick={() => onSubPage("create")}>+ Create another</button>
        </div>
      </div>
      {schoolResult && <ResultCard data={schoolResult} type="school" />}
    </div>
  );

  // ── LIST ──────────────────────────────────────────────────────────────────────
  return (
    <div className="page active" id="page-schools">
      <div className="page-hd">
        <div className="page-hd-left"><h1>School <em>Onboarding</em></h1><p>All tenants on the Vision platform</p></div>
        <div className="page-hd-right">
          <button className="btn btn-secondary btn-sm" onClick={load}>↻ Reload</button>
          <button className="btn btn-primary" onClick={() => onSubPage("create")}>+ Add New School</button>
        </div>
      </div>
      <div className="stats-row" style={{ marginBottom: 20 }}>
        {[
          { label: "All Schools",      val: statCounts.all      },
          { label: "Active Schools",   val: statCounts.active   },
          { label: "Pending Schools",  val: statCounts.pending  },
          { label: "Inactive Schools", val: statCounts.inactive },
        ].map((s) => (
          <div className="stat-card" key={s.label}>
            <div className="stat-label">{s.label}</div>
            <div className="stat-val">{loading ? "…" : s.val}</div>
          </div>
        ))}
      </div>
      <div className="card">
        <div className="card-head">
          <h3>All schools</h3>
          <div style={{ display: "flex", gap: 8 }}>
            <div className="search-bar" style={{ width: 220 }}>
              <input placeholder="Search…" value={q} onChange={(e) => setQ(e.target.value)} />
            </div>
            <button className="btn btn-ghost btn-sm" onClick={load}>↻</button>
          </div>
        </div>
        <div className="card-body" style={{ padding: 0 }}>
          <div className="tbl-wrap">
            <table className="tbl">
              <thead><tr><th>S/N</th><th>School Name</th><th>Address</th><th>Ownership</th><th>Status</th><th>Created</th></tr></thead>
              <tbody>
                {loading ? (
                  <tr><td colSpan={6} className="tbl-empty"><p>Loading…</p></td></tr>
                ) : visible.length === 0 ? (
                  <tr><td colSpan={6} className="tbl-empty"><p>No schools yet</p><span>Click "Add New School" to onboard your first tenant</span></td></tr>
                ) : visible.map((sch, i) => (
                  <tr key={sch.id || sch.slug} onClick={() => openSchDetail(sch)}>
                    <td style={{ color: "var(--ink3)", fontSize: 12 }}>{i + 1}</td>
                    <td><strong>{sch.name || "—"}</strong><div style={{ fontSize: 11, color: "var(--ink3)", fontFamily: "var(--fm)" }}>{sch.slug}</div></td>
                    <td style={{ fontSize: 12 }}>{sch.address || "—"}</td>
                    <td style={{ fontSize: 12 }}>{sch.ownership_type || "—"}</td>
                    <td><span className={`pill ${statusPill(sch.status)}`}>{sch.status || "—"}</span></td>
                    <td style={{ fontSize: 11, color: "var(--ink3)" }}>{fmtDate(sch.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  );
}

// ─── PlatformStaffPage ───────────────────────────────────────────────────────

function PlatformStaffPage({ call, showToast, openDetail, addActivity }) {
  const [tab, setTab] = useState("roles"); // "roles" | "assignments" | "requests" | "audit"

  // Roles tab
  const [roles, setRoles]           = useState([]);
  const [rolesLoading, setRolesLoading] = useState(false);
  const [rolesPage, setRolesPage]   = useState(1);
  const [rolesCount, setRolesCount] = useState(0);
  const [showRoleForm, setShowRoleForm] = useState(false);
  const [roleForm, setRoleForm]     = useState({ name: "", description: "", scope: "PLATFORM" });
  const [rolePerms, setRolePerms]   = useState([]);
  const [rolePermInput, setRolePermInput] = useState("");
  const [roleSaving, setRoleSaving] = useState(false);
  const [roleNotice, setRoleNotice] = useState(null);

  // Assignments tab
  const [assignments, setAssignments] = useState([]);
  const [assignLoading, setAssignLoading] = useState(false);
  const [showAssignForm, setShowAssignForm] = useState(false);
  const [users, setUsers] = useState([]);
  const [assignForm, setAssignForm] = useState({ user: "", role_template: "" });
  const [assignSaving, setAssignSaving] = useState(false);
  const [assignNotice, setAssignNotice] = useState(null);

  // Requests tab
  const [requests, setRequests] = useState([]);
  const [reqLoading, setReqLoading] = useState(false);

  // Audit tab
  const [auditLogs, setAuditLogs] = useState([]);
  const [auditLoading, setAuditLoading] = useState(false);

  // Stats
  const [stats, setStats] = useState({ roles: "—", assignments: "—", pending: "—" });

  const loadRoles = useCallback(async (page = 1) => {
    setRolesLoading(true);
    const r = await call("GET", `${EP.PLATFORM_ROLES}?page=${page}`);
    if (r.ok) {
      setRoles(r.data || []);
      setRolesCount(r.pagination?.totalItems || 0);
      setRolesPage(page);
    }
    setRolesLoading(false);
  }, [call]);

  const loadAssignments = useCallback(async () => {
    setAssignLoading(true);
    const r = await call("GET", EP.PLATFORM_ROLE_ASSIGNMENTS);
    setAssignLoading(false);
    if (r.ok) setAssignments(r.data?.results || r.data || []);
  }, [call]);

  const loadRequests = useCallback(async () => {
    setReqLoading(true);
    const r = await call("GET", EP.PLATFORM_CHANGE_REQUESTS);
    setReqLoading(false);
    if (r.ok) setRequests(r.data?.results || r.data || []);
  }, [call]);

  const loadAudit = useCallback(async () => {
    setAuditLoading(true);
    const r = await call("GET", EP.AUDIT_EVENTS_FILTER("rbac"));
    setAuditLoading(false);
    if (r.ok) setAuditLogs(r.data?.results || r.data || []);
  }, [call]);

  const loadUsers = useCallback(async () => {
    const r = await call("GET", EP.USERS);
    if (r.ok) setUsers(r.data?.results || r.data || []);
  }, [call]);

  useEffect(() => {
    loadRoles();
    loadAssignments();
    loadRequests();
  }, []);

  useEffect(() => {
    if (tab === "audit") loadAudit();
    if (tab === "assignments" && showAssignForm) loadUsers();
  }, [tab]);

  useEffect(() => {
    setStats({
      roles: rolesLoading ? "…" : roles.length,
      assignments: assignLoading ? "…" : assignments.length,
      pending: reqLoading ? "…" : requests.filter((r) => (r.status || "").toUpperCase() === "PENDING").length,
    });
  }, [roles, assignments, requests, rolesLoading, assignLoading, reqLoading]);

  function addRolePerm(v) {
    const k = v.trim().toLowerCase().replace(/\s+/g, "");
    if (!k || rolePerms.includes(k)) return;
    setRolePerms((p) => [...p, k]);
    setRolePermInput("");
  }

  async function createRole() {
    setRoleNotice(null);
    if (!roleForm.name) { setRoleNotice({ type: "error", title: "Missing name", msg: "Role name is required." }); return; }
    if (!rolePerms.length) { setRoleNotice({ type: "error", title: "No permissions", msg: "Add at least one permission key." }); return; }
    setRoleSaving(true);
    const r = await call("POST", EP.PLATFORM_ROLES, { ...roleForm, permission_keys: rolePerms });
    setRoleSaving(false);
    if (r.ok || r.status === 201) {
      showToast("Platform role created", "ok");
      addActivity("🔐", `Platform role created: ${roleForm.name}`);
      setShowRoleForm(false);
      setRoleForm({ name: "", description: "", scope: "PLATFORM" });
      setRolePerms([]);
      loadRoles();
    } else {
      setRoleNotice({ type: "error", title: "Create failed", msg: flattenErrors(r.data) });
    }
  }

  async function assignRole() {
    setAssignNotice(null);
    if (!assignForm.user || !assignForm.role_template) {
      setAssignNotice({ type: "error", title: "Missing fields", msg: "Select both a user and a role." });
      return;
    }
    setAssignSaving(true);
    const r = await call("POST", EP.PLATFORM_ROLE_ASSIGNMENTS, assignForm);
    setAssignSaving(false);
    if (r.ok || r.status === 201) {
      showToast("Role assigned", "ok");
      setShowAssignForm(false);
      setAssignForm({ user: "", role_template: "" });
      loadAssignments();
    } else {
      setAssignNotice({ type: "error", title: "Assignment failed", msg: flattenErrors(r.data) });
    }
  }

  async function revokeAssignment(id, name) {
    if (!confirm(`Revoke role assignment for ${name}?`)) return;
    const r = await call("DELETE", EP.PLATFORM_ROLE_ASSIGNMENT(id));
    if (r.ok || r.status === 204) { showToast("Assignment revoked", "ok"); loadAssignments(); }
    else showToast("Could not revoke", "err");
  }

  async function approveReq(id) {
    const r = await call("POST", EP.PLATFORM_CHANGE_REQUEST_DECIDE(id), { action: "APPROVE" });
    if (r.ok) { showToast("Request approved", "ok"); loadRequests(); }
    else showToast(r.data?.detail || "Could not approve", "err");
  }

  async function denyReq(id) {
    const reason = prompt("Reason for denial:");
    if (!reason) return;
    const r = await call("POST", EP.PLATFORM_CHANGE_REQUEST_DECIDE(id), { action: "DENY", reason });
    if (r.ok) { showToast("Request denied"); loadRequests(); }
    else showToast(r.data?.detail || "Could not deny", "err");
  }

  function openRoleDetail(role) {
    openDetail(role.name || "Role", (
      <PlatformRoleDetailPanel role={role} call={call} showToast={showToast} onSaved={loadRoles} />
    ));
  }

  const tabBtn = (key, label) => (
    <button key={key} onClick={() => setTab(key)} style={{ padding: "6px 16px", borderRadius: "var(--r6)", border: `1.5px solid ${tab === key ? "var(--v)" : "var(--line2)"}`, background: tab === key ? "var(--v)" : "var(--card)", color: tab === key ? "#fff" : "var(--ink2)", fontFamily: "var(--f)", fontSize: 13, fontWeight: tab === key ? 600 : 400, cursor: "pointer" }}>
      {label}
    </button>
  );

  const PLATFORM_PERMS = [
    "platform.schools.manage", "platform.users.manage", "platform.rbac.manage",
    "platform.audit.view", "platform.sessions.revoke", "platform.billing.view",
    "platform.reports.export", "platform.config.manage",
  ];

  return (
    <div className="page active" id="page-platform">
      <div className="page-hd">
        <div className="page-hd-left"><h1>Platform Staff <em>Access</em></h1><p>Manage roles, permissions and assignments for internal platform staff</p></div>
      </div>

      {/* Stats */}
      <div className="stats-row" style={{ marginBottom: 20 }}>
        {[
          { icon: "🔐", label: "Platform Roles", val: stats.roles },
          { icon: "👤", label: "Role Assignments", val: stats.assignments },
          { icon: "⏳", label: "Pending Requests", val: stats.pending },
        ].map((s) => (
          <div className="stat-card" key={s.label}>
            <div className="stat-icon">{s.icon}</div>
            <div className="stat-label">{s.label}</div>
            <div className="stat-val">{s.val}</div>
          </div>
        ))}
      </div>

      {/* Tab bar */}
      <div style={{ display: "flex", gap: 4, marginBottom: 16 }}>
        {tabBtn("roles", "🔐 Staff Roles")}
        {tabBtn("assignments", "👤 Assignments")}
        {tabBtn("requests", "⏳ Change Requests")}
        {tabBtn("audit", "📋 RBAC Audit")}
      </div>

      {/* ── Staff Roles tab ───────────────────────────────────────────────────── */}
      {tab === "roles" && (
        <div>
          <div className="card" style={{ marginBottom: 16 }}>
            <div className="card-head">
              <h3>Platform role definitions</h3>
              <button className="btn btn-primary btn-sm" onClick={() => { setShowRoleForm((v) => !v); setRoleNotice(null); }}>
                {showRoleForm ? "✕ Cancel" : "+ Create role"}
              </button>
            </div>
            {showRoleForm && (
              <div className="card-body" style={{ borderBottom: "1px solid var(--line)", paddingBottom: 20 }}>
                {roleNotice && <Notice {...roleNotice} onClear={() => setRoleNotice(null)} />}
                <div className="form-row-2">
                  <div className="form-row">
                    <label>Role name <span className="required">*</span></label>
                    <input type="text" placeholder="e.g. Compliance Admin" value={roleForm.name} onChange={(e) => setRoleForm((f) => ({ ...f, name: e.target.value }))} />
                  </div>
                  <div className="form-row">
                    <label>Scope</label>
                    <select value={roleForm.scope} onChange={(e) => setRoleForm((f) => ({ ...f, scope: e.target.value }))}>
                      <option value="PLATFORM">Platform</option>
                      <option value="GLOBAL">Global</option>
                    </select>
                  </div>
                </div>
                <div className="form-row">
                  <label>Description</label>
                  <textarea placeholder="What does this role allow?" value={roleForm.description} onChange={(e) => setRoleForm((f) => ({ ...f, description: e.target.value }))} rows={2} style={{ width: "100%", padding: "8px 12px", border: "1.5px solid var(--line2)", borderRadius: "var(--r8)", fontFamily: "var(--f)", fontSize: 13, resize: "vertical", outline: "none" }} />
                </div>
                <div className="form-row">
                  <label>Permission keys <span className="required">*</span></label>
                  <div style={{ display: "flex", gap: 8, marginBottom: 8 }}>
                    <input type="text" placeholder="e.g. platform.schools.manage" value={rolePermInput}
                      onChange={(e) => setRolePermInput(e.target.value)}
                      onKeyDown={(e) => e.key === "Enter" && addRolePerm(rolePermInput)}
                      style={{ flex: 1, height: 36, padding: "0 12px", border: "1.5px solid var(--line2)", borderRadius: "var(--r8)", background: "var(--page)", fontFamily: "var(--fm)", fontSize: 12, outline: "none" }} />
                    <button className="btn btn-secondary btn-sm" onClick={() => addRolePerm(rolePermInput)}>Add</button>
                  </div>
                  <div className="perm-tags">
                    {rolePerms.length === 0
                      ? <span style={{ fontSize: 11, color: "var(--ink3)", fontStyle: "italic" }}>Type a permission key above or click a suggestion below</span>
                      : rolePerms.map((p, i) => (
                        <div className="perm-tag" key={p}>{p}<button className="perm-tag-rm" onClick={() => setRolePerms((t) => t.filter((_, j) => j !== i))}>✕</button></div>
                      ))}
                  </div>
                </div>
                <div className="section-div"><span>Platform permission suggestions</span></div>
                <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBottom: 16 }}>
                  {PLATFORM_PERMS.map((p) => (
                    <button key={p} className="btn btn-ghost btn-sm" onClick={() => addRolePerm(p)} style={{ fontFamily: "var(--fm)", fontSize: 11 }}>{p}</button>
                  ))}
                </div>
                <div style={{ display: "flex", gap: 10 }}>
                  <button className="btn btn-primary" onClick={createRole} disabled={roleSaving}>
                    {roleSaving ? <span className="spin" /> : null}
                    <span>{roleSaving ? "Creating…" : "Create platform role"}</span>
                  </button>
                </div>
              </div>
            )}
            <div className="card-body" style={{ padding: 0 }}>
              {rolesLoading ? (
                <div className="tbl-empty"><p>Loading…</p></div>
              ) : roles.length === 0 ? (
                <div className="tbl-empty"><p>No platform roles defined</p><span>Create a role to get started</span></div>
              ) : (
                <div className="tbl-wrap">
                  <table className="tbl">
                    <thead><tr><th>Role name</th><th>Description</th><th>Permissions</th><th>Status</th></tr></thead>
                    <tbody>
                      {roles.map((r, i) => (
                        <tr key={r.id || i} style={{ cursor: "pointer" }} onClick={() => openRoleDetail(r)}>
                          <td>{r.name}</td>
                          <td style={{ fontSize: 12, color: "var(--ink2)", maxWidth: 200, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{r.description || "—"}</td>
                          <td style={{ fontFamily: "var(--fm)", fontSize: 12, color: "var(--ink3)" }}>{r.permissions_count ?? r.permission_keys?.length ?? 0} keys</td>
                          <td><span className={`pill ${statusPill(r.status || "ACTIVE")}`}>{r.status || "ACTIVE"}</span></td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              <Paginator count={rolesCount} page={rolesPage} onPage={loadRoles} />
            </div>
          </div>
        </div>
      )}

      {/* ── Assignments tab ───────────────────────────────────────────────────── */}
      {tab === "assignments" && (
        <div className="card">
          <div className="card-head">
            <h3>Role assignments</h3>
            <button className="btn btn-primary btn-sm" onClick={() => { setShowAssignForm((v) => !v); if (!showAssignForm) { loadUsers(); } setAssignNotice(null); }}>
              {showAssignForm ? "✕ Cancel" : "+ Assign role"}
            </button>
          </div>
          {showAssignForm && (
            <div className="card-body" style={{ borderBottom: "1px solid var(--line)", paddingBottom: 20 }}>
              {assignNotice && <Notice {...assignNotice} onClear={() => setAssignNotice(null)} />}
              <div className="form-row-2">
                <div className="form-row">
                  <label>Staff member <span className="required">*</span></label>
                  <select value={assignForm.user} onChange={(e) => setAssignForm((f) => ({ ...f, user: e.target.value }))} style={{ width: "100%", height: 38, padding: "0 10px", border: "1px solid var(--line2)", borderRadius: "var(--r6)", fontFamily: "var(--f)", fontSize: 13, color: "var(--ink)", background: "var(--card)", cursor: "pointer" }}>
                    <option value="">— select user —</option>
                    {users.map((u) => <option key={u.id} value={u.id}>{`${u.first_name || ""} ${u.last_name || ""}`.trim() || u.email}</option>)}
                  </select>
                </div>
                <div className="form-row">
                  <label>Platform role <span className="required">*</span></label>
                  <select value={assignForm.role_template} onChange={(e) => setAssignForm((f) => ({ ...f, role_template: e.target.value }))} style={{ width: "100%", height: 38, padding: "0 10px", border: "1px solid var(--line2)", borderRadius: "var(--r6)", fontFamily: "var(--f)", fontSize: 13, color: "var(--ink)", background: "var(--card)", cursor: "pointer" }}>
                    <option value="">— select role —</option>
                    {roles.map((r) => <option key={r.id} value={r.id}>{r.name}</option>)}
                  </select>
                </div>
              </div>
              <button className="btn btn-primary" onClick={assignRole} disabled={assignSaving}>
                {assignSaving ? <span className="spin" /> : null}
                <span>{assignSaving ? "Assigning…" : "Assign role"}</span>
              </button>
            </div>
          )}
          <div className="card-body" style={{ padding: 0 }}>
            {assignLoading ? (
              <div className="tbl-empty"><p>Loading…</p></div>
            ) : assignments.length === 0 ? (
              <div className="tbl-empty"><p>No role assignments</p><span>Assign a platform role to a staff member</span></div>
            ) : (
              <div className="tbl-wrap">
                <table className="tbl">
                  <thead><tr><th>Staff member</th><th>Email</th><th>Role</th><th>Assigned by</th><th>Date</th><th>Status</th><th></th></tr></thead>
                  <tbody>
                    {assignments.map((a, i) => (
                      <tr key={a.id || i}>
                        <td>{a.user_name || a.user_email?.split("@")[0] || "—"}</td>
                        <td style={{ fontFamily: "var(--fm)", fontSize: 11 }}>{a.user_email || "—"}</td>
                        <td>{a.role_name || a.role_template_name || "—"}</td>
                        <td style={{ fontSize: 12, color: "var(--ink3)" }}>{a.assigned_by_name || a.assigned_by || "System"}</td>
                        <td style={{ fontSize: 11, color: "var(--ink3)" }}>{fmtDate(a.created_at || a.assigned_at)}</td>
                        <td><span className={`pill ${statusPill(a.status || "ACTIVE")}`}>{a.status || "ACTIVE"}</span></td>
                        <td>
                          <button className="btn btn-danger btn-sm" onClick={() => revokeAssignment(a.id, a.user_email || a.user_name || "user")}>Revoke</button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </div>
      )}

      {/* ── Change Requests tab ───────────────────────────────────────────────── */}
      {tab === "requests" && (
        <div className="card">
          <div className="card-head">
            <h3>Role change requests</h3>
            <button className="btn btn-ghost btn-sm" onClick={loadRequests}>↻ Reload</button>
          </div>
          <div className="card-body" style={{ padding: 0 }}>
            {reqLoading ? (
              <div className="tbl-empty"><p>Loading…</p></div>
            ) : requests.length === 0 ? (
              <div className="tbl-empty"><p>No change requests</p><span>Role change requests will appear here for review</span></div>
            ) : (
              <div className="tbl-wrap">
                <table className="tbl">
                  <thead><tr><th>User</th><th>Requested role</th><th>Reason</th><th>Requested by</th><th>Date</th><th>Status</th><th>Actions</th></tr></thead>
                  <tbody>
                    {requests.map((req, i) => (
                      <tr key={req.id || i}>
                        <td style={{ fontFamily: "var(--fm)", fontSize: 11 }}>{req.user_email || req.user || "—"}</td>
                        <td>{req.role_name || req.role_template_name || "—"}</td>
                        <td style={{ fontSize: 12, color: "var(--ink2)", maxWidth: 180, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{req.reason || "—"}</td>
                        <td style={{ fontSize: 12, color: "var(--ink3)" }}>{req.requested_by_name || req.requested_by || "—"}</td>
                        <td style={{ fontSize: 11, color: "var(--ink3)" }}>{fmtDate(req.created_at)}</td>
                        <td><span className={`pill ${statusPill(req.status)}`}>{req.status || "PENDING"}</span></td>
                        <td>
                          {(req.status || "PENDING").toUpperCase() === "PENDING" && (
                            <div style={{ display: "flex", gap: 5 }}>
                              <button className="btn btn-primary btn-sm" onClick={() => approveReq(req.id)}>Approve</button>
                              <button className="btn btn-danger btn-sm" onClick={() => denyReq(req.id)}>Deny</button>
                            </div>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </div>
      )}

      {/* ── RBAC Audit tab ────────────────────────────────────────────────────── */}
      {tab === "audit" && (
        <div className="card">
          <div className="card-head">
            <h3>RBAC audit trail</h3>
            <button className="btn btn-ghost btn-sm" onClick={loadAudit}>↻ Reload</button>
          </div>
          <div className="card-body">
            {auditLoading ? (
              <div className="tbl-empty"><p>Loading…</p></div>
            ) : auditLogs.length === 0 ? (
              <div className="tbl-empty"><p>No RBAC audit events</p><span>Role changes and access events will appear here</span></div>
            ) : auditLogs.slice(0, 50).map((log, i) => {
              const icon = auditIcon(log.action || log.event_type || "rbac");
              return (
                <div className="activity-item" key={i}>
                  <div className="act-icon" style={{ background: icon.bg }}>{icon.i}</div>
                  <div className="act-body">
                    <div className="act-title">{log.action || log.event_type || "RBAC event"}</div>
                    <div className="act-meta">{log.actor || log.user || "System"} · {log.resource || log.target || ""}</div>
                  </div>
                  <div className="act-time">{fmtDate(log.created_at || log.timestamp)}</div>
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}

// ─── BranchesPage ────────────────────────────────────────────────────────────

const NG_TEST_BRANCHES = [
  {
    label: "Greenfield Academy – Lekki Campus",
    schoolHint: "greenfield",
    form: {
      name: "Lekki Campus", _type: "Secondary", country: "Nigeria",
      state: "Lagos", address: "Plot 14, Lekki Phase 1, Lagos",
      email: "lekki@greenfieldacademy.edu.ng", is_main: false,
      admin_full_name: "Chioma Ezenwachi", admin_email: "chioma.ezenwachi@greenfieldacademy.edu.ng",
      admin_phone: "+2348023456789", admin_branch_role: "Campus Coordinator",
    },
  },
  {
    label: "Heritage International – Maitama Campus",
    schoolHint: "heritage",
    form: {
      name: "Maitama Campus", _type: "Campus", country: "Nigeria",
      state: "FCT", address: "8 Oda Crescent, Maitama, Abuja",
      email: "maitama@heritageinternational.edu.ng", is_main: false,
      admin_full_name: "Yusuf Abdullahi", admin_email: "yusuf.abdullahi@heritageinternational.edu.ng",
      admin_phone: "+2348071234567", admin_branch_role: "Campus Director",
    },
  },
  {
    label: "Covenant Crown – Bodija Annex",
    schoolHint: "covenant",
    form: {
      name: "Bodija Annex", _type: "Annex", country: "Nigeria",
      state: "Oyo", address: "21 Bodija Estate, Ibadan",
      email: "bodija@covenantcrown.edu.ng", is_main: false,
      admin_full_name: "Funke Adesanya", admin_email: "funke.adesanya@covenantcrown.edu.ng",
      admin_phone: "+2348056789012", admin_branch_role: "Annex Principal",
    },
  },
  {
    label: "Lagos City Polytechnic – Apapa Campus",
    schoolHint: "lagos-city",
    form: {
      name: "Apapa Campus", _type: "Satellite", country: "Nigeria",
      state: "Lagos", address: "Block C, Mile 2 Road, Apapa, Lagos",
      email: "apapa@lagoscitypoly.edu.ng", is_main: false,
      admin_full_name: "Rotimi Adeyemo", admin_email: "rotimi.adeyemo@lagoscitypoly.edu.ng",
      admin_phone: "+2348034512345", admin_branch_role: "Campus Head",
    },
  },
  {
    label: "Sunrise Nursery – East Campus",
    schoolHint: "sunrise",
    form: {
      name: "East Campus", _type: "Campus", country: "Nigeria",
      state: "Rivers", address: "22 Ada George Road, Port Harcourt",
      email: "east@sunrisenursery.edu.ng", is_main: false,
      admin_full_name: "Gloria Nwachukwu", admin_email: "gloria.nwachukwu@sunrisenursery.edu.ng",
      admin_phone: "+2348098765432", admin_branch_role: "Head Teacher",
    },
  },
];

const INIT_BRANCH_FORM = {
  school: "", school_slug: "", name: "", _type: "Primary",
  country: "Nigeria", state: "", address: "", email: "",
  is_main: false,
  admin_full_name: "", admin_email: "", admin_phone: "", admin_branch_role: "Branch Manager",
};

function BranchesPage({ call, showToast, openDetail, addActivity, subPage, onSubPage }) {
  const [rows, setRows]             = useState([]);
  const [loading, setLoading]       = useState(false);
  const [notice, setNotice]         = useState(null);
  const [resultData, setResultData] = useState(null);
  const [schools, setSchools]       = useState([]);
  const [filterSlug, setFilterSlug] = useState("");
  const [form, setForm]             = useState(INIT_BRANCH_FORM);
  const [saving, setSaving]         = useState(false);
  const [prefillIdx, setPrefillIdx] = useState(0);

  const loadSchools = useCallback(async () => {
    const r = await call("GET", EP.SCHOOLS);
    if (r.ok) setSchools(r.data?.results || r.data || []);
  }, [call]);

  const loadBranches = useCallback(async (slug) => {
    if (!slug) { setRows([]); return; }
    setLoading(true);
    const r = await call("GET", EP.BRANCHES(slug));
    setLoading(false);
    if (r.ok) setRows(r.data?.results || r.data || []);
    else setRows([]);
  }, [call]);

  useEffect(() => {
    if (subPage === "list" || subPage === "create") loadSchools();
  }, [subPage]);

  useEffect(() => {
    if (subPage === "list") loadBranches(filterSlug);
  }, [filterSlug, subPage]);

  function setF(k) { return (e) => setForm((f) => ({ ...f, [k]: e.target.value })); }

  function fillTestData() {
    const d = NG_TEST_BRANCHES[prefillIdx % NG_TEST_BRANCHES.length];
    const matched = schools.find(
      (s) => (s.slug || "").includes(d.schoolHint) || (s.name || "").toLowerCase().includes(d.schoolHint)
    );
    setForm({
      ...INIT_BRANCH_FORM,
      ...d.form,
      school: matched ? (matched.id || matched.slug) : "",
      school_slug: matched ? matched.slug : "",
    });
    setPrefillIdx((i) => (i + 1) % NG_TEST_BRANCHES.length);
    showToast(`Prefilled: ${d.label}`, "ok");
  }

  async function create() {
    setNotice(null);
    const schoolObj = schools.find((s) => s.id === form.school || s.slug === form.school);
    const schoolSlug = form.school_slug || schoolObj?.slug;
    if (!schoolSlug) {
      setNotice({ type: "error", title: "Missing fields", msg: "Please select a school." });
      return;
    }
    if (!form.name.trim()) {
      setNotice({ type: "error", title: "Missing fields", msg: "Branch name is required." });
      return;
    }
    if (!form.admin_full_name.trim() || !form.admin_email.trim()) {
      setNotice({ type: "error", title: "Missing fields", msg: "Admin full name and email are required." });
      return;
    }
    setSaving(true);
    const payload = {
      name: form.name.trim(),
      _type: form._type,
      is_main: form.is_main,
      country: form.country || "Nigeria",
      ...(form.state    && { state: form.state }),
      ...(form.address  && { address: form.address }),
      ...(form.email    && { email: form.email }),
      primary_admin_data: {
        full_name: form.admin_full_name.trim(),
        email: form.admin_email.trim(),
        ...(form.admin_phone       && { phone: form.admin_phone }),
        ...(form.admin_branch_role && { branch_role: form.admin_branch_role }),
      },
    };
    const r = await call("POST", EP.BRANCH_CREATE(schoolSlug), payload);
    setSaving(false);
    if (r.ok) {
      setResultData(r.data);
      addActivity("🌿", `Branch created: ${r.data.name || form.name}`);
      showToast("Branch created", "ok");
      onSubPage("result");
    } else {
      setNotice({ type: "error", title: "Create failed", msg: flattenErrors(r.data) });
    }
  }

  function openBranchDetail(branch) {
    openDetail(branch.name || "Branch", (
      <div>
        <ResultCard data={branch} type="branch" />
        <div style={{ marginTop: 16, display: "flex", flexDirection: "column", gap: 8 }}>
          <div className="rc-row"><span className="rc-key">Type</span><span className="rc-val">{branch._type || branch.branch_type || "—"}</span></div>
          <div className="rc-row"><span className="rc-key">Country</span><span className="rc-val">{branch.country || "—"}</span></div>
          <div className="rc-row"><span className="rc-key">State</span><span className="rc-val">{branch.state || "—"}</span></div>
          {branch.address && <div className="rc-row"><span className="rc-key">Address</span><span className="rc-val">{branch.address}</span></div>}
          {branch.email && <div className="rc-row"><span className="rc-key">Email</span><span className="rc-val">{branch.email}</span></div>}
          <div className="rc-row"><span className="rc-key">Main branch</span><span className="rc-val">{branch.is_main ? "Yes" : "No"}</span></div>
        </div>
      </div>
    ));
  }

  if (subPage === "result") return (
    <div className="page active" id="page-branches-result">
      <div className="page-hd">
        <div className="page-hd-left">
          <button className="btn btn-ghost btn-sm" onClick={() => onSubPage("list")}>← Back to branches</button>
        </div>
      </div>
      {resultData ? (
        <ResultCard data={resultData} type="branch" extra={
          <button className="btn btn-secondary btn-sm" onClick={() => { setResultData(null); setForm(INIT_BRANCH_FORM); onSubPage("create"); }}>
            + Create another
          </button>
        } />
      ) : (
        <Notice type="error" title="No result" msg="No branch data to display." onClear={() => onSubPage("list")} />
      )}
    </div>
  );

  if (subPage === "create") return (
    <div className="page active" id="page-branches-create">
      <div className="page-hd">
        <div className="page-hd-left">
          <button className="btn btn-ghost btn-sm" onClick={() => onSubPage("list")}>← Branches</button>
          <h1>Create branch</h1>
          <p>Add a new branch to an existing school</p>
        </div>
        <div className="page-hd-right">
          <button className="btn btn-secondary btn-sm" onClick={fillTestData} title="Cycle through Nigerian test branch profiles">⚡ Prefill test data</button>
        </div>
      </div>
      <div className="card" style={{ maxWidth: 600 }}>
        <div className="card-head"><h3>Branch details</h3></div>
        <div className="card-body">
          {notice && <Notice type={notice.type} title={notice.title} msg={notice.msg} onClear={() => setNotice(null)} />}

          <div className="form-row">
            <label>School <span style={{ color: "var(--red)" }}>*</span></label>
            <select
              value={form.school}
              onChange={(e) => {
                const s = schools.find((sc) => sc.id === e.target.value || sc.slug === e.target.value);
                setForm((f) => ({ ...f, school: e.target.value, school_slug: s?.slug || "" }));
              }}
              style={{ width: "100%", height: 38, padding: "0 10px", border: "1px solid var(--line2)", borderRadius: "var(--r6)", fontFamily: "var(--f)", fontSize: 13, color: "var(--ink)", background: "var(--card)", cursor: "pointer" }}
            >
              <option value="">— select school —</option>
              {schools.map((s) => (
                <option key={s.slug || s.id} value={s.id || s.slug}>{s.name}</option>
              ))}
            </select>
          </div>

          <div className="form-row">
            <label>Branch name <span style={{ color: "var(--red)" }}>*</span></label>
            <input type="text" placeholder="e.g. Main Campus" value={form.name} onChange={setF("name")} />
          </div>

          <div className="form-row-2">
            <div className="form-row">
              <label>Branch type</label>
              <select value={form._type} onChange={setF("_type")} style={{ width: "100%", height: 38, padding: "0 10px", border: "1px solid var(--line2)", borderRadius: "var(--r6)", fontFamily: "var(--f)", fontSize: 13, color: "var(--ink)", background: "var(--card)", cursor: "pointer" }}>
                <option value="Primary">Primary</option>
                <option value="Secondary">Secondary</option>
                <option value="Main">Main</option>
                <option value="Satellite">Satellite</option>
                <option value="Campus">Campus</option>
                <option value="Annex">Annex</option>
                <option value="Combined">Combined</option>
              </select>
            </div>
            <div className="form-row">
              <label>Country</label>
              <select value={form.country} onChange={setF("country")} style={{ width: "100%", height: 38, padding: "0 10px", border: "1px solid var(--line2)", borderRadius: "var(--r6)", fontFamily: "var(--f)", fontSize: 13, color: "var(--ink)", background: "var(--card)", cursor: "pointer" }}>
                <option value="Nigeria">Nigeria 🇳🇬</option>
                <option value="Ghana">Ghana 🇬🇭</option>
                <option value="Kenya">Kenya 🇰🇪</option>
                <option value="South Africa">South Africa 🇿🇦</option>
              </select>
            </div>
          </div>

          <div className="form-row-2">
            <div className="form-row">
              <label>State / Province</label>
              <input type="text" placeholder="e.g. Lagos" value={form.state} onChange={setF("state")} />
            </div>
            <div className="form-row">
              <label>Contact email</label>
              <input type="email" placeholder="branch@school.edu.ng" value={form.email} onChange={setF("email")} />
            </div>
          </div>

          <div className="form-row">
            <label>Address</label>
            <input type="text" placeholder="Street address (optional)" value={form.address} onChange={setF("address")} />
          </div>

          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "12px 0", borderBottom: "1px solid var(--line)", marginBottom: 16 }}>
            <div>
              <div style={{ fontWeight: 600, fontSize: 13 }}>Main Branch</div>
              <div style={{ fontSize: 12, color: "var(--ink3)" }}>Toggle on if this is the school's main branch</div>
            </div>
            <div onClick={() => setForm((f) => ({ ...f, is_main: !f.is_main }))}
              style={{ width: 44, height: 24, borderRadius: 12, background: form.is_main ? "var(--v)" : "var(--line2)", cursor: "pointer", position: "relative", transition: "background .2s" }}>
              <div style={{ width: 18, height: 18, borderRadius: "50%", background: "#fff", position: "absolute", top: 3, left: form.is_main ? 23 : 3, transition: "left .2s" }} />
            </div>
          </div>

          <div className="section-div">
            <span>Branch admin <span style={{ color: "var(--red)", fontWeight: 400, fontSize: 11 }}>required</span></span>
          </div>
          <div className="form-row-2">
            <div className="form-row">
              <label>Full name <span style={{ color: "var(--red)" }}>*</span></label>
              <input type="text" placeholder="e.g. Emeka Okonkwo" value={form.admin_full_name} onChange={setF("admin_full_name")} />
            </div>
            <div className="form-row">
              <label>Email <span style={{ color: "var(--red)" }}>*</span></label>
              <input type="email" placeholder="admin@school.edu.ng" value={form.admin_email} onChange={setF("admin_email")} />
            </div>
          </div>
          <div className="form-row-2">
            <div className="form-row">
              <label>Phone</label>
              <input type="tel" placeholder="+234 801 234 5678" value={form.admin_phone} onChange={setF("admin_phone")} />
            </div>
            <div className="form-row">
              <label>Role / Title</label>
              <input type="text" placeholder="e.g. Branch Manager" value={form.admin_branch_role} onChange={setF("admin_branch_role")} />
            </div>
          </div>

          <div style={{ display: "flex", gap: 10, paddingTop: 8 }}>
            <button className="btn btn-primary" onClick={create} disabled={saving}>
              {saving ? <span className="spin" /> : null}
              <span>{saving ? "Creating…" : "🌿 Create branch"}</span>
            </button>
            <button className="btn btn-ghost" onClick={() => onSubPage("list")}>Cancel</button>
          </div>
        </div>
      </div>
    </div>
  );

  // list view
  return (
    <div className="page active" id="page-branches">
      <div className="page-hd">
        <div className="page-hd-left">
          <h1>Branches</h1>
          <p>School branches across the platform</p>
        </div>
        <div className="page-hd-right">
          <select
            value={filterSlug}
            onChange={(e) => setFilterSlug(e.target.value)}
            style={{ height: 34, padding: "0 10px", border: "1px solid var(--line2)", borderRadius: "var(--r6)", fontFamily: "var(--f)", fontSize: 13, color: "var(--ink)", background: "var(--card)", cursor: "pointer" }}
          >
            <option value="">— filter by school —</option>
            {schools.map((s) => (
              <option key={s.slug || s.id} value={s.slug}>{s.name}</option>
            ))}
          </select>
          {filterSlug && <button className="btn btn-secondary btn-sm" onClick={() => loadBranches(filterSlug)}>↻ Reload</button>}
          <button className="btn btn-primary btn-sm" onClick={() => onSubPage("create")}>+ Add branch</button>
        </div>
      </div>
      <div className="card">
        <div className="card-head">
          <h3>Branches{filterSlug ? ` — ${schools.find((s) => s.slug === filterSlug)?.name || filterSlug}` : ""}</h3>
          <span className="card-count">{rows.length}</span>
        </div>
        <div className="card-body">
          {!filterSlug ? (
            <div className="tbl-empty">
              <p>Select a school to view its branches</p>
              <span>Use the filter dropdown above to choose a school</span>
            </div>
          ) : loading ? (
            <div className="tbl-empty"><p>Loading…</p></div>
          ) : rows.length === 0 ? (
            <div className="tbl-empty">
              <p>No branches yet</p>
              <span>Create a branch to get started</span>
              <button className="btn btn-primary btn-sm" style={{ marginTop: 12 }} onClick={() => onSubPage("create")}>+ Add branch</button>
            </div>
          ) : (
            <div className="tbl-wrap">
              <table className="tbl">
                <thead><tr><th>Name</th><th>Code</th><th>Type</th><th>Status</th><th>Country</th><th>Main</th></tr></thead>
                <tbody>
                  {rows.map((b, i) => (
                    <tr key={b.id || b.code || i} onClick={() => openBranchDetail(b)}>
                      <td>{b.name}</td>
                      <td style={{ fontFamily: "var(--fm)", fontSize: 11 }}>{b.code}</td>
                      <td><span className="pill pill-violet">{b._type || b.branch_type || "—"}</span></td>
                      <td><span className={`pill ${statusPill(b.status)}`}>{b.status || "—"}</span></td>
                      <td>{b.country || "—"}</td>
                      <td>{b.is_main ? <span className="pill pill-green">Yes</span> : <span className="pill">No</span>}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// ─── UsersPage ────────────────────────────────────────────────────────────────

function UsersPage({ call, showToast, openDetail, addActivity, subPage, onSubPage }) {
  const [rows, setRows] = useState([]);
  const [invites, setInvites] = useState([]);
  const [loading, setLoading] = useState(false);
  const [invLoading, setInvLoading] = useState(false);
  const [notice, setNotice] = useState(null);
  const [resultData, setResultData] = useState(null);
  const [q, setQ] = useState("");
  const [tab, setTab] = useState("members"); // "members" | "invites"
  const [form, setForm] = useState({ first_name: "", last_name: "", email: "", phone_number: "", gender: "" });
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    const r = await call("GET", EP.USERS);
    setLoading(false);
    if (r.ok) setRows(r.data?.results || r.data || []);
  }, [call]);

  const loadInvites = useCallback(async () => {
    setInvLoading(true);
    const r = await call("GET", EP.INVITATIONS);
    setInvLoading(false);
    if (r.ok) setInvites(r.data?.results || r.data || []);
    else {
      // fallback: filter pending users
      const ur = await call("GET", `${EP.USERS}?status=PENDING`);
      if (ur.ok) setInvites(ur.data?.results || ur.data || []);
    }
  }, [call]);

  useEffect(() => {
    if (subPage === "list") {
      load();
      loadInvites();
    }
  }, [subPage]);

  function setF(k) { return (e) => setForm((f) => ({ ...f, [k]: e.target.value })); }

  async function invite() {
    setNotice(null);
    const { first_name, last_name, email } = form;
    if (!first_name || !last_name || !email) { setNotice({ type: "error", title: "Missing fields", msg: "First name, last name and email are required." }); return; }
    setSaving(true);
    const payload = { first_name, last_name, email };
    if (form.phone_number) payload.phone_number = form.phone_number;
    if (form.gender) payload.gender = form.gender;
    const r = await call("POST", EP.USERS, payload);
    setSaving(false);
    if (r.ok || r.status === 201) {
      addActivity("✉", `Invitation sent to ${email}`);
      setResultData(r.data || { email, first_name, last_name, status: "PENDING" });
      onSubPage("result");
      showToast("Invitation sent", "ok");
    } else {
      setNotice({ type: "error", title: "Could not invite user", msg: flattenErrors(r.data) });
    }
  }

  async function suspendUser(id, name) {
    if (!confirm(`Suspend ${name}? They will lose access immediately.`)) return;
    const r = await call("POST", EP.USER_SUSPEND(id), { reason: "Suspended via Vision Staff Console" });
    if (r.ok) { showToast(`${name} suspended`, "ok"); load(); }
    else showToast(r.data?.detail || "Could not suspend", "err");
  }

  function openUserDetail(user) {
    call("GET", EP.USER(user.id)).then((r) => {
      if (!r.ok) return;
      const u = r.data;
      openDetail(
        "User profile",
        <ResultCard
          data={u}
          type="user"
          extra={
            <div style={{ display: "flex", gap: 8 }}>
              {u.status !== "SUSPENDED" && (
                <button className="btn btn-danger btn-sm" onClick={() => suspendUser(u.id, u.full_name || u.email)}>Suspend</button>
              )}
              {u.status === "SUSPENDED" && (
                <button className="btn btn-secondary btn-sm" onClick={() => call("POST", EP.USER_REACTIVATE(u.id), {}).then((res) => { if (res.ok) showToast("User reactivated", "ok"); })}>Reactivate</button>
              )}
              {u.status === "LOCKED" && (
                <button className="btn btn-secondary btn-sm" onClick={() => call("POST", EP.USER_UNLOCK(u.id), {}).then((res) => { if (res.ok) showToast("Account unlocked", "ok"); })}>Unlock</button>
              )}
            </div>
          }
        />
      );
    });
  }

  const activeRows     = rows.filter((u) => ["ACTIVE"].includes((u.status || "ACTIVE").toUpperCase()));
  const suspendedRows  = rows.filter((u) => ["SUSPENDED", "DEACTIVATED", "LOCKED"].includes((u.status || "").toUpperCase()));
  const pendingInvites = invites.filter((inv) => (inv.status || "PENDING").toUpperCase() === "PENDING");
  const qLow = q.toLowerCase();
  const visible = activeRows.filter((u) =>
    [u.full_name, u.first_name, u.last_name, u.email, u.status].join(" ").toLowerCase().includes(qLow)
  );
  const suspendedVisible = suspendedRows.filter((u) =>
    [u.full_name, u.first_name, u.last_name, u.email, u.status].join(" ").toLowerCase().includes(qLow)
  );

  if (subPage === "invite") return (
    <div className="page active" id="page-users">
      <div className="page-hd">
        <div className="page-hd-left"><h1>Invite <em>user</em></h1><p>Send an invitation to join the platform</p></div>
        <div className="page-hd-right"><button className="btn btn-ghost" onClick={() => { onSubPage("list"); load(); }}>← Back to users</button></div>
      </div>
      <div style={{ maxWidth: 600 }}>
        <div className="card">
          <div className="card-head"><div><h3>Invitation details</h3><p>The user will receive an email to activate their account</p></div></div>
          <div className="card-body">
            {notice && <Notice {...notice} onClear={() => setNotice(null)} />}
            <div className="form-row-2">
              <div className="form-row"><label>First name <span className="required">*</span></label><input type="text" placeholder="Chukwuemeka" value={form.first_name} onChange={setF("first_name")} /></div>
              <div className="form-row"><label>Last name <span className="required">*</span></label><input type="text" placeholder="Okonkwo" value={form.last_name} onChange={setF("last_name")} /></div>
            </div>
            <div className="form-row"><label>Email address <span className="required">*</span></label><input type="email" placeholder="user@school.edu.ng" value={form.email} onChange={setF("email")} /></div>
            <div className="form-row"><label>Phone number</label>
              <div className="input-prefix-wrap"><span className="input-prefix">+234</span><input type="tel" placeholder="801 234 5678" value={form.phone_number} onChange={setF("phone_number")} /></div>
            </div>
            <div className="form-row"><label>Gender</label>
              <select value={form.gender} onChange={setF("gender")}>
                <option value="">Not specified</option>
                <option value="MALE">Male</option>
                <option value="FEMALE">Female</option>
              </select>
            </div>
            <div className="form-actions">
              <button className="btn btn-ghost" onClick={() => onSubPage("list")}>Cancel</button>
              <button className="btn btn-primary" onClick={invite} disabled={saving}>
                {saving ? <span className="spin" /> : null}
                <span>Send invitation</span>
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );

  if (subPage === "result") return (
    <div className="page active" id="page-users">
      <div className="page-hd">
        <div className="page-hd-left"><h1>Invitation <em>sent</em></h1><p>The user will receive an activation email shortly</p></div>
        <div className="page-hd-right">
          <button className="btn btn-ghost" onClick={() => { onSubPage("list"); load(); }}>← All users</button>
          <button className="btn btn-primary" onClick={() => { setForm({ first_name: "", last_name: "", email: "", phone_number: "", gender: "" }); onSubPage("invite"); }}>+ Invite another</button>
        </div>
      </div>
      {resultData && <ResultCard data={resultData} type="user" />}
    </div>
  );

  const tabBtn = (key, label, count) => (
    <button
      key={key}
      onClick={() => setTab(key)}
      style={{ padding: "7px 18px", borderRadius: "var(--r6)", border: `1.5px solid ${tab === key ? "var(--v)" : "var(--line2)"}`, background: tab === key ? "var(--v)" : "var(--card)", color: tab === key ? "#fff" : "var(--ink2)", fontFamily: "var(--f)", fontSize: 13, fontWeight: tab === key ? 600 : 400, cursor: "pointer", display: "flex", alignItems: "center", gap: 6 }}
    >
      {label}
      {count !== undefined && <span style={{ fontSize: 11, padding: "1px 6px", borderRadius: 10, background: tab === key ? "rgba(255,255,255,.25)" : "var(--v-l)", color: tab === key ? "#fff" : "var(--v)", fontWeight: 600, lineHeight: 1.4 }}>{count}</span>}
    </button>
  );

  return (
    <div className="page active" id="page-users">
      <div className="page-hd">
        <div className="page-hd-left"><h1>Team Management</h1><p>Manage platform users and pending invitations</p></div>
        <div className="page-hd-right">
          <button className="btn btn-primary" onClick={() => onSubPage("invite")}>+ Add New User</button>
        </div>
      </div>

      {/* Tabs */}
      <div style={{ display: "flex", gap: 4, marginBottom: 16 }}>
        {tabBtn("members", "Members", loading ? undefined : activeRows.length)}
        {tabBtn("invites", "Invites", invLoading ? undefined : pendingInvites.length)}
        {tabBtn("suspended", "Suspended", loading ? undefined : suspendedRows.length)}
      </div>

      {tab === "members" && (
        <div className="card">
          <div className="card-head">
            <h3>Active Members</h3>
            <div style={{ display: "flex", gap: 8 }}>
              <div className="search-bar" style={{ width: 220 }}>
                <input placeholder="Search…" value={q} onChange={(e) => setQ(e.target.value)} />
              </div>
              <button className="btn btn-ghost btn-sm" onClick={load}>↻</button>
            </div>
          </div>
          <div className="card-body" style={{ padding: 0 }}>
            <div className="tbl-wrap">
              <table className="tbl">
                <thead><tr><th>Full Name</th><th>Email</th><th>Role</th><th>Status</th><th>Date Created</th><th>Action</th></tr></thead>
                <tbody>
                  {loading ? (
                    <tr><td colSpan={6} className="tbl-empty"><p>Loading…</p></td></tr>
                  ) : visible.length === 0 ? (
                    <tr><td colSpan={6} className="tbl-empty"><p>No active users</p><span>Active users will appear here</span></td></tr>
                  ) : visible.map((u) => {
                    const fullName = u.full_name || `${u.first_name || ""} ${u.last_name || ""}`.trim() || "—";
                    return (
                      <tr key={u.id} onClick={() => openUserDetail(u)}>
                        <td>
                          <div style={{ display: "flex", alignItems: "center", gap: 9 }}>
                            <div style={{ width: 30, height: 30, borderRadius: "50%", background: "var(--v-l)", display: "flex", alignItems: "center", justifyContent: "center", fontSize: 10, fontWeight: 600, color: "var(--v-d)", flexShrink: 0 }}>{initials(fullName)}</div>
                            <span>{fullName}</span>
                          </div>
                        </td>
                        <td style={{ fontFamily: "var(--fm)", fontSize: 11 }}>{u.email || "—"}</td>
                        <td>{u.user_type || u.role || "—"}</td>
                        <td><span className={`pill ${statusPill(u.status)}`}>{u.status || "ACTIVE"}</span></td>
                        <td style={{ fontSize: 11, color: "var(--ink3)" }}>{fmtDate(u.created_at || u.date_joined)}</td>
                        <td>
                          <button className="btn btn-ghost btn-sm" onClick={(e) => { e.stopPropagation(); suspendUser(u.id, fullName); }}>⋯</button>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}

      {tab === "invites" && (
        <div className="card">
          <div className="card-head">
            <h3>Pending Invitations</h3>
            <button className="btn btn-ghost btn-sm" onClick={loadInvites}>↻ Reload</button>
          </div>
          <div className="card-body" style={{ padding: 0 }}>
            <div className="tbl-wrap">
              <table className="tbl">
                <thead><tr><th>Full Name</th><th>Email</th><th>Role</th><th>Status</th><th>Action</th></tr></thead>
                <tbody>
                  {invLoading ? (
                    <tr><td colSpan={5} className="tbl-empty"><p>Loading…</p></td></tr>
                  ) : pendingInvites.length === 0 ? (
                    <tr><td colSpan={5} className="tbl-empty"><p>No pending invitations</p><span>Invitations sent to new users will appear here</span></td></tr>
                  ) : pendingInvites.map((inv, i) => {
                    const fullName = inv.full_name || `${inv.first_name || ""} ${inv.last_name || ""}`.trim() || "—";
                    const statusKey = (inv.status || "PENDING").toUpperCase();
                    return (
                      <tr key={inv.id || inv.email || i}>
                        <td>{fullName !== "—" ? fullName : inv.email || "—"}</td>
                        <td style={{ fontFamily: "var(--fm)", fontSize: 11 }}>{inv.email || "—"}</td>
                        <td>{inv.role || inv.user_type || "—"}</td>
                        <td>
                          <span className={`pill ${statusKey === "PENDING" ? "pill-amber" : statusKey === "REJECTED" ? "pill-red" : statusKey === "ACCEPTED" ? "pill-green" : "pill-gray"}`}>
                            {statusKey}
                          </span>
                        </td>
                        <td>
                          <button className="btn btn-ghost btn-sm" onClick={() => { call("POST", EP.INVITATION_RESEND(inv.id), {}).then((r) => { showToast(r.ok ? "Invitation resent" : "Could not resend", r.ok ? "ok" : "err"); }); }}>
                            Resend
                          </button>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}

      {tab === "suspended" && (
        <div className="card">
          <div className="card-head">
            <h3>Suspended &amp; Deactivated</h3>
            <div style={{ display: "flex", gap: 8 }}>
              <div className="search-bar" style={{ width: 220 }}>
                <input placeholder="Search…" value={q} onChange={(e) => setQ(e.target.value)} />
              </div>
              <button className="btn btn-ghost btn-sm" onClick={load}>↻</button>
            </div>
          </div>
          <div className="card-body" style={{ padding: 0 }}>
            <div className="tbl-wrap">
              <table className="tbl">
                <thead><tr><th>Full Name</th><th>Email</th><th>Role</th><th>Status</th><th>Date</th><th>Actions</th></tr></thead>
                <tbody>
                  {loading ? (
                    <tr><td colSpan={6} className="tbl-empty"><p>Loading…</p></td></tr>
                  ) : suspendedVisible.length === 0 ? (
                    <tr><td colSpan={6} className="tbl-empty"><p>No suspended users</p><span>Suspended and deactivated accounts appear here</span></td></tr>
                  ) : suspendedVisible.map((u) => {
                    const fullName = u.full_name || `${u.first_name || ""} ${u.last_name || ""}`.trim() || "—";
                    return (
                      <tr key={u.id} onClick={() => openUserDetail(u)}>
                        <td>
                          <div style={{ display: "flex", alignItems: "center", gap: 9 }}>
                            <div style={{ width: 30, height: 30, borderRadius: "50%", background: "var(--red-bg)", display: "flex", alignItems: "center", justifyContent: "center", fontSize: 10, fontWeight: 600, color: "var(--red)", flexShrink: 0 }}>{initials(fullName)}</div>
                            <span>{fullName}</span>
                          </div>
                        </td>
                        <td style={{ fontFamily: "var(--fm)", fontSize: 11 }}>{u.email || "—"}</td>
                        <td>{u.user_type || u.role || "—"}</td>
                        <td><span className={`pill ${statusPill(u.status)}`}>{u.status}</span></td>
                        <td style={{ fontSize: 11, color: "var(--ink3)" }}>{fmtDate(u.created_at || u.date_joined)}</td>
                        <td>
                          <div style={{ display: "flex", gap: 5 }}>
                            {u.status === "SUSPENDED" && (
                              <button className="btn btn-secondary btn-sm" onClick={(e) => { e.stopPropagation(); call("POST", EP.USER_REACTIVATE(u.id), {}).then((r) => { if (r.ok) { showToast("User reactivated", "ok"); load(); } }); }}>Reactivate</button>
                            )}
                            {u.status === "LOCKED" && (
                              <button className="btn btn-secondary btn-sm" onClick={(e) => { e.stopPropagation(); call("POST", EP.USER_UNLOCK(u.id), {}).then((r) => { if (r.ok) { showToast("Account unlocked", "ok"); load(); } }); }}>Unlock</button>
                            )}
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// ─── RBACPage ─────────────────────────────────────────────────────────────────

function RBACPage({ call, showToast, openDetail, addActivity, subPage, onSubPage }) {
  const [roles, setRoles]           = useState([]);
  const [changeReqs, setChangeReqs] = useState([]);
  const [loadingRoles, setLoadingRoles] = useState(false);
  const [rolesPage, setRolesPage]   = useState(1);
  const [rolesCount, setRolesCount] = useState(0);
  const [notice, setNotice]         = useState(null);
  const [resultData, setResultData] = useState(null);
  const [permTags, setPermTags]     = useState([]);
  const [permInput, setPermInput]   = useState("");
  const [form, setForm]             = useState({ name: "", description: "" });
  const [saving, setSaving]         = useState(false);

  const loadRoles = useCallback(async (page = 1) => {
    setLoadingRoles(true);
    const r = await call("GET", `${EP.PLATFORM_ROLES}?page=${page}`);
    setLoadingRoles(false);
    if (r.ok) {
      setRoles(r.data || []);
      setRolesCount(r.pagination?.totalItems || 0);
      setRolesPage(page);
    }
  }, [call]);

  const loadChangeReqs = useCallback(async () => {
    const r = await call("GET", EP.PLATFORM_CHANGE_REQUESTS);
    if (r.ok) setChangeReqs(r.data?.results || r.data || []);
  }, [call]);

  useEffect(() => {
    if (subPage === "list") { loadRoles(); loadChangeReqs(); }
  }, [subPage]);

  function addPerm(val) {
    const v = val.trim().toLowerCase().replace(/\s+/g, "");
    if (!v || permTags.includes(v)) return;
    setPermTags((t) => [...t, v]);
    setPermInput("");
  }

  async function create() {
    setNotice(null);
    if (!form.name) { setNotice({ type: "error", title: "Missing name", msg: "Role name is required." }); return; }
    if (!permTags.length) { setNotice({ type: "error", title: "No permissions", msg: "Add at least one permission key." }); return; }
    setSaving(true);
    const r = await call("POST", EP.PLATFORM_ROLES, { name: form.name, description: form.description, permission_keys: permTags });
    setSaving(false);
    if (r.ok || r.status === 201) {
      addActivity("🛡", `Role template created: ${form.name}`);
      setResultData(r.data);
      setPermTags([]);
      onSubPage("result");
      showToast("Role template created", "ok");
    } else {
      setNotice({ type: "error", title: "Could not create role", msg: flattenErrors(r.data) });
    }
  }

  function openRoleDetail(role) {
    call("GET", EP.PLATFORM_ROLE(role.id)).then((r) => {
      if (!r.ok) return;
      const d = r.data;
      openDetail(
        "Role template",
        <div>
          <ResultCard data={d} type="role" />
          {d.permission_keys?.length > 0 && (
            <div style={{ marginTop: 14 }}>
              <div style={{ fontSize: 12, fontWeight: 600, color: "var(--ink3)", textTransform: "uppercase", letterSpacing: ".06em", marginBottom: 8 }}>
                Permission keys ({d.permission_keys.length})
              </div>
              <div className="perm-tags" style={{ maxHeight: "none" }}>
                {d.permission_keys.map((k) => (
                  <div className="perm-tag" key={k}>{k}</div>
                ))}
              </div>
            </div>
          )}
        </div>
      );
    });
  }

  async function approveReq(id) {
    const r = await call("POST", EP.PLATFORM_CHANGE_REQUEST_DECIDE(id), { action: "APPROVE" });
    if (r.ok) { showToast("Request approved", "ok"); loadChangeReqs(); }
    else showToast(r.data?.detail || "Could not approve", "err");
  }

  async function denyReq(id) {
    const reason = prompt("Reason for denial:");
    if (!reason) return;
    const r = await call("POST", EP.PLATFORM_CHANGE_REQUEST_DECIDE(id), { action: "DENY", reason });
    if (r.ok) { showToast("Request denied"); loadChangeReqs(); }
    else showToast(r.data?.detail || "Could not deny", "err");
  }

  if (subPage === "create") return (
    <div className="page active" id="page-rbac">
      <div className="page-hd">
        <div className="page-hd-left"><h1>Create <em>role template</em></h1><p>Define a reusable role with permission keys</p></div>
        <div className="page-hd-right"><button className="btn btn-ghost" onClick={() => { onSubPage("list"); loadRoles(); }}>← Back</button></div>
      </div>
      <div style={{ maxWidth: 700 }}>
        <div className="card">
          <div className="card-head"><div><h3>Role details</h3><p>Name this role and assign permission keys</p></div></div>
          <div className="card-body">
            {notice && <Notice {...notice} onClear={() => setNotice(null)} />}
            <div className="form-row"><label>Role name <span className="required">*</span></label><input type="text" placeholder="e.g. Branch Finance Officer" value={form.name} onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))} /></div>
            <div className="form-row"><label>Description</label><textarea placeholder="What does this role allow?" value={form.description} onChange={(e) => setForm((f) => ({ ...f, description: e.target.value }))} /></div>
            <div className="form-row">
              <label>Permission keys</label>
              <div style={{ display: "flex", gap: 8, marginBottom: 8 }}>
                <input
                  type="text" placeholder="e.g. finance.invoice.view"
                  value={permInput} onChange={(e) => setPermInput(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && addPerm(permInput)}
                  style={{ flex: 1, height: 36, padding: "0 12px", border: "1.5px solid var(--line2)", borderRadius: "var(--r8)", background: "var(--page)", fontFamily: "var(--fm)", fontSize: 12, outline: "none" }}
                />
                <button className="btn btn-secondary btn-sm" onClick={() => addPerm(permInput)}>Add</button>
              </div>
              <div className="perm-tags">
                {permTags.length === 0
                  ? <span style={{ fontSize: 11, color: "var(--ink3)", fontStyle: "italic" }}>Type a permission key above and click Add</span>
                  : permTags.map((p, i) => (
                    <div className="perm-tag" key={p}>
                      {p}
                      <button className="perm-tag-rm" onClick={() => setPermTags((t) => t.filter((_, j) => j !== i))}>✕</button>
                    </div>
                  ))}
              </div>
              <div className="form-hint">Format: module.resource.action — e.g. students.enrol, finance.invoice.approve</div>
            </div>
            <div className="section-div"><span>Common permissions</span></div>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBottom: 16 }}>
              {["students.manage_branch", "students.enrol", "finance.invoice.view", "finance.invoice.approve", "rbac.roles.manage", "audit.logs.view"].map((p) => (
                <button key={p} className="btn btn-ghost btn-sm" onClick={() => addPerm(p)}>{p}</button>
              ))}
            </div>
            <div className="form-actions">
              <button className="btn btn-ghost" onClick={() => onSubPage("list")}>Cancel</button>
              <button className="btn btn-primary" onClick={create} disabled={saving}>
                {saving ? <span className="spin" /> : null}
                <span>Create role template</span>
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );

  if (subPage === "result") return (
    <div className="page active" id="page-rbac">
      <div className="page-hd">
        <div className="page-hd-left"><h1>Role <em>created</em></h1><p>The role template is now available for assignment</p></div>
        <div className="page-hd-right">
          <button className="btn btn-ghost" onClick={() => { onSubPage("list"); loadRoles(); }}>← All roles</button>
          <button className="btn btn-primary" onClick={() => { setForm({ name: "", description: "" }); onSubPage("create"); }}>+ Create another</button>
        </div>
      </div>
      {resultData && <ResultCard data={resultData} type="role" />}
    </div>
  );

  return (
    <div className="page active" id="page-rbac">
      <div className="page-hd">
        <div className="page-hd-left"><h1>Roles &amp; Permissions</h1><p>Platform-wide role templates and assignments</p></div>
        <div className="page-hd-right">
          <button className="btn btn-primary" onClick={() => onSubPage("create")}>+ Create role template</button>
        </div>
      </div>
      <div className="grid-2">
        <div className="card">
          <div className="card-head"><h3>Role templates</h3><button className="btn btn-ghost btn-sm" onClick={() => loadRoles(1)}>↻ Reload</button></div>
          <div className="card-body" style={{ padding: 0 }}>
            <div className="tbl-wrap">
              <table className="tbl">
                <thead><tr><th>Name</th><th>Permissions</th><th>Scope</th></tr></thead>
                <tbody>
                  {loadingRoles ? (
                    <tr><td colSpan={3} className="tbl-empty"><p>Loading…</p></td></tr>
                  ) : roles.length === 0 ? (
                    <tr><td colSpan={3} className="tbl-empty"><p>No role templates</p><span>Create your first role template</span></td></tr>
                  ) : roles.map((role) => (
                    <tr key={role.id} style={{ cursor: "pointer" }} onClick={() => openRoleDetail(role)}>
                      <td>{role.name || "—"}</td>
                      <td style={{ fontFamily: "var(--fm)", fontSize: 11, color: "var(--ink3)" }}>{role.permissions_count ?? role.permission_keys?.length ?? 0} keys</td>
                      <td><span className="pill pill-violet">{role.scope || "—"}</span></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <Paginator count={rolesCount} page={rolesPage} onPage={loadRoles} />
          </div>
        </div>
        <div className="card">
          <div className="card-head"><h3>Change requests</h3><button className="btn btn-ghost btn-sm" onClick={loadChangeReqs}>↻ Reload</button></div>
          <div className="card-body" style={{ padding: 0 }}>
            <div className="tbl-wrap">
              <table className="tbl">
                <thead><tr><th>School</th><th>Type</th><th>Status</th><th>Actions</th></tr></thead>
                <tbody>
                  {changeReqs.length === 0 ? (
                    <tr><td colSpan={4} className="tbl-empty"><p>No pending requests</p><span>All clear</span></td></tr>
                  ) : changeReqs.map((req) => (
                    <tr key={req.id}>
                      <td style={{ fontSize: 11, fontFamily: "var(--fm)" }}>{req.school || req.school_id || "—"}</td>
                      <td>{req.type || "—"}</td>
                      <td><span className={`pill ${req.status === "PENDING" ? "pill-amber" : req.status === "APPROVED" ? "pill-green" : "pill-gray"}`}>{req.status || "—"}</span></td>
                      <td>
                        {req.status === "PENDING" ? (
                          <div style={{ display: "flex", gap: 5 }}>
                            <button className="btn btn-primary btn-sm" onClick={() => approveReq(req.id)}>Approve</button>
                            <button className="btn btn-danger btn-sm" onClick={() => denyReq(req.id)}>Deny</button>
                          </div>
                        ) : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

// ─── AuditPage ────────────────────────────────────────────────────────────────

function AuditPage({ call }) {
  const [logs, setLogs] = useState([]);
  const [loading, setLoading] = useState(false);
  const [filter, setFilter] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    const r = await call("GET", EP.AUDIT_EVENTS_FILTER(filter));
    setLoading(false);
    if (r.ok) setLogs(r.data?.results || r.data || []);
  }, [call, filter]);

  useEffect(() => { load(); }, [filter]);

  return (
    <div className="page active" id="page-audit">
      <div className="page-hd">
        <div className="page-hd-left"><h1>Audit Logs</h1><p>Immutable record of all platform actions</p></div>
        <div className="page-hd-right">
          <button className="btn btn-secondary btn-sm" onClick={load}>↻ Reload</button>
        </div>
      </div>
      <div className="card">
        <div className="card-head">
          <h3>All events</h3>
          <select
            style={{ height: 30, padding: "0 10px", border: "1px solid var(--line2)", borderRadius: "var(--r6)", fontFamily: "var(--f)", fontSize: 12, color: "var(--ink2)", background: "var(--page)", cursor: "pointer" }}
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          >
            <option value="">All types</option>
            <option value="auth">Auth</option>
            <option value="user">Users</option>
            <option value="school">Schools</option>
            <option value="rbac">RBAC</option>
          </select>
        </div>
        <div className="card-body">
          {loading ? (
            <div className="tbl-empty"><p>Loading…</p></div>
          ) : logs.length === 0 ? (
            <div className="tbl-empty"><p>No audit events found</p><span>Events are recorded as actions occur</span></div>
          ) : logs.slice(0, 50).map((log, i) => {
            const icon = auditIcon(log.action || log.event_type || "");
            return (
              <div className="activity-item" key={i}>
                <div className="act-icon" style={{ background: icon.bg }}>{icon.i}</div>
                <div className="act-body">
                  <div className="act-title">{log.action || log.event_type || "Event"}</div>
                  <div className="act-meta">{log.actor || log.user || "System"} · {log.resource || log.target || ""}</div>
                </div>
                <div className="act-time">{fmtDate(log.created_at || log.timestamp)}</div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

// ─── SessionsPage ────────────────────────────────────────────────────────────

function SessionsPage({ call, showToast, openDetail }) {
  const [tab, setTab]           = useState("sessions");
  const [sessions, setSessions] = useState([]);
  const [attempts, setAttempts] = useState([]);
  const [lockouts, setLockouts] = useState([]);
  const [events, setEvents]     = useState([]);
  const [loading, setLoading]   = useState(false);
  const [stats, setStats]       = useState({ sessions: "—", attempts: "—", lockouts: "—", events: "—" });

  const loadAll = useCallback(async () => {
    const [sr, ar, lr, er] = await Promise.allSettled([
      call("GET", EP.SESSIONS),
      call("GET", EP.AUTH_ATTEMPTS),
      call("GET", EP.ACCOUNT_LOCKOUTS),
      call("GET", EP.AUTH_EVENTS),
    ]);
    const extract = (r) => (r.status === "fulfilled" && r.value.ok) ? (r.value.data?.results || r.value.data || []) : [];
    const cnt     = (r) => { const d = extract(r); return Array.isArray(d) ? d.length : (r.status === "fulfilled" && r.value.ok ? (r.value.data?.count ?? "—") : "—"); };
    setSessions(extract(sr));
    setAttempts(extract(ar));
    setLockouts(extract(lr));
    setEvents(extract(er));
    setStats({ sessions: cnt(sr), attempts: cnt(ar), lockouts: cnt(lr), events: cnt(er) });
  }, [call]);

  const loadTab = useCallback(async () => {
    setLoading(true);
    const map = {
      sessions: EP.SESSIONS,
      attempts: EP.AUTH_ATTEMPTS,
      lockouts: EP.ACCOUNT_LOCKOUTS,
      events:   EP.AUTH_EVENTS,
    };
    const r = await call("GET", map[tab]);
    setLoading(false);
    const rows = r.ok ? (r.data?.results || r.data || []) : [];
    if (tab === "sessions") setSessions(rows);
    else if (tab === "attempts") setAttempts(rows);
    else if (tab === "lockouts") setLockouts(rows);
    else setEvents(rows);
  }, [call, tab]);

  useEffect(() => { loadAll(); }, []);
  useEffect(() => { loadTab(); }, [tab]);

  async function revokeSession(id) {
    const r = await call("DELETE", EP.SESSION(id));
    if (r.ok || r.status === 204) {
      showToast("Session revoked", "ok");
      setSessions((prev) => prev.filter((s) => s.id !== id));
    } else {
      showToast("Failed to revoke session", "err");
    }
  }

  async function unlockUser(userId) {
    const r = await call("POST", EP.USER_UNLOCK(userId), {});
    if (r.ok) {
      showToast("Account unlocked", "ok");
      setLockouts((prev) => prev.map((l) => l.user === userId || l.user_id === userId ? { ...l, status: "UNLOCKED" } : l));
    } else {
      showToast("Failed to unlock account", "err");
    }
  }

  const tabDef = [
    { key: "sessions", label: "Sessions",      icon: "🔑" },
    { key: "attempts", label: "Auth Attempts", icon: "🔍" },
    { key: "lockouts", label: "Lockouts",      icon: "🔒" },
    { key: "events",   label: "Auth Events",   icon: "📡" },
  ];

  return (
    <div className="page active" id="page-sessions">
      <div className="page-hd">
        <div className="page-hd-left"><h1>Sessions Tracker</h1><p>Monitor authentication activity and account security</p></div>
        <div className="page-hd-right">
          <button className="btn btn-secondary btn-sm" onClick={loadAll}>↻ Refresh</button>
        </div>
      </div>

      {/* Stats row */}
      <div className="stats-row" style={{ marginBottom: 20 }}>
        {[
          { icon: "🔑", label: "Active Sessions",  val: stats.sessions },
          { icon: "🔍", label: "Auth Attempts",    val: stats.attempts },
          { icon: "🔒", label: "Locked Accounts",  val: stats.lockouts },
          { icon: "📡", label: "Auth Events",      val: stats.events   },
        ].map((s) => (
          <div className="stat-card" key={s.label}>
            <div className="stat-icon">{s.icon}</div>
            <div className="stat-label">{s.label}</div>
            <div className="stat-val">{s.val}</div>
          </div>
        ))}
      </div>

      {/* Tabs */}
      <div style={{ display: "flex", gap: 4, marginBottom: 16 }}>
        {tabDef.map((t) => (
          <button
            key={t.key}
            onClick={() => setTab(t.key)}
            style={{
              padding: "6px 16px", borderRadius: "var(--r6)", border: "1px solid var(--line2)",
              fontFamily: "var(--f)", fontSize: 13, cursor: "pointer",
              background: tab === t.key ? "var(--v)" : "var(--card)",
              color: tab === t.key ? "#fff" : "var(--ink2)",
              fontWeight: tab === t.key ? 600 : 400,
              transition: "all .15s",
            }}
          >{t.icon} {t.label}</button>
        ))}
      </div>

      <div className="card">
        <div className="card-head">
          <h3>{tabDef.find((t) => t.key === tab)?.label}</h3>
          <button className="btn btn-ghost btn-sm" onClick={loadTab}>↻ Reload</button>
        </div>
        <div className="card-body">
          {loading ? (
            <div className="tbl-empty"><p>Loading…</p></div>
          ) : tab === "sessions" ? (
            sessions.length === 0 ? (
              <div className="tbl-empty"><p>No active sessions</p></div>
            ) : (
              <div className="tbl-wrap">
                <table className="tbl">
                  <thead><tr><th>User</th><th>Device / Agent</th><th>IP Address</th><th>Signed in</th><th>Last active</th><th>Status</th><th></th></tr></thead>
                  <tbody>
                    {sessions.map((s, i) => (
                      <tr key={s.id || i}>
                        <td style={{ fontFamily: "var(--fm)", fontSize: 11 }}>{s.user_email || s.user || "—"}</td>
                        <td style={{ maxWidth: 180, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", fontSize: 11, color: "var(--ink3)" }}>{s.user_agent || s.device || "—"}</td>
                        <td style={{ fontFamily: "var(--fm)", fontSize: 11 }}>{s.ip_address || s.ip || "—"}</td>
                        <td style={{ fontSize: 11, color: "var(--ink3)" }}>{fmtDate(s.created_at || s.signed_in)}</td>
                        <td style={{ fontSize: 11, color: "var(--ink3)" }}>{fmtDate(s.last_seen_at || s.last_active || s.updated_at)}</td>
                        <td><span className={`pill ${statusPill(s.is_active ? "ACTIVE" : "INACTIVE")}`}>{s.is_active ? "ACTIVE" : "INACTIVE"}</span></td>
                        <td>
                          <button className="btn btn-danger btn-sm" onClick={() => revokeSession(s.id)}>Revoke</button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )
          ) : tab === "attempts" ? (
            attempts.length === 0 ? (
              <div className="tbl-empty"><p>No auth attempts recorded</p></div>
            ) : (
              <div className="tbl-wrap">
                <table className="tbl">
                  <thead><tr><th>Email</th><th>IP Address</th><th>Result</th><th>Failure code</th><th>Device / Agent</th><th>Timestamp</th></tr></thead>
                  <tbody>
                    {attempts.map((a, i) => (
                      <tr key={a.id || i}>
                        <td style={{ fontFamily: "var(--fm)", fontSize: 11 }}>{a.email || a.user_email || "—"}</td>
                        <td style={{ fontFamily: "var(--fm)", fontSize: 11 }}>{a.ip_address || a.ip || "—"}</td>
                        <td>
                          <span className={`pill ${a.success || a.result === "SUCCESS" ? "pill-green" : "pill-red"}`}>
                            {a.result || (a.success ? "SUCCESS" : "FAILED")}
                          </span>
                        </td>
                        <td style={{ fontFamily: "var(--fm)", fontSize: 11, color: "var(--ink3)" }}>{a.failure_code || "—"}</td>
                        <td style={{ maxWidth: 160, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", fontSize: 11, color: "var(--ink3)" }}>{a.user_agent || a.device || "—"}</td>
                        <td style={{ fontSize: 11, color: "var(--ink3)" }}>{fmtDate(a.created_at || a.timestamp)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )
          ) : tab === "lockouts" ? (
            lockouts.length === 0 ? (
              <div className="tbl-empty"><p>No account lockouts</p></div>
            ) : (
              <div className="tbl-wrap">
                <table className="tbl">
                  <thead><tr><th>User</th><th>Locked at</th><th>Reason</th><th>Failed attempts</th><th>Status</th><th></th></tr></thead>
                  <tbody>
                    {lockouts.map((l, i) => (
                      <tr key={l.id || i}>
                        <td style={{ fontFamily: "var(--fm)", fontSize: 11 }}>{l.user_email || l.user || "—"}</td>
                        <td style={{ fontSize: 11, color: "var(--ink3)" }}>{fmtDate(l.locked_at || l.created_at)}</td>
                        <td style={{ fontSize: 12, color: "var(--ink2)" }}>{l.reason || l.locked_reason || "Too many failed attempts"}</td>
                        <td style={{ fontFamily: "var(--fm)", fontSize: 13, textAlign: "center" }}>{l.failure_count ?? l.failed_attempts ?? l.attempt_count ?? "—"}</td>
                        <td><span className={`pill ${statusPill(l.status || "LOCKED")}`}>{l.status || "LOCKED"}</span></td>
                        <td>
                          {(l.status || "LOCKED").toUpperCase() === "LOCKED" && (
                            <button className="btn btn-primary btn-sm" onClick={() => unlockUser(l.user_id || l.user)}>Unlock</button>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )
          ) : (
            // Auth Events feed
            events.length === 0 ? (
              <div className="tbl-empty"><p>No auth events recorded</p></div>
            ) : (
              events.slice(0, 60).map((ev, i) => {
                const icon = auditIcon(ev.event_type || ev.action || "auth");
                return (
                  <div className="activity-item" key={i}>
                    <div className="act-icon" style={{ background: icon.bg }}>{icon.i}</div>
                    <div className="act-body">
                      <div className="act-title">{ev.event_type || ev.action || "Auth event"}</div>
                      <div className="act-meta">{ev.user_email || ev.user || "System"} {ev.ip_address ? `· ${ev.ip_address}` : ""}</div>
                    </div>
                    <div className="act-time">{fmtDate(ev.created_at || ev.timestamp)}</div>
                  </div>
                );
              })
            )
          )}
        </div>
      </div>
    </div>
  );
}

// ─── Paginator ───────────────────────────────────────────────────────────────

function Paginator({ count, page, pageSize = 10, onPage }) {
  const total = Math.ceil(count / pageSize);
  if (total <= 1) return null;

  const delta = 2;
  const left  = Math.max(1, page - delta);
  const right = Math.min(total, page + delta);
  const pages = [];
  for (let i = left; i <= right; i++) pages.push(i);

  return (
    <div style={{ display: "flex", alignItems: "center", gap: 4, padding: "10px 16px", borderTop: "1px solid var(--line2)", justifyContent: "flex-end", fontSize: 12 }}>
      <span style={{ color: "var(--ink3)", marginRight: 8 }}>
        {Math.min((page - 1) * pageSize + 1, count)}–{Math.min(page * pageSize, count)} of {count}
      </span>
      <button className="btn btn-ghost btn-sm" disabled={page <= 1} onClick={() => onPage(page - 1)}>‹ Prev</button>
      {left > 1 && <button className="btn btn-ghost btn-sm" onClick={() => onPage(1)}>1</button>}
      {left > 2 && <span style={{ color: "var(--ink3)", padding: "0 4px" }}>…</span>}
      {pages.map((p) => (
        <button key={p} className="btn btn-ghost btn-sm"
          style={{ fontWeight: p === page ? 700 : 400, color: p === page ? "var(--v)" : "var(--ink2)", background: p === page ? "var(--v-l)" : "transparent", minWidth: 28 }}
          onClick={() => onPage(p)}>{p}</button>
      ))}
      {right < total - 1 && <span style={{ color: "var(--ink3)", padding: "0 4px" }}>…</span>}
      {right < total && <button className="btn btn-ghost btn-sm" onClick={() => onPage(total)}>{total}</button>}
      <button className="btn btn-ghost btn-sm" disabled={page >= total} onClick={() => onPage(page + 1)}>Next ›</button>
    </div>
  );
}

// ─── PlatformRoleDetailPanel ─────────────────────────────────────────────────

function PlatformRoleDetailPanel({ role, call, showToast, onSaved }) {
  const [detail, setDetail]     = useState(null);
  const [form, setForm]         = useState({ permission_keys: [], group_ids: [] });
  const [pkInput, setPkInput]   = useState("");
  const [allPerms, setAllPerms] = useState([]);
  const [allGroups, setAllGroups] = useState([]);
  const [loading, setLoading]   = useState(true);
  const [saving, setSaving]     = useState(false);

  useEffect(() => {
    Promise.all([
      call("GET", EP.PLATFORM_ROLE(role.id)),
      call("GET", `${EP.PERMISSIONS}?page_size=200`),
      call("GET", `${EP.PERMISSION_GROUPS}?page_size=200`),
    ]).then(([rd, rp, rg]) => {
      if (rd.ok) {
        const d = rd.data;
        setDetail(d);
        setForm({
          permission_keys: (d.role_permissions || []).filter((p) => p.granted).map((p) => p.permission_key),
          group_ids: (d.role_groups || []).map((g) => String(g.group.id)),
        });
      }
      if (rp.ok) setAllPerms(rp.data?.results || rp.data || []);
      if (rg.ok) setAllGroups(rg.data?.results || rg.data || []);
      setLoading(false);
    });
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  async function save() {
    setSaving(true);
    const payload = {
      permission_keys: form.permission_keys,
      group_ids: form.group_ids,
    };
    const r = await call("PATCH", EP.PLATFORM_ROLE(role.id), payload);
    if (r.ok) { showToast("Role permissions updated", "ok"); onSaved(); }
    else showToast(r.data?.detail || flattenErrors(r.data) || "Could not update", "err");
    setSaving(false);
  }

  const pkSuggestions = allPerms
    .filter((p) => pkInput && p.key.toLowerCase().includes(pkInput.toLowerCase()) && !form.permission_keys.includes(p.key))
    .slice(0, 10);

  if (loading) return <div style={{ padding: 20, textAlign: "center", color: "var(--ink3)" }}>Loading…</div>;

  return (
    <div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBottom: 16 }}>
        <span className={`pill ${statusPill(detail?.status || "ACTIVE")}`}>{detail?.status || "ACTIVE"}</span>
        {detail?.is_system_role && <span className="pill pill-amber">System</span>}
        {detail?.is_locked && <span className="pill pill-red">Locked</span>}
        <span style={{ fontSize: 11, color: "var(--ink3)" }}>v{detail?.version}</span>
      </div>

      <div className="section-div"><span>Direct permission keys</span></div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 6, padding: "8px 10px", border: "1.5px solid var(--line2)", borderRadius: "var(--r8)", background: "var(--card)", minHeight: 40, marginBottom: 6 }}>
        {form.permission_keys.map((k) => (
          <span key={k} style={{ display: "flex", alignItems: "center", gap: 4, padding: "3px 8px", borderRadius: 4, background: "var(--v-l)", color: "var(--v)", fontFamily: "var(--fm)", fontSize: 11 }}>
            {k}
            <button style={{ background: "none", border: "none", cursor: "pointer", color: "var(--ink3)", fontSize: 10, padding: 0, lineHeight: 1 }}
              onClick={() => setForm((f) => ({ ...f, permission_keys: f.permission_keys.filter((x) => x !== k) }))}>✕</button>
          </span>
        ))}
        <input placeholder="Type key and press Enter…" value={pkInput}
          onChange={(e) => setPkInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              const k = pkInput.trim();
              if (k && !form.permission_keys.includes(k)) setForm((f) => ({ ...f, permission_keys: [...f.permission_keys, k] }));
              setPkInput("");
            }
          }}
          style={{ border: "none", outline: "none", fontFamily: "var(--fm)", fontSize: 12, background: "transparent", minWidth: 180, flex: 1 }} />
      </div>
      {pkSuggestions.length > 0 && (
        <div style={{ marginBottom: 8, display: "flex", flexWrap: "wrap", gap: 4 }}>
          {pkSuggestions.map((p) => (
            <button key={p.key} className="btn btn-ghost btn-sm" style={{ fontFamily: "var(--fm)", fontSize: 11 }}
              onClick={() => { setForm((f) => ({ ...f, permission_keys: [...f.permission_keys, p.key] })); setPkInput(""); }}>
              {p.key}
            </button>
          ))}
        </div>
      )}

      <div className="section-div" style={{ marginTop: 10 }}><span>Permission groups</span></div>
      {allGroups.length === 0 ? (
        <p style={{ fontSize: 12, color: "var(--ink3)" }}>No permission groups available.</p>
      ) : (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBottom: 8 }}>
          {allGroups.map((g) => {
            const active = form.group_ids.includes(String(g.id));
            return (
              <button key={g.id}
                onClick={() => setForm((f) => ({
                  ...f,
                  group_ids: active
                    ? f.group_ids.filter((id) => id !== String(g.id))
                    : [...f.group_ids, String(g.id)],
                }))}
                style={{ padding: "4px 10px", borderRadius: 4, cursor: "pointer", fontFamily: "var(--f)", fontSize: 12, border: `1.5px solid ${active ? "var(--v)" : "var(--line2)"}`, background: active ? "var(--v-l)" : "var(--card)", color: active ? "var(--v)" : "var(--ink2)", fontWeight: active ? 600 : 400 }}>
                {g.name}{g.permissions_count !== undefined ? ` (${g.permissions_count})` : ""}{active ? " ✓" : ""}
              </button>
            );
          })}
        </div>
      )}

      <div style={{ display: "flex", gap: 8, marginTop: 16, alignItems: "center" }}>
        <button className="btn btn-primary btn-sm" onClick={save} disabled={saving || detail?.is_locked}>
          {saving ? "Saving…" : "Save changes"}
        </button>
        {detail?.is_locked && <span style={{ fontSize: 12, color: "var(--red)" }}>Role is locked</span>}
      </div>
    </div>
  );
}

// ─── PermissionsPage helpers ─────────────────────────────────────────────────

function SensBadge({ level }) {
  const styles = {
    NORMAL:   { bg: "var(--v-l)",    color: "var(--v)" },
    SENSITIVE:{ bg: "var(--red-bg)", color: "var(--amber)" },
    CRITICAL: { bg: "var(--red-bg)", color: "var(--red)" },
  };
  const s = styles[level] || styles.NORMAL;
  return <span style={{ fontSize: 11, padding: "2px 7px", borderRadius: 4, background: s.bg, color: s.color, fontWeight: 600 }}>{level || "NORMAL"}</span>;
}

// ─── PermissionsPage ─────────────────────────────────────────────────────────

function GroupDetailPanel({ group, call, showToast, onSaved }) {
  const [form, setForm]     = useState({
    name:            group.name        || "",
    description:     group.description || "",
    permission_keys: (group.permissions || []).map((p) => p.key),
    is_active:       group.is_active   ?? true,
  });
  const [pkInput, setPkInput] = useState("");
  const [allPerms, setAllPerms] = useState([]);
  const [saving, setSaving]   = useState(false);

  useEffect(() => {
    call("GET", EP.PERMISSIONS).then((r) => {
      if (r.ok) setAllPerms(r.data?.results || r.data || []);
    });
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  async function save() {
    setSaving(true);
    const r = await call("PATCH", EP.PERMISSION_GROUP(group.id), form);
    if (r.ok) { showToast("Group updated", "ok"); onSaved(); }
    else showToast(r.data?.detail || r.data?.name?.[0] || "Could not update", "err");
    setSaving(false);
  }

  async function deleteGroup() {
    if (group.is_system) { showToast("System groups cannot be deleted", "err"); return; }
    if (!confirm(`Delete group "${group.name}"?`)) return;
    const r = await call("DELETE", EP.PERMISSION_GROUP(group.id));
    if (r.ok) { showToast("Group deleted", "ok"); onSaved(); }
    else showToast(r.data?.detail || "Could not delete", "err");
  }

  function addTag() {
    const k = pkInput.trim();
    if (!k || form.permission_keys.includes(k)) return;
    setForm((f) => ({ ...f, permission_keys: [...f.permission_keys, k] }));
    setPkInput("");
  }

  const suggestions = allPerms
    .filter((p) => pkInput && p.key.includes(pkInput) && !form.permission_keys.includes(p.key))
    .slice(0, 10);

  return (
    <div>
      <div className="form-row">
        <label>Name</label>
        <input type="text" value={form.name} disabled={group.is_system} onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))} />
      </div>
      <div className="form-row">
        <label>Description</label>
        <input type="text" value={form.description} onChange={(e) => setForm((f) => ({ ...f, description: e.target.value }))} />
      </div>
      <div className="form-row">
        <label style={{ display: "flex", alignItems: "center", gap: 8, fontWeight: 400, cursor: "pointer" }}>
          <input type="checkbox" checked={form.is_active} onChange={(e) => setForm((f) => ({ ...f, is_active: e.target.checked }))} /> Active
        </label>
      </div>
      <div className="section-div"><span>Permission keys</span></div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 6, padding: "8px 10px", border: "1.5px solid var(--line2)", borderRadius: "var(--r8)", background: "var(--card)", minHeight: 40, marginBottom: 6 }}>
        {form.permission_keys.map((k) => (
          <span key={k} style={{ display: "flex", alignItems: "center", gap: 4, padding: "3px 8px", borderRadius: 4, background: "var(--v-l)", color: "var(--v)", fontFamily: "var(--fm)", fontSize: 11 }}>
            {k}
            <button style={{ background: "none", border: "none", cursor: "pointer", color: "var(--ink3)", fontSize: 10, padding: 0, lineHeight: 1 }}
              onClick={() => setForm((f) => ({ ...f, permission_keys: f.permission_keys.filter((x) => x !== k) }))}>✕</button>
          </span>
        ))}
        <input placeholder="Type key and press Enter…" value={pkInput}
          onChange={(e) => setPkInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); addTag(); } }}
          style={{ border: "none", outline: "none", fontFamily: "var(--fm)", fontSize: 12, background: "transparent", minWidth: 180, flex: 1 }} />
      </div>
      {suggestions.length > 0 && (
        <div style={{ marginBottom: 10, display: "flex", flexWrap: "wrap", gap: 4 }}>
          {suggestions.map((p) => (
            <button key={p.key} className="btn btn-ghost btn-sm" style={{ fontFamily: "var(--fm)", fontSize: 11 }}
              onClick={() => { setForm((f) => ({ ...f, permission_keys: [...f.permission_keys, p.key] })); setPkInput(""); }}>
              {p.key}
            </button>
          ))}
        </div>
      )}
      <div style={{ display: "flex", gap: 8, marginTop: 16 }}>
        <button className="btn btn-primary btn-sm" onClick={save} disabled={saving}>{saving ? "Saving…" : "Save changes"}</button>
        {!group.is_system && (
          <button className="btn btn-ghost btn-sm" style={{ color: "var(--red)" }} onClick={deleteGroup}>Delete group</button>
        )}
      </div>
    </div>
  );
}

function PermissionsPage({ call, showToast, openDetail }) {
  const [tab, setTab]       = useState("permissions");

  // ── Permissions tab
  const [perms, setPerms]               = useState([]);
  const [permsLoading, setPermsLoading] = useState(false);
  const [permsPage, setPermsPage]       = useState(1);
  const [permsCount, setPermsCount]     = useState(0);
  const [permQ, setPermQ]               = useState("");
  const [permModule, setPermModule]     = useState("");
  const [showNewPerm, setShowNewPerm]   = useState(false);
  const [permForm, setPermForm]         = useState({ key: "", module_key: "", action: "", description: "", sensitivity_level: "NORMAL", is_restricted: false, is_active: true });
  const [permSaving, setPermSaving]     = useState(false);

  // ── Groups tab
  const [groups, setGroups]               = useState([]);
  const [groupsLoading, setGroupsLoading] = useState(false);
  const [groupsPage, setGroupsPage]       = useState(1);
  const [groupsCount, setGroupsCount]     = useState(0);
  const [groupQ, setGroupQ]               = useState("");
  const [showNewGroup, setShowNewGroup]   = useState(false);
  const [groupForm, setGroupForm]         = useState({ name: "", description: "", permission_keys: [] });
  const [groupSaving, setGroupSaving]     = useState(false);
  const [gpkInput, setGpkInput]           = useState("");

  async function loadPerms(page = 1) {
    setPermsLoading(true);
    const r = await call("GET", `${EP.PERMISSIONS}?page=${page}`);
    if (r.ok) {
      setPerms(r.data || []);
      setPermsCount(r.pagination?.totalItems || 0);
      setPermsPage(page);
    } else {
      showToast("Could not load permissions", "err");
    }
    setPermsLoading(false);
  }

  async function loadGroups(page = 1) {
    setGroupsLoading(true);
    const r = await call("GET", `${EP.PERMISSION_GROUPS}?page=${page}`);
    if (r.ok) {
      setGroups(r.data || []);
      setGroupsCount(r.pagination?.totalItems || 0);
      setGroupsPage(page);
    } else {
      showToast("Could not load groups", "err");
    }
    setGroupsLoading(false);
  }

  useEffect(() => { loadPerms(1); loadGroups(1); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  async function createPerm() {
    if (!permForm.key.trim())        { showToast("Permission key required", "err"); return; }
    if (!permForm.module_key.trim()) { showToast("Module key required", "err"); return; }
    if (!permForm.action.trim())     { showToast("Action required", "err"); return; }
    setPermSaving(true);
    const r = await call("POST", EP.PERMISSIONS, permForm);
    if (r.ok) {
      showToast("Permission created", "ok");
      setShowNewPerm(false);
      setPermForm({ key: "", module_key: "", action: "", description: "", sensitivity_level: "NORMAL", is_restricted: false, is_active: true });
      loadPerms(1);
    } else {
      showToast(r.data?.detail || r.data?.key?.[0] || "Could not create permission", "err");
    }
    setPermSaving(false);
  }

  async function deletePerm(key) {
    if (!confirm(`Delete permission "${key}"?`)) return;
    const r = await call("DELETE", EP.PERMISSION(key));
    if (r.ok) { showToast("Permission deleted", "ok"); loadPerms(1); }
    else showToast(r.data?.detail || "Could not delete", "err");
  }

  async function createGroup() {
    if (!groupForm.name.trim()) { showToast("Group name required", "err"); return; }
    setGroupSaving(true);
    const r = await call("POST", EP.PERMISSION_GROUPS, groupForm);
    if (r.ok) {
      showToast("Group created", "ok");
      setShowNewGroup(false);
      setGroupForm({ name: "", description: "", permission_keys: [] });
      loadGroups(1);
    } else {
      showToast(r.data?.detail || r.data?.name?.[0] || "Could not create group", "err");
    }
    setGroupSaving(false);
  }

  async function openGroupDetail(group) {
    const r = await call("GET", EP.PERMISSION_GROUP(group.id));
    if (!r.ok) { showToast("Could not load group details", "err"); return; }
    openDetail(group.name, <GroupDetailPanel group={r.data} call={call} showToast={showToast} onSaved={() => loadGroups(groupsPage)} />);
  }

  const moduleKeys = [...new Set(perms.map((p) => p.module_key).filter(Boolean))].sort();
  const filteredPerms = perms.filter((p) => {
    const matchQ = !permQ || [p.key, p.module_key, p.action, p.description].join(" ").toLowerCase().includes(permQ.toLowerCase());
    const matchM = !permModule || p.module_key === permModule;
    return matchQ && matchM;
  });
  const filteredGroups = groups.filter((g) =>
    !groupQ || [g.name, g.description].join(" ").toLowerCase().includes(groupQ.toLowerCase())
  );
  const gpkSuggestions = perms
    .filter((p) => gpkInput && p.key.includes(gpkInput) && !groupForm.permission_keys.includes(p.key))
    .slice(0, 12);

  function tabBtn(k, label, count) {
    const active = tab === k;
    return (
      <button key={k} onClick={() => setTab(k)}
        style={{ padding: "7px 18px", borderRadius: "var(--r6)", border: `1.5px solid ${active ? "var(--v)" : "var(--line2)"}`, background: active ? "var(--v)" : "var(--card)", color: active ? "#fff" : "var(--ink2)", fontFamily: "var(--f)", fontSize: 13, fontWeight: active ? 600 : 400, cursor: "pointer", display: "flex", alignItems: "center", gap: 6 }}>
        {label}
        {count !== undefined && <span style={{ fontSize: 11, padding: "1px 6px", borderRadius: 10, background: active ? "rgba(255,255,255,.25)" : "var(--v-l)", color: active ? "#fff" : "var(--v)", fontWeight: 600, lineHeight: 1.4 }}>{count}</span>}
      </button>
    );
  }

  return (
    <div className="page active" id="page-permissions">
      <div className="page-hd">
        <div className="page-hd-left"><h1>Permission <em>Registry</em></h1><p>Manage permission keys and permission groups</p></div>
      </div>

      <div style={{ display: "flex", gap: 4, marginBottom: 20 }}>
        {tabBtn("permissions", "Permissions", permsCount)}
        {tabBtn("groups",      "Groups",      groupsCount)}
      </div>

      {/* ── Permissions tab ─────────────────────────────────────────────────── */}
      {tab === "permissions" && (
        <div className="card">
          <div className="card-head">
            <h3>Permission keys</h3>
            <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <select value={permModule} onChange={(e) => setPermModule(e.target.value)}
                style={{ height: 30, padding: "0 10px", border: "1px solid var(--line2)", borderRadius: "var(--r6)", fontFamily: "var(--f)", fontSize: 12, color: "var(--ink2)", background: "var(--page)", cursor: "pointer" }}>
                <option value="">All modules</option>
                {moduleKeys.map((m) => <option key={m} value={m}>{m}</option>)}
              </select>
              <div className="search-bar" style={{ width: 200 }}>
                <input placeholder="Search…" value={permQ} onChange={(e) => setPermQ(e.target.value)} />
              </div>
              <button className="btn btn-primary btn-sm" onClick={() => setShowNewPerm((v) => !v)}>
                {showNewPerm ? "✕ Cancel" : "+ Add permission"}
              </button>
            </div>
          </div>

          {showNewPerm && (
            <div style={{ borderBottom: "1px solid var(--line2)", padding: "16px 20px", background: "var(--page)" }}>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 12, marginBottom: 12 }}>
                <div className="form-row" style={{ margin: 0 }}>
                  <label>Key <span className="required">*</span></label>
                  <input type="text" placeholder="module.resource.action" value={permForm.key}
                    onChange={(e) => setPermForm((f) => ({ ...f, key: e.target.value }))} />
                  <div className="form-hint">Lowercase, dots as separators</div>
                </div>
                <div className="form-row" style={{ margin: 0 }}>
                  <label>Module key <span className="required">*</span></label>
                  <input type="text" placeholder="e.g. rbac" value={permForm.module_key}
                    onChange={(e) => setPermForm((f) => ({ ...f, module_key: e.target.value }))} />
                </div>
                <div className="form-row" style={{ margin: 0 }}>
                  <label>Action <span className="required">*</span></label>
                  <input type="text" placeholder="e.g. view_role" value={permForm.action}
                    onChange={(e) => setPermForm((f) => ({ ...f, action: e.target.value }))} />
                </div>
                <div className="form-row" style={{ margin: 0 }}>
                  <label>Description</label>
                  <input type="text" placeholder="What does this allow?" value={permForm.description}
                    onChange={(e) => setPermForm((f) => ({ ...f, description: e.target.value }))} />
                </div>
                <div className="form-row" style={{ margin: 0 }}>
                  <label>Sensitivity</label>
                  <select value={permForm.sensitivity_level} onChange={(e) => setPermForm((f) => ({ ...f, sensitivity_level: e.target.value }))}
                    style={{ width: "100%", height: 38, padding: "0 10px", border: "1.5px solid var(--line2)", borderRadius: "var(--r8)", fontFamily: "var(--f)", fontSize: 13, color: "var(--ink)", background: "var(--card)", cursor: "pointer" }}>
                    <option value="NORMAL">Normal</option>
                    <option value="SENSITIVE">Sensitive</option>
                    <option value="CRITICAL">Critical</option>
                  </select>
                </div>
                <div className="form-row" style={{ margin: 0, display: "flex", flexDirection: "column", justifyContent: "flex-end", gap: 8 }}>
                  <label style={{ display: "flex", alignItems: "center", gap: 8, fontWeight: 400, cursor: "pointer" }}>
                    <input type="checkbox" checked={permForm.is_restricted} onChange={(e) => setPermForm((f) => ({ ...f, is_restricted: e.target.checked }))} /> Restricted
                  </label>
                  <label style={{ display: "flex", alignItems: "center", gap: 8, fontWeight: 400, cursor: "pointer" }}>
                    <input type="checkbox" checked={permForm.is_active} onChange={(e) => setPermForm((f) => ({ ...f, is_active: e.target.checked }))} /> Active
                  </label>
                </div>
              </div>
              <div style={{ display: "flex", justifyContent: "flex-end" }}>
                <button className="btn btn-primary btn-sm" onClick={createPerm} disabled={permSaving}>{permSaving ? "Saving…" : "Create permission"}</button>
              </div>
            </div>
          )}

          <div className="card-body" style={{ padding: 0 }}>
            {permsLoading ? (
              <div className="tbl-empty"><p>Loading…</p></div>
            ) : perms.length === 0 ? (
              <div className="tbl-empty"><p>No permissions yet</p><span>Add the first permission key above</span></div>
            ) : filteredPerms.length === 0 ? (
              <div className="tbl-empty"><p>No matches</p><span>Try different filters</span></div>
            ) : (
              <div className="tbl-wrap">
                <table className="tbl">
                  <thead><tr><th>Key</th><th>Module</th><th>Action</th><th>Sensitivity</th><th>Restricted</th><th>Active</th><th></th></tr></thead>
                  <tbody>
                    {filteredPerms.map((p) => (
                      <tr key={p.key}>
                        <td style={{ fontFamily: "var(--fm)", fontSize: 12 }}>{p.key}</td>
                        <td><span style={{ fontSize: 11, padding: "2px 7px", borderRadius: 4, background: "var(--v-l)", color: "var(--v)", fontWeight: 600 }}>{p.module_key}</span></td>
                        <td style={{ fontSize: 12, color: "var(--ink2)" }}>{p.action}</td>
                        <td><SensBadge level={p.sensitivity_level} /></td>
                        <td style={{ fontSize: 12, color: "var(--ink3)" }}>{p.is_restricted ? "Yes" : "—"}</td>
                        <td><span style={{ fontSize: 11, padding: "2px 7px", borderRadius: 4, background: p.is_active ? "var(--green-bg)" : "var(--red-bg)", color: p.is_active ? "var(--green)" : "var(--red)", fontWeight: 600 }}>{p.is_active ? "Active" : "Inactive"}</span></td>
                        <td><button className="btn btn-ghost btn-sm" style={{ color: "var(--red)" }} onClick={() => deletePerm(p.key)}>Remove</button></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            <Paginator count={permsCount} page={permsPage} onPage={loadPerms} />
          </div>
        </div>
      )}

      {/* ── Groups tab ──────────────────────────────────────────────────────── */}
      {tab === "groups" && (
        <div className="card">
          <div className="card-head">
            <h3>Permission groups</h3>
            <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <div className="search-bar" style={{ width: 200 }}>
                <input placeholder="Search groups…" value={groupQ} onChange={(e) => setGroupQ(e.target.value)} />
              </div>
              <button className="btn btn-primary btn-sm" onClick={() => setShowNewGroup((v) => !v)}>
                {showNewGroup ? "✕ Cancel" : "+ Add group"}
              </button>
            </div>
          </div>

          {showNewGroup && (
            <div style={{ borderBottom: "1px solid var(--line2)", padding: "16px 20px", background: "var(--page)" }}>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, marginBottom: 12 }}>
                <div className="form-row" style={{ margin: 0 }}>
                  <label>Name <span className="required">*</span></label>
                  <input type="text" placeholder="e.g. Finance Managers" value={groupForm.name}
                    onChange={(e) => setGroupForm((f) => ({ ...f, name: e.target.value }))} />
                </div>
                <div className="form-row" style={{ margin: 0 }}>
                  <label>Description</label>
                  <input type="text" placeholder="What does this group bundle?" value={groupForm.description}
                    onChange={(e) => setGroupForm((f) => ({ ...f, description: e.target.value }))} />
                </div>
              </div>
              <div className="form-row" style={{ margin: "0 0 12px" }}>
                <label>Permission keys</label>
                <div style={{ display: "flex", flexWrap: "wrap", gap: 6, padding: "8px 10px", border: "1.5px solid var(--line2)", borderRadius: "var(--r8)", background: "var(--card)", minHeight: 40 }}>
                  {groupForm.permission_keys.map((k) => (
                    <span key={k} style={{ display: "flex", alignItems: "center", gap: 4, padding: "3px 8px", borderRadius: 4, background: "var(--v-l)", color: "var(--v)", fontFamily: "var(--fm)", fontSize: 11 }}>
                      {k}
                      <button style={{ background: "none", border: "none", cursor: "pointer", color: "var(--ink3)", fontSize: 10, padding: 0, lineHeight: 1 }}
                        onClick={() => setGroupForm((f) => ({ ...f, permission_keys: f.permission_keys.filter((x) => x !== k) }))}>✕</button>
                    </span>
                  ))}
                  <input placeholder="Type key, press Enter, or pick below…" value={gpkInput}
                    onChange={(e) => setGpkInput(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") {
                        e.preventDefault();
                        const k = gpkInput.trim();
                        if (k && !groupForm.permission_keys.includes(k)) {
                          setGroupForm((f) => ({ ...f, permission_keys: [...f.permission_keys, k] }));
                        }
                        setGpkInput("");
                      }
                    }}
                    style={{ border: "none", outline: "none", fontFamily: "var(--fm)", fontSize: 12, background: "transparent", minWidth: 200, flex: 1 }} />
                </div>
                {gpkSuggestions.length > 0 && (
                  <div style={{ marginTop: 4, display: "flex", flexWrap: "wrap", gap: 4 }}>
                    {gpkSuggestions.map((p) => (
                      <button key={p.key} className="btn btn-ghost btn-sm" style={{ fontFamily: "var(--fm)", fontSize: 11 }}
                        onClick={() => { setGroupForm((f) => ({ ...f, permission_keys: [...f.permission_keys, p.key] })); setGpkInput(""); }}>
                        {p.key}
                      </button>
                    ))}
                  </div>
                )}
              </div>
              <div style={{ display: "flex", justifyContent: "flex-end" }}>
                <button className="btn btn-primary btn-sm" onClick={createGroup} disabled={groupSaving}>{groupSaving ? "Saving…" : "Create group"}</button>
              </div>
            </div>
          )}

          <div className="card-body" style={{ padding: 0 }}>
            {groupsLoading ? (
              <div className="tbl-empty"><p>Loading…</p></div>
            ) : groups.length === 0 ? (
              <div className="tbl-empty"><p>No groups yet</p><span>Create a permission group to bundle related keys</span></div>
            ) : filteredGroups.length === 0 ? (
              <div className="tbl-empty"><p>No matches</p></div>
            ) : (
              <div className="tbl-wrap">
                <table className="tbl">
                  <thead><tr><th>Name</th><th>Description</th><th>System</th><th>Permissions</th><th>Active</th><th></th></tr></thead>
                  <tbody>
                    {filteredGroups.map((g) => (
                      <tr key={g.id} style={{ cursor: "pointer" }} onClick={() => openGroupDetail(g)}>
                        <td style={{ fontWeight: 600 }}>{g.name}</td>
                        <td style={{ fontSize: 12, color: "var(--ink3)", maxWidth: 260, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{g.description || "—"}</td>
                        <td>{g.is_system ? <span style={{ fontSize: 11, padding: "2px 7px", borderRadius: 4, background: "var(--red-bg)", color: "var(--amber)", fontWeight: 600 }}>System</span> : "—"}</td>
                        <td style={{ fontWeight: 600 }}>{g.permissions_count ?? 0}</td>
                        <td><span style={{ fontSize: 11, padding: "2px 7px", borderRadius: 4, background: g.is_active ? "var(--green-bg)" : "var(--red-bg)", color: g.is_active ? "var(--green)" : "var(--red)", fontWeight: 600 }}>{g.is_active ? "Active" : "Inactive"}</span></td>
                        <td><button className="btn btn-ghost btn-sm" onClick={(e) => { e.stopPropagation(); openGroupDetail(g); }}>Edit</button></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            <Paginator count={groupsCount} page={groupsPage} onPage={loadGroups} />
          </div>
        </div>
      )}
    </div>
  );
}

// ─── Dashboard (main) ─────────────────────────────────────────────────────────

export default function Dashboard() {
  const navigate = useNavigate();

  const [page, setPage] = useState("home");
  const [subPages, setSubPages] = useState({ schools: "list", branches: "list", users: "list", rbac: "list", platform: "list", sessions: "sessions" });
  const [navCollapsed, setNavCollapsed] = useState(false);
  const [profile, setProfile] = useState(null);
  const [detail, setDetail] = useState({ open: false, title: "", content: null });
  const [lastCall, setLastCall] = useState(null);
  const [debugOpen, setDebugOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [toast, setToast] = useState({ show: false, msg: "", type: "" });
  const [activity, setActivity] = useState([]);

  // ── api wrapper ──────────────────────────────────────────────────────────────
  const call = useCallback(async (method, path, body = null) => {
    try {
      const config = { method, url: path };
      if (body) config.data = body;
      const res = await api(config);
      setLastCall({ method, path, status: res.status, data: res.data });
      return { ok: true, status: res.status, data: res.data?.data ?? res.data, pagination: res.data?.pagination ?? null };
    } catch (err) {
      const status = err.response?.status || 0;
      const rawData = err.response?.data || { detail: err.message };
      setLastCall({ method, path, status, data: rawData });
      return { ok: false, status, data: rawData?.error ?? rawData };
    }
  }, []);

  // ── fetch profile ────────────────────────────────────────────────────────────
  // Uses raw axios (not the api instance) so a 401 here never triggers logout.
  useEffect(() => {
    const token = localStorage.getItem("access_token");
    if (!token) { navigate("/"); return; }
    const payload = decodeJWT(token);
    const uid = payload.user_id || payload.sub;
    if (!uid) return;
    axios
      .get(`${getBaseUrl()}${EP.USER(uid)}`, {
        headers: { Authorization: `Bearer ${token}` },
      })
      .then((res) => setProfile(res.data))
      .catch(() => {
        // Profile fetch failed — not fatal, sidebar shows placeholder.
      });
  }, []);

  // ── helpers ──────────────────────────────────────────────────────────────────
  const showToast = useCallback((msg, type = "") => {
    setToast({ show: true, msg, type });
    setTimeout(() => setToast((t) => ({ ...t, show: false })), 3000);
  }, []);

  const openDetail = useCallback((title, content) => {
    setDetail({ open: true, title, content });
  }, []);
  const closeDetail = useCallback(() => {
    setDetail((d) => ({ ...d, open: false }));
  }, []);

  const addActivity = useCallback((icon, title) => {
    setActivity((prev) => [{ icon, title, time: "Just now" }, ...prev].slice(0, 8));
  }, []);

  const goTo = useCallback((p, sub = null) => {
    setPage(p);
    if (sub) setSubPages((prev) => ({ ...prev, [p]: sub }));
    else setSubPages((prev) => ({ ...prev, [p]: "list" }));
  }, []);

  const setSubPage = useCallback((p, sub) => {
    setSubPages((prev) => ({ ...prev, [p]: sub }));
  }, []);

  // ── env info ─────────────────────────────────────────────────────────────────
  const base = getBaseUrl();
  const env = base.includes("localhost") ? "local" : "staging";
  const envDot = env === "local" ? "var(--blue)" : "var(--amber)";

  // ── profile display ──────────────────────────────────────────────────────────
  const profileName = profile
    ? [profile.first_name, profile.last_name].filter(Boolean).join(" ") || profile.email || "User"
    : "…";
  const profileInitials = initials(profileName);
  const profileRole = profile?.user_type || profile?.role || "Vision Staff";
  const profileStatus = profile?.status || "";
  const profileLastLogin = profile?.last_login ? fmtDate(profile.last_login) : "";

  const PAGE_TITLES = { home: "Dashboard", schools: "Schools", branches: "Branches", users: "Team Management", rbac: "Roles", permissions: "Permissions", platform: "Platform Staff", sessions: "Sessions Tracker", audit: "Audit Logs" };

  const shared = { call, showToast, openDetail, addActivity };

  return (
    <div id="app" className={navCollapsed ? "nav-collapsed" : ""}>
      {/* ── Sidebar ────────────────────────────────────────────────────────── */}
      <div id="sidebar">
        <div className="sb-head">
          <div className="sb-logo">
            <div className="sb-gem">XV</div>
            <div className="sb-brand">X <span>VS</span></div>
          </div>
          <button
            className="sb-toggle"
            onClick={() => setNavCollapsed((v) => !v)}
            title={navCollapsed ? "Expand sidebar" : "Collapse sidebar"}
          >
            {navCollapsed ? "›" : "‹"}
          </button>
        </div>

        {/* Nav */}
        <div className="sb-nav">
          <div className="nav-section">
            <div className="nav-section-label">
              <span className="nsl-full">Overview</span>
              <span className="nsl-abbr">O</span>
            </div>
            <div className={`nav-item${page === "home" ? " active" : ""}`} onClick={() => goTo("home")}>
              <span className="nav-icon">🏠</span><span className="nav-label">Dashboard</span>
            </div>
          </div>
          <div className="nav-section">
            <div className="nav-section-label">
              <span className="nsl-full">Platform</span>
              <span className="nsl-abbr">P</span>
            </div>
            {[
              { key: "schools",     icon: "🏫", label: "Schools" },
              { key: "branches",    icon: "🌿", label: "Branches" },
              { key: "users",       icon: "👥", label: "Users & Staff" },
              { key: "rbac",        icon: "🛡", label: "Roles" },
              { key: "permissions", icon: "🔑", label: "Permissions" },
            ].map((n) => (
              <div key={n.key} className={`nav-item${page === n.key ? " active" : ""}`} onClick={() => goTo(n.key)}>
                <span className="nav-icon">{n.icon}</span><span className="nav-label">{n.label}</span>
              </div>
            ))}
          </div>
          <div className="nav-section">
            <div className="nav-section-label">
              <span className="nsl-full">System</span>
              <span className="nsl-abbr">S</span>
            </div>
            <div className={`nav-item${page === "platform" ? " active" : ""}`} onClick={() => goTo("platform")}>
              <span className="nav-icon">🔐</span><span className="nav-label">Platform Staff</span>
            </div>
            <div className={`nav-item${page === "sessions" ? " active" : ""}`} onClick={() => goTo("sessions")}>
              <span className="nav-icon">🔑</span><span className="nav-label">Sessions Tracker</span>
            </div>
            <div className={`nav-item${page === "audit" ? " active" : ""}`} onClick={() => goTo("audit")}>
              <span className="nav-icon">📋</span><span className="nav-label">Audit Logs</span>
            </div>
          </div>
        </div>

        <div className="sb-bottom">
          {/* User profile row */}
          <div className="sb-user">
            <div className="sb-av">{profileInitials}</div>
            <div className="sb-uinfo">
              <div className="sb-uname">{profileName}</div>
              <div className="sb-urole">{profileRole}</div>
            </div>
          </div>
          {/* Log out */}
          <div className="sb-settings sb-logout" onClick={logout}>
            <span style={{ fontSize: 15, flexShrink: 0 }}>↩</span>
            <span className="sb-settings-label">Log out</span>
          </div>
          {/* API settings */}
          <div className="sb-settings" onClick={() => setSettingsOpen(true)}>
            <span style={{ fontSize: 15, opacity: 0.6, flexShrink: 0 }}>⚙</span>
            <span className="sb-settings-label">API Settings</span>
          </div>
        </div>
      </div>

      {/* ── Topbar ─────────────────────────────────────────────────────────── */}
      <div id="topbar">
        <div className="tb-page-title">{PAGE_TITLES[page] || page}</div>
        <div className={`tb-env-pill ${env}`} onClick={() => setSettingsOpen(true)}>
          <div className="tb-env-dot" style={{ background: envDot }} />
          <span style={{ textTransform: "capitalize" }}>{env}</span>
        </div>
        <div className="tb-notif">🔔<div className="tb-notif-dot" /></div>
      </div>

      {/* ── Main ───────────────────────────────────────────────────────────── */}
      <div id="main">
        {page === "home" && (
          <HomePage
            {...shared}
            profile={profile}
            activity={activity}
            onSubPageNav={(p, sub) => goTo(p, sub)}
          />
        )}
        {page === "schools" && (
          <SchoolsPage
            {...shared}
            subPage={subPages.schools || "list"}
            onSubPage={(sub) => setSubPage("schools", sub)}
          />
        )}
        {page === "branches" && (
          <BranchesPage
            {...shared}
            subPage={subPages.branches || "list"}
            onSubPage={(sub) => setSubPage("branches", sub)}
          />
        )}
        {page === "users" && (
          <UsersPage
            {...shared}
            subPage={subPages.users || "list"}
            onSubPage={(sub) => setSubPage("users", sub)}
          />
        )}
        {page === "rbac" && (
          <RBACPage
            {...shared}
            subPage={subPages.rbac || "list"}
            onSubPage={(sub) => setSubPage("rbac", sub)}
          />
        )}
        {page === "permissions" && <PermissionsPage {...shared} />}
        {page === "platform" && <PlatformStaffPage {...shared} />}
        {page === "sessions" && <SessionsPage {...shared} />}
        {page === "audit" && <AuditPage {...shared} />}
      </div>

      {/* ── Detail panel ───────────────────────────────────────────────────── */}
      <div id="overlay" className={detail.open ? "open" : ""} onClick={closeDetail} />
      <div id="detail-panel" className={detail.open ? "open" : ""}>
        <div className="dp-head">
          <h3>{detail.title}</h3>
          <button className="dp-close" onClick={closeDetail}>✕</button>
        </div>
        <div className="dp-body">{detail.content}</div>
      </div>

      {/* ── Debug panel ────────────────────────────────────────────────────── */}
      {lastCall && (
        <button
          id="debug-toggle"
          className="has-data"
          onClick={() => setDebugOpen((o) => !o)}
        >
          {debugOpen ? "▼ Close" : "▲"} {lastCall.method} {lastCall.path} — {lastCall.status}
        </button>
      )}
      <div id="debug-panel" className={debugOpen ? "open" : ""}>
        <div className="debug-head">
          <span>{lastCall ? `${lastCall.method} ${lastCall.path}` : "Last API call"}</span>
          {lastCall && (
            <span className={`debug-sc ${lastCall.status >= 200 && lastCall.status < 300 ? "ok" : "err"}`}>
              {lastCall.status}
            </span>
          )}
          <button className="debug-close" onClick={() => setDebugOpen(false)}>▼</button>
        </div>
        <div className="debug-body">
          <pre
            className="debug-json"
            dangerouslySetInnerHTML={{
              __html: lastCall
                ? highlight(JSON.stringify(lastCall.data, null, 2))
                : "No requests made yet.",
            }}
          />
        </div>
      </div>

      {/* ── Settings modal ─────────────────────────────────────────────────── */}
      <SettingsModal
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        onSave={() => { showToast("Settings saved"); setSettingsOpen(false); }}
      />

      {/* ── Toast ──────────────────────────────────────────────────────────── */}
      <div
        id="toast"
        className={toast.show ? "show" : ""}
        style={{
          background: toast.type === "err" ? "var(--red)" : toast.type === "ok" ? "var(--green)" : "var(--ink)",
        }}
      >
        {toast.msg}
      </div>
    </div>
  );
}
