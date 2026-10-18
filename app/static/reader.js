import {getDocument, GlobalWorkerOptions, PDFDataRangeTransport} from './vendor/pdfjs/pdf.min.mjs';

GlobalWorkerOptions.workerSrc = new URL('./vendor/pdfjs/pdf.worker.min.mjs', import.meta.url).href;
const reader = document.querySelector('[data-reader]');
const status = document.getElementById('pdf-status');
const transfer = document.getElementById('pdf-transfer');
const bufferProgress = document.getElementById('pdf-buffer');
const stage = document.getElementById('pdf-stage');
const input = document.getElementById('pdf-page');
const previous = document.getElementById('pdf-previous');
const next = document.getElementById('pdf-next');
const zoom = document.getElementById('pdf-zoom');
const jump = document.getElementById('pdf-jump');
let documentPDF, loadingTask, transport, rendering = false, fatal = false, currentPage = 1;

function fail(error) {
  if (fatal) return;
  fatal = true;
  status.textContent = error.message || 'PDF 无法打开，文件可能损坏或格式不受支持。';
  stage.setAttribute('aria-busy', 'false');
  for (const control of [previous, next, input, zoom, jump.querySelector('button')]) control.disabled = true;
  transport?.abort();
  if (loadingTask) loadingTask.destroy().catch(() => {});
}

function checkResponse(response) {
  if (response.status === 401) {
    location.assign('/login');
    throw new Error('登录已过期，请重新登录。');
  }
  if (response.status === 404) throw new Error('PDF 已移动或删除，请返回书库刷新。');
  if (!response.ok) throw new Error('PDF 读取失败，请检查网络和存储后重新加载。');
}

// Explicit ranges avoid even starting a whole-file GET. Each request is version-bound.
class FileRanges extends PDFDataRangeTransport {
  constructor(length, etag, url) {
    super(length, new Uint8Array(0), true);
    this.url = url;
    this.etag = etag;
    this.controllers = new Set();
    this.received = 0;
  }

  requestDataRange(begin, end) {
    const controller = new AbortController();
    this.controllers.add(controller);
    fetch(this.url, {
      headers: {Range: `bytes=${begin}-${end - 1}`, 'If-Range': this.etag},
      cache: 'no-store',
      signal: AbortSignal.any([controller.signal, AbortSignal.timeout(60000)]),
    }).then(async response => {
      checkResponse(response);
      if (response.status !== 206 || response.headers.get('Content-Range') !== `bytes ${begin}-${end - 1}/${this.length}`) {
        await response.body?.cancel();
        throw new Error('文件已变化，或代理未支持分段读取，请重新加载并检查代理设置。');
      }
      const bytes = new Uint8Array(await response.arrayBuffer());
      if (bytes.length !== end - begin) throw new Error('PDF 数据传输不完整，请重新加载。');
      this.received += bytes.length;
      bufferProgress.value = this.received;
      const percent = Math.min(100, Math.floor(this.received / this.length * 100));
      transfer.textContent = this.received >= this.length
        ? '整本已缓存 · 100%'
        : `后台缓存 ${percent}% · ${(this.received / 1048576).toFixed(1)} / ${(this.length / 1048576).toFixed(1)} MB`;
      this.onDataRange(begin, bytes);
    }).catch(error => {
      if (!controller.signal.aborted) fail(error.name === 'TimeoutError' ? new Error('读取超时，请检查网络后重新加载。') : error);
    }).finally(() => this.controllers.delete(controller));
  }

  abort() {
    for (const controller of this.controllers) controller.abort();
    this.controllers.clear();
  }
}

