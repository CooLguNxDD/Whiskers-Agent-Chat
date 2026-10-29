"""``cat-fleet-relay``: a hub-configured bridge between chat platforms and Cat Fleet Chat.

``core.RelayCore`` is platform-neutral: inbound routing, slash-style
commands, buttons, reply and route delivery. Each platform is an adapter
(``discord_adapter``, ``slack_adapter``, ``telegram_adapter``) that turns
native events into core calls and core output into native messages.
"""
