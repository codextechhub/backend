"""Requester facts a school's staff record adds to Dynamic Role conditions.

The workflow engine may not import this app, so the facts only a school knows
about the person who raised a document - their job title and the kind of
contract they are on - reach it the way this app's documents do: the app
registers them at startup, through the engine's autodiscovery of
``workflow_conditions`` modules.

A requester with no staff record here, such as a CodeX operator, has neither
fact, so a condition on them is simply not true for that person.
"""
from vs_workflow.conditions.context import register_requester_facts
from vs_workflow.conditions.fields import ConditionField, register_requester_field
from vs_workflow.constants import ConditionFieldType

from .constants import EmploymentType

register_requester_field(ConditionField(
    "requester.job_title", "Their job title", "requester", ConditionFieldType.TEXT))
register_requester_field(ConditionField(
    "requester.employment_type", "Their contract", "requester",
    ConditionFieldType.CHOICE, tuple(EmploymentType.choices)))


@register_requester_facts
def staff_record_facts(user, tenant) -> dict:
    """Job title and contract type from the requester's staff record in *tenant*."""
    profile = getattr(user, "staff_profile", None)
    if profile is None:
        return {}
    owner = getattr(profile, "tenant_id", None)
    if owner is not None and owner != getattr(tenant, "pk", tenant):
        return {}
    return {
        "job_title": profile.job_title or None,
        "employment_type": profile.employment_type or None,
    }
