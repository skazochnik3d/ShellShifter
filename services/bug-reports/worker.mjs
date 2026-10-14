// Public report intake; no access to Maya, source code, releases or owner account.
// Secrets live in Worker bindings, never in wrangler.jsonc or the plugin.
const REPO = 'skazochnik3d/ShellShifter';
const MAX_IMAGE = 2 * 1024 * 1024;
const MAX_BODY = 3 * 1024 * 1024;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const encoder = new TextEncoder();
const headers = {'Content-Type': 'application/json', 'Cache-Control': 'no-store',
  'X-Content-Type-Options': 'nosniff', 'Content-Security-Policy': "default-src 'none'",
  'Referrer-Policy': 'no-referrer'};
const reply = (body, status = 200) => new Response(JSON.stringify(body), {status, headers});
class Rejected extends Error {
  constructor(code, status = 400) { super(code); this.code = code; this.status = status; }
}
const fail = (code = 'invalid_report', status = 400) => { throw new Rejected(code, status); };

export async function hmac(secret, message) {
  const key = await crypto.subtle.importKey('raw', encoder.encode(secret),
    {name: 'HMAC', hash: 'SHA-256'}, false, ['sign']);
  return Array.from(new Uint8Array(await crypto.subtle.sign('HMAC', key, encoder.encode(message))))
    .map(x => x.toString(16).padStart(2, '0')).join('');
}

async function bounded(stream, limit) {
  if (!stream) fail();
  const reader = stream.getReader(), chunks = [];
  let size = 0;
  try {
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > limit) fail('invalid_report', 413);
      chunks.push(value);
    }
  } finally { await reader.cancel().catch(() => {}); }
  const result = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) { result.set(chunk, offset); offset += chunk.length; }
  return result;
}

function text(value, min, max) {
  if (typeof value !== 'string' || value.trim().length < min || value.length > max ||
      /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/.test(value)) fail();
  return value.trim();
}

export function validateReport(value) {
  if (!value || value.schema !== 1 || value.consent_public !== true || !UUID.test(value.request_id)) fail();
  const title = text(value.title, 1, 120), description = text(value.description, 10, 12000);
  if (!value.environment || typeof value.environment !== 'object' || Array.isArray(value.environment)) fail();
  const environment = {};
  for (const key of ['shellshifter', 'maya', 'maya_api', 'os', 'python', 'qt']) {
    environment[key] = text(value.environment[key], 1, 100).replace(/\r?\n/g, ' ');
  }
  if (!/^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,5}(?:-(?:alpha|beta|rc)[0-9.]*)?$/.test(environment.shellshifter) ||
      !/^(2025|2027)(?:\.[0-9]+)?$/.test(environment.maya)) fail();
  if (value.screenshot !== null && value.screenshot !== undefined &&
      (typeof value.screenshot !== 'string' || value.screenshot.length > 4 * Math.ceil(MAX_IMAGE / 3))) fail();
  return {request_id: value.request_id, title, description, environment, screenshot: value.screenshot};
}

