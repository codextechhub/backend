"""Addresses inside the parent app, for links the parent portal hands out.

The parent app's address is defined once, in ``schools.vs_notices.links``, which
every link to the parent app goes through. This module is the portal's own name
for it.
"""
from __future__ import annotations

from schools.vs_notices.constants import PARENT_APP_PATH
from schools.vs_notices.links import parent_app_url

__all__ = ["PARENT_APP_PATH", "parent_app_url"]
