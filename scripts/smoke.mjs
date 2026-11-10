// Real Community login, lobby joins and persistent identity over a restart.
import assert from 'node:assert/strict';
import { readFileSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const stateFile = fileURLToPath(new URL('../.build/smoke-accounts.json', import.meta.url));
const origin = 'http://127.0.0.1:17350';
const mode = process.argv[2];
async function request(path, body, authorization) {
  const response = await fetch(origin + path, {
    method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: authorization },
    body: JSON.stringify(body), signal: AbortSignal.timeout(15000),
  });
  const data = await response.json();
  assert.ok(response.ok, `${path}: ${JSON.stringify(data)}`);
  return data;
}
async function login(device) {
  const data = await request('/v2/account/authenticate/device?create=true', { id: device },
    'Basic ' + Buffer.from('defaultkey:').toString('base64'));
  assert.ok(data.token);
  const claims = JSON.parse(Buffer.from(data.token.split('.')[1], 'base64url'));
  return { token: data.token, userId: claims.uid, device };
}
async function rpc(account, name, payload) {
  const response = await request(`/v2/rpc/${name}`, JSON.stringify(payload), `Bearer ${account.token}`);
  return JSON.parse(response.payload);
}
async function socket(account) {
  const url = new URL(origin.replace('http:', 'ws:') + '/ws');
  url.searchParams.set('token', account.token);
  url.searchParams.set('format', 'json');
  const ws = new WebSocket(url);
  let counter = 0;
  const pending = new Map();
  ws.addEventListener('message', (event) => {
    const message = JSON.parse(event.data);
    const waiter = pending.get(message.cid);
    if (!waiter) return;
    clearTimeout(waiter.timeout);
    pending.delete(message.cid);
    if (message.error) waiter.reject(new Error(JSON.stringify(message.error)));
    else waiter.resolve(message);
  });
  await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => { ws.close(); reject(new Error('WebSocket connection timed out')); }, 10000);
    ws.addEventListener('open', () => { clearTimeout(timeout); resolve(); }, { once: true });
    ws.addEventListener('error', () => { clearTimeout(timeout); reject(new Error('WebSocket connection failed')); }, { once: true });
  });
  return {
    close: () => ws.close(),
    send: (body) => new Promise((resolve, reject) => {
      const cid = String(++counter);
      const timeout = setTimeout(() => { pending.delete(cid); reject(new Error('WebSocket reply timed out')); }, 15000);
      pending.set(cid, { resolve, reject, timeout });
      ws.send(JSON.stringify({ cid, ...body }));
    }),
  };
}

if (mode === 'create') {
  const accounts = await Promise.all([login('bmp-packaging-smoke-player-one'), login('bmp-packaging-smoke-player-two')]);
  for (let i = 0; i < accounts.length; i++) {
    const response = await rpc(accounts[i], 'session_init', { steamName: `SmokePlayer${i}` });
    assert.ok(response.player);
    assert.equal(response.tosRequired, false, 'Community login must not require Official notice');
    await rpc(accounts[i], 'tos_accept', { chatEligible: true });
  }
  const sockets = [];
  try {
    sockets.push(await socket(accounts[0]), await socket(accounts[1]));
    const lobby = await rpc(accounts[0], 'lobby_create', { modId: 'PvP', maxPlayers: 2 });
    assert.ok(lobby.matchId && lobby.code);
    const host = await sockets[0].send({ match_join: { match_id: lobby.matchId, metadata: { modId: 'PvP' } } });
    assert.equal(host.match.match_id, lobby.matchId);
    const found = await rpc(accounts[1], 'lobby_find', { code: lobby.code, modId: 'PvP' });
    assert.equal(found.matchId, lobby.matchId);
    const guest = await sockets[1].send({ match_join: { match_id: found.matchId, metadata: { modId: 'PvP' } } });
    assert.equal(guest.match.match_id, lobby.matchId);
    for (const connection of sockets) await connection.send({ match_leave: { match_id: lobby.matchId } });
    writeFileSync(stateFile, JSON.stringify(accounts.map(({ device, userId }) => ({ device, userId }))));
    console.log('Community Device login and two-player lobby joins passed');
  } finally {
    for (const connection of sockets) connection.close();
  }
} else if (mode === 'reconnect') {
  for (const saved of JSON.parse(readFileSync(stateFile, 'utf8'))) {
    const account = await login(saved.device);
    assert.equal(account.userId, saved.userId, 'Account identity must survive restart');
    const response = await rpc(account, 'session_init', {});
    assert.equal(response.ageRequired, false, 'Stored profile must survive restart');
  }
  console.log('Persistent accounts and profiles survived migration rerun and restart');
} else {
  throw new Error('Use create or reconnect');
}
