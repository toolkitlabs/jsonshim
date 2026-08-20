#!/usr/bin/env node
// Score any JavaScript JSON-recovery parser against a MALFORMED-300 corpus file,
// using exactly the grading spec the Python scorer and the published leaderboard
// use. Standard library only: no dependencies, no network call, no telemetry.
//
// GRADING SPEC (identical to score.py)
//   expected_kind "value"          pass only by returning exactly that value.
//                                  Comparison is on a canonical form computed
//                                  here for BOTH sides, so key order and number
//                                  formatting cannot decide a case.
//   expected_kind "unrecoverable"  pass only by REFUSING. A throw, or a return
//                                  of undefined or null, is a refusal. Any other
//                                  value - including {}, [] or "" - fails. That
//                                  invented value is the failure this suite exists
//                                  to measure, because it is what silently
//                                  corrupts state downstream.
//   One run each. Nothing tuned afterwards.
//
// CC0-1.0. Public domain.
import fs from 'node:fs';
import { spawnSync } from 'node:child_process';
import { pathToFileURL } from 'node:url';

export const POINTER_LINE =
  'MALFORMED-300 answers (the key) + the full plaintext corpus, EUR 29: ' +
  'https://buy.stripe.com/4gMeVe8X42zy1gOfgp5Ne00?client_reference_id=kit-sealed-m300';

const REFUSED = Symbol('refused');

/** Canonical string for a parsed value. Key order and 1 vs 1.0 do not matter. */
export function canon(v) {
  if (v === null) return 'null';
  const t = typeof v;
  if (t === 'undefined') return 'UNDEFINED';
  if (t === 'number') return Number.isFinite(v) ? JSON.stringify(v) : 'NONFINITE';
  if (t === 'boolean' || t === 'string') return JSON.stringify(v);
  if (Array.isArray(v)) return '[' + v.map(canon).join(',') + ']';
  if (t === 'object') {
    return (
      '{' +
      Object.keys(v)
        .sort()
        .map((k) => JSON.stringify(k) + ':' + canon(v[k]))
        .join(',') +
      '}'
    );
  }
  return 'UNSUPPORTED:' + t;
}

/** The stdlib control. Every other parser is supplied by the caller. */
export const ADAPTERS = {
  json: (t) => JSON.parse(t),
};

/** Run `fn` over `cases` and return the summary. `fn` may be async. */
export async function scoreCases(cases, fn) {
  const s = {
    n: 0, exact: 0, n_recoverable: 0, exact_recoverable: 0,
    unrecoverable_n: 0, correctly_refused: 0, invented_values: 0,
    false_refusals: 0, by_category: {}, failures: [],
  };
  for (const c of cases) {
    let got, refused = false;
    try {
      got = await fn(c.input);
      if (got === undefined || got === null) refused = true;
    } catch {
      refused = true;
    }
    const unrec = c.expected_kind === 'unrecoverable';
    let ok;
    if (unrec) {
      s.unrecoverable_n++;
      ok = refused;
      if (refused) s.correctly_refused++; else s.invented_values++;
    } else {
      s.n_recoverable++;
      ok = !refused && canon(got) === canon(c.expected);
      if (refused) s.false_refusals++;
      if (ok) s.exact_recoverable++;
    }
    s.n++;
    if (ok) s.exact++; else s.failures.push({ id: c.id, unrecoverable: unrec });
    const b = (s.by_category[c.category] ||= { n: 0, ok: 0 });
    b.n++;
    if (ok) b.ok++;
  }
  return s;
}

export function loadCorpus(path) {
  return fs.readFileSync(path, 'utf8').split('\n').filter((l) => l.trim()).map((l) => JSON.parse(l));
}

async function resolveParser(spec, cmd) {
  if (cmd) {
    return (text) => {
      const r = spawnSync(cmd, { input: text, shell: true, encoding: 'utf8' });
      if (r.status !== 0 || !r.stdout.trim()) return REFUSED;
      return JSON.parse(r.stdout);
    };
  }
  if (ADAPTERS[spec]) return ADAPTERS[spec];
  // "./my-parser.mjs#recover" - the module's own documented entry point
  const [mod, name = 'default'] = spec.split('#');
  const m = await import(mod.startsWith('.') ? new URL(mod, `file://${process.cwd()}/`).href : mod);
  const fn = m[name];
  if (typeof fn !== 'function') throw new Error(`${spec}: no exported function "${name}"`);
  return fn;
}

