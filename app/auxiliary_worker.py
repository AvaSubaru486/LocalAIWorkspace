"""Bounded, offline auxiliary decisions. This worker cannot execute user/model code."""
import contextlib
import json
import sys
from pathlib import Path


def main():
    from app.config import Settings
    import torch
    import laya
    settings = Settings()
    torch.set_num_threads(4)
    request = {"text": "Please describe the attached picture"} if "--self-test" in sys.argv else json.loads(Path(sys.argv[1]).read_text("utf-8"))
    with contextlib.redirect_stdout(sys.stderr):
        agent = laya.load(str(settings.data / "auxiliary"), device="cpu", compile=False)
        result = agent.predict(request["text"][:4000], {
            "task": {"type": "choice", "instructions": "What kind of help is being requested?",
                     "criteria": {"conversation": "normal questions and conversation", "document": "summarizing or analyzing text documents",
                                  "visual": "describing images or videos", "python": "writing or explaining Python code"}}
        })
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        sys.exit(1)
