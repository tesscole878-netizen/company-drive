'use strict';

const root = document.querySelector('#portal');
const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const query = value => encodeURIComponent(value);
const brand = '<div class="brand"><div class="brand-mark"><img src="/assets/suyan-logo.png" alt="苏盐标志"></div><div><strong>苏盐新云盘</strong><small>TEAM DRIVE</small></div></div>';
const loginArtwork = '<div><h1>资料各归其位，<br>团队随时相连。</h1><p>公司的文件，有自己的空间。<br>在办公室，也在每一个需要它的地方。</p><div class="folder-art" aria-hidden="true"><div class="art-ring"></div><div class="art-folder back"></div><div class="art-folder front"><svg viewBox="0 0 24 24"><path d="m12 2 9 4v6c0 5-9 10-9 10S3 17 3 12V6zM8 12l3 3 5-6"/></svg></div><span class="art-spark">✳</span></div></div><footer>YOUR FILES. YOUR SPACE.</footer>';
const tabs = {mine:'我的资料库', shared:'共享给我的', public:'公共资料库', groups:'群组共享'};
const tools = {favorites:'收藏夹',events:'文件活动',published:'已发布的资料库',devices:'已连接的设备',shares:'共享管理'};
let account = null;
let catalog = {groups:[], libraries:[]};
let view = 'mine';
let groupId = '';
let groupsExpanded = !matchMedia('(max-width: 720px)').matches;
let current = null;
let generation = 0;
let unread = 0;
let sharesView = 'shared-libraries';
let notificationPage = 1;

function bytes(size) {
  if (size < 1024) return `${size} B`;
  const unit = Math.min(Math.floor(Math.log(size) / Math.log(1024)), 4);
  return `${(size / 1024 ** unit).toFixed(1)} ${['B','KB','MB','GB','TB'][unit]}`;
}

async function api(path, options) {
  const response = await fetch('/api' + path, options);
  const data = await response.json();
  if (!response.ok) {
    if (response.status === 401 && account) loginPage();
    const error = new Error(data.detail || '连接失败，请重试');
    error.code = data.code;
    throw error;
  }
  return data;
}

function loginPage() {
  account = null;
  current = null;
  catalog = {groups:[], libraries:[]};
  generation++;
  root.innerHTML = `<div class="portal-auth"><aside class="auth-art">${brand}${loginArtwork}</aside><main class="auth-main"><div class="auth-form">
    <h2>登录苏盐新云盘</h2><p>使用公司账号，或系统管理员账号登录。</p>
    <form id="portal-login" class="form-stack">
      <label>账号<input name="username" required maxlength="254" autocomplete="username"></label>
      <label>密码<input name="password" required type="password" maxlength="400" autocomplete="current-password"></label>
      <label id="portal-otp" hidden>验证码<input name="otp" disabled maxlength="6" inputmode="numeric" pattern="[0-9]{6}" autocomplete="one-time-code"></label>
      <div id="portal-login-error" class="form-error" role="alert"></div>
      <button type="submit" class="btn primary">登录</button>
    </form>
  </div></main></div>`;
  const form = document.querySelector('#portal-login');
  form.elements.username.addEventListener('input', () => {
    form.querySelector('#portal-otp').hidden = true;
    form.elements.otp.disabled = true;
    form.elements.otp.required = false;
    form.elements.otp.value = '';
  });
  form.onsubmit = async event => {
    event.preventDefault();
    const submit = form.querySelector('[type=submit]');
    const message = form.querySelector('#portal-login-error');
    submit.disabled = true;
    message.textContent = '';
    try {
      account = await api('/login', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(Object.fromEntries(new FormData(form)))});
      if (['admin','workspace'].includes(account.mode)) { location.reload(); return; }
      if (account.unified_identity) { location.replace('/workspace'); return; }
      view = 'mine';
      groupId = '';
      renderShell();
      await loadCatalog();
      refreshNotifications();
    } catch (error) {
      message.textContent = error.message;
      if (error.code === 'seafile_otp_required') {
        form.querySelector('#portal-otp').hidden = false;
        form.elements.otp.disabled = false;
        form.elements.otp.required = true;
        form.elements.otp.focus();
      }
    } finally {
      form.elements.password.value = '';
      submit.disabled = false;
    }
  };
}

