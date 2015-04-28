"""
Ludolph: Monitoring Jabber bot
Copyright (C) 2012-2015 Erigones, s. r. o.
This file is part of Ludolph.

See the file LICENSE for copying permission.
"""


class LudolphPlugin(object):
    """
    Ludolph plugin base class.
    """
    xmpp = None  # Reference to LudolphBot object
    config = None  # Plugin configuration as list of (name, value) tuples
    ver = None

    # noinspection PyUnusedLocal
    def __init__(self, xmpp, config, reinit=False, **kwargs):
        self.xmpp = xmpp
        self.config = dict(config)

    def get_version(self):
        if self.ver is not None:
            return '**%s**' % self.ver
        else:
            return '**Not implemented** by plugin author...'