"""Comment orchestration and daemon polling loop."""

from __future__ import annotations

import asyncio
import copy
import logging
import random
import re
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from googleapiclient.errors import HttpError

from samye.commands import Instruct, ParseError, Pin, Unpin, parse
from samye.config import Config
from samye.gdocs import Doc, GDocs, RevisionConflict
from samye.pins import PinOutcome, handle_pin, handle_unpin
from samye.providers.base import Provider, ProviderError
from samye.state import (
    FileState,
    Inflight,
    PendingReply,
    Proposal,
    ProposalStatus,
    State,
)
from samye.textmap import Span, TextMap, build_text_map

LOGGER = logging.getLogger(__name__)
SYSTEM_PROMPT = (
    "Return ONLY the replacement text for the target span. "
    "Do not include a code fence or commentary."
)
INTERRUPTED_NOTICE = (
    "a previous attempt was interrupted and may have partially modified the document — "
    "please check it"
)
INDETERMINATE_NOTICE = (
    "the edit outcome is indeterminate — verify the document manually and re-trigger if needed"
)
STALE_NOTICE = "the proposal is stale because the target changed — please re-trigger"
FENCE = re.compile(r"^\s*```[^\n]*\n(.*?)\n```\s*$", re.DOTALL)


@dataclass(frozen=True)
class _Target:
    span: Span
    text_map: TextMap
    text: str


