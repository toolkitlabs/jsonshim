#!/usr/bin/env python3
"""
score.py -- run a tool-call normaliser against the TOOLCALL-300 conformance suite
(or the free 30-case sample) and print what it actually does.

Standard library only. No network. No telemetry. Public domain (CC0).

USAGE
  python3 score.py --corpus sample30.jsonl --adapter naive
  python3 score.py --corpus sample30.jsonl --adapter mymodule:normalise
  python3 score.py --corpus sample30.jsonl --adapter-cmd "./mytool --stdin"
  python3 score.py --corpus sample30.jsonl --adapter naive --baseline base.json
  python3 score.py --selftest
  python3 score.py --spec

ADAPTER PROTOCOL
  --adapter module:callable  the callable takes TWO arguments -- the raw model text
                             (str) and the declared tools (list of tool objects) --
                             and returns {"name": ..., "arguments": {...}}, OR
                             returns None to refuse, OR raises to refuse.
  --adapter naive            the control: json.loads the text and pass it straight
                             through. This is what you get if you trust the model.
  --adapter-cmd "..."        a subprocess: {"text": ..., "tools": [...]} goes in on
                             stdin as one JSON line; the normalised call comes back
                             on stdout as JSON. Non-zero exit or empty stdout is a
                             refusal.

EXIT CODES
  0  scored, and no regression against --baseline (if one was given)
  1  usage or harness error
  2  a regression against --baseline: fewer exact matches, more invented calls on
     cases that had none to make, or any category that fell. Wire this into CI.
"""
import argparse
import hashlib
import importlib
import io
import json
import os
import subprocess
import sys
import time

SPEC = """TOOLCALL-300 grading spec

WHAT IS BEING GRADED
  A normaliser: given the raw text a model produced and the tool schemas that were
  declared to it, return the call that the schema will accept -- {"name", "arguments"}
  -- or refuse. TOOLCALL-300 grades the SCHEMA layer. Getting JSON out of prose,
  fences and trailing commas is a different job (that is MALFORMED-300's); every
  input here that is meant to parse, parses.

1. expected_kind "value": pass only by returning exactly that call. Comparison is on
   json.dumps(v, sort_keys=True, separators=(",",":")). Key order and whitespace do
   not matter; names, types and values do. If the returned object carries extra
   top-level keys (an id, a "type"), only "name" and "arguments" are compared.
2. expected_kind "unrecoverable": pass only by REFUSING. Any returned call fails,
   including {"name": ..., "arguments": {}}. Turning "the model did not produce a
   usable call" into "the model called a tool with no arguments" is the failure this
   suite exists to measure -- a server will execute the second one.
3. Tool identity: namespace prefixes (functions., tools:, a/b), surrounding
   whitespace, trailing "()", letter case, and '-' ' ' '_' as separators are noise.
   A name resolves only if it matches EXACTLY ONE declared tool after that
   normalisation. Otherwise: refuse.
4. Enum members are matched the same way: trim, case-fold, and treat '-' and ' ' as
   '_'. If that matches exactly one member, it is that member. If it matches none:
   refuse. Never the "closest" member.
5. Type coercion is allowed only where it is lossless and reversible: "3" -> 3,
   "12.5" -> 12.5, 3.0 -> 3, "true"/"True" -> true. A value whose JSON literal does
   not read back as exactly one value of the declared type is not coercible: refuse.
6. A required property that is absent is filled from the schema's own "default" and
   from nowhere else. Absent with no default: refuse.
7. Undeclared properties are dropped (the schemas here set additionalProperties
   false). A flattened nested object is re-nested only when each key belongs to
   exactly one declared nested property and to no declared top-level property.
8. array-vs-scalar is repaired only in the one-element direction: a bare value where
   an array is declared is wrapped; a ONE-element array where a scalar is declared is
   unwrapped. Two or more elements into a scalar: refuse.
9. "arguments" delivered as a JSON string is decoded (repeatedly if it was encoded
   more than once). A string that does not decode to an object: refuse.
10. Several calls where one was allowed collapse to one ONLY if every copy
    canonicalises to the same value. Otherwise: refuse. Order of appearance is not
    evidence of intent.
11. Truncation: keep every property that was completely written before the cut, drop
    the incomplete tail, close the open containers, invent nothing. If that leaves a
    required property missing and it has no default: refuse.
12. No case expects null, so returning None is an unambiguous refusal signal.
"""


