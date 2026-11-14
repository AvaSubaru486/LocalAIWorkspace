import concurrent.futures
import pathlib
import shutil
import zipfile

from scripts.provision import ROOT, BUILD, fetch


def archive(url, name, target, basenames=None):
    source = fetch(url, BUILD / name)
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(source) as package:
        for item in package.infolist():
            filename = pathlib.PurePosixPath(item.filename).name
            if not filename or item.is_dir():
                continue
            if basenames is not None and filename not in basenames:
                continue
            if basenames is None and not filename.endswith((".exe", ".dll")) and not filename.startswith("LICENSE"):
                continue
            with package.open(item) as src, (target / filename).open("wb") as out:
                shutil.copyfileobj(src, out)
    print("Installed", name, flush=True)


if __name__ == "__main__":
    tasks = [
        ("https://github.com/GyanD/codexffmpeg/releases/download/9.0.2/ffmpeg-9.0.2-essentials_build.zip", "ffmpeg-github.zip", ROOT / "runtime/ffmpeg", {"ffmpeg.exe", "ffprobe.exe", "LICENSE", "README.txt"}),
        ("https://github.com/ggml-org/llama.cpp/releases/download/b11435/llama-b11435-bin-win-cuda-12.4-x64.zip", "llama-cuda.zip", ROOT / "runtime/llama", None),
        ("https://github.com/ggml-org/llama.cpp/releases/download/b11435/cudart-llama-bin-win-cuda-12.4-x64.zip", "llama-cudart.zip", ROOT / "runtime/llama", None),
    ]
    with concurrent.futures.ThreadPoolExecutor(3) as executor:
        futures = [executor.submit(archive, *task) for task in tasks]
        for future in concurrent.futures.as_completed(futures):
            try:
                future.result()
            except Exception as exc:
                print("FAILED", repr(exc), flush=True)
