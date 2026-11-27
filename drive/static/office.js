'use strict';

async function openOffice() {
  const errorBox = document.querySelector('#office-error');
  try {
    const query = new URLSearchParams(location.search);
    const account = await fetch('/api/me');
    if (!account.ok) throw new Error('请登录云盘后重新打开文档');
    const {csrf} = await account.json();
    const response = await fetch('/api/office/session', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-Drive-CSRF': csrf},
      body: JSON.stringify({library_id: query.get('library_id'), path: query.get('path'), legacy: query.get('legacy') === '1'})
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || '在线编辑服务未启用');
    const script = document.createElement('script');
    script.src = data.script;
    await new Promise((resolve, reject) => {
      script.onload = resolve;
      script.onerror = () => reject(new Error('无法连接文档编辑服务，请联系管理员'));
      document.head.append(script);
    });
    new window.DocsAPI.DocEditor('office-editor', data.config);
    errorBox.hidden = true;
  } catch (error) {
    errorBox.textContent = error.message;
  }
}

openOffice();
