// Fixture handler of the contract suite: the same behaviours as fixtures/python/main.py and fixtures/go/main.go.
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const json = (obj, statusCode = 200) => ({ statusCode, headers: { 'content-type': 'application/json' }, body: JSON.stringify(obj) });

export const NOT_CALLABLE = 5; // TITAN_HANDLER=index.NOT_CALLABLE

const INVALID = [
  undefined,
  null,
  {},
  { statusCode: 'abc' },
  { statusCode: 99 },
  { statusCode: 600 },
  { statusCode: 200, headers: ['x'] },
  { statusCode: 200, headers: { 'x-n': 5 } },
  { statusCode: 200, body: 5 },
  { statusCode: 200, isBase64Encoded: true, body: 'not base64!' },
  { statusCode: true },
];

export async function handler(event, context) {
  if (event.trigger === 'cron') {
    console.log('CRON-EVENT ' + JSON.stringify(event));
    const mode = process.env.FIXTURE_CRON ?? '';
    if (mode === 'fail') throw new Error('cron-boom');
    if (mode === 'sleep') await sleep(30000);
    return undefined;
  }
  const { path, query } = event;
  switch (path) {
    case '/echo':
      return json({
        event,
        context: { functionId: context.functionId, versionId: context.versionId, requestId: context.requestId, remainingMs: context.getRemainingTimeInMillis() },
      });
    case '/status':
      return { statusCode: Number(query.code), headers: { 'x-a': 'b' }, body: 'ok' };
    case '/string':
      return 'hello';
    case '/throw':
      throw new Error('boom-secret');
    case '/bad':
      return INVALID[Number(query.n)];
    case '/crlf':
      return { statusCode: 200, headers: { 'x-a': 'b\r\nx-evil: 1' }, body: 'x' };
    case '/framing':
      return { statusCode: 200, headers: { 'Content-Length': '1', Connection: 'close', 'X-Ok': '1' }, body: 'hello' };
    case '/b64':
      return { statusCode: 200, isBase64Encoded: true, body: Buffer.from([0, 255, 65, 66]).toString('base64') };
    case '/badhead':
      return { statusCode: 200, headers: { 'bad name': 'x' }, body: 'x' };
    case '/len':
      console.log('LEN-CALLED');
      return String(event.isBase64Encoded ? Buffer.from(event.body, 'base64').length : Buffer.byteLength(event.body));
    case '/sleep':
      await sleep(Number(query.ms));
      return 'slept';
    case '/remaining': {
      const first = context.getRemainingTimeInMillis();
      await sleep(300);
      return json([first, context.getRemainingTimeInMillis()]);
    }
    default:
      return { statusCode: 404, body: 'no such fixture route' };
  }
}
