// SPDX-License-Identifier: GPL-2.0
// AgentTx dashboard front end.  No framework, no build step: this has to
// run from a QEMU serial-forwarded port on four different machines.
'use strict';

const SVG = 'http://www.w3.org/2000/svg';
const CLS = ['rev', 'def', 'comp', 'irr'];          // index == enum tx_class
const $ = (id) => document.getElementById(id);
const el = (tag, attrs = {}, text) => {
  const n = document.createElementNS(SVG, tag);
  for (const k in attrs) n.setAttribute(k, attrs[k]);
  if (text != null) n.textContent = text;
  return n;
};
const esc = (s) => String(s ?? '').replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const pct = (x) => (x == null ? '—' : x.toFixed(1) + '%');
const f3 = (x) => (x == null ? '—' : x.toFixed(3));

let SNAP = null;

/* ============================================================
 * The mechanism diagram
 * ==========================================================*/
const STAGES = [
  ['agent',     'agent process',   'writes, unlinks, sendmsg'],
  ['syscall',   'syscall',         'the boundary today'],
  ['hook',      'BPF LSM hook',    'socket_sendmsg, inode_unlink…'],
  ['gate',      'tx gate',         'tx_current_id() kfunc'],
  ['feat',      'features',        '16 × u8, integer only'],
  ['model',     'classifier',      'int8, inside the hook'],
  ['thresh',    'fail-closed',     'conf < TX_CONFIDENCE_MIN'],
];

const OUTCOMES = [
  ['reversible',  'proceed — the CoW overlay captures it',      'captured'],
  ['deferrable',  'held in the WAL — userspace told “success”', 'deferred'],
  ['compensable', 'emitted — cancel window recorded',           'emitted'],
  ['irrevocable', 'tx DOOMED — escalate to a human',            'escalated'],
];

const paths = {};    // class index -> SVGPathElement for the fan-out
let mainPath = null; // the spine, agent -> threshold

function buildFlow() {
  const svg = $('flow');
  svg.innerHTML = '';
  const BW = 146, BH = 44, y0 = 38, gap = 18;

  // --- the spine -------------------------------------------------------
  const xs = [];
  STAGES.forEach((s, i) => {
    const x = 16 + i * (BW + gap);
    xs.push(x);
    const g = el('g');
    g.appendChild(el('rect', {x, y: y0, width: BW, height: BH, rx: 6,
                             class: 'box' + (i >= 4 ? ' hot' : '')}));
    g.appendChild(el('text', {x: x + BW / 2, y: y0 + 18, 'text-anchor': 'middle',
                              class: 'lbl'}, s[1]));
    g.appendChild(el('text', {x: x + BW / 2, y: y0 + 32, 'text-anchor': 'middle',
                              class: 'sub'}, s[2]));
    svg.appendChild(g);
    if (i > 0) {
      const px = x - gap, py = y0 + BH / 2;
      svg.appendChild(el('path', {d: `M${px - 1} ${py} L${x - 3} ${py}`, class: 'path'}));
      svg.appendChild(el('path', {d: `M${x - 8} ${py - 3.5} l5 3.5 l-5 3.5 z`,
                                  class: 'path', fill: '#2a3646'}));
    }
  });

  // invisible spine for particle animation
  const spineY = y0 + BH / 2;
  mainPath = el('path', {d: `M20 ${spineY} L${xs[6] + BW} ${spineY}`,
                         class: 'path', opacity: 0});
  svg.appendChild(mainPath);

  // --- the four-way fan-out -------------------------------------------
  const oy = 176, OW = 262, OH = 58, ogap = 20;
  const fanFromX = xs[6] + BW, fanFromY = spineY;
  OUTCOMES.forEach((o, i) => {
    const x = 16 + i * (OW + ogap);
    const cx = x + OW / 2;
    const d = `M${fanFromX} ${fanFromY} C${fanFromX + 60} ${fanFromY}, ` +
              `${cx} ${oy - 70}, ${cx} ${oy - 4}`;
    const p = el('path', {d, class: 'path ' + CLS[i], opacity: .42});
    svg.appendChild(p);
    paths[i] = p;

    const g = el('g');
    g.appendChild(el('rect', {x, y: oy, width: OW, height: OH, rx: 6, class: 'box'}));
    g.appendChild(el('rect', {x, y: oy, width: 3, height: OH,
                              fill: `var(--${CLS[i]})`}));
    g.appendChild(el('text', {x: x + 14, y: oy + 21, class: 'lbl',
                              fill: `var(--${CLS[i]})`}, o[0]));
    g.appendChild(el('text', {x: x + 14, y: oy + 38, class: 'sub'}, o[1]));
    g.appendChild(el('text', {x: x + OW - 12, y: oy + 21, class: 'cap',
                              'text-anchor': 'end'}, 'verdict: ' + o[2]));
    // live counter per class
    const c = el('text', {x: x + OW - 12, y: oy + 40, class: 'lbl',
                          'text-anchor': 'end', fill: `var(--${CLS[i]})`,
                          id: 'cnt' + i}, '0');
    g.appendChild(c);
    svg.appendChild(g);
  });

  // --- what commit and abort do ---------------------------------------
  const by = 282, BH2 = 76;
  const half = [
    ['tx_commit  (supervisor only)',
     ['CoW upper layer merged into lower',
      'WAL replayed in seq order — the packets finally leave',
      'compensable windows start counting']],
    ['tx_abort',
     ['upper layer discarded — the writes never happened',
      'WAL discarded — the packets never existed',
      'DOOMED transactions refuse: -EPERM']],
  ];
  half.forEach((h, i) => {
    const x = 16 + i * (574 + 20), W = 574;
    const g = el('g');
    g.appendChild(el('rect', {x, y: by, width: W, height: BH2, rx: 6, class: 'box'}));
    g.appendChild(el('text', {x: x + 14, y: by + 19, class: 'lbl',
                              fill: i ? 'var(--irr)' : 'var(--rev)'}, h[0]));
    h[1].forEach((line, j) =>
      g.appendChild(el('text', {x: x + 14, y: by + 36 + j * 13, class: 'sub'}, '· ' + line)));
    svg.appendChild(g);
  });
}