class Engine:
    """Coordinate polling, providers, proposals, replies, and persisted state."""

    def __init__(
        self,
        gdocs: GDocs,
        providers: dict[str, Provider],
        cfg: Config,
        state: State,
        state_path: Path,
    ) -> None:
        if cfg.write_mode == "suggest":
            raise ValueError("suggest mode requires the Preview extension")
        self.gdocs = gdocs
        self.providers = providers
        self.cfg = cfg
        self.state = state
        self.state_path = state_path.expanduser()
        self._locks: dict[str, asyncio.Lock] = {}
        self._rate_failures: dict[str, int] = {}
        self._rate_allowed_at: dict[str, float] = {}

    def _lock(self, file_id: str) -> asyncio.Lock:
        return self._locks.setdefault(file_id, asyncio.Lock())

    def _file_state(self, file_id: str) -> FileState:
        return self.state.files.setdefault(file_id, FileState())

    async def handle_comment(self, file_id: str, comment: dict[str, object]) -> None:
        """Handle one comment without leaking an exception to the poll loop."""
        content = comment.get("content")
        comment_id = comment.get("id")
        modified_time = comment.get("modifiedTime")
        if not isinstance(content, str):
            return
        command = parse(content)
        if command is None:
            return
        if not isinstance(comment_id, str) or not isinstance(modified_time, str):
            LOGGER.warning("ignoring a command comment missing an ID or modifiedTime")
            return

        file_state = self._file_state(file_id)
        if (
            comment_id in file_state.seen
            or comment_id in file_state.inflight
            or comment_id in file_state.pending_replies
        ):
            return

        if isinstance(command, ParseError):
            await self._queue_and_deliver(
                file_id, comment_id, modified_time, command.message, resolve=False
            )
            return

        try:
            if isinstance(command, Pin):
                await self._handle_pin(file_id, comment, command)
            elif isinstance(command, Unpin):
                await self._handle_unpin(file_id, comment, command)
            else:
                await self._handle_instruction(file_id, comment, command)
        except Exception as exc:
            LOGGER.exception("command handling failed for comment %s", comment_id)
            await self._safe_error_reply(file_id, comment_id, modified_time, exc)

    async def _handle_pin(
        self, file_id: str, comment: dict[str, object], command: Pin
    ) -> None:
        doc = await asyncio.to_thread(self.gdocs.get_doc, file_id)
        target, error = _locate_target(doc, _quote(comment))
        if target is None:
            await self._queue_and_deliver(
                file_id,
                _comment_id(comment),
                _modified_time(comment),
                error,
                resolve=False,
            )
            return
        await self._mutate_pin(file_id, comment, handle_pin, doc, target.span, command)

    async def _handle_unpin(
        self, file_id: str, comment: dict[str, object], command: Unpin
    ) -> None:
        doc = await asyncio.to_thread(self.gdocs.get_doc, file_id)
        await self._mutate_pin(file_id, comment, handle_unpin, doc, command)

    async def _mutate_pin(
        self,
        file_id: str,
        comment: dict[str, object],
        handler: Any,
        *arguments: object,
    ) -> None:
        comment_id = _comment_id(comment)
        modified_time = _modified_time(comment)
        async with self._lock(file_id):
            file_state = self._file_state(file_id)
            file_state.inflight[comment_id] = Inflight("mutating", modified_time)
            self.state.save(self.state_path)
            try:
                outcome: PinOutcome = await handler(self.gdocs, *arguments)
            except RevisionConflict:
                await self._terminal_reply_locked(
                    file_id,
                    comment_id,
                    modified_time,
                    "the document changed before the pin operation; please re-trigger",
                    resolve=False,
                )
                return
            except Exception:
                LOGGER.exception("pin mutation outcome is indeterminate for comment %s", comment_id)
                await self._terminal_reply_locked(
                    file_id,
                    comment_id,
                    modified_time,
                    INTERRUPTED_NOTICE,
                    resolve=True,
                )
                return
            await self._terminal_reply_locked(
                file_id,
                comment_id,
                modified_time,
                outcome.reply,
                resolve=outcome.resolve,
            )

    async def _handle_instruction(
        self, file_id: str, comment: dict[str, object], command: Instruct
    ) -> None:
        doc = await asyncio.to_thread(self.gdocs.get_doc, file_id)
        target, error = _locate_target(doc, _quote(comment))
        if target is None:
            await self._queue_and_deliver(
                file_id,
                _comment_id(comment),
                _modified_time(comment),
                error,
                resolve=False,
            )
            return
        pointers, missing = _pointer_text(doc, command.refs)
        if missing is not None:
            await self._queue_and_deliver(
                file_id,
                _comment_id(comment),
                _modified_time(comment),
                f"pointer @[{missing}] was not found; update the comment and re-trigger",
                resolve=False,
            )
            return

        provider_name = self.cfg.default_provider
        provider = self.providers[provider_name]
        user_prompt = _build_user_prompt(command, target, pointers, self.cfg.context_chars)
        replacement = _strip_wrapping_fence(await provider.complete(SYSTEM_PROMPT, user_prompt))
        if not replacement.strip():
            raise ProviderError(f"{provider_name}: provider returned an empty completion")

        if self.cfg.write_mode == "reply":
            await self._queue_and_deliver(
                file_id,
                _comment_id(comment),
                _modified_time(comment),
                f"replacement — {provider_name}/{self.cfg.providers[provider_name].model}\n\n"
                f"```\n{replacement}\n```",
                resolve=True,
            )
            return
        await self._create_proposal(file_id, comment, doc, target, replacement, provider_name)

    async def _create_proposal(
        self,
        file_id: str,
        comment: dict[str, object],
        doc: Doc,
        target: _Target,
        replacement: str,
        provider_name: str,
    ) -> None:
        proposal_id = secrets.token_urlsafe(16)
        comment_id = _comment_id(comment)
        modified_time = _modified_time(comment)
        proposal = Proposal(
            id=proposal_id,
            comment_id=comment_id,
            comment_modified_time=modified_time,
            tab_id=target.span.tab_id,
            document_title=doc.title,
            target_text=target.text,
            replacement=replacement,
            provider=provider_name,
            model=self.cfg.providers[provider_name].model,
            created=_rfc3339(datetime.now(UTC)),
        )
        reply = "proposal ready — review in the samye web UI"
        if self.cfg.web_base_url is not None:
            reply += f": {self.cfg.web_base_url}/#proposal={proposal_id}"
        async with self._lock(file_id):
            file_state = self._file_state(file_id)
            file_state.proposals[proposal_id] = proposal
            file_state.inflight[comment_id] = Inflight("replying", modified_time)
            file_state.pending_replies[comment_id] = PendingReply(
                text=reply,
                resolve=False,
                comment_modified_time=modified_time,
            )
            self.state.save(self.state_path)
            await self._deliver_pending_locked(file_id, comment_id)

    def list_proposals(self) -> list[tuple[str, Proposal]]:
        """Return detached proposal snapshots across all documents."""
        proposals = [
            (file_id, copy.deepcopy(proposal))
            for file_id, file_state in self.state.files.items()
            for proposal in file_state.proposals.values()
        ]
        return sorted(proposals, key=lambda item: (item[1].created, item[1].id), reverse=True)

    async def accept_proposal(self, file_id: str, proposal_id: str) -> ProposalStatus:
        """Revalidate and apply a pending proposal with one safe conflict retry."""
        async with self._lock(file_id):
            proposal = self._get_proposal(file_id, proposal_id)
            if proposal.status != "pending":
                return proposal.status

            doc = await asyncio.to_thread(self.gdocs.get_doc, file_id)
            target = _valid_proposal_target(doc, proposal)
            if target is None:
                return await self._finish_proposal_locked(file_id, proposal, "stale")

            proposal.status = "applying"
            self.state.save(self.state_path)
            try:
                await asyncio.to_thread(
                    self.gdocs.direct_replace,
                    doc,
                    target.span,
                    proposal.replacement,
                )
            except RevisionConflict:
                try:
                    refreshed = await asyncio.to_thread(self.gdocs.get_doc, file_id)
                except Exception:
                    proposal.status = "pending"
                    self.state.save(self.state_path)
                    raise
                refreshed_target = _valid_proposal_target(refreshed, proposal)
                if refreshed_target is None:
                    return await self._finish_proposal_locked(file_id, proposal, "stale")
                try:
                    await asyncio.to_thread(
                        self.gdocs.direct_replace,
                        refreshed,
                        refreshed_target.span,
                        proposal.replacement,
                    )
                except RevisionConflict:
                    return await self._finish_proposal_locked(file_id, proposal, "stale")
                except Exception:
                    LOGGER.exception("proposal retry outcome is indeterminate")
                    return await self._finish_proposal_locked(
                        file_id, proposal, "indeterminate"
                    )
            except Exception:
                LOGGER.exception("proposal write outcome is indeterminate")
                return await self._finish_proposal_locked(file_id, proposal, "indeterminate")
            return await self._finish_proposal_locked(file_id, proposal, "applied")

    async def reject_proposal(self, file_id: str, proposal_id: str) -> ProposalStatus:
        """Reject a pending proposal without touching the document."""
        async with self._lock(file_id):
            proposal = self._get_proposal(file_id, proposal_id)
            if proposal.status != "pending":
                return proposal.status
            return await self._finish_proposal_locked(file_id, proposal, "rejected")

    def _get_proposal(self, file_id: str, proposal_id: str) -> Proposal:
        try:
            return self.state.files[file_id].proposals[proposal_id]
        except KeyError:
            raise KeyError((file_id, proposal_id)) from None

    async def _finish_proposal_locked(
        self,
        file_id: str,
        proposal: Proposal,
        status: ProposalStatus,
    ) -> ProposalStatus:
        proposal.status = status
        replies = {
            "applied": f"applied — {proposal.provider}/{proposal.model}",
            "rejected": "proposal rejected",
            "stale": STALE_NOTICE,
            "indeterminate": INDETERMINATE_NOTICE,
        }
        text = replies[status]
        file_state = self._file_state(file_id)
        file_state.inflight[proposal.comment_id] = Inflight(
            "replying", proposal.comment_modified_time
        )
        file_state.pending_replies[proposal.comment_id] = PendingReply(
            text=text,
            resolve=True,
            comment_modified_time=proposal.comment_modified_time,
        )
        self.state.save(self.state_path)
        await self._deliver_pending_locked(file_id, proposal.comment_id)
        return proposal.status

    async def _safe_error_reply(
        self,
        file_id: str,
        comment_id: str,
        modified_time: str,
        error: Exception,
    ) -> None:
        if isinstance(error, ProviderError):
            text = "the completion provider failed; please re-trigger with a new comment"
        elif isinstance(error, (HttpError, RevisionConflict)):
            text = "the Google API request failed; please re-trigger with a new comment"
        else:
            text = "samye could not process this request; please re-trigger with a new comment"
        try:
            await self._queue_and_deliver(
                file_id, comment_id, modified_time, text, resolve=False
            )
        except Exception:
            LOGGER.exception("could not queue an error reply for comment %s", comment_id)

    async def _queue_and_deliver(
        self,
        file_id: str,
        comment_id: str,
        modified_time: str,
        text: str | None,
        *,
        resolve: bool,
    ) -> None:
        if text is None:
            text = "samye could not locate a safe target for this request"
        async with self._lock(file_id):
            await self._terminal_reply_locked(
                file_id, comment_id, modified_time, text, resolve=resolve
            )

    async def _terminal_reply_locked(
        self,
        file_id: str,
        comment_id: str,
        modified_time: str,
        text: str,
        *,
        resolve: bool,
    ) -> None:
        file_state = self._file_state(file_id)
        file_state.inflight[comment_id] = Inflight("replying", modified_time)
        file_state.pending_replies[comment_id] = PendingReply(
            text=text,
            resolve=resolve,
            comment_modified_time=modified_time,
        )
        self.state.save(self.state_path)
        await self._deliver_pending_locked(file_id, comment_id)

    async def _deliver_pending_locked(self, file_id: str, comment_id: str) -> None:
        file_state = self._file_state(file_id)
        pending = file_state.pending_replies.get(comment_id)
        if pending is None:
            return
        try:
            await asyncio.to_thread(
                self.gdocs.reply,
                file_id,
                comment_id,
                pending.text,
                resolve=pending.resolve,
            )
        except Exception:
            pending.attempts += 1
            if pending.attempts >= 3:
                LOGGER.warning("abandoning reply delivery for comment %s", comment_id)
                file_state.pending_replies.pop(comment_id, None)
                file_state.inflight.pop(comment_id, None)
                file_state.mark_seen(comment_id, pending.comment_modified_time)
            else:
                LOGGER.warning(
                    "reply delivery failed for comment %s; attempt %d of 3",
                    comment_id,
                    pending.attempts,
                )
            self.state.save(self.state_path)
            return
        file_state.pending_replies.pop(comment_id, None)
        file_state.inflight.pop(comment_id, None)
        file_state.mark_seen(comment_id, pending.comment_modified_time)
        self.state.save(self.state_path)

    async def _deliver_file_pending(self, file_id: str) -> None:
        async with self._lock(file_id):
            for comment_id in list(self._file_state(file_id).pending_replies):
                await self._deliver_pending_locked(file_id, comment_id)

    async def _recover_startup(self) -> None:
        for file_id, file_state in list(self.state.files.items()):
            async with self._lock(file_id):
                changed = False
                for proposal in file_state.proposals.values():
                    if proposal.status != "applying":
                        continue
                    proposal.status = "indeterminate"
                    file_state.inflight[proposal.comment_id] = Inflight(
                        "replying", proposal.comment_modified_time
                    )
                    file_state.pending_replies[proposal.comment_id] = PendingReply(
                        INDETERMINATE_NOTICE,
                        True,
                        proposal.comment_modified_time,
                    )
                    changed = True
                for comment_id, inflight in list(file_state.inflight.items()):
                    if inflight.stage == "mutating":
                        file_state.inflight[comment_id] = Inflight(
                            "replying", inflight.comment_modified_time
                        )
                        file_state.pending_replies[comment_id] = PendingReply(
                            INTERRUPTED_NOTICE,
                            True,
                            inflight.comment_modified_time,
                        )
                        changed = True
                    elif comment_id not in file_state.pending_replies:
                        file_state.pending_replies[comment_id] = PendingReply(
                            INTERRUPTED_NOTICE,
                            True,
                            inflight.comment_modified_time,
                        )
                        changed = True
                if changed:
                    self.state.save(self.state_path)
                for comment_id in list(file_state.pending_replies):
                    await self._deliver_pending_locked(file_id, comment_id)

    async def _poll_file(self, file_id: str) -> None:
        if time.monotonic() < self._rate_allowed_at.get(file_id, 0):
            return
        await self._deliver_file_pending(file_id)
        file_state = self._file_state(file_id)
        poll_started = datetime.now(UTC)
        first_run = file_state.watermark is None
        try:
            comments = await asyncio.to_thread(
                self.gdocs.list_comments, file_id, file_state.watermark
            )
        except Exception as exc:
            if _is_rate_limited(exc):
                failures = self._rate_failures.get(file_id, 0) + 1
                self._rate_failures[file_id] = failures
                delay = min(300.0, 2 ** (failures - 1)) * random.uniform(0.8, 1.2)
                self._rate_allowed_at[file_id] = time.monotonic() + delay
                LOGGER.warning("Google rate limit for document %s; backing off", file_id)
            else:
                LOGGER.exception("comment polling failed for document %s", file_id)
            return
        self._rate_failures.pop(file_id, None)
        self._rate_allowed_at.pop(file_id, None)

        candidates = comments
        if first_run:
            candidates = sorted(
                (comment for comment in comments if _is_actionable(comment, file_state)),
                key=lambda item: str(item.get("modifiedTime", "")),
                reverse=True,
            )[:100]
            candidates.reverse()
        for comment in candidates:
            if _is_actionable(comment, file_state):
                await self.handle_comment(file_id, comment)

        observed = [
            parsed
            for comment in comments
            if isinstance(comment.get("modifiedTime"), str)
            if (parsed := _parse_time(comment["modifiedTime"])) is not None
        ]
        margin = poll_started - timedelta(seconds=60)
        watermark = max([margin, *observed])
        file_state.watermark = _rfc3339(watermark)
        self.state.save(self.state_path)

    async def run_forever(self) -> None:
        """Recover persisted work and poll configured or discovered documents forever."""
        await self._recover_startup()
        discovered: list[str] = []
        next_discovery = 0.0
        while True:
            if self.cfg.docs:
                document_ids = self.cfg.docs
            else:
                now = time.monotonic()
                if now >= next_discovery:
                    try:
                        discovered = await asyncio.to_thread(self.gdocs.list_shared_docs)
                    except Exception:
                        LOGGER.exception("document discovery failed")
                    next_discovery = now + 600
                document_ids = discovered
            if len(document_ids) > self.cfg.max_docs:
                LOGGER.warning(
                    "document limit reached; polling %d of %d visible documents",
                    self.cfg.max_docs,
                    len(document_ids),
                )
                document_ids = document_ids[: self.cfg.max_docs]
            for file_id in document_ids:
                await self._poll_file(file_id)
            await asyncio.sleep(self.cfg.poll_interval_s)


