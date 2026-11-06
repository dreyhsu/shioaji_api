"""Lost HTTP responses must recover by reading the original durable command."""
import io
from urllib.error import URLError

from fastapi import HTTPException
import pytest
import trade_client as tc


@pytest.mark.parametrize('post_lost', [True, False])
def test_lost_response_recovers_by_get_without_reposting(tmp_path, monkeypatch, post_lost):
    client = tc.TradeClient(tmp_path / 'test.db', 'TMFR1')
    monkeypatch.setattr(client, '_snapshot', lambda: {})
    calls, reads = [], []
    def transport(path, payload=None):
        calls.append((path, payload))
        if payload is not None:
            if post_lost:
                raise HTTPException(504, 'response lost')
            return {'state': 'accepted'}
        reads.append(path)
        if len(reads) == 1:
            raise HTTPException(503, 'transient read outage')
        return {'state': 'succeeded', 'result': {'state': 'armed'}}
    monkeypatch.setattr(client, '_request', transport)
    token = tc.command_id.set('original-id')
    try:
        assert client._command('arm_curve', {}) == {'state': 'armed'}
    finally:
        tc.command_id.reset(token)
    assert sum(payload is not None for _, payload in calls) == 1
    assert all(path == '/api/commands/original-id' for path in reads)


def test_unresolved_post_keeps_id_in_pending_error(tmp_path, monkeypatch):
    client = tc.TradeClient(tmp_path / 'test.db', 'TMFR1', timeout=0)
    monkeypatch.setattr(client, '_snapshot', lambda: {})
    calls = []
    def lost(path, payload=None):
        calls.append(path)
        raise HTTPException(504, 'timeout')
    monkeypatch.setattr(client, '_request', lost)
    token = tc.command_id.set('keep-this-id')
    try:
        with pytest.raises(HTTPException) as caught:
            client._command('arm_curve')
    finally:
        tc.command_id.reset(token)
    assert caught.value.status_code == 504 and 'keep-this-id' in caught.value.detail
    assert calls == ['/api/commands']


@pytest.mark.parametrize('error,status', [(TimeoutError(), 504), (URLError(TimeoutError()), 504),
                                        (ConnectionRefusedError(), 503)])
def test_transport_timeout_is_distinct_from_connection_failure(tmp_path, monkeypatch, error, status):
    client = tc.TradeClient(tmp_path / 'test.db', 'TMFR1', request_timeout=8)
    def fail(request, timeout):
        assert timeout == 8
        raise error
    monkeypatch.setattr(tc, 'urlopen', fail)
    with pytest.raises(HTTPException) as caught:
        client.command('same-id')
    assert caught.value.status_code == status
    assert ('逾時' in caught.value.detail) == (status == 504)


def test_transport_uses_configured_timeout(tmp_path, monkeypatch):
    client = tc.TradeClient(tmp_path / 'test.db', 'TMFR1', request_timeout=7)
    def response(request, timeout):
        assert timeout == 7
        return io.StringIO('{"state":"succeeded"}')
    monkeypatch.setattr(tc, 'urlopen', response)
    assert client.command('same-id')['state'] == 'succeeded'
