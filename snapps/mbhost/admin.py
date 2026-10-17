"""Host moderation CLI: writes signed hide/unhide/remove records into the host's store.
  remove deletes the post text at once (tombstone + public record); hide keeps it.
  python3 -m mbhost.admin --data DIR --board demo hide post <id> --reason spam
  python3 -m mbhost.admin --data DIR --board demo unhide author <hexkey>
The running host serves the new record (and bumps checkpoint.rseq) on its next request."""
import argparse
import json

from .host import Host, load_or_make_keys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--board", default="demo")
    ap.add_argument("action", choices=["hide", "unhide", "remove"])
    ap.add_argument("target_type", choices=["post", "author"])
    ap.add_argument("target")
    ap.add_argument("--reason", default="")
    a = ap.parse_args()
    _, hk, d = load_or_make_keys(a.data, a.board)
    print(json.dumps(Host(a.data, a.board, hk, d).moderate(a.action, a.target_type, a.target,
                                                           a.reason)))


if __name__ == "__main__":
    main()
