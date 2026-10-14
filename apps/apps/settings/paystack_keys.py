"""Which kind of Paystack key each kind of server may hold.

A Paystack account has two sets of keys. Test keys (``sk_test_``, ``pk_test_``)
run against a sandbox where no money moves. Live keys (``sk_live_``,
``pk_live_``) charge real cards and pay real bank accounts. The prefix is the
only thing that tells them apart, and a server holding the wrong kind fails
quietly: a staging box with a live key charges a tester's real card, a
production box with a test key accepts "payments" that never settle.

So every deployed server states which kind it takes, in ``PAYSTACK_KEY_MODE``
(``live`` or ``test``), and refuses to start when its keys disagree. A
developer's machine and the test suites take no live key at all unless
``PAYSTACK_ALLOW_LIVE_KEY_LOCALLY`` says so deliberately.

This module imports nothing from Django so any settings module can use it. Each
function answers with a plain-words problem, or ``None`` when the keys are
acceptable, and the settings module turns a problem into the refusal to start.
No message carries any part of a key beyond its ``sk_live_``/``sk_test_``
prefix: start-up errors end up in logs and in error reports.
"""

LIVE = "live"
TEST = "test"
KEY_MODES = (LIVE, TEST)

#: The two kinds of key a secret or public key can be, by prefix.
_SECRET_PREFIXES = {LIVE: "sk_live_", TEST: "sk_test_"}
_PUBLIC_PREFIXES = {LIVE: "pk_live_", TEST: "pk_test_"}


def _kind_of(key: str, prefixes: dict) -> str | None:
    """``live`` or ``test`` by prefix, or ``None`` for anything else."""
    for kind, prefix in prefixes.items():
        if key.startswith(prefix):
            return kind
    return None


def deployed_key_problem(mode, secret_key, public_key) -> str | None:
    """What is wrong with the keys a deployed server holds, or ``None``.

    ``mode`` is the server's ``PAYSTACK_KEY_MODE`` as read from its
    environment: it must be exactly ``live`` or ``test``, with no default,
    because a default is a silent guess about which kind of server this is.
    Either key may be empty, which means payments are not configured and is
    always allowed; a key that is set must carry the prefix of ``mode``.
    """
    mode = (mode or "").strip()
    if mode not in KEY_MODES:
        return (
            "PAYSTACK_KEY_MODE must be set to live or test on a deployed "
            "service. Production takes live Paystack keys and staging takes "
            "test keys, and the server will not guess which it is."
        )

    other = TEST if mode == LIVE else LIVE
    for name, key, prefixes in (
        ("PAYSTACK_SECRET_KEY", secret_key, _SECRET_PREFIXES),
        ("PAYSTACK_PUBLIC_KEY", public_key, _PUBLIC_PREFIXES),
    ):
        key = (key or "").strip()
        if not key:
            continue
        kind = _kind_of(key, prefixes)
        if kind == mode:
            continue
        if kind == other:
            return (
                f"This server takes {mode} Paystack keys only "
                f"(PAYSTACK_KEY_MODE={mode}), but {name} is a {other} key "
                f"(it starts with {prefixes[other]})."
            )
        return (
            f"This server takes {mode} Paystack keys only "
            f"(PAYSTACK_KEY_MODE={mode}), but {name} is not a Paystack key: "
            f"it should start with {prefixes[mode]}."
        )
    return None


def local_key_problem(secret_key, allow_live: bool = False) -> str | None:
    """What is wrong with the secret key on a laptop or a test run, or ``None``.

    A live secret key is refused outright, because development data and test
    fixtures must never be able to charge a real card or pay a real account.
    ``allow_live`` is the deliberate override, read from
    ``PAYSTACK_ALLOW_LIVE_KEY_LOCALLY``.
    """
    key = (secret_key or "").strip()
    if allow_live or not key.startswith(_SECRET_PREFIXES[LIVE]):
        return None
    return (
        "PAYSTACK_SECRET_KEY is a live key (it starts with sk_live_), and "
        "development and test settings never take one: a laptop must never "
        "move real money by accident. Use a sk_test_ key, or set "
        "PAYSTACK_ALLOW_LIVE_KEY_LOCALLY=true if a live key is really meant."
    )