def canon(v):
    return json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class Refused(Exception):
    pass


# ------------------------------------------------------- sealed-corpus support
KIT_DIR = os.path.dirname(os.path.abspath(__file__))
CORPUS_CANDIDATES = ("toolcall300.jsonl", "cases.sealed.jsonl", "sample30.jsonl")
WRONG_KEY = "wrong key, or this file has been altered"


def expect_digest(case_id, expected):
    """The free-side grading digest: proves a match, carries nothing."""
    return hashlib.sha256((case_id + "\x00" + canon(expected)).encode("utf-8")).hexdigest()


def case_ok(c, got):
    """A repairable case passes on the answer if we have it, on the digest if we do not."""
    if "expected" in c:
        return canon(got) == canon(c["expected"])
    return expect_digest(c["id"], got) == c.get("expect_digest")


def _tokens(v, out):
    if isinstance(v, str):
        out.add(v)
    elif isinstance(v, dict):
        for k, x in v.items():
            out.add(k)
            _tokens(x, out)
    elif isinstance(v, list):
        for x in v:
            _tokens(x, out)
    return out


def invented_tokens(got, haystack):
    """Strings and keys among the ARGUMENTS that occur nowhere in the case's input text
    and nowhere in the declared schemas. The call envelope ("name"/"arguments") is
    structure, not content, so it is not counted. Computable without the answer, which
    is the point: it is the one failure mode a sealed kit can still name precisely."""
    args = got.get("arguments") if isinstance(got, dict) and "arguments" in got else got
    return sorted(t for t in _tokens(args, set()) if t and t not in haystack)


def haystack(c):
    return c["input"] + canon(c.get("tools", []))


def default_corpus():
    for name in CORPUS_CANDIDATES:
        p = os.path.join(KIT_DIR, name)
        if os.path.exists(p):
            return p
    return None


def unseal_corpus(cases, key, kit_path=None):
    """Merge the sealed truth back into the cases, in place. Raises ValueError on a
    wrong key -- never a stack trace at the top level."""
    sys.path.insert(0, KIT_DIR)
    try:
        import seal
    except ImportError:
        raise SystemExit("seal.py is not in this kit, so --key cannot be used")
    kit_path = kit_path or os.path.join(KIT_DIR, "kit.json")
    if not os.path.exists(kit_path):
        raise SystemExit("kit.json is not in this kit, so --key cannot be used")
    with io.open(kit_path, encoding="utf-8") as f:
        kit = json.load(f)
    import base64
    enc_key, mac_key = seal.derive(key, base64.b64decode(kit["kit_salt_b64"]))
    n = 0
    for c in cases:
        if "sealed" not in c:
            continue
        truth = json.loads(seal.unseal(c["sealed"], enc_key, mac_key).decode("utf-8"))
        c.update(truth)
        n += 1
    return n


