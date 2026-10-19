"""The parent portal's notice shape: the pupil portal's, plus whom it is for."""
from __future__ import annotations

from rest_framework import serializers

from schools.vs_notices.serializers import PortalNoticeSerializer


class ParentNoticeSerializer(PortalNoticeSerializer):
    """A notice in a parent's feed, with the first names of their children it reaches.

    ``context["children_for"]`` maps a notice id to those first names, in the
    order the parent's children are listed.
    """

    children = serializers.SerializerMethodField()

    class Meta(PortalNoticeSerializer.Meta):
        fields = [*PortalNoticeSerializer.Meta.fields, "children"]
        read_only_fields = fields

    def get_children(self, obj):
        return self.context.get("children_for", {}).get(obj.pk, [])
