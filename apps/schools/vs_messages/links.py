"""Addresses of a conversation inside each app that opens it.

Staff read their conversations in the school app, parents in the parent app
under ``/parent/`` and pupils in the student app under ``/student/``, all on
the school's own host. Each is blank where the deployment configures no host,
so an email leaves its button out rather than carry an address that opens
nothing.
"""
from __future__ import annotations

from vs_tenants.app_urls import school_app_url


def conversation_url(tenant, surface: str, conversation_id: int) -> str:
    """The address of conversation *conversation_id* for *surface* (``staff``, ``parent`` or ``student``)."""
    path = f"messages/{conversation_id}"
    if surface == "parent":
        from schools.vs_notices.links import parent_app_url

        return parent_app_url(tenant, path)
    if surface == "student":
        from schools.vs_student_portal.links import student_app_url

        return student_app_url(tenant, path)
    host = school_app_url(tenant.slug)
    return f"{host}/{path}" if host else ""
