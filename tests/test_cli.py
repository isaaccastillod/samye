"""Tests for the command-line parser."""

from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

from samye.cli import SCOPES, build_parser, main
from samye.config import Config


def config() -> Config:
    return Config.model_validate(
        {
            "default_provider": "local",
            "providers": {
                "local": {
                    "type": "openai_compat",
                    "base_url": "http://localhost:11434",
                    "model": "model",
                }
            },
        }
    )


@pytest.mark.parametrize("command", ["run", "auth", "docs"])
def test_supported_subcommands(command: str) -> None:
    assert build_parser().parse_args([command]).command == command


def test_web_subcommand_is_removed() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["web"])


@patch("samye.cli.get_credentials")
@patch("samye.cli.load_config")
def test_auth_subcommand_runs_installed_app_flow(
    load_config: Mock,
    get_credentials: Mock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = config()
    load_config.return_value = cfg

    main(["--config", "/tmp/config.toml", "auth"])

    load_config.assert_called_once_with(Path("/tmp/config.toml"))
    get_credentials.assert_called_once_with(cfg, SCOPES)
    assert capsys.readouterr().out == "authorization complete\n"


@patch("samye.cli.GDocs")
@patch("samye.cli.get_credentials")
@patch("samye.cli.load_config")
def test_docs_subcommand_lists_visible_document_ids(
    load_config: Mock,
    get_credentials: Mock,
    gdocs_class: Mock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    load_config.return_value = config()
    gdocs_class.return_value.list_shared_docs.return_value = ["doc-1", "doc-2"]

    main(["docs"])

    gdocs_class.assert_called_once_with(get_credentials.return_value)
    assert capsys.readouterr().out == "doc-1\ndoc-2\n"


@patch("samye.cli.asyncio.run")
@patch("samye.cli.Engine")
@patch("samye.cli.State.load")
@patch("samye.cli.make_provider")
@patch("samye.cli.GDocs")
@patch("samye.cli.get_credentials")
@patch("samye.cli.load_config")
def test_run_subcommand_constructs_engine_and_combined_service(
    load_config: Mock,
    get_credentials: Mock,
    gdocs_class: Mock,
    make_provider: Mock,
    state_load: Mock,
    engine_class: Mock,
    asyncio_run: Mock,
) -> None:
    cfg = config()
    load_config.return_value = cfg

    main(["run"])

    state_path = Path("~/.local/state/samye/state.json").expanduser()
    state_load.assert_called_once_with(state_path)
    engine_class.assert_called_once_with(
        gdocs_class.return_value,
        {"local": make_provider.return_value},
        cfg,
        state_load.return_value,
        state_path,
    )
    asyncio_run.assert_called_once()
    asyncio_run.call_args.args[0].close()


@pytest.mark.asyncio
async def test_serve_stops_web_when_poller_exits() -> None:
    engine = Mock()
    engine.cfg = config()
    engine.run_forever = AsyncMock(side_effect=RuntimeError("poller stopped"))
    server = Mock()
    server.serve = AsyncMock()

    with patch("samye.cli.uvicorn.Server", return_value=server):
        from samye.cli import _serve

        with pytest.raises(RuntimeError, match="poller stopped"):
            await _serve(engine, engine.cfg)

    assert server.should_exit is True
