from pathlib import Path

import pytest

from claimagent.core.settings import DEFAULT_CONFIG, ModelsConfig, Secrets, Settings


def test_default_config_loads_without_env_file(tmp_path: Path):
    settings = Settings.load(DEFAULT_CONFIG, env_file=tmp_path / "missing.env")
    assert settings.locale.country == "IN"
    assert settings.runs_dir.is_absolute()
    assert settings.models.chain("detect"), "default profile must configure the detect stage"


def test_env_file_supplies_keys(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("GEMINI_API_KEY=abc\nOPENAI_API_KEY=\n")
    settings = Settings.load(DEFAULT_CONFIG, env_file=env)
    assert settings.secrets.key_for("gemini") == "abc"
    assert settings.secrets.key_for("openai") is None  # empty counts as missing


def test_google_api_key_is_accepted_for_gemini():
    assert Secrets(_env_file=None, GOOGLE_API_KEY="xyz").key_for("gemini") == "xyz"


def test_override_wins_over_profile():
    models = ModelsConfig(profile="p", profiles={"p": {"read": ["a"]}}, overrides={"read": ["b", "c"]})
    assert models.chain("read") == ["b", "c"]
    assert models.chain("detect") == []


def test_unknown_profile_is_an_error():
    with pytest.raises(KeyError, match="unknown model profile"):
        ModelsConfig(profile="nope").chain("read")


def test_cache_file_overrides_the_shared_cache(tmp_path):
    settings = Settings(runs_dir=tmp_path / "runs")
    assert settings.cache_path == tmp_path / "runs" / ".cache.sqlite"
    settings.cache_file = tmp_path / "other.sqlite"
    assert settings.cache_path == tmp_path / "other.sqlite"
