"""Requester facts a school's staff record adds to Dynamic Role conditions.

The workflow engine may not import this app, so the facts only a school knows
about the person who raised a document - their job title and the kind of
contract they are on - reach it the way this app's documents do: the app
registers them at startup, through the engine's autodiscovery of
``workflow_conditions`` modules.

Both are a school's facts, and they say so: a staff record of this kind exists
only inside a school, so a platform operator has neither, and a rule testing one
on that side could not merely be false for one person - it could never be true
for anybody. They are declared for a SCHOOL audience so a picker on the platform
does not offer a question with no answer behind it.
"""
from vs_workflow.conditions.context import register_requester_facts
from vs_workflow.conditions.fields import ConditionField, register_requester_field
from vs_workflow.constants import ConditionFieldType, DocumentAudience

from .constants import EmploymentType

register_requester_field(ConditionField(
    "requester.job_title", "Their job title", "requester", ConditionFieldType.TEXT,
    audience=DocumentAudience.SCHOOL))
register_requester_field(ConditionField(
    "requester.employment_type", "Their contract", "requester",
    ConditionFieldType.CHOICE, tuple(EmploymentType.choices),
    audience=DocumentAudience.SCHOOL))


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