def _locate_target(doc: Doc, quote: str | None) -> tuple[_Target | None, str | None]:
    if quote is None or not quote:
        return None, "select text before posting an @ai instruction"
    maps = _document_maps(doc)
    matches = [
        _Target(span=span, text_map=text_map, text=quote)
        for text_map in maps
        for span in text_map.find_all(quote)
    ]
    if not matches:
        return None, "the quoted text is no longer present; please re-trigger"
    if len(matches) > 1:
        return None, "the quoted text is not unique in the document; please select more context"
    target = matches[0]
    if target.span.tab_id != doc.tab_ids[0] or target.span.segment_id is not None:
        return None, "the target is outside the first tab body, which is read-only in v1"
    if not target.text_map.is_clean_span(target.span):
        return None, "the target overlaps a structural boundary or pending suggestion"
    return target, None


def _valid_proposal_target(doc: Doc, proposal: Proposal) -> _Target | None:
    target, _ = _locate_target(doc, proposal.target_text)
    if target is None:
        return None
    if target.span.tab_id != proposal.tab_id:
        return None
    if target.text_map.text_for_span(target.span) != proposal.target_text:
        return None
    return target


def _document_maps(doc: Doc) -> list[TextMap]:
    tabs = doc.raw.get("tabs")
    if not isinstance(tabs, list):
        return []
    maps: list[TextMap] = []
    for tab in _flatten_raw_tabs(tabs):
        properties = tab.get("tabProperties")
        document_tab = tab.get("documentTab")
        if not isinstance(properties, dict) or not isinstance(document_tab, dict):
            continue
        tab_id = properties.get("tabId")
        if not isinstance(tab_id, str):
            continue
        body = document_tab.get("body")
        if isinstance(body, dict):
            maps.append(build_text_map(body, tab_id))
        for collection_name in ("headers", "footers", "footnotes"):
            collection = document_tab.get(collection_name)
            if not isinstance(collection, dict):
                continue
            for segment_id, segment in collection.items():
                if isinstance(segment_id, str) and isinstance(segment, dict):
                    maps.append(build_text_map(segment, tab_id, segment_id))
    return maps


