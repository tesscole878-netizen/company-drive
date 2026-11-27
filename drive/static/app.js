'use strict';
const $ = s => document.querySelector(s);
const state = { user: null, groups: [], group: null, path: '', view: 'files', files: [], query: '', usersQuery: '', upload: false };
const esc = x => String(x ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const icons = {
 box:'<path d="M4 7h16v13H4zM3 3h18v4H3zM9 11h6"/>',
 folder:'<path d="M2 6a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2z"/>',
 grid:'<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
 clock:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
 trash:'<path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7"/>',
 users:'<circle cx="9" cy="8" r="3"/><path d="M3 20v-2a6 6 0 0 1 12 0v2M16 5a3 3 0 0 1 0 6M18 14a5 5 0 0 1 3 4v2"/>',
 monitor:'<rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8M12 17v4"/>',
 arrow:'<path d="M5 12h14M14 7l5 5-5 5"/>',
 plus:'<path d="M12 5v14M5 12h14"/>',
 upload:'<path d="M12 16V3M7 8l5-5 5 5M3 15v5h18v-5"/>',
 download:'<path d="M12 3v13M7 11l5 5 5-5M3 16v5h18v-5"/>',
 search:'<circle cx="10" cy="10" r="6"/><path d="m15 15 6 6"/>',
 edit:'<path d="m4 16-1 5 5-1L20 8l-4-4zM13 7l4 4"/>',
 close:'<path d="m6 6 12 12M6 18 18 6"/>',
 check:'<path d="m5 12 4 4L19 6"/>',
 shield:'<path d="m12 2 9 4v6c0 5-9 10-9 10S3 17 3 12V6zM8 12l3 3 5-6"/>',
 storage:'<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14c0 4 18 4 18 0V5M3 12c0 4 18 4 18 0"/>',
 refresh:'<path d="M20 7v5h-5M4 17v-5h5M6 6a8 8 0 0 1 14 6M4 12a8 8 0 0 0 14 6"/>',
 logout:'<path d="M9 4H3v16h6M10 12h12m-5-5 5 5-5 5"/>',
 key:'<circle cx="8" cy="8" r="5"/><path d="m12 12 9 9m-4-4 3-3m-6 0 3-3"/>',
 star:'<path d="m12 3 2.8 5.7 6.2.9-4.5 4.4 1.1 6.2-5.6-3-5.6 3 1.1-6.2L3 9.6l6.2-.9z"/>',
 bell:'<path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4"/>',
 globe:'<circle cx="12" cy="12" r="9"/><ellipse cx="12" cy="12" rx="4" ry="9"/><path d="M3 12h18"/>',
 link:'<path d="m10 13 4-4m-6 6-2 2a3.5 3.5 0 1 1-5-5l4-4a3.5 3.5 0 0 1 5 0m4 1 2-2a3.5 3.5 0 1 1 5 5l-4 4a3.5 3.5 0 0 1-5 0" transform="translate(1 0)"/>',
 list:'<path d="M8 5h13M8 12h13M8 19h13M3 5h1M3 12h1M3 19h1"/>',
 columns:'<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16M15 4v16"/>',
 more:'<circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>',
 share:'<circle cx="6" cy="12" r="2.5"/><circle cx="18" cy="5" r="2.5"/><circle cx="18" cy="19" r="2.5"/><path d="m8.2 10.7 7.5-4.4m-7.5 7 7.5 4.4"/>'
};
const icon = name => `<svg viewBox="0 0 24 24" aria-hidden="true">${icons[name] || icons.box}</svg>`;
const bytes = n => {if(!n)return '0 B';const u=['B','KB','MB','GB','TB','PB'];const i=Math.min(Math.floor(Math.log(n)/Math.log(1024)),5);return `${(n/1024**i).toFixed(i>1?1:0)} ${u[i]}`;};
const date = n => new Date(n*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false});
function toast(message,error=false){const el=$('#toast');el.textContent=message;el.className='show'+(error?' error':'');clearTimeout(toast.timer);toast.timer=setTimeout(()=>el.className='',4000);}
async function api(path, options={}){
  const headers = {...(options.body && !(options.body instanceof Blob) ? {'Content-Type':'application/json'} : {}),...(state.user?.csrf ? {'X-Drive-CSRF':state.user.csrf}:{}),...options.headers};
  const r = await fetch('/api'+path,{...options,headers});
  let data;try{data=await r.json();}catch{throw new Error('服务器响应异常，请稍后重试');}
  if(!r.ok){
    if(r.status===401&&state.user){state.user=null;await boot();}
    if(r.status===402&&state.user&&typeof refreshLicenseNotice==='function')await refreshLicenseNotice();
    const error = new Error(typeof data.detail==='string'?data.detail:'输入格式有误，请检查后重试');
    error.code = data.code;
    throw error;
  }
  return data;
}
const json = (method,body) => ({method,body:JSON.stringify(body)});
const folderArt = `<div class="folder-art" aria-hidden="true"><div class="art-ring"></div><div class="art-folder back"></div><div class="art-folder front">${icon('shield')}</div><span class="art-spark">✳</span></div>`;
const brand = `<div class="brand"><div class="brand-mark"><img src="/assets/suyan-logo.png" alt="苏盐标志"></div><div><strong>苏盐新云盘</strong><small>TEAM DRIVE</small></div></div>`;

