"""Field validators shared across apps.

``phone_validator`` is the one rule every phone number typed into an account
or an administrator record passes through: a user created or edited through
the console, and the primary administrator named for a school or a branch at
onboarding. A country code is allowed but never required, so a local number
such as ``08012345678`` is as good as ``+2348012345678``, and spaces, hyphens
and brackets may be used to group the digits. The console's forms apply the
same pattern, so a number the form accepts is one the API accepts.
"""
from django.core.validators import RegexValidator

PHONE_PATTERN = r'^\+?[0-9 ()\-]{7,22}$'

phone_validator = RegexValidator(PHONE_PATTERN, message='Enter a valid phone number.')
