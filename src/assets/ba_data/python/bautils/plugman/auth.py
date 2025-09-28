# Released under the MIT License. See LICENSE for details.

"""Plugin template."""

# ba_meta require api 9

from __future__ import annotations
from typing import TYPE_CHECKING, override

import babase

# import bascenev1 as bs

if TYPE_CHECKING:
    from typing import Any


# ba_meta export babase.Plugin
class MyPlugin(babase.Plugin):
    """Plugin template."""

    def __init__(self) -> None:
        super().__init__()
        print("Auth plugin initialized!")

    # code thingy

    @override
    def on_app_running(self) -> None:
        """Called when app is ready and game is running."""
        print("Auth plugin ready - monitoring connections")
