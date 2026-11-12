if __name__ == '__main__':
    from pathlib import Path
    import sys

    root = str(Path(__file__).resolve().parent)
    if sys.path[0] != root:
        sys.path.insert(0, root)

    from deathnote.cli.main import main

    raise SystemExit(main())
