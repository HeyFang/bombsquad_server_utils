# Released under the MIT License. See LICENSE for details.
#
"""A command module handing user commands."""

from __future__ import annotations
from typing import override

import bascenev1 as bs

from bautils.chat import ServerCommand, register_command


# TODO: make it look more pretty, make characters icon appear in list
@register_command
class List(ServerCommand):
    """/l, /list or /clients"""

    aliases = ["l", "clients"]

    @override
    def on_command_call(self) -> None:

        # Build and broadcast a clean ASCII player list table.
        header = "{0:^4} | {1:<16} | {2:^8}"
        separator = "-" * 50

        lines = []
        lines.append(separator)
        lines.append(header.format("No.", "Name", "ClientID"))
        lines.append(separator)

        session = bs.get_foreground_host_session()
        assert session is not None

        for index, player in enumerate(session.sessionplayers, start=1):
            lines.append(
                header.format(
                    index,
                    player.getname(icon=True),
                    player.inputdevice.client_id,
                )
            )

        lines.append(separator)
        _list = "\n".join(lines)

        bs.broadcastmessage(_list, transient=True, clients=[self.client_id])

    @override
    def admin_authentication(self) -> bool:
        return False
