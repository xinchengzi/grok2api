let apiKey = '';

let state = {
  page: 1,
  pageSize: 50,
  model: '',
  success: ''
};

async function init() {
  apiKey = await ensureApiKey();
  if (apiKey === null) return;

  await loadHeader();
  await loadFooter();

  const pageSizeEl = document.getElementById('filter-page-size');
  pageSizeEl.value = String(state.pageSize);
  pageSizeEl.addEventListener('change', () => {
    state.pageSize = parseInt(pageSizeEl.value, 10);
    state.page = 1;
    refreshLogs();
  });

  refreshLogs();
}

function buildAuthHeaders() {
  return {
    'Content-Type': 'application/json',
    'Authorization': `Bearer ${apiKey}`
  };
}

function fmtTime(ts) {
  if (!ts) return '';
  try {
    const d = new Date(Number(ts));
    if (!Number.isNaN(d.getTime())) return d.toLocaleString();
  } catch (_) {}
  return String(ts);
}

function fmtMs(ms) {
  if (ms === null || ms === undefined) return '';
  const n = Number(ms);
  if (Number.isNaN(n)) return String(ms);
  return `${n.toFixed(0)}ms`;
}

function badge(ok) {
  if (ok) return '<span class="inline-flex items-center px-2 py-0.5 rounded bg-emerald-50 text-emerald-700 border border-emerald-200">OK</span>';
  return '<span class="inline-flex items-center px-2 py-0.5 rounded bg-red-50 text-red-700 border border-red-200">FAIL</span>';
}

function safeText(s) {
  if (s === null || s === undefined) return '';
  return String(s)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;');
}

async function refreshLogs() {
  const tbody = document.getElementById('logs-tbody');
  tbody.innerHTML = '<tr><td class="px-4 py-3 text-[var(--accents-4)]" colspan="6">加载中...</td></tr>';

  const params = new URLSearchParams({
    page: String(state.page),
    page_size: String(state.pageSize)
  });
  if (state.model) params.set('model', state.model);
  if (state.success) params.set('success', state.success);

  try {
    const res = await fetch(`/api/v1/admin/logs?${params.toString()}`, {
      headers: buildAuthHeaders()
    });
    if (res.status === 401) {
      logout();
      return;
    }
    if (!res.ok) {
      throw new Error(await res.text());
    }
    const data = await res.json();
    const logs = data.logs || [];
    const total = data.total || 0;

    document.getElementById('total-count').textContent = String(total);
    document.getElementById('page-number').textContent = String(state.page);

    if (!logs.length) {
      tbody.innerHTML = '<tr><td class="px-4 py-3 text-[var(--accents-4)]" colspan="6">暂无日志</td></tr>';
      return;
    }

    tbody.innerHTML = logs.map(item => {
      const ok = !!item.success;
      const respMs = (item.response_time !== undefined && item.response_time !== null)
        ? (Number(item.response_time) * 1000)
        : (item.response_time_ms ?? item.duration_ms ?? item.response_time);
      return `
        <tr class="border-t border-[var(--border)]">
          <td class="px-4 py-3 whitespace-nowrap">${safeText(fmtTime(item.timestamp))}</td>
          <td class="px-4 py-3 whitespace-nowrap">${safeText(item.model || '')}</td>
          <td class="px-4 py-3 whitespace-nowrap">${badge(ok)} ${safeText(item.status_code || '')}</td>
          <td class="px-4 py-3 whitespace-nowrap">${safeText(fmtMs(respMs))}</td>
          <td class="px-4 py-3 whitespace-nowrap">${safeText(item.sso || '')}</td>
          <td class="px-4 py-3 whitespace-nowrap">${safeText(item.proxy_used || '')}</td>
          <td class="px-4 py-3">${safeText(item.error_message || '')}</td>
        </tr>
      `;
    }).join('');
  } catch (e) {
    tbody.innerHTML = '<tr><td class="px-4 py-3 text-red-700" colspan="6">加载失败</td></tr>';
    showToast(`加载失败：${e.message || e}`, 'error');
  }
}

function applyFilters() {
  const model = document.getElementById('filter-model').value.trim();
  const success = document.getElementById('filter-success').value;
  state.model = model;
  state.success = success;
  state.page = 1;
  refreshLogs();
}

function prevPage() {
  if (state.page <= 1) return;
  state.page -= 1;
  refreshLogs();
}

function nextPage() {
  state.page += 1;
  refreshLogs();
}

async function clearLogs() {
  if (!confirm('确认清空所有调用日志？')) return;

  try {
    const res = await fetch('/api/v1/admin/logs', {
      method: 'DELETE',
      headers: buildAuthHeaders()
    });
    if (res.status === 401) {
      logout();
      return;
    }
    if (!res.ok) {
      throw new Error(await res.text());
    }
    showToast('已清空', 'success');
    state.page = 1;
    refreshLogs();
  } catch (e) {
    showToast(`清空失败：${e.message || e}`, 'error');
  }
}
