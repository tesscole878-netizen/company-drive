'use strict';

function legacyAdvancedRef(data, extra = '') {
  return legacyRef(data.repo, data.name, data.encrypted === '1', data.managed === '1') +
    ' data-pathname="' + esc(data.pathname) + '" ' + extra;
}

function reopenLegacy(data) {
  return legacyFolder(data.repo, data.name, data.pathname, data.encrypted === '1', data.managed === '1');
}

function legacyEventTime(value) {
  if (!value) return '—';
  const seconds = typeof value === 'number' ? value : Date.parse(value) / 1000;
  return Number.isFinite(seconds) ? date(seconds) : '—';
}

async function legacyHistory(data, pageNumber = 1) {
  const path = data.pathname;
  const base = legacyURL(data.repo) + '/history';
  const history = await api(base + '?' + new URLSearchParams({path, page: pageNumber}));
  const rows = history.items.map(item => {
    const download = '/api' + base + '/download?' + new URLSearchParams({path, original: item.path, commit: item.commit, proof: item.proof});
    const actions = [
      '<a class="btn secondary" href="' + esc(download) + '">下载此版</a>',
      history.writable && workspace.license?.can_write !== false
        ? button('恢复此版','legacy-history-restore',legacyAdvancedRef(data,
          'data-commit="' + esc(item.commit) + '" data-proof="' + esc(item.proof) + '" data-original="' +
          esc(item.path) + '" data-version="' + esc(history.version) + '"')) : ''
    ];
    return '<tr><td>' + esc(legacyEventTime(item.time)) + '</td><td>' + esc(item.author || '—') +
      '</td><td>' + esc(typeof item.size === 'number' ? bytes(item.size) : '—') + '</td><td>' + actions.join('') + '</td></tr>';
  });
  const pager = (pageNumber > 1 ? button('上一页','legacy-history',legacyAdvancedRef(data,'data-page="' + (pageNumber - 1) + '"')) : '') +
    (history.more ? button('下一页','legacy-history',legacyAdvancedRef(data,'data-page="' + (pageNumber + 1) + '"')) : '');
  modal('历史版本', legacyDisplayName(pathName(path)), table(['时间','修改人','大小','操作'], rows, '暂无历史版本'), pager);
}

async function restoreLegacyHistory(data) {
  if (!await confirmAction('恢复历史版本','选中的历史内容将成为当前版本，同时作用于两个入口。','确认恢复',[legacyDisplayName(data.pathname)])) return;
  const {pathname:path,commit,version,proof,original} = data;
  await api(legacyURL(data.repo) + '/history/restore', json('POST', {path,commit,version,proof,original}));
  $('#modal').close();
  toast('已恢复历史版本');
  await reopenLegacy({...data,pathname: parentPath(path) || '/'});
}

async function legacyTrash(data, cursor = '') {
  const path = data.pathname;
  const base = legacyURL(data.repo) + '/trash';
  const result = await api(base + '?' + new URLSearchParams({path,cursor}));
  const rows = result.items.map(item => {
    const restore = item.writable && workspace.license?.can_write !== false
      ? button('恢复','legacy-trash-restore',legacyAdvancedRef(data,
        'data-target-path="' + esc(item.path) + '" data-commit="' + esc(item.commit) + '" data-proof="' + esc(item.proof) + '"')) : '';
    return '<tr><td>' + esc(legacyDisplayName(item.path)) + '</td><td>' + (item.is_dir ? '文件夹' : '文件') +
      '</td><td>' + esc(legacyEventTime(item.time)) + '</td><td>' + restore + '</td></tr>';
  });
  const footer = (cursor ? button('返回第一页','legacy-trash',legacyAdvancedRef(data)) : '') +
    (result.cursor ? button('继续查找','legacy-trash',legacyAdvancedRef(data,'data-cursor="' + esc(result.cursor) + '"')) : '');
  modal('旧资料库回收站', data.name + path, table(['原路径','类型','删除时间','操作'], rows, '此页没有可见的已删除项目'), footer);
}

async function restoreLegacyTrash(data) {
  if (!await confirmAction('恢复已删除项目','将项目恢复到原 Seafile 的原位置。','确认恢复',[legacyDisplayName(data.targetPath)])) return;
  await api(legacyURL(data.repo) + '/trash/restore', json('POST', {path: data.targetPath, commit: data.commit, proof: data.proof}));
  await reopenLegacy(data);
  await legacyTrash(data);
  toast('已从旧盘回收站恢复');
}

function legacyGroupSettings(groupId) {
  return api('/legacy/groups/' + groupId + '/settings').then(settings => {
    formModal('原群组设置', '更改会写入原 Seafile。',
      '<label>群组名称<input required name="name" maxlength="80" value="' + esc(settings.name) + '"></label>',
      '保存群组名称', async ({name}) => {
        await api('/legacy/groups/' + groupId + '/settings', json('PATCH', {name}));
        await loadWorkspace();
        toast('已保存群组设置');
      });
  });
}

