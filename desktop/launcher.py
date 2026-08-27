"""Desktop launcher for the e2etrans web platform.

Double-click: opens https://obbot.tpcnailab.com/v2/ in the default
browser, where the user signs in with their corporate (Azure AD)
account. The URL can be overridden as the first command line argument.

Built as a single Windows exe via PyInstaller (see
.github/workflows/build-exe.yml, job "build-launcher").
"""

import sys
import webbrowser

DEFAULT_URL = "https://obbot.tpcnailab.com/v2/"


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    try:
        webbrowser.open(url, new=2)
        return 0
    except Exception as error:
        print(f"无法打开浏览器: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    if getattr(sys, "frozen", False):
        import multiprocessing

        multiprocessing.freeze_support()
    sys.exit(main())
