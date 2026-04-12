from __future__ import annotations

import random
import uuid
from dataclasses import dataclass
from typing import List, Optional, Tuple

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from vs_schools.models import (
    RESERVED_TENANT_SLUGS,
    AuditEvent,
    ContactInfo,
    School,
    SchoolBranding,
    SchoolLifecycleEvent,
    SchoolModuleSetting,
    SchoolOperationEvent,
    SchoolPrimaryAdmin,
    SchoolStatus,
    InviteStatus,
    OperationOutcome,
    OperationType,
    ProvisioningRecord,
    ProvisioningStatus,
)

# -----------------------------------------------------------------------------
# Random data pools
# -----------------------------------------------------------------------------

WORDS_A = ["nova", "bright", "green", "royal", "prime", "swift", "crown", "unity", "pillar", "horizon", "atlas", "zenith"]
WORDS_B = ["academy", "college", "institute", "school", "university", "polytechnic", "foundation", "campus", "group"]

COUNTRIES = ["Nigeria", "Ghana", "Kenya", "South Africa", "Egypt"]
REGIONS = ["Lagos", "Abuja", "Accra", "Nairobi", "Cape Town", "Cairo"]
TIMEZONES = ["Africa/Lagos", "Africa/Accra", "Africa/Nairobi", "Africa/Johannesburg", "Africa/Cairo"]
CURRENCIES = ["NGN", "GHS", "KES", "ZAR", "EGP"]
PLAN_TIERS = ["Starter", "Pro", "Enterprise"]
CATEGORIES = ["School", "College", "Organization"]
SCHOOL_TYPES = ["Public", "Private"]

MODULE_KEYS = [
    "STUDENTS",
    "STAFF",
    "ATTENDANCE",
    "FINANCE",
    "PROCUREMENT",
    "ANALYTICS",
    "TIMETABLE",
    "EXAMS",
]


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def _r_school_name() -> str:
    return f"{random.choice(WORDS_A).title()} {random.choice(WORDS_B).title()}"


def _r_group() -> str:
    # sometimes empty
    return random.choice(["", "Codex Group", "Vision Network", "EduSphere Holdings", ""])


def _slugify_basic(text: str) -> str:
    text = (text or "").strip().lower()
    text = text.replace("&", "and")
    # keep alnum and spaces only
    cleaned = "".join(ch if (ch.isalnum() or ch == " ") else " " for ch in text)
    parts = [p for p in cleaned.split() if p]
    slug = "-".join(parts)[:60] if parts else ""
    return slug


def _unique_school_slug(base: str) -> str:
    base = _slugify_basic(base)
    if not base:
        base = f"school-{random.randint(1000, 9999)}"

    if base in RESERVED_TENANT_SLUGS:
        base = f"{base}-inst"

    slug = base
    i = 2
    while School.objects.filter(school_slug=slug).exists() or slug in RESERVED_TENANT_SLUGS:
        slug = f"{base}-{i}"
        i += 1
    return slug


def _email_for(name: str) -> str:
    token = "".join(ch for ch in name.lower() if ch.isalnum() or ch == " ").strip().replace(" ", ".")
    domain = random.choice(["vision-demo.com", "codex.local", "school.test"])
    return f"admin.{token}.{random.randint(10, 999)}@{domain}"


def _phone() -> str:
    return f"+234{random.randint(7000000000, 9999999999)}"


def _short_hash() -> str:
    return uuid.uuid4().hex[:16]


def _pick_final_status() -> str:
    """
    Choose a realistic final state distribution:
    - most end up Ready or Live
    - some are Suspended
    - rare: Locked, Soft Deleted
    """
    roll = random.random()
    if roll < 0.55:
        return SchoolStatus.LIVE
    if roll < 0.85:
        return SchoolStatus.READY
    if roll < 0.93:
        return SchoolStatus.SUSPENDED
    if roll < 0.98:
        return SchoolStatus.LOCKED
    return SchoolStatus.DELETED_SOFT


