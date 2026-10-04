from dromos.config import PROJECT_ROOT, load_settings


def test_defaults_resolve_under_project_root(tmp_path):
    settings = load_settings(tmp_path / "missing.toml")
    assert settings.raw_dir == PROJECT_ROOT / "data" / "raw"
    assert settings.processed_dir == PROJECT_ROOT / "data" / "processed"
    assert settings.posts_dir == PROJECT_ROOT / "posts"


def test_toml_values_override_defaults(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('data_path = "elsewhere"\n', encoding="utf-8")
    assert load_settings(config).data_dir == PROJECT_ROOT / "elsewhere"
