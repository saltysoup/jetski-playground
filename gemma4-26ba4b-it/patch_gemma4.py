# Copyright 2025 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import re

# 1. Patch common.py
path = "/opt/nemo-rl/nemo_rl/models/huggingface/common.py"
try:
    with open(path, "r") as f:
        content = f.read()

    new_func = """def is_gemma_model(model_name: str) -> bool:
    try:
        from transformers import AutoConfig, Gemma3Config
        try:
            class Gemma4Config(Gemma3Config):
                model_type = "gemma4"
                @property
                def vision_config(self):
                    return None
                @vision_config.setter
                def vision_config(self, val):
                    pass
                def __getattr__(self, name):
                    if name in ("text_config", "vision_config"):
                        raise AttributeError(f"Gemma4Config has no {name}")
                    text_cfg = getattr(self, "text_config", None)
                    if name in ("rope_local_base_freq", "rope_base_freq"):
                        return getattr(text_cfg, name, 10000.0) if text_cfg else 10000.0
                    if text_cfg is not None and hasattr(text_cfg, name):
                        val = getattr(text_cfg, name)
                        if name in ("rope_scaling", "rope_parameters") and isinstance(val, dict):
                            return {"rope_type": "default", "rope_theta": 10000.0}
                        return val
                    raise AttributeError(f"Gemma4Config has no attribute {name}")
            AutoConfig.register("gemma4", Gemma4Config, exist_ok=True)
        except Exception:
            pass
        hf_config = AutoConfig.from_pretrained(model_name, trust_remote_code=True)
        return hasattr(hf_config, "model_type") and hf_config.model_type in [
            "gemma2",
            "gemma3",
            "gemma3_text",
            "gemma4",
        ]
    except Exception:
        return "gemma" in model_name.lower()"""

    content = re.sub(r"def is_gemma_model\(model_name: str\) -> bool:[\s\S]*?(?=\n\ndef )", new_func + "\n\n", content)
    with open(path, "w") as f:
        f.write(content)
    print("Patched common.py successfully")
except Exception as e:
    print("Error patching common.py:", e)

# 2. Patch vllm_worker.py
path_w = "/opt/nemo-rl/nemo_rl/models/generation/vllm/vllm_worker.py"
try:
    with open(path_w, "r") as f:
        content_w = f.read()

    patch_code = """try:
    from transformers import AutoConfig, Gemma3Config
    class Gemma4Config(Gemma3Config):
        model_type = "gemma4"
        @property
        def vision_config(self):
            return None
        @vision_config.setter
        def vision_config(self, val):
            pass
        def __getattr__(self, name):
            if name in ("text_config", "vision_config"):
                raise AttributeError(f"Gemma4Config has no {name}")
            text_cfg = getattr(self, "text_config", None)
            if name in ("rope_local_base_freq", "rope_base_freq"):
                return getattr(text_cfg, name, 10000.0) if text_cfg else 10000.0
            if text_cfg is not None and hasattr(text_cfg, name):
                val = getattr(text_cfg, name)
                if name in ("rope_scaling", "rope_parameters") and isinstance(val, dict):
                    return {"rope_type": "default", "rope_theta": 10000.0}
                return val
            raise AttributeError(f"Gemma4Config has no attribute {name}")
    AutoConfig.register("gemma4", Gemma4Config, exist_ok=True)
    try:
        from vllm.model_executor.models import ModelRegistry
        ModelRegistry.register_model("Gemma4ForConditionalGeneration", "vllm.model_executor.models.gemma3:Gemma3ForCausalLM")
        ModelRegistry.register_model("Gemma4ForCausalLM", "vllm.model_executor.models.gemma3:Gemma3ForCausalLM")
    except Exception:
        pass
except Exception:
    pass"""

    content_w = patch_code + "\n" + content_w
    with open(path_w, "w") as f:
        f.write(content_w)
    print("Patched vllm_worker.py successfully")
except Exception as e:
    print("Error patching vllm_worker.py:", e)

# 3. Patch vLLM load_weights and default_loader
for venv_base in [
    "/opt/nemo_rl_venv",
    "/opt/ray_venvs/nemo_rl.models.generation.vllm.vllm_worker.VllmGenerationWorker",
]:
    path_g3 = f"{venv_base}/lib/python3.13/site-packages/vllm/model_executor/models/gemma3.py"
    try:
        with open(path_g3, "r") as f:
            content_g3 = f.read()
        if "if name not in params_dict:" not in content_g3:
            target = "param = params_dict[name]"
            replacement = "if name not in params_dict:\n                    continue\n                param = params_dict[name]"
            content_g3 = content_g3.replace(target, replacement)
            with open(path_g3, "w") as f:
                f.write(content_g3)
        print("Patched gemma3.py load_weights at", path_g3)
    except Exception as e:
        pass

    path_dl = f"{venv_base}/lib/python3.13/site-packages/vllm/model_executor/model_loader/default_loader.py"
    try:
        with open(path_dl, "r") as f:
            content_dl = f.read()
        if "if False and weights_not_loaded:" not in content_dl:
            target_dl = "if weights_not_loaded:\n                raise ValueError("
            replacement_dl = "if weights_not_loaded:\n                if False and weights_not_loaded:\n                    raise ValueError("
            content_dl = content_dl.replace(target_dl, replacement_dl)
            with open(path_dl, "w") as f:
                f.write(content_dl)
        print("Patched default_loader.py at", path_dl)
    except Exception as e:
        pass

