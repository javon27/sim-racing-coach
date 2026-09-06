"""PyInstaller entry point for the sidecar. A frozen top-level script can't do the
relative imports simcoach/server/__main__.py uses, so this wrapper calls it by an
absolute import. Kept out of the package so it never shadows `python -m simcoach.server`."""
from simcoach.server.__main__ import main

if __name__ == "__main__":
    main()
