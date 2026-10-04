"""Entry point for the packaged HogWatch.exe (PyInstaller needs a script outside the package).

Double-clicking the exe runs `launch`: it starts HogWatch in the background as
administrator and opens the dashboard. All the usual commands still work, e.g.
`HogWatch.exe stop` or `HogWatch.exe installer-report`.
"""

import sys

from hogwatch.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
