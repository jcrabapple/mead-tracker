"""Shared fixtures and helpers.

The app module is imported fresh per test (sys.modules.pop) so each test gets
a clean database and its own login-throttle counters.
"""
import base64
import importlib
import os
import sys

import pytest
from werkzeug.security import generate_password_hash

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TOKEN = "test-token-123"
PASSWORD = "hunter2"


@pytest.fixture
def appmod(tmp_path, monkeypatch):
    monkeypatch.setenv("MEAD_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("MEAD_USER", "jason")
    monkeypatch.setenv("MEAD_PASSWORD_HASH", generate_password_hash(PASSWORD))
    monkeypatch.setenv("MEAD_API_TOKEN", TOKEN)
    monkeypatch.delenv("MEAD_AUTH_DISABLED", raising=False)
    sys.modules.pop("app", None)
    app_mod = importlib.import_module("app")
    app_mod.app.config["TESTING"] = True
    assert app_mod.scheduler is None, "scheduler must not start on import"
    return app_mod


@pytest.fixture
def anon(appmod):
    """What a stranger's browser gets: no session cookie."""
    return appmod.app.test_client()


@pytest.fixture
def client(anon):
    """Signed-in client. Most UI tests want the session already established."""
    r = login(anon)
    assert r.status_code == 302
    return anon


def basic(user="jason", pw=PASSWORD):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


def login(c, password=PASSWORD, user="jason"):
    return c.post("/login", data={"username": user, "password": password})


API = {"Authorization": f"Bearer {TOKEN}"}
