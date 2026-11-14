"""AirLLM lives in a dedicated process so switching models releases CUDA and RAM."""
import contextlib
import json
import sys
import threading
import time
import traceback

wire = sys.stdout
wire_lock = threading.Lock()


def emit(data):
    with wire_lock:
        wire.write(json.dumps(data, ensure_ascii=False) + "\n")
        wire.flush()


def decode_metrics(count, first, now, started):
    elapsed = max(0.0, now - first) if first is not None else 0.0
    return {"generated_tokens": count,
            "first_token_seconds": round(first - started, 3) if first is not None else None,
            "generation_seconds": round(elapsed, 3),
            # The first token is produced by prompt evaluation; decode speed
            # measures only subsequent tokens, like the GGUF timing stream.
            "tokens_per_second": (count - 1) / elapsed if count > 1 and elapsed > 0 else None}


def main():
    from app.config import Settings
    from app.models import ModelCatalog
    settings = Settings()
    catalog = ModelCatalog(settings)
    spec = catalog.get(sys.argv[1])
    request = json.loads(sys.stdin.readline())
    with contextlib.redirect_stdout(sys.stderr):
        import torch
        from airllm import AutoModel
        from transformers import TextIteratorStreamer
        if not torch.cuda.is_available():
            raise ValueError("AirLLM 分层 GPU 推理需要 NVIDIA 驱动和可用 CUDA；普通内置 Qwen3 可使用 CPU。")
        total = torch.cuda.get_device_properties(0).total_memory / 1024**3
        torch.cuda.set_per_process_memory_fraction(min(0.95, settings["gpu_memory_gb"] / total))
        model = AutoModel.from_pretrained(str(catalog.directory(spec)),
                                         compression=spec["quantization"] if spec["quantization"] in {"4bit", "8bit"} else None,
                                         layer_shards_saving_path=str(settings.data / "offload" / spec["id"]),
                                         prefetching=False, delete_original=False, max_seq_len=settings["context_length"])
        tokenizer = model.tokenizer
        prompt = tokenizer.apply_chat_template(request["messages"], tokenize=False, add_generation_prompt=True)
        tokens = tokenizer(prompt, return_tensors="pt", truncation=False)
        if tokens["input_ids"].shape[-1] + request["max_new_tokens"] > settings["context_length"]:
            raise ValueError("当前输入超过模型上下文长度，请缩短内容或增大上下文设置；没有截断消息。")
        class CountingStreamer(TextIteratorStreamer):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.generated_tokens = 0
                self._seen_prompt = False
                self._counter_lock = threading.Lock()
                self.first_token_at = None
                self.last_metrics_at = 0.0

            def put(self, value):
                count = int(value.numel()) if hasattr(value, "numel") else len(value)
                with self._counter_lock:
                    if not self._seen_prompt:
                        self._seen_prompt = True
                    else:
                        self.generated_tokens += count
                        self.first_token_at = self.first_token_at or time.perf_counter()
                        now = time.perf_counter()
                        if now - self.last_metrics_at >= 0.3:
                            emit({"metrics": decode_metrics(self.generated_tokens, self.first_token_at, now, started)})
                            self.last_metrics_at = now
                return super().put(value)

        streamer = CountingStreamer(tokenizer, skip_prompt=True, skip_special_tokens=True, timeout=600)
        errors = []
        started = time.perf_counter()
        first_text = None
        def generate():
            try:
                with torch.inference_mode():
                    model.generate(tokens["input_ids"].cuda(), max_new_tokens=request["max_new_tokens"],
                                   use_cache=True, streamer=streamer)
            except Exception as exc:
                errors.append(exc)
                streamer.end()
        thread = threading.Thread(target=generate, daemon=True)
        thread.start()
        for chunk in streamer:
            if chunk:
                first_text = first_text or time.perf_counter()
                emit({"delta": chunk})
        thread.join()
        if errors:
            raise errors[0]
        finished = time.perf_counter()
        with streamer._counter_lock:
            generated_tokens = streamer.generated_tokens
        with streamer._counter_lock:
            token_started = streamer.first_token_at
        metrics = decode_metrics(generated_tokens, token_started, finished, started)
        emit({"metrics": metrics})
        emit({"done": True, "metrics": metrics})


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        emit({"error": str(exc)})
