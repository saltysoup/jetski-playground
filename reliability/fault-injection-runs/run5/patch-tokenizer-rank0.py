p='/opt/nemo-rl/nemo_rl/models/automodel/checkpoint.py'
s=open(p).read()
old = """        if tokenizer_path and tokenizer is not None:
            print(f"Saving tokenizer (or processor) to {tokenizer_path}")
            tokenizer.save_pretrained(tokenizer_path)"""
new = """        if tokenizer_path and tokenizer is not None:
            # Rank-0 only. Every rank calls this with an identical tokenizer_path;
            # on a POSIX fs the duplicate writes are harmless, but on GCS FUSE 16
            # concurrent writers to the same objects raise OSError 116 (stale file
            # handle) and kill the job. Weights/optimizer are unaffected: they are
            # rank-sharded to unique paths.
            import torch.distributed as _dist
            if (not _dist.is_available()) or (not _dist.is_initialized()) or _dist.get_rank() == 0:
                print(f"Saving tokenizer (or processor) to {tokenizer_path}")
                tokenizer.save_pretrained(tokenizer_path)"""
if new in s:
    print("ALREADY PATCHED")
else:
    assert old in s, "ANCHOR NOT FOUND"
    open(p,'w').write(s.replace(old,new))
    print("PATCHED")