const crcTable = Array.from({length: 256}, (_, index) => {
  let c = index;
  for (let i = 0; i < 8; i++) c = (c & 1) ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
  return c >>> 0;
});
function crc(bytes) {
  let c = 0xffffffff;
  for (const value of bytes) c = crcTable[(c ^ value) & 255] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

export async function sanitizePng(encoded) {
  if (!encoded) return null;
  if (encoded.length % 4 || !/^[A-Za-z0-9+/]+={0,2}$/.test(encoded)) fail();
  let raw;
  try { raw = Uint8Array.from(atob(encoded), c => c.charCodeAt(0)); } catch { fail(); }
  if (raw.length > MAX_IMAGE || raw.length < 45) fail();
  if ([137,80,78,71,13,10,26,10].some((x,i) => raw[i] !== x)) fail();
  const view = new DataView(raw.buffer), kept = [raw.slice(0, 8)], idat = [];
  let offset = 8, width = 0, height = 0, channels = 0, ended = false, dataEnded = false;
  while (offset + 12 <= raw.length) {
    const size = view.getUint32(offset), end = offset + 12 + size;
    if (end > raw.length || size > MAX_IMAGE) fail();
    const kind = String.fromCharCode(...raw.slice(offset + 4, offset + 8));
    if (!/^[A-Za-z]{4}$/.test(kind) || crc(raw.subarray(offset + 4, end - 4)) !== view.getUint32(end - 4)) fail();
    if (offset === 8 && kind !== 'IHDR') fail();
    if (kind === 'IHDR') {
      if (offset !== 8 || size !== 13) fail();
      width = view.getUint32(offset + 8); height = view.getUint32(offset + 12);
      channels = raw[offset + 17] === 6 ? 4 : raw[offset + 17] === 2 ? 3 : 0;
      if (!width || !height || width > 1600 || height > 1600 || !channels || raw[offset + 16] !== 8 ||
          raw[offset + 18] || raw[offset + 19] || raw[offset + 20]) fail();
      kept.push(raw.slice(offset, end));
    } else if (kind === 'IDAT') {
      if (dataEnded || ended) fail();
      idat.push(raw.slice(offset + 8, end - 4));
      kept.push(raw.slice(offset, end));
    } else if (kind === 'IEND') {
      if (size || !idat.length || end !== raw.length) fail();
      ended = true;
      kept.push(raw.slice(offset, end));
    } else {
      // Ancillary metadata is discarded. Palette, animation and unknown critical chunks are rejected.
      if (kind[0] === kind[0].toUpperCase() || ['acTL','fcTL','fdAT'].includes(kind)) fail();
      if (idat.length) dataEnded = true;
    }
    offset = end;
  }
  if (!ended || offset !== raw.length) fail();
  // Check actual decompressed scanlines; bounded read rejects compressed bombs.
  const stride = 1 + width * channels, expected = stride * height;
  let pixels;
  try {
    pixels = await bounded(new Blob(idat).stream().pipeThrough(new DecompressionStream('deflate')), expected);
  } catch { fail(); }
  if (pixels.length !== expected) fail();
  for (let row = 0; row < height; row++) if (pixels[row * stride] > 4) fail();
  return new Uint8Array(await new Blob(kept).arrayBuffer());
}

function fenced(value) {
  // Render user text as literal text: no HTML, Markdown, links or @mention spam.
  const runs = value.match(/`+/g) || [];
  const fence = '`'.repeat(Math.max(3, ...runs.map(x => x.length + 1)));
  return fence + 'text\n' + value.replace(/@/g, '＠') + '\n' + fence;
}
export function issueBody(report, imageUrl) {
  return 'Submitted from ShellShifter. The reporter approved public publication.\n\n' +
    '### Description\n' + fenced(report.description) + '\n\n### Environment\n' +
    fenced(Object.entries(report.environment).map(([k,v]) => k + ': ' + v).join('\n')) +
    (imageUrl ? '\n\n### Screenshot\n![Screenshot](' + imageUrl + ')' : '') +
    '\n\n<!-- shellshifter-report:' + report.request_id + ' -->';
}

export class ReportQuota {
  constructor(ctx, env) { this.ctx = ctx; this.env = env; }
  async fetch(request) {
    const path = new URL(request.url).pathname;
    if (path === '/v1/reports') {
      try {
        const ipHash = await hmac(this.env.LINK_SECRET, 'ip:' + request.headers.get('CF-Connecting-IP'));
        return await intake(request, this.env, ipHash, this);
      } catch (error) {
        return reply({error:error instanceof Rejected ? error.code : 'disabled'},
          error instanceof Rejected ? error.status : 503);
      }
    }
    const input = await request.json();
    if (!UUID.test(input.id) || !/^[a-f0-9]{64}$/.test(input.fingerprint)) return reply({error:'invalid_report'}, 400);
    return this.ctx.storage.transaction(async txn => {
      const day = new Date().toISOString().slice(0,10);
      let state = await txn.get('quota');
      if (!state) state = {day, used:0, ips:{}, reports:{}};
      if (state.day !== day) { state.day = day; state.used = 0; state.ips = {}; }
      for (const [id, report] of Object.entries(state.reports)) {
        if (Date.now() - report.started > 7 * 86400000) delete state.reports[id];
      }
      const old = state.reports[input.id];
      if (path === '/finish') {
        if (!old || old.fingerprint !== input.fingerprint) return reply({error:'conflict'}, 409);
        old.result = input.result;
        await txn.put('quota', state);
        return reply({ok:true});
      }
      if (path !== '/reserve' || !/^[a-f0-9]{64}$/.test(input.ip)) return reply({error:'invalid_report'}, 400);
      if (old) {
        if (old.fingerprint !== input.fingerprint) return reply({error:'conflict'}, 409);
        if (old.result && old.result.body.error !== 'upstream') return reply({cached:old.result});
        if (!old.result) return reply({error: Date.now() - old.started > 90000 ? 'unconfirmed' : 'in_progress'}, 409);
        // A known pre-submit failure or explicit 4xx rejection may retry, but
        // consumes another attempt from both hard caps. Ambiguous POSTs never retry.
      }
      // Hard caps are global, atomic and not reset by isolate restarts or IP rotation.
      if (state.used >= 20 || (state.ips[input.ip] || 0) >= 3) return reply({error:'rate_limit'}, 429);
      state.used++;
      state.ips[input.ip] = (state.ips[input.ip] || 0) + 1;
      state.reports[input.id] = {fingerprint:input.fingerprint, started:Date.now()};
      await txn.put('quota', state);
      return reply({reserved:true});
    });
  }
}

function configured(env) {
  return env.REPORTS_ENABLED === 'true' && env.GITHUB_TOKEN && /^[a-f0-9]{64}$/.test(env.LINK_SECRET || '') &&
    /^https:\/\/shellshifter-reports\.[a-z0-9-]+\.workers\.dev$/.test(env.PUBLIC_ORIGIN || '') &&
    env.IMAGES && env.QUOTA && env.INTAKE_LIMIT && env.IMAGE_LIMIT;
}

async function handle(request, env) {
  const url = new URL(request.url);
  if (url.protocol !== 'https:') fail('invalid_report');
  if (request.method === 'GET' && url.pathname === '/health') return reply({schema:1, enabled:!!configured(env)});
  if (!configured(env)) fail('disabled', 503);
  if (url.origin !== env.PUBLIC_ORIGIN) fail('invalid_report');
  // Cloudflare supplies this header at the edge. Do not trust client-supplied X-Forwarded-For.
  const ip = request.headers.get('CF-Connecting-IP');
  if (!ip) fail('invalid_report');
  const ipHash = await hmac(env.LINK_SECRET, 'ip:' + ip);
  if (request.method === 'GET' && url.pathname.startsWith('/images/')) {
    if (!(await env.IMAGE_LIMIT.limit({key:ipHash})).success) fail('rate_limit', 429);
    const match = /^\/images\/([0-9a-f-]{36})\.png$/.exec(url.pathname);
    if (!match || !UUID.test(match[1]) || url.searchParams.get('key') !== await hmac(env.LINK_SECRET, url.pathname)) {
      return reply({error:'not_found'},404);
    }
    const image = await env.IMAGES.get('screenshots/' + match[1] + '.png');
    if (!image) return reply({error:'not_found'},404);
    return new Response(image.body, {headers: {...headers, 'Content-Type':'image/png',
      'Content-Disposition':'inline; filename="screenshot.png"', 'Cache-Control':'private, max-age=3600'}});
  }
  if (request.method !== 'POST' || url.pathname !== '/v1/reports' || url.search) return reply({error:'not_found'},404);
  if (!(await env.INTAKE_LIMIT.limit({key:ipHash})).success) fail('rate_limit',429);
  const origin = request.headers.get('Origin');
  if (origin && origin !== env.PUBLIC_ORIGIN) fail();
  if (request.headers.get('Content-Type')?.split(';')[0] !== 'application/json' ||
      Number(request.headers.get('Content-Length')) > MAX_BODY) fail();
  // Large JSON/image validation runs in the Durable Object, whose CPU budget
  // supports image checks. The front Worker only streams the bounded request.
  return env.QUOTA.get(env.QUOTA.idFromName('global-v1')).fetch(request);
}

async function intake(request, env, ipHash, quota) {
  const raw = await bounded(request.body, MAX_BODY);
  let report;
  try { report = validateReport(JSON.parse(new TextDecoder('utf-8', {fatal:true}).decode(raw))); }
  catch { fail(); }
  const fingerprint = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', raw)))
    .map(x => x.toString(16).padStart(2,'0')).join('');
  const quotaCall = (path, extra) => quota.fetch(new Request('https://quota' + path,
    {method:'POST', body:JSON.stringify({id:report.request_id, fingerprint, ...extra})}));
  const reservation = await quotaCall('/reserve', {ip:ipHash});
  const ticket = await reservation.json();
  if (!reservation.ok) return reply(ticket, reservation.status);
  if (ticket.cached) return reply(ticket.cached.body, ticket.cached.status);
  let imageKey, result, githubAttempted = false;
  try {
    const image = await sanitizePng(report.screenshot);
    let imageUrl;
    if (image) {
      const id = crypto.randomUUID(), path = '/images/' + id + '.png';
      imageKey = 'screenshots/' + id + '.png';
      await env.IMAGES.put(imageKey, image, {httpMetadata:{contentType:'image/png'}});
      imageUrl = env.PUBLIC_ORIGIN + path + '?key=' + await hmac(env.LINK_SECRET, path);
    }
    githubAttempted = true;
    const response = await fetch('https://api.github.com/repos/' + REPO + '/issues', {
      method:'POST', redirect:'manual', signal:AbortSignal.timeout(15000),
      headers: {'Authorization':'Bearer ' + env.GITHUB_TOKEN, 'Accept':'application/vnd.github+json',
        'X-GitHub-Api-Version':'2022-11-28', 'User-Agent':'ShellShifter-Reports', 'Content-Type':'application/json'},
      body:JSON.stringify({title:'[Bug] ' + report.title.replace(/@/g,'＠').replace(/[\r\n]/g,' '),
        body:issueBody(report, imageUrl)})});
    if (response.status !== 201) {
      if (response.status >= 500) fail('unconfirmed',503);
      // A definite rejection can clean its orphan image. Never echo GitHub errors/secrets.
      if (imageKey) await env.IMAGES.delete(imageKey);
      githubAttempted = false;
      fail('upstream',503);
    }
    const issue = await response.json();
    if (!new RegExp('^https://github\\.com/' + REPO + '/issues/[1-9][0-9]*$').test(issue.html_url)) fail('unconfirmed',503);
    result = {status:201, body:{issue_url:issue.html_url}};
  } catch (error) {
    // An ambiguous POST is never retried automatically: it may already have created an issue.
    const code = githubAttempted ? 'unconfirmed' : error instanceof Rejected ? error.code : 'upstream';
    if (!githubAttempted && imageKey) await env.IMAGES.delete(imageKey).catch(() => {});
    result = {status:error instanceof Rejected ? error.status : 503, body:{error:code}};
  }
  await quotaCall('/finish', {result});
  return reply(result.body, result.status);
}

export default {
  async fetch(request, env) {
    try { return await handle(request, env); }
    catch (error) { return reply({error:error instanceof Rejected ? error.code : 'disabled'},
      error instanceof Rejected ? error.status : 503); }
  }
};
