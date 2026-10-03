const assert = require('assert');

function nextFlagCycle(flag) {
  if (flag === 'sick') return 'alcohol';
  if (flag === 'alcohol') return null;
  return 'sick';
}

assert.strictEqual(nextFlagCycle(null), 'sick');
assert.strictEqual(nextFlagCycle('sick'), 'alcohol');
assert.strictEqual(nextFlagCycle('alcohol'), null);
console.log('OK');
