from __future__ import annotations

# Compatibility facade for Jane configuration.
#
# New settings should live in the focused modules under `settings/`. Most of
# the codebase still imports `config`, so this module intentionally re-exports
# the section modules to keep that API stable while the config surface shrinks.

from settings.env import _envFlag, _envInt, _envText
from settings.core import *
from settings.staff import *
from settings.operations import *
from settings.community import *
