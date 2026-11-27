'use strict';

const region = document.querySelector('#public-content');
const token = location.pathname.split('/')[2];
const endpoint = '/public/' + encodeURIComponent(token);
const escapeText = value => String(value ?? '').replace(/[&<>"']/g, char => ({
  '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'
}[char]));
let directory = '';

async function request(path, options = {}) {
  const response = await fetch(endpoint + path, options);
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(typeof data.detail === 'string' ? data.detail : '请求失败');
    error.status = response.status;
    throw error;
  }
  return data;
}

function jsonBody(method, body) {
  return {method, headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)};
}

function showError(message) {
  const error = document.querySelector('#public-error');
  if (error) error.textContent = message;
}

function unlockForm() {
  region.innerHTML = `<h1>输入访问密码</h1><form class="form-stack" id="unlock-share">
    <label>访问密码<input name="password" type="password" required maxlength="200" autocomplete="off"></label>
    <div class="form-error" id="public-error" role="alert"></div><button class="btn primary">打开链接</button>
  </form>`;
  document.querySelector('#unlock-share').onsubmit = async event => {
    event.preventDefault();
    const submit = event.currentTarget.querySelector('button');
    const password = new FormData(event.currentTarget).get('password');
    submit.disabled = true;
    try {
      await request('/unlock',jsonBody('POST',{password}));
      await openDirectory();
    } catch (error) {
      showError(error.message);
    } finally {
      submit.disabled = false;
    }
  };
}

async function uploadPublic(files) {
  const progress = document.querySelector('#share-progress');
  const status = document.querySelector('#share-progress-text');
  const input = document.querySelector('#public-files');
  input.disabled = true;
  let failures = 0;
  try {
    for (const file of files) {
      try {
        status.textContent = '正在上传 ' + file.name;
        progress.value = 0;
        const data = await request('/uploads',jsonBody('POST',{
          name:file.name,size:file.size,fingerprint:crypto.randomUUID?.() || String(Date.now()) + Math.random()
        }));
        let offset = data.offset;
        while (offset < file.size) {
          const end = Math.min(file.size,offset+4*1024*1024);
          const result = await request('/uploads/' + data.id,{
            method:'PATCH',headers:{'Upload-Offset':String(offset),'Content-Type':'application/octet-stream'},
            body:file.slice(offset,end)
          });
          offset = result.offset;
          progress.value = 100 * offset / file.size;
        }
        await request('/uploads/' + data.id + '/complete',{method:'POST'});
        progress.value = 100;
      } catch (error) {
        failures++;
        showError(file.name + '：' + error.message);
      }
    }
    status.textContent = '已上传 ' + (files.length-failures) + ' 个文件' + (failures ? '，失败 ' + failures + ' 个' : '');
  } finally {
    input.disabled = false;
    input.value = '';
  }
}

async function openDirectory(path = '') {
  try {
    const data = await request('?path=' + encodeURIComponent(path));
    directory = path;
    if (data.kind === 'upload') {
      if (data.read_only) {
        region.innerHTML = `<h1>上传至 ${escapeText(data.name)}</h1><p class="notice">云盘授权已到期，上传暂时停用，请联系收件人续期。</p>`;
        return;
      }
      region.innerHTML = `<h1>上传至 ${escapeText(data.name)}</h1><p class="notice">请选取需要提交给收件人的文件。</p>
        <label>选择文件<input id="public-files" type="file" multiple></label>
        <progress max="100" value="0" id="share-progress" aria-label="上传进度"></progress>
        <p id="share-progress-text" role="status"></p><div id="public-error" class="form-error" role="alert"></div>`;
      document.querySelector('#public-files').onchange = event => uploadPublic([...event.target.files]);
      return;
    }
    const download = rel => endpoint + '/download?path=' + encodeURIComponent(rel);
    const rows = data.items.map(file => {
      const rel = data.base_path ? file.path.slice(data.base_path.length+1) : file.path;
      const action = file.is_dir
        ? `<button class="btn soft" data-folder="${escapeText(rel)}">打开</button>`
        : `<a class="btn secondary" href="${escapeText(download(rel))}" download>下载</a>`;
      return `<tr><td>${escapeText(file.name)}</td><td>${file.is_dir ? '文件夹' : (file.size/1024).toFixed(1) + ' KB'}</td><td>${action}</td></tr>`;
    });
    region.innerHTML = `<h1>${escapeText(data.name)}</h1>
      ${path ? `<div class="breadcrumb"><button id="public-up">上一级</button><span>/</span><strong>${escapeText(path)}</strong></div>` : ''}
      ${data.is_dir ? (rows.length ? `<div class="table-wrap"><table><thead><tr><th>名称</th><th>大小</th><th>操作</th></tr></thead><tbody>${rows.join('')}</tbody></table></div>` : '<p class="notice">文件夹为空</p>') : `<a class="btn primary" href="${escapeText(download(path))}" download>下载文件</a>`}`;
    for (const button of region.querySelectorAll('[data-folder]')) {
      button.onclick = () => openDirectory(button.dataset.folder);
    }
    const up = document.querySelector('#public-up');
    if (up) up.onclick = () => openDirectory(directory.split('/').slice(0,-1).join('/'));
  } catch (error) {
    if (error.status === 401) unlockForm();
    else region.innerHTML = `<h1>链接无法打开</h1><p>${escapeText(error.message)}</p>`;
  }
}

openDirectory();