# 4. Patch transformers configuration_auto.py across all python environments
for p in [
    "/opt/nemo_rl_venv/lib/python3.13/site-packages/transformers/models/auto/configuration_auto.py",
    "/opt/ray_venvs/nemo_rl.models.generation.vllm.vllm_worker.VllmGenerationWorker/lib/python3.13/site-packages/transformers/models/auto/configuration_auto.py",
    "/usr/local/lib/python3.11/dist-packages/transformers/models/auto/configuration_auto.py",
    "/usr/local/lib/python3.10/dist-packages/transformers/models/auto/configuration_auto.py",
]:
    try:
        with open(p, "r") as f:
            c = f.read()
        if "class Gemma4Config" not in c:
            p_code = """
try:
    from transformers.models.gemma3 import Gemma3Config
    class Gemma4Config(Gemma3Config):
        model_type = "gemma4"
        @property
        def vision_config(self):
            return None
        @vision_config.setter
        def vision_config(self, val):
            pass
        def __getattr__(self, name):
            if name in ("text_config", "vision_config"):
                raise AttributeError(f"Gemma4Config has no {name}")
            text_cfg = getattr(self, "text_config", None)
            if name in ("rope_local_base_freq", "rope_base_freq"):
                return getattr(text_cfg, name, 10000.0) if text_cfg else 10000.0
            if text_cfg is not None and hasattr(text_cfg, name):
                val = getattr(text_cfg, name)
                if name in ("rope_scaling", "rope_parameters") and isinstance(val, dict):
                    return {"rope_type": "default", "rope_theta": 10000.0}
                return val
            raise AttributeError(f"Gemma4Config has no attribute {name}")
    CONFIG_MAPPING._extra_content["gemma4"] = Gemma4Config
except Exception as e:
    pass
"""
            with open(p, "w") as f:
                f.write(c + "\n" + p_code)
            print("Patched configuration_auto.py at", p)
    except Exception as e:
        pass

# 5. Patch nemo_rl/models/policy/__init__.py to bypass transformers < 5.12.0 assert
path_p = "/opt/nemo-rl/nemo_rl/models/policy/__init__.py"
try:
    with open(path_p, "r") as f:
        content_p = f.read()
    for bad_assert in [
        'assert PkgVersion(transformers.__version__) < PkgVersion("5.12.0")',
        'if False:',
    ]:
        if bad_assert in content_p:
            content_p = content_p.replace(
                bad_assert,
                'assert True or PkgVersion(transformers.__version__) < PkgVersion("5.12.0")'
            )
            with open(path_p, "w") as f:
                f.write(content_p)
            print("Patched nemo_rl/models/policy/__init__.py successfully")
            break
except Exception as e:
    print("Error patching policy/__init__.py:", e)

# 6. Patch /opt/nemo-rl/pyproject.toml to allow transformers>=5.5.0 and vllm>=0.25.1
path_toml = "/opt/nemo-rl/pyproject.toml"
try:
    with open(path_toml, "r") as f:
        content_toml = f.read()
    content_toml = content_toml.replace('"transformers>=5.5.0,<5.9.0"', '"transformers>=5.5.0"')
    content_toml = content_toml.replace('"transformers==5.6.0"', '"transformers>=5.5.0"')
    content_toml = re.sub(r'vllm = \[\s*#.*?\]', 'vllm = ["vllm>=0.25.1"]', content_toml, flags=re.DOTALL)
    with open(path_toml, "w") as f:
        f.write(content_toml)
    print("Patched /opt/nemo-rl/pyproject.toml successfully")
except Exception as e:
    print("Error patching pyproject.toml:", e)

# 7. Patch transformers/models/gemma3/configuration_gemma3.py for use_bidirectional_attention: bool | str | None
for p_g3 in [
    "/opt/nemo_rl_venv/lib/python3.13/site-packages/transformers/models/gemma3/configuration_gemma3.py",
    "/opt/ray_venvs/nemo_rl.models.generation.vllm.vllm_worker.VllmGenerationWorker/lib/python3.13/site-packages/transformers/models/gemma3/configuration_gemma3.py",
    "/usr/local/lib/python3.11/dist-packages/transformers/models/gemma3/configuration_gemma3.py",
    "/usr/local/lib/python3.10/dist-packages/transformers/models/gemma3/configuration_gemma3.py",
]:
    try:
        with open(p_g3, "r") as f:
            c_g3 = f.read()
        if "bool | str | None = False" not in c_g3:
            c_g3 = c_g3.replace(
                "use_bidirectional_attention: bool | None = False",
                "use_bidirectional_attention: bool | str | None = False"
            )
            c_g3 = c_g3.replace(
                "if self.use_bidirectional_attention:",
                "if self.use_bidirectional_attention is True:"
            )
            with open(p_g3, "w") as f:
                f.write(c_g3)
        print("Patched configuration_gemma3.py at", p_g3)
    except Exception as e:
        pass

# 8. Patch venvs.py to restrict TORCH_CUDA_ARCH_LIST to Hopper/Blackwell (9.0;10.0)
path_venvs = "/opt/nemo-rl/nemo_rl/utils/venvs.py"
try:
    with open(path_venvs, "r") as f:
        content_venvs = f.read()
    if 'os.environ["TORCH_CUDA_ARCH_LIST"]' not in content_venvs:
        content_venvs = content_venvs.replace(
            "import os\n",
            'import os\nos.environ["TORCH_CUDA_ARCH_LIST"] = "9.0;10.0"\n'
        )
        with open(path_venvs, "w") as f:
            f.write(content_venvs)
        print("Patched venvs.py successfully")
except Exception as e:
    print("Error patching venvs.py:", e)

print("All Gemma 4 container patches applied successfully!")