def _lifecycle_path_to(final_status: str) -> List[str]:
    """
    A plausible lifecycle path.
    We keep it forward-moving for demo realism.
    """
    base = [
        SchoolStatus.CREATED,
        SchoolStatus.CONFIGURING,
        SchoolStatus.DATA_IMPORTING,
        SchoolStatus.READY,
    ]
    if final_status == SchoolStatus.READY:
        return base
    if final_status == SchoolStatus.LIVE:
        return base + [SchoolStatus.LIVE]
    if final_status == SchoolStatus.SUSPENDED:
        return base + [SchoolStatus.LIVE, SchoolStatus.SUSPENDED]
    if final_status == SchoolStatus.LOCKED:
        # Locked can happen during configuring or importing
        lock_point = random.choice([SchoolStatus.CONFIGURING, SchoolStatus.DATA_IMPORTING])
        return [SchoolStatus.CREATED, lock_point, SchoolStatus.LOCKED]
    if final_status == SchoolStatus.DELETED_SOFT:
        return base + [SchoolStatus.LIVE, SchoolStatus.DELETED_SOFT]
    return base


@dataclass
class SeedStats:
    schools: int = 0
    branding: int = 0
    module_settings: int = 0
    lifecycle_events: int = 0
    provisioning_records: int = 0
    contacts: int = 0
    primary_admin_links: int = 0
    operation_events: int = 0
    audit_events: int = 0


