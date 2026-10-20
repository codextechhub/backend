"""A parent's own profile: what the school manages, and the contact details they edit.

**A parent holds no role, so Field Access switches do not apply.** The profile
is a fixed, narrow shape written for the signed-in parent: their name, the
address they sign in with and their children on the roll are the school's to
manage and are shown read-only; their phone numbers, address and photograph
are theirs to change. The photograph is the guardian's own (``Guardian.photo``),
the one the school sees on the guardian record, set and removed through the
same Student Management services and audited with the parent as the actor.

The guardian record belongs to Student Management and is written only through
``update_guardian``, which writes the fields that changed and audits them with
the parent as the actor. A contact email change is not offered here because the
guardian's email is the address their account signs in with: changing it
re-resolves the account the guardian is linked to, which would cut the parent
off. The sign-in address is shown as it is on the account.

Anything outside ``phone``, ``alt_phone`` and ``address`` is refused by name
rather than ignored, so a client that sends a field the parent may not change
learns that at once.
"""
from __future__ import annotations

import re

from schools.vs_student_portal.exceptions import ProfileFieldInvalid
from schools.vs_student_portal.services import _PHONE_RE

EDITABLE_FIELDS = ("phone", "alt_phone", "address")
ADDRESS_MAX_LENGTH = 500
NOT_CHANGEABLE = "This cannot be changed here. Ask the school."


def profile(guardian, children, *, user, request) -> dict:
    """The parent's profile as it now stands."""
    from schools.vs_students.serializers import guardian_photo_url
    from schools.vs_students.services.scoping import branch_dimension_applies

    from .services import _child_summary, child_enrolments

    show_branch = branch_dimension_applies(guardian.tenant)
    placed = child_enrolments(children)
    return {
        "photo_url": guardian_photo_url(guardian, request=request) or "",
        "managed_by_school": {
            "full_name": guardian.full_name,
            "sign_in_email": user.email or "",
            "children": [
                _child_summary(c, placed.get(c.pk), request=request, show_branch=show_branch)
                for c in children
            ],
        },
        "editable": {
            "phone": guardian.phone or "",
            "alt_phone": guardian.alt_phone or "",
            "address": guardian.address or "",
        },
        "email_editable": False,
    }


def _clean_phone(value, *, field: str, required: bool) -> str:
    """A phone number of 7 to 15 digits as the pupil's profile accepts one."""
    text = str(value or "").strip()
    if not text:
        if required:
            raise ProfileFieldInvalid("Enter a phone number.", field=field)
        return ""
    digits = re.sub(r"\D", "", text)
    if not _PHONE_RE.match(text) or not 7 <= len(digits) <= 15:
        raise ProfileFieldInvalid(
            "Enter a phone number of 7 to 15 digits, such as 0803 456 7890.", field=field,
        )
    return text


def _clean_address(value) -> str:
    text = str(value or "").strip()
    if len(text) > ADDRESS_MAX_LENGTH:
        raise ProfileFieldInvalid(
            f"Keep the address to {ADDRESS_MAX_LENGTH} characters or fewer.", field="address",
        )
    return text


def update_contact(guardian, data, *, actor):
    """Change the parent's phone, second phone or address; returns the guardian.

    Every key in *data* is checked before anything is written, so a refused
    request changes nothing. A key that is not one of ``EDITABLE_FIELDS`` is
    refused on its own name.
    """
    from schools.vs_students.services.guardians import update_guardian

    if not hasattr(data, "keys"):
        raise ProfileFieldInvalid("Send the details to change.", field="")
    for key in data.keys():
        if key not in EDITABLE_FIELDS:
            raise ProfileFieldInvalid(NOT_CHANGEABLE, field=key)

    fields = {}
    if "phone" in data:
        fields["phone"] = _clean_phone(data["phone"], field="phone", required=True)
    if "alt_phone" in data:
        fields["alt_phone"] = _clean_phone(data["alt_phone"], field="alt_phone", required=False)
    if "address" in data:
        fields["address"] = _clean_address(data["address"])
    guardian, _ = update_guardian(guardian, actor=actor, **fields)
    return guardian