# ------------------------------------------------- schema subset, for one metric
def validate(v, schema, path="args"):
    errs = []
    t = schema.get("type")
    if t == "object":
        if not isinstance(v, dict):
            return ["%s: expected object" % path]
        props = schema.get("properties", {})
        for k in schema.get("required", []):
            if k not in v:
                errs.append("%s.%s: required property missing" % (path, k))
        if schema.get("additionalProperties") is False:
            for k in v:
                if k not in props:
                    errs.append("%s.%s: undeclared property" % (path, k))
        for k, val in v.items():
            if k in props:
                errs += validate(val, props[k], "%s.%s" % (path, k))
    elif t == "array":
        if not isinstance(v, list):
            return ["%s: expected array" % path]
        if "items" in schema:
            for i, x in enumerate(v):
                errs += validate(x, schema["items"], "%s[%d]" % (path, i))
    elif t == "string":
        if not isinstance(v, str):
            errs.append("%s: expected string" % path)
    elif t == "integer":
        if isinstance(v, bool) or not isinstance(v, int):
            errs.append("%s: expected integer" % path)
    elif t == "number":
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            errs.append("%s: expected number" % path)
    elif t == "boolean":
        if not isinstance(v, bool):
            errs.append("%s: expected boolean" % path)
    if "enum" in schema and v not in schema["enum"]:
        errs.append("%s: %s is not a declared member" % (path, json.dumps(v)))
    return errs


def schema_ok(call, tools):
    """True if the returned call would be accepted by the declared schema."""
    if not isinstance(call, dict):
        return False
    tool = next((t for t in tools if t.get("name") == call.get("name")), None)
    if tool is None:
        return False
    args = call.get("arguments")
    if not isinstance(args, dict):
        return False
    return not validate(args, tool["parameters"])


def reduce_call(v):
    """Extra top-level keys (id, type, index) are ignored; the call is the pair."""
    if isinstance(v, dict) and "name" in v and "arguments" in v:
        return {"name": v["name"], "arguments": v["arguments"]}
    return v