class Command(BaseCommand):
    help = "Seed Module 1 (School & Tenant Management) demo data."

    def add_arguments(self, parser):
        parser.add_argument("--count", type=int, default=5, help="Number of schools to create (default: 5).")
        parser.add_argument(
            "--modules-per-school",
            type=int,
            default=5,
            help="Module settings per school (default: 5).",
        )
        parser.add_argument(
            "--actor-id",
            type=str,
            default="seed-script",
            help="Actor id used for lifecycle/audit logs (default: seed-script).",
        )

    def handle(self, *args, **kwargs):
        count: int = kwargs["count"]
        modules_per_school: int = kwargs["modules_per_school"]
        actor_id: str = kwargs["actor_id"]

        if count < 1:
            self.stdout.write(self.style.WARNING("Nothing to do: --count must be >= 1."))
            return

        stats = SeedStats()

        with transaction.atomic():
            for i in range(count):
                school = self._create_school(actor_id=actor_id)
                stats.schools += 1

                self._ensure_branding(school, stats)
                self._seed_module_settings(school, modules_per_school, actor_id, stats)
                self._seed_primary_admin(school, actor_id, stats)
                self._seed_provisioning_record(school, stats)
                self._seed_lifecycle_events_and_state(school, actor_id, stats)
                self._seed_ops_and_audit(school, actor_id, stats)

                self.stdout.write(self.style.SUCCESS(
                    f"[{i+1}/{count}] Created school '{school.school_name}' ({school.school_slug}) status={school.status}"
                ))

        self.stdout.write(self.style.SUCCESS("\nSeed complete."))
        self.stdout.write(
            self.style.SUCCESS(
                f"School={stats.schools} | Branding={stats.branding} | ModuleSettings={stats.module_settings} | "
                f"LifecycleEvents={stats.lifecycle_events} | Provisioning={stats.provisioning_records} | "
                f"Contacts={stats.contacts} | PrimaryAdmins={stats.primary_admin_links} | "
                f"OpEvents={stats.operation_events} | AuditEvents={stats.audit_events}"
            )
        )

    # -----------------------------------------------------------------------------
    # Create core school
    # -----------------------------------------------------------------------------

    def _create_school(self, *, actor_id: str) -> School:
        name = _r_school_name()
        slug = _unique_school_slug(name)

        country = random.choice(COUNTRIES)
        region = random.choice(REGIONS)
        tz = random.choice(TIMEZONES)
        currency = random.choice(CURRENCIES)

        inst = School.objects.create(
            school_name=name,
            school_slug=slug,
            school_group=_r_group(),
            category=random.choice(CATEGORIES),
            school_type=random.choice(SCHOOL_TYPES),
            plan_tier=random.choice(PLAN_TIERS),
            country=country,
            region=region,
            timezone=tz,
            currency=currency,
            primary_contact_name=f"{random.choice(['Ife', 'Ada', 'Tunde', 'Kwame', 'Amina'])} {random.choice(['Okafor', 'Mensah', 'Adeyemi', 'Kamau', 'Hassan'])}",
            primary_contact_email=_email_for(name),
            primary_contact_phone=_phone(),
            status=SchoolStatus.CREATED,
        )

        # Optional: run model validation
        # inst.full_clean()

        # Audit: school created
        AuditEvent.objects.create(
            school=inst,
            actor_id=actor_id,
            action="SCHOOL_CREATE",
            resource_type="School",
            resource_id=str(inst.id),
            before_hash="",
            after_hash=_short_hash(),
            outcome=OperationOutcome.SUCCEEDED,
        )
        return inst

    # -----------------------------------------------------------------------------
    # Branding
    # -----------------------------------------------------------------------------

    def _ensure_branding(self, inst: School, stats: SeedStats) -> None:
        # Your branding now has only `logo` (ImageField). We keep it null for seeding.
        branding, created = SchoolBranding.objects.get_or_create(school=inst)
        if created:
            stats.branding += 1

    # -----------------------------------------------------------------------------
    # Module Settings
    # -----------------------------------------------------------------------------

    def _seed_module_settings(self, inst: School, per_inst: int, actor_id: str, stats: SeedStats) -> None:
        keys = random.sample(MODULE_KEYS, k=min(per_inst, len(MODULE_KEYS)))
        now = timezone.now()

        for k in keys:
            obj, created = SchoolModuleSetting.objects.get_or_create(
                school=inst,
                module_key=k,
                defaults={
                    "enabled": random.choice([True, False, True]),  # bias slightly toward enabled
                    "effective_from": now - timezone.timedelta(days=random.randint(0, 30)),
                    "changed_by_actor_id": actor_id,
                },
            )
            if created:
                stats.module_settings += 1

        # Audit backstop
        AuditEvent.objects.create(
            school=inst,
            actor_id=actor_id,
            action="SCHOOL_MODULE_SETTINGS_SEEDED",
            resource_type="School",
            resource_id=str(inst.id),
            before_hash="",
            after_hash=_short_hash(),
            outcome=OperationOutcome.SUCCEEDED,
        )
        stats.audit_events += 1

    # -----------------------------------------------------------------------------
    # Primary Admin + Contact
    # -----------------------------------------------------------------------------

    def _seed_primary_admin(self, inst: School, actor_id: str, stats: SeedStats) -> None:
        # Create contact
        contact = ContactInfo.objects.create(
            full_name=f"{random.choice(['Mary', 'John', 'Seyi', 'Fatima', 'Chidi'])} {random.choice(['Okoro', 'Diallo', 'Boateng', 'Njoroge', 'El-Sayed'])}",
            email=_email_for(inst.school_name),
            phone=_phone(),
        )
        stats.contacts += 1

        # Link as primary admin
        link, created = SchoolPrimaryAdmin.objects.get_or_create(
            school=inst,
            defaults={
                "contact": contact,
                "role_label": random.choice(["School Admin", "Head Admin", "Registrar", "Operations Lead"]),
                "invite_status": InviteStatus.QUEUED,
                "invite_queued_at": timezone.now(),
            },
        )
        if not created:
            # If it already exists, ensure contact is not orphaned (cleanup)
            contact.delete()
        else:
            stats.primary_admin_links += 1

        AuditEvent.objects.create(
            school=inst,
            actor_id=actor_id,
            action="SCHOOL_PRIMARY_ADMIN_ASSIGNED",
            resource_type="SchoolPrimaryAdmin",
            resource_id=str(link.id),
            before_hash="",
            after_hash=_short_hash(),
            outcome=OperationOutcome.SUCCEEDED,
        )
        stats.audit_events += 1

    # -----------------------------------------------------------------------------
    # Provisioning Record
    # -----------------------------------------------------------------------------

    def _seed_provisioning_record(self, inst: School, stats: SeedStats) -> None:
        # Make provisioning roughly consistent with likely lifecycle outcomes
        # Live/Ready -> Succeeded, Locked -> Failed, Suspended -> Succeeded
        if inst.status in (SchoolStatus.CREATED, SchoolStatus.CONFIGURING, SchoolStatus.DATA_IMPORTING):
            prov_status = random.choice([ProvisioningStatus.QUEUED, ProvisioningStatus.RUNNING])
        else:
            prov_status = ProvisioningStatus.SUCCEEDED

        # we'll adjust later after lifecycle sets final status; create now as queued
        prov, created = ProvisioningRecord.objects.get_or_create(
            school=inst,
            defaults={"provisioning_status": ProvisioningStatus.QUEUED},
        )
        if created:
            stats.provisioning_records += 1

    # -----------------------------------------------------------------------------
    # Lifecycle events + final status (uses your School.transition helpers)
    # -----------------------------------------------------------------------------

    def _seed_lifecycle_events_and_state(self, inst: School, actor_id: str, stats: SeedStats) -> None:
        final_status = _pick_final_status()
        path = _lifecycle_path_to(final_status)

        # Start from current status (should be CREATED)
        # We'll use transition() so it generates SchoolLifecycleEvent rows itself.
        # Note: transition() blocks illegal jumps (good).
        current = inst.status
        for state in path[1:]:
            # If soft delete is in path, call soft_delete to properly set deleted_at
            if state == SchoolStatus.DELETED_SOFT:
                inst.soft_delete(actor_id=actor_id, reason="Seeded soft-delete for demo")
            else:
                inst.transition(to_state=state, actor_id=actor_id, reason="Seeded lifecycle transition")
            current = state

        # Count lifecycle events created (approx): query per school, since transition() creates them internally
        created_events = SchoolLifecycleEvent.objects.filter(school=inst).count()
        stats.lifecycle_events += created_events

        # Align provisioning status to final outcome
        prov = inst.provisioning
        if final_status == SchoolStatus.LOCKED:
            prov.provisioning_status = ProvisioningStatus.FAILED
            prov.last_error_code = "PROVISIONING_FAILED"
            prov.last_error_message = "Seeded failure: simulated provisioning error."
            prov.completed_at = timezone.now()
        elif final_status in (SchoolStatus.READY, SchoolStatus.LIVE, SchoolStatus.SUSPENDED, SchoolStatus.DELETED_SOFT):
            prov.provisioning_status = ProvisioningStatus.SUCCEEDED
            prov.completed_at = timezone.now()
        else:
            # Created/Configuring/Importing should be queued/running
            prov.provisioning_status = random.choice([ProvisioningStatus.QUEUED, ProvisioningStatus.RUNNING])

        prov.save()

    # -----------------------------------------------------------------------------
    # Operation Events + Audit
    # -----------------------------------------------------------------------------

    def _seed_ops_and_audit(self, inst: School, actor_id: str, stats: SeedStats) -> None:
        # Create operation events only for states that imply an operation
        if inst.status == SchoolStatus.SUSPENDED:
            SchoolOperationEvent.objects.create(
                school=inst,
                operation_type=OperationType.SUSPEND,
                actor_id=actor_id,
                reason="Seeded suspension for demo",
                outcome=OperationOutcome.SUCCEEDED,
            )
            stats.operation_events += 1

            AuditEvent.objects.create(
                school=inst,
                actor_id=actor_id,
                action="SCHOOL_SUSPEND",
                resource_type="School",
                resource_id=str(inst.id),
                before_hash=_short_hash(),
                after_hash=_short_hash(),
                outcome=OperationOutcome.SUCCEEDED,
            )
            stats.audit_events += 1

        if inst.status == SchoolStatus.DELETED_SOFT:
            SchoolOperationEvent.objects.create(
                school=inst,
                operation_type=OperationType.SOFT_DELETE,
                actor_id=actor_id,
                reason="Seeded soft-delete for demo",
                confirmation_token="seed-confirm",
                outcome=OperationOutcome.SUCCEEDED,
            )
            stats.operation_events += 1

            AuditEvent.objects.create(
                school=inst,
                actor_id=actor_id,
                action="SCHOOL_SOFT_DELETE",
                resource_type="School",
                resource_id=str(inst.id),
                before_hash=_short_hash(),
                after_hash=_short_hash(),
                outcome=OperationOutcome.SUCCEEDED,
            )
            stats.audit_events += 1

        if inst.status == SchoolStatus.LOCKED:
            SchoolOperationEvent.objects.create(
                school=inst,
                operation_type=OperationType.RESET,
                actor_id=actor_id,
                reason="Seeded lock scenario for demo; reset recommended",
                outcome=OperationOutcome.FAILED,
                error_code="LOCKED_SCHOOL",
                error_message="Seeded locked school; manual intervention required.",
            )
            stats.operation_events += 1

            AuditEvent.objects.create(
                school=inst,
                actor_id=actor_id,
                action="SCHOOL_LOCKED",
                resource_type="School",
                resource_id=str(inst.id),
                before_hash=_short_hash(),
                after_hash=_short_hash(),
                outcome=OperationOutcome.FAILED,
            )
            stats.audit_events += 1
