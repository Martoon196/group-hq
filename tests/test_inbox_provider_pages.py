from dataclasses import replace

import httpx
import pytest

from app.services.inbox_providers import Account, InboxError, Zernio


def connection(handler):
    account = Account("page-test", "kp", "instagram", "Test", "zernio", "account-id", "profile-id", token="fixture")
    return Zernio(account, httpx.MockTransport(handler))


def page(ids, more=False, cursor=None, account="account-id"):
    data = [{"id": ident, "accountId": account, "participantId": "person-" + ident, "platform": "instagram"} for ident in ids]
    return httpx.Response(200, json={"data": data, "pagination": {"hasMore": more, "nextCursor": cursor}})


@pytest.fixture(autouse=True)
def empty_cache():
    Zernio._older_pages.clear()
    yield
    Zernio._older_pages.clear()


def test_older_conversations_accumulate_without_refetch_or_duplicates():
    seen = []
    def handle(request):
        assert request.url.params["accountId"] == "account-id" and request.url.params["profileId"] == "profile-id"
        cursor = request.url.params.get("cursor")
        seen.append(cursor)
        if cursor is None:
            return page(["new"], True, "older/+1==")
        if cursor == "older/+1==":
            return page(["new", "middle"], True, "last")
        assert cursor == "last"
        return page(["old"])
    provider = connection(handle)
    threads, more = provider.conversations()
    assert [r["id"] for r in threads] == ["new", "middle"] and more
    threads, more = provider.conversations()
    assert [r["id"] for r in threads] == ["new", "middle", "old"] and not more
    assert seen == [None, "older/+1==", None, "last"]
    rotated = Zernio(replace(provider.account, token="new-credential"), provider.transport)
    assert rotated.conversations()[1] and seen[-2:] == [None, "older/+1=="]


def test_transient_failure_retries_and_repeated_cursor_stops():
    calls = []
    def handle(request):
        cursor = request.url.params.get("cursor")
        calls.append(cursor)
        if cursor is None:
            return page(["new"], True, "stuck")
        if len(calls) == 2:
            return httpx.Response(429)
        return page(["older"], True, "stuck")
    provider = connection(handle)
    threads, more = provider.conversations()
    assert len(threads) == 1 and more
    threads, more = provider.conversations()
    assert len(threads) == 2 and more and len(calls) == 4


def test_later_page_cannot_cross_accounts():
    def handle(request):
        return page(["wrong"], account="another-account") if "cursor" in request.url.params else page(["new"], True, "next")
    with pytest.raises(InboxError, match="unbound_target"):
        connection(handle).conversations()


def test_missing_cursor_keeps_first_page_and_signals_partial():
    provider = connection(lambda request: page(["new"], True))
    threads, more = provider.conversations()
    assert len(threads) == 1 and more