async function setLegacyGroupAdmin(data) {
  const administrator = data.promote === '1';
  const action = administrator ? '设为管理员' : '取消管理员';
  if (!await confirmAction(action, '角色变更会立即写入原 Seafile。', action, [data.membername])) return;
  await api('/legacy/groups/' + Number(data.external) + '/members/admin',json('PATCH',{email:data.email,administrator}));
  await originalMembers(Number(data.external), Number(data.pagenum || 1));
  toast('已更新原群组管理员');
}

function externalLinkRoute(data) {
  const route = legacyURL(data.repo) + '/external-links';
  return data.key ? route + '/' + encodeURIComponent(data.kind) + '/' + encodeURIComponent(data.key) : route;
}

async function legacyExternalLinks(data) {
  const result = await api(legacyURL(data.repo) + '/external-links?' + new URLSearchParams({path:data.pathname,folder:data.folder === '1'}));
  const writable = workspace.license?.can_write !== false;
  const rows = result.items.map(item => {
    const target = legacyAdvancedRef(data,'data-kind="' + esc(item.kind) + '" data-key="' + esc(item.id) + '"');
    const actions = writable ? button('改期限','legacy-link-expiry',target) + button('换密码','legacy-link-password',target) +
      button('撤销','legacy-link-revoke',target,'danger') : '';
    return '<tr><td>' + (item.kind === 'upload' ? '上传链接' : '下载链接') + '</td><td><a href="' +
      esc(item.url) + '" target="_blank" rel="noopener noreferrer">' + esc(item.url) + '</a></td><td>' +
      esc(item.expires ? legacyEventTime(item.expires) : '不限期') + (item.expired ? ' · 已到期' : '') + '</td><td>' +
      (item.has_password ? '已设置' : '无') + '</td><td>' + actions + '</td></tr>';
  });
  modal('旧盘分享链接', data.name + ' / ' + legacyDisplayName(data.pathname),
    table(['类型','链接','有效期','密码','操作'], rows, '你尚未为此处创建外链'),
    writable ? button('创建链接','legacy-link-create',legacyAdvancedRef(data,'data-folder="' + (data.folder === '1' ? '1' : '0') + '"'),'primary') : '');
}

const legacyExpiryField = '<label>到期时间<input name="expires" type="datetime-local"></label>';

function createLegacyExternalLink(data) {
  const kindField = data.folder === '1'
    ? '<label>用途<select name="kind"><option value="download">下载</option><option value="upload">接收上传</option></select></label>'
    : '<input type="hidden" name="kind" value="download">';
  formModal('创建旧盘分享链接','链接使用原 Seafile 的权限规则。', kindField +
    '<label>访问密码<input name="password" type="password" autocomplete="new-password" maxlength="64"></label>' +
    legacyExpiryField, '创建链接', async values => {
      const link = await api(externalLinkRoute(data), json('POST', {path:data.pathname,kind:values.kind,password:values.password,
        expires:values.expires ? new Date(values.expires).toISOString() : null}));
      showCreatedLegacyLink(link.url, data);
    });
}

function showCreatedLegacyLink(url, data) {
  // Keep the current form's submit handler from closing the next dialog.
  setTimeout(() => {
    const fields = '<div class="copy-row"><input id="legacy-link-copy" readonly value="' + esc(url) + '"><button type="button" id="legacy-link-copy-action" class="btn secondary">复制</button></div>';
    modal('链接已保存','可将此网址发给收件人。',fields,button('返回链接管理','legacy-links',legacyAdvancedRef(data,'data-folder="' + (data.folder === '1' ? '1' : '0') + '"')));
    $('#legacy-link-copy-action').onclick = () => runAction(async () => { await navigator.clipboard.writeText(url); toast('链接已复制'); });
  },0);
}

function changeLegacyExternalExpiry(data) {
  formModal('修改旧盘链接期限','设置新的到期时间。',legacyExpiryField.replace('name="expires"','name="expires" required'),
    '保存期限',async ({expires}) => {
      await api(externalLinkRoute(data),json('PATCH',{expires:new Date(expires).toISOString()}));
      toast('链接期限已更新');
    });
}

function changeLegacyExternalPassword(data) {
  formModal('更换旧盘链接密码','保存后将发放新网址，已有网址及其解锁会话会失效。',
    '<label>新密码<input name="password" type="password" autocomplete="new-password" maxlength="64" required></label>',
    '更新密码和链接',async ({password}) => {
      const link = await api(externalLinkRoute(data),json('PATCH',{password}));
      showCreatedLegacyLink(link.url, data);
    });
}

