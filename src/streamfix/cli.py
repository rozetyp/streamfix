"""Command line: `streamfix repair [FILE]` and `streamfix serve`."""
from __future__ import annotations

import argparse
import json
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="streamfix", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    rep = sub.add_parser("repair", help="repair the JSON in a model response (file or stdin)")
    rep.add_argument("file", nargs="?", help="input file (default: stdin)")
    rep.add_argument("--report", action="store_true", help="print what was repaired to stderr")

    srv = sub.add_parser("serve", help="run the OpenAI-compatible repair proxy")
    srv.add_argument("--host", default="127.0.0.1")
    srv.add_argument("--port", type=int, default=8000)
    srv.add_argument("--upstream", help="upstream base URL (default: $STREAMFIX_UPSTREAM_URL or OpenAI)")

    args = parser.parse_args(argv)

    if args.cmd == "repair":
        from .repair import repair

        try:
            text = open(args.file, encoding="utf-8").read() if args.file else sys.stdin.read()
        except OSError as e:
            print(f"streamfix: {e}", file=sys.stderr)
            return 1
        result = repair(text)
        if args.report:
            print(f"status={result.status} repairs={','.join(result.repairs) or 'none'}", file=sys.stderr)
        if not result.ok:
            print("streamfix: no JSON found", file=sys.stderr)
            return 1
        print(json.dumps(result.value, ensure_ascii=False, indent=2))
        return 0

    try:
        import uvicorn

        from .server import create_app
    except ImportError:
        print("streamfix serve needs the server extra: pip install \"streamfix[server] @ git+https://github.com/rozetyp/streamfix\"", file=sys.stderr)
        return 1
    uvicorn.run(create_app(upstream_url=args.upstream), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
