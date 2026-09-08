"""本地翻译模块（基于 Qwen3 小模型）。

功能：
- 将 ASR 转录的文本翻译为目标语言（默认中文）
- 支持 INT4/INT8/FP16 量化，适配不同显存配置
- GPU 显存不足时自动降级到更小模型或 CPU
- 使用 /no_think 模式减少翻译延迟
"""

from __future__ import annotations

import threading
import time
from typing import Optional

# 翻译模型优先级列表：从大到小，显存不足时自动降级
# (模型名称, 量化方式, 显存估算 MB)
_MODEL_CHAIN = [
    ("Qwen/Qwen3-1.7B", "int4", 1500),
    ("Qwen/Qwen3-0.6B", "int4", 700),
]

# 目标语言名称映射（用于翻译 prompt）
_LANG_NAMES = {
    "zh": "Chinese",
    "en": "English",
    "ja": "Japanese",
    "ko": "Korean",
    "fr": "French",
    "de": "German",
    "es": "Spanish",
    "ru": "Russian",
}


def _cuda_available() -> bool:
    try:
        import torch
        return torch.cuda.is_available()
    except Exception:
        return False


def _get_gpu_free_mb() -> int:
    """获取当前 GPU 空闲显存（MB）。"""
    try:
        import torch
        if not torch.cuda.is_available():
            return 0
        free, _ = torch.cuda.mem_get_info(0)
        return free // (1024 * 1024)
    except Exception:
        return 0


