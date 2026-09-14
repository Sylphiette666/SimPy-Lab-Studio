"""Frozen desktop entry: multiprocessing dispatch must precede GUI initialization."""

if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()
    from simlab.desktop import main

    raise SystemExit(main())
