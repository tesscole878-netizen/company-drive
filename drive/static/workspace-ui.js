'use strict';

const workspaceTabs = {
  mine: '我的资料库', shared: '共享给我的', public: '公共资料库', groups: '群组共享', legacy: '旧资料库',
  favorites: '收藏夹', events: '文件活动', published: '已发布的资料库',
  devices: '已连接的设备', shares: '共享管理', trash: '文件回收站',
  deleted: '已删除的资料库', users: '成员管理', search: '搜索结果', license: '授权管理', usage: '使用情况'
};
const libraryViews = new Set(['mine', 'shared', 'public']);
const textExtensions = new Set(['txt','md','markdown','csv','json','xml','log','yaml','yml','ini','py','js','ts','css','html','sql']);
const documentExtensions = new Set(['docx','xlsx','pptx']);
const imageExtensions = new Set(['jpg','jpeg','png','gif','webp','bmp']);
const workspace = {
  libraries: [], favorites: [], banner: '', unread: 0, officeEnabled: false, legacyEnabled: false,
  selected: new Set(), display: localStorage.getItem('drive-view') || 'list',
  order: 'name', descending: false, generation: 0, currentGroup: '',
  filePage: 0, fileTotal: 0, fileRequest: 0, fileEntries: new Map(),
  groupsExpanded: !matchMedia('(max-width: 720px)').matches,
  sharesTab: 'library', search: '', tag: '', searchCursor: '', searchBack: []
};
workspace.license = null;
const urlPath = path => encodeURIComponent(path);
const parentPath = path => path.split('/').slice(0, -1).join('/');
const inPath = (path, root) => !root || path === root || path.startsWith(root + '/');
const libraryURL = (lid, endpoint) => `/libraries/${lid}/${endpoint}`;
const roleText = role => role === 'editor' ? '可编辑' : '只读';
const legacyTextExtensions = new Set(['txt','md','csv','tsv','log','json','xml','ini','yaml','yml']);
const legacyPermissionText = permission => ({rw:'可编辑',r:'只读',preview:'仅预览',preview_edit:'预览与编辑',cloud_edit:'预览与编辑',admin:'管理'})[permission] || (permission ? '原库权限' : '—');
const legacyAccessText = lib => lib.effective_permission ? legacyPermissionText(lib.effective_permission) :
  (lib.permission ? '共享标记：' + legacyPermissionText(lib.permission) : '—');
const legacyDisplayName = name => name.replaceAll('\x7f', '␡');
const pathName = path => path.split('/').at(-1);
const extension = name => name.includes('.') ? name.split('.').at(-1).toLowerCase() : '';
const activeLibrary = () => state.group;

