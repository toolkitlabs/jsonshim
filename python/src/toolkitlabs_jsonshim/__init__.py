"""toolkitlabs-jsonshim: the jsonshim recovery library and the two free
conformance scorers, as installed console scripts.

    jsonshim-score --corpus open12.jsonl --parser jsonshim:loads
    toolcall-score --corpus open12.jsonl --parser json

The library keeps its own import name so every published command still works:

    from jsonshim import extract, loads
"""
__version__ = "1.0.0"


def _run(mod):
    """Exactly what `python3 score.py` does, including the trailer line that the
    module only prints under its own __main__ guard."""
    import sys
    rc = mod.main()
    if "--json" not in sys.argv:
        line = mod.OWNED_LINE if mod.KEY_ACCEPTED else mod.POINTER_LINE
        sys.stderr.write(line + "\n")
    return rc


def _cli(name):
    # import_module, not `from . import X`: a function of the same name in this
    # namespace would shadow the submodule and hand back itself.
    import importlib
    raise SystemExit(_run(importlib.import_module("toolkitlabs_jsonshim." + name)))


def run_m300_scorer():
    _cli("score_m300")


def run_tc300_scorer():
    _cli("score_tc300")
