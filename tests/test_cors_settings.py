"""Tests for CORS origin configuration (Céluma 1.3 Fase 2, Bloque D, Historia D1).

`Settings.cors_allowed_origins` is a raw comma-separated string (env var
`CORS_ALLOWED_ORIGINS`); `cors_allowed_origins_list` is what `app/main.py`
actually feeds to `CORSMiddleware.allow_origins`.
"""
import pytest

from app.core.config import Settings


def _settings(raw: str) -> Settings:
    return Settings(
        database_url="postgresql://x",
        jwt_secret="x",
        cors_allowed_origins=raw,
    )


def test_default_origins_include_vite_dev_and_preview_ports():
    settings = Settings(database_url="postgresql://x", jwt_secret="x")
    origins = settings.cors_allowed_origins_list
    assert "http://localhost:5173" in origins
    assert "http://localhost:4173" in origins


def test_parses_comma_separated_list():
    settings = _settings("https://a.example.com,https://b.example.com")
    assert settings.cors_allowed_origins_list == [
        "https://a.example.com",
        "https://b.example.com",
    ]


def test_strips_whitespace_and_drops_empty_entries():
    settings = _settings(" https://a.example.com ,, https://b.example.com ,")
    assert settings.cors_allowed_origins_list == [
        "https://a.example.com",
        "https://b.example.com",
    ]


def test_single_origin():
    settings = _settings("https://app.example.com")
    assert settings.cors_allowed_origins_list == ["https://app.example.com"]


def test_bare_star_is_rejected():
    settings = _settings("https://a.example.com,*")
    with pytest.raises(ValueError, match="bare '\\*'"):
        settings.cors_allowed_origins_list


def test_bare_star_alone_is_rejected():
    settings = _settings("*")
    with pytest.raises(ValueError, match="bare '\\*'"):
        settings.cors_allowed_origins_list