async function revokeLegacyExternalLink(data) {
  if (!await confirmAction('撤销旧盘链接','撤销后收件人无法继续使用该链接。','确认撤销',[legacyDisplayName(data.pathname)])) return;
  await api(externalLinkRoute(data),{method:'DELETE'});
  await legacyExternalLinks(data);
  toast('链接已撤销');
}

async function chooseLegacyUpload(data, replace = false) {
  // Capture the version before opening the file chooser.
  const expected = replace
    ? (await api(legacyURL(data.repo) + '/write-version?' + new URLSearchParams({path:data.pathname}))).version : '';
  const picker = document.createElement('input');
  picker.type = 'file';
  picker.multiple = !replace;
  picker.onchange = () => runAction(async () => {
    const files = Array.from(picker.files);
    if (!files.length) return;
    if (replace && !await confirmAction('上传新版本','上传内容将替换原 Seafile 的当前文件。','确认更新',[legacyDisplayName(data.pathname)])) return;
    await uploadLegacyFiles(data, files, expected);
  });
  picker.click();
}

async function uploadLegacyFiles(data, files, expected) {
  if (state.upload) throw new Error('请等待当前上传完成');
  state.upload = true;
  const transfer = $('#transfer');
  transfer.hidden = false;
  transfer.innerHTML = '<header><span>上传到原 Seafile</span></header><p id="legacy-upload-status"></p><progress max="100" value="0"></progress>';
  const message = $('#legacy-upload-status'), bar = transfer.querySelector('progress');
  let completed = 0;
  const retryable = [];
  const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
  const base = legacyURL(data.repo) + '/uploads';
  const directory = expected ? parentPath(data.pathname) || '/' : data.pathname;
  async function commit(url) {
    await api(url + '/complete', {method:'POST'});
    let status;
    do { await wait(1300); status = await api(url); } while (status.state === 'committing');
    return status;
  }
  function summary() {
    transfer.innerHTML = '<header><span>上传结束</span><button class="icon-btn" id="legacy-close-transfer" aria-label="关闭上传面板">' +
      icon('close') + '</button></header><p>已保存 ' + completed + ' / ' + files.length + ' 个文件</p>';
    $('#legacy-close-transfer').onclick = () => { transfer.hidden = true; };
    for (const item of retryable) {
      const control = document.createElement('button');
      control.type = 'button';
      control.className = 'btn secondary';
      control.textContent = '重试写回 ' + item.name;
      transfer.append(control);
      control.onclick = () => runAction(async () => {
        if (state.upload) return;
        state.upload = control.disabled = true;
        try {
          const status = await commit(item.url);
          if (status.state !== 'retry') retryable.splice(retryable.indexOf(item), 1);
          if (status.state !== 'done') throw new Error(status.error || '原 Seafile 未确认文件保存');
          completed++;
          await reopenLegacy({...data,pathname:directory});
        } finally {
          state.upload = false;
          summary();
        }
      });
    }
  }
  try {
    for (const file of files) {
      const path = expected ? data.pathname : data.pathname.replace(/\/$/, '') + '/' + file.name;
      try {
        message.textContent = file.name + ' · 正在校验内容';
        bar.value = 0;
        const {id,offset,state: phase} = await api(base,json('POST',{path,size:file.size,expected,
          fingerprint:await uploadFingerprint(file)}));
        const url = base + '/' + id;
        let sent = offset;
        while (phase === 'receiving' && sent < file.size) {
          const stop = Math.min(sent + 4 * 1024 * 1024,file.size);
          for (let attempt = 0; attempt < 3 && sent < stop; attempt++) {
            try {
              const reply = await api(url, {method:'PATCH',headers:{'Content-Type':'application/octet-stream','Upload-Offset':String(sent)},body:file.slice(sent,stop)});
              sent = reply.offset;
            } catch (error) {
              if (attempt === 2) throw error;
              await wait(700);
              const progress = await api(url);
              if (progress.state !== 'receiving') throw error;
              sent = progress.offset;
            }
          }
          bar.value = Math.round(100 * sent / file.size);
          message.textContent = file.name + ' · ' + bytes(sent) + ' / ' + bytes(file.size);
        }
        message.textContent = file.name + ' · 正在写入原 Seafile…';
        const status = await commit(url);
        if (status.state === 'retry') retryable.push({url,name:file.name});
        if (status.state !== 'done') throw new Error(status.error || '原 Seafile 未确认文件保存');
        completed++;
      } catch (error) { toast(file.name + ' · ' + error.message, true); }
    }
  } finally {
    state.upload = false;
    summary();
    if (completed) await reopenLegacy({...data,pathname: directory});
  }
}
