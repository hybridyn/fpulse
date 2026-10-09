"""Bounded, ephemeral API inspection; never creates a connector or saves secrets."""
from __future__ import annotations

import base64
import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import time
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit

import anyio
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from fpulse.auth.deps import require_min_rank
from fpulse.connectors.ai_authoring import parse_spec_text

router = APIRouter(prefix="/explorer", dependencies=[Depends(require_min_rank("developer"))])
LIMIT = 256 * 1024
SPEC_LIMIT = 32 * 1024 * 1024
REFERENCE_URLS = {
    'stripe': 'https://raw.githubusercontent.com/stripe/openapi/master/openapi/spec3.json',
    'github': 'https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/api.github.com.json',
    'slack': 'https://raw.githubusercontent.com/slackapi/slack-api-specs/master/web-api/slack_web_openapi_v2.json',
    'twilio': 'https://raw.githubusercontent.com/twilio/twilio-oai/main/spec/json/twilio_api_v2010.json',
    'plaid': 'https://raw.githubusercontent.com/plaid/plaid-openapi/master/2020-09-14.yml',
    'digitalocean': 'https://api-engineering.nyc3.cdn.digitaloceanspaces.com/spec-ci/DigitalOcean-public.v2.yaml',
}
SENSITIVE = re.compile(r"password|secret|token|authorization|api.?key|cookie", re.I)


class InspectRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    method: Literal['GET', 'HEAD', 'OPTIONS', 'POST', 'PUT', 'PATCH', 'DELETE'] = 'GET'
    headers: dict[str, str] = Field(default_factory=dict, max_length=40)
    query: dict[str, str] = Field(default_factory=dict, max_length=40)
    body: str = Field(default='', max_length=32768)
    auth_type: Literal['none', 'bearer', 'basic', 'api_key', 'api_key_query'] = 'none'
    token: str = Field(default='', max_length=8192)
    username: str = Field(default='', max_length=512)
    password: str = Field(default='', max_length=8192)
    key_name: str = Field(default='X-API-Key', max_length=128)
    confirm_write: bool = False


def _tls_context() -> ssl.SSLContext:
    """Verifying TLS context with TLS 1.0/1.1 refused.

    `ssl.create_default_context()` verifies certificates and hostnames, but it
    leaves TLS 1.0 and 1.1 permitted on builds whose OpenSSL still offers them.
    The Explorer reaches arbitrary third-party hosts, so pin the floor
    explicitly rather than inherit whatever the platform happens to allow.
    """
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx

def _target(url):
    parsed = urlsplit(url)
    if parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise ValueError('Use an HTTP(S) URL without embedded credentials or a fragment')
    addresses = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80), type=socket.SOCK_STREAM)
    allow_private = os.getenv('FPULSE_API_SOURCE_ALLOW_PRIVATE', '').lower() in ('1', 'true', 'yes')
    for entry in addresses:
        ip = ipaddress.ip_address(entry[4][0])
        if ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified or (not allow_private and (ip.is_private or ip.is_loopback)):
            raise ValueError('Target blocked by network policy; internal APIs require FPULSE_API_SOURCE_ALLOW_PRIVATE=1')
    if not addresses:
        raise ValueError('No address found')
    return parsed, addresses[0]