function tabButton(key) {
  return `<button type="button" class="nav-item${view === key ? ' active' : ''}" data-view="${key}" ${view === key ? 'aria-current="page"' : ''}>${tabs[key]}</button>`;
}

function renderShell() {
  if (!account) return;
  const scrollTop = root.querySelector('.sidebar-scroll')?.scrollTop || 0;
  const groups = [{id:'', name:'所有群组'}, ...catalog.groups];
  const groupButtons = groups.map(group => `<button type="button" class="group-nav-item" data-group="${escapeHtml(group.id)}" ${view === 'groups' && String(group.id) === groupId ? 'aria-current="page"' : ''} title="${escapeHtml(group.name)}"><span aria-hidden="true">#</span><span>${escapeHtml(group.name)}</span></button>`).join('');
  root.innerHTML = `<div class="shell library-shell"><aside class="sidebar">${brand}<div class="sidebar-scroll">
    <nav class="portal-nav" aria-label="资料库">${tabButton('mine')}${tabButton('shared')}${tabButton('public')}
      <button type="button" class="nav-item group-nav-toggle" data-action="groups" aria-expanded="${groupsExpanded}" aria-controls="portal-groups">群组共享<span class="group-nav-chevron" aria-hidden="true"></span></button>
      <div class="group-nav-list" id="portal-groups" ${groupsExpanded ? '' : 'hidden'}>${groupButtons}</div>
    </nav>${account.unified_identity ? '<a class="nav-item" href="/workspace">新文件与共享</a>' : ''}<div class="portal-tools-label">工具</div><nav class="portal-nav" aria-label="工具">${Object.entries(tools).map(([key,label]) => `<button type="button" class="nav-item${view === key ? ' active' : ''}" data-tool="${key}">${label}</button>`).join('')}</nav></div></aside>
    <main class="workspace"><header class="topbar portal-topbar"><div class="crumb-top"><strong id="top-label"></strong></div>
      <div class="portal-account"><button type="button" class="portal-bell" data-action="notifications" aria-label="通知">🔔<span class="portal-unread" ${unread ? '' : 'hidden'}>${unread || ''}</span></button><span class="user-info">${escapeHtml(account.name)}</span><button type="button" class="btn secondary" data-action="logout">退出</button></div></header>
      <section class="content" id="portal-content" aria-live="polite"></section></main></div>`;
  root.querySelector('.sidebar-scroll').scrollTop = scrollTop;
}

function content(title, body, actions = '', path = '') {
  root.querySelector('#top-label').textContent = title;
  root.querySelector('#portal-content').innerHTML = `<div class="page-heading"><div><h1 class="portal-title">${escapeHtml(title)}</h1>${path ? `<p>${escapeHtml(path)}</p>` : ''}</div>
    <div class="portal-actions"><span class="badge">只读</span>${actions}<button type="button" class="btn secondary" data-action="refresh">刷新</button></div></div>${body}`;
}

function table(columns, rows, empty) {
  if (!rows.length) return `<div class="empty"><h3>${escapeHtml(empty)}</h3></div>`;
  return `<div class="table-wrap"><table class="portal-table"><thead><tr>${columns.map(column => `<th scope="col">${column}</th>`).join('')}</tr></thead><tbody>${rows.join('')}</tbody></table></div>`;
}

function libraryTable(libraries) {
  const rows = libraries.map(lib => {
    const label = lib.encrypted ? `<span class="portal-file-name">${escapeHtml(lib.name)}</span>` : `<button type="button" class="file-name portal-file-name" data-library="${escapeHtml(lib.id)}">${escapeHtml(lib.name)}</button>`;
    return `<tr><td>${label}</td><td>${lib.encrypted ? '加密库，请在原 Seafile 打开' : '只读'}</td></tr>`;
  });
  return table(['资料库','权限'], rows, '暂无可访问的资料库');
}

