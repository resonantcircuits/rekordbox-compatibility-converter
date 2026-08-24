"""Entrypoint for Rekordbox Format Checker GUI."""

import os


def main():
    smoke_mode = os.environ.get("RBCONVERT_SMOKE_TEST")
    if smoke_mode == "conversion":
        from rekordbox_compatibility_converter.packaging_smoke import (
            run_frozen_conversion_smoke_test,
        )

        run_frozen_conversion_smoke_test()
        return

    try:
        from rekordbox_compatibility_converter.gui.modern_app import main as modern_main
    except ImportError as exc:
        raise SystemExit(
            "The GUI requires CustomTkinter. Install project dependencies with 'uv sync'."
        ) from exc
    if smoke_mode == "1":
        return
    modern_main()


if __name__ == "__main__":
    main()
