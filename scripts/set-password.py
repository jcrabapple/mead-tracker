#!/usr/bin/env python3
"""Manage Mead Tracker credentials in ~/.config/mead-tracker/env.

  set-password.py            prompt (hidden) for a new UI password
  set-password.py --random   generate a random password, write it to
                             ~/.config/mead-tracker/credentials (0600)
  set-password.py --rotate-token   issue a new API bearer token

Restart the service afterwards: systemctl --user restart mead-tracker
"""
import getpass
import os
import secrets
import sys

from werkzeug.security import generate_password_hash

CONF_DIR = os.path.expanduser("~/.config/mead-tracker")
ENV_PATH = os.path.join(CONF_DIR, "env")
CRED_PATH = os.path.join(CONF_DIR, "credentials")


def load():
    env = {}
    if os.path.exists(ENV_PATH):
        for line in open(ENV_PATH):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k] = v.strip().strip("'\"")
    return env


def save(env):
    os.makedirs(CONF_DIR, mode=0o700, exist_ok=True)
    fd = os.open(ENV_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("# Mead Tracker secrets. Managed by scripts/set-password.py\n")
        for k, v in env.items():
            # single quotes: werkzeug hashes contain '$'
            f.write(f"{k}='{v}'\n")


def main():
    args = sys.argv[1:]
    os.makedirs(CONF_DIR, mode=0o700, exist_ok=True)
    env = load()
    env.setdefault("MEAD_USER", "jason")
    env.setdefault("MEAD_SECRET_KEY", secrets.token_hex(32))
    env.setdefault("MEAD_API_TOKEN", secrets.token_urlsafe(32))

    if "--rotate-token" in args:
        env["MEAD_API_TOKEN"] = secrets.token_urlsafe(32)
        print("New API token written to", ENV_PATH)

    if "--random" in args:
        pw = secrets.token_urlsafe(18)
        env["MEAD_PASSWORD_HASH"] = generate_password_hash(pw)
        fd = os.open(CRED_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(f"username: {env['MEAD_USER']}\npassword: {pw}\n")
        print("Random password written to", CRED_PATH)
    elif "--rotate-token" not in args:
        pw = getpass.getpass("New Mead Tracker password: ")
        if len(pw) < 10 or pw != getpass.getpass("Again: "):
            sys.exit("Passwords didn't match or shorter than 10 characters.")
        env["MEAD_PASSWORD_HASH"] = generate_password_hash(pw)
        if os.path.exists(CRED_PATH):
            os.remove(CRED_PATH)
        print("Password updated.")

    save(env)
    print("Now: systemctl --user restart mead-tracker")


if __name__ == "__main__":
    main()