/* ---- particle animation: an effect travelling the real path ---- */
function fly(classIdx) {
  const svg = $('flow');
  const colour = `var(--${CLS[classIdx]})`;
  const dot = el('circle', {class: 'dot', r: 3.5, fill: colour,
                            opacity: .95});
  svg.appendChild(dot);
  const p1 = mainPath, p2 = paths[classIdx];
  const L1 = p1.getTotalLength(), L2 = p2.getTotalLength();
  const T1 = 900, T2 = 520;
  const start = performance.now();

  function step(now) {
    const dt = now - start;
    let pt;
    if (dt < T1) {
      pt = p1.getPointAtLength(L1 * (dt / T1));
    } else if (dt < T1 + T2) {
      pt = p2.getPointAtLength(L2 * ((dt - T1) / T2));
    } else {
      dot.remove();
      return;
    }
    dot.setAttribute('cx', pt.x);
    dot.setAttribute('cy', pt.y);
    requestAnimationFrame(step);
  }
  requestAnimationFrame(step);
}

/* ============================================================
 * Live panels
 * ==========================================================*/
const counts = [0, 0, 0, 0];
let effRows = 0;

function onEffect(ev) {
  const k = ev.klass ?? 0;
  counts[k]++;
  const c = $('cnt' + k);
  if (c) c.textContent = counts[k];
  fly(k);

  $('effempty').style.display = 'none';
  const tb = $('effects');
  const tr = document.createElement('tr');
  const dest = ev.daddr ? `${ev.daddr}:${ev.dport ?? '—'}` : '—';
  const flag = ev.escalated_by_threshold
    ? ' <span class="cap" style="color:var(--irr)">↑fail-closed</span>' : '';
  tr.innerHTML =
    `<td class="num">${ev.seq ?? ''}</td>` +
    `<td>${esc(ev.hook ?? ev.syscall ?? '—')}</td>` +
    `<td>${esc(dest)}</td>` +
    `<td class="num">${ev.payload_len ?? '—'}</td>` +
    `<td><span class="tag ${CLS[k]}">${esc(ev.klass_name)}</span>${flag}</td>` +
    `<td class="num">${ev.confidence ?? '—'}</td>` +
    `<td>${esc(ev.verdict_name ?? '—')}</td>`;
  tb.prepend(tr);
  if (++effRows > 200) { tb.lastChild?.remove(); effRows--; }
}

function onKmsg(ev) {
  $('kmsgempty').style.display = 'none';
  const tb = $('kmsg');
  const tr = document.createElement('tr');
  const colour = ev.kind === 'leak' || ev.kind === 'alarm' ? 'var(--irr)'
               : ev.kind === 'tx' ? 'var(--def)' : 'var(--dim)';
  tr.innerHTML = `<td style="color:${colour};white-space:normal">${esc(ev.text)}</td>`;
  tb.prepend(tr);
  while (tb.childElementCount > 120) tb.lastChild.remove();
}

function renderTx(stat, present) {
  const states = SNAP?.contract?.state_names ?? [];
  const cur = stat ? stat.state : 0;
  let h = '<div class="sm">';
  states.forEach((s, i) => {
    const term = ['done', 'aborted', 'failed'].includes(s);
    h += `<span class="st${i === cur ? ' cur' : ''}${term ? ' term' : ''}">${esc(s)}</span>`;
  });
  h += '</div>';
  if (!present) {
    h += `<p class="note warn" style="margin-top:11px">/dev/agenttx not present —
          the module is not loaded, so no transaction can be live. The diagram
          above is driven by replay.</p>`;
  } else if (stat && stat.tx_id) {
    h += `<hr class="sep"><div class="kv">
      <span class="k">tx_id</span><span class="v">${stat.tx_id}</span>
      <span class="k">owner pid</span><span class="v">${stat.owner_pid}</span>
      <span class="k">worst class</span><span class="v">
        <span class="tag ${CLS[stat.worst_class]}">${esc(stat.worst_class_name)}</span></span>
      <span class="k">effects deferred</span><span class="v">${stat.n_deferred}</span>
      <span class="k">inodes written</span><span class="v">${stat.n_written}</span>
      </div>`;
  } else {
    h += `<p class="note" style="margin-top:11px">module loaded, no transaction open.</p>`;
  }
  $('txstate').innerHTML = h;
}

/* ============================================================
 * Static panels from the snapshot
 * ==========================================================*/
