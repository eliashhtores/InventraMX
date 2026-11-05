from django.db.models import ProtectedError
from django.utils.translation import gettext as _
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler


def api_exception_handler(exc, context):
    """Extend DRF's handler so deleting referenced records returns 409 instead of 500."""
    if isinstance(exc, ProtectedError):
        return Response(
            {"detail": _(
                "No se puede eliminar este registro porque otros registros lo utilizan. Desactívalo en su lugar."
            )},
            status=status.HTTP_409_CONFLICT,
        )
    return exception_handler(exc, context)
