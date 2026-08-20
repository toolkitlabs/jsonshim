#!/usr/bin/env python3
"""
score.py -- run a JSON-recovery parser against the MALFORMED-300 conformance
suite (or the free 30-case sample) and print what it actually does.

Standard library only. No network. No telemetry. Public domain (CC0).

USAGE
  python3 score.py --corpus sample30.jsonl --parser json
  python3 score.py --corpus sample30.jsonl --parser mymodule:recover
  python3 score.py --corpus sample30.jsonl --parser-cmd "./myparser --stdin"
  python3 score.py --corpus sample30.jsonl --parser json --baseline base.json
  python3 score.py --selftest
  python3 score.py --spec

PARSER PROTOCOL
  --parser module:callable   the callable takes one str and returns the parsed
                             value, OR returns None to refuse, OR raises to
                             refuse. No case in this suite has a top-level
                             expected value of null, so None is unambiguous.
  --parser json              the standard library json.loads, as a control.
  --parser-cmd "..."         a subprocess: the malformed text goes in on stdin;
                             a recovered value comes back on stdout as JSON; a
                             non-zero exit or empty stdout counts as a refusal.

EXIT CODES
  0  scored, and no regression against --baseline (if one was given)
  1  usage or harness error
  2  a regression against --baseline: fewer exact matches, or more invented
     values on unrecoverable cases. Wire this into CI.
"""
import argparse, hashlib, json, importlib, io, os, subprocess, sys, time

SPEC = """MALFORMED-300 grading spec
1. expected_kind "value": pass only by returning exactly that value. Comparison
   is on json.dumps(v, sort_keys=True, separators=(",",":")) -- key order and
   whitespace do not matter, types and values do.
2. expected_kind "unrecoverable": pass only by REFUSING. Any returned value,
   including {} or [] or "", fails. Inventing an empty object where the model
   produced nothing is the failure this suite exists to measure.
3. Truncated cases: keep every pair or element that was completely written
   before the cut, drop the incomplete tail, close the open containers, invent
   nothing.
4. True->true, False->false, None/undefined->null. NaN and Infinity are absent
   from the suite on purpose: they have no JSON equivalent.
5. Typographic quotes around a key or string map to the ASCII form.
6. No case has a top-level expected value of null.
"""


def canon(v):
    return json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class Refused(Exception):
    pass


# ------------------------------------------------------- sealed-corpus support
KIT_DIR = os.path.dirname(os.path.abspath(__file__))
CORPUS_CANDIDATES = ("malformed300.jsonl", "cases.sealed.jsonl", "sample30.jsonl")
WRONG_KEY = "wrong key, or this file has been altered"


def expect_digest(case_id, expected):
    """The free-side grading digest: proves a match, carries nothing."""
    return hashlib.sha256((case_id + "\x00" + canon(expected)).encode("utf-8")).hexdigest()


def case_ok(c, got):
    """A recoverable case passes on the answer if we have it, on the digest if we do not."""
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
    """Strings and keys in the output that occur nowhere in the case's input bytes.

    This is computable without the answer, which is the whole point: it is the one
    failure mode a sealed kit can still name precisely."""
    return sorted(t for t in _tokens(got, set()) if t and t not in haystack)


def haystack(c):
    return c["input"]


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


