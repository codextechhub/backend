"""The notice fields an administrator may restrict per role.

One resource, ``school.notices``. Every field is included and open by default.

The title, the body and the audience are what a poster writes, so each has a
Read and a Write switch, and the audience is open on create: whoever may post a
notice chooses who it is for, and the switch decides who may change that on a
draft afterwards. The audience travels as ``audience`` (the rows) and as
``audience_label`` (the same rows in words), so one switch governs both.

The status, the publication and withdrawal stamps and the read counts are
recorded by the system and are read-only. ``read_count`` and
``audience_count`` are one switch: together they say how many pupils opened the
notice of how many it reached.

A pupil reading a notice in the portal holds no role, so these switches do not
reach the portal: a notice is the school speaking to the pupils it addresses,
and the portal shows each of them the notice they were sent.
"""
from vs_rbac.field_registry import FieldSpec, register_fields

_TENANT = "TENANT"


def register():
    """Publish the notice field declarations to the Field Access registry."""
    register_fields(
        "school",
        "notices",
        surfaces=(
            "schools.vs_notices.serializers.NoticeSerializer",
            "schools.vs_notices.serializers.NoticeWriteSerializer",
        ),
        fields=(
            FieldSpec("title", "Title", group="Notice", scope=_TENANT,
                      sort_order=10, open_on_create=True),
            FieldSpec("body", "Message", group="Notice", scope=_TENANT,
                      sort_order=20, open_on_create=True),
            FieldSpec("audience", "Audience", group="Notice", scope=_TENANT,
                      sort_order=30, open_on_create=True,
                      api_names=("audience", "audience_label"),
                      description="Who the notice is for: the whole school, "
                                  "branches, levels or classes."),
            FieldSpec("status", "Status", group="Publication", scope=_TENANT,
                      writable=False, sort_order=10,
                      api_names=("status", "status_label")),
            FieldSpec("publication", "Published and withdrawn by", group="Publication",
                      scope=_TENANT, writable=False, sort_order=20,
                      api_names=("published_at", "published_by", "edited_at",
                                 "withdrawn_at", "withdrawn_by"),
                      description="When the notice was published, corrected "
                                  "and withdrawn, and by whom."),
            FieldSpec("reads", "Read count", group="Publication", scope=_TENANT,
                      writable=False, sort_order=30,
                      api_names=("read_count", "audience_count"),
                      description="How many pupils opened the notice, of how "
                                  "many it reached."),
        ),
    )
