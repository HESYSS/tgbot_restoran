import json
import httpx
import pytest
from booking_agent.providers import GroqProvider
from booking_agent.models import BookingRequest
from test_llm_policy import run, decision


def client_stub(monkeypatch, responses):
    calls = []
    class Client:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def post(self, url, **kwargs):
            calls.append(kwargs['json'])
            return responses.pop(0)
    monkeypatch.setenv('GROQ_API_KEY', 'test-key')
    monkeypatch.setattr(httpx, 'AsyncClient', Client)
    return calls


def response(status, body):
    return httpx.Response(status, json=body, request=httpx.Request('POST', 'https://example.test'))


def schema_error():
    return response(400, {'error': {'code': 'json_validate_failed', 'failed_generation': 'untrusted'}})


def valid(value=None):
    result = decision('offer_time', offered_times=['17:30', '18:00'], client_question=None) if value is None else value
    return response(200, {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(result)}}]})


def test_schema_failure_retries_once_with_same_context_and_strict_guard(monkeypatch):
    calls = client_stub(monkeypatch, [schema_error(), valid()])
    result = run(GroqProvider().decide(BookingRequest('Test Bistro', '2030-01-15'), []))
    assert result['offered_times'] == ['17:30', '18:00']
    assert len(calls) == 2
    assert calls[0]['messages'] == calls[1]['messages']
    assert calls[1]['temperature'] == 0
    assert calls[1]['response_format']['json_schema']['strict'] is True


def test_repeated_schema_error_stops_after_two_calls(monkeypatch):
    calls = client_stub(monkeypatch, [schema_error(), schema_error()])
    with pytest.raises(httpx.HTTPStatusError):
        run(GroqProvider().decide(BookingRequest('Test Bistro', '2030-01-15'), []))
    assert len(calls) == 2


@pytest.mark.parametrize('status', [401, 403, 429, 500])
def test_other_errors_are_not_retried(monkeypatch, status):
    calls = client_stub(monkeypatch, [response(status, {'error': {'code': 'other'}})])
    with pytest.raises(httpx.HTTPStatusError):
        run(GroqProvider().decide(BookingRequest('Test Bistro', '2030-01-15'), []))
    assert len(calls) == 1


def test_invalid_fact_types_are_not_used(monkeypatch):
    value = decision('offer_time', offered_times=['17:30'], client_question=None, guests=True)
    client_stub(monkeypatch, [valid(value)])
    with pytest.raises(ValueError, match='fact fields'):
        run(GroqProvider().decide(BookingRequest('Test Bistro', '2030-01-15'), []))