function renderGate(g) {
  const box = $('gate');
  if (!g || !g.available) {
    let h = `<div class="missing">no summarised gate run.<br><br>
      <code>strace -f -tt -yy -e trace=network,desc -o t.strace &lt;agent&gt;</code><br>
      <code>python3 tools/harness/measure_gate.py --strace t.strace \
        --jsonl data/gate/run.jsonl --summary data/gate/run.summary.json</code></div>`;
    if (g && g.unsummarised?.length) {
      h += `<p class="note warn">${g.unsummarised.length} record file(s) have no
        summary: ${g.unsummarised.map(esc).join(', ')}. Re-run measure_gate.py with
        <code>--summary</code>. The connection figure cannot be recovered from the
        records alone — they contain only outbound operations.</p>`;
    }
    box.innerHTML = h;
    return;
  }
  let h = '';
  g.runs.forEach(r => {
    const s = r.syscall, c = r.connection;
    const band = (p) => p > 20 ? 'rev' : p >= 10 ? 'comp' : 'irr';
    const ratio = c.pct > 0.01 ? (s.pct / c.pct) : null;
    const parsePct = r.lines_total ? 100 * r.lines_parsed / r.lines_total : null;
    h += `
    <div class="grid g2" style="margin:0 0 6px">
      <div>
        <div class="note">per syscall — what the gate threshold is written against</div>
        <div class="big ${band(s.pct)}">${pct(s.pct)}</div>
        <div class="bar">
          <i style="width:${100*s.ff/s.total}%;background:var(--def)"></i>
          <i style="width:${100*s.rr/s.total}%;background:var(--comp)"></i>
          <i style="width:${100*s.undetermined/s.total}%;background:var(--dimmer)"></i>
        </div>
        <div class="legend">
          <span><b style="color:var(--def)">${s.ff}</b> fire-and-forget</span>
          <span><b style="color:var(--comp)">${s.rr}</b> request-response</span>
          <span><b>${s.undetermined}</b> undetermined</span>
        </div>
      </div>
      <div>
        <div class="note">per connection — a connection that ever read a byte back</div>
        <div class="big ${band(c.pct)}">${pct(c.pct)}</div>
        <div class="bar">
          <i style="width:${c.pct}%;background:var(--def)"></i>
          <i style="width:${100-c.pct}%;background:var(--comp)"></i>
        </div>
        <div class="legend">
          <span><b style="color:var(--def)">${c.ff}</b> of ${c.total} connections never read a reply</span>
          <span><b>${c.ops_on_ff}</b> of ${s.total} ops sit on those</span>
        </div>
      </div>
    </div>`;

    if (!r.trustworthy && ratio) {
      h += `<p class="note bad" style="margin-top:10px">
        <b>Both numbers come from the same trace (<code>${esc(r.file)}</code>,
        ${s.total} outbound operations) and disagree by ${ratio.toFixed(0)}×.</b>
        The per-syscall figure counts MTU-sized TLS fragments of a single logical
        request as independent effects, inflating fire-and-forget in the project's
        own favour. The per-connection figure scores a persistent connection that
        carries a blocking request <em>and</em> a fire-and-forget ping entirely as
        request-response, deflating it. The true value is bracketed between them;
        neither is publishable alone. The unit that would settle it is the logical
        request, which needs request framing the tool does not parse —
        <code>docs/journal/p4.md</code>.</p>`;
    } else if (r.trustworthy) {
      h += `<p class="note" style="margin-top:10px">The two units agree to within
        1.5×, which is the strongest statement this method supports.</p>`;
    }
    if (parsePct != null && parsePct < 95) {
      h += `<p class="note warn"><b>${(100-parsePct).toFixed(0)}% of the trace did not
        parse</b> (${r.lines_parsed} of ${r.lines_total} lines). Unfinished/resumed
        pairs across threads are the likely cause, and those are exactly where a
        blocking read appears — so the parse gap is not obviously neutral in either
        direction. Quantify before trusting either figure.</p>`;
    }
    h += `<p class="note">gate threshold verdict on the per-syscall figure:
      <b style="color:var(--${band(s.pct)})">${esc(r.verdict)}</b>
      — recorded, not acted on, while the two units disagree.</p>`;
  });
  box.innerHTML = h;
}

function confusion(cm, names) {
  if (!cm) return '';
  let h = '<table class="cm"><tr><th></th>';
  names.forEach(n => h += `<th>${esc(n.slice(0, 4))}</th>`);
  h += '</tr>';
  cm.forEach((row, i) => {
    h += `<tr><th>${esc(names[i].slice(0, 4))}</th>`;
    row.forEach((v, j) => {
      const cls = i === j ? 'diag' : (i === 3 && j !== 3 && v > 0 ? 'miss' : '');
      h += `<td class="${cls}">${v}</td>`;
    });
    h += '</tr>';
  });
  return h + '</table>';
}

function renderTrain(t, names) {
  if (!t) { $('train').innerHTML = '<div class="missing">run <code>make pipeline</code></div>'; return; }
  const tree = t.models?.tree, mlp = t.models?.mlp;
  let h = `<div class="kv">
    <span class="k">rows train / test</span><span class="v">${t.n_train} / ${t.n_test}</span>
  </div><hr class="sep">`;
  [['tree', tree], ['mlp', mlp]].forEach(([nm, m]) => {
    if (!m) return;
    h += `<div class="note" style="color:var(--ink);margin-bottom:3px">${nm}</div>
      <div class="kv">
        <span class="k">balanced accuracy</span><span class="v">${f3(m.balanced_accuracy)}</span>
        <span class="k">irrevocable recall</span>
          <span class="v" style="color:${m.irrevocable_recall >= .97 ? 'var(--rev)' : 'var(--comp)'}">
            ${f3(m.irrevocable_recall)}</span>
        <span class="k">irrevocable missed</span>
          <span class="v" style="color:${m.irrevocable_missed ? 'var(--irr)' : 'var(--rev)'}">
            ${m.irrevocable_missed}</span>
        <span class="k">escalation rate</span><span class="v">${f3(m.escalation_rate)}</span>
      </div><hr class="sep">`;
  });
  h += `<div class="note">tree confusion (rows = truth)</div>${confusion(tree?.confusion_matrix, names)}`;
  h += `<p class="note warn" style="margin-top:9px">Class balance is ≈95/2/1/2 — raw accuracy
        is meaningless here; predicting “reversible” unconditionally scores 95%.</p>`;
  h += `<p class="note bad">Every label in this run is synthetic. It shows the pipeline
        works. It is not a result.</p>`;
  $('train').innerHTML = h;
}

