from django.db.models import ProtectedError
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler


def api_exception_handler(exc, context):
    """Extend DRF's handler so deleting referenced records returns 409 instead of 500."""
    if isinstance(exc, ProtectedError):
        return Response(
            {"detail": "This record is referenced by other records and cannot be deleted. Deactivate it instead."},
            status=status.HTTP_409_CONFLICT,
        )
    return exception_handler(exc, context)