class Translator:
    """本地翻译器，基于 Qwen3 小模型。"""

    def __init__(
        self,
        model_name: str = "Qwen/Qwen3-1.7B",
        device: Optional[str] = None,
        target_lang: str = "zh",
        quantize: str = "int4",
        use_gpu: bool = True,
    ) -> None:
        self.model_name = model_name
        self.target_lang = target_lang
        self.quantize = quantize
        self.use_gpu = use_gpu

        # 设备选择
        if device:
            self.device = device
        elif use_gpu and _cuda_available():
            self.device = "cuda"
        else:
            self.device = "cpu"

        self._model = None
        self._tokenizer = None
        self._lock = threading.Lock()
        self._loaded_model_name: Optional[str] = None  # 实际加载的模型名称

    def load(self) -> bool:
        """加载翻译模型。GPU 显存不足时自动降级。"""
        if self._model is not None:
            return True

        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer
        except ImportError as exc:
            print(f"[Translator] 缺少依赖: {exc}")
            return False

        # 确定尝试的模型列表
        models_to_try = []
        if self.device == "cuda":
            free_mb = _get_gpu_free_mb()
            print(f"[Translator] GPU 空闲显存: {free_mb}MB")

            # 从用户指定的模型开始，按显存降级
            start_idx = 0
            for i, (name, _, _) in enumerate(_MODEL_CHAIN):
                if name == self.model_name:
                    start_idx = i
                    break

            for name, quant, est_mb in _MODEL_CHAIN[start_idx:]:
                if est_mb < free_mb - 500:  # 留 500MB 余量
                    models_to_try.append((name, quant, "cuda"))
                else:
                    print(f"[Translator] {name} 需要 ~{est_mb}MB，空闲 {free_mb}MB，跳过")

            # 如果 GPU 都不够，尝试 CPU
            if not models_to_try:
                print("[Translator] GPU 显存不足，尝试 CPU 运行")
                models_to_try.append((self.model_name, self.quantize, "cpu"))
        else:
            models_to_try.append((self.model_name, self.quantize, "cpu"))

        # 逐个尝试加载（与 ASR 共用串行锁，避免二者同时抢显存/CPU 导致失败）
        lock = None
        try:
            from .model_load_lock import model_load_lock
            lock = model_load_lock()
        except Exception:
            lock = None

        def _attempt_all() -> bool:
            for name, quant, device in models_to_try:
                if self._try_load(name, quant, device):
                    return True
            return False

        if lock is not None:
            with lock:
                if _attempt_all():
                    return True
        elif _attempt_all():
            return True

        print("[Translator] 所有模型加载失败")
        return False

    def _try_load(self, model_name: str, quantize: str, device: str) -> bool:
        """尝试加载指定模型。"""
        # 禁用系统代理（避免 SSL 握手问题导致卡死）
        import os
        os.environ["no_proxy"] = "*"
        os.environ["NO_PROXY"] = "*"
        if not os.environ.get("HF_ENDPOINT"):
            os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"

        import requests
        _original_init = requests.Session.__init__
        def _patched_init(self, *args, **kwargs):
            _original_init(self, *args, **kwargs)
            self.trust_env = False
        requests.Session.__init__ = _patched_init

        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

            print(f"[Translator] 加载模型: {model_name} ({quantize}, {device})")

            # 确定本地缓存目录
            local_dir = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "..", "..", "models",
                model_name.replace("/", "_"),
            )

            # 检查本地是否已有完整模型（config.json + 权重文件）
            config_file = os.path.join(local_dir, "config.json")
            has_weights = False
            if os.path.isdir(local_dir):
                has_weights = any(
                    f.endswith((".safetensors", ".bin"))
                    for f in os.listdir(local_dir)
                )

            if os.path.isfile(config_file) and has_weights:
                print(f"[Translator] 使用本地缓存: {local_dir}")
            else:
                # 需要下载模型
                from huggingface_hub import snapshot_download
                print(f"[Translator] 下载模型到: {local_dir}")
                snapshot_download(
                    model_name,
                    local_dir=local_dir,
                    ignore_patterns=["*.md", "*.txt"],
                )

            # Tokenizer
            tokenizer = AutoTokenizer.from_pretrained(
                local_dir, trust_remote_code=True
            )

            # 量化配置
            model_kwargs = {"trust_remote_code": True, "low_cpu_mem_usage": True}

            # 使用本地目录加载
            load_path = local_dir

            if device == "cuda" and quantize == "int4":
                model_kwargs["quantization_config"] = BitsAndBytesConfig(
                    load_in_4bit=True,
                    bnb_4bit_compute_dtype=torch.float16,
                    bnb_4bit_quant_type="nf4",
                    bnb_4bit_use_double_quant=True,
                )
                model_kwargs["device_map"] = "auto"
            elif device == "cuda" and quantize == "int8":
                model_kwargs["quantization_config"] = BitsAndBytesConfig(
                    load_in_8bit=True,
                )
                model_kwargs["device_map"] = "auto"
            elif device == "cuda":
                model_kwargs["torch_dtype"] = torch.float16
                model_kwargs["device_map"] = "auto"
            else:
                # CPU 模式
                model_kwargs["torch_dtype"] = torch.float32
                model_kwargs["device_map"] = "cpu"

            model = AutoModelForCausalLM.from_pretrained(load_path, **model_kwargs)
            model.eval()

            self._model = model
            self._tokenizer = tokenizer
            self._loaded_model_name = model_name
            self.device = device

            # 打印显存使用
            if device == "cuda":
                allocated = torch.cuda.memory_allocated(0) // (1024 * 1024)
                print(f"[Translator] 加载成功，GPU 显存占用: {allocated}MB")
            else:
                print(f"[Translator] 加载成功 (CPU 模式)")

            return True

        except torch.cuda.OutOfMemoryError:
            print(f"[Translator] {model_name} GPU 显存不足")
            import gc
            gc.collect()
            if _cuda_available():
                import torch
                torch.cuda.empty_cache()
            return False

        except Exception as exc:
            print(f"[Translator] 加载 {model_name} 失败: {exc}")
            return False

        finally:
            # 恢复原始 requests.Session.__init__
            requests.Session.__init__ = _original_init

    def translate(self, text: str) -> str:
        """翻译文本到目标语言。

        Args:
            text: 源文本（ASR 转录结果）

        Returns:
            翻译后的文本，失败时返回空字符串
        """
        if not text or not text.strip():
            return ""
        if self._model is None or self._tokenizer is None:
            return ""

        with self._lock:
            try:
                return self._do_translate(text.strip())
            except Exception as exc:
                print(f"[Translator] 翻译失败: {exc}")
                return ""

    def _do_translate(self, text: str) -> str:
        """执行翻译推理。"""
        import torch

        target_name = _LANG_NAMES.get(self.target_lang, self.target_lang)

        # 构造翻译 prompt
        # 使用 /no_think 标签禁用思考模式，减少延迟
        messages = [
            {
                "role": "system",
                "content": (
                    f"You are a professional translator. "
                    f"Translate the user's text to {target_name}. "
                    f"Output ONLY the translation, no explanations."
                ),
            },
            {"role": "user", "content": text},
        ]

        # 应用 chat template
        input_text = self._tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True,
            enable_thinking=False,  # Qwen3: 禁用思考模式
        )

        inputs = self._tokenizer(input_text, return_tensors="pt")
        if self.device == "cuda":
            inputs = {k: v.to("cuda") for k, v in inputs.items()}

        # 生成翻译
        with torch.no_grad():
            outputs = self._model.generate(
                **inputs,
                max_new_tokens=512,
                do_sample=False,  # 贪心解码，翻译更稳定
                temperature=1.0,
                top_p=1.0,
                repetition_penalty=1.1,
            )

        # 解码输出（只取新生成的 token）
        input_len = inputs["input_ids"].shape[1]
        generated = outputs[0][input_len:]
        result = self._tokenizer.decode(generated, skip_special_tokens=True)

        return result.strip()

    def unload(self) -> None:
        """释放模型和显存。"""
        if self._model is not None:
            del self._model
            self._model = None
        if self._tokenizer is not None:
            del self._tokenizer
            self._tokenizer = None
        self._loaded_model_name = None

        import gc
        gc.collect()
        if _cuda_available():
            try:
                import torch
                torch.cuda.empty_cache()
            except Exception:
                pass

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    @property
    def loaded_model(self) -> Optional[str]:
        return self._loaded_model_name

    @property
    def device_type(self) -> str:
        return self.device


def discover_translation_models():
    """列出可用的翻译模型。"""
    return [
        {
            "name": "Qwen/Qwen3-1.7B",
            "label": "Qwen3-1.7B (推荐, ~1.5GB INT4)",
            "size_mb": 1500,
        },
        {
            "name": "Qwen/Qwen3-0.6B",
            "label": "Qwen3-0.6B (省显存, ~700MB INT4)",
            "size_mb": 700,
        },
    ]
