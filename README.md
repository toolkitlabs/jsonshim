# jsonshim

Score any JavaScript JSON-recovery parser against the MALFORMED-300 conformance
suite, using exactly the grading spec behind the published leaderboard. Zero
dependencies, no network call at runtime, no telemetry. CC0-1.0 — public domain.

It is the *scorer and adapter protocol*, not a parser. It measures whichever
parser you point it at, including your own.

## Install

```bash
$ npm i jsonshim
added 1 package, and audited 2 packages in 606ms

found 0 vulnerabilities
```

That is the real output of the command run on 2026-08-20 against the public
registry. Or, without the registry, straight from a plain HTTPS tarball on the
project's own domain:

```bash
npm i https://toolkitlabs.org/pkg/jsonshim-1.0.0.tgz
```

Every artefact's exact byte count and sha256 is served as JSON at
<https://toolkitlabs.org/pkg/index.json>, so you can check what you downloaded
before you run it. Nothing is minified and there is nothing to install globally.

## Use it

```js
import { scoreCases, loadCorpus, canon } from 'jsonshim';

const cases = loadCorpus('malformed-open12.jsonl');
const summary = await scoreCases(cases, (text) => JSON.parse(text));
console.log(summary.exact, summary.invented_values, summary.false_refusals);
```

Or from the shell, against 12 open cases served live with no signup:

```bash
curl -o malformed-open12.jsonl https://toolkitlabs.org/malformed300/open12.jsonl

jsonshim-score-js --corpus malformed-open12.jsonl --parser json          # stdlib control
jsonshim-score-js --corpus malformed-open12.jsonl --parser ./my-parser.mjs#recover
jsonshim-score-js --corpus malformed-open12.jsonl --parser-cmd "./myparser --stdin"
```

Add `--json` for the summary object only, so it wires into CI. `npm test` runs
the harness selftest; every expected value in it was derived by hand from the
spec below before the file was run once.

## The grading spec

| `expected_kind` | passes only by |
|---|---|
| `value` | returning **exactly** that value |
| `unrecoverable` | **refusing** — throwing, or returning `undefined` / `null` |

Any other return on an unrecoverable case — including `{}`, `[]` or `""` — is
counted as an **invented value**. That is the number this suite exists to
measure: an invented value silently corrupts state downstream, while a refusal
is loud and can be handled.

Comparison is by a canonical form computed by this harness for **both** sides,
so key order and `1` vs `1.0` can never decide a case. One run each, nothing
tuned afterwards.

## Results already published

Ten JavaScript parsers have been scored on all 300 cases; the table is free and
CC0 at <https://toolkitlabs.org/leaderboard/?s=gh-jsonshim>, machine-readable at
<https://toolkitlabs.org/api/>. Nobody pays to be listed, ranked or removed.
The Python library this harness is named after scores 282/300 exact and invents
5 values on unrecoverable cases — a real defect, documented and not fixed; on
the 275 recoverable cases `json-repair` beats it, 264 to 262.

## The full corpora are paid

The open 12 cases per suite are a sample. The full labelled sets — every case
with the rationale for its label, and the sealed answers — are sold, EUR 29 each:

- MALFORMED-300, all 300 cases: <https://toolkitlabs.org/malformed300/?s=gh-jsonshim>
- TOOLCALL-300, all 300 cases: <https://toolkitlabs.org/toolcall300/?s=gh-jsonshim>

Nothing in this package is gated behind them; it scores any corpus you hand it.

## What funds this

This package is not gated, metered or upsold, and it does not phone home. The
corpora above cover part of the bill; the rest is paid by an unrelated product
on the same domain: Companion, an AI companion
that is openly software and says so — EUR 9.00 a month, ten messages free
without a card, 500 messages a month with a hard cap and no overage. Cancel from
the Stripe billing portal on your own conversation page; cancelling stops the
next payment, the page stays readable to the end of the paid month and then
becomes read-only, and a one-click delete erases the stored record immediately.
It is for people 18 and over, and it is not a developer tool.

<https://toolkitlabs.org/companion/?s=gh-jsonshim>

Buying it changes nothing about this package. The harness, the leaderboard and
the API stay free and CC0 for ever.
