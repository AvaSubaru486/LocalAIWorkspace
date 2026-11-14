"""Real base-model inference with network denied and PATH cleared, on the bundled runtime."""
import json
import os
import pathlib
import socket
import sys
import tempfile

from app.config import Settings
from app.models import ModelCatalog, ModelRuntime
from app.knowledge import BUNDLED_WIKIPEDIA, BUNDLED_WIKIPEDIA_ARTICLES, KnowledgeBase


def network_policy(event, args):
    if event == "socket.connect":
        host = args[1][0]
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise PermissionError("Offline smoke test denies non-loopback connections")


def main():
    os.environ["PATH"] = ""
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    sys.addaudithook(network_policy)
    settings = Settings()
    settings.values["max_new_tokens"] = 16
    knowledge = KnowledgeBase(settings)
    wikipedia = next((row for row in knowledge.report()["sources"]
                      if row["kind"] == "bundled-zim" and row["path"] == BUNDLED_WIKIPEDIA), None)
    assert wikipedia and wikipedia["entries"] == BUNDLED_WIKIPEDIA_ARTICLES, wikipedia
    pages = knowledge.search("United States", 2)
    assert pages and pages[0]["url"].startswith("https://en.wikipedia.org/wiki/"), pages
    catalog = ModelCatalog(settings)
    bundled = [model["id"] for model in catalog.entries() if model.get("bundled") and model.get("role") == "chat"]
    assert set(bundled) == {"qwen3-8b", "gemma3-27b-fast", "llama3-70b-q4km-cpu"}, bundled
    runtime = ModelRuntime(settings, catalog)
    try:
        response = "".join(runtime.stream(catalog.get("qwen3-8b"), [{"role": "user", "content": "Reply with exactly: OK"}]))
        assert "OK" in response, response
        first_process = runtime.process
        runtime.unload()
        # A Windows PID can remain visible briefly after exit, or be reused.
        # The owned Popen handle is the identity and termination authority.
        assert first_process.poll() is not None
        response2 = "".join(runtime.stream(catalog.get("qwen3-8b"), [
            {"role": "user", "content": "The secret word is cedar."},
            {"role": "assistant", "content": "Understood."},
            {"role": "user", "content": "What is the secret word? Reply with one word."}]))
        assert "cedar" in response2.lower(), response2
        print(json.dumps({"passed": True, "python": sys.executable, "path": os.environ["PATH"],
                          "nomad_wikipedia": {"articles": wikipedia["entries"], "source": pages[0]["url"]},
                          "bundled_chat_models": bundled, "qwen": response, "history": response2,
                          "process_release": True, "network": "non-loopback denied in harness; model workers use offline/local paths"}, ensure_ascii=False, indent=2))
    finally:
        runtime.unload()


if __name__ == "__main__":
    main()