function renderQuant(q, model) {
  if (!q) { $('quant').innerHTML = '<div class="missing">run <code>make quantize</code></div>'; return; }
  const lost = q.int8.irrevocable_missed - q.float.irrevocable_missed;
  let h = `<div class="kv">
    <span class="k"></span><span class="v" style="color:var(--dim)">float32 → int8</span>
    <span class="k">balanced accuracy</span>
      <span class="v">${f3(q.float.balanced_accuracy)} → ${f3(q.int8.balanced_accuracy)}</span>
    <span class="k">irrevocable recall</span>
      <span class="v delta bad">${f3(q.float.irrevocable_recall)} → ${f3(q.int8.irrevocable_recall)}</span>
    <span class="k">escalation rate</span>
      <span class="v">${f3(q.float.escalation_rate)} → ${f3(q.int8.escalation_rate)}</span>
    <span class="k">decision agreement</span><span class="v">${f3(q.decision_agreement)}</span>
    <span class="k">blob</span><span class="v">${model?.blob_bytes ?? '—'} B</span>
  </div>`;
  if (lost > 0) {
    h += `<hr class="sep"><div class="big irr">${lost}</div>
      <p class="note bad">irrevocable effect${lost === 1 ? '' : 's'} the float model caught
      and the int8 model missed. That is a safety regression, not a rounding error.
      Either <code>TX_CONFIDENCE_MIN</code> rises or the tree ships — the tree is a
      bounded walk over integer comparisons, which is what the eBPF verifier wants
      anyway.</p>`;
  }
  $('quant').innerHTML = h;
}

function renderContract(c) {
  let h = `<div class="kv">
    <span class="k">ABI version</span><span class="v">${c.abi}</span>
    <span class="k">TX_CONFIDENCE_MIN</span>
      <span class="v">${c.confidence_min} <span style="color:var(--dim)">(${c.confidence_min_pct}%)</span></span>
    <span class="k">features</span><span class="v">${c.n_features} × u8</span>
  </div><hr class="sep">
  <div class="note">taxonomy — severity ascends, so “worst class so far” is a max()</div>
  <div class="sm" style="margin-top:6px">`;
  c.class_names.forEach((n, i) =>
    h += `<span class="tag ${CLS[i]}">${i} ${esc(n)}</span>`);
  h += `</div><hr class="sep"><div class="note">LSM hooks</div>
    <div class="sm" style="margin-top:6px">`;
  c.hook_names.slice(1).forEach(n => h += `<span class="st">${esc(n)}</span>`);
  h += `</div><p class="note" style="margin-top:9px">Read from the header at startup.
    If the taxonomy is reordered by a contract-change PR the dashboard refuses to
    start rather than recolouring itself wrongly.</p>`;
  $('contract').innerHTML = h;
}

function renderPills(s) {
  const d = s.device, src = s.sources;
  const p = [];
  p.push(d.present
    ? `<span class="pill on">module loaded · abi ${d.abi}</span>`
    : `<span class="pill off">/dev/agenttx absent</span>`);
  p.push(src.replay_on
    ? `<span class="pill warn">replay on — synthetic</span>`
    : `<span class="pill off">replay off</span>`);
  p.push(`<span class="pill ${src.dmesg === 'following' ? 'on' : 'off'}">dmesg ${esc(src.dmesg)}</span>`);
  p.push(`<button class="btn" id="toggle">${src.replay_on ? 'stop' : 'start'} replay</button>`);
  $('pills').innerHTML = p.join('');
  $('toggle').onclick = async () => {
    await fetch(src.replay_on ? '/api/replay/off' : '/api/replay/on');
    setTimeout(refresh, 150);
  };
}

async function refresh() {
  const r = await fetch('/api/snapshot');
  SNAP = await r.json();
  renderPills(SNAP);
  renderTx(SNAP.device.stat, SNAP.device.present);
  renderGate(SNAP.gate);
  renderTrain(SNAP.train, SNAP.contract.class_names);
  renderQuant(SNAP.quantize, SNAP.model);
  renderContract(SNAP.contract);
  $('effsrc').textContent = SNAP.sources.replay_on
    ? 'replay — static rule table, NOT the in-kernel model'
    : (SNAP.device.present ? 'live from the module' : 'idle');
}

buildFlow();
refresh();
setInterval(refresh, 5000);

const es = new EventSource('/api/events');
es.onmessage = (m) => {
  const ev = JSON.parse(m.data);
  if (ev.kind === 'effect') onEffect(ev);
  else if (ev.kind === 'stat') renderTx(ev.stat, ev.present);
  else onKmsg(ev);
};

/* ============================================================
 * Concurrency and deadlock (docs/deadlock.md)
 *
 * Renders the wait-for graph the simulator produces. Nodes are
 * transactions, edges are waits coloured by kind, and a detected cycle is
 * drawn in the irrevocable colour because that is what it usually costs.
 * ==========================================================*/
const WAITCOL = {OUTPUT: 'var(--def)', ESCALATE: 'var(--comp)', DATA: 'var(--dim)'};
let DLSTATE = {scenario: 'subagents', policy: 'least-severe'};