def _flatten_raw_tabs(tabs: list[object]) -> list[dict[str, Any]]:
    flattened: list[dict[str, Any]] = []
    for tab in tabs:
        if not isinstance(tab, dict):
            continue
        flattened.append(tab)
        children = tab.get("childTabs")
        if isinstance(children, list):
            flattened.extend(_flatten_raw_tabs(children))
    return flattened


def _pointer_text(doc: Doc, names: list[str]) -> tuple[list[tuple[str, str]], str | None]:
    maps = _document_maps(doc)
    tab_order = {tab_id: index for index, tab_id in enumerate(doc.tab_ids)}
    resolved: list[tuple[str, str]] = []
    for name in names:
        infos = doc.named_ranges.get(name)
        if not infos:
            return [], name
        spans = [span for info in infos for span in info.spans]
        if not spans:
            return [], name
        spans.sort(
            key=lambda span: (
                tab_order.get(span.tab_id, len(tab_order)),
                span.segment_id or "",
                span.start,
                span.end,
            )
        )
        values: list[str] = []
        for span in spans:
            value = next(
                (
                    text_map.text_for_span(span)
                    for text_map in maps
                    if _contains_span(text_map, span)
                ),
                None,
            )
            if value is None:
                return [], name
            values.append(value)
        resolved.append((name, "\n".join(values)))
    return resolved, None


