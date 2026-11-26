import logging
from contextlib import asynccontextmanager, contextmanager
from time import monotonic

import httpx


log = logging.getLogger("app.api")
API_CONTEXT = "partyup_api"


class InvalidAPIResponse(ValueError):
    pass


def api_issue(provider, operation, reason):
    log.warning("provider=%s operation=%s error=%s", provider, operation, reason)


def api_skipped(provider, operation, reason):
    log.info("provider=%s operation=%s skipped=%s", provider, operation, reason)


@contextmanager
def log_network_errors(provider, operation, method):
    start = monotonic()
    try:
        yield start
    except (httpx.RequestError, httpx.InvalidURL) as error:
        log.warning(
            "provider=%s operation=%s method=%s error=%s elapsed_ms=%.0f",
            provider, operation, method, type(error).__name__, (monotonic() - start) * 1000,
        )
        raise


def log_response(response, provider, operation, method, start):
    response.extensions[API_CONTEXT] = (provider, operation)
    retry_after = response.headers.get("Retry-After", "")
    retry = f" retry_after_s={retry_after}" if (
        len(retry_after) <= 9 and retry_after.isascii() and retry_after.isdecimal()
    ) else ""
    log.log(
        logging.WARNING if response.is_error else logging.INFO,
        "provider=%s operation=%s method=%s status=%d elapsed_ms=%.0f%s",
        provider, operation, method, response.status_code, (monotonic() - start) * 1000, retry,
    )


async def request_api(client: httpx.AsyncClient, provider: str, operation: str, method: str, url: str, **kwargs):
    # Provider and operation are internal labels. URLs, headers and bodies can contain credentials or IDs.
    with log_network_errors(provider, operation, method) as start:
        response = await client.request(method, url, **kwargs)
        log_response(response, provider, operation, method, start)
        return response


@asynccontextmanager
async def stream_api(client: httpx.AsyncClient, provider: str, operation: str, method: str, url: str, **kwargs):
    with log_network_errors(provider, operation, method) as start:
        async with client.stream(method, url, **kwargs) as response:
            log_response(response, provider, operation, method, start)
            yield response


def read_json(response: httpx.Response, expected: type | None = None):
    provider, operation = response.extensions.get(API_CONTEXT, ("external", "json"))
    try:
        payload = response.json()
    except ValueError:
        api_issue(provider, operation, "invalid_json")
        raise InvalidAPIResponse("Invalid API JSON") from None
    if expected is not None and not isinstance(payload, expected):
        api_issue(provider, operation, "unexpected_payload")
        raise InvalidAPIResponse("Unexpected API payload")
    return payload
