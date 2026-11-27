// Node.js runtime adapter: loads the customer's handler from /function and serves it over HTTP or runs it once as cron.
// The contract is the one in titan-ops features/ovc-function/design.md#runtimes. The adapter is PID 1 in the pod, and
// the kernel gives PID 1 no default action for SIGTERM, so it handles SIGTERM itself. No npm package.
//
// Environment: PORT, TITAN_HANDLER (`module.function`), TITAN_TRIGGER (`http` or `cron`), TITAN_FUNCTION_ID,
// TITAN_VERSION_ID, TITAN_TIMEOUT_SECONDS, TITAN_SCHEDULE, TITAN_SCHEDULED_TIME, TITAN_FUNCTION_DIR (default /function).
import { randomUUID } from 'node:crypto';
import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const MAX_BODY = 6 * 1024 * 1024;
const SHUTDOWN_WAIT_MS = 2000;
const FRAMING_HEADERS = new Set(['content-length', 'transfer-encoding', 'connection', 'server']);
const TOKEN = /^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$/;
const HEADER_VALUE = /^[\t\x20-\x7e]*$/; // visible ASCII: what every runtime sends as a header value
const TARGET = /^(?:[A-Za-z][A-Za-z0-9+.-]*:\/\/[^/?]*)?([^?]*)(?:\?([\s\S]*))?$/;
const EXTENSIONS = ['.mjs', '.js', '.cjs'];

class InvalidResponse extends Error {}

function fail(message, code = 1) {
  process.stderr.write(message + '\n');
  process.exit(code);
}

// TITAN_TIMEOUT_SECONDS as a number of seconds; anything but a positive finite number means no deadline
function timeoutSeconds() {
  const raw = process.env.TITAN_TIMEOUT_SECONDS ?? '';
  const n = raw.trim() === '' ? NaN : Number(raw);
  return Number.isFinite(n) && n > 0 ? n : null;
}

// (path, raw query) of a request target: an absolute-form target loses its scheme and host, a leading run of slashes is one
export function splitTarget(target) {
  const [, path, query] = TARGET.exec(target);
  return ['/' + path.replace(/^\/+/, ''), query ?? ''];
}

function makeContext(requestId) {
  const timeout = timeoutSeconds();
  const deadline = timeout === null ? null : performance.now() + timeout * 1000;
  return {
    functionId: process.env.TITAN_FUNCTION_ID ?? '',
    versionId: process.env.TITAN_VERSION_ID ?? '',
    requestId,
    getRemainingTimeInMillis: () => (deadline === null ? null : Math.max(0, Math.floor(deadline - performance.now()))),
  };
}

export async function loadHandler(spec, codeDir) {
  const dot = spec.lastIndexOf('.');
  if (dot <= 0 || dot === spec.length - 1) throw new Error(`TITAN_HANDLER ${JSON.stringify(spec)} is not \`module.function\``);
  const root = path.resolve(codeDir);
  const base = path.resolve(root, spec.slice(0, dot));
  if (!base.startsWith(root + path.sep)) throw new Error(`module ${spec.slice(0, dot)} is outside ${codeDir}`);
  const file = [base, ...EXTENSIONS.map((e) => base + e)].find((f) => fs.existsSync(f) && fs.statSync(f).isFile());
  if (!file) throw new Error(`no module ${spec.slice(0, dot)} (.mjs, .js or .cjs) in ${codeDir}`);
  const ns = await import(pathToFileURL(file).href);
  const name = spec.slice(dot + 1);
  const fn = ns[name] ?? ns.default?.[name]; // ESM export, or CommonJS module.exports.<name>
  if (fn === undefined) throw new Error(`module ${spec.slice(0, dot)} has no ${JSON.stringify(name)}`);
  if (typeof fn !== 'function') throw new TypeError(`${spec} is not a function`);
  return fn;
}

