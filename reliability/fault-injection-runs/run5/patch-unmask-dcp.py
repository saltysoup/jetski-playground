"""Stop torch DCP from destroying the real error during checkpoint load.

_DistWrapper.reduce_scatter runs create_local_plan inside a try/except and, on
failure, gathers the exception across ranks with pickle:

    local_data = _wrap_exception(e)      # -> (exc, StackSummary)
    all_data = self.gather_object(local_data)

_wrap_exception returns the *raw* exception object. If that exception holds a
reference to a code object it cannot be pickled, so the gather raises
"TypeError: cannot pickle code objects" and the original error is lost. That is
what run5-ckptval3-resume hit loading step_5's optimizer shard.

This replaces any unpicklable exception with a plain RuntimeError carrying the
original type, message and traceback — picklable, so DCP's normal error
propagation runs and reports what actually went wrong.

Diagnostic, not a fix. Applies to the worker venv and the base env.
"""

import glob
import sys

OLD = """def _wrap_exception(exc: BaseException) -> WRAPPED_EXCEPTION:
    return (exc, tb.extract_tb(exc.__traceback__))"""

NEW = '''def _wrap_exception(exc: BaseException) -> WRAPPED_EXCEPTION:
    # PATCHED (reliability demo): the raw exception is about to be pickled for a
    # cross-rank gather. If it is not picklable the gather raises and the real
    # error is lost, which is how a checkpoint-load failure surfaces as the
    # useless "cannot pickle code objects". Downgrade to a picklable stand-in.
    _tb = tb.extract_tb(exc.__traceback__)
    try:
        import pickle as _pickle

        _pickle.dumps(exc)
    except Exception as _pe:  # noqa: BLE001
        import traceback as _traceback

        _detail = "".join(_traceback.format_exception(type(exc), exc, exc.__traceback__))
        print(
            "[UNMASK] original exception was not picklable "
            f"({type(_pe).__name__}: {_pe}); real error follows:\\n{_detail}",
            flush=True,
        )
        exc = RuntimeError(f"[unmasked] {type(exc).__name__}: {exc}")
    return (exc, _tb)'''

targets = glob.glob("/opt/ray_venvs/*/lib/python3.*/site-packages/torch/distributed/checkpoint/api.py")
targets += glob.glob("/usr/local/lib/python3.*/site-packages/torch/distributed/checkpoint/api.py")
targets += glob.glob("/usr/lib/python3/dist-packages/torch/distributed/checkpoint/api.py")

if not targets:
    print("NO TARGETS FOUND")
    sys.exit(1)

for path in targets:
    src = open(path).read()
    if "[UNMASK]" in src:
        print(f"ALREADY PATCHED  {path}")
        continue
    if OLD not in src:
        print(f"ANCHOR NOT FOUND {path}")
        continue
    open(path, "w").write(src.replace(OLD, NEW))
    print(f"PATCHED          {path}")
