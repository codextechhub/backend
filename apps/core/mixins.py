# core/mixins.py
#
# Drop-in mixins for DRF generic views and viewsets.
# Override retrieve / create / update / destroy so every non-paginated
# response is wrapped in the standard envelope:
#
#   { "success": true, "message": "...", "data": { ... } }
#
# Usage (generic view):
#   class MyView(RetrieveModelMixin, generics.RetrieveAPIView): ...
#
# Usage (viewset):
#   class MyViewSet(XVSModelViewSetMixin, viewsets.ModelViewSet): ...

from rest_framework import status
from rest_framework.response import Response

from .response import success_response


class RetrieveModelMixin:
    """
    Wraps the default retrieve() response in the success envelope.
    Replaces rest_framework.mixins.RetrieveModelMixin.
    """

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        serializer = self.get_serializer(instance)
        return success_response(
            message="Data retrieved successfully.",
            data=serializer.data,
        )


class CreateModelMixin:
    """
    Wraps the default create() response in the success envelope.
    Replaces rest_framework.mixins.CreateModelMixin.
    """

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        return success_response(
            message="Created successfully.",
            data=serializer.data,
            status=status.HTTP_201_CREATED,
        )

    def perform_create(self, serializer):
        serializer.save()


class UpdateModelMixin:
    """
    Wraps the default update() / partial_update() response in the success envelope.
    Replaces rest_framework.mixins.UpdateModelMixin.
    """

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop("partial", False)
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)

        # Invalidate prefetch cache after update.
        if getattr(instance, "_prefetched_objects_cache", None):
            instance._prefetched_objects_cache = {}

        return success_response(
            message="Updated successfully.",
            data=serializer.data,
        )

    def perform_update(self, serializer):
        serializer.save()

    def partial_update(self, request, *args, **kwargs):
        kwargs["partial"] = True
        return self.update(request, *args, **kwargs)


class DestroyModelMixin:
    """
    Wraps the default destroy() response in the success envelope (HTTP 200).
    Replaces rest_framework.mixins.DestroyModelMixin.
    """

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        self.perform_destroy(instance)
        return success_response(message="Deleted successfully.")

    def perform_destroy(self, instance):
        instance.delete()


# ---------------------------------------------------------------------------
# Convenience combos — mirrors the common DRF generic view combinations
# ---------------------------------------------------------------------------

class XVSModelViewSetMixin(
    RetrieveModelMixin,
    CreateModelMixin,
    UpdateModelMixin,
    DestroyModelMixin,
):
    """
    Mixin for ModelViewSet — covers retrieve, create, update, destroy.
    List is handled by XVSPagination.get_paginated_response() automatically.

    Usage:
        class MyViewSet(XVSModelViewSetMixin, viewsets.ModelViewSet):
            ...
    """
    pass