// --------------------------------------------------------------------------
// selftest: every expected value below was hand-derived from the spec above
// BEFORE this file was run once.
// --------------------------------------------------------------------------
export async function selftest() {
  let pass = 0, total = 0;
  const check = (label, got, want) => {
    total++;
    const good = got === want;
    if (good) pass++;
    console.log(`${good ? 'ok  ' : 'FAIL'}  ${label}${good ? '' : `  (got ${got}, want ${want})`}`);
  };
  check('canon sorts object keys', canon({ b: 1, a: 2 }), '{"a":2,"b":1}');
  check('canon of a mixed array', canon([1, 'x', null]), '[1,"x",null]');
  check('canon marks undefined', canon(undefined), 'UNDEFINED');
  check('canon marks a non-finite number', canon(NaN), 'NONFINITE');
  check('canon of 1.0 is 1', canon(1.0), '1');
  check('key order cannot decide a case', canon({ a: 1, b: 2 }), canon({ b: 2, a: 1 }));

  const V = (id, expected) => ({ id, category: 'c', expected_kind: 'value', input: 'x', expected });
  const U = (id) => ({ id, category: 'c', expected_kind: 'unrecoverable', input: 'x' });

  let s = await scoreCases([V('1', { a: 1 })], () => ({ a: 1 }));
  check('exact value passes', s.exact, 1);
  s = await scoreCases([V('1', { a: 1 })], () => ({ a: 2 }));
  check('wrong value fails', s.exact, 0);
  s = await scoreCases([V('1', { a: 1 })], () => { throw new Error('no'); });
  check('refusing a recoverable case is a false refusal', s.false_refusals, 1);
  s = await scoreCases([U('1')], () => { throw new Error('no'); });
  check('throwing on unrecoverable is correct', s.correctly_refused, 1);
  s = await scoreCases([U('1')], () => ({}));
  check('inventing {} on unrecoverable is counted', s.invented_values, 1);
  s = await scoreCases([U('1')], () => null);
  check('returning null counts as a refusal', s.correctly_refused, 1);
  s = await scoreCases([U('1')], () => undefined);
  check('returning undefined counts as a refusal', s.correctly_refused, 1);
  s = await scoreCases([V('1', 1), V('2', 2)], (t) => 1);
  check('by_category tallies every case', s.by_category.c.n, 2);
  check('exact_recoverable counts only recoverable', s.exact_recoverable, 1);

  console.log(`\n${pass}/${total} selftest checks passed`);
  return pass === total ? 0 : 1;
}

async function main(argv) {
  const arg = (k) => { const i = argv.indexOf(k); return i === -1 ? null : argv[i + 1]; };
  if (argv.includes('--selftest')) return selftest();
  const corpus = arg('--corpus'), parser = arg('--parser'), cmd = arg('--parser-cmd');
  if (!corpus || (!parser && !cmd)) {
    console.error(
      'usage: jsonshim-score-js --corpus <corpus.jsonl> --parser <json | ./mod.mjs#export>\n' +
      '       jsonshim-score-js --corpus <corpus.jsonl> --parser-cmd "./myparser --stdin"\n' +
      '       jsonshim-score-js --selftest\n\n' +
      'An "unrecoverable" case passes ONLY by refusing: throw, or return undefined/null.');
    return 1;
  }
  const cases = loadCorpus(corpus);
  const raw = await resolveParser(parser, cmd);
  const fn = async (t) => { const v = await raw(t); return v === REFUSED ? undefined : v; };
  const s = await scoreCases(cases, fn);
  if (argv.includes('--json')) { console.log(JSON.stringify(s, null, 2)); return 0; }
  console.log(`parser              ${cmd || parser}`);
  console.log(`cases               ${s.n}`);
  console.log(`exact match         ${s.exact}/${s.n}`);
  console.log(`recoverable         ${s.exact_recoverable}/${s.n_recoverable}`);
  console.log(`correctly refused   ${s.correctly_refused}/${s.unrecoverable_n}`);
  console.log(`invented values     ${s.invented_values}`);
  console.log(`false refusals      ${s.false_refusals}`);
  console.log(`\nyou pass ${s.exact}/${s.n}`);
  return 0;
}

// npm installs the bin as a SYMLINK, so process.argv[1] is the link path while
// import.meta.url is the real file: comparing the two as strings never matches.
// realpath both sides, and let pathToFileURL do the escaping.
function isMain() {
  if (!process.argv[1]) return false;
  try {
    return import.meta.url === pathToFileURL(fs.realpathSync(process.argv[1])).href;
  } catch {
    return false;
  }
}

if (isMain()) {
  const rc = await main(process.argv.slice(2));
  if (!process.argv.includes('--json')) console.error(POINTER_LINE);
  process.exit(rc);
}