function dlControls(d) {
  const box = $('dlctl');
  let h = '<span class="note">scenario</span>';
  for (const [k, blurb] of Object.entries(d.scenarios)) {
    h += `<button class="btn dlsc${k === d.scenario ? ' on' : ''}" data-sc="${esc(k)}"
            title="${esc(blurb)}"
            style="${k === d.scenario ? 'color:var(--ink);border-color:var(--accent)' : ''}">
            ${esc(k)}</button>`;
  }
  h += '<span class="note" style="margin-left:10px">victim policy</span>';
  d.policies.forEach(p => {
    h += `<button class="btn dlpol" data-pol="${esc(p)}"
            style="${p === d.policy ? 'color:var(--ink);border-color:var(--accent)' : ''}">
            ${esc(p)}</button>`;
  });
  box.innerHTML = h;
  box.querySelectorAll('.dlsc').forEach(b =>
    b.onclick = () => { DLSTATE.scenario = b.dataset.sc; loadDeadlock(); });
  box.querySelectorAll('.dlpol').forEach(b =>
    b.onclick = () => { DLSTATE.policy = b.dataset.pol; loadDeadlock(); });
}

function drawWFG(d) {
  const svg = $('wfg');
  svg.innerHTML = '';
  const evs = d.events;
  const waits = evs.filter(e => e.kind === 'wait');
  const dl = evs.find(e => e.kind === 'deadlock');
  const victim = evs.find(e => e.kind === 'victim');
  const unres = evs.find(e => e.kind === 'unresolvable');
  const doomed = new Set(evs.filter(e => e.kind === 'doomed').map(e => e.tx));
  const cycle = new Set(dl ? dl.cycle : []);

  const txs = d.txs;
  const cx = 260, cy = 150, R = 105;
  const pos = {};
  txs.forEach((t, i) => {
    const a = -Math.PI / 2 + (2 * Math.PI * i) / txs.length;
    pos[t.tx_id] = {x: cx + R * Math.cos(a), y: cy + R * Math.sin(a)};
  });

  // edges first, so nodes sit on top
  waits.forEach(w => {
    const a = pos[w.tx], b = pos[w.holder];
    if (!a || !b) return;
    const inCycle = cycle.has(w.tx) && cycle.has(w.holder);
    const dx = b.x - a.x, dy = b.y - a.y;
    const len = Math.hypot(dx, dy) || 1;
    const ux = dx / len, uy = dy / len;
    const r = 30;
    const x1 = a.x + ux * r, y1 = a.y + uy * r;
    const x2 = b.x - ux * r, y2 = b.y - uy * r;
    // bow the line so A->B and B->A do not overlap
    const mx = (x1 + x2) / 2 - uy * 26, my = (y1 + y2) / 2 + ux * 26;
    svg.appendChild(el('path', {
      d: `M${x1} ${y1} Q${mx} ${my} ${x2} ${y2}`,
      fill: 'none',
      stroke: inCycle ? 'var(--irr)' : (WAITCOL[w.wait_name] || 'var(--dim)'),
      'stroke-width': inCycle ? 2.4 : 1.5,
      'stroke-dasharray': w.wait_name === 'ESCALATE' ? '5 3' : '',
      opacity: inCycle ? 1 : .75,
    }));
    // arrowhead
    const ang = Math.atan2(y2 - my, x2 - mx);
    svg.appendChild(el('path', {
      d: `M${x2} ${y2} L${x2 - 8 * Math.cos(ang - .4)} ${y2 - 8 * Math.sin(ang - .4)} ` +
         `L${x2 - 8 * Math.cos(ang + .4)} ${y2 - 8 * Math.sin(ang + .4)} Z`,
      fill: inCycle ? 'var(--irr)' : (WAITCOL[w.wait_name] || 'var(--dim)'),
      opacity: inCycle ? 1 : .75,
    }));
    svg.appendChild(el('text', {
      x: mx, y: my - 4, 'text-anchor': 'middle',
      fill: inCycle ? 'var(--irr)' : 'var(--dimmer)',
      style: 'font:8.5px var(--mono)',
    }, w.wait_name));
  });

  txs.forEach(t => {
    const p = pos[t.tx_id];
    const isDoomed = doomed.has(t.tx_id);
    const isVictim = victim && victim.tx === t.tx_id;
    const g = el('g');
    g.appendChild(el('circle', {
      cx: p.x, cy: p.y, r: 28,
      fill: isDoomed ? '#1f0f0e' : '#161d27',
      stroke: isVictim ? 'var(--rev)' : isDoomed ? 'var(--irr)'
              : cycle.has(t.tx_id) ? 'var(--irr)' : 'var(--line)',
      'stroke-width': isVictim || isDoomed ? 2.2 : 1.2,
      'stroke-dasharray': isVictim ? '4 2' : '',
    }));
    g.appendChild(el('text', {
      x: p.x, y: p.y - 2, 'text-anchor': 'middle',
      fill: isDoomed ? 'var(--irr)' : 'var(--ink)',
      style: 'font:600 9px var(--mono)',
    }, t.agent.length > 10 ? t.agent.slice(0, 10) : t.agent));
    g.appendChild(el('text', {
      x: p.x, y: p.y + 9, 'text-anchor': 'middle',
      fill: 'var(--dimmer)', style: 'font:8px var(--mono)',
    }, isDoomed ? 'DOOMED' : `tx ${t.tx_id}`));
    if (isDoomed) {
      g.appendChild(el('text', {
        x: p.x, y: p.y + 42, 'text-anchor': 'middle',
        fill: 'var(--irr)', style: 'font:8px var(--mono)',
      }, 'not abortable'));
    }
    if (isVictim) {
      g.appendChild(el('text', {
        x: p.x, y: p.y + 42, 'text-anchor': 'middle',
        fill: 'var(--rev)', style: 'font:8px var(--mono)',
      }, 'victim → abort'));
    }
    svg.appendChild(g);
  });

  if (dl) {
    svg.appendChild(el('text', {
      x: 260, y: 300, 'text-anchor': 'middle',
      fill: unres ? 'var(--irr)' : 'var(--comp)',
      style: 'font:600 11px var(--mono)',
    }, unres ? 'DEADLOCK — UNRESOLVABLE' : 'DEADLOCK — resolved by abort'));
  }
}

