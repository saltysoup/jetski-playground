"""Find the code object DCP can't pickle when loading the optimizer shard.

dcp.load builds a LoadPlan per rank and gathers it with pickle. Rebuild that
plan single-rank against the real step_5 optimizer shard and pickle each piece
until something fails. No GPUs, no distributed init.
"""

import pickle
import traceback

from torch.distributed.checkpoint import FileSystemReader

PATH = "/gcs/ckpt/dapo-gemma3-27b-it-2n8g/step_5/policy/optimizer/optim"


def probe(label, obj):
    try:
        pickle.dumps(obj)
        print(f"  {label:34s} PICKLES OK")
        return True
    except Exception as e:
        print(f"  {label:34s} FAILS: {type(e).__name__}: {e}")
        return False


reader = FileSystemReader(PATH)
md = reader.read_metadata()

print("metadata:")
probe("Metadata (whole)", md)
probe("md.state_dict_metadata", md.state_dict_metadata)
probe("md.planner_data", md.planner_data)
probe("md.storage_data", md.storage_data)

print(f"\nmd.planner_data type: {type(md.planner_data).__name__}")
if isinstance(md.planner_data, dict):
    for k, v in list(md.planner_data.items())[:5]:
        print(f"  {k!r} -> {type(v).__name__} {v!r}"[:160])

print("\nentries in state_dict_metadata (first 8):")
for i, (k, v) in enumerate(md.state_dict_metadata.items()):
    if i >= 8:
        break
    print(f"  {k!r:60s} {type(v).__name__}")
print(f"  ... {len(md.state_dict_metadata)} entries total")

# Anything non-tensor is stored as a pickled blob; those are the suspects.
print("\nnon-tensor (BytesStorageMetadata) entries:")
bytes_entries = [
    k for k, v in md.state_dict_metadata.items() if type(v).__name__ == "BytesStorageMetadata"
]
for k in bytes_entries:
    print(f"  {k!r}")
print(f"  ({len(bytes_entries)} of {len(md.state_dict_metadata)})")

# Read each blob back and see which one carries a code object.
if bytes_entries:
    print("\nreading blobs back:")
    import io

    from torch.distributed.checkpoint.planner import LoadItemType, ReadItem
    from torch.distributed.checkpoint.metadata import MetadataIndex

    for k in bytes_entries:
        try:
            buf = io.BytesIO()
            item = ReadItem(
                type=LoadItemType.BYTE_IO,
                dest_index=MetadataIndex(fqn=k),
                dest_offsets=None,
                storage_index=MetadataIndex(fqn=k),
                storage_offsets=None,
                lengths=None,
            )
            print(f"  {k!r}: constructed ReadItem ok")
        except Exception as e:
            print(f"  {k!r}: {type(e).__name__}: {e}")
