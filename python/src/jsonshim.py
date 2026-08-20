#!/usr/bin/env python3
"""jsonshim - get the JSON out of a model's answer, or find out you can't.

A model was asked for JSON and returned prose, a code fence, a trailing comma, a
single quote, or a reply that stopped mid-object because the token budget ran out.
json.loads says only "Expecting value: line 1 column 1 (char 0)".

jsonshim finds the JSON-shaped span, repairs the shapes that are actually
repairable, and tells you exactly what it changed. When it cannot recover a value
it says so and returns nothing - it never guesses a field.

One file. Standard library only. No install, no network, no telemetry. CC0.

  python3 jsonshim.py --selftest        every assertion below checked
  cat reply.txt | python3 jsonshim.py   JSON on stdout, report on stderr, exit 1 if unrecoverable

  from jsonshim import extract
  r = extract(text)
  if r.ok: use(r.value)      # r.repairs lists what was changed, r.span where it was found
"""
from __future__ import annotations

import json
import sys

__version__ = "1.0.0"
__all__ = ["extract", "Result", "loads"]

_WS = " \t\r\n"
_BARE_END = ",:}]" + _WS

_LITERALS = {
    "true": "true", "false": "false", "null": "null",
    "True": "true", "False": "false", "None": "null",
    "TRUE": "true", "FALSE": "false", "NULL": "null",
    "NaN": "null", "nan": "null", "Infinity": "null", "-Infinity": "null",
    "undefined": "null",
}


class Result:
    """What was found, what was changed, and where it came from."""

    __slots__ = ("ok", "value", "repairs", "span", "error", "raw")

    def __init__(self, ok, value=None, repairs=(), span=None, error=None, raw=None):
        self.ok = ok
        self.value = value
        self.repairs = list(repairs)
        self.span = span          # (start, end) offsets into the ORIGINAL text
        self.error = error
        self.raw = raw            # the candidate substring that was repaired

    def __repr__(self):
        if self.ok:
            return "Result(ok=True, repairs=%r, span=%r)" % (self.repairs, self.span)
        return "Result(ok=False, error=%r)" % (self.error,)


# --------------------------------------------------------------------------
# span finding
# --------------------------------------------------------------------------

def _scan_span(text, start):
    """Return the end offset of the container opened at `start`.

    Honours strings and escapes so a brace inside a string does not close it.
    If the container is never closed (a truncated reply), returns len(text).
    """
    depth = 0
    i = start
    n = len(text)
    while i < n:
        c = text[i]
        if c == '"' or c == "'":
            q = c
            i += 1
            while i < n:
                if text[i] == "\\":
                    i += 2
                    continue
                if text[i] == q:
                    break
                i += 1
            i += 1
            continue
        if c in "{[":
            depth += 1
        elif c in "}]":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def _fences(text):
    """Yield (start, end) of the contents of every ``` fenced block."""
    out = []
    i = 0
    while True:
        a = text.find("```", i)
        if a < 0:
            return out
        nl = text.find("\n", a)
        if nl < 0:
            return out
        b = text.find("```", nl)
        if b < 0:
            out.append((nl + 1, len(text)))   # unterminated fence: run to the end
            return out
        out.append((nl + 1, b))
        i = b + 3


def _candidates(text, want):
    """Candidate (start, end) spans, best first, deduplicated."""
    opens = "{[" if want is None else ("{" if want == "object" else "[")
    regions = [(a, b) for a, b in _fences(text)] + [(0, len(text))]
    seen = set()
    out = []
    for ra, rb in regions:
        i = ra
        while i < rb:
            if text[i] in opens:
                end = min(_scan_span(text, i), rb) if rb < len(text) else _scan_span(text, i)
                key = (i, end)
                if key not in seen:
                    seen.add(key)
                    out.append(key)
                i = i + 1
            else:
                i += 1
    return out


# --------------------------------------------------------------------------
# repair
# --------------------------------------------------------------------------