def _request_parts(req):
    if sum(len(k) + len(v) for k, v in [*req.headers.items(), *req.query.items()]) > 32768:
        raise ValueError('Headers and query parameters exceed 32 KB')
    if '{' in req.url or '}' in req.url:
        raise ValueError('Replace path and server placeholders with actual values before sending')
    if req.method not in ('GET', 'HEAD', 'OPTIONS') and not req.confirm_write:
        raise ValueError('Confirm this request may modify remote data')
    headers = {'Accept': 'application/json', 'Accept-Encoding': 'identity'}
    for key, value in req.headers.items():
        if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", key) or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError('Invalid request header')
        if key.lower() in ('host', 'content-length', 'transfer-encoding', 'connection', 'proxy-authorization', 'accept-encoding', 'authorization', 'cookie'):
            raise ValueError('Use authentication fields; transport and cookie headers cannot be overridden')
        headers = {k: v for k, v in headers.items() if k.lower() != key.lower()}
        headers[key] = value
    query = dict(req.query)
    secrets = [req.token, req.password, req.username]
    if req.auth_type == 'basic':
        if not req.username or ':' in req.username:
            raise ValueError('Basic authentication requires a username without a colon')
        encoded = base64.b64encode(f'{req.username}:{req.password}'.encode()).decode()
        headers['Authorization'] = 'Basic ' + encoded
        secrets.append(encoded)
    elif req.auth_type != 'none':
        if not req.token or any(ord(c) < 32 or ord(c) == 127 for c in req.token):
            raise ValueError('Authentication value is missing or invalid')
        if req.auth_type == 'bearer':
            headers['Authorization'] = 'Bearer ' + req.token
        else:
            if not re.fullmatch(r'[A-Za-z0-9_-]+', req.key_name) or req.key_name.lower() in ('host', 'content-length', 'transfer-encoding', 'connection'):
                raise ValueError('Invalid API key name')
            if req.auth_type == 'api_key':
                headers = {k: v for k, v in headers.items() if k.lower() != req.key_name.lower()}
                headers[req.key_name] = req.token
            else:
                query[req.key_name] = req.token
    secrets.extend(req.headers.values())
    secrets.extend(v for k, v in req.query.items() if SENSITIVE.search(k))
    return headers, query, [s for s in secrets if s]


def _redact(value, secrets, depth=0):
    if depth > 16:
        return '[depth limit]'
    if isinstance(value, dict):
        return {k: '[redacted]' if SENSITIVE.search(k) else _redact(v, secrets, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v, secrets, depth + 1) for v in value[:100]]
    if isinstance(value, str):
        for secret in sorted(secrets, key=len, reverse=True):
            value = value.replace(secret, '[redacted]')
    return value


