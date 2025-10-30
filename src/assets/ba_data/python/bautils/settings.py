# Released under the MIT License. See LICENSE for details.
#
"""Settings for server utils provided by this project."""

# ba_meta require api 9

# ---- DISCORD SETTINGS ----
# Still in development and may have bugs, avoid using for now if not necessary... ill update it soon :>
enableDiscordAdminAuth: bool = False
enableDiscordIntegration: bool = False

# ---- IP AUTH SETTINGS ----
enableIpAuth: bool = False
authorizedAdminIPs: list[str] = [
    "17126921592780111236",  # Fang
    "5738055069092261189",   # another admin
]
