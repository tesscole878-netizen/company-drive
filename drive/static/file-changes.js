(() => {
  'use strict';
  const endpoint = document.currentScript.dataset.endpoint;
  const original = endpoint.startsWith('/api/v2.1/');
  let cursor = '', account = '', timer, popup, closeTimer, refreshTimer, pending;
  const labels = {create:'已新增', edit:'已修改', update:'已更新', recover:'已恢复', copy:'已复制', upload:'已上传'};
  function dismiss() {
    clearTimeout(closeTimer);
    if (popup) popup.remove();
    popup = null;
  }
  function show(items) {
    if (!popup) {
      popup = document.createElement('aside');
      popup.setAttribute('role', 'status');
      popup.setAttribute('aria-live', 'polite');
      Object.assign(popup.style,{position:'fixed',right:'16px',bottom:'20px',zIndex:2147483600,
        width:'min(340px, calc(100vw - 32px))',boxSizing:'border-box',background:'#fff',color:'#27374b',
        border:'1px solid #b9c9d9',borderRadius:'10px',padding:'14px',boxShadow:'0 5px 22px #24364b33',
        font:'14px/1.45 system-ui,sans-serif'});
      const heading = document.createElement('strong'); heading.textContent = '云盘文件更新';
      const close = document.createElement('button'); close.type = 'button'; close.textContent = '关闭';
      close.style.float = 'right'; close.addEventListener('click',dismiss);
      const list = document.createElement('div'); list.dataset.changes = '';
      Object.assign(list.style,{clear:'both',maxHeight:'220px',overflowY:'auto',overflowWrap:'anywhere'});
      popup.append(heading,close,list); document.body.append(popup);
    }
    const list = popup.querySelector('[data-changes]');
    for (const item of items) {
      const row = document.createElement('div'); row.style.marginTop='7px';
      const title = document.createElement('strong');
      title.textContent = item.actor + ' ' + (labels[item.action] || '操作了') + ' ' + item.path.split('/').pop();
      const path = document.createElement('div'); path.style.color = '#4c6278';
      path.textContent = item.library + ' / ' + item.path.replace(/^\/+/, '');
      row.append(title,path); list.prepend(row);
    }
    while (list.children.length > 100) list.lastChild.remove();
    clearTimeout(closeTimer); closeTimer = setTimeout(dismiss, 18000);
  }

  function openDirectory() {
    if (!original) return typeof currentFileChangeDirectory === 'function' ? currentFileChangeDirectory() : null;
    const parts = location.pathname.split('/').slice(1);
    if (parts[0] !== 'library' || !/^[a-f0-9-]{36}$/.test(parts[1] || '') || !parts[2]) return null;
    try {
      const path = '/' + parts.slice(3).map(decodeURIComponent).join('/').replace(/\/$/, '');
      return {repo:parts[1], source:'legacy', path, key:location.pathname,
        refresh:() => window.dispatchEvent(new PopStateEvent('popstate', {state:{path}}))};
    } catch { return null; }
  }

  function refreshWhenIdle() {
    clearTimeout(refreshTimer);
    if (!pending || openDirectory()?.key !== pending.key) { pending = null; return; }
    if (document.hidden || document.querySelector('dialog[open], .modal.show, [role="dialog"][aria-modal="true"], ' +
        '.dropdown-menu.show, [popover]:popover-open, input[type="checkbox"]:checked') ||
        document.activeElement?.matches('input, textarea, select, [contenteditable="true"]') ||
        (!original && (state.upload || (typeof workspace !== 'undefined' && workspace.selected.size)))) {
      refreshTimer = setTimeout(refreshWhenIdle, 750);
      return;
    }
    const refresh = pending.refresh;
    pending = null;
    Promise.resolve().then(refresh).catch(console.warn);
  }

  function queueDirectoryRefresh(items) {
    const directory = openDirectory();
    if (!directory || !items.some(item => item.repo === directory.repo &&
        (original || item.source === directory.source) &&
        (item.action === 'refresh' ? ('/' + item.path.replace(/^\/+/, '')).replace(/\/$/, '') :
          ('/' + item.path.replace(/^\/+/, '')).replace(/\/[^/]+$/, '')) ===
        ('/' + directory.path.replace(/^\/+/, '')).replace(/\/$/, ''))) return;
    pending = directory;
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(refreshWhenIdle, 350);
  }
  async function poll() {
    let delay = 22000 + Math.random() * 5000;
    try {
      const identity = original ? 'seafile-session' : (typeof state !== 'undefined' && state.user?.id || '');
      if (identity !== account) { cursor = ''; account = identity; pending = null; dismiss(); }
      if (!identity) { delay = 1500; return; }
      const key = original ? 'after' : 'cursor';
      const url = endpoint + (cursor ? '?' + new URLSearchParams({[key]:cursor}) : '');
      const response = await fetch(url,{cache:'no-store',credentials:'same-origin'});
      if (response.status === 401 || response.status === 403) {cursor = ''; pending = null; dismiss(); return;}
      if (!response.ok) return;
      const data = await response.json();
      if (!original && state.user?.id !== identity) { cursor = ''; dismiss(); return; }
      cursor = data.cursor;
      if (data.items.length) {
        const notifications = data.items.filter(item => item.action !== 'refresh');
        if (notifications.length) show(notifications);
        queueDirectoryRefresh(data.items);
      }
      if (data.more) delay = 700;
    } catch { /* Keep the last successfully delivered cursor for the next poll. */ }
    finally { timer = setTimeout(poll,delay); }
  }
  window.addEventListener('pagehide',() => {clearTimeout(timer); clearTimeout(refreshTimer); dismiss();});
  window.addEventListener('pageshow',e => {if(e.persisted){clearTimeout(timer); timer=setTimeout(poll,100); refreshWhenIdle();}});
  void poll();
})();