function dlEventRows(d) {
  const tb = $('dlevents');
  tb.innerHTML = '';
  d.events.forEach(e => {
    let txt = '', col = 'var(--dim)';
    switch (e.kind) {
      case 'begin':  txt = `tx ${e.tx} BEGIN ${e.agent}` + (e.parent ? ` (subagent of ${e.parent})` : ''); break;
      case 'write':  txt = `tx ${e.tx} write ${e.path}`; break;
      case 'effect': txt = `tx ${e.tx} ${e.klass_name} — ${e.what}`;
                     col = {reversible:'var(--rev)',deferrable:'var(--def)',
                            compensable:'var(--comp)',irrevocable:'var(--irr)'}[e.klass_name]; break;
      case 'doomed': txt = `tx ${e.tx} DOOMED — ${e.what} (can no longer be aborted)`;
                     col = 'var(--irr)'; break;
      case 'wait':   txt = `tx ${e.tx} WAIT → tx ${e.holder} [${e.wait_name}] ${e.why}`;
                     col = 'var(--comp)'; break;
      case 'deadlock': txt = `DEADLOCK cycle ${e.agents.join(' → ')} → ${e.agents[0]} · ${e.n_doomed}/${e.cycle.length} doomed`;
                     col = 'var(--irr)'; break;
      case 'victim': txt = `victim: ${e.agent} (policy=${e.policy}, cost=${e.cost})` + (e.forced ? ' — FORCED, only one abortable' : '');
                     col = e.forced ? 'var(--comp)' : 'var(--rev)'; break;
      case 'unresolvable': txt = `UNRESOLVABLE — ${e.reason}`; col = 'var(--irr)'; break;
      case 'stranded': txt = `stranded: ${e.waiter_agents.join(', ')} — ${e.policy}`;
                     col = 'var(--comp)'; break;
      case 'abort':  txt = `tx ${e.tx} ABORT ${e.agent} — released ${e.released.join(', ') || 'nothing'}`;
                     col = 'var(--rev)'; break;
      default:       txt = JSON.stringify(e);
    }
    const tr = document.createElement('tr');
    tr.innerHTML = `<td style="color:${col};white-space:normal">${esc(txt)}</td>`;
    tb.appendChild(tr);
  });
}

function dlVerdict(d) {
  const unres = d.events.find(e => e.kind === 'unresolvable');
  const victim = d.events.find(e => e.kind === 'victim');
  let h = `<p class="note">${esc(d.teaches)}</p>`;
  if (unres) {
    h = `<p class="note bad"><b>Irrevocability turned a resolvable deadlock into an
      unresolvable one.</b> Every transaction in the cycle emitted an irrevocable
      effect, so every one is DOOMED, and <code>DOOMED → ABORTING</code> does not
      exist in <code>include/agenttx.h</code> — it is unrepresentable, not merely
      refused. Abort is the preemption primitive that breaks every other cycle, and
      here there is nothing to preempt. The kernel must report this rather than
      hang.</p>` + h;
  } else if (victim && victim.forced) {
    h = `<p class="note warn"><b>Victim choice was forced, not chosen.</b> Only one
      member of the cycle was abortable, so every policy picks the same transaction.
      “Abort the cheapest” silently degrades to “abort the only one”, and the cost of
      recovery rises without anything appearing to fail.</p>` + h;
  }
  h += `<p class="note" style="color:var(--dimmer)">Simulated semantics from
    <code>tools/harness/deadlock.py</code>. Nothing in <code>src/</code> implements a
    wait-for graph yet — <code>docs/deadlock.md</code> proposes fragments P1-15…P1-18,
    P2-12. Every event carries <code>sim:true</code>.</p>`;
  $('dlverdict').innerHTML = h;
}

async function loadDeadlock() {
  try {
    const r = await fetch(`/api/deadlock?scenario=${DLSTATE.scenario}&policy=${DLSTATE.policy}`);
    const d = await r.json();
    if (d.error) { $('dlverdict').innerHTML = `<p class="note bad">${esc(d.error)}</p>`; return; }
    dlControls(d); drawWFG(d); dlEventRows(d); dlVerdict(d);
  } catch (e) {
    $('dlverdict').innerHTML = `<p class="note bad">deadlock panel: ${esc(e)}</p>`;
  }
}

loadDeadlock();

/* ============================================================
 * System strip — live state from the guest
 * ==========================================================*/
function cell(k, v, n, colour) {
  return `<div class="cell"><div class="k">${esc(k)}</div>
    <div class="v" style="color:${colour || 'var(--ink)'}">${v}</div>
    <div class="n">${esc(n || '')}</div></div>`;
}

