from __future__ import annotations

from typing import Any, Callable, Coroutine

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import logger


_STATUS_CODES = {
    400: "BAD_REQUEST",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    413: "PAYLOAD_TOO_LARGE",
    415: "UNSUPPORTED_MEDIA_TYPE",
    422: "VALIDATION_ERROR",
    429: "RATE_LIMITED",
    500: "INTERNAL_ERROR",
    502: "UPSTREAM_ERROR",
    503: "SERVICE_UNAVAILABLE",
}


class ApiError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        details: Any = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = int(status_code)
        self.code = str(code or _STATUS_CODES.get(self.status_code, "API_ERROR"))
        self.message = str(message or "Request failed")
        self.details = {} if details is None else details
        self.headers = headers or {}


def request_id(request: Request) -> str:
    value = str(getattr(request.state, "request_id", "") or "").strip()
    if value:
        return value
    # The gateway middleware normally owns request IDs. This fallback keeps the
    # router usable in focused tests and embedded FastAPI applications.
    import secrets

    value = f"req_{secrets.token_hex(12)}"
    request.state.request_id = value
    return value


def error_response(request: Request, error: ApiError) -> JSONResponse:
    req_id = request_id(request)
    headers = {"X-Request-Id": req_id, **error.headers}
    return JSONResponse(
        status_code=error.status_code,
        headers=headers,
        content={
            "error": {
                "code": error.code,
                "message": error.message,
                "request_id": req_id,
                "details": jsonable_encoder(error.details),
            }
        },
    )


def from_http_exception(exc: HTTPException | StarletteHTTPException) -> ApiError:
    detail = exc.detail
    if isinstance(detail, dict):
        message = str(detail.get("message") or detail.get("detail") or "Request failed")
        details: Any = detail
    else:
        message = str(detail or "Request failed")
        details = {}
    return ApiError(
        int(exc.status_code),
        _STATUS_CODES.get(int(exc.status_code), "API_ERROR"),
        message,
        details=details,
        headers=dict(exc.headers or {}),
    )


class AgentApiRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            try:
                response = await original(request)
                response.headers.setdefault("X-Request-Id", request_id(request))
                return response
            except ApiError as exc:
                return error_response(request, exc)
            except RequestValidationError as exc:
                return error_response(
                    request,
                    ApiError(
                        422,
                        "VALIDATION_ERROR",
                        "Request validation failed",
                        details={"errors": exc.errors()},
                    ),
                )
            except HTTPException as exc:
                return error_response(request, from_http_exception(exc))
            except Exception:
                req_id = request_id(request)
                logger.exception("agent api request failed request_id=%s path=%s", req_id, request.url.path)
                return error_response(
                    request,
                    ApiError(500, "INTERNAL_ERROR", "Internal server error"),
                )

        return handler


def install_agent_api_error_handlers(app: FastAPI) -> None:
    """Standardize router-level 404/405/validation errors without changing other APIs."""

    def is_agent_path(request: Request) -> bool:
        path = request.url.path
        return path == "/api/v1" or path.startswith("/api/v1/")

    async def handle_http(request: Request, exc: StarletteHTTPException) -> Response:
        if is_agent_path(request):
            return error_response(request, from_http_exception(exc))
        return await http_exception_handler(request, exc)

    async def handle_validation(request: Request, exc: RequestValidationError) -> Response:
        if is_agent_path(request):
            return error_response(
                request,
                ApiError(
                    422,
                    "VALIDATION_ERROR",
                    "Request validation failed",
                    details={"errors": exc.errors()},
                ),
            )
        return await request_validation_exception_handler(request, exc)

    app.add_exception_handler(StarletteHTTPException, handle_http)
    app.add_exception_handler(RequestValidationError, handle_validation)
