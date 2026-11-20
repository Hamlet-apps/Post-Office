"""Errors use {"error": {"code": "...", "message": "..."}} with a matching HTTP status."""
from rest_framework import status
from rest_framework.exceptions import APIException
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_handler


class ApiError(APIException):
    def __init__(self, code, message, http_status=status.HTTP_400_BAD_REQUEST):
        super().__init__(detail=message)
        self.error_code = code
        self.status_code = http_status


def error_response(code, message, http_status=400):
    return Response({"error": {"code": code, "message": message}}, status=http_status)


def exception_handler(exc, context):
    response = drf_handler(exc, context)
    if response is None:
        return None
    code = getattr(exc, "error_code", None) or getattr(exc, "default_code", "error")
    detail = getattr(exc, "detail", "")
    message = detail if isinstance(detail, str) else str(detail)
    response.data = {"error": {"code": str(code), "message": str(message)}}
    return response