function authPage(setup){
  $('#app').innerHTML=`<div class="auth"><aside class="auth-art">${brand}<div><h1>资料各归其位，<br>团队随时相连。</h1><p>公司的文件，有自己的空间。<br>在办公室，也在每一个需要它的地方。</p>${folderArt}</div><footer>YOUR FILES. YOUR SPACE.</footer></aside><main class="auth-main"><div class="auth-form"><div class="eyebrow">COMPANY WORKSPACE</div><h2>${setup?'开启团队的文件空间':'欢迎回来'}</h2><p>${setup?'创建第一个管理员账号，开始管理成员和共享资料。':'登录苏盐新云盘，访问与你共享的团队文件。'}</p><form id="auth-form" class="form-stack">${setup?'<label>初始化密钥<input name="key" autocomplete="off" required placeholder="从服务器 state/setup.key 获取"></label><label>你的姓名<input name="name" autocomplete="name" required maxlength="60" placeholder="例如：张明"></label>':''}<label>账号<input name="username" autocomplete="username" required maxlength="40" placeholder="请输入账号"></label><label>密码<input name="password" type="password" autocomplete="${setup?'new-password':'current-password'}" required ${setup?'minlength="6"':''} maxlength="200" placeholder="${setup?'至少 6 位':'请输入密码'}"></label><div class="form-error" id="auth-error" role="alert"></div><button class="btn primary" type="submit">${setup?'创建并进入云盘':'登录云盘'} ${icon('arrow')}</button></form><div class="auth-foot">${setup?'初始化密钥仅使用一次，创建成功后会自动失效。':'账号由公司管理员分配。忘记密码请联系管理员。'}</div></div></main></div>`;
  $('#auth-form').addEventListener('submit',async e=>{e.preventDefault();const button=e.target.querySelector('button');button.disabled=true;$('#auth-error').textContent='';try{state.user=await api(setup?'/setup':'/login',json('POST',Object.fromEntries(new FormData(e.target))));await loadWorkspace();}catch(err){$('#auth-error').textContent=err.message;}finally{button.disabled=false;}});
}
async function boot(){try{const status=await api('/status');if(!status.initialized){authPage(true);return;}try{state.user=await api('/me');await loadWorkspace();}catch{authPage(false);}}catch(err){$('#app').innerHTML=`<div class="empty"><h3>暂时无法连接云盘</h3><p>${esc(err.message)}</p><button class="btn secondary" id="retry-boot">重新连接</button></div>`;$('#retry-boot').onclick=boot;}}
function modal(title,description,body,foot=''){const d=$('#modal');$('#modal-body').innerHTML=`<header class="modal-head"><div><h2>${esc(title)}</h2><p>${esc(description)}</p></div><button class="icon-btn" data-close aria-label="关闭">${icon('close')}</button></header><div class="modal-content">${body}</div>${foot?`<footer class="modal-foot">${foot}</footer>`:''}`;if(!d.open)d.showModal();}
function confirmAction(title, description, accept = '确认', items = []) {
  if ($('#confirmation')) return Promise.resolve(false);
  const dialog = document.createElement('dialog');
  dialog.id = 'confirmation';
  dialog.setAttribute('closedby', 'none');
  dialog.setAttribute('aria-labelledby', 'confirmation-title');
  dialog.setAttribute('aria-describedby', 'confirmation-description');
  dialog.innerHTML = `<header class="modal-head"><h2 id="confirmation-title">${esc(title)}</h2><button type="button" class="icon-btn" data-confirm-cancel aria-label="关闭">${icon('close')}</button></header>
    <div class="modal-content"><p id="confirmation-description">${esc(description)}</p>${items.length ? `<ul class="confirmation-items">${items.map(item => `<li>${esc(item)}</li>`).join('')}</ul>` : ''}</div>
    <footer class="modal-foot"><button type="button" class="btn secondary" data-confirm-cancel autofocus>取消</button><button type="button" class="btn danger-solid" data-confirm-accept>${esc(accept)}</button></footer>`;
  document.body.append(dialog);
  return new Promise(resolve => {
    dialog.oncancel = event => event.preventDefault();
    dialog.onclose = () => {
      const confirmed = dialog.returnValue === 'confirm';
      dialog.remove();
      resolve(confirmed);
    };
    dialog.querySelectorAll('[data-confirm-cancel]').forEach(control => control.onclick = () => dialog.close('cancel'));
    dialog.querySelector('[data-confirm-accept]').onclick = () => dialog.close('confirm');
    dialog.showModal();
  });
}
function formModal(title,description,fields,submit,fn){
  modal(title,description,`<form id="modal-form" class="form-stack">${fields}<div class="form-error" id="modal-error" role="alert"></div><button class="btn primary" type="submit">${esc(submit)}</button></form>`);
  $('#modal-form').onsubmit=async e=>{e.preventDefault();const b=e.target.querySelector('button[type=submit]');b.disabled=true;try{await fn(Object.fromEntries(new FormData(e.target)));$('#modal').close();}catch(err){$('#modal-error').textContent=err.message;}finally{b.disabled=false;}};
}
function newFolder(){
  const {id} = state.group;
  const folder = state.path;
  formModal('新建文件夹','','<label>文件夹名称<input name="name" required maxlength="180"></label>','创建',async ({name})=>{
    if (name.includes('/') || name.includes('\\')) throw new Error('名称不能包含路径分隔符');
    await api(`/groups/${id}/folders`,json('POST',{path:[folder,name].filter(Boolean).join('/')}));
    await openGroup(id,folder);
    toast('文件夹已创建');
  });
}
async function uploadFingerprint(file) {
  if (!globalThis.crypto?.subtle) return 'single-upload:' + Math.random().toString(36) + Date.now();
  const size = 4 * 1024 * 1024;
  const blocks = new Uint8Array(Math.ceil(file.size / size) * 32);
  for (let offset = 0; offset < file.size; offset += size) {
    const bytes = await file.slice(offset, offset + size).arrayBuffer();
    const digest = await crypto.subtle.digest('SHA-256', bytes);
    blocks.set(new Uint8Array(digest), offset / size * 32);
  }
  const digest = await crypto.subtle.digest('SHA-256', blocks);
  return 'sha256-blocks-v1:' + Array.from(new Uint8Array(digest), value => value.toString(16).padStart(2, '0')).join('');
}

