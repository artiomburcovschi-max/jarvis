"""Тесты для framework/jarvis_config.py (раунд 21, A4).

Проверяем именно порядок приоритета из докстринга модуля:
    os.environ > .env > config.yaml > default
и то, что get_secret() НИКОГДА не смотрит в config.yaml - секреты там
быть не должны в принципе, даже если кто-то по ошибке впишет их туда.

Всё через monkeypatch + временные файлы (tmp_path), реальные
config/config.yaml и .env проекта не трогаем. jarvis_config.reset_cache_for_tests()
вызывается в конце каждого теста - иначе кэш модуля "запомнит" путь к
временному файлу первого теста и все остальные тесты в этом же процессе
увидят чужие данные.
"""
import os

import pytest

import jarvis_config


@pytest.fixture(autouse=True)
def _clean_config_cache(monkeypatch):
    # Каждый тест сам решает, что подложить в _project_root() - патчим её
    # индивидуально в каждом тесте, но кэш сбрасываем и до, и после,
    # чтобы тесты не зависели от порядка запуска.
    jarvis_config.reset_cache_for_tests()
    yield
    jarvis_config.reset_cache_for_tests()


def _point_project_root_at(monkeypatch, tmp_path):
    monkeypatch.setattr(jarvis_config, "_project_root", lambda: tmp_path)


def test_falls_back_to_default_when_nothing_set(tmp_path, monkeypatch):
    _point_project_root_at(monkeypatch, tmp_path)
    monkeypatch.delenv("SOME_VAR", raising=False)

    result = jarvis_config.get("SOME_VAR", "some.path", "дефолт")
    assert result == "дефолт"


def test_yaml_value_used_when_no_env(tmp_path, monkeypatch):
    _point_project_root_at(monkeypatch, tmp_path)
    monkeypatch.delenv("WHISPER_MODEL_SIZE", raising=False)

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(
        "whisper:\n  model_size: medium\n", encoding="utf-8"
    )

    result = jarvis_config.get("WHISPER_MODEL_SIZE", "whisper.model_size", "small")
    assert result == "medium"


def test_dotenv_value_used_when_no_process_env(tmp_path, monkeypatch):
    _point_project_root_at(monkeypatch, tmp_path)
    monkeypatch.delenv("LLM_MODEL", raising=False)

    (tmp_path / ".env").write_text("LLM_MODEL=some/model-from-dotenv\n", encoding="utf-8")

    result = jarvis_config.get("LLM_MODEL", "llm.model", "default-model")
    assert result == "some/model-from-dotenv"


def test_process_env_wins_over_dotenv_and_yaml(tmp_path, monkeypatch):
    _point_project_root_at(monkeypatch, tmp_path)

    (tmp_path / ".env").write_text("LLM_MODEL=from-dotenv\n", encoding="utf-8")
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text("llm:\n  model: from-yaml\n", encoding="utf-8")

    monkeypatch.setenv("LLM_MODEL", "from-real-shell-export")

    result = jarvis_config.get("LLM_MODEL", "llm.model", "default")
    assert result == "from-real-shell-export"


def test_dotenv_does_not_override_existing_env_var(tmp_path, monkeypatch):
    # .env должен вести себя как "заполнить только то, что ещё не задано" -
    # если в shell уже что-то экспортировано, .env это не перебивает.
    _point_project_root_at(monkeypatch, tmp_path)
    monkeypatch.setenv("LLM_MODEL", "already-exported")
    (tmp_path / ".env").write_text("LLM_MODEL=from-dotenv\n", encoding="utf-8")

    result = jarvis_config.get("LLM_MODEL", "llm.model", "default")
    assert result == "already-exported"


def test_empty_string_env_var_treated_as_unset(tmp_path, monkeypatch):
    # Пустая переменная окружения (export LLM_SITE_URL=) - это то же самое,
    # что "не задано", а не "явно пустая строка главнее дефолта/YAML".
    _point_project_root_at(monkeypatch, tmp_path)
    monkeypatch.setenv("LLM_SITE_URL", "")
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text('llm:\n  site_url: "from-yaml"\n', encoding="utf-8")

    result = jarvis_config.get("LLM_SITE_URL", "llm.site_url", "")
    assert result == "from-yaml"


def test_missing_config_yaml_does_not_crash(tmp_path, monkeypatch):
    _point_project_root_at(monkeypatch, tmp_path)  # ни .env, ни config/ не создаём
    monkeypatch.delenv("WHISPER_DEVICE", raising=False)

    result = jarvis_config.get("WHISPER_DEVICE", "whisper.device", "cpu")
    assert result == "cpu"


def test_get_secret_ignores_config_yaml_even_if_present(tmp_path, monkeypatch):
    # Секреты в config.yaml по задумке быть не должно, но если кто-то туда
    # всё же впишет ключ по ошибке - get_secret() всё равно его игнорирует.
    _point_project_root_at(monkeypatch, tmp_path)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(
        'llm:\n  api_key: "sk-should-be-ignored"\n', encoding="utf-8"
    )

    result = jarvis_config.get_secret("LLM_API_KEY", None)
    assert result is None


def test_get_secret_reads_dotenv(tmp_path, monkeypatch):
    _point_project_root_at(monkeypatch, tmp_path)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    (tmp_path / ".env").write_text("LLM_API_KEY=sk-or-from-dotenv\n", encoding="utf-8")

    result = jarvis_config.get_secret("LLM_API_KEY")
    assert result == "sk-or-from-dotenv"


def test_dotenv_quotes_and_comments_handled(tmp_path, monkeypatch):
    _point_project_root_at(monkeypatch, tmp_path)
    monkeypatch.delenv("QUOTED_VAR", raising=False)
    monkeypatch.delenv("PLAIN_VAR", raising=False)
    (tmp_path / ".env").write_text(
        "# это комментарий, пропускаем\n"
        "\n"
        'QUOTED_VAR="значение в кавычках"\n'
        "PLAIN_VAR=без_кавычек\n",
        encoding="utf-8",
    )

    assert jarvis_config.get_secret("QUOTED_VAR") == "значение в кавычках"
    assert jarvis_config.get_secret("PLAIN_VAR") == "без_кавычек"


def test_nested_yaml_path_not_found_returns_default(tmp_path, monkeypatch):
    _point_project_root_at(monkeypatch, tmp_path)
    monkeypatch.delenv("SOME_VAR", raising=False)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text("llm:\n  model: x\n", encoding="utf-8")

    # "backend.url" не существует в этом yaml - должен тихо вернуться default,
    # а не упасть с KeyError/TypeError.
    result = jarvis_config.get("SOME_VAR", "backend.url", "fallback")
    assert result == "fallback"
