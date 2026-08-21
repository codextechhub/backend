"""Read shapes for the tenant's own structures.

Small on purpose. ``vs_tenants`` owns the models every other app scopes itself
by; it is not where those apps' screens are served from.
"""
from rest_framework import serializers

from .models import Branch


class BranchOptionSerializer(serializers.ModelSerializer):
    """A branch as something to pick, not a branch to administer.

    Deliberately narrower than ``vs_schools.BranchListSerializer``, which backs
    the School Management table and carries address, type and school slug. This
    one answers "which sites may I file this against?", so it carries the id the
    write path resolves, the name a human reads, and the two flags a picker
    sorts and labels by - nothing that would turn a payroll screen into a
    second, unaudited window onto a school's estate.
    """

    class Meta:
        model = Branch
        fields = ["id", "name", "code", "is_main", "status"]
        read_only_fields = fields
