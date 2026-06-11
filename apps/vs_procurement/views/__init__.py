"""vs_procurement views package (split from a single 1,800-line module, B25).

Import through ``vs_procurement.views`` exactly as before.
"""
from .base import *             # noqa: F401,F403
from .vendors import *          # noqa: F401,F403
from .contracts import *        # noqa: F401,F403
from .catalog import *          # noqa: F401,F403
from .requisitions import *     # noqa: F401,F403
from .orders import *           # noqa: F401,F403
from .receiving import *        # noqa: F401,F403
from .vendor_payments import *  # noqa: F401,F403
from .reports import *          # noqa: F401,F403
from .stock import *            # noqa: F401,F403
