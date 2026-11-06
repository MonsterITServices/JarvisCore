const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '../frontend/index.html'), 'utf8');
const source = html.match(/    function voiceCommandFromTranscript\([\s\S]*?\n    }/);
assert.ok(source, 'voice command parser must exist');
const parse = new Function(`${source[0]}; return voiceCommandFromTranscript;`)();

assert.deepEqual(parse('hello there', false), { command: '', awaitingCommand: false });
assert.deepEqual(parse('Computer, what time is it?', false), { command: 'what time is it?', awaitingCommand: false });
assert.deepEqual(parse('COMPUTER', false), { command: '', awaitingCommand: true });
assert.deepEqual(parse('turn on the light', true), { command: 'turn on the light', awaitingCommand: false });
assert.deepEqual(parse('computerized systems', false), { command: '', awaitingCommand: false });
console.log('Voice wake-word checks passed');