"""Runnable JSON HTTP adapter for a separately deployed acoustic model service."""
from __future__ import annotations

import json
import os
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from .providers import PerceptionResult, validate_perception_result


class HttpAcousticProvider:
    def __init__(self, endpoint, *, timeout_s=0.8, token_env=None):
        parsed = urlsplit(endpoint)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('model endpoint must be HTTP(S), without URL credentials')
        if not 0.01 <= timeout_s <= 60:
            raise ValueError('invalid model HTTP timeout')
        self.endpoint, self.timeout_s, self.token_env = endpoint, timeout_s, token_env

    def infer(self, frame):
        if not frame.audio_ref:
            raise ValueError('real inference needs audio_ref from a capture/file adapter')
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json'}
        if self.token_env:
            token = os.environ.get(self.token_env)
            if not token:
                raise ValueError('model service token environment variable is missing')
            headers['Authorization'] = 'Bearer ' + token
        request = Request(self.endpoint, json.dumps(frame.to_dict()).encode(), headers, method='POST')
        with urlopen(request, timeout=self.timeout_s) as response:
            raw = response.read(65537)
        if len(raw) > 65536:
            raise ValueError('model response exceeds 64 KiB')
        payload = json.loads(raw)
        result = PerceptionResult(**payload)
        return validate_perception_result(result, frame)
