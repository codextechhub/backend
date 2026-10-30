"""Erasing a sign-in account, and CodeX's own staff, once they are long gone.

One account can stand behind several people records: a teacher who is also a
parent at the same school has a staff record and a guardian record on one
sign-in. The account is erased only when every record standing behind it has
been, so the teacher who left keeps signing in as a parent. Each product
registers the models that hold an account (:func:`register_account_holder`);
an account none of them names (a school administrator with no staff record)
is never erased here.

Erasing an account closes it, then blanks its phone, sign-in identifiers and
gender and gives it an address that delivers nowhere; it is closed exactly as "Delete my account" does (:func:`close_account`).
Names stay unless the caller replaces them (a parent's are).

CodeX staff (:class:`PlatformStaffProfile`) are erased 6 years after the date
they exited: their personal details, next of kin and bank details go; their
name, staff number and job stay.
"""
from __future__ import annotations

from django.db.models.functions import Coalesce, TruncDate
from django.utils import timezone

from core.erasure import erase_each, scrub_history

#: Models whose un-erased row keeps an account alive: (model getter, field).
_HOLDERS: list = []


def register_account_holder(get_model, field: str = "user") -> None:
    """Name a model whose rows hold an account until their ``erased_at`` is set."""
    if (get_model, field) not in _HOLDERS:
        _HOLDERS.append((get_model, field))


def still_held(user) -> bool:
    """Whether any un-erased person record still stands behind ``user``."""
    for get_model, field in _HOLDERS:
        model = get_model()
        if model._base_manager.filter(**{field: user, "erased_at__isnull": True}).exists():
            return True
    return False


def erase_account(user, *, names: tuple[str, str] | None = None) -> bool:
    """Blank and close ``user`` unless a record still holds it; answer whether it was."""
    from .models import User
    from .services.account_closure import close_account

    if user is None or still_held(user):
        return False
    if user.is_active or user.has_usable_password():
        close_account(user, actor=None, origin="RETENTION")
    # Some accounts must carry an address; ``.invalid`` is reserved and never
    # delivers, so the account stays valid and reaches nobody.
    blanks = {"email": f"erased-{user.pk}@erased.invalid", "phone": None, "gender": ""}
    if names:
        blanks.update(first_name=names[0], last_name=names[1])
    scrub_history(User, [user.pk], blanks)
    User.objects.filter(pk=user.pk).update(uid=None, card_login_id=None, **blanks)
    return True


#: What an erased CodeX staff member keeps blank.
PLATFORM_STAFF_BLANKS = {
    "date_of_birth": None, "marital_status": "", "nationality": "",
    "state_of_origin": "", "profile_photo": "", "bio": "", "personal_email": "",
    "alternate_phone": "", "residential_address": "", "city": "", "state": "",
    "nok_name": "", "nok_relationship": "", "nok_phone": "", "nok_address": "",
    "bank_name": "", "account_name": "", "account_number": "",
}


def erase_platform_leavers(cutoff, *, dry_run: bool = False) -> dict:
    from .models import PlatformStaffProfile

    due = PlatformStaffProfile._base_manager.annotate(
        left_on=Coalesce("date_exited", TruncDate("updated_at")),
    ).filter(
        employment_status=PlatformStaffProfile.EmploymentStatus.EXITED,
        erased_at__isnull=True, left_on__lt=cutoff.date(),
    )
    if dry_run:
        return {"codex_staff": due.count()}
    return {"codex_staff": erase_each(due, erase_platform_staff)}


def erase_platform_staff(profile) -> None:
    from .models import PlatformStaffProfile

    scrub_history(PlatformStaffProfile, [profile.pk], PLATFORM_STAFF_BLANKS)
    for field, value in PLATFORM_STAFF_BLANKS.items():
        setattr(profile, field, value)
    profile.erased_at = timezone.now()
    profile.save()
    erase_account(profile.user)

