# toolkitlabs-jsonshim

Pull the JSON out of a model reply, report every repair, and return **nothing**
when the value cannot be recovered. One file, standard library only, no network
call and no telemetry. Public domain (CC0-1.0).

## Install

There is no PyPI listing yet, so `pip install toolkitlabs-jsonshim` will not
work. Install it from this repository, from the v1.0.0 release, or from the wheel on
the project's own domain:

```bash
pip install "git+https://github.com/toolkitlabs/jsonshim#subdirectory=python"
pip install https://github.com/toolkitlabs/jsonshim/releases/download/v1.0.0/toolkitlabs_jsonshim-1.0.0-py3-none-any.whl
pip install https://toolkitlabs.org/pkg/toolkitlabs_jsonshim-1.0.0-py3-none-any.whl
```

Every artefact's exact byte count and sha256 is served as JSON at
<https://toolkitlabs.org/pkg/index.json>, so you can check what you downloaded
before you run it. An sdist sits beside the wheel at the same path.

## Use it

```python
from jsonshim import extract

r = extract('here you go: {"a": 1, }')
print(r.ok, r.value, r.repairs)   # True {'a': 1} ['trailing comma removed']

r = extract('sorry, I cannot')
print(r.ok, r.error)              # False no JSON-shaped span found
```

Those two outputs are pasted from the installed package, not from memory.
`r.span` tells you where in the text the value was found. The distribution is
`toolkitlabs-jsonshim`; the import name is `jsonshim`, so every command
published on the leaderboard works unchanged.

## Measured, not claimed

`jsonshim` scored on all 300 cases of MALFORMED-300, one run, nothing tuned
afterwards:

| measure | result |
|---|---|
| exact match | **282/300** (94.0%) |
| unrecoverable cases correctly refused | 20/25 |
| values invented on unrecoverable cases | **5** |
| library selftest | 37/37 |

Reproduce it yourself:

```bash
jsonshim-score --corpus malformed300.jsonl --parser jsonshim:loads
```

corpus sha256 `ded36c275f9ff481a9e264f9e900ccbb16da9cf430ff6fe8ffc23b9c02acb88a`

**The 5 invented values are a real defect and are documented, not fixed.**
On the 275 recoverable cases `json-repair` recovers 264 and `jsonshim` 262 - it
loses that column, and leads overall only by refusing 20 of the 25
unrecoverable cases where `json-repair` refuses 1. The whole table, with every
parser that beats it somewhere, is at <https://toolkitlabs.org/leaderboard/>.

## The two scorers ship with it

Both are the free scorers from the public kits, unmodified.

```bash
# 12 open cases per suite, served live, no signup.
# Both files are named open12.jsonl upstream - rename on the way down.
curl -o malformed-open12.jsonl https://toolkitlabs.org/malformed300/open12.jsonl
jsonshim-score --corpus malformed-open12.jsonl --parser jsonshim:loads

curl -o toolcall-open12.jsonl https://toolkitlabs.org/toolcall300/open12.jsonl
toolcall-score --corpus toolcall-open12.jsonl --adapter naive
```

Score any parser you like - your own, or a competitor's:

```bash
jsonshim-score --corpus malformed-open12.jsonl --parser mymodule:recover
jsonshim-score --corpus malformed-open12.jsonl --parser-cmd "./myparser --stdin"
jsonshim-score --corpus malformed-open12.jsonl --parser jsonshim:loads --baseline base.json
```

Exit code 2 on a regression against a baseline, so it wires straight into CI.
`jsonshim-score --spec` prints the grading rules (`toolcall-score` takes
`--adapter` / `--adapter-cmd` where `jsonshim-score` takes `--parser`). Scorer selftests: 24/24
and 33/33.

## The full corpora are paid

The open 12 per suite are a sample. The full labelled sets - every case with the
rationale for its label, and the sealed answers - are sold, EUR 29 each:

- MALFORMED-300, all 300 cases: <https://buy.stripe.com/4gMeVe8X42zy1gOfgp5Ne00?client_reference_id=ghpy-jsonshim-m300>
- TOOLCALL-300, all 300 cases: <https://buy.stripe.com/14AdRa0qy3DC2kSb095Ne03?client_reference_id=ghpy-jsonshim-tc300>

Nothing in this library is gated behind them.

## What funds this

This library is not gated, metered or upsold, and it does not phone home. The
corpora above cover part of the bill; the rest is paid by an unrelated product
on the same domain: Companion, an AI companion that is openly software and says
so - EUR 9.00 a month, ten messages free without a card, 500 messages a month
with a hard cap and no overage. Cancel from the Stripe billing portal on your
own conversation page; cancelling stops the next payment, the page stays
readable to the end of the paid month and then becomes read-only, and a
one-click delete erases the stored record immediately. It is for people 18 and
over, and it is not a developer tool.

<https://toolkitlabs.org/companion/>

Buying it changes nothing about this library. The library, both scorers and the
leaderboard data stay free and CC0 for ever.
