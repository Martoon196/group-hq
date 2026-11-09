const crypto = require('node:crypto');

function packets(projection, reachedStart, connectorId='beeper') {
  const messages = projection.thread.messages;
  const groups = [];
  let group = [], size = 0;
  for (const message of messages) {
    const bytes = Buffer.byteLength(JSON.stringify(message)) + 1;
    if (group.length && (group.length >= 100 || size + bytes > 175000)) {
      groups.push(group); group = []; size = 0;
    }
    group.push(message); size += bytes;
  }
  if (group.length || !groups.length) groups.push(group);
  return groups.map((items, index) => {
    // The oldest portion is first, but record source completion only after all pieces are acknowledged.
    const reached = reachedStart && index === groups.length - 1;
    const thread = {...projection.thread, messages:items, preview:''};
    const omitted = index === groups.length - 1 ? projection.evidence?.omitted_rows || 0 : 0;
    const chunk = crypto.createHash('sha256').update(JSON.stringify([connectorId, projection.account_key,
      projection.account_id, projection.user_id, thread.id, thread.participant_id, items, reached, omitted])).digest('hex');
    return {...projection, connector_id:connectorId, thread, evidence:{omitted_rows:omitted}, chunk_id:chunk, reached_start:reached,
      oldest_id:items[0]?.id ?? null, newest_id:items.at(-1)?.id ?? null};
  });
}

async function submit(call, chunks) {
  for (const packet of chunks) {
    const ack = await call(packet);
    if (ack?.ok !== true || ack.chunk_id !== packet.chunk_id || !Number.isInteger(ack.stored)
        || ack.stored < 0 || ack.reached_start !== packet.reached_start) throw Error('history_not_acknowledged');
  }
}

module.exports = {packets, submit};