function showCatalog() {
  current = null;
  renderShell();
  if (view !== 'groups') {
    content(tabs[view], `<section class="panel">${libraryTable(catalog.libraries.filter(lib => lib.category === view))}</section>`);
    return;
  }
  const group = catalog.groups.find(item => String(item.id) === groupId);
  const groups = group ? [group] : catalog.groups;
  const sections = groups.map(item => `<section class="panel team-section">${group ? '' : `<div class="panel-head"><h2>${escapeHtml(item.name)}</h2></div>`}${libraryTable(catalog.libraries.filter(lib => lib.group_id === item.id))}</section>`);
  content(group?.name || '所有群组', sections.join('') || '<section class="panel empty"><h3>暂无群组</h3></section>');
}

async function loadCatalog() {
  const requestId = ++generation;
  current = null;
  content(tabs[view], '<div class="empty">正在读取资料库…</div>');
  try {
    const latest = await api('/catalog');
    if (requestId !== generation) return;
    catalog = latest;
    if (groupId && !catalog.groups.some(group => String(group.id) === groupId)) groupId = '';
    showCatalog();
  } catch (error) {
    if (requestId === generation) content(tabs[view], `<div class="panel portal-notice" role="alert">${escapeHtml(error.message)}</div>`);
  }
}

async function openDirectory(library, path) {
  const requestId = ++generation;
  current = {library, path};
  const back = `<button type="button" class="btn secondary" data-action="back">${path === '/' ? '返回资料库' : '上一级'}</button>`;
  content(library.name, '<div class="empty">正在读取文件…</div>', back, path);
  const base = `/libraries/${query(library.id)}`;
  try {
    const entries = await api(`${base}/files?path=${query(path)}`);
    if (requestId !== generation) return;
    const rows = entries.map(entry => {
      const fullPath = `${path === '/' ? '' : path}/${entry.name}`;
      const label = `<button type="button" class="file-name portal-file-name" ${entry.is_dir ? 'data-folder' : 'data-preview'}="${escapeHtml(fullPath)}">${escapeHtml(entry.name)}</button>`;
      const action = entry.is_dir ? '' : `<a class="btn secondary" href="/api${base}/download?path=${query(fullPath)}">下载</a>`;
      return `<tr><td>${label}</td><td>${entry.is_dir ? '—' : bytes(entry.size)}</td><td>${action}</td></tr>`;
    });
    content(library.name, `<section class="panel">${table(['名称','大小','操作'], rows, '文件夹为空')}</section>`, back, path);
  } catch (error) {
    if (requestId === generation) content(library.name, `<div class="panel portal-notice" role="alert">${escapeHtml(error.message)}</div>`, back, path);
  }
}

function popup(title, className='portal-preview') {
  const previous = root.querySelector('dialog');
  if (previous) { previous.close(); previous.remove(); }
  const dialog = document.createElement('dialog');
  dialog.className = className;
  dialog.setAttribute('closedby','none');
  dialog.innerHTML = `<header class="portal-dialog-head"><strong>${escapeHtml(title)}</strong><button type="button" class="icon-btn" data-action="close-dialog" aria-label="关闭">✕</button></header><div class="portal-dialog-body"></div>`;
  dialog.addEventListener('cancel', event => event.preventDefault());
  root.append(dialog);
  dialog.showModal();
  return dialog;
}

async function previewFile(library, path) {
  const dialog = popup(path.split('/').at(-1));
  const body = dialog.querySelector('.portal-dialog-body');
  const download = `/api/libraries/${query(library.id)}/download?path=${query(path)}`;
  body.innerHTML = '<p>正在打开预览…</p>';
  try {
    const response = await fetch(`/api/libraries/${query(library.id)}/preview?path=${query(path)}`);
    if (!response.ok) {
      const error = await response.json();
      throw new Error(error.detail || '预览失败，请下载查看');
    }
    if (!dialog.open) return;
    const mediaType = response.headers.get('content-type') || '';
    if (mediaType.startsWith('text/plain')) {
      const text = document.createElement('pre');
      text.textContent = await response.text();
      body.replaceChildren(text);
    } else {
      const url = URL.createObjectURL(await response.blob());
      const element = document.createElement(mediaType.startsWith('image/') ? 'img' : 'iframe');
      element.src = url;
      element.title = path.split('/').at(-1);
      dialog.addEventListener('close', () => URL.revokeObjectURL(url), {once:true});
      if (!dialog.open) { URL.revokeObjectURL(url); return; }
      body.replaceChildren(element);
    }
  } catch (error) {
    body.innerHTML = `<p>${escapeHtml(error.message)}</p><p><a class="btn secondary" href="${download}">下载文件</a></p>`;
  }
}

