"""Tests for the local proposal review application."""

from pathlib import Path

import httpx
import pytest

from samye.config import Config
from samye.state import Proposal
from samye.web.app import create_app


class MockEngine:
    def __init__(self) -> None:
        self.cfg = Config.model_validate(
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
        self.proposal = Proposal(
            id="proposal",
            comment_id="comment",
            comment_modified_time="2026-08-16T10:00:00Z",
            tab_id="tab-1",
            document_title="Fixture Document",
            target_text="The quick brown fox",
            replacement="The nimble brown fox",
            provider="local",
            model="model",
            created="2026-08-16T10:01:00Z",
        )
        self.accept_status = "applied"
        self.reject_status = "rejected"

    def list_proposals(self) -> list[tuple[str, Proposal]]:
        return [("doc", self.proposal)]

    async def accept_proposal(self, file_id: str, proposal_id: str) -> str:
        if file_id == "missing" or proposal_id == "missing":
            raise KeyError(proposal_id)
        return self.accept_status

    async def reject_proposal(self, file_id: str, proposal_id: str) -> str:
        if file_id == "missing" or proposal_id == "missing":
            raise KeyError(proposal_id)
        return self.reject_status

    async def remove_proposal(self, file_id: str, proposal_id: str) -> None:
        if file_id == "missing" or proposal_id == "missing":
            raise KeyError(proposal_id)
        if self.proposal.status in {"pending", "applying"}:
            raise ValueError("not terminal")


@pytest.fixture
def web() -> tuple[MockEngine, object, str]:
    engine = MockEngine()
    app = create_app(engine)  # type: ignore[arg-type]
    return engine, app, app.state.csrf_token


async def request(app: object, method: str, path: str, **kwargs: object) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://localhost",
    ) as client:
        return await client.request(method, path, **kwargs)


@pytest.mark.asyncio
async def test_lists_fixture_proposal_for_diff_rendering(
    web: tuple[MockEngine, object, str],
) -> None:
    _, app, _ = web

    response = await request(app, "GET", "/api/proposals")

    assert response.status_code == 200
    assert response.json() == [
        {
            "file_id": "doc",
            "id": "proposal",
            "comment_id": "comment",
            "comment_modified_time": "2026-08-16T10:00:00Z",
            "tab_id": "tab-1",
            "document_title": "Fixture Document",
            "target_text": "The quick brown fox",
            "replacement": "The nimble brown fox",
            "provider": "local",
            "model": "model",
            "created": "2026-08-16T10:01:00Z",
            "status": "pending",
        }
    ]
    script = (
        Path(__file__).parents[1] / "src/samye/web/static/app.js"
    ).read_text(encoding="utf-8")
    assert "wordDiff(proposal.target_text, proposal.replacement)" in script
    assert 'diffPane("Current"' in script
    assert 'diffPane("Proposed"' in script
    assert "setInterval(() => load({ silent: true }), refreshIntervalMs)" in script
    assert 'document.addEventListener("visibilitychange"' in script
    assert "loadInFlight || transitionInFlight > 0" in script
    assert 'element("button", "Remove")' in script
    assert "removeProposal(proposal, remove)" in script
    assert "innerHTML" not in script


@pytest.mark.parametrize("status", ["applied", "stale", "indeterminate"])
@pytest.mark.asyncio
async def test_accept_surfaces_every_engine_status(
    web: tuple[MockEngine, object, str], status: str
) -> None:
    engine, app, token = web
    engine.accept_status = status

    response = await request(
        app,
        "POST",
        "/api/proposals/doc/proposal/accept",
        headers={"X-Samye-Token": token},
    )

    assert response.status_code == 200
    assert response.json() == {"status": status}


@pytest.mark.asyncio
async def test_reject_returns_status(web: tuple[MockEngine, object, str]) -> None:
    _, app, token = web

    response = await request(
        app,
        "POST",
        "/api/proposals/doc/proposal/reject",
        headers={"X-Samye-Token": token},
    )

    assert response.json() == {"status": "rejected"}


@pytest.mark.asyncio
async def test_unknown_proposal_is_404(web: tuple[MockEngine, object, str]) -> None:
    _, app, token = web

    response = await request(
        app,
        "POST",
        "/api/proposals/doc/missing/accept",
        headers={"X-Samye-Token": token},
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_removes_terminal_proposal(web: tuple[MockEngine, object, str]) -> None:
    engine, app, token = web
    engine.proposal.status = "applied"

    response = await request(
        app,
        "POST",
        "/api/proposals/doc/proposal/remove",
        headers={"X-Samye-Token": token},
    )

    assert response.status_code == 200
    assert response.json() == {"removed": True}


@pytest.mark.asyncio
async def test_remove_refuses_pending_proposal(web: tuple[MockEngine, object, str]) -> None:
    _, app, token = web

    response = await request(
        app,
        "POST",
        "/api/proposals/doc/proposal/remove",
        headers={"X-Samye-Token": token},
    )

    assert response.status_code == 409


@pytest.mark.asyncio
async def test_remove_unknown_proposal_is_404(
    web: tuple[MockEngine, object, str]
) -> None:
    engine, app, token = web
    engine.proposal.status = "applied"

    response = await request(
        app,
        "POST",
        "/api/proposals/doc/missing/remove",
        headers={"X-Samye-Token": token},
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_post_without_csrf_token_is_403(web: tuple[MockEngine, object, str]) -> None:
    _, app, _ = web

    response = await request(app, "POST", "/api/proposals/doc/proposal/accept")

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_bad_host_is_403(web: tuple[MockEngine, object, str]) -> None:
    _, app, token = web

    response = await request(
        app,
        "POST",
        "/api/proposals/doc/proposal/accept",
        headers={"Host": "attacker.example", "X-Samye-Token": token},
    )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_index_injects_per_run_csrf_token(web: tuple[MockEngine, object, str]) -> None:
    _, app, token = web

    response = await request(app, "GET", "/")

    assert response.status_code == 200
    assert f'content="{token}"' in response.text
    assert "__SAMYE_TOKEN__" not in response.text
    assert "<title>samye</title>" in response.text
    assert "<h1>samye</h1>" in response.text
    assert "samye proposals" not in response.text
