from __future__ import annotations

import ipaddress
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path


class ProviderError(RuntimeError):
    """Safe error codes only; never expose response bodies or credentials."""


def load_env(path: Path) -> None:
    if not path.is_file():
        return
    allowed = {'CASEFLOW_API_KEY', 'CASEFLOW_BASE_URL', 'CASEFLOW_MODEL', 'DEEPSEEK_API_KEY'}
    for raw in path.read_text(encoding='utf-8-sig').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        if key.strip() in allowed:
            os.environ.setdefault(key.strip(), value.strip().strip('\"\''))


@dataclass(frozen=True)
class Config:
    api_key: str
    base_url: str = 'https://api.deepseek.com'
    model: str = 'deepseek-flash'
    timeout: int = 45

    @classmethod
    def from_env(cls):
        return cls(os.getenv('CASEFLOW_API_KEY') or os.getenv('DEEPSEEK_API_KEY', ''),
                   os.getenv('CASEFLOW_BASE_URL', cls.base_url),
                   os.getenv('CASEFLOW_MODEL', cls.model))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Provider:
    """One HTTP attempt per reserved model step; no implicit retries."""

    def __init__(self, config: Config):
        self.config = config
        if not config.api_key:
            raise ProviderError('missing_api_key')
        try:
            url = urllib.parse.urlsplit(config.base_url)
            local = url.hostname == 'localhost'
            if url.hostname and not local:
                try:
                    local = ipaddress.ip_address(url.hostname).is_loopback
                except ValueError:
                    pass
            if (not url.hostname or url.username or url.password or url.query or url.fragment
                    or url.scheme not in {'https', 'http'} or (url.scheme == 'http' and not local)):
                raise ValueError
            url.port
        except ValueError:
            raise ProviderError('invalid_base_url') from None
        self.url = config.base_url.rstrip('/') + '/chat/completions'
        self.opener = urllib.request.build_opener(NoRedirect())

    def complete(self, messages: list, tools: list) -> dict:
        body = {'model': self.config.model, 'messages': messages, 'tools': tools,
                'tool_choice': 'required', 'parallel_tool_calls': False,
                'temperature': 0, 'max_tokens': 1200}
        if urllib.parse.urlsplit(self.url).hostname == 'api.deepseek.com':
            body['thinking'] = {'type': 'disabled'}
        request = urllib.request.Request(self.url, data=json.dumps(body).encode('utf-8'),
                    headers={'Authorization': 'Bearer ' + self.config.api_key,
                             'Content-Type': 'application/json'}, method='POST')
        try:
            with self.opener.open(request, timeout=self.config.timeout) as response:
                raw = response.read(1_048_577)
            if len(raw) > 1_048_576:
                raise ProviderError('model_response_too_large')
            payload = json.loads(raw)
        except urllib.error.HTTPError as error:
            code = error.code
            error.close()
            raise ProviderError(f'provider_http_{code}') from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ProviderError('provider_connection_failed') from None
        except (ValueError, UnicodeError):
            raise ProviderError('provider_invalid_json') from None
        try:
            msg = payload['choices'][0]['message']
            if not isinstance(msg, dict) or msg.get('role') != 'assistant':
                raise ValueError
            calls = msg.get('tool_calls') or []
            if not isinstance(calls, list) or len(calls) > 4:
                raise ValueError
            ids = set()
            for call in calls:
                if (not isinstance(call, dict) or call.get('type') != 'function'
                        or not isinstance(call.get('id'), str) or not call['id']
                        or call['id'] in ids or len(call['id']) > 128
                        or not isinstance(call['function']['name'], str)
                        or not isinstance(call['function']['arguments'], str)
                        or len(call['function']['arguments']) > 16000):
                    raise ValueError
                ids.add(call['id'])
            tokens = (payload.get('usage') or {}).get('total_tokens')
            if tokens is not None and (type(tokens) is not int or tokens < 0):
                raise ValueError
            # Keep only protocol fields; private reasoning is not persisted or exposed.
            clean = {'role': 'assistant', 'content': msg.get('content')}
            if clean['content'] is not None and not isinstance(clean['content'], str):
                raise ValueError
            if calls:
                clean['tool_calls'] = calls
            return {'message': clean, 'tokens': tokens}
        except (KeyError, IndexError, TypeError, ValueError, AttributeError):
            raise ProviderError('provider_invalid_response') from None