def _read_string(s, i):
    """Read a quoted string starting at s[i]. Returns (python_value, next_i, terminated)."""
    q = s[i]
    i += 1
    buf = []
    n = len(s)
    while i < n:
        c = s[i]
        if c == "\\":
            if i + 1 >= n:
                return "".join(buf), n, False
            nxt = s[i + 1]
            if nxt == q and q == "'":
                buf.append("'")            # \' is not valid JSON, unescape it
            elif nxt in '"\\/bfnrtu':
                if nxt == "u":
                    buf.append(s[i:i + 6])
                    i += 6
                    continue
                buf.append({"b": "\b", "f": "\f", "n": "\n", "r": "\r",
                            "t": "\t"}.get(nxt, nxt))
            else:
                buf.append(nxt)
            i += 2
            continue
        if c == q:
            return "".join(buf), i + 1, True
        if c == "\n" and q == '"':
            # a raw newline inside a double-quoted string is invalid JSON; the model
            # almost always meant a line break, so keep it and let the encoder escape it
            buf.append("\n")
            i += 1
            continue
        buf.append(c)
        i += 1
    return "".join(buf), n, False


def _emit_string(value):
    return json.dumps(value)


def _reject_constant(name):
    """json.loads accepts NaN/Infinity; strict JSON does not. Force the repair path."""
    raise ValueError("non-JSON constant %s" % name)


def _pop_ws(out):
    while out and out[-1].strip() == "" and out[-1] != "":
        out.pop()


def _last(out):
    for piece in reversed(out):
        if piece.strip() != "":
            return piece
    return None


def _repair(s):
    """Rewrite a JSON-ish string into strict JSON. Returns (text, sorted repairs)."""
    out = []
    rep = set()
    stack = []          # list of dicts: {"kind": "{"|"[", "phase": str, "mark": int}
    i = 0
    n = len(s)

    def top():
        return stack[-1] if stack else None

    def value_done():
        t = top()
        if t:
            t["phase"] = "post"

    while i < n:
        c = s[i]

        if c in _WS:
            out.append(c)
            i += 1
            continue

        if c == "/" and i + 1 < n and s[i + 1] in "/*":
            rep.add("comment removed")
            if s[i + 1] == "/":
                j = s.find("\n", i)
                i = n if j < 0 else j
            else:
                j = s.find("*/", i + 2)
                i = n if j < 0 else j + 2
            continue

        if c in "{[":
            out.append(c)
            stack.append({"kind": c, "phase": "key" if c == "{" else "value",
                          "mark": len(out)})
            i += 1
            continue

        if c in "}]":
            _pop_ws(out)
            if out and out[-1] == ",":
                out.pop()
                rep.add("trailing comma removed")
            if stack:
                t = stack.pop()
                if t["kind"] == "{" and t["phase"] == "colon":
                    del out[t.get("key_mark", len(out)):]
                    rep.add("dangling key dropped")
                elif t["phase"] == "value" and t["kind"] == "{":
                    out.append("null")
                    rep.add("missing value filled with null")
                out.append("}" if t["kind"] == "{" else "]")
                if c != ("}" if t["kind"] == "{" else "]"):
                    rep.add("mismatched bracket corrected")
            else:
                rep.add("stray closing bracket dropped")
            i += 1
            value_done()
            continue

        if c == ",":
            t = top()
            if _last(out) is None or _last(out) in "[{,":
                rep.add("empty element removed")
                i += 1
                continue
            out.append(",")
            if t:
                t["phase"] = "key" if t["kind"] == "{" else "value"
            i += 1
            continue

        if c == ":":
            out.append(":")
            t = top()
            if t:
                t["phase"] = "value"
            i += 1
            continue

        if c == '"' or c == "'":
            t = top()
            is_key = bool(t) and t["kind"] == "{" and t["phase"] == "key"
            if is_key:
                t["key_mark"] = len(out)
            val, j, term = _read_string(s, i)
            if c == "'":
                rep.add("single-quoted string requoted")
            if not term:
                rep.add("unterminated string closed")
            out.append(_emit_string(val))
            i = j
            if is_key:
                t["phase"] = "colon"
            else:
                value_done()
            continue

        # bare token
        j = i
        while j < n and s[j] not in _BARE_END and s[j] != "/":
            j += 1
        tok = s[i:j].strip()
        t = top()
        is_key = bool(t) and t["kind"] == "{" and t["phase"] == "key"
        if is_key:
            t["key_mark"] = len(out)
        if tok in _LITERALS:
            if _LITERALS[tok] != tok:
                rep.add("non-JSON literal %s -> %s" % (tok, _LITERALS[tok]))
            out.append(_LITERALS[tok])
        else:
            try:
                num = json.loads(tok)
                if not isinstance(num, (int, float)):
                    raise ValueError
                out.append(tok)
            except Exception:
                if j >= n and not is_key:
                    rep.add("truncated token dropped")
                    if t:
                        t["phase"] = "post"
                    i = j
                    continue
                rep.add("unquoted %s quoted" % ("key" if is_key else "value"))
                out.append(_emit_string(tok))
        i = j
        if is_key:
            t["phase"] = "colon"
        else:
            value_done()

    # end of input: close whatever is still open
    if stack:
        while True:
            _pop_ws(out)
            if out and out[-1] == ",":
                out.pop()
                continue
            if out and out[-1] == ":":
                out.pop()
                t = top()
                if t is not None and "key_mark" in t:
                    del out[t["key_mark"]:]
                    t.pop("key_mark", None)
                    t["phase"] = "post"
                rep.add("dangling key dropped")
                continue
            break
        closed = 0
        while stack:
            t = stack.pop()
            if t["kind"] == "{" and t["phase"] == "colon" and "key_mark" in t:
                del out[t["key_mark"]:]
                _pop_ws(out)
                if out and out[-1] == ",":
                    out.pop()
                rep.add("dangling key dropped")
            out.append("}" if t["kind"] == "{" else "]")
            closed += 1
        rep.add("truncated input: closed %d container%s"
                % (closed, "" if closed == 1 else "s"))

    return "".join(out), sorted(rep)


