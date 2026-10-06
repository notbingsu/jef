"""The `jev` command. It parses here, then hands the request to this project's long-running server (client.py), or runs
it in this process with --no-server. It imports nothing heavy, so a request to a warm server starts in milliseconds.
"""

import argparse
import os
import sys
from pathlib import Path

from . import config

DESCRIPTION = (
    "jev <request in plain words>. Jev picks the skill; browser skills drive a real Chrome tab, API skills don't."
)


def load_environment():
    path = Path.cwd() / ".env"
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                key, value = line.split("=", 1)
                os.environ.setdefault(key, value)


def arguments():
    parser = argparse.ArgumentParser(prog="jev", description=DESCRIPTION)
    parser.add_argument("request", nargs="*", help="what you want done, e.g. move the dentist to 5.30pm")
    parser.add_argument("--skill", help="skip routing and run this leaf, e.g. calendar/update-event")
    parser.add_argument("--list", action="store_true", help="show the skill tree")
    parser.add_argument("--route-only", action="store_true", help="show which skill Jev picks, then stop")
    parser.add_argument(
        "--close",
        action=argparse.BooleanOptionalAction,
        help="close a browser skill's tab afterwards; default: on for a background run, off for a visible one",
    )
    parser.add_argument(
        "--trace",
        choices=config.CHOICES["trace"],
        help="how much of this run to record in artifacts/runs/; default: jev.toml's trace (full if unset)",
    )
    parser.add_argument(
        "--background",
        action=argparse.BooleanOptionalAction,
        help="open the tab in the background instead of switching to it; default: the skill's own setting",
    )
    parser.add_argument("--no-server", action="store_true", help="run in this process instead of the jev server")
    parser.add_argument("--stop-server", action="store_true", help="stop the jev server once its current run ends")
    return parser


def parse(argv):
    """Parsed arguments. A request with nothing to do is a usage error here, before any server is involved."""
    parser = arguments()
    args = parser.parse_args(argv)
    if not (args.list or args.skill or args.stop_server or " ".join(args.request).strip()):
        parser.error("say what you want done, e.g. jev move the dentist to 5.30pm")
    return args


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    args = parse(argv)
    load_environment()
    try:
        if args.stop_server:
            from . import client

            return client.stop()
        if config.get("server") and not args.no_server:
            from . import client

            return client.run(argv)
        from . import cli

        return cli.execute(args)
    except (ValueError, RuntimeError) as error:
        raise SystemExit(f"jev: {error}") from None
    except KeyboardInterrupt:
        raise SystemExit("jev: interrupted") from None


if __name__ == "__main__":
    sys.exit(main())