const dateLabel = value => value ? new Date(value).toLocaleString('zh-CN') : '—';
const activityNames = {create:'创建',edit:'更新',delete:'删除',rename:'重命名',move:'移动',publish:'发布',revert:'恢复'};
async function showTool(name) {
  view = name;
  current = null;
  const requestId = ++generation;
  renderShell();
  const title = tools[name];
  const shareLabels = {'shared-libraries':'资料库','shared-folders':'文件夹','download-links':'共享链接','upload-links':'上传链接'};
  const actions = name === 'shares' ? Object.entries(shareLabels).map(([key,label]) => `<button type="button" class="btn ${key === sharesView ? 'primary' : 'secondary'}" data-shares="${key}">${label}</button>`).join('') : '';
  content(title,'<div class="empty">正在读取记录…</div>',actions);
  try {
    const rows = await api('/tools/' + (name === 'shares' ? sharesView : name));
    if (requestId !== generation) return;
    let columns, entries;
    if (name === 'devices') {
      columns = ['设备','平台','最近访问','IP 地址'];
      entries = rows.map(item => [item.device_name || '未命名设备',item.platform,dateLabel(item.last_accessed),item.last_login_ip]);
    } else if (name === 'events') {
      columns = ['资料库','文件','操作','操作人','时间'];
      entries = rows.map(item => [item.repo_name,item.path,activityNames[item.op_type] || item.op_type,item.author_name,dateLabel(item.time)]);
    } else if (name === 'shares') {
      columns = ['资料库','位置','共享对象 / 类型','原权限'];
      entries = rows.map(item => [item.repo_name,item.path || '/',item.user_name || item.group_name || item.share_type || shareLabels[sharesView],item.share_permission || (item.is_expired ? '已过期' : '有效')]);
    } else {
      columns = ['名称','资料库','位置'];
      entries = rows.map(item => [item.obj_name || item.name || item.repo_name,item.repo_name,item.path || '/']);
    }
    const rendered = entries.map((cells,index) => {
      const item = rows[index];
      const library = catalog.libraries.find(lib => lib.id === item.repo_id && !lib.encrypted);
      const path = item.path || '/';
      return '<tr>' + cells.map((cell, column) => {
        if (column === 0 && library && name === 'favorites' && !item.deleted) {
          return `<td><button type="button" class="file-name portal-file-name" data-tool-open="${index}">${escapeHtml(cell)}</button></td>`;
        }
        return `<td>${escapeHtml(cell || '—')}</td>`;
      }).join('') + '</tr>';
    });
    content(title,`<section class="panel">${table(columns,rendered,'暂无记录')}</section>`,actions);
    root.querySelectorAll('[data-tool-open]').forEach(button => {
      button.onclick = async () => {
        const item = rows[Number(button.dataset.toolOpen)];
        const library = catalog.libraries.find(lib => lib.id === item.repo_id);
        if (item.is_dir) await openDirectory(library,item.path || '/');
        else await previewFile(library,item.path);
      };
    });
  } catch(error) {
    if (requestId === generation) content(title,`<div class="panel portal-notice">${escapeHtml(error.message)}</div>`,actions);
  }
}

async function refreshNotifications() {
  if (!account || account.mode === 'admin') return;
  try {
    const value = await api('/notifications');
    unread = Number(value.unread) || 0;
    const badge = root.querySelector('.portal-unread');
    if (badge) { badge.hidden = !unread; badge.textContent = unread > 99 ? '99+' : unread; }
  } catch { /* A temporary notification outage must not interrupt file browsing. */ }
}

