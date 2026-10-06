"""Rebuild DCP's local LoadPlan offline and pickle it.

dcp.load dies pickling the per-rank LoadPlan. The saved metadata pickles fine,
so the code object is introduced while building the plan. Reconstruct the plan
from a small synthetic state_dict shaped like the real one (a few tensor entries
plus the non-tensor param_group entries) and pickle each component.

Subset only — materialising all 17408 entries would be 300 GiB.
"""

import pickle

import torch
from torch.distributed.checkpoint import FileSystemReader
from torch.distributed.checkpoint.default_planner import DefaultLoadPlanner

PATH = "/gcs/ckpt/dapo-gemma3-27b-it-2n8g/step_5/policy/optimizer/optim"


def probe(label, obj):
    try:
        pickle.dumps(obj)
        print(f"  {label:40s} PICKLES OK")
        return True
    except Exception as e:
        print(f"  {label:40s} FAILS: {type(e).__name__}: {e}")
        return False


reader = FileSystemReader(PATH)
md = reader.read_metadata()

tensor_keys, bytes_keys = [], []
for k, v in md.state_dict_metadata.items():
    if type(v).__name__ == "BytesStorageMetadata":
        bytes_keys.append(k)
    else:
        tensor_keys.append(k)

sd = {}
for k in tensor_keys[:20]:
    m = md.state_dict_metadata[k]
    sd[k] = torch.empty(m.size, dtype=m.properties.dtype)
for k in bytes_keys[:20]:
    sd[k] = 0.0

print(f"synthetic state_dict: {len(sd)} entries "
      f"({len(tensor_keys[:20])} tensor + {len(bytes_keys[:20])} bytes)")

planner = DefaultLoadPlanner()
planner.set_up_planner(sd, md, is_coordinator=True)
plan = planner.create_local_plan()
print("\nplan built.")
probe("LoadPlan (whole)", plan)
probe("plan.items", plan.items)
probe("plan.storage_data", getattr(plan, "storage_data", None))
probe("plan.planner_data", getattr(plan, "planner_data", None))

print(f"\nplan.planner_data type: {type(getattr(plan, 'planner_data', None)).__name__}")

# The reader gets to rewrite the plan before it is gathered — the last suspect.
plan2 = reader.prepare_local_plan(plan)
print("\nafter storage_reader.prepare_local_plan():")
probe("LoadPlan (whole)", plan2)
probe("plan2.items", plan2.items)
probe("plan2.storage_data", getattr(plan2, "storage_data", None))
probe("plan2.planner_data", getattr(plan2, "planner_data", None))

if not probe("final check", plan2):
    for i, it in enumerate(plan2.items):
        try:
            pickle.dumps(it)
        except Exception as e:
            print(f"    item[{i}] UNPICKLABLE: {it}")
            print(f"      {type(e).__name__}: {e}")
            break
