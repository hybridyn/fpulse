import json

import httpx
import pytest

from fpulse.ai.openai_client import OpenAIAgentClient


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [200, 400, 401, 429])
async def test_agent_parameter_retry_is_bounded_and_preserves_tools(monkeypatch, status):
    from fpulse.planner import ai_client
    from fpulse.ai import openai_client
    monkeypatch.setattr(ai_client, 'resolve_provider', lambda **kwargs: ('openai', 'secret-canary', 'test-model', ''))
    requests = []
    def respond(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1 and status != 200:
            return httpx.Response(status, json={'error': {
                'param': 'max_tokens', 'code': 'unsupported_parameter',
                'message': 'Use max_completion_tokens instead. secret-canary'}})
        return httpx.Response(200, json={'choices': [{'message': {'content': 'Ready'}, 'finish_reason': 'stop'}]})
    factory = httpx.AsyncClient
    monkeypatch.setattr(openai_client.httpx, 'AsyncClient',
        lambda **kwargs: factory(transport=httpx.MockTransport(respond), **kwargs))
    tools = [{'name': 'inspect', 'input_schema': {'type': 'object'}}]
    client = OpenAIAgentClient()
    if status in (401, 429):
        with pytest.raises(RuntimeError) as error:
            await client.call(system='system', messages=[{'role': 'user', 'content': 'help'}], tools=tools)
        assert f'HTTP {status}' in str(error.value)
        assert 'secret-canary' not in str(error.value)
        assert len(requests) == 1
    else:
        result = await client.call(system='system', messages=[{'role': 'user', 'content': 'help'}], tools=tools)
        assert result.text == 'Ready'
        assert len(requests) == (2 if status == 400 else 1)
        if status == 400:
            assert requests[1]['max_completion_tokens'] == client.max_tokens_per_turn
            assert 'max_tokens' not in requests[1]
            assert requests[1]['tools'] == requests[0]['tools']
            assert requests[1]['messages'] == requests[0]['messages']
            assert requests[1]['store'] is False


@pytest.mark.parametrize('error', [
    {'param': 'temperature', 'code': 'unsupported_parameter', 'message': 'max_completion_tokens'},
    {'param': 'max_tokens', 'code': 'other', 'message': 'max_completion_tokens'},
    'invalid',
])
def test_unrelated_bad_requests_are_not_retried(error):
    from fpulse.ai.openai_client import _requires_completion_tokens
    assert not _requires_completion_tokens(httpx.Response(400, json={'error': error}))


def test_entire_tool_registry_has_explicit_object_properties_without_mutation():
    from copy import deepcopy
    from fpulse.ai.tools import INITIAL_TOOLS
    from fpulse.ai.openai_client import _openai_schema
    def check(node):
        if isinstance(node, dict):
            if node.get('type') == 'object':
                assert isinstance(node.get('properties'), dict)
            for child in node.values():
                check(child)
        elif isinstance(node, list):
            for child in node:
                check(child)
    for tool in INITIAL_TOOLS:
        before = deepcopy(tool.input_schema)
        check(_openai_schema(tool.input_schema))
        assert tool.input_schema == before
    dynamic = {'type': 'object', 'description': 'Op-specific args'}
    assert _openai_schema(dynamic) == {**dynamic, 'properties': {}}


def test_provider_error_fields_exclude_message_and_malformed_values():
    from fpulse.ai.openai_client import _error_fields
    response = httpx.Response(400, json={'error': {
        'code': 'invalid_function_parameters', 'param': 'tools[3].function.parameters',
        'message': 'secret-canary and private prompt'}})
    text = _error_fields(response)
    assert 'invalid_function_parameters' in text
    assert 'tools[3].function.parameters' in text
    assert 'secret-canary' not in text
    assert _error_fields(httpx.Response(400, json={'error': {'param': 'Bearer secret value'}})) == ''