export function toResponse(result) {
  if (typeof result === 'string') return { status: 200, headers: [['content-type', 'text/plain; charset=utf-8']], body: Buffer.from(result) };
  if (result !== null && typeof result === 'object' && typeof result.then === 'function') {
    throw new InvalidResponse('the function returned a promise that was not awaited');
  }
  if (result === null || typeof result !== 'object' || Array.isArray(result)) {
    throw new InvalidResponse(`the function returned ${result === null ? 'null' : Array.isArray(result) ? 'an array' : typeof result}, not a string or an object with statusCode`);
  }
  const status = result.statusCode;
  if (!Number.isInteger(status) || status < 200 || status > 599) throw new InvalidResponse(`statusCode ${JSON.stringify(status)} is not an integer from 200 to 599`);
  const headers = new Map(); // one header per name: the last one given wins
  const given = result.headers ?? {};
  if (typeof given !== 'object' || Array.isArray(given)) throw new InvalidResponse('headers is not an object');
  for (const [name, value] of Object.entries(given)) {
    if (!TOKEN.test(name)) throw new InvalidResponse(`header name ${JSON.stringify(name)} is not valid`);
    if (typeof value !== 'string') throw new InvalidResponse(`header ${name}: the value is not a string`);
    if (!HEADER_VALUE.test(value)) throw new InvalidResponse(`header ${name}: the value is not visible ASCII`);
    if (!FRAMING_HEADERS.has(name.toLowerCase())) headers.set(name.toLowerCase(), [name, value]);
  }
  const body = result.body ?? '';
  const list = [...headers.values()];
  if (typeof body !== 'string') throw new InvalidResponse('body is not a string');
  if (result.isBase64Encoded === true) {
    if (!/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(body)) throw new InvalidResponse('body is not valid base64');
    return { status, headers: list, body: Buffer.from(body, 'base64') };
  }
  return { status, headers: list, body: Buffer.from(body) };
}

async function readBody(req) {
  const declared = Number(req.headers['content-length'] ?? 0);
  if (declared > MAX_BODY) return null;
  const chunks = [];
  let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > MAX_BODY) return null;
    chunks.push(chunk);
  }
  return Buffer.concat(chunks);
}

