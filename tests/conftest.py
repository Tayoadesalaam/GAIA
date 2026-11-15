"""Tests never touch a real Supabase project, whatever .env says: force the local file store
and drop Supabase/Gemini credentials from the environment for every test."""
import pytest


@pytest.fixture(autouse=True)
def _offline_store(monkeypatch):
    monkeypatch.setenv("GAIA_STORE", "local")
    for var in ("SUPABASE_URL", "SUPABASE_SECRET_KEY", "SUPABASE_SERVICE_ROLE_KEY"):
        monkeypatch.delenv(var, raising=False)