async function uploadFiles(files, overwrite=false, folderUpload=false){
  if(!files.length)return;if(state.upload){toast('请等待当前上传完成',true);return;}
  const group=state.group,folder=state.path;state.upload=true;const transfer=$('#transfer');transfer.hidden=false;let done=0,failures=0;
  const preparedFolders = new Set();
  for(const file of files){
    transfer.innerHTML=`<header><span>${icon('upload')} 上传文件 ${done+failures+1} / ${files.length}</span><span id="upload-percent">0%</span></header><div class="transfer-name">${esc(file.name)}</div><progress max="100" value="0"></progress><p id="upload-info">准备上传…</p>`;
    try{
      $('#upload-info').textContent='正在校验文件内容…';
      const fingerprint = await uploadFingerprint(file);
      const relative = folderUpload && file.webkitRelativePath ? file.webkitRelativePath : file.name;
      const target = [folder, relative].filter(Boolean).join('/');
      const parts = target.split('/');
      for (let depth = folder ? folder.split('/').length + 1 : 1; depth < parts.length; depth++) {
        const path = parts.slice(0, depth).join('/');
        if (preparedFolders.has(path)) continue;
        await api(`/libraries/${group.id}/ensure-folder`, json('POST', {path}));
        preparedFolders.add(path);
      }
      const task=await api('/uploads',json('POST',{group_id:group.id,path:target,size:file.size,fingerprint,overwrite}));let offset=task.offset;
      while(offset<file.size){
        const next=Math.min(offset+4*1024*1024,file.size);let sent=false;
        for(let attempt=0;attempt<3&&!sent;attempt++){
          try{const result=await api(`/uploads/${task.id}`,{method:'PATCH',headers:{'Content-Type':'application/octet-stream','Upload-Offset':String(offset)},body:file.slice(offset,next)});offset=result.offset;sent=true;}catch(err){if(attempt===2)throw err;await new Promise(r=>setTimeout(r,700));const progress=await api(`/uploads/${task.id}`);offset=progress.offset;if(offset>=next)sent=true;}
        }
        const pct=Math.round(offset/file.size*100);transfer.querySelector('progress').value=pct;$('#upload-percent').textContent=pct+'%';$('#upload-info').textContent=`${bytes(offset)} / ${bytes(file.size)}`;
      }
      $('#upload-info').textContent='正在校验并保存…';await api(`/uploads/${task.id}/complete`,{method:'POST'});done++;
    }catch(err){failures++;toast(`${file.name}：${err.message}`,true);}
  }
  state.upload=false;transfer.innerHTML=`<header><span>${icon('check')} 上传结束</span><button class="icon-btn" id="close-transfer" aria-label="关闭上传面板">${icon('close')}</button></header><p style="margin-top:14px">成功 ${done} 个${failures?`，失败 ${failures} 个。重新选择原文件可继续上传。`:'，文件已保存到共享空间。'}</p>`;$('#close-transfer').onclick=()=>transfer.hidden=true;
  if(state.group?.id===group.id&&state.path===folder)await openGroup(group.id,folder);if(done)toast(`已上传 ${done} 个文件`);
}
async function usersPage() {
  state.group = null;
  state.view = 'users';
  $('#top-label').textContent = '成员管理';
  const generation = workspace.generation;
  const users = await api('/users');
  if (generation !== workspace.generation) return;
  const isAdmin = state.user.admin;
  const memberActions = u => {
    if (u.id === state.user.id) return '<small>当前账号</small>';
    if (!isAdmin) return '—';
    return `<div class="toolbar"><button class="btn secondary" data-work="reset-member-password" data-uid="${u.id}" data-name="${esc(u.name)}">重置密码</button><button class="btn ${u.active?'danger':'soft'}" data-toggle-user="${u.id}" data-name="${esc(u.name)}" data-active="${u.active?'0':'1'}">${u.active?'停用':'启用'}</button></div>`;
  };
  $('#content').innerHTML = heading('成员管理',isAdmin ? '创建员工账号，再将成员加入对应分组。' : '查看、新增普通员工账号。',`<button class="btn primary" data-action="new-user">${icon('plus')}添加成员</button>`) +
    `<section class="panel"><div class="panel-head members-head"><h2>全部成员 <span class="muted">${users.length}</span></h2>
      <form id="users-search" class="search" role="search" aria-label="搜索成员">${icon('search')}<input type="search" name="q" aria-label="按姓名或账号搜索成员" placeholder="搜索成员姓名或账号" maxlength="100" value="${esc(state.usersQuery)}"><button type="button" class="icon-btn" id="clear-users-search" aria-label="清空成员搜索">${icon('close')}</button></form>
      <select id="users-active" aria-label="按账号状态筛选"><option value="all">全部状态</option><option value="active">正常</option><option value="inactive">已停用</option></select>
      ${isAdmin ? '<small>账号停用后，网页登录和电脑连接会同时失效</small>' : ''}</div>
      <div id="users-search-summary" class="members-summary muted" role="status" aria-live="polite"></div>
      <div class="table-wrap"><table><thead><tr><th scope="col">成员</th><th scope="col">账号</th><th scope="col">身份</th><th scope="col">状态</th><th scope="col">操作</th></tr></thead><tbody id="users-rows"></tbody></table></div>
      <div class="panel-head" id="users-pages" hidden><small id="users-page-number"></small><div class="toolbar"><button type="button" class="btn secondary" id="users-prev">上一页</button><button type="button" class="btn secondary" id="users-next">下一页</button></div></div></section>${footer()}`;
  // Filter the complete directory already loaded for this page; typing does not trigger directory syncs.
  const searchable = users.map(user => ({user, name: user.name.toLocaleLowerCase(), username: user.username.toLocaleLowerCase()}));
  const filter = $('#users-active');
  filter.value = state.usersActive || 'all';
  let page = 0;
  const pageSize = 50;
  const renderUsers = () => {
    const query = state.usersQuery.trim().toLocaleLowerCase();
    const matches = searchable.filter(item => (item.name.includes(query) || item.username.includes(query)) &&
      (filter.value === 'all' || Boolean(item.user.active) === (filter.value === 'active'))).map(item => item.user);
    const pageCount = Math.max(1, Math.ceil(matches.length / pageSize));
    page = Math.min(page, pageCount - 1);
    $('#users-pages').hidden = pageCount === 1;
    $('#users-page-number').textContent = `第 ${page + 1} / ${pageCount} 页`;
    $('#users-prev').disabled = page === 0;
    $('#users-next').disabled = page + 1 === pageCount;
    $('#users-search-summary').textContent = query || filter.value !== 'all' ? `找到 ${matches.length} 位成员，共 ${users.length} 位` : `共 ${users.length} 位成员`;
    $('#clear-users-search').hidden = !state.usersQuery;
    $('#users-rows').innerHTML = matches.slice(page * pageSize, (page + 1) * pageSize).map(u => `<tr><td><div class="file-cell"><span class="avatar">${esc(u.name.slice(0,1))}</span>${esc(u.name)}</div></td><td>${esc(u.username)}</td><td>${u.admin?'管理员':'成员'}</td><td><span class="badge ${u.active?'':'off'}">${u.active?'正常':'已停用'}</span></td><td>${memberActions(u)}</td></tr>`).join('') || `<tr><td colspan="5"><div class="empty"><h3>${users.length?'没有找到匹配的成员':'暂无成员'}</h3><p>${users.length?'请调整搜索词或账号状态。':'点击“添加成员”创建账号。'}</p></div></td></tr>`;
  };
  $('#users-search').onsubmit = event => { event.preventDefault(); renderUsers(); };
  $('#users-search input').oninput = event => { page = 0; state.usersQuery = event.target.value; renderUsers(); };
  $('#clear-users-search').onclick = () => { page = 0; state.usersQuery = ''; $('#users-search input').value = ''; renderUsers(); $('#users-search input').focus(); };
  filter.onchange = () => { page = 0; state.usersActive = filter.value; renderUsers(); };
  $('#users-prev').onclick = () => { page--; renderUsers(); };
  $('#users-next').onclick = () => { page++; renderUsers(); };
  renderUsers();
}
function newUser(){formModal('添加团队成员','创建账号后，在共享分组中分配访问权限。','<label>姓名<input name="name" required maxlength="60" placeholder="成员姓名"></label><label>登录账号<input name="username" required minlength="2" maxlength="40" placeholder="可直接用中文姓名，或字母、数字"></label><label>初始密码<input name="password" type="password" autocomplete="new-password" required minlength="6" maxlength="200" placeholder="至少 6 位，无需组合大小写或符号"></label>','创建成员',async d=>{await api('/users',json('POST',d));await usersPage();toast('成员已创建，请将其加入分组');});}
async function trashPage(){state.group=null;state.view='trash';$('#top-label').textContent='回收站';const rows=await api('/trash');$('#content').innerHTML=heading('回收站','删除的文件保存在这里，管理员可以恢复。')+`<section class="panel">${rows.length?`<div class="table-wrap"><table><thead><tr><th>原文件路径</th><th>所属分组</th><th class="hide-mobile">删除时间</th><th>操作</th></tr></thead><tbody>${rows.map(r=>`<tr><td><div class="file-cell">${icon(r.is_dir?'folder':'box')}${esc(r.path)}</div></td><td>${esc(r.group_name)}</td><td class="muted hide-mobile">${date(r.deleted)}</td><td><button class="btn soft" data-restore="${r.id}">${icon('refresh')}恢复</button></td></tr>`).join('')}</tbody></table></div>`:`<div class="empty">${icon('trash')}<h3>回收站是空的</h3><p>从网页或电脑盘删除的文件会出现在这里。</p></div>`}</section>${footer()}`;}
async function eventsPage(){state.group=null;state.view='events';$('#top-label').textContent='活动记录';const rows=await api('/events');const labels={upload:'上传 / 更新文件',download:'下载文件',mkdir:'新建文件夹',rename:'重命名',delete:'移入回收站',restore:'恢复文件',move:'移动文件',membership:'更新分组成员'};$('#content').innerHTML=heading('活动记录','查看共享空间最近发生的文件操作。')+`<section class="panel">${rows.length?`<div class="table-wrap"><table><thead><tr><th>操作内容</th><th>分组</th><th>操作人</th><th class="hide-mobile">时间</th></tr></thead><tbody>${rows.map(r=>`<tr><td><div class="event-action">${esc(labels[r.action]||r.action)}</div><div class="event-path">${esc(r.path)}</div></td><td>${esc(r.group_name)}</td><td>${esc(r.actor||'成员')}</td><td class="muted hide-mobile">${date(r.created)}</td></tr>`).join('')}</tbody></table></div>`:`<div class="empty">${icon('clock')}<h3>还没有活动记录</h3><p>上传第一份共享文件，开始团队协作。</p></div>`}</section>${footer()}`;}
async function connect(){
  const [tokens,client]=await Promise.all([api('/tokens'),api('/client')]);const endpoint=location.origin+'/dav/';
  modal('把云盘连接到电脑','安装连接助手，登录后在资源管理器中打开苏盐新云盘。',`<div class="connect-step"><strong>01　安装 Windows 连接助手</strong><p>适用于 Windows 10/11 64 位。安装后打开桌面的“苏盐新云盘”；首次使用，按助手提示安装磁盘组件。</p>${client.available?`<a class="btn primary" style="margin-top:13px" href="/api/client/windows" download>${icon('download')}下载 Windows 连接助手 <span style="font-weight:400;font-size:11px">${bytes(client.size)}</span></a>`:'<div class="notice" style="margin-top:12px">管理员尚未放置安装包，请联系管理员获取。</div>'}</div><div class="connect-step"><strong>02　用你的云盘账号登录助手</strong><p>填写以下网站地址、账号，以及你平时登录网页的密码。助手会自动配置电脑盘，无需手工生成密码或输入命令。</p><div class="copy-field"><input readonly id="server-url" value="${esc(location.origin)}" aria-label="云盘网站地址"><button class="btn secondary" id="copy-url">复制地址</button></div><p style="margin-top:9px">账号：<b>${esc(state.user.username)}</b>　默认盘符：<b>Z:</b></p>${['localhost','127.0.0.1','[::1]'].includes(location.hostname)?'<div class="notice" style="margin-top:12px">当前是本机试用地址。给其他员工使用时，请由管理员提供 NAS 的局域网地址或苏盐新云盘域名。</div>':''}</div><div class="connect-step"><strong>03　点击“登录并连接”</strong><p>资源管理器中会出现“苏盐新云盘（Z:）”。关闭助手窗口后，云盘仍可在托盘中运行；断开前请关闭文档并等待上传完成。</p></div><details style="border-top:1px solid var(--line);padding-top:17px"><summary style="cursor:pointer;color:var(--muted);font-size:12px">高级：手动配置 WebDAV</summary><div class="connect-step" style="margin-top:17px"><p>WebDAV 地址：<code>${esc(endpoint)}</code>。手动挂载时，使用下方生成的独立连接密码。</p><form id="token-form" class="inline-form" style="margin:12px 0"><input name="name" placeholder="设备名称，例如：办公室电脑" required maxlength="60" style="flex:1"><button class="btn secondary">生成密码</button></form><div id="new-token"></div><p>rclone 中新建名为 company 的 WebDAV 连接，vendor 选 other，填写上述 WebDAV 地址、账号和独立连接密码。再运行：</p><pre class="code-block">rclone mount company: Z: --vfs-cache-mode full --network-mode</pre></div></details><div class="subheading">已授权的电脑</div><div id="token-list">${tokens.map(t=>`<div class="member-row"><div class="who"><strong>${esc(t.name)}</strong><small>${date(t.created)}</small></div><button class="btn danger" data-revoke="${t.id}">撤销连接</button></div>`).join('')||'<p class="muted" id="no-tokens" style="font-size:12px">还没有已授权的电脑</p>'}</div>`);
  $('#copy-url').onclick=async()=>{try{await navigator.clipboard.writeText(location.origin);toast('地址已复制');}catch{$('#server-url').select();toast('已选中地址，请按 Ctrl+C 复制');}};
  $('#token-form').onsubmit=async e=>{e.preventDefault();const b=e.target.querySelector('button');b.disabled=true;try{const t=await api('/tokens',json('POST',Object.fromEntries(new FormData(e.target))));$('#new-token').innerHTML=`<div class="token-box"><strong>请保存这次生成的连接密码</strong><code>${esc(t.password)}</code><p>密码只显示这一次，请粘贴到手动挂载客户端的密码栏。</p></div>`;$('#no-tokens')?.remove();$('#token-list').insertAdjacentHTML('afterbegin',`<div class="member-row"><div class="who"><strong>刚创建的电脑连接</strong></div><button class="btn danger" data-revoke="${t.id}">撤销连接</button></div>`);}catch(err){toast(err.message,true);}finally{b.disabled=false;}};
}
function account(){
  modal('我的账号',`${state.user.name} · ${state.user.username}`,`<div class="toolbar">
    <button class="btn secondary" data-work="profile">修改显示姓名</button>
    <button class="btn secondary" id="change-password">${icon('key')}修改登录密码</button>
    <button class="btn secondary danger" id="logout">${icon('logout')}退出登录</button>
  </div>`);
  $('#logout').onclick=async()=>{
    if(state.upload){toast('请等待上传完成后退出',true);return;}
    await api('/logout',{method:'POST'});
    $('#modal').close();
    state.user=null;
    authPage(false);
  };
  $('#change-password').onclick=()=>formModal('修改密码','修改后需要重新登录，并会撤销已有电脑连接。','<label>当前密码<input name="old_password" type="password" autocomplete="current-password" required></label><label>新密码<input name="new_password" type="password" autocomplete="new-password" minlength="6" maxlength="200" placeholder="至少 6 位" required></label>','更新密码',async values=>{
    await api('/password',json('POST',values));
    state.user=null;
    authPage(false);
    toast('密码已更新，请重新登录');
  });
}