def inspect_request(req: InspectRequest):
    headers, query, secrets = _request_parts(req)
    parsed, address = _target(req.url)
    original_query = parse_qsl(parsed.query, keep_blank_values=True)
    secrets.extend(v for k, v in original_query if SENSITIVE.search(k))
    query_string = urlencode([(k, v) for k, v in original_query if k not in query] + list(query.items()))
    path = (parsed.path or '/') + ('?' + query_string if query_string else '')
    if req.body and not any(k.lower() == 'content-type' for k in headers):
        headers['Content-Type'] = 'application/json'
    started = time.monotonic()
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80), timeout=15)
    sock = None
    try:
        # Pin the validated address. TLS still verifies the original hostname.
        family, socktype, proto, _, sockaddr = address
        sock = socket.socket(family, socktype, proto)
        sock.settimeout(15)
        sock.connect(sockaddr)
        if parsed.scheme == 'https':
            sock = _tls_context().wrap_socket(sock, server_hostname=parsed.hostname)
        conn.sock = sock
        conn.request(req.method, path, body=req.body.encode() if req.body else None, headers=headers)
        response = conn.getresponse()
        raw = bytearray()
        while len(raw) <= LIMIT:
            remaining = 15 - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError()
            sock.settimeout(remaining)
            chunk = response.read1(min(16384, LIMIT + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        truncated = len(raw) > LIMIT
        text = bytes(raw[:LIMIT]).decode('utf-8', errors='replace')
        try:
            data = json.loads(text) if not truncated else None
        except (ValueError, RecursionError):
            data = None
        return {'status': response.status, 'elapsed_ms': round((time.monotonic() - started) * 1000),
                'bytes': len(raw[:LIMIT]), 'truncated': truncated,
                'headers': {k: v for k, v in response.getheaders() if k.lower() in ('content-type', 'date', 'retry-after')},
                'data': _redact(data, secrets), 'text': _redact(text, secrets) if data is None else '',
                'sample_limit': 100}
    finally:
        conn.close()
        if sock is not None:
            sock.close()


@router.post('/request')
async def send_request(req: InspectRequest):
    try:
        return await anyio.to_thread.run_sync(inspect_request, req)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    except Exception:
        raise HTTPException(502, 'Request failed. Check the address, network access and TLS certificate trust.') from None


class DiscoverRequest(BaseModel):
    text: str = Field(max_length=2 * 1024 * 1024)


def discover_spec(spec):
    try:
        if not isinstance(spec, dict) or not (spec.get('openapi') or spec.get('swagger')):
            raise ValueError('Not an OpenAPI specification')
        paths = spec.get('paths')
        if not isinstance(paths, dict):
            raise ValueError('Specification must contain paths')
        servers = spec.get('servers') or []
        base = servers[0].get('url', '') if servers else ''
        if not base and spec.get('host'):
            base = (spec.get('schemes') or ['https'])[0] + '://' + spec['host'] + spec.get('basePath', '')
        operations = []
        for path, item in paths.items():
            if not isinstance(path, str) or not path.startswith('/'):
                raise ValueError('Invalid operation path')
            if not isinstance(item, dict):
                continue
            for method, operation in item.items():
                if not isinstance(method, str):
                    raise ValueError('Invalid operation method')
                if method.upper() not in ('GET', 'HEAD', 'OPTIONS', 'POST', 'PUT', 'PATCH', 'DELETE') or not isinstance(operation, dict):
                    continue
                security = operation.get('security', spec.get('security', []))
                if not isinstance(security, list) or any(not isinstance(option, dict) or any(not isinstance(name, str) for name in option) for option in security):
                    raise ValueError('Invalid security requirements')
                operations.append({'path': path, 'method': method.upper(), 'summary': str(operation.get('summary', ''))[:200],
                                   'security': security})
                if len(operations) >= 5000:
                    break
            if len(operations) >= 5000:
                break
        return {'base_url': base, 'operations': operations, 'limit': 5000,
                'limit_reached': len(operations) == 5000}
    except (ValueError, TypeError, AttributeError, IndexError):
        raise HTTPException(400, 'Invalid OpenAPI JSON/YAML specification') from None


@router.post('/discover')
async def discover(req: DiscoverRequest):
    try:
        return await anyio.to_thread.run_sync(lambda: discover_spec(parse_spec_text(req.text)))
    except (ValueError, RecursionError):
        raise HTTPException(400, 'Invalid OpenAPI JSON/YAML specification (maximum 2 MB)') from None


class ReferenceRequest(BaseModel):
    reference: Literal['stripe', 'github', 'slack', 'twilio', 'plaid', 'digitalocean']


def load_reference(reference: str):
    # Only curated public URLs; never forward app credentials or follow redirects.
    url = REFERENCE_URLS[reference]
    parsed, address = _target(url)
    started = time.monotonic()
    conn = http.client.HTTPConnection(parsed.hostname, 443, timeout=30)
    sock = None
    try:
        family, kind, proto, _, sockaddr = address
        sock = socket.socket(family, kind, proto)
        sock.settimeout(30)
        sock.connect(sockaddr)
        sock = _tls_context().wrap_socket(sock, server_hostname=parsed.hostname)
        conn.sock = sock
        conn.request('GET', parsed.path, headers={'Accept-Encoding': 'identity', 'User-Agent': 'F-Pulse-API-Explorer'})
        response = conn.getresponse()
        if response.status != 200:
            raise ValueError(f'Specification source returned HTTP {response.status}. Redirects are not followed.')
        length = response.getheader('Content-Length')
        if length and int(length) > SPEC_LIMIT:
            raise ValueError('Specification exceeds the 32 MB remote import limit')
        raw = bytearray()
        while len(raw) <= SPEC_LIMIT:
            remaining = 30 - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError()
            sock.settimeout(remaining)
            chunk = response.read1(min(65536, SPEC_LIMIT + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        if len(raw) > SPEC_LIMIT:
            raise ValueError('Specification exceeds the 32 MB remote import limit')
        text = raw.decode('utf-8-sig')
        # Large JSON specs use the dedicated bounded path. YAML retains its tighter
        # shared parser limit because expansion can consume substantially more memory.
        spec = json.loads(text) if text.lstrip().startswith('{') else parse_spec_text(text)
        result = discover_spec(spec)
        return {**result, 'reference': reference, 'source_url': url, 'bytes': len(raw)}
    finally:
        conn.close()
        if sock is not None:
            sock.close()


@router.post('/reference')
async def import_reference(req: ReferenceRequest):
    try:
        return await anyio.to_thread.run_sync(load_reference, req.reference)
    except HTTPException:
        raise
    except (ValueError, RecursionError) as exc:
        raise HTTPException(400, str(exc)[:240]) from None
    except Exception:
        raise HTTPException(502, 'Specification download failed. Check network access and TLS certificate trust, then retry.') from None