async function refreshSystem() {
  let d;
  try { d = await (await fetch('/api/live')).json(); }
  catch (e) { d = {reachable: false, error: String(e)}; }
  LIVE = d;

  if (!d.reachable) {
    $('sysstrip').innerHTML =
      `<div class="missing">no link to the guest — ${esc(d.error || 'unreachable')}<br>
       <span style="color:var(--dimmer)">boot it with
       <code>SERIAL_LOG=/tmp/c.log make vm-boot</code></span></div>`;
    return;
  }
  const m = d.module || {};
  const st = d.stat || {};
  const inTx = st.tx_id && st.tx_id !== 0;
  const fsReal = d.fs_is_stub === false;

  $('sysstrip').innerHTML = '<div class="strip">' +
    cell('guest', 'reachable', 'ssh :2222', 'var(--rev)') +
    cell('module', m.loaded ? 'loaded' : 'not loaded',
         m.dev ? '/dev/agenttx present' : 'no device node',
         m.loaded ? 'var(--rev)' : 'var(--irr)') +
    cell('P2 provider', fsReal ? 'real' : 'stub',
         fsReal ? 'src/fs/ is linked' : 'src/stub/tx_fs_stub.c',
         fsReal ? 'var(--rev)' : 'var(--comp)') +
    cell('transaction', inTx ? `tx ${st.tx_id}` : 'none',
         inTx ? st.state_name : 'no live transaction',
         inTx ? 'var(--accent)' : 'var(--dimmer)') +
    cell('CoW areas', String((d.txdirs || []).length),
         'under /var/lib/agenttx') +
    cell('overlay mounts', String(d.overlays || 0),
         'in the guest root ns',
         d.overlays ? 'var(--def)' : 'var(--dimmer)') +
    cell('BPF LSM', (d.bpf && d.bpf.lsm) ? 'active' : 'not active',
         (d.bpf && d.bpf.modbtf) ? 'module BTF present — kfuncs resolvable'
                                 : 'no module BTF — kfuncs unresolvable',
         (d.bpf && d.bpf.lsm && d.bpf.modbtf) ? 'var(--rev)' : 'var(--irr)') +
    cell('hooks', (d.bpf && d.bpf.running) ? 'streaming' :
                  (d.bpf && d.bpf.loader) ? 'built, detached' : 'not built',
         (d.bpf && d.bpf.running) ? 'txload is draining the WAL'
                                  : 'run src/bpf/txload in the guest',
         (d.bpf && d.bpf.running) ? 'var(--def)' : 'var(--dimmer)') +
    cell('KASAN / lockdep', d.health ? `${d.health} line(s)` : 'clean',
         d.health ? 'BUG/WARNING in dmesg' : 'no BUG, no WARNING',
         d.health ? 'var(--irr)' : 'var(--rev)') +
    '</div>';

  if (d.dmesg && d.dmesg.length) {
    const tb = $('kmsg');
    if (tb && tb.childElementCount === 0) {
      $('kmsgempty').style.display = 'none';
      d.dmesg.slice(-40).reverse().forEach(line => {
        const tr = document.createElement('tr');
        const bad = /LEAK|refused|PARTIAL|FAILED|BUG|WARNING/.test(line);
        const tx  = /tx=\d+/.test(line);
        tr.innerHTML = `<td style="white-space:normal;color:${
          bad ? 'var(--irr)' : tx ? 'var(--def)' : 'var(--dim)'}">${esc(line)}</td>`;
        tb.appendChild(tr);
      });
    }
  }
}
let LIVE = null;

/* ============================================================
 * Copy-on-write, watched happening
 * ==========================================================*/
function layerList(title, sub, items, render) {
  let h = `<div class="layer"><h4>${esc(title)}<small>${sub}</small></h4><ul>`;
  if (!items.length) h += `<li style="color:var(--dimmer)">— empty —</li>`;
  items.forEach(it => h += render(it));
  return h + '</ul></div>';
}

function renderCow(d) {
  if (d.error) {
    $('cowbody').innerHTML = `<div class="missing">${esc(d.error)}</div>`;
    return;
  }
  const beforeSet = new Set(d.before.map(x => x.path));
  const afterSet  = new Set(d.after.map(x => x.path));
  const agentSet  = new Set(d.agent_view.map(x => x.path));
  const aborted   = /aborted tx=/.test(d.log.join('\n'));

  // column 1 — the lower layer as it was
  const c1 = layerList('lower layer', 'on disk, before the transaction',
    d.before, it => `<li><span class="badge b-dir">${
      it.path.includes('/') ? 'sub' : 'disk'}</span>${esc(it.path)}</li>`);

  // column 2 — what the agent sees through the overlay
  const c2 = layerList("agent's view", 'through the overlay mount',
    d.agent_view, it => {
      const isNew = !beforeSet.has(it.path);
      return `<li><span class="badge ${isNew ? 'b-new' : 'b-dir'}">${
        isNew ? 'new' : 'was'}</span>${esc(it.path)}</li>`;
    });

  // column 3 — the CoW diff overlayfs actually recorded
  const c3 = layerList('upper layer', 'what copy-on-write recorded',
    d.upper, it => {
      let cls = 'b-dir', tag = it.kind;
      if (it.kind === 'whiteout') { cls = 'b-wh'; tag = 'whiteout'; }
      else if (it.kind === 'dir') { cls = 'b-dir'; tag = 'dir'; }
      else if (!beforeSet.has(it.path)) { cls = 'b-new'; tag = 'new'; }
      else { cls = 'b-mod'; tag = 'copy-up'; }
      return `<li><span class="badge ${cls}">${tag}</span>${esc(it.path)}</li>`;
    });

  let h = `<div class="cow">${c1}${c2}${c3}</div>`;

  h += `<div class="verdictbar ${aborted ? 'vb-abort' : 'vb-commit'}">
    <b>${aborted ? 'ABORTED' : 'COMMITTED'}</b> — ${esc(d.title)}.
    ${aborted
      ? 'The upper layer was discarded. The lower layer was never written to, so there is nothing to undo — that is why abort is cheap enough to also be the deadlock recovery primitive.'
      : 'The upper layer was merged down: files renamed into the lower layer, whiteouts applied as unlinks, then the CoW area drained.'}
  </div>`;

  // per-file outcome
  h += `<div style="margin-top:6px">`;
  Object.entries(d.content).forEach(([f, v]) => {
    const gone = v === '<absent>';
    const wasThere = beforeSet.has(f);
    let verdict, colour;
    if (aborted) {
      const same = wasThere !== gone;
      verdict = gone ? 'never existed' : 'restored';
      colour = same ? 'var(--rev)' : 'var(--irr)';
    } else {
      verdict = gone ? 'deleted' : 'landed';
      colour = 'var(--def)';
    }
    h += `<div class="filerow">
      <span class="p">${esc(f)}</span>
      <span style="color:${gone ? 'var(--dimmer)' : 'var(--ink)'}">${
        gone ? '—' : esc(v)}</span>
      <span style="color:${colour}">${verdict}</span>
    </div>`;
  });
  h += `</div>`;

  if (d.kmsg && d.kmsg.length) {
    h += `<div class="note" style="margin-top:12px;color:var(--dimmer)">what the kernel logged</div>
      <div class="scroll" style="max-height:150px;margin-top:5px"><table><tbody>`;
    d.kmsg.slice(-10).forEach(l => {
      const em = /COMMIT|ABORT|merged|discarded/.test(l);
      h += `<tr><td style="white-space:normal;color:${em ? 'var(--def)' : 'var(--dim)'}">${esc(l)}</td></tr>`;
    });
    h += `</tbody></table></div>`;
  }
  $('cowbody').innerHTML = h;
}

