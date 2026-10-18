const library = document.querySelector('[data-library]');
if (library) {
  let loading = false;
  const params = new URLSearchParams(location.search);
  const path = params.get('path') || '';
  const requestedPage = Number(params.get('page'));
  let page = Number.isSafeInteger(requestedPage) && requestedPage > 0 ? requestedPage : 1;
  const status = document.getElementById('library-status');

  function link(text, href, className = '') {
    const node = document.createElement('a');
    node.textContent = text;
    node.href = href;
    node.className = className;
    return node;
  }

  const crumbs = document.getElementById('breadcrumbs');
  crumbs.append(link('下载目录', '/library'));
  const parts = path.split('/').filter(Boolean);
  parts.forEach((part, index) => {
    crumbs.append(document.createTextNode(' / '), link(part, `/library?${new URLSearchParams({path: parts.slice(0, index + 1).join('/')})}`));
  });

  async function refresh() {
    if (loading) return;
    loading = true;
    document.getElementById('library-refresh').disabled = true;
    status.textContent = '正在读取目录…';
    try {
      const response = await fetch(`/api/library?${new URLSearchParams({path, page})}`, {signal: AbortSignal.timeout(15000)});
      if (response.status === 401) { location.assign('/login'); return; }
      if (!response.ok) throw new Error(response.status === 404 ? '目录不存在，请返回上级目录或检查挂载。' : '目录暂时不可读，请稍后刷新。');
      const data = await response.json();
      const nodes = data.entries.map(entry => {
        const folder = entry.kind === 'directory';
        const row = link('', `/${folder ? 'library' : 'reader'}?${new URLSearchParams({path: entry.path})}`, 'library-entry');
        const icon = document.createElement('span');
        icon.className = 'library-icon';
        icon.textContent = folder ? '目录' : 'PDF';
        const name = document.createElement('span');
        name.className = 'library-name';
        name.textContent = entry.name;
        const detail = document.createElement('span');
        detail.className = 'muted';
        detail.textContent = folder ? '打开 →' : `${(entry.size / 1048576).toFixed(1)} MB · 阅读 →`;
        row.append(icon, name, detail);
        return row;
      });
      document.getElementById('library-entries').replaceChildren(...nodes);
      status.textContent = data.total ? `共 ${data.total} 项 · 仅显示文件夹和 PDF` : '当前目录没有子文件夹或 PDF。';
      document.getElementById('library-pagination').hidden = page === 1 && !data.has_more;
      document.getElementById('library-previous').disabled = page === 1;
      document.getElementById('library-next').disabled = !data.has_more;
      document.getElementById('library-page').textContent = `第 ${page} 页`;
      history.replaceState(null, '', `/library?${new URLSearchParams({path, page})}`);
    } catch (error) {
      document.getElementById('library-entries').replaceChildren();
      document.getElementById('library-pagination').hidden = true;
      status.textContent = error.name === 'TimeoutError' ? '读取目录超时，请检查存储后刷新。' : error.message;
    } finally {
      loading = false;
      document.getElementById('library-refresh').disabled = false;
    }
  }
  document.getElementById('library-refresh').addEventListener('click', refresh);
  document.getElementById('library-previous').addEventListener('click', () => { if (!loading && page > 1) { page--; refresh(); } });
  document.getElementById('library-next').addEventListener('click', () => { if (!loading) { page++; refresh(); } });
  refresh();
}