async function notifications() {
  const dialog = popup('通知','portal-notification-dialog');
  const body = dialog.querySelector('.portal-dialog-body');
  body.textContent = '正在读取通知…';
  try {
    const value = await api('/notifications?page=' + notificationPage);
    if (!dialog.open) return;
    unread = Number(value.unread) || 0;
    const notices = value.items.map(item => {
      const detail = item.detail;
      const from = detail.share_from_user_name || detail.author_name || detail.user_name || '';
      const group = detail.group_name ? `在群组 ${detail.group_name} 中` : '';
      const resource = detail.repo_name || detail.file_name || detail.name || detail.path || '';
      const message = item.type === 'repo-share' ? `${from} ${group} 共享了资料库 ${resource}` : `${from} ${group} ${resource} ${detail.msg || detail.comment || ''}`;
      return `<article class="portal-notification ${item.seen ? '' : 'unread'}"><div>${escapeHtml(message.trim() || 'Seafile 系统通知')}</div><time>${escapeHtml(dateLabel(item.time))}</time></article>`;
    });
    body.innerHTML = (notices.join('') || '<p>暂无通知</p>') + `<div class="portal-actions"><button type="button" class="btn secondary" data-action="previous-notice" ${notificationPage === 1 ? 'disabled' : ''}>上一页</button><span>第 ${notificationPage} 页</span><button type="button" class="btn secondary" data-action="next-notice" ${notificationPage * 20 >= value.total ? 'disabled' : ''}>下一页</button></div>`;
  } catch(error) { body.textContent = error.message; }
}

root.addEventListener('click', async event => {
  const button = event.target.closest('button');
  if (!button || !account) return;
  if (button.dataset.action === 'close-dialog') {
    button.closest('dialog').close();
  } else if (button.dataset.action === 'notifications') {
    notificationPage = 1;
    await notifications();
  } else if (['previous-notice','next-notice'].includes(button.dataset.action)) {
    notificationPage += button.dataset.action === 'next-notice' ? 1 : -1;
    await notifications();
  } else if (button.dataset.tool) {
    await showTool(button.dataset.tool);
  } else if (button.dataset.shares) {
    sharesView = button.dataset.shares;
    await showTool('shares');
  } else if (button.dataset.preview && current) {
    await previewFile(current.library,button.dataset.preview);
  } else if (button.dataset.view) {
    view = button.dataset.view;
    groupId = '';
    await loadCatalog();
  } else if (button.hasAttribute('data-group')) {
    view = 'groups';
    groupId = button.dataset.group;
    await loadCatalog();
  } else if (button.dataset.library) {
    const library = catalog.libraries.find(item => item.id === button.dataset.library);
    if (library) await openDirectory(library, '/');
  } else if (button.dataset.folder && current) {
    await openDirectory(current.library, button.dataset.folder);
  } else if (button.dataset.action === 'groups') {
    groupsExpanded = !groupsExpanded;
    button.setAttribute('aria-expanded', groupsExpanded);
    root.querySelector('#portal-groups').hidden = !groupsExpanded;
  } else if (button.dataset.action === 'refresh') {
    if (current) await openDirectory(current.library, current.path);
    else if (view in tools) await showTool(view);
    else await loadCatalog();
  } else if (button.dataset.action === 'back' && current) {
    if (current.path === '/' && view in tools) await showTool(view);
    else if (current.path === '/') await loadCatalog();
    else await openDirectory(current.library, current.path.slice(0, current.path.lastIndexOf('/')) || '/');
  } else if (button.dataset.action === 'logout') {
    try {
      await api('/logout', {method:'POST'});
      loginPage();
    } catch (error) {
      content('无法退出', `<div class="panel portal-notice" role="alert">${escapeHtml(error.message)}</div>`);
    }
  }
});

async function boot() {
  try {
    account = await api('/session');
    if (['admin','workspace'].includes(account.mode)) { location.reload(); return; }
    renderShell();
    await loadCatalog();
    refreshNotifications();
  } catch {
    loginPage();
  }
}

boot();
setInterval(refreshNotifications, 60000);
