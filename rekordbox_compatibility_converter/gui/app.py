"""Entrypoint for Rekordbox Format Checker GUI."""

import os


def main():
    try:
        from rekordbox_compatibility_converter.gui.modern_app import main as modern_main
    except ImportError as exc:
        raise SystemExit(
            "The GUI requires CustomTkinter. Install project dependencies with 'uv sync'."
        ) from exc
    if os.environ.get("RBCONVERT_SMOKE_TEST") == "1":
        return
    modern_main()


if __name__ == "__main__":
    main()
