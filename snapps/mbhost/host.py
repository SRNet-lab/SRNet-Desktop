"""Message-board host = generic typed collection host with the board (msgboard v1) codec."""
from typed.host import (DEFAULT_POLICY, Host, load_or_make_keys, load_policy, main)  # noqa: F401
from typed.node import bind_ok, serve, url_of  # noqa: F401

if __name__ == "__main__":
    main()
