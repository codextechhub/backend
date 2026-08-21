"""Read and write shapes for the control room.

What is *not* here is the point. No user email, no permission key list, no raw
JSON metadata and no internal ids beyond the go-live request's own, which the
approve and reject URLs need. A task is addressed by its catalog key, so its
primary key never leaves the server.
"""
from __future__ import annotations

from rest_framework import serializers

from .models import GoLiveRequest, OnboardingTask


class OnboardingTaskSerializer(serializers.ModelSerializer):
    """One checklist row, exactly as FR-002 specifies it.

    The list is returned in ``order_index`` order, so the index itself is not
    exposed: the client renders the order it was given rather than sorting on a
    number that means nothing to it.
    """

    class Meta:
        model = OnboardingTask
        fields = ["key", "title", "is_required", "status", "completed_at"]
        read_only_fields = fields


class OnboardingTaskTransitionSerializer(serializers.Serializer):
    """The body of a task transition: a target status and nothing else."""

    status = serializers.ChoiceField(
        choices=[
            choice for choice in OnboardingTask._meta.get_field("status").choices
        ],
    )


class GoLiveRequestSerializer(serializers.ModelSerializer):
    """A go-live request as the school and the reviewer both see it.

    Names, not accounts: ``requested_by_name`` and ``reviewed_by_name`` are
    rendered from the related user so the payload carries no email address and
    no user id.
    """

    requested_by_name = serializers.SerializerMethodField()
    reviewed_by_name = serializers.SerializerMethodField()
    # Which school this request is for. A school reading its own list learns
    # nothing it did not already know, and the platform reviewer cannot work
    # without it: a queue of request ids with no school against them names
    # nobody to decide about. Still no account, no email, no internal tenant id.
    tenant_slug = serializers.CharField(source="tenant.slug", read_only=True)
    school_name = serializers.SerializerMethodField()

    class Meta:
        model = GoLiveRequest
        fields = [
            "id",
            "tenant_slug",
            "school_name",
            "status",
            "preferred_go_live_at",
            "note",
            "acknowledged",
            "requested_by_name",
            "reviewed_by_name",
            "reviewed_at",
            "rejection_reason",
            "failure_reference",
            "created_at",
        ]
        read_only_fields = fields

    def get_school_name(self, obj) -> str:
        """The school's name, or its tenant's when no profile exists yet.

        A tenant can exist before its school profile does, so this must not
        assume the relation is there - an AttributeError here would take out
        the whole queue rather than one row.
        """
        school = getattr(obj.tenant, "school_profile", None)
        return getattr(school, "name", None) or obj.tenant.name

    def _name(self, user):
        if user is None:
            return ""
        return (
            getattr(user, "full_name", None)
            or getattr(user, "get_full_name", lambda: "")()
            or ""
        )

    def get_requested_by_name(self, obj) -> str:
        return self._name(obj.requested_by)

    def get_reviewed_by_name(self, obj) -> str:
        return self._name(obj.reviewed_by)


class GoLiveSubmitSerializer(serializers.Serializer):
    """The go-live request body.

    ``acknowledged`` is deliberately optional here and defaults to False, so a
    missing acknowledgement and a false one take the same route: the service
    refuses both with 422 ACKNOWLEDGEMENT_REQUIRED. Making it a required field
    would answer a bare 400 for the absent case and a domain code for the false
    one, which is two different errors for one mistake.
    """

    preferred_go_live_at = serializers.DateTimeField()
    note = serializers.CharField(required=False, allow_blank=True, default="")
    acknowledged = serializers.BooleanField(required=False, default=False)


class GoLiveRejectSerializer(serializers.Serializer):
    """The rejection body. Emptiness is the service's refusal to make, not
    DRF's, so the school is told REJECTION_REASON_REQUIRED rather than that a
    field failed validation."""

    rejection_reason = serializers.CharField(
        required=False, allow_blank=True, default="",
    )
