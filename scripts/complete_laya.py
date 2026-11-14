"""Fetch the local tokenizer/config bundle matching the existing 0.4B checkpoint."""
import concurrent.futures
from scripts.provision import ROOT, fetch


def one(subdir, name):
    target = ROOT / "data/auxiliary" / subdir / name
    urls = [f"https://hf-mirror.com/convaiinnovations/laya/resolve/main/{subdir}/{name}",
            f"https://hf-mirror.com/answerdotai/ModernBERT-large/resolve/main/{name}"]
    for url in urls:
        try:
            fetch(url, target)
            return
        except Exception as exc:
            print(url, str(exc), flush=True)
    raise RuntimeError("Missing Laya resource " + str(target))


if __name__ == "__main__":
    with concurrent.futures.ThreadPoolExecutor(4) as executor:
        tasks = [executor.submit(one, subdir, name) for subdir, name in [
            ("encoder", "config.json"), ("tokenizer", "tokenizer.json"),
            ("tokenizer", "tokenizer_config.json"), ("tokenizer", "special_tokens_map.json")]]
        for task in tasks:
            task.result()