function runHttp(handler) {
  const raw = process.env.PORT || '8080';
  const port = Number(raw);
  if (!/^[0-9]+$/.test(raw) || port > 65535) fail(`PORT ${JSON.stringify(raw)} is not a number`);
  let inflight = 0;
  let stopping = false;

  const server = http.createServer(async (req, res) => {
    inflight++;
    const send = (status, headers, body) => {
      if (stopping) res.setHeader('connection', 'close');
      res.statusCode = status;
      for (const [k, v] of headers) res.setHeader(k, v);
      if (status !== 204) res.setHeader('content-length', body.length); // RFC 9110: a 204 has no Content-Length
      res.end(req.method === 'HEAD' || status === 204 || status === 304 ? undefined : body);
    };
    try {
      let body;
      try {
        body = await readBody(req);
      } catch {
        return res.destroy(); // the client hung up mid-body
      }
      if (body === null) {
        res.setHeader('connection', 'close');
        req.resume(); // read what the client is still sending, so the answer is not lost to a reset
        return send(413, [['content-type', 'text/plain; charset=utf-8']], Buffer.from('Request Entity Too Large'));
      }
      const [path, rawQuery] = splitTarget(req.url);
      const query = new Map(); // not an object literal: a name like `constructor` or `__proto__` is a name here
      for (const [k, v] of new URLSearchParams(rawQuery)) query.set(k, query.has(k) ? `${query.get(k)},${v}` : v);
      const headers = new Map();
      for (const [k, v] of Object.entries(req.headersDistinct)) headers.set(k, v.join(', '));
      let text;
      let isBase64Encoded = false;
      try {
        text = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(body); // ignoreBOM: keep a leading BOM
      } catch {
        text = body.toString('base64');
        isBase64Encoded = true;
      }
      const event = {
        trigger: 'http', method: req.method, path, query: Object.fromEntries(query), headers: Object.fromEntries(headers),
        body: text, isBase64Encoded,
      };
      const context = makeContext(headers.get('x-request-id') || randomUUID());
      let out;
      try {
        out = toResponse(await handler(event, context));
        for (const [k, v] of out.headers) res.setHeader(k, v); // Node rejects what is not a valid header; checked before sending
        for (const [k] of out.headers) res.removeHeader(k);
      } catch (e) {
        const what = e instanceof InvalidResponse ? 'invalid response' : 'function error';
        process.stderr.write(`${what} (${context.requestId}):\n${e instanceof InvalidResponse ? e.message : (e?.stack ?? e)}\n`);
        out = { status: 500, headers: [['content-type', 'text/plain; charset=utf-8']], body: Buffer.from('Internal Server Error') };
      }
      send(out.status, out.headers, out.body);
    } catch (e) {
      process.stderr.write(`adapter error:\n${e?.stack ?? e}\n`); // whatever it is, a request must not end the process
      if (res.headersSent) res.destroy();
      else {
        res.statusCode = 500;
        res.end();
      }
    } finally {
      inflight--;
    }
  });
  server.keepAliveTimeout = 0; // no idle timeout: the peer (queue-proxy) closes, as with the Python and Go adapters
  server.on('clientError', (_e, socket) => socket.end('HTTP/1.1 400 Bad Request\r\nconnection: close\r\n\r\n'));
  server.on('error', (e) => fail(`cannot listen on ${port}: ${e.message}`));
  // a promise the function left unhandled, or a throw in its timer, must not end PID 1 and the requests of other callers
  process.on('unhandledRejection', (e) => process.stderr.write(`unhandled rejection:\n${e?.stack ?? e}\n`));
  process.on('uncaughtException', (e) => process.stderr.write(`uncaught exception:\n${e?.stack ?? e}\n`));

  process.removeAllListeners('SIGTERM'); // the start-up one
  process.on('SIGTERM', () => {
    stopping = true;
    server.close();
    server.closeIdleConnections();
    const end = Date.now() + SHUTDOWN_WAIT_MS;
    const tick = setInterval(() => {
      server.closeIdleConnections();
      if (inflight === 0 || Date.now() >= end) process.exit(0);
    }, 20);
    tick.unref?.();
    setTimeout(() => process.exit(0), SHUTDOWN_WAIT_MS + 100); // the interval alone would not keep an empty loop alive
  });
  server.listen(port);
}

async function runCron(handler) {
  const scheduledTime = process.env.TITAN_SCHEDULED_TIME || new Date().toISOString().replace(/:\d\d\.\d+Z$/, ':00Z');
  const event = { trigger: 'cron', schedule: process.env.TITAN_SCHEDULE ?? '', scheduledTime };
  process.removeAllListeners('SIGTERM'); // the start-up one
  process.on('SIGTERM', () => process.exit(143));
  let code = 0;
  try {
    await handler(event, makeContext(randomUUID()));
  } catch (e) {
    process.stderr.write(`${e?.stack ?? e}\n`);
    code = 1;
  }
  process.exit(code);
}

async function main() {
  // PID 1 has no default action for SIGTERM: until the real handler is in, ending is all there is to do
  process.on('SIGTERM', () => process.exit(143));
  const trigger = process.env.TITAN_TRIGGER || 'http';
  if (trigger !== 'http' && trigger !== 'cron') fail(`TITAN_TRIGGER ${JSON.stringify(trigger)} is neither http nor cron`);
  let handler;
  try {
    handler = await loadHandler(process.env.TITAN_HANDLER ?? '', process.env.TITAN_FUNCTION_DIR ?? '/function');
  } catch (e) {
    fail(String(e?.stack ?? e));
  }
  if (trigger === 'cron') await runCron(handler);
  else runHttp(handler);
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) await main();
