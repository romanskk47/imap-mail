#!/usr/bin/env python3
"""Portable command-line entrypoint; credentials stay outside this package."""

from imap_mail.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
