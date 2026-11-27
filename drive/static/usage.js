'use strict';

async function usagePage() {
  const generation = workspace.generation;
  const labels = {upload:'上传', download:'下载', share:'分享'};
  const clock = new Intl.DateTimeFormat('zh-CN', {
    timeZone:'Asia/Shanghai', year:'numeric', month:'2-digit', day:'2-digit',
    hour:'2-digit', minute:'2-digit', second:'2-digit', hourCycle:'h23'
  });
  const formatTime = value => clock.format(new Date(value * 1000));
  page('使用情况', '<section id="usage-online" aria-label="活跃连接" aria-live="polite"></section>' +
    '<section aria-label="关键操作日志"><div class="panel-head"><h2>操作日志</h2>' +
    '<label>操作类型 <select id="usage-action"><option value="">全部</option><option value="upload">上传</option>' +
    '<option value="download">下载</option><option value="share">分享</option></select></label></div>' +
    '<div id="usage-operations" aria-live="polite"></div><div id="usage-pager" class="toolbar"></div></section>',
    '<button type="button" id="usage-refresh" class="btn secondary">刷新</button>');
  const online = $('#usage-online'), operations = $('#usage-operations'), action = $('#usage-action'), pager = $('#usage-pager');
  const refresh = $('#usage-refresh');
  let before = 0, sequence = 0;
  const onPage = () => workspace.generation === generation;
  const errorText = error => '<p class="form-error" role="alert">' + esc(error.message) + '</p>';
  const actor = row => esc(row.name || row.username || '外链访客') +
    (row.username && row.name !== row.username ? '<small class="file-description">' + esc(row.username) + '</small>' : '');

  async function loadConnections() {
    try {
      const data = await api('/admin/usage');
      if (!onPage()) return;
      const rows = data.connections.map(row =>
        '<tr><td>' + actor(row) + '</td><td>' + (row.channel === 'web' ? '网页版' : '电脑盘 / WebDAV') +
        (row.device ? '<small class="file-description">' + esc(row.device) + '</small>' : '') +
        '</td><td><code>' + esc(row.ip || '—') + '</code></td><td>' + esc(formatTime(row.last_seen)) + '</td></tr>');
      online.innerHTML = '<p class="notice">员工账号 <strong>' + data.total + '</strong> · 已启用 <strong>' + data.enabled +
        '</strong> · 近 24 小时使用员工 <strong>' + data.recent_accounts + '</strong> · 近 ' +
        (data.active_window / 60) + ' 分钟活跃账号 <strong>' + data.active_accounts +
        '</strong></p><div class="panel-head"><h2>活跃连接</h2><span class="muted">以最近请求判断，IP 为服务器看到的地址</span></div>' +
        table(['账号','来源 / 设备','IP','最后活跃'], rows, '最近没有活跃连接');
    } catch (error) { if (onPage()) online.innerHTML = errorText(error); }
  }

  async function loadOperations(cursor) {
    const request = ++sequence;
    operations.textContent = '正在读取操作日志…';
    pager.replaceChildren();
    try {
      const result = await api('/admin/usage/operations?' + new URLSearchParams({before:cursor,action:action.value}));
      if (!onPage() || request !== sequence) return;
      before = cursor;
      const rows = result.items.map(row => {
        const source = row.source === 'legacy' ? '原 Seafile' : '新盘原生库';
        const location = row.library_name || source;
        return '<tr><td>' + esc(formatTime(row.created)) + '</td><td>' + actor(row) + '</td><td>' +
          esc(labels[row.action]) + '</td><td><span title="' + esc(row.library_id) + '">' + esc(location) +
          '</span><small class="file-description">' + esc(legacyDisplayName(row.path)) + '</small></td></tr>';
      });
      operations.innerHTML = table(['时间','账号','操作','资料库 / 文件'], rows, '暂无符合条件的记录');
      pager.innerHTML = (before ? '<button type="button" class="btn secondary" data-usage-first>返回最新</button>' : '') +
        (result.next ? '<button type="button" class="btn secondary" data-usage-next>查看更早记录</button>' : '');
      if (before) pager.querySelector('[data-usage-first]').onclick = () => loadOperations(0);
      if (result.next) pager.querySelector('[data-usage-next]').onclick = () => loadOperations(result.next);
    } catch (error) { if (onPage() && request === sequence) operations.innerHTML = errorText(error); }
  }

  action.onchange = () => loadOperations(0);
  refresh.onclick = async () => {
    refresh.disabled = true;
    try { await Promise.all([loadConnections(), loadOperations(0)]); }
    finally { refresh.disabled = false; }
  };
  await Promise.all([loadConnections(), loadOperations(0)]);
}