def _contains_span(text_map: TextMap, span: Span) -> bool:
    try:
        text_map.to_py_span(span)
    except ValueError:
        return False
    return True


def _build_user_prompt(
    command: Instruct,
    target: _Target,
    pointers: list[tuple[str, str]],
    context_chars: int,
) -> str:
    py_start, py_end = target.text_map.to_py_span(target.span)
    before = target.text_map.text[max(0, py_start - context_chars) : py_start]
    after = target.text_map.text[py_end : py_end + context_chars]
    blocks = "\n\n".join(f"@[{name}]:\n{text}" for name, text in pointers) or "(none)"
    return (
        f"Instruction:\n{command.instruction}\n\n"
        f"Target text:\n{target.text}\n\n"
        f"Pointer context:\n{blocks}\n\n"
        f"Surrounding context:\n{before}[TARGET]{target.text}[/TARGET]{after}"
    )


def _strip_wrapping_fence(text: str) -> str:
    match = FENCE.fullmatch(text)
    return match.group(1) if match is not None else text


def _quote(comment: dict[str, object]) -> str | None:
    quoted = comment.get("quotedFileContent")
    if not isinstance(quoted, dict):
        return None
    value = quoted.get("value")
    return value if isinstance(value, str) else None


def _comment_id(comment: dict[str, object]) -> str:
    value = comment.get("id")
    if not isinstance(value, str):
        raise ValueError("comment is missing its ID")
    return value


def _modified_time(comment: dict[str, object]) -> str:
    value = comment.get("modifiedTime")
    if not isinstance(value, str):
        raise ValueError("comment is missing its modifiedTime")
    return value


def _is_actionable(comment: dict[str, object], file_state: FileState) -> bool:
    comment_id = comment.get("id")
    content = comment.get("content")
    if not isinstance(comment_id, str) or not isinstance(content, str) or parse(content) is None:
        return False
    if comment.get("resolved") is True or comment_id in file_state.seen:
        return False
    if comment_id in file_state.inflight or comment_id in file_state.pending_replies:
        return False
    author = comment.get("author")
    if isinstance(author, dict) and author.get("me") is True:
        return False
    replies = comment.get("replies")
    if isinstance(replies, list):
        for reply in replies:
            if isinstance(reply, dict):
                reply_author = reply.get("author")
                if isinstance(reply_author, dict) and reply_author.get("me") is True:
                    return False
    return True


def _parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _rfc3339(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _is_rate_limited(error: Exception) -> bool:
    return isinstance(error, HttpError) and getattr(error.resp, "status", None) in {403, 429}
