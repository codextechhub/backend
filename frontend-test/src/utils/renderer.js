function esc(s) {
  return String(s ?? '').replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function initials(name) {
  if (!name) return '';
  const p = name.trim().split(' ');
  return p.length >= 2 ? p[0][0].toUpperCase() + p[p.length-1][0].toUpperCase() : name.slice(0,2).toUpperCase();
}

export function highlight(json) {
  return json.replace(/("(\\u[a-zA-Z0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d*)?(?:[eE][+\-]?\d+)?)/g, m => {
    let c = 'jn';
    if (/^"/.test(m)) { c = /:$/.test(m) ? 'jk' : 'js'; }
    else if (/true|false/.test(m)) c = 'jb';
    else if (/null/.test(m)) c = 'jnull';
    return `<span class="${c}">${m}</span>`;
  });
}

export function statusStr(c) {
  return { 200:'OK', 201:'Created', 204:'No Content', 400:'Bad Request', 401:'Unauthorized', 403:'Forbidden', 404:'Not Found', 405:'Method Not Allowed', 422:'Unprocessable Entity', 500:'Internal Server Error', 502:'Bad Gateway', 503:'Service Unavailable' }[c] || '';
}

export function statusClass(s) {
  return s < 300 ? 's2' : s < 500 ? 's4' : 's5';
}

function buildErrorCard(status, data) {
  let h = `<div class="r-error"><h4><span style="font-size:16px">✗</span> ${status} — ${esc(statusStr(status))}</h4><div class="r-error-body">`;
  if (typeof data === 'object' && data !== null) {
    const flatten = (obj, prefix = '') => {
      Object.entries(obj).forEach(([k, v]) => {
        const label = prefix ? `${prefix}.${k}` : k;
        if (Array.isArray(v)) { v.forEach(m => h += `<div class="r-error-row"><span class="r-error-field">${esc(label)}</span><span>${esc(String(m))}</span></div>`); }
        else if (typeof v === 'object' && v !== null) { flatten(v, label); }
        else { h += `<div class="r-error-row"><span class="r-error-field">${esc(label)}</span><span>${esc(String(v))}</span></div>`; }
      });
    };
    flatten(data);
  } else {
    h += `<div class="r-error-row"><span>${esc(String(data))}</span></div>`;
  }
  h += '</div></div>';
  return h;
}

function buildTokenCard(label, token, hint) {
  const short = token.slice(0, 28) + '…';
  const safe = esc(token);
  return `<div class="r-token-card"><h4>🔑 ${esc(label)}</h4><div class="r-token-val" title="Click to copy" onclick="navigator.clipboard.writeText('${safe}').then(()=>{const t=document.querySelector('.toast');t.textContent='Token copied';t.classList.add('show');setTimeout(()=>t.classList.remove('show'),2600)})">${esc(short)}</div><div class="r-token-hint">${esc(hint)}</div></div>`;
}

function buildDetailCard(obj, title) {
  if (typeof obj !== 'object' || obj === null) return '';
  const skip = ['password', 'token', 'access', 'refresh'];
  const av = initials(obj.full_name || obj.name || obj.first_name || '');
  let h = `<div class="r-detail-card"><div class="r-dc-hero"><div class="r-dc-av">${av || '?'}</div><div><div class="r-dc-name">${esc(obj.full_name || obj.name || (obj.first_name ? `${obj.first_name} ${obj.last_name || ''}` : title))}</div><div class="r-dc-sub">${esc(obj.email || obj.slug || obj.id || '')}</div></div></div><div class="r-dc-body"><div class="r-dc-rows">`;
  Object.entries(obj).forEach(([k, v]) => {
    if (skip.includes(k) || typeof v === 'object') return;
    const display = typeof v === 'boolean'
      ? `<span class="t-badge ${v}">${v ? 'Yes' : 'No'}</span>`
      : (k.includes('status') || k === 'state')
        ? `<span class="t-badge ${v}">${esc(String(v))}</span>`
        : esc(String(v ?? '—'));
    h += `<div class="r-dc-row"><span class="r-dc-k">${esc(k.replace(/_/g,' '))}</span><span class="r-dc-v">${display}</span></div>`;
  });
  Object.entries(obj).forEach(([k, v]) => {
    if (typeof v === 'object' && v !== null && !Array.isArray(v)) {
      h += `<div class="r-dc-row" style="align-items:flex-start"><span class="r-dc-k">${esc(k.replace(/_/g,' '))}</span><span class="r-dc-v" style="font-size:10px;opacity:.7">${esc(JSON.stringify(v).slice(0,60))}…</span></div>`;
    }
  });
  h += '</div></div></div>';
  return h;
}

function buildTable(items) {
  if (!items.length) return '';
  const skip = ['password', 'access', 'refresh', 'token'];
  const sample = items[0];
  let cols = Object.keys(sample).filter(k => {
    if (skip.includes(k)) return false;
    return typeof sample[k] !== 'object' || sample[k] === null;
  }).slice(0, 7);
  if (!cols.length) cols = Object.keys(sample).slice(0, 5);

  let h = `<div class="r-table-wrap"><table class="r-table"><thead><tr>`;
  cols.forEach(c => h += `<th>${esc(c.replace(/_/g,' '))}</th>`);
  h += `</tr></thead><tbody>`;
  items.slice(0, 50).forEach(row => {
    h += `<tr>`;
    cols.forEach((c) => {
      const v = row[c];
      let cell = '—';
      if (v === null || v === undefined) cell = '—';
      else if (typeof v === 'boolean') cell = `<span class="t-badge ${v}">${v ? 'Yes' : 'No'}</span>`;
      else if (c === 'status' || c === 'state') cell = `<span class="t-badge ${v}">${esc(String(v))}</span>`;
      else if (c === 'id' || c.endsWith('_id')) cell = `<span class="t-id">${esc(String(v).slice(0,12))}…</span>`;
      else cell = esc(String(v));
      h += `<td>${cell}</td>`;
    });
    h += `</tr>`;
  });
  h += `</tbody></table>`;
  if (items.length > 50) h += `<div class="r-table-footer">Showing 50 of ${items.length} records</div>`;
  h += `</div>`;
  return h;
}

export function buildSmartHTML(res, data, ep) {
  const status = res.status;
  let html = '';

  if (status >= 400) {
    html += buildErrorCard(status, data);
    if (typeof data === 'object') html += `<details style="margin-top:12px"><summary style="font-size:11px;color:var(--ink3);cursor:pointer;user-select:none">Raw JSON</summary><pre class="rjson" style="margin-top:8px;font-size:11px">${highlight(JSON.stringify(data, null, 2))}</pre></details>`;
    return html;
  }

  if (status === 204 || data === null || data === undefined) {
    return `<div class="r-success"><div class="r-success-icon">✓</div><div class="r-success-body"><h4>${esc(ep?.l || 'Request')} — success</h4><p>Completed successfully with no response body (${status}).</p></div></div>`;
  }

  const rt = ep?.rt || '';

  if (rt === 'auth' && typeof data === 'object') {
    const name = data.user?.full_name || data.user?.email?.split('@')[0] || 'there';
    const role = data.user?.role || data.user?.user_type || 'User';
    html += `<div class="r-greet"><div class="r-greet-tag">Authentication successful</div><div class="r-greet-name">Welcome back, ${esc(name)}</div><div class="r-greet-sub">${esc(role)} · Signed in ${new Date().toLocaleTimeString()}</div></div>`;
    if (data.access) html += buildTokenCard('Access token', data.access, 'Expires in ~15 min · Auto-injected into future requests');
    if (data.refresh) html += buildTokenCard('Refresh token', data.refresh, 'Use to obtain a new access token');
    if (data.user) html += buildDetailCard(data.user, 'User record');
    return html;
  }

  if (rt === 'token' && data?.access) {
    html += `<div class="r-success"><div class="r-success-icon">↺</div><div class="r-success-body"><h4>Token refreshed</h4><p>New access token issued at ${new Date().toLocaleTimeString()}.</p></div></div>`;
    html += buildTokenCard('New access token', data.access, 'Auto-injected into Authorization header');
    return html;
  }

  if (status === 201 || rt === 'created') {
    html += `<div class="r-success"><div class="r-success-icon">✓</div><div class="r-success-body"><h4>${esc(ep?.l || 'Record')} — created</h4><p>Record created successfully${data?.id ? ` · ID: ${data.id}` : ''}.</p></div></div>`;
    if (typeof data === 'object') html += buildDetailCard(data, 'Created record');
    return html;
  }

  if (rt === 'action' && status < 300) {
    const msg = data?.message || data?.detail || `${ep?.l || 'Request'} completed successfully.`;
    html += `<div class="r-success"><div class="r-success-icon">✓</div><div class="r-success-body"><h4>${esc(ep?.l || 'Action')}</h4><p>${esc(msg)}</p></div></div>`;
    if (typeof data === 'object' && Object.keys(data).length > 1) html += buildDetailCard(data, 'Response data');
    return html;
  }

  if (rt === 'log' && (Array.isArray(data) || Array.isArray(data?.results))) {
    const items = Array.isArray(data) ? data : data.results;
    if (!items.length) return `<div class="r-empty-list"><p>No logs found</p><span>The endpoint returned an empty list.</span></div>`;
    html += `<div class="a-log">`;
    items.slice(0, 40).forEach(log => {
      const icon = log.action?.includes('DELETE') ? '🗑️' : log.action?.includes('CREATE') ? '✨' : '📝';
      html += `<div class="a-log-row"><div class="a-log-icon" style="background:var(--indigo-l)">${icon}</div><div class="a-log-body"><div class="a-log-action">${esc(log.action || log.event_type || log.description || 'Event')}</div><div class="a-log-meta">${esc(log.actor || log.user || '')}${log.created_at ? ' · ' + new Date(log.created_at).toLocaleString() : ''}</div></div></div>`;
    });
    html += `</div>`;
    return html;
  }

  if (Array.isArray(data) || (data?.results && Array.isArray(data.results))) {
    const items = Array.isArray(data) ? data : data.results;
    const total = data?.count ?? items.length;
    if (!items.length) return `<div class="r-empty-list"><p>No records found</p><span>The endpoint returned an empty list.</span></div>`;
    html += `<div class="r-info-grid"><div class="r-stat"><div class="r-stat-lbl">Total records</div><div class="r-stat-val">${total}</div><div class="r-stat-sub">in this response</div></div>${data?.next ? `<div class="r-stat"><div class="r-stat-lbl">Next page</div><div class="r-stat-val" style="font-size:13px;font-style:normal">Available</div><div class="r-stat-sub">paginated results</div></div>` : ''}</div>`;
    html += buildTable(items);
    return html;
  }

  if (typeof data === 'object' && data !== null) {
    return buildDetailCard(data, ep?.l || 'Response');
  }

  return `<div style="padding:14px;background:var(--paper2);border-radius:var(--r8);font-family:var(--fm);font-size:12px;color:var(--ink2);line-height:1.7">${esc(String(data))}</div>`;
}
