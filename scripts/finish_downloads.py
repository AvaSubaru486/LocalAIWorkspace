"""Recover isolated stalled ranges in smaller pieces, then verify entire artifacts."""
import concurrent.futures
import hashlib
import pathlib
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]


def finish(url, filename, size, digest):
    chunk_size = 8 * 1024 * 1024
    parts = ROOT / "build" / (filename + ".chunks")
    missing = [start for start in range(0, size, chunk_size) if not (parts / str(start)).exists()]
    def fetch(start, end):
        for attempt in range(5):
            try:
                req = urllib.request.Request(url, headers={"Range": f"bytes={start}-{end}", "User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=20) as response:
                    if response.headers.get("Content-Range") != f"bytes {start}-{end}/{size}":
                        raise ValueError("Content range mismatch")
                    data = response.read(end - start + 2)
                if len(data) != end - start + 1:
                    raise ValueError("Incomplete range")
                return start, data
            except Exception as exc:
                print(filename, start, "retry", attempt, str(exc), flush=True)
                if attempt == 4:
                    raise
    ranges = [(start, min(start + 1024 * 1024 - 1, base + chunk_size - 1, size - 1))
              for base in missing for start in range(base, min(base + chunk_size, size), 1024 * 1024)]
    recovered = {}
    with concurrent.futures.ThreadPoolExecutor(8) as executor:
        futures = [executor.submit(fetch, *pair) for pair in ranges]
        for future in concurrent.futures.as_completed(futures):
            start, data = future.result()
            recovered[start] = data
            print(filename, "received", start, len(data), flush=True)
    for base in missing:
        with (parts / str(base)).open("wb") as out:
            for start in range(base, min(base + chunk_size, size), 1024 * 1024):
                out.write(recovered[start])
    target = ROOT / "build" / filename
    hasher = hashlib.sha256()
    with target.with_suffix(target.suffix + ".verified").open("wb") as out:
        for base in range(0, size, chunk_size):
            data = (parts / str(base)).read_bytes()
            out.write(data)
            hasher.update(data)
    if hasher.hexdigest() != digest:
        raise ValueError("Final SHA256 mismatch")
    target.with_suffix(target.suffix + ".verified").replace(target)
    print("VERIFIED", filename, size, digest, flush=True)


if __name__ == "__main__":
    tasks = [
        ("https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip", "ffmpeg-ranged.zip", 114768076, "60f467265b1e312373dbcd92200c2618a74850f98d3d078e94296bb3fa2047ba"),
        ("https://download-r2.pytorch.org/whl/cu124/torch-2.6.0%2Bcu124-cp311-cp311-win_amd64.whl", "torch-2.6.0+cu124-cp311-cp311-win_amd64.whl", 2532350702, "6a1fb2714e9323f11edb6e8abf7aad5f79e45ad25c081cde87681a18d99c29eb"),
    ]
    with concurrent.futures.ThreadPoolExecutor(2) as executor:
        futures = [executor.submit(finish, *task) for task in tasks]
        for future in futures:
            future.result()
