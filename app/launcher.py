"""Portable native desktop entry; no browser or application HTTP server."""
import argparse
import sys
import os

from .config import Settings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--smoke-test", action="store_true")
    parser.add_argument("--report")
    parser.add_argument("--screenshot")
    args = parser.parse_args()
    # PySide6's Windows bootstrap expects PATH to exist even when the product
    # deliberately runs with an empty environment search path.
    os.environ.setdefault("PATH", "")
    settings = Settings()
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    qt = settings.root / "runtime/python/Lib/site-packages/PySide6"
    os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = str(qt / "plugins/platforms")
    os.environ.pop("QT_PLUGIN_PATH", None)
    if args.smoke_test:
        from .desktop_smoke import run
        return run(settings, args.report, args.screenshot)
    from .desktop import run
    return run(settings)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback
        settings = Settings()
        (settings.data / "logs/startup-error.log").write_text(traceback.format_exc(), "utf-8")
        raise