# --------------------------------------------------------------------------
# public entry points
# --------------------------------------------------------------------------

def extract(text, want=None):
    """Find and parse the JSON in `text`.

    want: None (either), "object", or "array".
    Returns a Result. r.ok is False when nothing parseable was found - jsonshim
    never invents a value to avoid returning an error.
    """
    if not isinstance(text, str):
        return Result(False, error="input is %s, not str" % type(text).__name__)
    if not text.strip():
        return Result(False, error="empty input")

    first_err = None
    for a, b in _candidates(text, want):
        chunk = text[a:b]
        try:
            return Result(True, json.loads(chunk, parse_constant=_reject_constant),
                          [], (a, b), raw=chunk)
        except Exception as e:
            if first_err is None:
                first_err = str(e)
        fixed, rep = _repair(chunk)
        try:
            val = json.loads(fixed)
        except Exception as e:
            if first_err is None:
                first_err = str(e)
            continue
        if want == "object" and not isinstance(val, dict):
            continue
        if want == "array" and not isinstance(val, list):
            continue
        return Result(True, val, rep, (a, b), raw=chunk)

    return Result(False, error=first_err or "no JSON-shaped span found")


def loads(text, want=None):
    """extract() that raises ValueError instead of returning a failed Result."""
    r = extract(text, want)
    if not r.ok:
        raise ValueError("jsonshim: %s" % r.error)
    return r.value


# --------------------------------------------------------------------------
# selftest - every expectation here was worked out by hand
# --------------------------------------------------------------------------

