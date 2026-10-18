const dashboard = document.querySelector('[data-dashboard]');
if (dashboard) {
  const csrf = document.querySelector('meta[name="csrf-token"]').content;
  const statuses = {queued: '排队中', downloading: '下载中', completed: '已完成', failed: '失败'};
  const cursors = [null];
  let page = 0, nextBefore = null, loading = false, toastTimer, lastTasks = '';

  function notify(message, error = false) {
    const toast = document.getElementById('toast');
    toast.textContent = message;
    toast.className = error ? 'error' : '';
    toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { toast.hidden = true; }, 4500);
  }

  async function api(path, options = {}) {
    const response = await fetch(path, {
      ...options,
      headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf},
      signal: AbortSignal.timeout(10000),
    });
    if (response.status === 401) { location.href = '/login'; throw new Error('请重新登录'); }
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '操作失败，请稍后重试');
    return data;
  }

  function element(tag, className, text) {
    const node = document.createElement(tag);
    node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function taskRow(task) {
    const row = element('article', 'task-row');
    const main = element('div', 'task-main');
    main.append(element('span', `task-icon ${task.status}`, task.status === 'completed' ? '✓' : '↓'));
    const info = element('div', 'task-info');
    info.append(element('strong', 'task-title', `JM${task.jm_id}`));
    info.append(element('span', 'task-subtitle', task.title || '等待获取信息'));
    if (task.mode !== dashboard.dataset.mode) info.append(element('span', 'task-mode-note', '其他模式 · 暂不执行'));
    main.append(info);
    const state = element('div', 'task-state');
    const stateTop = element('div', 'state-top');
    const badge = element('span', `badge ${task.status}`, statuses[task.status]);
    stateTop.append(badge);
    const percent = task.total ? Math.min(100, Math.floor(task.done / task.total * 100)) : 0;
    if (task.status === 'downloading') {
      stateTop.append(element('span', 'percentage', task.total ? `${percent}%` : '准备中'));
      const progress = element('progress', 'progress');
      progress.max = 100;
      progress.value = percent;
      progress.setAttribute('aria-label', `JM${task.jm_id} 下载进度`);
      state.append(stateTop, progress);
    } else { state.append(stateTop); }
    let description = task.message;
    if (task.status === 'downloading' && task.total && !task.message.startsWith('正在生成 PDF')) description = `${task.done} / ${task.total} 张`;
    if (task.status === 'completed' && task.total) description = `${task.total} ${task.output.endsWith('.pdf') ? '页 · PDF 已生成' : `张 · ${task.mode === 'mock' ? '模拟完成' : '已保存'}`}`;
    state.append(element('span', task.status === 'failed' ? 'task-detail failure-message' : 'task-detail', description));
    const time = element('time', 'task-time', new Date(task.created_at).toLocaleString('zh-CN', {
      month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
    }));
    time.dateTime = task.created_at;
    time.title = new Date(task.created_at).toLocaleString('zh-CN');
    const actions = element('div', 'task-actions');
    if (task.status === 'failed' && task.mode === dashboard.dataset.mode) {
      const retry = element('button', 'retry-button', '↻ 重试');
      retry.type = 'button';
      retry.setAttribute('aria-label', `重试 JM${task.jm_id}`);
      retry.addEventListener('click', async () => {
        retry.disabled = true;
        try { await api(`/api/tasks/${task.id}/retry`, {method: 'POST', body: '{}'}); notify('已重新加入队列'); await refresh(); }
        catch (error) { notify(error.message, true); }
        finally { retry.disabled = false; }
      });
      actions.append(retry);
    } else { actions.append(element('span', 'no-action', '—')); }
    row.append(main, state, time, actions);
    return row;
  }

  async function refresh() {
    if (loading) return;
    loading = true;
    const button = document.getElementById('refresh');
    button.disabled = true;
    try {
      const query = cursors[page] ? `?before=${cursors[page]}` : '';
      const data = await api(`/api/tasks${query}`);
      for (const [status, count] of Object.entries(data.summary)) document.getElementById(`count-${status}`).textContent = count;
      const total = Object.values(data.summary).reduce((a, b) => a + b, 0);
      document.getElementById('task-count').textContent = `共 ${total} 个任务`;
      const list = document.getElementById('tasks');
      const snapshot = JSON.stringify(data.tasks);
      if (data.tasks.length && snapshot !== lastTasks) list.replaceChildren(...data.tasks.map(taskRow));
      else if (!data.tasks.length && snapshot !== lastTasks) {
        const empty = element('div', 'empty-state');
        empty.append(element('span', 'empty-icon', '↓'), element('h3', '', '队列空空，刚刚好'), element('p', '', '在上方输入一个 JM 号，开始你的第一个任务。'));
        list.replaceChildren(empty);
      }
      lastTasks = snapshot;
      nextBefore = data.next_before;
      document.getElementById('pagination').hidden = !page && !nextBefore;
      document.getElementById('previous-page').disabled = page === 0;
      document.getElementById('next-page').disabled = !nextBefore;
      document.getElementById('page-number').textContent = `第 ${page + 1} 页`;
      document.getElementById('connection-status').textContent = '每 2 秒自动刷新';
    } catch (error) {
      document.getElementById('connection-status').textContent = '暂时无法连接，将自动重试';
      if (!document.querySelector('.task-row')) document.getElementById('task-count').textContent = '连接失败，请稍后刷新';
      if (error.name !== 'AbortError') console.warn('任务刷新失败');
    } finally { loading = false; button.disabled = false; }
  }

  document.getElementById('download-form').addEventListener('submit', async event => {
    event.preventDefault();
    const input = document.getElementById('jm-id');
    const value = input.value.trim();
    if (!/^(?:JM)?[0-9]{1,12}$/i.test(value) || !Number(value.replace(/^JM/i, ''))) {
      notify('请输入有效的 JM 号，例如 114514', true); input.focus(); return;
    }
    const button = document.getElementById('submit-button');
    button.disabled = true;
    try {
      const data = await api('/api/tasks', {method: 'POST', body: JSON.stringify({jm_id: value})});
      notify(data.created ? '已加入队列，稍后自动开始' : '这个编号已在队列中');
      input.value = ''; page = 0; cursors.splice(1); await refresh();
    } catch (error) { notify(error.message, true); }
    finally { button.disabled = false; input.focus(); }
  });
  document.getElementById('refresh').addEventListener('click', refresh);
  document.getElementById('previous-page').addEventListener('click', () => { if (!loading && page) { page--; refresh(); } });
  document.getElementById('next-page').addEventListener('click', () => { if (!loading && nextBefore) { cursors[++page] = nextBefore; refresh(); } });
  async function poll() { await refresh(); setTimeout(poll, 2000); }
  poll();
}
