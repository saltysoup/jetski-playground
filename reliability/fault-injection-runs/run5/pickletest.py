"""Which half of the optimizer checkpoint can't be pickled?

dcp.load gathers its load plan across ranks with pickle, and dies with
"TypeError: cannot pickle code objects". OptimizerState.state_dict() returns
{"optim": ..., "sched": scheduler.state_dict()}, so test the scheduler the
recipe actually builds — SequentialLR(LinearLR, ConstantLR, milestones=[10]).

CPU-only, no GPUs, runs in seconds.
"""

import io
import pickle

import torch
from torch.optim.lr_scheduler import ConstantLR, LinearLR, SequentialLR

p = torch.nn.Parameter(torch.zeros(2))
opt = torch.optim.SGD([p], lr=1e-3)

s1 = LinearLR(opt, start_factor=0.1, end_factor=1, total_iters=10)
s2 = ConstantLR(opt, factor=1, total_iters=10_000_000_000)
seq = SequentialLR(opt, schedulers=[s1, s2], milestones=[10])


def probe(label, obj):
    try:
        pickle.dumps(obj)
        print(f"  {label:28s} PICKLES OK")
        return True
    except Exception as e:
        print(f"  {label:28s} FAILS: {type(e).__name__}: {e}")
        return False


print("scheduler state_dicts:")
probe("LinearLR.state_dict()", s1.state_dict())
probe("ConstantLR.state_dict()", s2.state_dict())
probe("SequentialLR.state_dict()", seq.state_dict())

sd = seq.state_dict()
print("\nSequentialLR.state_dict() keys:")
for k, v in sd.items():
    ok = ""
    try:
        pickle.dumps(v)
    except Exception as e:
        ok = f"  <-- UNPICKLABLE ({type(e).__name__})"
    print(f"  {k!r:26s} = {type(v).__name__}{ok}")
    if k == "_schedulers":
        for i, inner in enumerate(v):
            for ik, iv in (inner or {}).items():
                bad = ""
                try:
                    pickle.dumps(iv)
                except Exception as e:
                    bad = f"  <-- UNPICKLABLE ({type(e).__name__}: {e})"
                if bad:
                    print(f"      [{i}] {ik!r} = {type(iv).__name__}{bad}")

# torch gathers objects with its own pickler, not plain pickle
print("\nvia torch's object-gather path:")
try:
    f = io.BytesIO()
    torch.distributed.distributed_c10d._pickler(f).dump(sd)
    print("  torch _pickler(SequentialLR.state_dict()) OK")
except Exception as e:
    print(f"  torch _pickler FAILS: {type(e).__name__}: {e}")