function currentFileChangeDirectory() {
  const folder = workspace.fileChangeDirectory;
  return folder?.key === workspace.generation ? folder : null;
}
const FILE_PAGE_SIZE = 100;
const metadataDate = new Intl.DateTimeFormat('zh-CN', {timeZone:'Asia/Shanghai', year:'numeric', month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit', hourCycle:'h23'});
const sizeText = value => Number.isFinite(value) && value >= 0 ? bytes(value) : '—';
const modifiedText = value => Number.isFinite(value) && value > 0 ? metadataDate.format(value * 1000) : '—';
const fileKinds = {
  sheet: ['表格','xlsx xls xlsm xlsb ods csv tsv et','<path d="M7 10h10v7H7zm0 3.5h10m-5-3.5v7"/>'],
  word: ['文档','docx doc docm odt rtf wps','<path d="M7 10h10m-10 3h10m-10 3h7"/>'],
  slides: ['演示文稿','ppt pptx pptm odp dps','<path d="M7 10h10v6H7zm5 6v2m-2 0h4"/>'],
  pdf: ['PDF','pdf','<path d="M7 15.5c2-1 4-4.8 4.3-6 .3 3.5 2.1 5.4 5.3 5.4-3.5-1.7-6.4-.4-9.6 2"/>'],
  image: ['图片','png jpg jpeg gif webp bmp tif tiff svg heic ico','<rect x="6.5" y="9.5" width="11" height="8" rx="1"/><path d="m7 16 3-3 2.5 2 2-2 3 3"/><circle cx="14.5" cy="12" r=".5"/>'],
  archive: ['压缩包','zip rar 7z tar gz bz2 xz tgz','<path d="M12 8v2m0 1v2m0 1v2"/><rect x="10.5" y="16" width="3" height="2" rx=".5"/>'],
  media: ['音视频','mp4 mov avi mkv webm mp3 wav flac ogg m4a','<path d="M10 10l5 3.5-5 3.5z"/>'],
  text: ['文本','txt md markdown log ini json xml yaml yml js ts py css html sql','<path d="M7 11h10m-10 3h8m-8 3h6"/>'],
};
const kindByExtension = new Map(Object.entries(fileKinds).flatMap(([kind, [, extensions]]) => extensions.split(' ').map(ext => [ext, kind])));
const pageOutline = '<path d="M13.5 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9.5zM13 3v5a2 2 0 0 0 2 2h5"/>';
const specialGlyphs = {
  folder: ['文件夹', '<path d="M3 6a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2zM3 9h18"/>'],
  library: ['资料库', '<path d="M4 7h16v13H4zM3 4h18v3H3zm6 7h6m-6 4h6"/>'],
  locked: ['加密资料库', '<rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V7a4 4 0 1 1 8 0v3m-4 5v2"/>'],
};

function heading(title, description = '', action = '') {
  return `<div class="page-heading"><div><h1>${esc(title)}</h1>${description ? `<p>${esc(description)}</p>` : ''}</div><div class="toolbar">${action}</div></div>`;
}

function footer() { return ''; }

function button(text, action, data = '', style = 'secondary', glyph = '') {
  return `<button type="button" class="btn ${style}" data-work="${action}" ${data}>${glyph ? icon(glyph) : ''}${esc(text)}</button>`;
}

function table(headers, rows, empty) {
  if (!rows.length) return `<div class="empty"><h3>${esc(empty)}</h3></div>`;
  return `<div class="table-wrap"><table><thead><tr>${headers.map(label => `<th scope="col">${esc(label)}</th>`).join('')}</tr></thead><tbody>${rows.join('')}</tbody></table></div>`;
}

function page(title, content, actions = '', description = '') {
  $('#top-label').textContent = title;
  $('#content').innerHTML = heading(title, description, actions) + `<section class="panel">${content}</section>`;
}

function navItem(key, glyph) {
  const active = state.view === key;
  return `<button type="button" class="nav-item ${active ? 'active' : ''}" data-nav="${key}" ${active ? 'aria-current="page"' : ''}>${icon(glyph)}${workspaceTabs[key]}</button>`;
}

function groupNavigation() {
  const selected = state.view === 'groups' ? workspace.currentGroup : null;
  const groups = [{id: '', name: '所有群组'}, ...state.groups];
  const entries = groups.map(({id, name}) => `<button type="button" class="group-nav-item" data-work="select-team" data-gid="${esc(id)}" ${selected === id ? 'aria-current="page"' : ''} title="${esc(name)}"><span aria-hidden="true">#</span><span>${esc(name)}</span></button>`);
  const collapsedActive = selected !== null && !workspace.groupsExpanded;
  return `<button type="button" class="nav-item group-nav-toggle${collapsedActive ? ' active' : ''}" data-work="toggle-team-list" aria-expanded="${workspace.groupsExpanded}" aria-controls="sidebar-groups">${icon('users')}群组共享<span class="group-nav-chevron" aria-hidden="true"></span></button>
    <div class="group-nav-list" id="sidebar-groups" role="group" aria-label="群组列表" ${workspace.groupsExpanded ? '' : 'hidden'}>${entries.join('')}</div>`;
}

function renderShell() {
  const sidebarScroll = $('.sidebar-scroll')?.scrollTop ?? 0;
  $('#app').innerHTML = `<div class="shell library-shell">
    <aside class="sidebar">
      ${brand}
      <div class="sidebar-scroll">
        <div class="nav-label">文件</div>
        <nav class="workspace-file-nav" aria-label="资料库">${navItem('mine','box')}${navItem('shared','users')}${navItem('public','folder')}${groupNavigation()}</nav>
        <div class="nav-label">工具</div>
        <nav aria-label="工具">${navItem('favorites','star')}${navItem('events','clock')}${navItem('published','globe')}${navItem('devices','monitor')}${navItem('shares','link')}${state.user.admin ? navItem('usage','monitor') + navItem('trash','trash') + navItem('deleted','folder') : ''}${state.user.can_manage_members ? navItem('users','users') : ''}${state.user.admin ? navItem('license','key') : ''}</nav>
      </div>
      <div class="sidebar-bottom"><button type="button" class="nav-item" data-action="connect">${icon('monitor')}连接电脑</button></div>
    </aside>
    <main class="workspace">
      <div id="license-banner" class="license-banner" ${workspace.license?.read_only ? '' : 'hidden'} role="status"><span>${esc(workspace.license?.message || '')}</span>${state.user.admin ? '<button class="btn secondary" data-nav="license">授权管理</button>' : ''}</div>
      ${workspace.banner ? `<div class="announcement" role="status"><span>${esc(workspace.banner)}</span><button class="icon-btn" data-work="dismiss-banner" aria-label="关闭公告">${icon('close')}</button></div>` : ''}
      <header class="topbar">
        <div class="crumb-top"><strong id="top-label">${workspaceTabs[state.view] || '资料库'}</strong></div>
        <div class="top-right">
          ${state.user.admin ? `<button class="license-status-button" data-nav="license" id="license-status-button">${licenseLabel()}</button>` : ''}
          <form id="workspace-search" class="search" role="search">${icon('search')}<input name="q" aria-label="搜索资料库中的文件" placeholder="搜索文件名称" required maxlength="100" value="${esc(state.view === 'search' ? workspace.search : '')}"></form>
          <button type="button" class="icon-btn notification-button" data-work="notifications" aria-label="通知${workspace.unread ? '，' + workspace.unread + ' 条未读' : ''}">${icon('bell')}${workspace.unread ? `<sup>${workspace.unread}</sup>` : ''}</button>
          <button type="button" class="user-button" data-action="account"><span class="avatar">${esc(state.user.name.slice(0,1))}</span><span class="user-info"><strong>${esc(state.user.name)}</strong><small>${state.user.admin ? '管理员' : '成员'}</small></span></button>
        </div>
      </header>
      <div class="content" id="content"></div>
    </main>
  </div>`;
  $('.sidebar-scroll').scrollTop = sidebarScroll;
  $('#workspace-search').addEventListener('submit', async event => {
    event.preventDefault();
    workspace.search = new FormData(event.currentTarget).get('q').trim();
    workspace.tag = '';
    if (workspace.search) await runAction(() => routeNav('search'));
  });
}

async function updateWorkspace() {
  const data = await api('/workspace?include_favorites=true');
  state.groups = data.groups;
  workspace.libraries = data.libraries;
  workspace.favorites = data.favorites;
  workspace.banner = data.banner;
  workspace.unread = data.unread;
  workspace.officeEnabled = data.office_enabled;
  workspace.legacyEnabled = data.legacy_enabled;
  workspace.license = data.license;
}

async function loadWorkspace() {
  await updateWorkspace();
  await routeNav('mine');
}

async function routeNav(view) {
  if (view === 'users' && !state.user?.can_manage_members) {
    toast('此页面需要成员管理权限', true);
    return;
  }
  if (['trash', 'deleted', 'license', 'usage'].includes(view) && !state.user?.admin) {
    toast('此页面仅限管理员访问', true);
    return;
  }
  state.group = null;
  state.path = '';
  state.files = [];
  state.view = view;
  if (view === 'search') {
    workspace.searchCursor = '';
    workspace.searchBack = [];
  }
  workspace.generation++;
  if (view !== 'groups' || !state.groups.some(group => group.id === workspace.currentGroup)) workspace.currentGroup = '';
  renderShell();
  const routes = {
    mine: () => libraryPage('mine'), shared: () => libraryPage('shared'),
    public: () => libraryPage('public'), groups: groupPage, legacy: legacyPage, favorites: favoritesPage,
    events: eventsPage, published: () => linksPage(true), devices: devicesPage,
    shares: shareManagement, trash: trashPage, deleted: deletedPage, users: usersPage, search: searchPage,
    license: licensePage, usage: usagePage
  };
  if (routes[view]) await routes[view]();
}

function favoriteIs(lid, path) {
  return workspace.favorites.some(item => item.library_id === lid && item.path === path);
}

async function legacyPage() {
  const generation = ++workspace.generation;
  page('旧资料库', '<div class="empty muted">正在连接旧资料库…</div>');
  try {
    const {account, can_link_here} = await api('/legacy/binding');
    if (generation !== workspace.generation) return;
    const link = can_link_here ? button(account ? '重新关联账号' : '关联 Seafile 账号', 'link-legacy', '', 'primary') : '';
    if (!account) {
      const message = can_link_here ? '关联自己的 Seafile 账号后，即可按原有权限浏览和下载。' : '请联系管理员在服务器本机关联你的 Seafile 账号。';
      page('旧资料库', `<div class="empty"><h3>尚未关联 Seafile 账号</h3><p>${message}</p></div>`, link, '旧文件可浏览和下载');
      return;
    }
    const actions = link + button('解除关联', 'unlink-legacy');
    try {
      const libraries = await api('/legacy/libraries?effective=1');
      if (generation !== workspace.generation) return;
      const rows = libraries.map(lib => `<tr><td>${button(lib.name,'legacy-open',`data-repo="${esc(lib.id)}" data-name="${esc(lib.name)}" data-pathname="/" data-encrypted="${lib.encrypted ? '1' : '0'}"`,'soft legacy-name-link',lib.encrypted ? 'key' : 'folder')}</td><td>${legacyAccessText(lib)}</td></tr>`);
      page('旧资料库', table(['资料库','访问方式'], rows, '当前旧账号没有可访问的资料库'), actions, `已关联 ${account}`);
    } catch (error) {
      if (generation === workspace.generation) page('旧资料库', `<div class="empty"><p>${esc(error.message)}</p></div>`, actions);
    }
  } catch (error) {
    if (generation === workspace.generation) page('旧资料库', `<div class="empty"><p>${esc(error.message)}</p></div>`);
  }
}

const legacyURL = repo => `/legacy/libraries/${urlPath(repo)}`;
const legacyRef = (repo, name, encrypted, managed) =>
  `data-repo="${esc(repo)}" data-name="${esc(name)}" data-encrypted="${encrypted ? '1' : '0'}" data-managed="${managed ? '1' : '0'}"`;

async function legacyFolder(repo, name, path, encrypted = false, managed = false) {
  const generation = ++workspace.generation;
  workspace.fileChangeDirectory = {repo, source:'legacy', path, key:generation,
    refresh:() => legacyFolder(repo, name, path, encrypted, managed)};
  const ref = legacyRef(repo, name, encrypted, managed);
  const parent = path.slice(0, path.lastIndexOf('/')) || '/';
  const back = path !== '/' ? button('上一级','legacy-open',`${ref} data-pathname="${esc(parent)}"`) :
    state.view === 'groups' ? button('返回群组','legacy-view-back') :
    libraryViews.has(state.view) ? button('返回列表','legacy-view-back') : button('返回旧资料库','legacy-home');
  try {
    const base = legacyURL(repo);
    const entries = await api(`${base}/files?path=${urlPath(path)}`);
    if (generation !== workspace.generation) return;
    const rows = entries.map(entry => {
      const itemPath = path === '/' ? `/${entry.name}` : `${path}/${entry.name}`;
      const label = `<button type="button" class="file-name legacy-name-link" data-work="${entry.is_dir ? 'legacy-open' : 'legacy-preview'}" ${ref} data-pathname="${esc(itemPath)}">${esc(legacyDisplayName(entry.name))}</button>`;
      const suffix = extension(entry.name);
      const editable = !entry.is_dir && entry.permission === 'rw' && workspace.license?.can_write !== false;
      const editor = editable && !encrypted && documentExtensions.has(suffix) && workspace.officeEnabled
        ? `<a class="btn secondary" href="/office/editor?legacy=1&amp;library_id=${urlPath(repo)}&amp;path=${urlPath(itemPath)}" target="_blank" rel="noopener noreferrer">在线编辑</a>`
        : editable && legacyTextExtensions.has(suffix) ? button('编辑','legacy-edit-text',`${ref} data-pathname="${esc(itemPath)}"`) : '';
      const action = entry.is_dir
        ? (managed && !encrypted && entry.permission === 'rw' && workspace.license?.can_write !== false
          ? legacyManagementActions(ref, itemPath) : '')
        : `<div class="toolbar">${editor}${legacyDownloadLink(base, repo, name, itemPath, '下载', 'download')}</div>`;
      const itemRef = ref + ` data-pathname="${esc(itemPath)}"`;
      const advanced = [
        !entry.is_dir ? button('历史版本','legacy-history',itemRef) : '',
        editable ? button('上传新版本','legacy-upload-version',itemRef) : '',
        !encrypted ? button('外链','legacy-links',itemRef + ' data-folder="' + (entry.is_dir ? '1' : '0') + '"') : ''
      ].join('');
      const ops = !encrypted && entry.permission === 'rw' && workspace.license?.can_write !== false ?
        ['rename','move','delete'].map((op, i) => button(['改名','移动','删除'][i], 'legacy-entry-' + op,
          ref + ` data-pathname="${esc(itemPath)}"`)).join('') : '';
      return `<tr><td><div class="file-cell">${fileGlyph(entry)}${label}</div></td><td>${legacyPermissionText(entry.permission)}</td><td class="metadata-cell">${sizeText(entry.size)}</td><td class="metadata-cell">${modifiedText(entry.modified)}</td><td>${action}${advanced}${ops}</td></tr>`;
    });
    const here = ref + ` data-pathname="${esc(path)}"`;
    const full = button('回收站','legacy-trash',here) + (!encrypted ? button('外链','legacy-links',here + ' data-folder="1"') : '');
    const permission = workspace.license?.can_write !== false ?
      await api(base + '/folder-permission?path=' + urlPath(path)) : {};
    if (generation !== workspace.generation) return;
    const create = permission.writable ? (!encrypted ? button('新建文件夹','legacy-mkdir',here) : '') + button('上传文件','legacy-upload',here) : '';
    page(name, table(['名称','权限','大小','更新时间','操作'], rows, '此文件夹为空'), back + create + full, path);
  } catch (error) {
    if (generation !== workspace.generation) return;
    if (error.code === 'legacy_library_locked') {
      legacyUnlock(repo, name, () => legacyFolder(repo, name, path, true, managed));
      return;
    }
    page(name, `<div class="empty"><p>${esc(error.message)}</p></div>`, back, path);
  }
}

async function originalMembers(id, pageNum = 1, q = '') {
  const endpoint = '/legacy/groups/' + id + '/members';
  const [members,people,settings] = await Promise.all([api(endpoint + '?page=' + pageNum), api('/people?q=' + urlPath(q)), api('/legacy/groups/' + id + '/settings')]);
  const labels = {Owner:'所有者', Admin:'管理员', Member:'成员'};
  const rows = members.map(m => `<tr><td>${esc(m.name)}</td><td>${esc(labels[m.role] || m.role)}</td><td>${settings.can_manage_admins && m.role !== 'Owner' ? button(m.role === 'Admin' ? '取消管理员' : '设为管理员','legacy-group-admin',`data-email="${esc(m.email)}" data-membername="${esc(m.name)}" data-external="${id}" data-pagenum="${pageNum}" data-promote="${m.role === 'Admin' ? '0' : '1'}"`) : ''}${m.role === 'Owner' ? '' : button('移出','original-remove',`data-email="${esc(m.email)}" data-membername="${esc(m.name)}" data-external="${id}"`, 'danger')}</td></tr>`);
  modal('原群组成员', '变更立即作用于两个入口', `<form id="original-people"><label>查找员工<input name="q" value="${esc(q)}"></label><button>搜索</button></form>
    <form id="original-add"><label>员工<select name="uid" required><option value="">请选择</option>${people.filter(p=>p.identity_source === 'seafile' || p.username !== 'admin').map(p => `<option value="${esc(p.id)}">${esc(p.name)} · ${esc(p.username)}</option>`).join('')}</select></label><button>加入群组</button></form>
    ${table(['成员','角色','操作'],rows,'没有更多成员')}${pageNum>1 ? button('上一页','original-members',`data-external="${id}" data-pagenum="${pageNum-1}"`) : ''}${members.length===100 ? button('下一页','original-members',`data-external="${id}" data-pagenum="${pageNum+1}"`) : ''}
    ${button('群组设置','legacy-group-settings',`data-external="${id}"`)}`);
  $('#original-people').onsubmit = e => {e.preventDefault();void runAction(() => originalMembers(id,1,new FormData(e.target).get('q')));};
  $('#original-add').onsubmit = e => {e.preventDefault(); const uid = new FormData(e.target).get('uid'); void runAction(async () => {
    await api(endpoint,json('POST',{uid})); await originalMembers(id,1,q); toast('成员已加入原群组');
  });};
}

async function originalPage(el) {
  const {repo, pathname, external} = el.dataset;
  const data = await api('/legacy/open', json('POST', {repo: repo || '', path: pathname || '/', group_id: Number(external || 0)}));
  location.assign(data.url);
}

async function changeOriginal(el, action) {
  const {repo, name, pathname, managed} = el.dataset;
  const parent = action === 'mkdir' ? pathname : (parentPath(pathname) || '/');
  const submit = async value => {
    const target = action === 'mkdir' ? (parent === '/' ? '/' : parent + '/') + value : pathname;
    await api(legacyURL(repo) + '/entries', json('POST', {action, path: target, destination: value || ''}));
    await legacyFolder(repo, name, parent, false, managed === '1');
  };
  if (action === 'delete') {
    if (await confirmAction('删除文件或文件夹','删除后两个入口将同时更新。','确认删除',[pathname])) await submit('');
    return;
  }
  const label = {mkdir:'新建文件夹',rename:'重命名',move:'移动到文件夹'}[action];
  const value = action === 'move' ? '/' : action === 'rename' ? pathName(pathname) : '';
  formModal(label, pathname, `<label>${action === 'move' ? '本资料库中的目标路径' : '名称'}<input name="value" required value="${esc(value)}"></label>`, '确定', fields => submit(fields.value));
}

async function legacyEditText(repo, name, path, managed, encrypted) {
  const base = `${legacyURL(repo)}/text`;
  const {content, version} = await api(`${base}?path=${urlPath(path)}`);
  formModal(pathName(path), '保存到原资料库', `<label>文件内容<textarea name="content" rows="15" spellcheck="false">${esc(content)}</textarea></label>`,
    '保存修改', async values => {
      await api(base, json('PUT', {path, version, content: values.content}));
      await legacyFolder(repo, name, parentPath(path) || '/', encrypted, managed);
      toast('文件已保存');
    });
}

function legacyUnlock(repo, name, resume) {
  modal('解锁原加密资料库', name, `<form id="legacy-unlock" class="form-stack" autocomplete="off">
    <label>原资料库密码<input name="password" type="password" autocomplete="off" required maxlength="400"></label>
    <div class="form-error" role="alert"></div><button type="submit" class="btn primary">解锁</button></form>`);
  const dialog = $('#modal');
  const form = $('#legacy-unlock');
  const input = form.elements.password;
  const submit = form.querySelector('button');
  const message = form.querySelector('[role="alert"]');
  input.focus();
  dialog.addEventListener('close', () => { input.value = ''; }, {once:true});
  form.onsubmit = async event => {
    event.preventDefault();
    if (submit.disabled) return;
    submit.disabled = true;
    message.textContent = '';
    const request = json('POST', {password: input.value});
    input.value = '';
    try {
      await api(`/legacy/libraries/${encodeURIComponent(repo)}/unlock`, request);
    } catch (error) {
      if (dialog.open && form.isConnected) { message.textContent = error.message; input.focus(); }
      return;
    } finally {
      submit.disabled = false;
    }
    if (!dialog.open || !form.isConnected) return;
    dialog.addEventListener('close', () => runAction(resume), {once:true});
    dialog.close();
  };
}

function legacyDownloadLink(base, repo, name, path, label, glyph) {
  return `<a class="btn secondary" href="/api${base}/download?path=${urlPath(path)}" data-work="legacy-download" data-repo="${esc(repo)}" data-name="${esc(name)}" data-pathname="${esc(path)}">${glyph ? icon(glyph) : ''}${label}</a>`;
}

async function legacyDownload(repo, name, path) {
  const base = `/legacy/libraries/${encodeURIComponent(repo)}`;
  try {
    await api(`${base}/download-access?path=${urlPath(path)}`);
  } catch (error) {
    if (error.code !== 'legacy_library_locked') throw error;
    $('#modal').classList.remove('legacy-file-dialog');
    legacyUnlock(repo, name, () => legacyDownload(repo, name, path));
    return;
  }
  nativeDownload(`/api${base}/download?path=${urlPath(path)}`);
}

async function legacyPreview(repo, name, path) {
  const base = `/legacy/libraries/${encodeURIComponent(repo)}`;
  modal(pathName(path), '只读预览', '<div id="legacy-file-preview" role="status">正在打开预览…</div>', legacyDownloadLink(base, repo, name, path, '下载文件'));
  const dialog = $('#modal');
  const body = $('#legacy-file-preview');
  dialog.classList.add('legacy-file-dialog');
  dialog.addEventListener('close', () => dialog.classList.remove('legacy-file-dialog'), {once:true});
  try {
    const response = await fetch(`/api${base}/preview?path=${urlPath(path)}`);
    if (!body.isConnected || !dialog.open) return;
    if (!response.ok) {
      const value = await response.json();
      if (value.code === 'legacy_library_locked') {
        if (body.isConnected && dialog.open) {
          dialog.classList.remove('legacy-file-dialog');
          legacyUnlock(repo, name, () => legacyPreview(repo, name, path));
        }
        return;
      }
      throw new Error(value.detail || '暂时无法预览，请下载后查看');
    }
    const type = response.headers.get('content-type') || '';
    if (!body.isConnected || !dialog.open) return;
    if (type.startsWith('text/plain')) {
      const text = document.createElement('pre');
      text.className = 'text-preview';
      text.textContent = await response.text();
      body.replaceChildren(text);
    } else {
      const blob = await response.blob();
      if (!body.isConnected || !dialog.open) return;
      const url = URL.createObjectURL(blob);
      const element = document.createElement(type.startsWith('image/') ? 'img' : 'iframe');
      element.className = type.startsWith('image/') ? 'file-preview' : 'legacy-pdf-preview';
      element.src = url;
      element.title = pathName(path);
      dialog.addEventListener('close', () => URL.revokeObjectURL(url), {once:true});
      body.replaceChildren(element);
    }
  } catch (error) {
    if (body.isConnected && dialog.open) body.textContent = error.message;
  }
}

function legacyPairing() {
  formModal('关联 Seafile 账号', '使用你自己原来的 Seafile 登录账号。',
    '<label>旧账号<input name="username" required maxlength="254" autocomplete="section-legacy username"></label><label>旧密码<input name="password" type="password" required maxlength="400" autocomplete="section-legacy current-password"></label><label>验证码（启用两步验证时填写）<input name="otp" maxlength="6" inputmode="numeric" pattern="[0-9]{6}" autocomplete="one-time-code"></label>',
    '关联账号', async values => {
      await api('/legacy/binding', json('POST', values));
      await legacyPage();
      toast('已关联旧账号');
    });
}

function libraryRef(lib, path = lib.root_path) {
  return `data-lid="${esc(lib.id)}" data-rel="${esc(path || '')}"`;
}

function libraryTitle(lib) {
  return lib.root_path ? `${pathName(lib.root_path)} · ${lib.name}` : lib.name;
}

function libraryIconButton(label, glyph, action, ref, danger = false) {
  return `<button type="button" class="icon-btn library-icon-button${danger ? ' danger' : ''}" data-work="${action}" ${ref} title="${label}" aria-label="${label}">${icon(glyph)}</button>`;
}

function libraryActions(lib) {
  const ref = libraryRef(lib);
  const owner = lib.is_owner && !lib.root_path;
  const canManage = owner && workspace.license?.can_write !== false;
  const menu = [button('属性','library-properties',ref,'')];
  if (!lib.root_path && !lib.locked) menu.push(button('历史与快照','library-history',ref,''));
  if (state.user.admin && !lib.locked && writable(lib, lib.root_path)) menu.push(button('回收站','library-trash',ref,''));
  if (canManage) {
    menu.unshift(button('重命名','rename-library',ref,''), button('转让所有权','transfer-library',ref,''));
    if (!lib.locked) menu.push(button('共享管理','manage-library-shares',ref,''));
    menu.push(button('资料库设置','library-settings',ref,''));
  }
  if (lib.encrypted) menu.push(button(lib.locked ? '解锁资料库' : '锁定资料库', lib.locked ? 'open-library' : 'lock-library', ref, ''));
  return `<div class="row-actions library-row-actions">
    ${canManage && !lib.locked ? libraryIconButton('共享','share','share',ref) : ''}
    ${canManage ? libraryIconButton('删除资料库','trash','delete-library',ref,true) : ''}
    <button type="button" class="icon-btn library-icon-button" data-library-menu aria-expanded="false" title="更多操作" aria-label="更多操作">${icon('more')}</button>
    <div class="library-popover" popover="auto" aria-label="资料库操作">${menu.join('')}</div>
  </div>`;
}

function legacyManagementActions(ref, path) {
  const root = path === '/';
  const target = `${ref} data-pathname="${esc(path)}"`;
  return `<div class="row-actions library-row-actions">
    ${libraryIconButton('共享','share','legacy-share',target)}
    ${root ? libraryIconButton('删除原资料库','trash','legacy-delete-library',target,true) : ''}
    <button type="button" class="icon-btn library-icon-button" data-library-menu aria-expanded="false" title="更多操作" aria-label="更多操作">${icon('more')}</button>
    <div class="library-popover" popover="auto" aria-label="${root ? '原资料库操作' : '原文件夹操作'}">
      ${button('重命名','legacy-rename',target,'')}${button('共享管理','legacy-share',target,'')}
    </div></div>`;
}

function libraryRows(libraries, original = []) {
  const rows = libraries.map(lib => {
    const label = libraryTitle(lib);
    return `<tr data-library-metadata ${libraryRef(lib)} ${lib.locked ? 'data-locked' : ''}>
      <td><button class="icon-btn favorite ${favoriteIs(lib.id, lib.root_path) ? 'selected' : ''}" data-work="favorite" ${libraryRef(lib)} aria-label="${favoriteIs(lib.id, lib.root_path) ? '取消收藏' : '收藏'} ${esc(label)}">${icon('star')}</button></td>
      <td><div class="file-cell">${fileGlyph({kind: lib.encrypted ? 'locked' : lib.root_path ? 'folder' : 'library'})}<div>
        <button class="file-name" data-work="open-library" ${libraryRef(lib)}>${esc(label)}</button>
        ${lib.description ? `<small class="file-description">${esc(lib.description)}</small>` : ''}
      </div></div></td>
      <td>${esc(lib.owner_name)}</td><td><span class="badge ${lib.role === 'reader' ? 'reader' : ''}">${roleText(lib.role)}</span></td>
      <td class="metadata-cell" data-library-size>—</td><td class="metadata-cell" data-library-modified>—</td>
      <td>${libraryActions(lib)}</td>
    </tr>`;
  });
  for (const lib of original) {
    const managed = lib.category === 'mine' && lib.permission === 'rw';
    const ref = legacyRef(lib.id,lib.name,lib.encrypted,managed);
    const name = `<button class="file-name legacy-name-link" data-work="legacy-open" ${ref} data-pathname="/">${esc(lib.name)}</button>`;
    rows.push(`<tr>
      <td></td><td><div class="file-cell">${fileGlyph({kind: lib.encrypted ? 'locked' : 'library'})}<div>${name}
        <small class="file-description">原资料库</small></div></div></td>
      <td>${esc(lib.owner || '—')}</td><td><span class="badge ${lib.effective_permission === 'rw' ? '' : 'reader'}">${legacyAccessText(lib)}</span></td>
      <td class="metadata-cell">${sizeText(lib.size)}</td><td class="metadata-cell">${modifiedText(lib.modified)}</td>
      <td>${managed && workspace.license?.can_write !== false ? legacyManagementActions(ref,'/') : ''}</td>
    </tr>`);
  }
  return table(['','名称','所有者','权限','大小','更新时间','操作'], rows, '暂无资料库');
}

async function loadLibrarySummaries(container, generation) {
  const rows = [...container.querySelectorAll('[data-library-metadata]:not([data-locked])')];
  const keys = new Map();
  for (const row of rows) {
    const key = JSON.stringify([row.dataset.lid, row.dataset.rel]);
    if (!keys.has(key)) keys.set(key, []);
    keys.get(key).push(row);
  }
  const queue = [...keys.keys()];
  while (queue.length && generation === workspace.generation) {
    const query = new URLSearchParams();
    for (const key of queue.splice(0, 8)) {
      const [lid, path] = JSON.parse(key);
      query.append('library_id', lid);
      query.append('path', path);
    }
    try {
      const summaries = await api('/library-summaries?' + query);
      if (generation !== workspace.generation) return;
      for (const item of summaries) {
        for (const row of keys.get(JSON.stringify([item.library_id, item.path])) || []) {
          const size = row.querySelector('[data-library-size]');
          const modified = row.querySelector('[data-library-modified]');
          size.textContent = (item.partial ? '≥ ' : '') + sizeText(item.size);
          if (!item.partial) modified.textContent = modifiedText(item.modified);
          else size.title = '只统计了部分条目，可在资料库属性中查看';
        }
      }
    } catch (error) {
      if (generation === workspace.generation && container.isConnected) {
        for (const row of rows) row.querySelector('[data-library-size]').title = '暂时无法读取资料库大小';
      }
      return;
    }
  }
}

async function libraryPage(view) {
  const generation = workspace.generation;
  const originalRequest = workspace.legacyEnabled && libraryViews.has(view) ?
    api(`/legacy/libraries?view=${view}&effective=1`).then(libraries => ({libraries}), error => ({libraries: [], error: error.message})) :
    Promise.resolve({libraries: []});
  const [libraries, original] = await Promise.all([api(`/libraries?view=${view}`), originalRequest]);
  if (generation !== workspace.generation) return;
  let actions = '';
  if (view === 'mine' || (view === 'public' && state.user.admin)) {
    actions = button('新建资料库', 'new-library', `data-visibility="${view === 'public' ? 'public' : 'private'}"`, 'primary', 'plus');
  }
  if (view === 'mine' && state.user.admin) actions += button('公告', 'announcement');
  const warning = original.error ? `<div class="empty"><p>无法读取原资料库。${esc(original.error)}</p></div>` : '';
  const rows = warning && !libraries.length ? '' : libraryRows(libraries, original.libraries);
  page(workspaceTabs[view], warning + rows, actions);
  void loadLibrarySummaries($('#content'), generation);
}

function createLibrary(visibility) {
  const password = visibility === 'private' ? '<label>资料库密码（可选）<input name="password" type="password" autocomplete="new-password" minlength="10" maxlength="200"><small>填写密码后，文件内容会加密保存。请妥善保管密码。</small></label>' : '';
  formModal('新建资料库', '', '<label>名称<input name="name" required maxlength="80"></label><label>说明<textarea name="description" maxlength="500" rows="2"></textarea></label>' + password,
    '创建', async fields => {
      if (visibility === 'private' && state.user.identity_source === 'seafile' && workspace.legacyEnabled) {
        const lib = await api('/legacy/libraries', json('POST', fields));
        await updateWorkspace();
        await legacyFolder(lib.id, lib.name, '/', lib.encrypted, true);
        toast('资料库已创建，两个入口均可使用');
        return;
      }
      const lib = await api('/libraries', json('POST', {...fields, visibility}));
      await updateWorkspace();
      await openGroup(lib.id, lib.root_path);
      toast('资料库已创建');
    });
}

async function librarySettings(lid, transfer = false) {
  const lib = workspace.libraries.find(entry => entry.id === lid && entry.is_owner);
  if (!lib) throw new Error('资料库设置不可用');
  let fields = transfer ? '' : `<label>名称<input name="name" required maxlength="80" value="${esc(lib.name)}"></label>
    <label>说明<textarea name="description" maxlength="500" rows="2">${esc(lib.description)}</textarea></label>`;
  if (!transfer && state.user.admin && !lib.encrypted) {
    fields += `<label>访问范围<select name="visibility"><option value="private">按共享权限访问</option><option value="public" ${lib.visibility === 'public' ? 'selected' : ''}>所有已登录员工可读</option></select></label>`;
  }
  fields += `<div class="inline-form owner-search"><label>查找新所有者<input id="owner-query" maxlength="60" placeholder="姓名或账号"></label>
    <button type="button" id="owner-search" class="btn secondary">搜索</button></div>
    <label>所有者<select name="owner_id"><option value="${esc(lib.owner_id)}">${esc(lib.owner_name)}（当前所有者）</option></select></label>
    <small id="owner-search-status" role="status"></small>`;
  formModal(transfer ? '转让资料库所有权' : '资料库设置', lib.name, fields, transfer ? '转让' : '保存', async values => {
    if (transfer && values.owner_id === lib.owner_id) throw new Error('请先选择新所有者');
    if (values.owner_id !== lib.owner_id && !await confirmAction('转移资料库所有权','转移后，现有外链会失效。','确认转移',[$('#modal-form [name="owner_id"]').selectedOptions[0].textContent])) throw new Error('已取消转移');
    await api(`/libraries/${lid}`, json('PATCH', values));
    await updateWorkspace();
    await routeNav('mine');
    toast(transfer ? '资料库已转让' : '设置已保存');
  });
  const ownerSelect = $('#modal-form [name="owner_id"]');
  const ownerQuery = $('#owner-query');
  const ownerSearch = $('#owner-search');
  const ownerStatus = $('#owner-search-status');
  ownerSearch.onclick = async () => {
    const query = ownerQuery.value.trim();
    if (!query) { ownerStatus.textContent = '请输入姓名或账号'; return; }
    ownerSearch.disabled = true;
    ownerStatus.textContent = '正在搜索…';
    try {
      const people = await api('/people?q=' + urlPath(query));
      const currentOwner = ownerSelect.options[0].cloneNode(true);
      const previous = ownerSelect.value;
      const matches = people.filter(person => person.id !== lib.owner_id);
      ownerSelect.replaceChildren(currentOwner, ...matches.map(person => new Option(`${person.name} · ${person.username}`, person.id)));
      ownerSelect.value = matches.some(person => person.id === previous) ? previous : lib.owner_id;
      ownerStatus.textContent = matches.length ? `找到 ${matches.length} 位候选人，请选择新所有者` : '没有找到其他可选成员';
    } catch (error) { ownerStatus.textContent = error.message; }
    finally { ownerSearch.disabled = false; }
  };
  ownerQuery.onkeydown = event => {
    if (event.key === 'Enter' && !event.isComposing) {
      event.preventDefault();
      ownerSearch.click();
    }
  };
  if (!transfer) $('.modal-content').insertAdjacentHTML('beforeend', `<div class="library-settings-footer">${button('移入已删除资料库','delete-library',libraryRef(lib),'danger','trash')}${lib.encrypted && !lib.locked ? button('锁定加密资料库','lock-library',libraryRef(lib),'secondary','key') : ''}</div>`);
}

function renameLibrary(lid) {
  const lib = chooseLibrary(lid, '');
  if (!lib?.is_owner) throw new Error('需要资料库所有者权限');
  formModal('重命名资料库', lib.name, `<label>资料库名称<input name="name" required maxlength="80" value="${esc(lib.name)}"></label>`, '保存', async ({name}) => {
    await api(`/libraries/${lid}`, json('PATCH', {name}));
    await updateWorkspace();
    if (state.group?.id === lid) await openGroup(lid, state.path);
    else await routeNav(state.view);
    toast('资料库已重命名');
  });
}

async function groupPage() {
  const generation = ++workspace.generation;
  const current = state.groups.find(group => group.id === workspace.currentGroup);
  const groups = current ? [current] : state.groups;
  const title = current?.name || '所有群组';
  const actions = button('新建群组','new-team','', 'primary','plus');
  $('#top-label').textContent = title;
  $('#content').innerHTML = heading(title, '', actions) + '<div id="group-sections" aria-live="polite"></div>';
  const target = $('#group-sections');
  target.innerHTML = '<div class="empty muted">正在读取群组资料库…</div>';
  const sections = [];
  try {
    const filter = current ? '?group_id=' + urlPath(current.id) : '';
    const nativeRequest = current
      ? api('/libraries' + filter).then(libraries => ({groups, libraries: {[current.id]: libraries}}))
      : api('/group-libraries');
    const originalRequest = groups.some(group => group.identity_source === 'seafile')
      ? api('/legacy/group-libraries?effective=1' + (current ? '&group_id=' + urlPath(current.id) : '')).then(data => ({data}), error => ({data: {}, error: error.message}))
      : Promise.resolve({data: {}});
    const roleRequest = groups.some(group => group.identity_source === 'seafile') ? api('/legacy/group-roles') : {};
    const [native, original, roles] = await Promise.all([nativeRequest, originalRequest, roleRequest]);
    if (generation !== workspace.generation) return;
    if (!native.groups.length) {
      target.innerHTML = '<section class="panel empty"><h3>还没有群组</h3></section>';
      return;
    }
    for (const group of native.groups) {
      const libraries = native.libraries[group.id] || [];
      const manage = !roles[group.id] && ['owner','manager'].includes(group.management_role);
      const role = roles[group.id];
      const originalManagement = role && ['owner','manager'].includes(role.role) ?
        button('管理原群组成员','original-members',`data-external="${Number(role.id)}"`) : '';
      const previous = original.data[group.id] || [];
      const warning = original.error && group.identity_source === 'seafile' ?
        `<div class="empty"><p>无法读取原群组资料。${esc(original.error)}</p></div>` : '';
      const rows = warning && !libraries.length ? '' : libraryRows(libraries, previous);
      sections.push(`<section class="panel team-section"><div class="panel-head"><div>${current ? '' : `<h2>${esc(group.name)}</h2>`}<p>${group.identity_source === 'seafile' ? ({owner:'原群组所有者',manager:'原群组管理员',member:'原群组成员'}[role?.role] || '沿用原群组成员关系') : `${group.members} 位成员`} ${esc(group.description)}</p></div><div class="toolbar">${originalManagement}${manage ? button('管理成员','team-members',`data-gid="${group.id}"`) + button('群组设置','team-settings',`data-gid="${group.id}"`) : ''}</div></div>${warning}${rows}</section>`);
    }
    target.innerHTML = sections.join('');
    void loadLibrarySummaries(target, generation);
  } catch (error) {
    if (generation === workspace.generation) {
      target.innerHTML = `<section class="panel empty"><p>${esc(error.message)}</p></section>`;
    }
  }
}

function createTeam() {
  formModal('新建群组', '', '<label>群组名称<input name="name" required maxlength="50"></label><label>说明<textarea name="description" maxlength="240" rows="2"></textarea></label>',
    '创建', async values => {
      const group = await api('/groups', json('POST', values));
      await updateWorkspace();
      workspace.currentGroup = group.id;
      workspace.groupsExpanded = true;
      await routeNav('groups');
      toast('群组已创建');
    });
}

function teamSettings(gid) {
  const group = state.groups.find(item => item.id === gid);
  if (!group) return;
  formModal('群组设置', '', `<label>群组名称<input name="name" required maxlength="50" value="${esc(group.name)}"></label><label>说明<textarea name="description" maxlength="240" rows="2">${esc(group.description)}</textarea></label>`,
    '保存', async values => {
      await api(`/team-groups/${gid}`, json('PATCH', values));
      await updateWorkspace();
      await routeNav('groups');
      toast('群组已更新');
    });
}

async function teamMembers(gid, query = '') {
  const group = state.groups.find(item => item.id === gid);
  const [people, members] = await Promise.all([api('/people?q=' + urlPath(query)), api(`/groups/${gid}/members`)]);
  const choices = people.filter(person => !members.some(member => member.id === person.id));
  const rows = members.map(member => `<div class="member-row"><div class="who"><strong>${esc(member.name)}</strong><small>${esc(member.username)}</small></div>
    <select aria-label="${esc(member.name)}的默认资料库权限" data-member-permission="${member.id}"><option value="reader">只读</option><option value="editor" ${member.role === 'editor' ? 'selected' : ''}>可编辑</option></select>
    ${member.management_role === 'owner' ? '<small>所有者</small>' : button('移出','remove-member',`data-gid="${gid}" data-uid="${member.id}"`,'danger')}
  </div>`);
  modal('群组成员', group?.name || '', `<form class="inline-form" id="member-search"><label>查找员工<input name="q" maxlength="60" value="${esc(query)}"></label><button class="btn secondary">搜索</button></form>
    <form class="inline-form" id="member-add"><label>员工<select name="uid" required><option value="">请选择</option>${choices.map(person => `<option value="${person.id}">${esc(person.name)} · ${esc(person.username)}</option>`).join('')}</select></label>
    <label>默认资料库权限<select name="role"><option value="reader">只读</option><option value="editor">可编辑</option></select></label><button class="btn primary">加入群组</button></form>
    ${rows.join('') || '<p class="muted">暂无群组成员</p>'}`);
  $('#member-search').onsubmit = event => {
    event.preventDefault();
    const value = new FormData(event.currentTarget).get('q');
    runAction(() => teamMembers(gid, value));
  };
  $('#member-add').onsubmit = event => {
    event.preventDefault();
    const values = Object.fromEntries(new FormData(event.currentTarget));
    runAction(async () => {
      await api(`/groups/${gid}/members/${values.uid}`, json('PUT', {role: values.role}));
      await updateWorkspace();
      await groupPage();
      await teamMembers(gid);
      toast('成员已加入群组');
    });
  };
  document.querySelectorAll('[data-member-permission]').forEach(select => {
    select.onchange = () => runAction(async () => {
      await api(`/groups/${gid}/members/${select.dataset.memberPermission}`, json('PUT', {role: select.value}));
      toast('权限已保存');
    });
  });
}

function chooseLibrary(id, path) {
  const matching = workspace.libraries.filter(lib => lib.id === id && inPath(path, lib.root_path));
  return matching.sort((a,b) => b.root_path.length - a.root_path.length)[0];
}

function writable(lib, path) {
  return workspace.license?.can_write !== false && lib.write_paths.some(prefix => inPath(path, prefix));
}

function licenseLabel() {
  const license = workspace.license;
  if (!license) return '授权管理';
  if (license.read_only) return '授权受限 · 只读';
  return (license.activated ? '授权' : '试用') + '剩余 ' + license.days_remaining + ' 天';
}

async function refreshLicenseNotice() {
  if (!state.user) return;
  try {
    workspace.license = await api(state.user.admin ? '/license' : '/access-mode');
    const banner = $('#license-banner');
    if (banner) {
      banner.hidden = !workspace.license.read_only;
      banner.querySelector('span').textContent = workspace.license.message;
    }
    const label = $('#license-status-button');
    if (label) label.textContent = licenseLabel();
  } catch {}
}

async function licensePage() {
  if (!state.user?.admin) throw new Error('此页面仅限管理员访问');
  const license = await api('/license');
  workspace.license = license;
  const labels = {trial:'试用中',active:'已激活',expired:'已到期',invalid:'授权校验失败',clock_error:'服务器时间异常',not_started:'授权尚未生效'};
  const expires = new Date(license.expires_at * 1000).toLocaleString('zh-CN', {timeZone:'Asia/Shanghai',hour12:false});
  const content = `<div class="license-panel"><dl class="file-properties">
    <dt>产品</dt><dd>苏盐新云盘</dd><dt>授权状态</dt><dd>${esc(labels[license.state] || license.state)}</dd>
    <dt>客户名称</dt><dd>${esc(license.customer || '试用部署')}</dd>
    <dt>到期时间</dt><dd>${esc(expires)}（上海时间）</dd>
    <dt>剩余天数</dt><dd>${license.days_remaining} 天</dd>
    <dt>部署编号</dt><dd><code id="license-deployment">${esc(license.deployment_id)}</code> ${button('复制','copy-deployment','','soft')}</dd>
    </dl><p class="notice">${esc(license.message)}。到期后保留查看和下载，上传和修改暂停。</p>
    ${state.user.admin ? `<form id="license-activation" class="form-stack"><label>激活码<textarea name="activation_code" rows="5" required maxlength="16384" autocomplete="off" spellcheck="false" placeholder="粘贴软件提供方签发的完整激活码"></textarea></label><div class="form-error" id="activation-error" role="alert"></div><button class="btn primary">激活或续期</button></form>` : '<p class="muted">请联系管理员提交部署编号并激活授权。</p>'}</div>`;
  page('授权管理', content);
  const form = $('#license-activation');
  if (form) form.onsubmit = async event => {
    event.preventDefault();
    const submit = form.querySelector('button');
    submit.disabled = true;
    $('#activation-error').textContent = '';
    const activation_code = new FormData(form).get('activation_code').trim();
    try {
      await api('/license/activate', json('POST', {activation_code}));
      await updateWorkspace();
      await routeNav('license');
      toast('授权已激活');
    } catch (error) {
      $('#activation-error').textContent = error.message;
    } finally {
      submit.disabled = false;
    }
  };
}

async function openGroup(id, path = '') {
  let lib = chooseLibrary(id, path);
  if (!lib) {
    await updateWorkspace();
    lib = chooseLibrary(id, path);
  }
  if (!lib) throw new Error('你没有此目录的访问权限');
  if (lib.locked) {
    formModal('解锁加密资料库', lib.name, '<label>资料库密码<input name="password" type="password" required autocomplete="off" maxlength="200"></label>',
      '解锁', async values => {
        await api(libraryURL(id,'unlock'), json('POST', values));
        await updateWorkspace();
        await openGroup(id, path);
      });
    return;
  }
  const generation = ++workspace.generation;
  state.group = lib;
  state.path = path;
  workspace.fileChangeDirectory = {repo:id, source:'native', path, key:generation, refresh:loadFilePage};
  state.query = '';
  state.files = [];
  workspace.selected.clear();
  workspace.fileEntries.clear();
  workspace.fileTotal = 0;
  workspace.filePage = 0;
  const canWrite = writable(lib, path);
  const sections = path === lib.root_path ? [] : path.slice(lib.root_path.length).replace(/^\//, '').split('/');
  const crumbs = sections.map((part, index) => {
    const rel = [lib.root_path, ...sections.slice(0, index+1)].filter(Boolean).join('/');
    return `<span>/</span><button data-path="${esc(rel)}">${esc(part)}</button>`;
  });
  $('#top-label').textContent = lib.name;
  $('#content').innerHTML = heading(libraryTitle(lib), lib.description, [
    lib.is_owner ? button('共享','share',libraryRef(lib,path),'secondary','link') : '',
    !lib.root_path ? button('历史','library-history',libraryRef(lib),'secondary','clock') : '',
    state.user.admin && canWrite ? button('回收站','library-trash',libraryRef(lib),'secondary','trash') : '',
    button('属性','library-properties',libraryRef(lib,path)),
    lib.is_owner ? button('设置','library-settings',libraryRef(lib)) : '',
    lib.encrypted ? button('锁定','lock-library',libraryRef(lib),'secondary','key') : ''
  ].join('')) + `<section class="panel" id="file-panel">
    <div class="panel-head files-head"><div class="breadcrumb">${state.view === 'groups' ? '<button data-nav="groups">群组共享</button>' : '<button data-nav="mine">资料库</button>'}<span>/</span><button data-path="${esc(lib.root_path)}">${esc(libraryTitle(lib))}</button>${crumbs.join('')}</div>
      <div class="toolbar">
        ${canWrite ? `<details class="action-menu"><summary class="btn primary">${icon('upload')}上传</summary><div class="action-popup"><button data-work="select-upload">上传文件</button><button data-work="upload-directory">上传文件夹</button><button data-work="upload-overwrite">上传新版本</button></div></details>
        <details class="action-menu"><summary class="btn secondary">${icon('plus')}新建</summary><div class="action-popup">
        <button data-action="new-folder">文件夹</button>${[['markdown','Markdown'],['text','文本文件'],['word','Word'],['excel','Excel'],['ppt','PPT']].map(([kind,label]) => `<button data-work="document" data-kind="${kind}">${label}</button>`).join('')}</div></details>
        <input id="file-input" type="file" multiple hidden><input id="directory-input" type="file" webkitdirectory multiple hidden>` : '<span class="badge reader">只读</span>'}
        <button class="icon-btn" data-work="refresh-files" aria-label="刷新文件">${icon('refresh')}</button>
      </div>
    </div>
    <div class="file-controls">
      <label class="search">${icon('search')}<input id="search" placeholder="搜索当前文件夹" aria-label="搜索当前文件夹"></label>
      <div class="view-switch" aria-label="显示方式">${[['list','列表','list'],['grid','格点','grid'],['columns','分栏','columns']].map(([value,label,glyph]) => `<button type="button" data-work="view-mode" data-mode="${value}" title="${label}" aria-label="${label}视图" aria-pressed="${workspace.display === value}">${icon(glyph)}</button>`).join('')}</div>
      <select id="file-sort" aria-label="文件排序"><option value="name">按名称</option><option value="modified">按修改时间</option><option value="size">按大小</option></select>
      <button class="icon-btn" data-work="sort-direction" aria-label="切换排序方向">${workspace.descending ? '↓' : '↑'}</button>
    </div>
    <div id="selection-bar" class="selection-bar" hidden></div>
    <div id="file-table" aria-live="polite"><div class="empty muted">正在读取文件…</div></div>
    <div class="panel-head" id="file-pages" hidden><small id="file-page-number"></small><div class="toolbar"><button type="button" class="btn secondary" id="previous-file-page">上一页</button><button type="button" class="btn secondary" id="next-file-page">下一页</button></div></div>
  </section><div class="page-footer"><span id="file-count"></span><span>${canWrite ? '可以拖入文件上传' : ''}</span></div>`;
  if (canWrite) {
    $('#file-input').onchange = event => {
      const files = [...event.target.files];
      const overwrite = event.target.dataset.overwrite === '1';
      event.target.value = '';
      runAction(() => uploadFiles(files, overwrite));
    };
    $('#directory-input').onchange = event => {
      const files = [...event.target.files];
      event.target.value = '';
      runAction(() => uploadFiles(files, false, true));
    };
    const panel = $('#file-panel');
    panel.ondragover = event => { event.preventDefault(); panel.classList.add('drop-active'); };
    panel.ondragleave = event => { if (!panel.contains(event.relatedTarget)) panel.classList.remove('drop-active'); };
    panel.ondrop = event => {
      event.preventDefault();
      panel.classList.remove('drop-active');
      runAction(() => uploadFiles([...event.dataTransfer.files]));
    };
  }
  let searchTimer;
  $('#search').oninput = event => {
    state.query = event.target.value;
    workspace.filePage = 0;
    workspace.fileRequest++;
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      if (generation === workspace.generation) runAction(() => loadFilePage());
    }, 200);
  };
  $('#file-sort').value = workspace.order;
  $('#file-sort').onchange = event => { workspace.order = event.target.value; workspace.filePage = 0; runAction(() => loadFilePage()); };
  $('#previous-file-page').onclick = () => { workspace.filePage--; runAction(() => loadFilePage()); };
  $('#next-file-page').onclick = () => { workspace.filePage++; runAction(() => loadFilePage()); };
  await loadFilePage();
}

async function loadFilePage() {
  const lib = activeLibrary();
  if (!lib || !$('#file-table')) return;
  const generation = workspace.generation;
  const request = ++workspace.fileRequest;
  const params = new URLSearchParams({path: state.path, q: state.query, offset: String(workspace.filePage * FILE_PAGE_SIZE),
    limit: String(FILE_PAGE_SIZE), order: workspace.order, descending: String(workspace.descending)});
  $('#previous-file-page').disabled = true;
  $('#next-file-page').disabled = true;
  try {
    const result = await api(libraryURL(lib.id, 'files?' + params));
    if (generation !== workspace.generation || request !== workspace.fileRequest) return;
    workspace.fileTotal = result.total;
    const lastPage = Math.max(0, Math.ceil(result.total / FILE_PAGE_SIZE) - 1);
    if (workspace.filePage > lastPage) {
      workspace.filePage = lastPage;
      return loadFilePage();
    }
    state.files = result.items;
    for (const file of state.files) workspace.fileEntries.set(file.path, file);
    renderFiles();
  } catch (error) {
    if (generation === workspace.generation && request === workspace.fileRequest) {
      $('#file-table').innerHTML = `<div class="empty"><p>${esc(error.message)}</p></div>`;
    }
  }
}

function fileGlyph(file) {
  const kind = file.kind || (file.is_dir ? 'folder' : kindByExtension.get(extension(file.name)) || 'file');
  const [label, paths] = specialGlyphs[kind] || [fileKinds[kind]?.[0] || '文件', pageOutline + (fileKinds[kind]?.[2] || '')];
  return `<span class="file-icon type-icon type-${kind}" title="${label}" aria-hidden="true"><svg viewBox="0 0 24 24">${paths}</svg></span>`;
}

function fileRef(file) { return libraryRef(activeLibrary(), file.path); }

function fileName(file) {
  return `<button class="file-name" data-work="open-file" ${fileRef(file)}>${esc(file.name)}</button>`;
}

function fileActions(file) {
  const lib = activeLibrary();
  const ref = fileRef(file);
  const item = (label, action) => `<button type="button" data-work="${action}" ${ref}>${esc(label)}</button>`;
  const actions = [item('属性与评论','properties'), item(favoriteIs(lib.id, file.path) ? '取消收藏' : '收藏','favorite')];
  if (!file.is_dir) actions.push(item('下载','download-file'), item('历史版本','file-history'));
  if (lib.is_owner) actions.push(item('共享','share'));
  if (writable(lib, file.path)) {
    actions.push(item('标签','tags'));
    if (file.can_remove) actions.push(item('重命名','rename-file'), item('移动','move-files'), item('移入回收站','delete-files'));
  }
  actions.push(item('复制','copy-files'));
  return `<details class="action-menu row-menu"><summary class="icon-btn" aria-label="${esc(file.name)}的操作">···</summary><div class="action-popup">${actions.join('')}</div></details>`;
}

function checkbox(file) {
  return `<input type="checkbox" data-select-file="${esc(file.path)}" aria-label="选择 ${esc(file.name)}" ${workspace.selected.has(file.path) ? 'checked' : ''}>`;
}

function renderFiles() {
  const target = $('#file-table');
  if (!target) return;
  const pageCount = Math.max(1, Math.ceil(workspace.fileTotal / FILE_PAGE_SIZE));
  workspace.filePage = Math.min(workspace.filePage, pageCount - 1);
  const files = state.files;
  $('#file-count').textContent = `${workspace.fileTotal} 个项目`;
  $('#file-pages').hidden = pageCount === 1;
  $('#file-page-number').textContent = `第 ${workspace.filePage + 1} / ${pageCount} 页`;
  $('#previous-file-page').disabled = workspace.filePage === 0;
  $('#next-file-page').disabled = workspace.filePage + 1 === pageCount;
  if (!files.length) {
    target.innerHTML = `<div class="empty">${icon(state.query ? 'search' : 'folder')}<h3>${state.query ? '没有匹配的文件' : '文件夹为空'}</h3></div>`;
  } else if (workspace.display === 'grid') {
    target.innerHTML = `<div class="file-grid">${files.map(file => `<article class="file-tile"><div class="tile-tools">${checkbox(file)}${fileActions(file)}</div>${fileGlyph(file)}${fileName(file)}<small>${file.is_dir ? '文件夹' : bytes(file.size)}</small></article>`).join('')}</div>`;
  } else if (workspace.display === 'columns') {
    const lib = activeLibrary();
    const ancestors = [lib.root_path];
    if (state.path !== lib.root_path) {
      const parts = state.path.slice(lib.root_path.length).replace(/^\//, '').split('/');
      for (let index = 0; index < parts.length; index++) ancestors.push([lib.root_path, ...parts.slice(0,index+1)].filter(Boolean).join('/'));
    }
    target.innerHTML = `<div class="columns-view"><div class="columns-tree" aria-label="目录层级">${ancestors.map(path => `<button class="nav-folder ${path === state.path ? 'selected' : ''}" data-path="${esc(path)}">${icon('folder')}${esc(path === lib.root_path ? libraryTitle(lib) : pathName(path))}</button>`).join('')}</div>
      <div class="columns-entries">${files.map(file => `<div class="column-file">${checkbox(file)}${fileGlyph(file)}${fileName(file)}${fileActions(file)}</div>`).join('')}</div></div>`;
  } else {
    const checked = files.every(file => workspace.selected.has(file.path));
    target.innerHTML = `<div class="table-wrap file-list"><table>
      <thead><tr><th><input type="checkbox" id="select-all" aria-label="全选本页文件" ${checked ? 'checked' : ''}></th><th>名称</th><th class="hide-mobile">大小</th><th class="hide-mobile">更新时间</th><th>操作</th></tr></thead>
      <tbody>${files.map(file => `<tr><td>${checkbox(file)}</td><td><div class="file-cell">${fileGlyph(file)}<div>${fileName(file)}${file.tags.length ? `<div class="file-tags">${file.tags.map(tag => `<button type="button" data-work="search-tag" data-tag="${esc(tag)}">${esc(tag)}</button>`).join('')}</div>` : ''}</div></div></td>
        <td class="hide-mobile metadata-cell">${file.is_dir ? '—' : sizeText(file.size)}</td><td class="hide-mobile metadata-cell">${modifiedText(file.modified)}</td><td>${fileActions(file)}</td></tr>`).join('')}</tbody>
    </table></div>`;
  }
  for (const input of target.querySelectorAll('[data-select-file]')) {
    input.onchange = () => {
      if (input.checked) workspace.selected.add(input.dataset.selectFile);
      else workspace.selected.delete(input.dataset.selectFile);
      renderSelection();
      if ($('#select-all')) $('#select-all').checked = files.every(file => workspace.selected.has(file.path));
    };
  }
  if ($('#select-all')) $('#select-all').onchange = event => {
    for (const file of files) {
      if (event.target.checked) workspace.selected.add(file.path);
      else workspace.selected.delete(file.path);
    }
    renderFiles();
  };
  renderSelection();
}

function renderSelection() {
  const bar = $('#selection-bar');
  if (!bar) return;
  const selected = [...workspace.selected];
  bar.hidden = !selected.length;
  if (!selected.length) return;
  const data = libraryRef(activeLibrary(), state.path);
  const canRemove = selected.every(path => workspace.fileEntries.get(path)?.can_remove && writable(activeLibrary(), path));
  bar.innerHTML = `<span>已选 ${selected.length} 项</span>${button('下载','batch-download',data,'secondary','download')}${button('复制','copy-files',data + ' data-batch="1"')}
    ${canRemove ? button('移动','move-files',data + ' data-batch="1"') + button('移入回收站','delete-files',data + ' data-batch="1"','danger') : ''}
    ${activeLibrary().is_owner && !activeLibrary().encrypted ? button('批量生成链接','batch-links',data) : ''}
    ${button('取消选择','clear-selection','', 'soft')}`;
}

function nativeDownload(url, name = '') {
  const link = document.createElement('a');
  link.href = url;
  link.download = name;
  document.body.append(link);
  link.click();
  link.remove();
}

function contentURL(id, path, inline = false) {
  return '/api' + libraryURL(id, 'content?path=' + urlPath(path) + (inline ? '&inline=true' : ''));
}

async function openFile(id, path) {
  const file = activeLibrary()?.id === id && state.files.find(item => item.path === path);
  if (file?.is_dir) return openGroup(id, path);
  const lib = chooseLibrary(id, path);
  if (!lib) throw new Error('你没有此文件的访问权限');
  if (lib.locked) return openGroup(id, parentPath(path));
  const name = pathName(path), suffix = extension(name);
  const data = libraryRef(lib, path);
  if (textExtensions.has(suffix)) {
    const doc = await api(libraryURL(id,'text?path=' + urlPath(path)));
    if (doc.editable) {
      formModal(name, '文本编辑', `<label>文件内容<textarea name="content" rows="15" spellcheck="false">${esc(doc.content)}</textarea></label>`,
        '保存修改', async ({content}) => {
          await api(libraryURL(id,'text'), json('PUT', {path, content, etag: doc.etag}));
          if (activeLibrary()?.id === id) await openGroup(id,state.path);
          toast('文件已保存');
        });
    } else {
      modal(name, '只读预览', `<pre class="text-preview">${esc(doc.content)}</pre>`, button('下载','download-file',data));
    }
    return;
  }
  const src = esc(contentURL(id,path,true));
  const footer = button('属性与评论','properties',data) + button('下载','download-file',data,'primary','download');
  if (imageExtensions.has(suffix)) {
    modal(name,'',`<img class="file-preview" alt="${esc(name)}" src="${src}">`,footer);
  } else if (['mp4','webm'].includes(suffix)) {
    modal(name,'',`<video class="file-preview" controls preload="metadata" src="${src}"></video>`,footer);
  } else if (['mp3','wav','ogg'].includes(suffix)) {
    modal(name,'',`<audio controls preload="metadata" src="${src}"></audio>`,footer);
  } else if (suffix === 'pdf') {
    modal(name,'PDF 文档',`<a class="btn secondary" href="${src}" target="_blank" rel="noopener noreferrer">在新窗口预览</a>`,footer);
  } else if (documentExtensions.has(suffix) && workspace.officeEnabled && !lib.encrypted) {
    const url = `/office/editor?library_id=${encodeURIComponent(id)}&path=${urlPath(path)}`;
    modal(name,'Office 文档',`<a class="btn primary" href="${esc(url)}" target="_blank" rel="noopener noreferrer">打开在线编辑器</a>`,footer);
  } else {
    modal(name,'', '<p class="muted">可下载文件，或通过电脑盘使用本机软件打开。</p>',footer);
  }
}

function newDocument(kind) {
  const lib = activeLibrary();
  const folder = state.path;
  const labels = {markdown:'Markdown',text:'文本文件',word:'Word 文档',excel:'Excel 表格',ppt:'PPT 演示文稿'};
  formModal('新建' + labels[kind], '', '<label>文件名称<input name="name" required maxlength="170"></label>', '创建', async ({name}) => {
    if (name.includes('/') || name.includes('\\')) throw new Error('名称不能包含路径分隔符');
    await api(libraryURL(lib.id,'documents'),json('POST',{kind,path:[folder,name].filter(Boolean).join('/')}));
    await openGroup(lib.id,folder);
    toast('文档已创建');
  });
}

function renameFile(lid, path) {
  formModal('重命名', '', `<label>名称<input name="name" required maxlength="180" value="${esc(pathName(path))}"></label>`,'保存', async values => {
    await api(`/groups/${lid}/files`,json('PATCH',{path,name:values.name}));
    await openGroup(lid,state.path);
    toast('名称已更新');
  });
}

function editTags(lid, path) {
  const file = state.files.find(item => item.path === path);
  formModal('文件标签', pathName(path), `<label>标签，用逗号分隔<input name="tags" value="${esc(file?.tags.join('，') || '')}" maxlength="680"></label>`,'保存', async values => {
    const tags = values.tags.split(/[,，]/).map(tag => tag.trim()).filter(Boolean);
    await api(libraryURL(lid,'tags'),json('PUT',{path,tags}));
    await openGroup(lid,state.path);
    toast('标签已保存');
  });
}

function selectPaths(el) {
  return el.dataset.batch === '1' ? [...workspace.selected] : [el.dataset.rel];
}

function transferFiles(lid, paths, move) {
  const destinations = workspace.libraries.filter(lib => !lib.locked && lib.write_paths.length);
  if (!destinations.length) throw new Error('没有可写的目标资料库');
  formModal(move ? '移动文件' : '复制文件', `已选 ${paths.length} 项`,
    `<label>目标资料库<select name="target" required>${destinations.map((lib,index) => `<option value="${index}">${esc(libraryTitle(lib))}</option>`).join('')}</select></label>
    <label>目标文件夹（留空使用所选空间的根目录）<input name="destination" placeholder="例如：项目/交付"></label>`,
    move ? '移动' : '复制', async values => {
      const target = destinations[Number(values.target)];
      await api(libraryURL(lid,'transfer'),json('POST',{paths,destination_library:target.id,destination:[target.root_path,values.destination.trim()].filter(Boolean).join('/'),move}));
      await openGroup(lid,state.path);
      toast(move ? '文件已移动' : '文件已复制');
    });
}

async function deleteFiles(lid, paths) {
  if (!paths.length || !await confirmAction('删除文件或文件夹', `所选 ${paths.length} 项会移入文件回收站，可在回收站恢复。删除文件夹会同时移除其全部内容。`, '确认删除', paths)) return;
  const result = await api(libraryURL(lid,'batch-delete'),json('POST',{paths}));
  await openGroup(lid,state.path);
  const failed = result.results.filter(item => !item.ok);
  toast(failed.length ? `${failed.length} 项未删除，${failed[0].error}` : '已移入回收站',Boolean(failed.length));
}

async function batchDownload(lid) {
  const paths = [...workspace.selected];
  const {id} = await api(libraryURL(lid,'archive-jobs'), json('POST', {paths}));
  const endpoint = '/archive-jobs/' + id;
  modal('批量下载', `已选 ${paths.length} 项`, `<p id="archive-status" role="status">正在等待打包…</p><progress id="archive-progress" hidden></progress>`,
    '<button type="button" class="btn secondary" id="archive-cancel">取消打包</button><button type="button" class="btn primary" id="archive-download" hidden>下载 ZIP</button>');
  const dialog = $('#modal');
  const status = $('#archive-status');
  const progress = $('#archive-progress');
  const cancel = $('#archive-cancel');
  const download = $('#archive-download');
  let startedDownload = false;
  let timer;
  const stop = () => {
    clearTimeout(timer);
    if (!startedDownload) api(endpoint, {method: 'DELETE'}).catch(() => {});
  };
  dialog.addEventListener('close', stop, {once: true});
  cancel.onclick = () => dialog.close();
  download.onclick = () => {
    startedDownload = true;
    nativeDownload('/api' + endpoint + '/download', '共享文件.zip');
  };
  const poll = async () => {
    if (!dialog.open || !status.isConnected) return;
    try {
      const task = await api(endpoint);
      if (!dialog.open || !status.isConnected) return;
      if (task.status === 'ready') {
        status.textContent = '打包完成。ZIP 保留 15 分钟。';
        progress.hidden = true;
        cancel.textContent = '关闭';
        download.hidden = false;
        return;
      }
      if (task.status === 'failed') throw new Error(task.error || '打包失败，请重试');
      if (task.status === 'scanning') status.textContent = '正在检查文件和可用空间…';
      if (task.status === 'packing') {
        status.textContent = `正在打包，已处理 ${bytes(task.bytes_done)} / ${bytes(task.total)}`;
        progress.hidden = !task.total;
        if (task.total) { progress.max = task.total; progress.value = Math.min(task.bytes_done, task.total); }
      }
      timer = setTimeout(poll, 900);
    } catch (error) {
      status.textContent = error.message;
      progress.hidden = true;
      cancel.textContent = '关闭';
    }
  };
  await poll();
}

async function properties(lid, path) {
  const file = state.files.find(item => item.path === path);
  const comments = await api(libraryURL(lid,'comments?path=' + urlPath(path)));
  const details = file ? `<dl class="file-properties"><dt>类型</dt><dd>${file.is_dir ? '文件夹' : esc(extension(file.name) || '文件')}</dd><dt>大小</dt><dd>${file.is_dir ? '—' : bytes(file.size)}</dd><dt>修改时间</dt><dd>${date(file.modified)}</dd><dt>标签</dt><dd>${esc(file.tags.join('、') || '无')}</dd></dl>` : '';
  modal('属性与评论', path, details + `<h3 class="subheading">评论</h3><div class="comment-list">${comments.map(comment => `<div class="member-row"><div class="who"><strong>${esc(comment.actor)}</strong><small>${date(comment.created)}</small><p>${esc(comment.body)}</p></div>${comment.user_id === state.user.id || chooseLibrary(lid,path)?.is_owner ? button('删除评论','delete-comment',`data-cid="${comment.id}" data-lid="${lid}" data-rel="${esc(path)}"`,'danger') : ''}</div>`).join('') || '<p class="muted">暂无评论</p>'}</div>
    <form id="comment-form" class="form-stack"><label>添加评论<textarea name="body" required maxlength="4000" rows="2"></textarea></label><button class="btn primary">发送</button></form>`);
  $('#comment-form').onsubmit = event => {
    event.preventDefault();
    const body = new FormData(event.currentTarget).get('body').trim();
    if (body) runAction(async () => {
      await api(libraryURL(lid,'comments'), json('POST',{path,body}));
      await properties(lid,path);
    });
  };
}

async function fileHistory(lid,path) {
  const versions = await api(libraryURL(lid,'versions?path=' + urlPath(path)));
  const rows = versions.map(version => `<tr><td>${date(version.created)}</td><td>${esc(version.actor || '成员')}</td><td>${bytes(version.size)}</td><td><a class="btn secondary" href="/api/versions/${version.id}/download">下载</a>${writable(chooseLibrary(lid,path),path) ? button('恢复此版本','restore-version',`data-vid="${version.id}" data-lid="${lid}" data-rel="${esc(path)}"`,'soft') : ''}</td></tr>`);
  modal('历史版本',pathName(path),table(['时间','操作人','大小','操作'],rows,'暂无历史版本'));
}

async function libraryHistory(lid) {
  const [snapshots,events] = await Promise.all([api(libraryURL(lid,'snapshots')),api('/events?library_id=' + lid)]);
  const lib = chooseLibrary(lid,'');
  const rows = snapshots.map(snapshot => `<tr><td>${esc(snapshot.label)}</td><td>${date(snapshot.created)}</td><td>${lib?.is_owner ? button('恢复','restore-snapshot',`data-sid="${snapshot.id}" data-lid="${lid}"`,'soft') : ''}</td></tr>`);
  modal('资料库历史',lib?.name || '', `${lib?.is_owner ? button('创建快照','new-snapshot',`data-lid="${lid}"`,'primary','plus') : ''}<h3 class="subheading">资料库快照</h3>${table(['名称','创建时间','操作'],rows,'暂无快照')}
    <h3 class="subheading">近期活动</h3>${table(['操作','文件','时间'],events.slice(0,20).map(event => `<tr><td>${esc(eventName(event.action))}</td><td>${esc(event.path)}</td><td>${date(event.created)}</td></tr>`),'暂无活动')}`);
}

async function libraryTrash(lid) {
  const rows = await api('/trash?library_id=' + lid);
  const items = rows.map(row => `<tr><td>${esc(row.path)}</td><td>${date(row.deleted)}</td><td>${button('恢复','restore-library-file',`data-tid="${row.id}" data-lid="${lid}"`,'soft')}</td></tr>`);
  modal('资料库回收站',workspace.libraries.find(lib => lib.id === lid)?.name || '',table(['原路径','删除时间','操作'],items,'回收站为空'));
}

async function libraryProperties(lid,path) {
  const lib = chooseLibrary(lid,path);
  if (lib.locked) throw new Error('请先解锁资料库后查看属性');
  modal('资料库属性',path || lib.name,'<p role="status">正在统计文件数量和大小…</p>');
  const data = await api(libraryURL(lid,'properties?path=' + urlPath(path)));
  if (!$('#modal').open || $('#modal h2')?.textContent !== '资料库属性') return;
  modal('资料库属性',path || lib.name,`<dl class="file-properties">
    <dt>所有者</dt><dd>${esc(lib.owner_name)}</dd><dt>创建时间</dt><dd>${date(lib.created)}</dd>
    <dt>权限</dt><dd>${roleText(data.role)}</dd><dt>文件数</dt><dd>${data.files}${data.partial ? '+' : ''}</dd>
    <dt>文件大小</dt><dd>${bytes(data.size)}${data.partial ? '（部分统计）' : ''}</dd>
    <dt>更新时间</dt><dd>${data.partial ? '—' : modifiedText(data.modified)}</dd>
    <dt>加密状态</dt><dd>${lib.encrypted ? '内容加密' : '普通资料库'}</dd>
  </dl>`);
}

function eventName(action) {
  return {upload:'上传',download:'下载',mkdir:'新建文件夹',rename:'重命名',delete:'删除',restore:'恢复',move:'移动',copy:'复制',edit:'修改',create:'新建文件',membership:'更新成员',share:'共享',unshare:'撤销共享',publish:'发布',link_create:'创建外链',library_create:'新建资料库',library_update:'更新资料库',library_delete:'删除资料库',library_restore:'恢复资料库',version_restore:'恢复版本',snapshot:'创建快照',snapshot_restore:'恢复快照',tags:'更新标签',comment:'评论'}[action] || action;
}

async function favoritesPage() {
  workspace.favorites = await api('/favorites');
  const rows = workspace.favorites.map(item => `<tr><td><div class="file-cell">${fileGlyph(item.path ? item : {kind:'library'})}${button(item.name,item.is_dir ? 'open-library' : 'open-file',`data-lid="${item.library_id}" data-rel="${esc(item.path)}"`,'file-name')}</div></td>
    <td>${esc(item.library_name)}</td><td>${date(item.created)}</td><td>${button('取消收藏','favorite',`data-lid="${item.library_id}" data-rel="${esc(item.path)}"`)}</td></tr>`);
  page('收藏夹',table(['名称','资料库','收藏时间','操作'],rows,'还没有收藏'));
}

async function deletedPage() {
  const rows = await api('/deleted-libraries');
  page('已删除的资料库', table(['名称','删除时间','操作'], rows.map(item => `<tr><td>${esc(item.name)}</td><td>${date(item.deleted)}</td><td>${button('恢复','restore-library',`data-lid="${item.id}"`,'soft')}</td></tr>`), '没有已删除的资料库'));
}

async function devicesPage() {
  const rows = await api('/tokens');
  page('已连接的设备',table(['平台','设备名称','IP 地址','最后访问','操作'],rows.map(device => `<tr><td>${esc(device.platform)}</td><td>${esc(device.name)}</td><td>${esc(device.last_ip || '—')}</td><td>${device.last_seen ? date(device.last_seen) : '—'}</td><td>${button('撤销连接','revoke-device',`data-tid="${device.id}"`,'danger')}</td></tr>`),'还没有已授权设备'),'<button class="btn primary" data-action="connect">连接电脑</button>');
}

async function searchPage(cursor = workspace.searchCursor, back = workspace.searchBack) {
  const generation = ++workspace.generation;
  const params = new URLSearchParams({q: workspace.search, tag: workspace.tag, cursor, limit: '100'});
  if (!cursor && !back.length) page('搜索结果', '<div class="empty muted" role="status">正在搜索…</div>');
  const result = await api('/search?' + params);
  if (generation !== workspace.generation) return;
  workspace.searchCursor = cursor;
  workspace.searchBack = back;
  const rows = result.items.map(item => `<tr><td><div class="file-cell">${fileGlyph(item)}${button(item.name,item.is_dir ? 'open-library' : 'open-file',`data-lid="${item.library_id}" data-rel="${esc(item.path)}"`,'file-name')}</div></td><td>${esc(item.library_name)}</td><td>${esc(item.path)}</td><td class="metadata-cell">${item.is_dir ? '—' : sizeText(item.size)}</td><td class="metadata-cell">${modifiedText(item.modified)}</td></tr>`);
  const pagination = back.length || result.next_cursor ? `<div class="panel-head" aria-label="搜索结果分页"><small>第 ${back.length + 1} 页</small><div class="toolbar">
    ${back.length ? button('上一页','search-previous') : ''}${result.next_cursor ? button('继续搜索','search-next',`data-cursor="${esc(result.next_cursor)}"`) : ''}</div></div>` : '';
  const empty = result.next_cursor ? '本页没有匹配文件，可以继续搜索' : '没有找到匹配文件';
  page('搜索结果', table(['名称','资料库','所在路径','大小','更新时间'], rows, empty) + pagination, '', workspace.tag ? `标签 ${workspace.tag}` : workspace.search);
  if (!back.length && !workspace.tag && workspace.legacyEnabled && state.user.identity_source === 'seafile') {
    const section = document.createElement('section');
    $('#content').append(section);
    await originalSearch(section, generation, workspace.search);
  }
}

async function originalSearch(section, generation, q) {
  const rows = [];
  let cursor = null;
  let batches = 0;
  const progress = document.createElement('p');
  section.append(progress);
  const fetchMore = async () => {
    try {
      do {
        progress.textContent = '正在搜索原资料库…';
        const params = new URLSearchParams({q, ...(cursor || {})});
        const result = await api('/legacy/search?' + params);
        if (workspace.generation !== generation || !section.isConnected) return;
        for (const item of result.items) {
          const ref = legacyRef(item.repo,item.library,false,false) + ` data-pathname="${esc(item.path)}"`;
          rows.push(`<tr><td>${button(item.name, item.is_dir ? 'legacy-open' : 'legacy-preview', ref,'file-name')}</td><td>${esc(item.library)}</td><td>${esc(parentPath(item.path) || '/')}</td></tr>`);
        }
        cursor = result.next;
        batches++;
        section.innerHTML = '<h2>原资料库</h2>' + table(['名称','资料库','所在路径'], rows, cursor ? '正在查找匹配文件' : '没有匹配文件');
        section.append(progress);
      } while (cursor && batches % 20 !== 0 && rows.length < 100 * Math.ceil(batches / 20));
      progress.textContent = '';
      if (cursor) {
        const more = document.createElement('button'); more.className = 'btn secondary'; more.textContent = '继续搜索原资料库';
        more.onclick = () => { more.remove(); void fetchMore(); }; progress.append(more);
      }
    } catch (error) { progress.textContent = '原资料库搜索未完成。' + error.message; }
  };
  await fetchMore();
}

async function toggleFavorite(id,path) {
  const enabled = !favoriteIs(id,path);
  await api('/favorites',json('PUT',{library_id:id,path,enabled}));
  workspace.favorites = await api('/favorites');
  if (state.group) renderFiles();
  else if (state.view === 'favorites') await favoritesPage();
  else if (state.view === 'groups') await groupPage();
  else await libraryPage(state.view);
  toast(enabled ? '已收藏' : '已取消收藏');
}

function internalShareForm(subjects, kind, query) {
  const subjectOptions = subjects.map(item => `<option value="${esc(item.id)}">${esc(item.name)}${item.username ? ' · ' + esc(item.username) : ''}</option>`);
  return `${kind === 'user' ? `<form class="inline-form" id="share-search"><label>查找员工<input name="q" value="${esc(query)}" maxlength="60"></label><button class="btn secondary">搜索</button></form>` : ''}
    <form class="form-stack" id="internal-share">
      <label>${kind === 'user' ? '员工' : '群组'}<select name="subject_id" required><option value="">请选择</option>${subjectOptions.join('')}</select></label>
      <label>权限<select name="role"><option value="reader">只读</option><option value="editor">可编辑</option></select></label>
      <button class="btn primary">保存共享</button>
    </form><h3 class="subheading">已有授权</h3>`;
}

async function legacyShareDialog({repo, name, pathname: path, encrypted}, kind = 'user', query = '') {
  const ref = `${legacyRef(repo, name, encrypted === '1', true)} data-pathname="${esc(path)}" data-kind="${kind}"`;
  const base = legacyURL(repo);
  const params = new URLSearchParams({subject_type:kind, q:query});
  const [subjects, shares] = await Promise.all([api(`${base}/share-targets?${params}`), api(`${base}/shares?path=${urlPath(path)}`)]);
  const tabs = [['user','共享给用户'],['group','共享给群组']].map(([value,label]) =>
    button(label,'legacy-share',`${legacyRef(repo,name,encrypted === '1',true)} data-pathname="${esc(path)}" data-kind="${value}"`, `tab-chip ${value === kind ? 'active' : ''}`));
  const rows = shares.filter(share => share.subject_type === kind).map(share =>
    `<div class="member-row"><div class="who"><strong>${esc(share.name)}</strong><small>${legacyPermissionText(share.permission)}</small></div>
      ${button('撤销共享','legacy-unshare',`${ref} data-target="${esc(share.target)}" data-subject="${esc(share.name)}"`,'danger')}</div>`);
  modal('原资料共享', path === '/' ? name : `${name}${path}`,
    `<div class="chip-bar modal-tabs">${tabs.join('')}</div>${internalShareForm(subjects,kind,query)}${rows.join('') || '<p class="muted">暂无授权</p>'}`);
  const target = {repo, name, pathname:path, encrypted};
  if ($('#share-search')) $('#share-search').onsubmit = event => {
    event.preventDefault();
    const value = new FormData(event.currentTarget).get('q');
    runAction(() => legacyShareDialog(target, kind, value));
  };
  $('#internal-share').onsubmit = event => {
    event.preventDefault();
    const form = event.currentTarget;
    const values = Object.fromEntries(new FormData(form));
    const submit = form.querySelector('button');
    submit.disabled = true;
    runAction(async () => {
      try {
        await api(`${base}/shares`,json('PUT',{...values,path,subject_type:kind}));
        await legacyShareDialog(target, kind, query);
        toast('共享已保存');
      } finally { submit.disabled = false; }
    });
  };
}

function renameLegacy({repo,name,pathname:path,encrypted}) {
  const root = path === '/';
  const currentName = root ? name : pathName(path);
  const base = legacyURL(repo);
  formModal(root ? '重命名原资料库' : '重命名原文件夹', currentName,
    `<label>名称<input name="name" value="${esc(currentName)}" maxlength="180" required></label>`, '保存', async values => {
      await api(root ? base : `${base}/folders`, json('PATCH', root ? {name:values.name} : {path,name:values.name}));
      if (root) await routeNav(state.view);
      else await legacyFolder(repo,name,parentPath(path) || '/',encrypted === '1',true);
      toast('名称已更新');
    });
}

async function deleteLegacyLibrary({repo}) {
  const base = legacyURL(repo);
  const {name,version} = await api(`${base}/management`);
  if (!version) throw new Error('无法确认原资料库版本，请刷新后重试');
  if (!await confirmAction('删除原资料库','这会从原 Seafile 删除整个资料库，资料库中的文件和现有共享都会受到影响。','确认删除',[name])) return;
  await api(`${base}?${new URLSearchParams({name,version})}`,{method:'DELETE'});
  await routeNav(state.view);
  toast('原资料库已删除');
}

async function shareDialog(lid, path, kind = 'download', query = '') {
  const lib = chooseLibrary(lid,path);
  if (!lib?.is_owner) throw new Error('需要资料库所有者权限');
  const file = state.files.find(item => item.path === path);
  const folder = !path || file?.is_dir || (!file && path === state.path);
  const choices = [];
  if (!lib.encrypted) choices.push(['download','共享链接']);
  if (folder && !lib.encrypted) choices.push(['upload','上传链接']);
  if (folder) choices.push(['user','共享给用户'],['group','共享给群组']);
  if (!choices.length) throw new Error('加密资料库请通过文件夹授权共享');
  if (!choices.some(([value]) => value === kind)) kind = choices[0][0];
  const tabs = choices.map(([value,label]) => `<button type="button" class="tab-chip ${kind === value ? 'active' : ''}" data-work="share-tab" ${libraryRef(lib,path)} data-kind="${value}">${label}</button>`);
  let content = '';
  if (kind === 'user' || kind === 'group') {
    const subjects = kind === 'user' ? await api('/people?q=' + urlPath(query)) : state.groups;
    const shares = (await api('/shares?library_id=' + lid)).filter(item => item.path === path && item.subject_type === kind);
    content = `${internalShareForm(subjects,kind,query)}
      ${shares.map(share => `<div class="member-row"><div class="who"><strong>${esc(share.user_name || share.group_name)}</strong><small>${roleText(share.role)}</small></div>
      ${button('撤销共享','unshare',`data-sid="${share.id}" data-lid="${lid}" data-rel="${esc(path)}" data-kind="${kind}" data-subject="${esc(share.user_name || share.group_name || '已删除对象')}" data-resource="${esc(lib.name + ' / ' + (path || '根目录'))}"`,'danger')}</div>`).join('') || '<p class="muted">暂无授权</p>'}`;
  } else {
    const links = (await api('/links')).filter(link => link.library_id === lid && link.path === path && link.kind === kind && link.active && !link.expired);
    content = `<p class="notice">${kind === 'upload' ? '收到链接的人可以向此文件夹上传文件，不能查看其他资料。' : '收到链接的人可以查看并下载此文件或文件夹。'}</p>
      <form class="form-stack" id="external-share">
        <label>访问密码（可选）<input name="password" type="password" autocomplete="new-password" maxlength="200"></label>
        <label>过期时间（可选）<input name="expires" type="datetime-local"></label>
        <button class="btn primary">生成链接</button>
      </form><h3 class="subheading">有效链接</h3>
      ${links.map(link => `<div class="member-row"><div class="who"><strong>${link.has_password ? '有密码保护' : '凭链接访问'}</strong><small>${link.expires ? '过期时间 ' + date(link.expires) : '长期有效'} · 访问 ${link.visits} 次</small></div>
      ${linkActions(link,'share')}</div>`).join('') || '<p class="muted">暂无外链</p>'}`;
  }
  modal('共享', path ? pathName(path) : lib.name, `<div class="chip-bar modal-tabs">${tabs.join('')}</div>${content}`);
  if ($('#share-search')) $('#share-search').onsubmit = event => {
    event.preventDefault();
    const value = new FormData(event.currentTarget).get('q');
    runAction(() => shareDialog(lid,path,kind,value));
  };
  if ($('#internal-share')) $('#internal-share').onsubmit = event => {
    event.preventDefault();
    const values = Object.fromEntries(new FormData(event.currentTarget));
    runAction(async () => {
      await api(libraryURL(lid,'shares'),json('POST',{...values,path,subject_type:kind}));
      await shareDialog(lid,path,kind);
      toast('共享已保存');
    });
  };
  if ($('#external-share')) $('#external-share').onsubmit = event => {
    event.preventDefault();
    const values = Object.fromEntries(new FormData(event.currentTarget));
    runAction(async () => {
      const expires = values.expires ? new Date(values.expires).getTime()/1000 : null;
      const link = await api(libraryURL(lid,'links'),json('POST',{path,kind,password:values.password,expires}));
      showLink(link);
    });
  };
}

function linkActions(link, source) {
  return `<a class="btn secondary" href="/s/${encodeURIComponent(link.token)}" target="_blank" rel="noopener noreferrer">查看</a>
    ${button('复制链接','copy-link',`data-token="${esc(link.token)}"`,'soft')}
    ${button('撤销','revoke-link',`data-link-id="${link.id}" data-lid="${link.library_id}" data-rel="${esc(link.path)}" data-kind="${link.kind}" data-source="${source}"`,'danger')}`;
}

function showLink(link) {
  const url = location.origin + '/s/' + encodeURIComponent(link.token);
  modal('链接已创建','', `<label>访问地址<input type="url" readonly value="${esc(url)}" id="new-link-url"></label>`, button('复制链接','copy-link',`data-token="${esc(link.token)}"`,'primary'));
}

function batchLinks(lid) {
  const paths = [...workspace.selected];
  formModal('批量生成链接', `为所选 ${paths.length} 项分别生成下载链接。`,
    '<label>访问密码（可选）<input name="password" type="password" autocomplete="new-password" maxlength="200"></label><label>过期时间（可选）<input name="expires" type="datetime-local"></label>',
    '生成', async fields => {
      const expires = fields.expires ? new Date(fields.expires).getTime()/1000 : null;
      const links = await api(libraryURL(lid,'batch-links'),json('POST',{paths,expires,password:fields.password}));
      const rows = links.map(link => `<label>${esc(pathName(link.path))}<input readonly value="${esc(location.origin + '/s/' + encodeURIComponent(link.token))}"></label>`);
      setTimeout(() => modal('链接已生成', '', '<div class="form-stack">' + rows.join('') + '</div>'),0);
    });
}

async function copyLink(token) {
  const url = location.origin + '/s/' + encodeURIComponent(token);
  try {
    await navigator.clipboard.writeText(url);
    toast('链接已复制');
  } catch {
    modal('复制链接','','<label>按 Ctrl+C 复制链接<input id="copy-link-input" readonly value="' + esc(url) + '"></label>');
    $('#copy-link-input').select();
  }
}

async function linksPage(published) {
  const links = await api('/links');
  const active = links.filter(link => link.active && (published ? link.kind === 'published' : link.kind === workspace.sharesTab));
  const rows = active.map(link => `<tr><td>${esc(link.path || link.library_name)}</td><td>${esc(link.library_name)}</td><td>${link.visits}</td>
    <td>${link.expired ? '已过期' : link.expires ? date(link.expires) : '长期有效'}${link.has_password ? ' · 密码保护' : ''}</td><td><div class="toolbar">${linkActions(link,published ? 'published' : 'links')}</div></td></tr>`);
  if (published) {
    page('已发布的资料库',table(['名称','资料库','访问次数','有效期','操作'],rows,'暂无已发布资料库'),button('发布资料库','publish-library','','primary','plus'));
  } else {
    page('共享管理',shareTabs() + table(['名称','资料库','访问次数','有效期','操作'],rows,'暂无链接'),button('清除无效链接','clean-links'));
  }
}

function shareTabs() {
  return `<div class="chip-bar panel-tabs">${[['library','资料库'],['folder','文件夹'],['download','共享链接'],['upload','上传链接']].map(([key,label]) => `<button type="button" data-work="shares-filter" data-kind="${key}" class="tab-chip ${workspace.sharesTab === key ? 'active' : ''}">${label}</button>`).join('')}</div>`;
}

async function shareManagement() {
  if (['download','upload'].includes(workspace.sharesTab)) return linksPage(false);
  const shares = await api('/shares');
  const rows = shares.filter(share => workspace.sharesTab === 'folder' ? share.path : !share.path).map(share => `<tr><td>${esc(share.library_name)}</td><td>${esc(share.path || '根目录')}</td>
    <td>${esc(share.user_name || share.group_name || '已删除对象')}</td><td>${roleText(share.role)}</td><td>${button('撤销共享','unshare',`data-sid="${share.id}" data-subject="${esc(share.user_name || share.group_name || '已删除对象')}" data-resource="${esc(share.library_name + ' / ' + (share.path || '根目录'))}"`,'danger')}</td></tr>`);
  page('共享管理',shareTabs() + table(['资料库','目录','共享对象','权限','操作'],rows,'暂无共享记录'));
}

function publishLibrary() {
  const libraries = workspace.libraries.filter(lib => lib.is_owner && !lib.encrypted && !lib.root_path);
  if (!libraries.length) throw new Error('没有可发布的资料库');
  formModal('发布资料库','知道链接的人可以访问所选资料库。', `<label>资料库<select name="lid" required>${libraries.map(lib => `<option value="${lib.id}">${esc(lib.name)}</option>`).join('')}</select></label><label>确认发布<input type="checkbox" name="confirmed" required>允许通过此链接访问</label>`,
    '发布', async values => {
      const link = await api(libraryURL(values.lid,'links'),json('POST',{path:'',kind:'published'}));
      await linksPage(true);
      // formModal closes on success. Show the result after it has closed.
      setTimeout(() => showLink(link), 0);
    });
}

async function notificationsPanel() {
  const items = await api('/notifications');
  modal('通知','', items.map(item => `<div class="member-row"><div class="who"><strong>${esc(item.text)}</strong><small>${date(item.created)}${item.read_at ? '' : ' · 未读'}</small></div></div>`).join('') || '<p class="muted">暂无通知</p>',
    workspace.unread ? button('全部标为已读','read-notifications','','primary') : '');
}

async function runAction(action) {
  try { await action(); } catch (error) { toast(error.message, true); }
}

const workspaceActions = {
  'reset-member-password': el => formModal('重置成员密码', el.dataset.name,
    '<label>新密码<input name="new_password" type="password" autocomplete="new-password" minlength="6" maxlength="200" placeholder="至少 6 位" required></label><label>再次输入新密码<input name="confirm_password" type="password" autocomplete="new-password" minlength="6" maxlength="200" required></label><p class="muted">重置后，成员需要重新登录网页并重新连接电脑盘。</p>',
    '重置密码', async fields => {
      if (fields.new_password !== fields.confirm_password) throw new Error('两次输入的密码不一致');
      await api('/users/' + el.dataset.uid + '/password', json('POST', {new_password: fields.new_password}));
      toast('成员密码已重置');
    }),
  'copy-deployment': async () => {
    const id = workspace.license.deployment_id;
    try { await navigator.clipboard.writeText(id); toast('部署编号已复制'); }
    catch { modal('部署编号', '', `<input id="copy-deployment-input" readonly value="${esc(id)}">`); $('#copy-deployment-input').select(); }
  },
  'new-library': el => createLibrary(el.dataset.visibility),
  'library-settings': el => librarySettings(el.dataset.lid),
  'rename-library': el => renameLibrary(el.dataset.lid),
  'transfer-library': el => librarySettings(el.dataset.lid, true),
  'manage-library-shares': el => shareDialog(el.dataset.lid, el.dataset.rel, 'user'),
  'new-team': createTeam,
  'legacy-home': legacyPage,
  'legacy-view-back': () => routeNav(state.view),
  'original-members': el => originalMembers(Number(el.dataset.external), Number(el.dataset.pagenum || 1)),
  'original-remove': async el => {
    const {external,email,membername} = el.dataset;
    if (!await confirmAction('移出原群组','两个云盘将同时移除此群组成员。','确认移出',[membername])) return;
    await api('/legacy/groups/'+Number(external)+'/members?email='+urlPath(email), {method:'DELETE'});
    await originalMembers(Number(external));
  },
  'open-original': originalPage,
  'legacy-mkdir': el => changeOriginal(el, 'mkdir'),
  'legacy-entry-rename': el => changeOriginal(el, 'rename'),
  'legacy-entry-move': el => changeOriginal(el, 'move'),
  'legacy-entry-delete': el => changeOriginal(el, 'delete'),
  'legacy-upload': el => chooseLegacyUpload(el.dataset),
  'legacy-upload-version': el => chooseLegacyUpload(el.dataset, true),
  'legacy-history': el => legacyHistory(el.dataset, Number(el.dataset.page || 1)),
  'legacy-history-restore': el => restoreLegacyHistory(el.dataset),
  'legacy-trash': el => legacyTrash(el.dataset, el.dataset.cursor || ''),
  'legacy-trash-restore': el => restoreLegacyTrash(el.dataset),
  'legacy-group-settings': el => legacyGroupSettings(Number(el.dataset.external)),
  'legacy-group-admin': el => setLegacyGroupAdmin(el.dataset),
  'legacy-links': el => legacyExternalLinks(el.dataset),
  'legacy-link-create': el => createLegacyExternalLink(el.dataset),
  'legacy-link-expiry': el => changeLegacyExternalExpiry(el.dataset),
  'legacy-link-password': el => changeLegacyExternalPassword(el.dataset),
  'legacy-link-revoke': el => revokeLegacyExternalLink(el.dataset),
  'legacy-open': el => legacyFolder(el.dataset.repo, el.dataset.name, el.dataset.pathname, el.dataset.encrypted === '1', el.dataset.managed === '1'),
  'legacy-edit-text': el => legacyEditText(el.dataset.repo, el.dataset.name, el.dataset.pathname, el.dataset.managed === '1', el.dataset.encrypted === '1'),
  'legacy-share': el => legacyShareDialog(el.dataset,el.dataset.kind),
  'legacy-rename': el => renameLegacy(el.dataset),
  'legacy-delete-library': el => deleteLegacyLibrary(el.dataset),
  'legacy-unshare': async el => {
    const {repo,name,pathname:path,kind,target,subject} = el.dataset;
    if (!await confirmAction('撤销共享','该用户或群组将失去通过此授权获得的访问权限。','确认撤销', [path === '/' ? name : `${name}${path}`, `共享对象：${subject}`])) return;
    await api(`${legacyURL(repo)}/shares?${new URLSearchParams({path,subject_type:kind,target})}`,{method:'DELETE'});
    await legacyShareDialog(el.dataset,kind);
    toast('共享已撤销');
  },
  'legacy-preview': el => legacyPreview(el.dataset.repo, el.dataset.name, el.dataset.pathname),
  'legacy-download': el => legacyDownload(el.dataset.repo, el.dataset.name, el.dataset.pathname),
  'link-legacy': legacyPairing,
  'unlink-legacy': () => formModal('解除旧账号关联', '解除后可重新关联。', '', '解除关联', async () => {
    await api('/legacy/binding', {method: 'DELETE'});
    await legacyPage();
  }),
  'toggle-team-list': el => {
    workspace.groupsExpanded = !workspace.groupsExpanded;
    el.setAttribute('aria-expanded', String(workspace.groupsExpanded));
    el.classList.toggle('active', state.view === 'groups' && !workspace.groupsExpanded);
    $('#sidebar-groups').hidden = !workspace.groupsExpanded;
  },
  'select-team': el => {
    workspace.currentGroup = el.dataset.gid;
    const navigation = routeNav('groups');
    $('.group-nav-item[aria-current="page"]')?.focus({preventScroll: true});
    return navigation;
  },
  'team-settings': el => teamSettings(el.dataset.gid),
  'team-members': el => teamMembers(el.dataset.gid),
  'remove-member': async el => {
    if (!await confirmAction('移出群组成员','移出后，该员工将失去通过此群组获得的资料访问权限。','确认移出')) return;
    await api(`/groups/${el.dataset.gid}/members/${el.dataset.uid}`,{method:'DELETE'});
    await updateWorkspace();
    await groupPage();
    await teamMembers(el.dataset.gid);
  },
  'delete-library': async el => {
    const recovery = state.user.admin ? '可以在「已删除的资料库」中恢复。' : '如需恢复，请联系管理员。';
    if (!await confirmAction('删除资料库', `此资料库会从列表和电脑盘移除，原有外链会失效。${recovery}`, '确认删除', [chooseLibrary(el.dataset.lid, '')?.name || '当前资料库'])) return;
    await api(`/libraries/${el.dataset.lid}`,{method:'DELETE'});
    $('#modal').close();
    await updateWorkspace();
    await routeNav(state.user.admin ? 'deleted' : 'mine');
    toast('资料库已删除');
  },
  'restore-library': async el => {
    await api(libraryURL(el.dataset.lid,'restore'),{method:'POST'});
    await updateWorkspace();
    await deletedPage();
    toast('资料库已恢复');
  },
  'lock-library': async el => {
    await api(libraryURL(el.dataset.lid,'lock'),{method:'POST'});
    $('#modal').close();
    await updateWorkspace();
    await routeNav('mine');
    toast('资料库已锁定');
  },
  'open-library': el => openGroup(el.dataset.lid,el.dataset.rel),
  'open-file': el => openFile(el.dataset.lid,el.dataset.rel),
  'refresh-files': () => openGroup(activeLibrary().id,state.path),
  'favorite': el => toggleFavorite(el.dataset.lid,el.dataset.rel),
  'share': el => shareDialog(el.dataset.lid,el.dataset.rel),
  'share-tab': el => shareDialog(el.dataset.lid,el.dataset.rel,el.dataset.kind),
  'unshare': async el => {
    if (!await confirmAction('撤销共享','确认撤销以下共享授权？撤销后，该用户或群组将失去通过此授权获得的访问权限。','确认撤销',[el.dataset.resource, `共享对象：${el.dataset.subject}`])) return;
    await api('/shares/' + el.dataset.sid,{method:'DELETE'});
    if (el.dataset.lid) await shareDialog(el.dataset.lid,el.dataset.rel,el.dataset.kind);
    else await shareManagement();
    toast('共享已撤销');
  },
  'copy-link': el => copyLink(el.dataset.token),
  'revoke-link': async el => {
    if (!await confirmAction('撤销链接','撤销后，原链接将立即失效，收到链接的人将无法继续通过它访问或上传文件。','确认撤销')) return;
    await api('/links/' + el.dataset.linkId,{method:'DELETE'});
    if (el.dataset.source === 'share') await shareDialog(el.dataset.lid,el.dataset.rel,el.dataset.kind);
    else await linksPage(el.dataset.source === 'published');
    toast('链接已撤销');
  },
  'shares-filter': async el => { workspace.sharesTab = el.dataset.kind; await shareManagement(); },
  'clean-links': async () => {
    if (!await confirmAction('清除无效链接','将清除已过期或失效的链接，有效链接会保留。','确认清除')) return;
    const result = await api('/links/clean-expired',{method:'POST'});
    await linksPage(false);
    toast(`已清除 ${result.count} 个无效链接`);
  },
  'publish-library': publishLibrary,
  'revoke-device': async el => {
    if (!await confirmAction('撤销电脑连接','撤销后，这台电脑需要重新授权才能连接云盘。','确认撤销')) return;
    await api('/tokens/' + el.dataset.tid,{method:'DELETE'});
    await devicesPage();
    toast('设备连接已撤销');
  },
  'document': el => newDocument(el.dataset.kind),
  'download-file': el => nativeDownload(contentURL(el.dataset.lid,el.dataset.rel),pathName(el.dataset.rel)),
  'rename-file': el => renameFile(el.dataset.lid,el.dataset.rel),
  'tags': el => editTags(el.dataset.lid,el.dataset.rel),
  'search-tag': async el => {
    workspace.search = '';
    workspace.tag = el.dataset.tag;
    await routeNav('search');
  },
  'search-next': el => searchPage(el.dataset.cursor, [...workspace.searchBack, workspace.searchCursor]),
  'search-previous': () => searchPage(workspace.searchBack.at(-1), workspace.searchBack.slice(0, -1)),
  'properties': el => properties(el.dataset.lid,el.dataset.rel),
  'delete-comment': async el => {
    if (!await confirmAction('删除评论','评论删除后无法恢复，确定继续？','确认删除')) return;
    await api('/comments/' + el.dataset.cid,{method:'DELETE'});
    await properties(el.dataset.lid,el.dataset.rel);
  },
  'file-history': el => fileHistory(el.dataset.lid,el.dataset.rel),
  'library-history': el => libraryHistory(el.dataset.lid),
  'library-trash': el => libraryTrash(el.dataset.lid),
  'library-properties': el => libraryProperties(el.dataset.lid,el.dataset.rel),
  'restore-library-file': async el => {
    await api('/trash/' + el.dataset.tid + '/restore',{method:'POST'});
    if (activeLibrary()) await openGroup(el.dataset.lid,state.path);
    await libraryTrash(el.dataset.lid);
    toast('已恢复到原位置');
  },
  'new-snapshot': el => formModal('创建资料库快照','','<label>快照名称<input name="label" required maxlength="100"></label>','创建',async values => {
    await api(libraryURL(el.dataset.lid,'snapshots'),json('POST',values));
    toast('快照已创建');
  }),
  'restore-version': el => formModal('恢复文件版本','当前内容会保留在版本历史中。','','恢复',async () => {
    await api(`/versions/${el.dataset.vid}/restore`,{method:'POST'});
    if (state.group) await openGroup(el.dataset.lid,state.path);
    toast('版本已恢复');
  }),
  'restore-snapshot': el => formModal('恢复资料库快照','恢复前会保留当前快照。文件夹授权和文件外链会撤销，请核对后重新共享。','','恢复',async () => {
    await api(`/snapshots/${el.dataset.sid}/restore`,{method:'POST'});
    await openGroup(el.dataset.lid,'');
    toast('资料库已恢复');
  }),
  'copy-files': el => transferFiles(el.dataset.lid,selectPaths(el),false),
  'move-files': el => transferFiles(el.dataset.lid,selectPaths(el),true),
  'delete-files': el => deleteFiles(el.dataset.lid,selectPaths(el)),
  'batch-download': el => batchDownload(el.dataset.lid),
  'batch-links': el => batchLinks(el.dataset.lid),
  'clear-selection': () => { workspace.selected.clear(); renderFiles(); },
  'select-upload': () => { $('#file-input').dataset.overwrite = '0'; $('#file-input').click(); },
  'upload-overwrite': () => {
    if (!window.confirm('上传同名文件会生成新版本。继续？')) return;
    $('#file-input').dataset.overwrite = '1';
    $('#file-input').click();
  },
  'upload-directory': () => $('#directory-input').click(),
  'view-mode': el => {
    workspace.display = el.dataset.mode;
    localStorage.setItem('drive-view',workspace.display);
    document.querySelectorAll('[data-mode]').forEach(item => item.setAttribute('aria-pressed',String(item.dataset.mode === workspace.display)));
    renderFiles();
  },
  'sort-direction': el => {
    workspace.descending = !workspace.descending;
    workspace.filePage = 0;
    el.textContent = workspace.descending ? '↓' : '↑';
    return loadFilePage();
  },
  'notifications': notificationsPanel,
  'read-notifications': async () => {
    await api('/notifications/read',{method:'POST'});
    workspace.unread = 0;
    $('.notification-button sup')?.remove();
    $('.notification-button').setAttribute('aria-label','通知');
    await notificationsPanel();
  },
  'announcement': () => formModal('编辑公告','留空可撤下公告。',`<label>公告内容<textarea name="text" maxlength="1000" rows="4">${esc(workspace.banner)}</textarea></label>`,'保存',async values => {
    await api('/announcement',json('PUT',values));
    await updateWorkspace();
    await routeNav('mine');
  }),
  'dismiss-banner': () => { workspace.banner = ''; $('.announcement')?.remove(); },
  'profile': () => formModal('修改显示姓名','',`<label>姓名<input name="name" required maxlength="60" value="${esc(state.user.name)}"></label>`,'保存',async values => {
    const updated = await api('/profile',json('PATCH',values));
    state.user = {...state.user,...updated};
    await routeNav('mine');
  })
};

function positionLibraryMenu(trigger, panel) {
  const anchor = trigger.getBoundingClientRect();
  const menu = panel.getBoundingClientRect();
  if (anchor.bottom <= 0 || anchor.top >= innerHeight || anchor.right <= 0 || anchor.left >= innerWidth) {
    panel.hidePopover();
    return;
  }
  const left = Math.min(innerWidth - menu.width - 8, Math.max(8, anchor.right - menu.width));
  const top = anchor.bottom + menu.height + 12 <= innerHeight ? anchor.bottom + 4 : Math.max(8, anchor.top - menu.height - 4);
  panel.style.left = `${left}px`;
  panel.style.top = `${top}px`;
}

document.addEventListener('click', event => {
  const trigger = event.target.closest('[data-library-menu]');
  if (trigger) {
    const panel = trigger.nextElementSibling;
    if (panel.matches(':popover-open')) {
      panel.hidePopover();
      return;
    }
    panel.showPopover();
    positionLibraryMenu(trigger, panel);
    trigger.setAttribute('aria-expanded', 'true');
    panel.addEventListener('toggle', function close(event) {
      if (event.newState !== 'closed') return;
      trigger.setAttribute('aria-expanded', 'false');
      panel.removeEventListener('toggle', close);
    });
    panel.querySelector('button')?.focus({preventScroll:true});
    return;
  }
  const control = event.target.closest('[data-work]');
  for (const details of document.querySelectorAll('.action-menu[open]')) {
    if (!details.contains(event.target) || event.target.closest('button')) details.open = false;
  }
  if (!control) return;
  const action = workspaceActions[control.dataset.work];
  if (!action) return;
  event.preventDefault();
  if (control.disabled) return;
  const panel = control.closest('.library-popover');
  if (panel?.matches(':popover-open')) panel.hidePopover();
  control.disabled = true;
  runAction(async () => {
    try { await action(control); }
    finally { control.disabled = false; }
  });
});

window.addEventListener('resize', () => document.querySelector('.library-popover:popover-open')?.hidePopover());
document.addEventListener('scroll', event => {
  const panel = document.querySelector('.library-popover:popover-open');
  if (panel && !panel.contains(event.target)) positionLibraryMenu(panel.previousElementSibling, panel);
}, true);

boot();
setInterval(refreshLicenseNotice, 60000);


