"""One-time setup of the LaTeX engine used for the PDF preview. run.bat calls this (`python -m paperscriptor setup-latex`) before starting the app.

If MiKTeX, TeX Live or Tectonic is already installed, nothing happens. Otherwise Tectonic (a free, single-file
LaTeX engine, about 21 MB) is downloaded from its official GitHub release into tools/tectonic and checked against
its published SHA-256 checksum. Set PAPERSCRIPTOR_NO_LATEX=1 to skip this.
"""

import os
import sys

from . import latex


def main() -> int:
    if os.environ.get("PAPERSCRIPTOR_NO_LATEX") == "1":
        return 0
    found = latex.engine()
    if found:
        print(f"PDF preview: using {found}.")
        return 0
    if os.name != "nt":
        print("PDF preview: no LaTeX found. Install TeX Live or Tectonic to compile PDFs.")
        return 0
    print(f"PDF preview: setting up Tectonic {latex.TECTONIC_VERSION} (about 21 MB, one time only)...")
    try:
        latex.download_tectonic()
    except Exception as e:
        print(f"PDF preview: setup failed ({e}). The app still works; press 'Set up PDF preview' in the editor to retry.")
        return 0
    print("PDF preview: ready. The first compile downloads the LaTeX packages it needs, so it can take a few minutes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
