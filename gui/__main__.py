"""
gui/__main__.py — Entry point for the OCR correction GUI.

Usage:
  python -m gui                                    # opens blank, use File > Open
  python -m gui --config-path collection.json      # open via collection.json
  python -m gui --collection-path /path/to/coll/  # open via directory path
  python -m gui --collection-path /path/ --ark metapth1478562 --page 3
"""

import argparse
import sys
from pathlib import Path


def main():
    p = argparse.ArgumentParser(
        prog="python -m gui",
        description="UNT Archive OCR Correction GUI",
    )
    p.add_argument("--collection-path", default=None,
                   help="Path to the collection directory (contains collection.json)")
    p.add_argument("--config-path", default=None,
                   help="Path to collection.json (directory is derived from it)")
    p.add_argument("--ark", default=None,
                   help="Open directly to this ARK ID")
    p.add_argument("--page", type=int, default=1,
                   help="Page number to open (default: 1)")
    args = p.parse_args()

    # Resolve collection directory
    collection_dir = None
    if args.collection_path:
        collection_dir = Path(args.collection_path)
    elif args.config_path:
        collection_dir = Path(args.config_path).parent
    # else: user opens via File menu

    # Check DPG is installed
    try:
        import dearpygui.dearpygui  # noqa: F401
    except ImportError:
        print("ERROR: Dear PyGui is not installed.")
        print("Install with:  pip install dearpygui")
        sys.exit(1)

    from .app import App
    app = App()

    if collection_dir and args.ark:
        # Pre-navigate after load
        original_load = app.state.load_collection

        def _patched_load(path):
            original_load(path)
            # Find ark in issue list
            for i, issue in enumerate(app.state.issues):
                if issue.get("ark_id") == args.ark:
                    app.state.go_to_issue(i, page=args.page)
                    return
            # Not found — just load the page
            app.state.go_to_page(args.page)

        app.state.load_collection = _patched_load

    app.run(collection_path=collection_dir)


if __name__ == "__main__":
    main()
