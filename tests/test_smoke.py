def test_settings_import() -> None:
    from app.config import settings

    assert settings.env in {"local", "staging", "prod"}
