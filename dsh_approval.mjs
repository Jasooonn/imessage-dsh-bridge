// An answerer for the bridge's headless process only. No approval is granted
// unless the paired bridge process sends a matching one-shot decision on stdin.
import { randomUUID } from 'node:crypto';
import { createInterface } from 'node:readline';

export const name = 'imessage-bridge-approval';

const PREFIX = '\u001eDS_BRIDGE_APPROVAL ';
const MAX_ARGUMENT_BYTES = 4096;
const WAIT_MS = 125_000;

function exactToolCall(request) {
  if (typeof request.callId !== 'string' || !request.callId) return null;
  const session = request.agent?.session;
  if (!session || !Number.isSafeInteger(session.seq)) return null;
  for (let seq = session.seq - 1; seq >= 0; seq -= 1) {
    const event = session.eventAt(seq);
    if (event?.type !== 'tool/call' || event.data?.callId !== request.callId) continue;
    if (event.data.name !== request.toolName || typeof event.data.arguments !== 'string') return null;
    if (Buffer.byteLength(event.data.arguments, 'utf8') > MAX_ARGUMENT_BYTES) return null;
    return event.data.arguments;
  }
  return null;
}

function askBridge(request, argumentsJson) {
  const id = randomUUID();
  const line = JSON.stringify({
    id,
    sessionId: String(request.agent.session.id),
    callId: request.callId,
    tool: request.toolName,
    arguments: argumentsJson,
  });
  return new Promise((resolve) => {
    let settled = false;
    const input = createInterface({ input: process.stdin, terminal: false });
    process.stdin._handle?.ref?.();
    const finish = (outcome) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      request.signal?.removeEventListener('abort', onAbort);
      input.close();
      process.stdin.pause();
      process.stdin._handle?.unref?.();
      resolve(outcome);
    };
    const onAbort = () => finish('cancelled');
    const timer = setTimeout(() => finish('rejected'), WAIT_MS);
    request.signal?.addEventListener('abort', onAbort, { once: true });
    input.on('line', (text) => {
      if (text.length > 512) return;
      let response;
      try { response = JSON.parse(text); } catch { return; }
      if (response?.type !== 'ds-approval-decision' || response.id !== id) return;
      finish(response.outcome === 'allowed-once' ? 'allowed-once' : 'rejected');
    });
    input.on('close', () => finish('rejected'));
    process.stdout.write(`${PREFIX}${line}\n`);
  });
}

export function apply(ctx) {
  ctx.on('approval/request', async (request) => {
    // The patch is installed only on the bridge invocation. Missing transport
    // or an unidentifiable tool call is a denial, never an implicit allow.
    if (process.env.DS_BRIDGE_APPROVAL !== '1') return 'rejected';
    const argumentsJson = exactToolCall(request);
    if (argumentsJson === null) return 'rejected';
    return askBridge(request, argumentsJson);
  });
}
