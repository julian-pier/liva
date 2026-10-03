const assert = require('assert');

function classifyTbSlots(sets, pct = 0.05, abs = 5) {
  const weights = sets.map(s => s.weight).filter(w => Number.isFinite(w));
  if (!weights.length) return sets.map(() => ({ tb_type: null, tb_slot: null }));
  const topWeight = Math.max(...weights);
  const cutoff = Math.max(topWeight * (1 - pct), topWeight - abs);
  const counters = { T: 0, B: 0 };
  return sets.map((set) => {
    if (!Number.isFinite(set.weight)) return { tb_type: null, tb_slot: null };
    const tb_type = set.weight >= cutoff ? 'T' : 'B';
    counters[tb_type] += 1;
    return { tb_type, tb_slot: counters[tb_type], reps: set.reps, weight: set.weight, rpe: set.rpe };
  });
}

function findLastReference(lastSlots, tbType, tbSlot) {
  if (!tbType || !tbSlot) return null;
  const sameType = lastSlots.filter(slot => slot.tb_type === tbType);
  if (!sameType.length) return null;
  const exact = sameType.find(slot => slot.tb_slot === tbSlot);
  if (exact) return exact;
  let best = sameType[0];
  let bestDiff = Math.abs(best.tb_slot - tbSlot);
  sameType.forEach(slot => {
    const diff = Math.abs(slot.tb_slot - tbSlot);
    if (diff < bestDiff) {
      best = slot;
      bestDiff = diff;
    }
  });
  return best;
}

function formatLastRef(lastRef) {
  if (!lastRef) return '';
  const reps = lastRef.reps != null ? lastRef.reps : '';
  const weight = lastRef.weight != null ? lastRef.weight : '';
  const rpe = lastRef.rpe != null ? lastRef.rpe : '';
  let text = '';
  if (reps && weight) text = `${reps}x${weight}`;
  else if (weight) text = `${weight}kg`;
  else if (reps) text = `${reps} reps`;
  if (rpe) text = `${text}@${rpe}`;
  return text;
}

function lastTextForRow(currentSets, lastSets, idx) {
  const currentSlots = classifyTbSlots(currentSets);
  const lastSlots = classifyTbSlots(lastSets);
  const slot = currentSlots[idx] || { tb_type: null, tb_slot: null };
  const lastRef = (!lastSets.length || idx < lastSets.length)
    ? findLastReference(lastSlots, slot.tb_type, slot.tb_slot)
    : null;
  if (lastRef) return formatLastRef(lastRef);
  if (lastSets.length && idx >= lastSets.length) return '-';
  return '';
}

function run() {
  const lastSets = [
    { reps: 8, weight: 100, rpe: 9 },
    { reps: 10, weight: 90, rpe: 8 },
  ];
  const currentSets = [
    { reps: 9, weight: 100, rpe: 8.5 },
    { reps: 10, weight: 90, rpe: 8 },
    { reps: 12, weight: 80, rpe: 7.5 },
  ];

  assert.strictEqual(lastTextForRow(currentSets, lastSets, 0), '8x100@9');
  assert.strictEqual(lastTextForRow(currentSets, lastSets, 1), '10x90@8');
  assert.strictEqual(lastTextForRow(currentSets, lastSets, 2), '-');

  const onlyOne = [{ reps: 6, weight: 60, rpe: 8 }];
  assert.strictEqual(lastTextForRow(onlyOne, lastSets, 0), '8x100@9');
  console.log('OK');
}

run();
