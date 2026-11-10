"""Print response headers and status for one URL, stdlib only.

Installed by the project-local recipe: no system pip, no venv, just this file
under the state directory, run by the interpreter that is already here.
"""

import sys
import urllib.error
import urllib.request


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: http_headers.py <url>", file=sys.stderr)
        return 2
    request = urllib.request.Request(argv[0], method="GET")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            print(f"# status {response.status} {response.reason}")
            for name, value in response.headers.items():
                print(f"{name}: {value}")
            body = response.read(4096).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        print(f"# status {exc.code} {exc.reason}")
        for name, value in exc.headers.items():
            print(f"{name}: {value}")
        return 0
    except (urllib.error.URLError, OSError) as exc:
        print(f"# error {exc}", file=sys.stderr)
        return 1
    print()
    print(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