def load_corpus(path):
    cases = []
    with io.open(path, encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            c = json.loads(line)
            for k in ("id", "category", "input", "expected_kind"):
                if k not in c:
                    raise SystemExit("corpus line %d is missing %r" % (ln, k))
            if (c["expected_kind"] == "value" and "expected" not in c
                    and "expect_digest" not in c):
                raise SystemExit("corpus line %d claims a value and carries neither an "
                                 "expected value nor an expect_digest" % ln)
            cases.append(c)
    if not cases:
        raise SystemExit("corpus %s is empty" % path)
    return cases


def make_parser(spec, cmd):
    if cmd:
        def run(text):
            p = subprocess.run(cmd, shell=True, input=text.encode("utf-8"),
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            if p.returncode != 0 or not p.stdout.strip():
                raise Refused()
            return json.loads(p.stdout.decode("utf-8"))
        return run
    if spec == "json":
        return json.loads
    if ":" not in spec:
        raise SystemExit("--parser wants module:callable, or the word json")
    mod, fn = spec.split(":", 1)
    sys.path.insert(0, os.getcwd())
    m = importlib.import_module(mod)
    f = getattr(m, fn)
    if not callable(f):
        raise SystemExit("%s is not callable" % spec)
    return f


def score(cases, parser):
    rows, cats = [], {}
    exact = invented = refused_right = refused_wrong = errors = invented_tok = 0
    t0 = time.time()
    for c in cases:
        got, refused, err = None, False, None
        try:
            got = parser(c["input"])
            if got is None:
                refused = True
        except Refused:
            refused = True
        except Exception as e:
            refused = True
            err = "%s: %s" % (type(e).__name__, e)
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
        "invented_values_on_unrecoverable": invented,
        "false_refusals_on_recoverable": refused_wrong,
        "mismatch": n - exact - refused_wrong - invented,
        "invented_value_cases": invented_tok,
        "sealed": sum(1 for c in cases if "expected" not in c
                      and c["expected_kind"] == "value"),
        "parser_exceptions": errors,
        "by_category": {k: {"n": v["n"], "ok": v["ok"],
                            "rate": round(v["ok"] / v["n"], 4)} for k, v in sorted(cats.items())},
        "seconds": round(time.time() - t0, 3),
    }, rows


def report(res, name, rows=None, sealed=True):
    w = sys.stdout.write
    w("\nMALFORMED-300  parser: %s\n" % name)
    w("\nyou pass %d/%d\n" % (res["exact_match"], res["cases"]))
    w("%-18s %d / %d\n" % ("refused correctly", res["correctly_refused"],
                           res["unrecoverable_cases"]))
    w("\n  %-16s %5s %5s %7s\n" % ("category", "n", "ok", "rate"))
    for k, v in res["by_category"].items():
        w("  %-16s %5d %5d %6.1f%%\n" % (k, v["n"], v["ok"], 100 * v["rate"]))
    w("\n  %-46s %5s\n" % ("failure mode", "cases"))
    w("  %-46s %5d\n" % ("raised", res["parser_exceptions"]))
    w("  %-46s %5d\n" % ("refused when a value was expected",
                          res["false_refusals_on_recoverable"]))
    w("  %-46s %5d\n" % ("returned a value when refusal was correct",
                          res["invented_values_on_unrecoverable"]))
    w("  %-46s %5d\n" % ("mismatch (a value, but not the expected one)", res["mismatch"]))
    w("  %-46s %5d\n" % ("of those, invented values *", res["invented_value_cases"]))
    w("\n  * a string or key in the output that occurs nowhere in that case's input.\n")
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
                w("  %-16s %3d  %s\n" % (k, len(ids), " ".join(ids)))
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
    if res["invented_values_on_unrecoverable"] > baseline["invented_values_on_unrecoverable"]:
        fails.append("invented values rose from %d to %d"
                     % (baseline["invented_values_on_unrecoverable"],
                        res["invented_values_on_unrecoverable"]))
    for k, v in res["by_category"].items():
        b = baseline["by_category"].get(k)
        if b and v["ok"] < b["ok"]:
            fails.append("%s fell from %d to %d" % (k, b["ok"], v["ok"]))
    return fails


# ------------------------------------------------------------------ selftest
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
    """Every number below is derived by hand first, then checked."""
    ok = []

    def check(name, cond):
        ok.append((name, bool(cond)))

    check("canon ignores key order",
          canon({"b": 1, "a": 2}) == canon({"a": 2, "b": 1}) == '{"a":2,"b":1}')
    check("canon separates types", canon(1) != canon("1") and canon(True) != canon(1))

    corpus = [
        {"id": "t1", "category": "x", "input": "a", "expected_kind": "value", "expected": {"a": 1}},
        {"id": "t2", "category": "x", "input": "b", "expected_kind": "value", "expected": [1, 2]},
        {"id": "t3", "category": "y", "input": "c", "expected_kind": "value", "expected": {"k": "v"}},
        {"id": "t4", "category": "z", "input": "d", "expected_kind": "unrecoverable"},
        {"id": "t5", "category": "z", "input": "e", "expected_kind": "unrecoverable"},
    ]
    table = {"a": {"a": 1}, "b": [1, 2], "c": {"k": "WRONG"}, "d": None, "e": {}}
    res, rows = score(corpus, lambda t: table[t])
    # hand-derived: t1 ok, t2 ok, t3 wrong value, t4 refused correctly, t5 invented {}
    check("exact match is 3/5", res["exact_match"] == 3 and res["exact_match_rate"] == 0.6)
    check("one invented value", res["invented_values_on_unrecoverable"] == 1)
    check("one correct refusal", res["correctly_refused"] == 1)
    check("no false refusals", res["false_refusals_on_recoverable"] == 0)
    check("category x is 2/2", res["by_category"]["x"] == {"n": 2, "ok": 2, "rate": 1.0})
    check("category z is 1/2", res["by_category"]["z"] == {"n": 2, "ok": 1, "rate": 0.5})

    # an empty object is NOT a pass on an unrecoverable case
    check("{} does not satisfy a refusal", rows[4]["ok"] is False)
    # raising is a refusal
    def raiser(t):
        raise ValueError("nope")
    res2, _ = score(corpus, raiser)
    check("raising refuses everything", res2["correctly_refused"] == 2
          and res2["false_refusals_on_recoverable"] == 3 and res2["exact_match"] == 2)
    check("exceptions counted", res2["parser_exceptions"] == 5)

    # regression detection
    base = dict(res)
    worse = json.loads(json.dumps(res))
    worse["exact_match"] = 2
    check("a drop in exact match is a regression", compare(worse, base))
    better = json.loads(json.dumps(res))
    better["exact_match"] = 4
    better["by_category"]["y"]["ok"] = 1
    check("an improvement is not a regression", compare(better, base) == [])
    inv = json.loads(json.dumps(res))
    inv["invented_values_on_unrecoverable"] = 2
    check("more invented values is a regression", compare(inv, base))

    # ---- M-15: digest grading must be the same verdict as answer grading -------
    # Hand-derived: t1 and t2 pass on the digest exactly as they pass on the answer;
    # t3 fails either way; the two unrecoverable cases never carry a digest at all.
    sealed_corpus = []
    for c in corpus:
        d = {k: v for k, v in c.items() if k != "expected"}
        if "expected" in c:
            d["expect_digest"] = expect_digest(c["id"], c["expected"])
        sealed_corpus.append(d)
    res3, rows3 = score(sealed_corpus, lambda t: table[t])
    check("digest grading gives the same score", res3["exact_match"] == res["exact_match"] == 3)
    check("digest grading gives the same categories",
          res3["by_category"] == res["by_category"])
    check("a sealed corpus is flagged as sealed", res3["sealed"] == 3 and res["sealed"] == 0)
    check("a sealed row prints no answer", rows3[2]["expected"] == "<sealed>")
    check("digest of the wrong value does not match",
          expect_digest("t3", {"k": "WRONG"}) != expect_digest("t3", {"k": "v"}))
    # mismatch decomposition: 5 cases, 3 pass, t3 is a mismatch, t5 invented, no refusals
    check("mismatch is the remainder", res["mismatch"] == 1
          and res["cases"] - res["exact_match"] == res["mismatch"]
          + res["false_refusals_on_recoverable"] + res["invented_values_on_unrecoverable"])
    # invented tokens: "WRONG" is not in t3's input text "c"
    check("an invented token is caught", res["invented_value_cases"] == 1)
    # (the key "k" is a token too, and it is not in t3's input "c" either -- the first
    #  hand-derivation of this line said ["WRONG"] and was wrong before the code ran)
    check("invented_tokens names them", rows[2]["invented_tokens"] == ["WRONG", "k"])
    check("a token present in the input is not invented",
          invented_tokens({"a": "cat"}, "the cat sat") == [])
    check("SEAL v1 test vector (kit copy)", _seal_vector_ok())

    for name, good in ok:
        print("%-42s %s" % (name, "PASS" if good else "FAIL"))
    bad = [n for n, g in ok if not g]
    print("\n%d/%d selftest checks passed" % (len(ok) - len(bad), len(ok)))
    return 0 if not bad else 1


def diff_report(rows, limit=None):
    """Unsealed mode: which case, and why. Expected against got, per failure."""
    bad = [r for r in rows if not r["ok"]]
    if not bad:
        print("  every case passed; nothing to diff.\n")
        return
    print("  %d failing cases, expected against got:\n" % len(bad))
    for r in bad[:limit] if limit else bad:
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
                  "expected_kind": c["expected_kind"],
                  "expected": c.get("expected", None),
                  "rationale": c.get("rationale"),
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
    ap.add_argument("--parser")
    ap.add_argument("--parser-cmd")
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
    if not corpus_path or not (a.parser or a.parser_cmd):
        ap.print_help()
        return 1
    cases = load_corpus(corpus_path)
    if a.emit_fixtures and not a.key:
        raise SystemExit("--emit-fixtures needs --key: a fixture without the expected "
                         "value would be a test with nothing to assert")
    unsealed = 0
    if a.key:
        try:
            unsealed = unseal_corpus(cases, a.key)
        except ValueError:
            sys.stderr.write(WRONG_KEY + "\n")
            return 3
        globals()["KEY_ACCEPTED"] = True          # M-19: this run belongs to a buyer
    parser = make_parser(a.parser, a.parser_cmd)
    res, rows = score(cases, parser)
    res["unsealed_cases"] = unsealed
    name = a.parser_cmd or a.parser
    res["parser"] = name
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
        with open(a.jsonl_out, "w") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    if a.write_baseline:
        with open(a.write_baseline, "w") as f:
            json.dump(res, f, indent=2, sort_keys=True)
        print("baseline written to %s" % a.write_baseline)
    if a.baseline:
        with open(a.baseline) as f:
            base = json.load(f)
        fails = compare(res, base)
        if fails:
            print("REGRESSION against %s:" % a.baseline)
            for x in fails:
                print("  - %s" % x)
            return 2
        print("no regression against %s" % a.baseline)
    return 0


# M-13/M-15: this scorer grades all 300 cases; in the free kit their answers are sealed.
# A normal run ends with one line on STDERR naming where the key lives, so that a copy of
# this file that has travelled away from the site still says where it came from. STDOUT is
# never touched, so --json output stays byte-parseable; --json suppresses the line entirely.
KIT_BUY_URL = "https://buy.stripe.com/4gMeVe8X42zy1gOfgp5Ne00?client_reference_id=kit-sealed-m300"
POINTER_LINE = ("MALFORMED-300 answers (the key) + the full plaintext corpus, EUR 29: "
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