async function renderPage(number, scroll = true) {
  if (!documentPDF || rendering || fatal) return;
  if (!Number.isInteger(number) || number < 1 || number > documentPDF.numPages) { input.value = currentPage; return; }
  rendering = true;
  previous.disabled = next.disabled = input.disabled = zoom.disabled = jump.querySelector('button').disabled = true;
  stage.setAttribute('aria-busy', 'true');
  status.textContent = `正在读取第 ${number} 页…`;
  let page;
  try {
    page = await documentPDF.getPage(number);
    const natural = page.getViewport({scale: 1});
    const width = Math.min(1100, stage.clientWidth - 32) * Number(zoom.value);
    const scale = Math.max(0.1, width / natural.width);
    const view = page.getViewport({scale});
    // Bound both decoded canvas area and browser canvas dimensions for very tall pages.
    const ratio = Math.min(window.devicePixelRatio || 1, 2, Math.sqrt(16000000 / (view.width * view.height)), 16000 / view.width, 16000 / view.height);
    const canvas = document.createElement('canvas');
    canvas.id = 'pdf-canvas';
    canvas.width = Math.max(1, Math.floor(view.width * ratio));
    canvas.height = Math.max(1, Math.floor(view.height * ratio));
    canvas.style.width = `${view.width}px`;
    canvas.style.height = `${view.height}px`;
    canvas.setAttribute('aria-label', `PDF 第 ${number} 页，共 ${documentPDF.numPages} 页`);
    await page.render({canvasContext: canvas.getContext('2d'), viewport: view, transform: [ratio, 0, 0, ratio, 0, 0]}).promise;
    if (fatal) return;
    const old = document.getElementById('pdf-canvas');
    old.replaceWith(canvas);
    old.width = old.height = 0;
    currentPage = number;
    input.value = number;
    history.replaceState(null, '', `${location.pathname}${location.search}#page=${number}`);
    status.textContent = `第 ${number} / ${documentPDF.numPages} 页`;
    if (scroll) window.scrollTo({top: 0, behavior: 'instant'});
  } catch (error) {
    fail(new Error(error.name === 'PasswordException' ? '此 PDF 已加密，请使用未加密的副本。' : '当前页面无法显示，请重新加载或检查 PDF 文件。'));
  } finally {
    page?.cleanup();
    rendering = false;
    stage.setAttribute('aria-busy', 'false');
    if (!fatal) {
      previous.disabled = currentPage <= 1;
      next.disabled = currentPage >= documentPDF.numPages;
      input.disabled = zoom.disabled = jump.querySelector('button').disabled = false;
    }
  }
}

async function open() {
  try {
    const url = reader.dataset.fileUrl;
    const head = await fetch(url, {method: 'HEAD', cache: 'no-store', signal: AbortSignal.timeout(15000)});
    checkResponse(head);
    const length = Number(head.headers.get('Content-Length'));
    const etag = head.headers.get('ETag');
    if (!Number.isSafeInteger(length) || length <= 0 || !etag || head.headers.get('Accept-Ranges') !== 'bytes') {
      throw new Error('PDF 为空或代理未保留分段读取信息，请检查文件和代理设置。');
    }
    transport = new FileRanges(length, etag, url);
    bufferProgress.max = length;
    transfer.textContent = `后台缓存 0% · 共 ${(length / 1048576).toFixed(1)} MB`;
    const assets = new URL('./vendor/pdfjs/', import.meta.url).href;
    loadingTask = getDocument({
      // Range requests make the first page available early; auto-fetch keeps filling
      // PDF.js's byte cache in the background even when the reader stays on one page.
      range: transport, rangeChunkSize: 64 * 1024, disableStream: true, disableAutoFetch: false,
      cMapUrl: `${assets}cmaps/`, cMapPacked: true, standardFontDataUrl: `${assets}standard_fonts/`,
      wasmUrl: `${assets}wasm/`, isEvalSupported: false,
    });
    // This reader displays page content only; PDF actions, JavaScript and forms are not executed.
    loadingTask.onPassword = () => fail(new Error('此 PDF 已加密，请使用未加密的副本。'));
    documentPDF = await loadingTask.promise;
    input.max = documentPDF.numPages;
    document.getElementById('pdf-total').textContent = `/ ${documentPDF.numPages}`;
    const requested = Number(new URLSearchParams(location.hash.slice(1)).get('page')) || 1;
    await renderPage(Math.max(1, Math.min(documentPDF.numPages, Math.trunc(requested))), false);
  } catch (error) {
    fail(new Error(error.name === 'InvalidPDFException' ? 'PDF 格式无效或文件已损坏。' : error.message));
  }
}

previous.addEventListener('click', () => renderPage(currentPage - 1));
next.addEventListener('click', () => renderPage(currentPage + 1));
jump.addEventListener('submit', event => { event.preventDefault(); renderPage(Number(input.value)); });
zoom.addEventListener('change', () => renderPage(currentPage, false));
document.addEventListener('keydown', event => {
  if (event.target.closest('input, select, button, a') || event.altKey || event.ctrlKey || event.metaKey) return;
  if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
    event.preventDefault();
    renderPage(currentPage + (event.key === 'ArrowRight' ? 1 : -1));
  }
});
let resizeTimer;
window.addEventListener('resize', () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(() => renderPage(currentPage, false), 200); });
window.addEventListener('pagehide', () => { transport?.abort(); loadingTask?.destroy().catch(() => {}); });
window.addEventListener('pageshow', event => { if (event.persisted) location.reload(); });
open();