# ---------------------------------------------------------------------- corpus
def load_corpus(path):
    cases = []
    with io.open(path, encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            c = json.loads(line)
            for k in ("id", "category", "tools", "input", "expected_kind"):
                if k not in c:
                    raise SystemExit("corpus line %d is missing %r" % (ln, k))
            if (c["expected_kind"] == "value" and "expected" not in c
                    and "expect_digest" not in c):
                raise SystemExit("corpus line %d claims a value and carries neither an "
                                 "expected call nor an expect_digest" % ln)
            cases.append(c)
    if not cases:
        raise SystemExit("corpus %s is empty" % path)
    return cases


def naive(text, tools):
    """The control. Parse the model's output and pass it on, unchanged."""
    d = json.loads(text)
    if not isinstance(d, dict):
        raise Refused()
    return d


def make_adapter(spec, cmd):
    if cmd:
        def run(text, tools):
            payload = json.dumps({"text": text, "tools": tools}, ensure_ascii=False)
            p = subprocess.run(cmd, shell=True, input=payload.encode("utf-8"),
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            if p.returncode != 0 or not p.stdout.strip():
                raise Refused()
            return json.loads(p.stdout.decode("utf-8"))
        return run
    if spec == "naive":
        return naive
    if ":" not in spec:
        raise SystemExit("--adapter wants module:callable, or the word naive")
    mod, fn = spec.split(":", 1)
    sys.path.insert(0, os.getcwd())
    m = importlib.import_module(mod)
    f = getattr(m, fn)
    if not callable(f):
        raise SystemExit("%s is not callable" % spec)
    return f


# ---------------------------------------------------------------------- scoring
def score(cases, adapter):
    rows, cats = [], {}
    exact = invented = refused_right = refused_wrong = errors = bad_schema = 0
    invented_tok = 0
    t0 = time.time()
    for c in cases:
        got, refused, err = None, False, None
        try:
            got = adapter(c["input"], c["tools"])
            if got is None:
                refused = True
        except Refused:
            refused = True
        except Exception as e:
            refused = True
            err = "%s: %s" % (type(e).__name__, e)
        if not refused:
            got = reduce_call(got)
            if not schema_ok(got, c["tools"]):
                bad_schema += 1
        if c["expected_kind"] == "unrecoverable":
            ok = refused
            if refused:
                refused_right += 1
            else:
                invented += 1
        else:
            ok = (not refused) and case_ok(c, got)
            if refused:
                refused_wrong += 1
        made_up = []
        if not ok and not refused:
            made_up = invented_tokens(got, haystack(c))
            if made_up:
                invented_tok += 1
        if ok:
            exact += 1
        if err:
            errors += 1
        st = cats.setdefault(c["category"], {"n": 0, "ok": 0})
        st["n"] += 1
        st["ok"] += 1 if ok else 0
        rows.append({"id": c["id"], "category": c["category"], "ok": ok,
                     "refused": refused, "error": err, "invented_tokens": made_up,
                     "got": None if refused else canon(got),
                     "expected": ("<refusal>" if c["expected_kind"] == "unrecoverable"
                                  else canon(c["expected"]) if "expected" in c
                                  else "<sealed>")})
    n = len(cases)
    n_unre = sum(1 for c in cases if c["expected_kind"] == "unrecoverable")
    return {
        "cases": n,
        "exact_match": exact,
        "exact_match_rate": round(exact / n, 4),
        "unrecoverable_cases": n_unre,
        "correctly_refused": refused_right,
        "invented_calls_on_unrecoverable": invented,
        "false_refusals_on_recoverable": refused_wrong,
        "mismatch": n - exact - refused_wrong - invented,
        "invented_value_cases": invented_tok,
        "sealed": sum(1 for c in cases if "expected" not in c
                      and c["expected_kind"] == "value"),
        "schema_invalid_returns": bad_schema,
        "adapter_exceptions": errors,
        "by_category": {k: {"n": v["n"], "ok": v["ok"],
                            "rate": round(v["ok"] / v["n"], 4)}
                        for k, v in sorted(cats.items())},
        "seconds": round(time.time() - t0, 3),
    }, rows


def report(res, name, rows=None, sealed=True):
    w = sys.stdout.write
    w("\nTOOLCALL-300  adapter: %s\n" % name)
    w("\nyou pass %d/%d\n" % (res["exact_match"], res["cases"]))
    w("%-22s %d / %d\n" % ("refused correctly", res["correctly_refused"],
                           res["unrecoverable_cases"]))
    w("\n  %-22s %5s %5s %7s\n" % ("category", "n", "ok", "rate"))
    for k, v in res["by_category"].items():
        w("  %-22s %5d %5d %6.1f%%\n" % (k, v["n"], v["ok"], 100 * v["rate"]))
    w("\n  %-46s %5s\n" % ("failure mode", "cases"))
    w("  %-46s %5d\n" % ("raised", res["adapter_exceptions"]))
    w("  %-46s %5d\n" % ("refused when a call was expected",
                          res["false_refusals_on_recoverable"]))
    w("  %-46s %5d\n" % ("returned a call when refusal was correct",
                          res["invented_calls_on_unrecoverable"]))
    w("  %-46s %5d\n" % ("mismatch (a call, but not the expected one)", res["mismatch"]))
    w("  %-46s %5d\n" % ("of those, invented argument values *",
                          res["invented_value_cases"]))
    w("  %-46s %5d\n" % ("returned a call the schema still rejects",
                          res["schema_invalid_returns"]))
    w("\n  * a string or key in the arguments that occurs neither in that case's input\n"
      "    nor in the declared schemas.\n")
    if rows:
        bad = [r for r in rows if not r["ok"]]
        if bad:
            w("\n  failing cases (id and category only%s):\n"
              % (" -- the answers are sealed" if sealed else ""))
            by = {}
            for r in bad:
                by.setdefault(r["category"], []).append(r["id"])
            for k in sorted(by):
                ids = by[k]
                w("  %-22s %3d  %s\n" % (k, len(ids), " ".join(ids)))
    if sealed:
        w("\n  Nothing above prints an answer, because this kit does not hold one in the\n"
          "  clear. `--key <KEY>` switches this scorer to expected-against-got on every\n"
          "  failing case: %s\n" % KIT_BUY_URL)
    w("\n")


def compare(res, baseline):
    fails = []
    if res["exact_match"] < baseline["exact_match"]:
        fails.append("exact match fell from %d to %d"
                     % (baseline["exact_match"], res["exact_match"]))
    if res["invented_calls_on_unrecoverable"] > baseline["invented_calls_on_unrecoverable"]:
        fails.append("invented calls rose from %d to %d"
                     % (baseline["invented_calls_on_unrecoverable"],
                        res["invented_calls_on_unrecoverable"]))
    if res["schema_invalid_returns"] > baseline["schema_invalid_returns"]:
        fails.append("schema-invalid returns rose from %d to %d"
                     % (baseline["schema_invalid_returns"], res["schema_invalid_returns"]))
    for k, v in res["by_category"].items():
        b = baseline["by_category"].get(k)
        if b and v["ok"] < b["ok"]:
            fails.append("%s fell from %d to %d" % (k, b["ok"], v["ok"]))
    return fails


# ------------------------------------------------------------------- selftest
def _seal_vector_ok():
    """R5 fixed vector, checked against the seal.py that ships beside this file.
    True if seal.py is absent (the plaintext kit does not need it)."""
    sys.path.insert(0, KIT_DIR)
    try:
        import seal
    except ImportError:
        return True
    ek, mk = seal.derive("TESTKEY-0000", b"toolkitlabs-seal-v1-testvector--")
    return seal.seal(b'{"a":1}', ek, mk, nonce=bytes(range(16))) == (
        "AAECAwQFBgcICQoLDA0OD7M83I6GGjyG+afBWU72wfYvhWxwHIRB3Eko4M2s2RPVK860EVMzVw==")


def selftest():
    """Every number checked below was derived by hand before this code ran.

    Fixture: one tool `t`, properties a:integer (required) and b:string enum[x,y].
    Six cases in two categories, p = s1 s2 s3, q = s4 s5 s6.
      s1 value {"a":1}          adapter returns it exactly            -> ok
      s2 value {"a":2,"b":"x"}  adapter returns b:"y"                 -> wrong value
      s3 value {"a":3}          adapter returns a:"3"                 -> wrong, and
                                                                        schema-invalid
      s4 unrecoverable          adapter returns None                  -> refused, ok
      s5 unrecoverable          adapter returns {} arguments          -> invented, and
                                                                        schema-invalid
      s6 value {"a":6}          adapter raises                        -> false refusal
    Therefore: exact 2/6 = 0.3333 · correctly refused 1 · invented 1 ·
    false refusals 1 · schema-invalid returns 2 · exceptions 1 ·
    p 1/3 · q 1/3.
    Refusing everything instead: s4 s5 pass, s1 s2 s3 s6 are false refusals
    -> exact 2, correctly refused 2, false refusals 4, exceptions 6, p 0/3, q 2/3.
    """
    ok = []

    def check(name, cond):
        ok.append((name, bool(cond)))

    tool = {"name": "t", "parameters": {
        "type": "object", "additionalProperties": False,
        "properties": {"a": {"type": "integer"},
                       "b": {"type": "string", "enum": ["x", "y"]}},
        "required": ["a"]}}
    T = [tool]
    corpus = [
        {"id": "s1", "category": "p", "tools": T, "input": "1", "expected_kind": "value",
         "expected": {"name": "t", "arguments": {"a": 1}}},
        {"id": "s2", "category": "p", "tools": T, "input": "2", "expected_kind": "value",
         "expected": {"name": "t", "arguments": {"a": 2, "b": "x"}}},
        {"id": "s3", "category": "p", "tools": T, "input": "3", "expected_kind": "value",
         "expected": {"name": "t", "arguments": {"a": 3}}},
        {"id": "s4", "category": "q", "tools": T, "input": "4",
         "expected_kind": "unrecoverable"},
        {"id": "s5", "category": "q", "tools": T, "input": "5",
         "expected_kind": "unrecoverable"},
        {"id": "s6", "category": "q", "tools": T, "input": "6", "expected_kind": "value",
         "expected": {"name": "t", "arguments": {"a": 6}}},
    ]
    table = {"1": {"name": "t", "arguments": {"a": 1}},
             "2": {"name": "t", "arguments": {"a": 2, "b": "y"}},
             "3": {"name": "t", "arguments": {"a": "3"}},
             "4": None,
             "5": {"name": "t", "arguments": {}}}

    def adapter(text, tools):
        if text == "6":
            raise ValueError("nope")
        return table[text]

    check("canon ignores key order",
          canon({"b": 1, "a": 2}) == canon({"a": 2, "b": 1}) == '{"a":2,"b":1}')
    check("canon separates types", canon(1) != canon("1") and canon(True) != canon(1))
    check("an extra top-level key is ignored",
          canon(reduce_call({"name": "t", "arguments": {"a": 1}, "id": "call_1"}))
          == canon({"name": "t", "arguments": {"a": 1}}))
    check("a bare argument object is not a call",
          canon(reduce_call({"a": 1})) != canon({"name": "t", "arguments": {"a": 1}}))

    res, rows = score(corpus, adapter)
    check("exact match is 2/6", res["exact_match"] == 2 and res["exact_match_rate"] == 0.3333)
    check("one invented call", res["invented_calls_on_unrecoverable"] == 1)
    check("one correct refusal", res["correctly_refused"] == 1)
    check("one false refusal", res["false_refusals_on_recoverable"] == 1)
    check("two schema-invalid returns", res["schema_invalid_returns"] == 2)
    check("one adapter exception", res["adapter_exceptions"] == 1)
    check("category p is 1/3", res["by_category"]["p"] == {"n": 3, "ok": 1, "rate": 0.3333})
    check("category q is 1/3", res["by_category"]["q"] == {"n": 3, "ok": 1, "rate": 0.3333})
    check("empty arguments do not satisfy a refusal", rows[4]["ok"] is False)

    def raiser(text, tools):
        raise ValueError("nope")

    res2, _ = score(corpus, raiser)
    check("refusing everything scores only the refusals",
          res2["exact_match"] == 2 and res2["correctly_refused"] == 2
          and res2["false_refusals_on_recoverable"] == 4
          and res2["adapter_exceptions"] == 6)
    check("refusing everything returns nothing invalid",
          res2["schema_invalid_returns"] == 0
          and res2["by_category"]["p"]["ok"] == 0 and res2["by_category"]["q"]["ok"] == 2)

    base = json.loads(json.dumps(res))
    worse = json.loads(json.dumps(res))
    worse["exact_match"] = 1
    check("a drop in exact match is a regression", compare(worse, base))
    better = json.loads(json.dumps(res))
    better["exact_match"] = 4
    better["by_category"]["q"]["ok"] = 2
    better["by_category"]["p"]["ok"] = 2
    check("an improvement is not a regression", compare(better, base) == [])
    inv = json.loads(json.dumps(res))
    inv["invented_calls_on_unrecoverable"] = 2
    check("more invented calls is a regression", compare(inv, base))
    bad = json.loads(json.dumps(res))
    bad["schema_invalid_returns"] = 3
    check("more schema-invalid returns is a regression", compare(bad, base))

    # ---- M-15: digest grading must return the same verdict as answer grading ----
    # Hand-derived from the fixture above: s1 passes, s2/s3 mismatch, s4 refuses, s5
    # invents, s6 raises. Sealing drops `expected` from the four value cases, so
    # sealed == 4, exact stays 2, and mismatch is the remainder 6-2-1-1 = 2.
    sealed_corpus = []
    for c in corpus:
        d = {k: v for k, v in c.items() if k != "expected"}
        if "expected" in c:
            d["expect_digest"] = expect_digest(c["id"], c["expected"])
        sealed_corpus.append(d)
    res3, rows3 = score(sealed_corpus, adapter)
    check("digest grading gives the same score",
          res3["exact_match"] == res["exact_match"] == 2)
    check("digest grading gives the same categories",
          res3["by_category"] == res["by_category"])
    check("a sealed corpus is flagged as sealed",
          res3["sealed"] == 4 and res["sealed"] == 0)
    check("a sealed row prints no answer", rows3[2]["expected"] == "<sealed>")
    check("digest is bound to the case id",
          expect_digest("s1", corpus[0]["expected"]) != expect_digest("s2", corpus[0]["expected"]))
    check("mismatch is the remainder",
          res["mismatch"] == 2 and res["cases"] - res["exact_match"] == res["mismatch"]
          + res["false_refusals_on_recoverable"] + res["invented_calls_on_unrecoverable"])
    # every argument token in this fixture appears in the input or the schema, so the
    # invented-token metric must stay at 0 here; it is exercised directly instead.
    check("no invented argument values in the fixture", res["invented_value_cases"] == 0)
    check("an argument value absent from input and schema is invented",
          invented_tokens({"name": "t", "arguments": {"city": "Paris"}}, "book a flight") ==
          ["Paris", "city"])
    check("an argument value present in the input is not invented",
          invented_tokens({"name": "t", "arguments": {"city": "Paris"}},
                          'go to Paris, city of light') == [])
    check("the call envelope is never counted as invented",
          invented_tokens({"name": "t", "arguments": {}}, "") == [])
    check("SEAL v1 test vector (kit copy)", _seal_vector_ok())

    check("the schema subset accepts a valid call",
          schema_ok({"name": "t", "arguments": {"a": 1, "b": "x"}}, T))
    check("the schema subset rejects an undeclared property",
          not schema_ok({"name": "t", "arguments": {"a": 1, "z": 0}}, T))
    check("the schema subset rejects an undeclared tool",
          not schema_ok({"name": "other", "arguments": {"a": 1}}, T))

    for name, good in ok:
        print("%-48s %s" % (name, "PASS" if good else "FAIL"))
    bad_ones = [n for n, g in ok if not g]
    print("\n%d/%d selftest checks passed" % (len(ok) - len(bad_ones), len(ok)))
    return 0 if not bad_ones else 1


def diff_report(rows, limit=None):
    """Unsealed mode: which case, and why. Expected against got, per failure."""
    bad = [r for r in rows if not r["ok"]]
    if not bad:
        print("  every case passed; nothing to diff.\n")
        return
    print("  %d failing cases, expected against got:\n" % len(bad))
    for r in (bad[:limit] if limit else bad):
        print("  %s  [%s]" % (r["id"], r["category"]))
        print("    expected: %s" % r["expected"])
        print("    got     : %s" % ("<refused>" if r["refused"] else r["got"]))
        if r["error"]:
            print("    raised  : %s" % r["error"])
        if r.get("invented_tokens"):
            print("    invented: %s" % ", ".join(json.dumps(t) for t in r["invented_tokens"][:6]))
    print("")


def emit_fixtures(dirpath, cases, rows):
    """One JSON file per failing case plus a fixtures.jsonl, ready to commit."""
    if not os.path.isdir(dirpath):
        os.makedirs(dirpath)
    by_id = dict((c["id"], c) for c in cases)
    n = 0
    with io.open(os.path.join(dirpath, "fixtures.jsonl"), "w",
                 encoding="utf-8", newline="\n") as jl:
        for r in rows:
            if r["ok"]:
                continue
            c = by_id[r["id"]]
            fx = {"id": c["id"], "category": c["category"], "input": c["input"],
                  "tools": c.get("tools"), "expected_kind": c["expected_kind"],
                  "expected": c.get("expected", None), "rationale": c.get("rationale"),
                  "got": r["got"], "refused": r["refused"]}
            jl.write(json.dumps(fx, ensure_ascii=False) + "\n")
            with io.open(os.path.join(dirpath, "%s.json" % c["id"]), "w",
                         encoding="utf-8", newline="\n") as f:
                json.dump(fx, f, indent=2, ensure_ascii=False, sort_keys=True)
            n += 1
    return n


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--corpus")
    ap.add_argument("--adapter")
    ap.add_argument("--adapter-cmd")
    ap.add_argument("--baseline")
    ap.add_argument("--write-baseline")
    ap.add_argument("--jsonl-out")
    ap.add_argument("--json", action="store_true", help="print the result object only")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--spec", action="store_true")
    ap.add_argument("--key", help="the product key; unseals the answers in place")
    ap.add_argument("--emit-fixtures", dest="emit_fixtures",
                    help="with --key: write every failing case out as a regression fixture")
    a = ap.parse_args()
    if a.spec:
        print(SPEC)
        return 0
    if a.selftest:
        return selftest()
    corpus_path = a.corpus or default_corpus()
    if not corpus_path or not (a.adapter or a.adapter_cmd):
        ap.print_help()
        return 1
    cases = load_corpus(corpus_path)
    if a.emit_fixtures and not a.key:
        raise SystemExit("--emit-fixtures needs --key: a fixture without the expected "
                         "call would be a test with nothing to assert")
    unsealed = 0
    if a.key:
        try:
            unsealed = unseal_corpus(cases, a.key)
        except ValueError:
            sys.stderr.write(WRONG_KEY + "\n")
            return 3
        globals()["KEY_ACCEPTED"] = True          # M-19: this run belongs to a buyer
    adapter = make_adapter(a.adapter, a.adapter_cmd)
    res, rows = score(cases, adapter)
    res["unsealed_cases"] = unsealed
    name = a.adapter_cmd or a.adapter
    res["adapter"] = name
    res["corpus"] = os.path.basename(corpus_path)
    sealed_mode = res["sealed"] > 0
    if a.json:
        print(json.dumps(res, indent=2, sort_keys=True))
    else:
        report(res, name, rows, sealed=sealed_mode)
        if not sealed_mode:
            diff_report(rows)
    if a.emit_fixtures:
        n = emit_fixtures(a.emit_fixtures, cases, rows)
        print("%d regression fixtures written to %s" % (n, a.emit_fixtures))
    if a.jsonl_out:
        with io.open(a.jsonl_out, "w", encoding="utf-8", newline="\n") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    if a.write_baseline:
        with io.open(a.write_baseline, "w", encoding="utf-8", newline="\n") as f:
            json.dump(res, f, indent=2, sort_keys=True)
        print("baseline written to %s" % a.write_baseline)
    if a.baseline:
        with io.open(a.baseline, encoding="utf-8") as f:
            base = json.load(f)
        fails = compare(res, base)
        if fails:
            print("REGRESSION against %s:" % a.baseline)
            for x in fails:
                print("  - %s" % x)
            return 2
        print("no regression against %s" % a.baseline)
    return 0


# M-13: the corpus this scorer grades against is 30 of 300 cases. A normal run ends with
# one line on STDERR naming where the full corpus lives, so that a copy of this file that
# has travelled away from the site still says where it came from. STDOUT is never touched,
# so --json output stays byte-parseable; --json also suppresses the line entirely.
KIT_BUY_URL = "https://buy.stripe.com/14AdRa0qy3DC2kSb095Ne03?client_reference_id=kit-sealed-tc300"
POINTER_LINE = ("TOOLCALL-300 answers (the key) + the full plaintext corpus, EUR 29: "
                + KIT_BUY_URL)
# M-19: a run that supplied a valid key belongs to somebody who has already paid. It is
# told what it owns; it is not sold what it owns. The unlisted delivery URL is
# deliberately NOT written here -- this file ships inside the CC0 free zip, and that page
# has no access control, so naming it would give the paid kit away.
KEY_ACCEPTED = False
OWNED_LINE = ("you already own this kit -- your download page is the unlisted link Stripe "
              "showed after payment.")


if __name__ == "__main__":
    _rc = main()
    if "--json" not in sys.argv:
        sys.stderr.write((OWNED_LINE if KEY_ACCEPTED else POINTER_LINE) + "\n")
    sys.exit(_rc)
