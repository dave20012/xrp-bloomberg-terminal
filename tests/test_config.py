import importlib

import core.config as config


def test_coalesce_env_handles_double_brace_placeholders(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "${{Postgres.DATABASE_URL}}")

    importlib.reload(config)

    assert config.settings.database_url == "sqlite:///./xrp_intel.db"


def test_placeholder_password_is_removed(monkeypatch):
    placeholder_url = "redis://default:${{Redis.REDIS_PASSWORD}}@localhost:6379/0"
    monkeypatch.setenv("REDIS_URL", placeholder_url)

    import core.redis_client as redis_client

    importlib.reload(config)
    importlib.reload(redis_client)

    captured = {}

    def fake_from_url(url, decode_responses=True):  # noqa: ANN001
        captured["url"] = url

        class Dummy:
            def set(self, *args, **kwargs):  # noqa: ANN002,ANN003
                return None

            def get(self, key):  # noqa: ANN001
                return None

        return Dummy()

    monkeypatch.setattr(redis_client.redis, "from_url", fake_from_url)

    client = redis_client.get_redis_client()

    assert captured["url"] == "redis://default@localhost:6379/0"
    assert client.get("irrelevant") is None
