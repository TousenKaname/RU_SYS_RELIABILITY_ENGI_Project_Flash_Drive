"""Allow ``python -m flashrel``.

The guard matters: worker processes are started with the *spawn* method, which
re-imports the main module, and must not run the command line again.
"""

from flashrel.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
