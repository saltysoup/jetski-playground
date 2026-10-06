"""Isolation test for gcsfuse directory rename on /gcs/ckpt.

NeMo-RL's checkpoint.finalize_checkpoint() does os.rename(tmp_step_N -> step_N).
On a non-HNS bucket gcsfuse refuses directory renames unless rename-dir-limit is
set, and reports the refusal as EMFILE ("Too many open files"), which reads like
fd exhaustion but is not. This reproduces the rename in ~2s instead of the ~25min
a training run takes to reach step 5.
"""

import os
import shutil
import socket
import time

BASE = "/gcs/ckpt/_renametest_" + socket.gethostname()[-5:]


def attempt(label, src, dst):
    t = time.time()
    try:
        os.rename(src, dst)
    except OSError as e:
        print(f"{label:8s} RENAME FAIL errno={e.errno} ({e.strerror})")
        return False
    dt = time.time() - t
    print(f"{label:8s} RENAME OK   {dt:.2f}s  dst_exists={os.path.isdir(dst)}")
    return True


shutil.rmtree(BASE, ignore_errors=True)

for n in (1, 50):
    src, dst = f"{BASE}/src_{n}", f"{BASE}/dst_{n}"
    os.makedirs(src, exist_ok=True)
    for i in range(n):
        with open(f"{src}/f{i}.bin", "w") as f:
            f.write("x" * 1024)
    if attempt(f"n={n}", src, dst):
        print(f"         files preserved: {len(os.listdir(dst))}/{n}")

# nested layout, shaped like a real checkpoint dir
src, dst = f"{BASE}/src_nested", f"{BASE}/dst_nested"
os.makedirs(f"{src}/policy/optimizer/optim", exist_ok=True)
os.makedirs(f"{src}/policy/tokenizer", exist_ok=True)
for i in range(6):
    with open(f"{src}/policy/optimizer/optim/shard{i}.bin", "w") as f:
        f.write("y" * 1024)
with open(f"{src}/policy/tokenizer/tokenizer.json", "w") as f:
    f.write("{}")
if attempt("nested", src, dst):
    shards = os.listdir(f"{dst}/policy/optimizer/optim")
    print(f"         shards preserved: {len(shards)}/6")

shutil.rmtree(BASE, ignore_errors=True)
