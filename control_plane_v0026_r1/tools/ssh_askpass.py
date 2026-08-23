#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Minimal SSH_ASKPASS bridge for protected rollout credentials."""

import os
import sys

from pathlib import Path

from credential_reader import load_credential


def main():
    credentials_file = os.environ.get("AUTOMATION_CREDENTIALS_FILE", "")
    credential_key = os.environ.get("AUTOMATION_SSH_CREDENTIAL_KEY", "")
    if not credentials_file or not credential_key:
        print("SSH credential environment is incomplete", file=sys.stderr)
        return 67
    try:
        value = load_credential(
            Path(credentials_file),
            (credential_key, "ssh." + credential_key),
        )
    except (ValueError, OSError) as error:
        print("credentials error: " + str(error), file=sys.stderr)
        return 65
    sys.stdout.write(value + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
