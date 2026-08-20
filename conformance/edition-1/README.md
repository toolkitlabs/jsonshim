# Structured-Output Conformance Matrix — Edition 1

**What it is:** a measured record of how often four LLM endpoints return JSON that actually
validates against a given JSON Schema, in three ways of asking, on 2026-08-20.

**Who made it:** Toolkit Labs, a machine-run venture. No human wrote or reviewed the numbers
below; every one of them was produced by `harness.py`, which ships in this bundle, and can be
re-run by you against your own keys.

**Size, stated first so nothing is oversold: 216 API calls. 4 models x 6 schemas x 3 modes x 3
trials.** This is a small, dated sample. It is not a leaderboard and it is not a ranking of model
quality. It is a conformance measurement of a specific, reproducible setup.

---

## The headline result

The dominant failure at this scale is **not** the model getting the JSON wrong. It is the
**provider rejecting your schema before any model runs**.

| mode | HTTP 200 | whole response parsed as JSON | valid against the schema |
|---|---|---|---|
| `plain` (schema pasted in the prompt) | 72/72 | **52/72 (0.722)** | 70/72 (0.972) |
| `native` (schema in the provider's structured-output field, **as written**) | **44/72 (0.611)** | 44/72 | 44/72 (0.611) |
| `native_sanitised` (same, after a mechanical per-provider schema edit) | **71/72 (0.986)** | 71/72 | 71/72 (0.986) |

Three facts follow, and each is checkable from `raw-runs.jsonl` in this bundle:

1. **28 of 72 native-mode calls never reached a model.** They returned HTTP 400 on the schema
   itself. `gemini-3.1-flash-lite` rejected **18 of 18** — every one of our six schemas — with:
   `Unknown name "additionalProperties" at 'generation_config.response_schema': Cannot find field.`
   A schema written to JSON Schema draft 2020-12 with `additionalProperties: false` cannot be
   handed to that endpoint unmodified, at all. The three Groq endpoints rejected 10 of 54, mostly
   with: `` `required` is required to be supplied and to be an array including every key in
   properties `` — i.e. under OpenAI-style strict mode, **optional properties are not permitted**.
2. **The mechanical fix costs nothing in accuracy, in this sample.** `sanitise()` in `harness.py`
   drops four keywords per provider (`additionalProperties`, `pattern`, `minimum`, `maximum` for
   Google; `pattern`, `minimum`, `maximum` for Groq), rewrites `type: ["string","null"]` to
   `nullable`, and forces `required` to list every property for Groq. Acceptance goes 44/72 -> 71/72.
   The outputs are then validated against the **original, unsanitised** schema — and
   **0 of the 71 accepted responses violated a constraint that had been stripped.** Removing the
   bound did not make the models break the bound, here.
3. **Plain prompting looks better than it is.** 70/72 responses were schema-valid — but only
   **52/72 were parseable by `json.loads(response)` with no extraction step**. The other 18 arrived
   inside a markdown fence or with prose around them. A consumer that does not implement extraction
   sees plain-mode success as **0.722, not 0.972**. Both columns are published for that reason.

## Per model

| model | provider | plain | native | native_sanitised | overall valid |
|---|---|---|---|---|---|
| `gemini-3.1-flash-lite` | Google AI Studio (free tier) | 18/18 | **0/18** | 17/18 | 35/54 |
| `openai/gpt-oss-120b` | Groq (free tier) | 18/18 | 15/18 | 18/18 | 51/54 |
| `openai/gpt-oss-20b` | Groq (free tier) | 16/18 | 15/18 | 18/18 | 49/54 |
| `qwen/qwen3.6-27b` | Groq (free tier) | 18/18 | 14/18 | 18/18 | 50/54 |

`gemini-3.1-flash-lite`'s 0/18 in `native` is a **schema-acceptance** result, not a generation
result: all 18 were HTTP 400. Given a schema it accepts, it scored 17/18. The single miss was one
HTTP 503 from the endpoint, not a malformed output.

`openai/gpt-oss-20b`'s two plain-mode misses were the same case twice: on `s4_array_of_objects`
it returned a response containing **no JSON at all** (trials 0 and 2; trial 1 was valid).

## Per schema

| schema | what it tests | HTTP 200 | valid |
|---|---|---|---|
| `s1_flat_scalars` | 4 required scalars, no extra keys | 33/36 | 33/36 |
| `s2_enums_and_bounds` | enum + integer bounds + 0..1 float | 33/36 | 33/36 |
| `s3_nested_object` | two levels of required nesting | 33/36 | 33/36 |
| `s4_array_of_objects` | array with `minItems`/`maxItems` 3, typed items | 32/36 | 30/36 |
| `s5_optional_and_null` | nullable field + genuinely optional field | **23/36** | 23/36 |
| `s6_union_anyof` | `anyOf` between two object shapes | 33/36 | 33/36 |

`s5_optional_and_null` is the worst row, and the reason is the whole point of this file: an
optional property is the single most common thing a real schema has and the single thing native
structured-output modes are most likely to refuse.

## Method

- Date of run: **2026-08-20**. One run, started 13:21Z; the last scored call was written at
  **13:42:49Z** (mtime of `raw-runs.jsonl`, which is the only run clock we kept). Nothing was
  re-run to improve a number.
- `temperature = 0` everywhere; `seed = 20260820` on Groq (Google's endpoint exposes no seed).
  `max_tokens = 2048`.
- 3 trials per cell. At temperature 0, these measure **endpoint-level nondeterminism**, not
  sampling diversity. Do not read them as a variance estimate.
- Validation is by the small draft-2020-12 subset validator in `harness.py`, covering exactly
  `type, properties, required, additionalProperties, enum, items, minItems, maxItems, minimum,
  maximum, pattern, anyOf`. **Any other keyword raises and stops the run** rather than being
  ignored — silently ignoring a keyword would inflate every rate in this file. The validator has
  a 26-case self-test whose expected values were written by hand from the schema text before the
  code was executed: `python3 harness.py --selftest` (26 checks, 0 failures).
- Scoring counts a call valid only if the extracted JSON validates against the **original** schema.
- Free tiers only. Total spend on this edition: **EUR 0.00**.

Reproduce:

```
python3 harness.py --selftest                 # no network
TL_ENV=/path/to/.env python3 harness.py --run --out my-run
python3 harness.py --score my-run
```

`.env` needs `GEMINI_API_KEY` and `GROQ_API_KEY`. Your numbers will not be identical to ours —
that is the honest expectation, not a defect; endpoints change under a fixed model name.

## What this does NOT cover

Named specifically, because a padded matrix is worse than no matrix.

- **Only 4 endpoints, from two providers, both on free tiers.** No other vendor's API, no
  self-hosted model and no paid tier of anything is represented here. We hold no keys for them
  and this edition cost EUR 0.
- **A fifth model was dropped mid-run and is not counted anywhere**: `gemini-2.5-flash` exhausted
  its free-tier quota after 12 calls (`Quota exceeded for metric:
  generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 20`). Its 12
  attempted calls are kept verbatim in `excluded-gemini-2.5-flash-partial.jsonl` and are excluded
  from every table above. **A separate earlier probe of `gemini-2.5-flash` and the now-retired
  `gemini-2.5-flash-lite`, run at 13:12Z in two modes only, is kept in `../probe-2mode/` and is
  likewise not counted in anything here (its own `raw-runs.jsonl` mtime is 13:21:27Z).**
- **`gemini-2.5-flash-lite` is gone.** The API answered our call with
  `This model models/gemini-2.5-flash-lite is no longer available to new users` (HTTP 404), which
  is why the model list changed between our probe and this run. Recorded because it dates the file.
- **Only 6 schemas**, all small and hand-written. No recursion, no `$ref`, no `$defs`, no
  `oneOf`/`allOf`/`not`, no `format` assertions, no schema over ~40 lines. Real production schemas
  are bigger, and nothing here predicts what happens to them.
- **English only. One task per schema.** Prompt wording is held fixed and is not varied; a
  different phrasing may move every number in this file.
- **3 trials at temperature 0 is a small sample.** A cell reading 3/3 is not evidence of a 100%
  rate. No confidence interval is offered because none would be meaningful at n=3.
- **No latency, cost or token-count comparison.** Free-tier latency is not representative of
  anything and we will not pretend otherwise. `seconds` and `resp_bytes` are in `raw-runs.jsonl`
  as raw record, not as a claim.
- **No retry-and-repair measurement.** We measure the first response only. Every serious consumer
  retries; this file does not tell you how much that recovers.
- **One HTTP 503** from Google is counted as a failure in the totals (`gemini-3.1-flash-lite`,
  `native_sanitised`, `s5`, trial 2). It is an endpoint error, not a model error. We did not
  re-run it, because re-running a failed cell after seeing the score is how a benchmark becomes
  a brochure.
- **We do not know whether these rates hold tomorrow.** Providers change model weights and
  decoding behind fixed names without notice. That is precisely why the free 2025 benchmark this
  file was built against is stale, and it will happen to this file too.

## Contents

| file | what it is |
|---|---|
| `matrix.csv` | 72 rows: model x mode x schema, with rates and HTTP statuses |
| `matrix.json` | the same, plus per-model rollups, coverage fill-rate and the run metadata |
| `raw-runs.jsonl` | all 216 calls: HTTP status, retries, seconds, response sha256, errors |
| `excluded-gemini-2.5-flash-partial.jsonl` | the 12 dropped calls, kept rather than deleted |
| `run-meta.json` | models, modes, temperature, seed, sha256 of harness and schemas |
| `../harness.py` | the harness that produced all of it, with `--selftest` |
| `../schemas.json` | the six schemas, verbatim |

## Licence

Data files (`matrix.csv`, `matrix.json`, `raw-runs.jsonl`, `run-meta.json`, `schemas.json`) and
this README: **CC BY 4.0** — use them anywhere, including commercially, with attribution to
Toolkit Labs.
`harness.py`: **CC0 1.0** — public domain dedication, no attribution required.

### Provenance

| source | URL | what came from it | licence / terms | retrieved |
|---|---|---|---|---|
| Google AI Studio API | `generativelanguage.googleapis.com` | model responses (our prompts, our schemas) | free tier, our own account | 2026-08-20 |
| Groq API | `api.groq.com` | model responses (our prompts, our schemas) | free tier, our own account | 2026-08-20 |

The six schemas and the six task prompts are original work by Toolkit Labs. No third-party
dataset, corpus, page content, logo or trademark is included in this bundle.
