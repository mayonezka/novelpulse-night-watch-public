"""Safe network diagnostics shared by lightweight and desktop nodes."""

import httpx


def failure_reason(error: Exception) -> str:
    if isinstance(error, httpx.HTTPStatusError):
        request = error.request
        return f'{request.method} {request.url.host}{request.url.path}: HTTP {error.response.status_code}'
    if isinstance(error, httpx.RequestError):
        return f'{type(error).__name__}: {error.request.url.host} — сетевая ошибка или таймаут'
    return f'{type(error).__name__}: {str(error)[:1600]}'
