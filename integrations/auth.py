import argparse
import getpass
import hashlib
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash


SESSION_SECONDS = 12 * 60 * 60
_USERNAME = re.compile(r"[a-zA-Z0-9._-]{1,64}\Z")
_DUMMY_HASH = generate_password_hash("not-a-real-password")


class AuthError(Exception):
    pass


@contextmanager
def _connect():
    path = Path(os.environ.get("JARVIS_AUTH_DB", "")).expanduser() if os.environ.get("JARVIS_AUTH_DB") else Path.home() / ".local" / "share" / "jarviscore" / "auth.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    connection = sqlite3.connect(path, timeout=30)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS users (username TEXT PRIMARY KEY COLLATE NOCASE, password_hash TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE, expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS attempts (attempted REAL NOT NULL);
    """)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def add_user(username, password):
    if not isinstance(username, str) or not _USERNAME.fullmatch(username):
        raise AuthError("Use 1-64 letters, numbers, dots, underscores or hyphens for the username.")
    if not isinstance(password, str) or not 12 <= len(password) <= 1024:
        raise AuthError("Passwords must be between 12 and 1024 characters.")
    password_hash = generate_password_hash(password)
    with _connect() as connection:
        try:
            connection.execute("INSERT INTO users VALUES (?, ?)", (username, password_hash))
        except sqlite3.IntegrityError as error:
            raise AuthError("That username already exists.") from error


def users():
    with _connect() as connection:
        return [row[0] for row in connection.execute("SELECT username FROM users ORDER BY username COLLATE NOCASE")]


def remove_user(username):
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] <= 1:
            raise AuthError("The last user cannot be removed.")
        if not connection.execute("DELETE FROM users WHERE username = ?", (username,)).rowcount:
            raise AuthError("User not found.")


def login(username, password):
    if not isinstance(username, str) or not isinstance(password, str) or len(username) > 64 or len(password) > 1024:
        raise AuthError("Invalid username or password.")
    now = time.time()
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM attempts WHERE attempted < ?", (now - 300,))
        if connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] >= 20:
            raise AuthError("Too many login attempts. Try again in five minutes.")
        connection.execute("INSERT INTO attempts VALUES (?)", (now,))
        row = connection.execute("SELECT username, password_hash FROM users WHERE username = ?", (username,)).fetchone()
    valid = check_password_hash(row[1] if row else _DUMMY_HASH, password)
    if not row or not valid:
        raise AuthError("Invalid username or password.")
    token = secrets.token_urlsafe(32)
    with _connect() as connection:
        connection.execute("DELETE FROM sessions WHERE expires < ?", (now,))
        connection.execute("INSERT INTO sessions VALUES (?, ?, ?)", (_token_hash(token), row[0], now + SESSION_SECONDS))
    return token


def _token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def session_user(token):
    if not isinstance(token, str) or len(token) > 128:
        return None
    with _connect() as connection:
        row = connection.execute("SELECT username FROM sessions WHERE token_hash = ? AND expires > ?", (_token_hash(token), time.time())).fetchone()
        return row[0] if row else None


def logout(token):
    if isinstance(token, str) and len(token) <= 128:
        with _connect() as connection:
            connection.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))


def main():
    parser = argparse.ArgumentParser(description="Create the initial Computer user")
    parser.add_argument("--bootstrap", action="store_true", required=True)
    parser.parse_args()
    if users():
        print("Existing Computer users retained.")
        return
    while True:
        username = input("First Computer username: ").strip()
        password = getpass.getpass("Password (at least 12 characters): ")
        confirmation = getpass.getpass("Confirm password: ")
        if password != confirmation:
            print("Passwords do not match.")
            continue
        try:
            add_user(username, password)
        except AuthError as error:
            print(error)
            continue
        print("First Computer user created.")
        return


if __name__ == "__main__":
    main()