"""Static checks for container and setup documentation."""

import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_package_metadata_links_readme_and_repository() -> None:
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]

    assert metadata["readme"] == "README.md"
    assert metadata["license"] == "MIT"
    assert metadata["license-files"] == ["LICENSE"]
    assert metadata["urls"] == {
        "Repository": "https://github.com/isaaccastillod/samye",
        "Issues": "https://github.com/isaaccastillod/samye/issues",
    }


def test_mit_license_names_repository_author() -> None:
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")

    assert license_text.startswith("MIT License\n")
    assert "Copyright (c) 2026 Isaac Castillo" in license_text
    assert "Permission is hereby granted, free of charge" in license_text


def test_container_runs_as_non_root_and_uses_locked_environment() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "uv sync --frozen --no-dev" in dockerfile
    assert "USER samye" in dockerfile
    assert 'CMD [".venv/bin/samye", "run"]' in dockerfile


def test_compose_publishes_only_on_host_loopback_and_mounts_state() -> None:
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")

    assert '"127.0.0.1:8321:8321"' in compose
    assert "/home/samye/.config/samye:ro" in compose
    assert "/home/samye/.local/state/samye" in compose


def test_readme_states_security_and_delivery_limits() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "do not require Developer Preview enrollment" in readme
    assert "writes only to the first tab body" in readme
    assert "indeterminate" in readme
    assert "best-effort" in readme
    assert "no login" in readme
    assert "authenticated reverse proxy or VPN" in readme
    assert "browser-equipped machine" in readme


def test_readme_explicitly_documents_local_model_support() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "Local models are fully supported" in readme
    assert 'type = "openai_compat"' in readme
    assert "Ollama, llama.cpp server, vLLM, LocalAI" in readme
    assert "http://host.docker.internal:11434" in readme