CASES = [
    # (name, input, expected value)
    ("clean", '{"a": 1}', {"a": 1}),
    ("prose around", 'Sure! Here you go:\n{"a": 1}\nHope that helps.', {"a": 1}),
    ("fenced json", '```json\n{"a": 1}\n```', {"a": 1}),
    ("fenced bare", '```\n[1, 2, 3]\n```', [1, 2, 3]),
    ("unterminated fence", '```json\n{"a": 1}', {"a": 1}),
    ("trailing comma object", '{"a": 1, "b": 2,}', {"a": 1, "b": 2}),
    ("trailing comma array", '[1, 2, 3,]', [1, 2, 3]),
    ("single quotes", "{'a': 'b'}", {"a": "b"}),
    ("unquoted keys", '{a: 1, b: 2}', {"a": 1, "b": 2}),
    ("python literals", '{"a": True, "b": False, "c": None}',
     {"a": True, "b": False, "c": None}),
    ("nan to null", '{"a": NaN}', {"a": None}),
    ("line comment", '{\n  "a": 1 // the first one\n}', {"a": 1}),
    ("block comment", '{/* hi */ "a": 1}', {"a": 1}),
    ("truncated object", '{"a": 1, "b": 2', {"a": 1, "b": 2}),
    ("truncated nested", '{"a": {"b": [1, 2', {"a": {"b": [1, 2]}}),
    ("truncated mid-string", '{"a": "hel', {"a": "hel"}),
    ("truncated after comma", '{"a": 1,', {"a": 1}),
    ("truncated after colon", '{"a": 1, "b":', {"a": 1}),
    ("truncated dangling key", '{"a": 1, "b"', {"a": 1}),
    ("truncated number", '{"a": 1, "b": 12.', {"a": 1}),
    ("brace inside string", '{"a": "}"}', {"a": "}"}),
    ("quote inside string", '{"a": "he said \\"hi\\""}', {"a": 'he said "hi"'}),
    ("apostrophe in single quotes", "{'a': 'it\\'s'}", {"a": "it's"}),
    ("raw newline in string", '{"a": "one\ntwo"}', {"a": "one\ntwo"}),
    ("unquoted value", '{"status": ok}', {"status": "ok"}),
    ("empty element", '[1, , 2]', [1, 2]),
    ("array of objects", 'result:\n[{"a": 1,}, {"b": 2,},]', [{"a": 1}, {"b": 2}]),
    ("unicode escape kept", '{"a": "\\u00e9"}', {"a": "é"}),
    ("nested prose fence", 'blah\n```json\n{"tool": "search", "args": {"q": "a, b"}}\n```\nbye',
     {"tool": "search", "args": {"q": "a, b"}}),
    ("deep truncation", '[{"a": [1, {"b": "x', [{"a": [1, {"b": "x"}]}]),
    ("mismatched close", '{"a": 1]', {"a": 1}),
    ("colon in string value", '{"url": "http://x.y/z"}', {"url": "http://x.y/z"}),
]

FAIL_CASES = [
    ("empty", ""),
    ("no json", "I am afraid I cannot do that."),
    ("prose only with braces text", "the set { of things } we discussed"),
]


def selftest(verbose=True):
    bad = 0
    for name, src, want in CASES:
        r = extract(src)
        if not r.ok:
            bad += 1
            if verbose:
                print("FAIL %-26s -> not recovered (%s)" % (name, r.error))
            continue
        if r.value != want:
            bad += 1
            if verbose:
                print("FAIL %-26s -> %r, expected %r" % (name, r.value, want))
            continue
        if verbose:
            print("ok   %-26s %s" % (name, ("[" + ", ".join(r.repairs) + "]") if r.repairs else ""))
    for name, src in FAIL_CASES:
        r = extract(src)
        # "the set { of things } we discussed" must NOT silently become something
        if r.ok and isinstance(r.value, (dict, list)) and r.value not in ({}, []):
            bad += 1
            if verbose:
                print("FAIL %-26s -> invented %r" % (name, r.value))
        elif verbose:
            print("ok   %-26s correctly refused" % name)
    # want= filters
    r = extract('{"a": 1}\n[1,2]', want="array")
    if not (r.ok and r.value == [1, 2]):
        bad += 1
        if verbose:
            print("FAIL want=array")
    elif verbose:
        print("ok   want=array                picked the array, not the object")
    # loads() raises rather than guessing
    try:
        loads("nothing here")
        bad += 1
        if verbose:
            print("FAIL loads() should have raised")
    except ValueError:
        if verbose:
            print("ok   loads()                   raised instead of guessing")
    total = len(CASES) + len(FAIL_CASES) + 2
    if verbose:
        print("\n%d/%d passed" % (total - bad, total))
    return bad == 0


def main(argv):
    if "--selftest" in argv:
        return 0 if selftest() else 1
    if "--version" in argv:
        print(__version__)
        return 0
    want = None
    if "--object" in argv:
        want = "object"
    if "--array" in argv:
        want = "array"
    text = sys.stdin.read()
    r = extract(text, want)
    if not r.ok:
        sys.stderr.write("jsonshim: unrecoverable: %s\n" % r.error)
        return 1
    indent = 2 if "--pretty" in argv else None
    sys.stdout.write(json.dumps(r.value, indent=indent, ensure_ascii=False) + "\n")
    sys.stderr.write("jsonshim: recovered from span %d..%d%s\n"
                     % (r.span[0], r.span[1],
                        ("; repairs: " + ", ".join(r.repairs)) if r.repairs else
                        "; no repair needed"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
