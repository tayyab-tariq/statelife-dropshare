"""Disable the `perf-…` test accounts the performance scripts made (OQ-40), through the
server's own service (audited as `cli`). Their files stay until an administrator deletes the
accounts.

    uv run python scripts/perf/cleanup.py
"""

import argparse

from perf_common import add_target_arguments, check_target, disable_test_users, say


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    add_target_arguments(parser)
    check_target(parser.parse_args())
    names = disable_test_users()
    say(f"Disabled {len(names)} test account(s): {', '.join(names) or 'none'}")


if __name__ == "__main__":
    main()
