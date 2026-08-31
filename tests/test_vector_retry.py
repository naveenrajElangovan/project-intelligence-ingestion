import asyncio

from app import vector


def test_transient_local_connection_failure_is_retried(monkeypatch) -> None:
    attempts = 0
    delays: list[float] = []

    def operation():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TimeoutError("Chroma temporarily unavailable")
        return "ok"

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(vector.asyncio, "sleep", fake_sleep)
    result = asyncio.run(vector._with_retry(operation))

    assert result == "ok"
    assert attempts == 2
    assert len(delays) == 1
    assert delays[0] == 0.25


def test_permanent_chroma_contract_failure_is_not_retried(monkeypatch) -> None:
    attempts = 0

    def operation():
        nonlocal attempts
        attempts += 1
        raise ValueError("invalid collection contract")

    async def unexpected_sleep(_delay: float) -> None:
        raise AssertionError("Permanent failures must not sleep or retry")

    monkeypatch.setattr(vector.asyncio, "sleep", unexpected_sleep)
    try:
        asyncio.run(vector._with_retry(operation))
    except ValueError:
        pass
    else:
        raise AssertionError("Expected ValueError")

    assert attempts == 1