async function runDemo(name) {
  $('demoStatus').textContent = 'running in the guest…';
  $('cowbody').innerHTML = '<div class="missing">driving the real module…</div>';
  try {
    const d = await (await fetch('/api/demo?name=' + name)).json();
    renderCow(d);
    $('demoStatus').textContent = d.error ? 'failed' : 'done — this ran for real';
  } catch (e) {
    $('cowbody').innerHTML = `<div class="missing">${esc(e)}</div>`;
    $('demoStatus').textContent = 'failed';
  }
}

/* ============================================================
 * Who may commit
 * ==========================================================*/
function renderProcTree() {
  $('proctree').innerHTML = `
    <div class="ptree">
      <div class="pnode"><span class="pbox sup">txctl</span>
        <span class="pmark">supervisor · registered with CAP_SYS_ADMIN</span>
        <span class="pmark yes">may commit ✓</span></div>
      <div class="pnode" style="margin-left:22px"><span class="arrow">└─</span>
        <span class="pbox owner">holder</span>
        <span class="pmark">called tx_begin · OWNS the transaction</span>
        <span class="pmark no">may not commit ✗</span></div>
      <div class="pnode" style="margin-left:44px"><span class="arrow">└─</span>
        <span class="pbox inside">agent</span>
        <span class="pmark">inside by inheritance</span>
        <span class="pmark no">may not commit ✗</span></div>
      <div class="pnode" style="margin-left:66px"><span class="arrow">└─</span>
        <span class="pbox inside">subprocess</span>
        <span class="pmark">still inside, any depth</span>
        <span class="pmark no">may not commit ✗</span></div>
    </div>
    <hr class="sep">
    <p class="note">Three processes, not two, and each boundary is load-bearing:</p>
    <p class="note">· If <b>txctl</b> opened the transaction it would be inside its own
      transaction, and the kernel would refuse its commit — correctly.</p>
    <p class="note">· If the <b>agent</b> opened it, the transaction would die when the
      agent exits — and the agent must exit before its exit status can be the
      verification signal. The holder exists only to outlive it.</p>
    <p class="note bad">Authority is a <b>membership</b> question, not a tgid comparison.
      Once membership is inherited, comparing tgids would let the agent
      <code>fork()</code> once and have the child commit — the premature-commit
      attack plus one line. <code>tests/p1/t05</code> asserts the refusal three
      forks deep.</p>`;
}

/* ============================================================
 * Fragment progress
 * ==========================================================*/
async function renderFragments() {
  try {
    const r = await fetch('/api/fragments');
    const d = await r.json();
    let h = '<div class="frag">';
    d.rows.forEach(f => {
      const cls = f.status === 'done' ? 'f-done'
                : f.status === 'proposed' ? 'f-prop' : 'f-todo';
      h += `<i class="${cls}" title="${esc(f.id)} — ${esc(f.title)} [${esc(f.status)}]"></i>`;
    });
    h += '</div>';
    h += `<div class="legend" style="padding:0 13px 12px">
      <span><b style="color:var(--rev)">${d.counts.done || 0}</b> done</span>
      <span><b style="color:var(--accent)">${d.counts.proposed || 0}</b> proposed</span>
      <span><b style="color:var(--dim)">${d.counts.todo || 0}</b> todo</span>
      <span style="color:var(--dimmer)">hover a square for the fragment</span></div>`;
    $('fragments').innerHTML = h;
  } catch (e) {
    $('fragments').innerHTML = `<div class="missing">${esc(e)}</div>`;
  }
}

$('demoAbort').onclick  = () => runDemo('abort');
$('demoCommit').onclick = () => runDemo('commit');
renderProcTree();
renderFragments();
refreshSystem();
setInterval(refreshSystem, 4000);
