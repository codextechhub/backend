# vs_config/models.py
#
# Four models covering the full System Configuration & Feature Flags module:
#
#   ConfigurationKey       — global platform-wide key/value settings
#   BranchFeatureFlag      — per-branch on/off module flags
#   BranchConfigOverride   — per-branch overrides of permitted keys
#   ConfigurationChangeLog — append-only audit history for all changes
#
# Scoping rules enforced here:
#   - ConfigurationKey has NO branch FK. It is platform-wide.
#   - BranchFeatureFlag is scoped to vs_schools.Branch. Querysets MUST always
#     be filtered by branch in views — never returned unscoped.
#   - BranchConfigOverride is scoped to vs_schools.Branch. Querysets MUST
#     always be filtered by branch in views — never returned unscoped.
#   - ConfigurationChangeLog is append-only. Views must never update or delete
#     its rows. All writes go through ConfigurationService or FlagService.

import uuid
from django.db import models
from django.conf import settings

from .constants import ChangeType
from vs_rbac.managers import TenantAwareManager


# ---------------------------------------------------------------------------
# 1. ConfigurationKey
#    Global platform-wide settings. No institution FK.
#    Only Vision Super Admins may create, update, or soft-delete these.
# ---------------------------------------------------------------------------
class ConfigurationKey(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    # Dot-notation key name. Immutable after creation.
    # Format enforced by ConfigKeyValidator. Example: auth.session_timeout_minutes
    key = models.CharField(
        max_length=200,
        unique=True,
        db_index=True,
    )
    value = models.TextField()
    description = models.TextField(
        help_text="Human-readable explanation of what this key controls. Required.",
    )
    # Soft-delete flag. Inactive keys are excluded from system lookups
    # but retained for audit history.
    is_active = models.BooleanField(default=True, db_index=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="config_keys_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["key"]
        verbose_name = "Configuration Key"
        verbose_name_plural = "Configuration Keys"

    def __str__(self):
        status = "active" if self.is_active else "archived"
        return f"{self.key} ({status})"


# ---------------------------------------------------------------------------
# 2. BranchFeatureFlag
#    Records the on/off state of a named flag for a specific branch.
#    Flags not explicitly set default to disabled (is_enabled=False).
#    Valid flag_key values are defined in constants.FLAG_REGISTRY — not here.
# ---------------------------------------------------------------------------
class BranchFeatureFlag(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    branch = models.ForeignKey(
        "vs_schools.Branch",
        on_delete=models.CASCADE,
        related_name="feature_flags",
    )
    # Must be a key present in constants.FLAG_REGISTRY.
    # Validated at service level — not a FK so new flags need no migration.
    flag_key = models.CharField(max_length=200, db_index=True)

    is_enabled = models.BooleanField(default=False)

    set_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="feature_flags_set",
    )
    # auto_now=True so every toggle automatically refreshes the timestamp.
    set_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [["branch", "flag_key"]]
        indexes = [
            models.Index(fields=["branch", "is_enabled"]),
            models.Index(fields=["branch", "flag_key"]),
        ]
        verbose_name = "Branch Feature Flag"
        verbose_name_plural = "Branch Feature Flags"

    def __str__(self):
        state = "ON" if self.is_enabled else "OFF"
        return f"{self.branch_id} | {self.flag_key} [{state}]"


# ---------------------------------------------------------------------------
# 3. BranchConfigOverride
#    Branch-specific overrides for a controlled subset of global keys.
#    Branch Admins may only write keys listed in PERMITTED_SELF_SERVICE_KEYS.
#    Resolution order: override → global key → caller default.
# ---------------------------------------------------------------------------
class BranchConfigOverride(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    branch = models.ForeignKey(
        "vs_schools.Branch",
        on_delete=models.CASCADE,
        related_name="config_overrides",
    )
    # Must be in constants.PERMITTED_SELF_SERVICE_KEYS. Validated at service level.
    key = models.CharField(max_length=200)
    value = models.TextField()

    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="config_overrides_updated",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [["branch", "key"]]
        indexes = [
            models.Index(fields=["branch", "key"]),
        ]
        verbose_name = "Branch Config Override"
        verbose_name_plural = "Branch Config Overrides"

    def __str__(self):
        return f"{self.branch_id} | {self.key} = {self.value[:40]}"


# ---------------------------------------------------------------------------
# 4. ConfigurationChangeLog
#    Append-only audit log for all configuration and flag changes.
#    Never updated or deleted — each row is one immutable change event.
#    All writes must go through ConfigurationService or FlagService,
#    never directly from views.
# ---------------------------------------------------------------------------
class ConfigurationChangeLog(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    change_type = models.CharField(
        max_length=20,
        choices=ChangeType.CHOICES,
        db_index=True,
    )
    # The config key name or flag_key that was changed.
    target_key = models.CharField(max_length=200, db_index=True)

    # Null for global config changes (no institution scope remains).
    institution = models.ForeignKey(
        "vs_schools.School",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="config_change_logs",
    )
    # Set for branch-scoped flag changes and branch config overrides; null for global changes.
    branch = models.ForeignKey(
        "vs_schools.Branch",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="config_change_logs",
    )
    # Empty string on new key creation (no prior value).
    previous_value = models.TextField(blank=True, default="")
    new_value = models.TextField()

    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="config_changes_made",
    )
    # auto_now_add makes this immutable — cannot be altered post-creation.
    changed_at = models.DateTimeField(auto_now_add=True, db_index=True)

    # Optional reason. Required when a Vision staff member disables a flag
    # on a Live institution (enforced at service level, not DB level).
    reason = models.TextField(blank=True, default="")

    objects = TenantAwareManager(tenant_field="institution", include_global=True)
    all_objects = models.Manager()

    class Meta:
        default_manager_name = "objects"
        base_manager_name = "all_objects"
        ordering = ["-changed_at"]
        indexes = [
            models.Index(fields=["institution", "change_type", "-changed_at"]),
            models.Index(fields=["target_key", "-changed_at"]),
            models.Index(fields=["changed_by", "-changed_at"]),
        ]
        verbose_name = "Configuration Change Log"
        verbose_name_plural = "Configuration Change Logs"

    def __str__(self):
        if self.branch_id:
            scope = f" | branch:{self.branch_id}"
        elif self.institution_id:
            scope = f" | inst:{self.institution_id}"
        else:
            scope = ""
        return f"[{self.change_type}] {self.target_key}{scope} @ {self.changed_at}"