document.addEventListener('click',async e=>{const el=e.target.closest('button');if(!el)return;try{
  if(el.hasAttribute('data-close')){$('#modal').close();return;}
  if(el.dataset.nav){await routeNav(el.dataset.nav);return;}
  if(el.hasAttribute('data-path')){await openGroup(state.group.id,el.dataset.path);return;}
  if(el.dataset.toggleUser){if(el.dataset.active==='0'&&!await confirmAction('停用成员账号','停用后，该成员的网页登录和电脑连接会立即失效。','确认停用',[el.dataset.name]))return;await api(`/users/${el.dataset.toggleUser}`,json('PATCH',{active:el.dataset.active==='1'}));await usersPage();toast('账号状态已更新');return;}
  if(el.dataset.restore){await api(`/trash/${el.dataset.restore}/restore`,{method:'POST'});await trashPage();toast('已恢复到原位置');return;}
  if(el.dataset.revoke){if(!await confirmAction('撤销电脑连接','撤销后，这台电脑需要重新授权才能连接云盘。','确认撤销',[el.closest('.member-row').querySelector('strong').textContent]))return;await api(`/tokens/${el.dataset.revoke}`,{method:'DELETE'});el.closest('.member-row').remove();toast('电脑连接已撤销');return;}
  const actions={'new-folder':newFolder,'new-user':newUser,'connect':connect,'account':account};if(actions[el.dataset.action])await actions[el.dataset.action]();
}catch(err){toast(err.message,true);}});
$('#modal').addEventListener('cancel',event=>event.preventDefault());
window.addEventListener('beforeunload',e=>{if(state.upload){e.preventDefault();e.returnValue='';}});
