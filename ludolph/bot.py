"""
Ludolph: Monitoring Jabber Bot
Copyright (C) 2012-2015 Erigones, s. r. o.
This file is part of Ludolph.

See the LICENSE file for copying permission.
"""

import ssl
import time
import copy
import logging
from datetime import datetime
from sleekxmpp import ClientXMPP
from sleekxmpp.xmlstream import ET
from sleekxmpp.exceptions import IqError

try:
    from collections import OrderedDict
except ImportError:
    # noinspection PyUnresolvedReferences,PyPackageRequirements
    from ordereddict import OrderedDict

from ludolph.message import IncomingLudolphMessage, OutgoingLudolphMessage
from ludolph.command import COMMANDS, USERS, ADMINS
from ludolph.db import LudolphDB, LudolphDBMixin
from ludolph.web import WebServer
from ludolph.cron import Cron

logger = logging.getLogger(__name__)

__all__ = ('LudolphBot',)

PLUGINS = OrderedDict()  # {modname : instance}


def get_xmpp():
    """Return LudolphBot instance"""
    return PLUGINS[__name__]


class LudolphBot(ClientXMPP, LudolphDBMixin):
    """
    Ludolph bot.
    """
    _start_time = None
    _muc_ready = False
    _reloaded = False
    reloading = False
    shutting_down = False
    commands = COMMANDS
    plugins = PLUGINS
    users = USERS
    admins = ADMINS
    room = None
    room_config = None
    room_invites = True
    muc = None
    nick = 'Ludolph'
    xmpp = None
    maxhistory = '4096'
    webserver = None
    cron = None

    # noinspection PyUnusedLocal
    def __init__(self, config, plugins=None, *args, **kwargs):
        self.room_users = set()
        self.room_admins = set()
        self.room_users_invited = set()
        self.room_users_last_seen = {}

        self._load_config(config, init=True)
        logger.info('Initializing *%s* jabber bot', self.nick)
        self._load_plugins(config, plugins, init=True)

        # Initialize the SleekXMPP client
        super(LudolphBot, self).__init__(config.get('xmpp', 'username'), config.get('xmpp', 'password'))

        # Auto-authorize is enabled by default. User subscriptions are
        # controlled by self._handle_new_subscription
        self.auto_authorize = True

        # Register XMPP plugins
        self.register_plugin('xep_0030')  # Service Discovery
        self.register_plugin('xep_0045')  # Multi-User Chat
        self.register_plugin('xep_0199')  # XMPP Ping
        self.register_plugin('xep_0084')  # User Avatar
        self.register_plugin('xep_0153')  # User Avatar vCard

        # Register event handlers
        self.add_event_handler('session_start', self.session_start, threaded=True)
        self.add_event_handler('message', self.message, threaded=True)

        if self.room:
            self.muc = self.plugin['xep_0045']
            self.add_event_handler('groupchat_message', self.muc_message, threaded=True)
            self.add_event_handler('muc::%s::got_online' % self.room, self.muc_online, threaded=True)
            self.add_event_handler('muc::%s::got_offline' % self.room, self.muc_offline, threaded=True)

        # Start the web server thread for processing HTTP requests
        if self.webserver:
            self._start_thread('webserver', self.webserver.start, track=False)

        # Start the scheduler thread for running periodic cron jobs
        if self.cron:
            self._start_thread('cron', self.cron.run, track=False)

        # Save start time
        self._start_time = time.time()

    def _db_set_items(self):
        """Save data to persistent DB"""
        logger.info('Syncing LudolphBot data with persistent DB file')
        self.db['room_users_invited'] = self.room_users_invited
        self.db['room_users_last_seen'] = self.room_users_last_seen

    def _db_load_items(self):
        """Load saved data from persistent DB"""
        logger.info('Loading LudolphBot data from persistent DB file')
        self.room_users_invited.update(self.db.get('room_users_invited', ()))
        self.room_users_last_seen.update(self.db.get('room_users_last_seen', {}))

    def _load_config(self, config, init=False):
        """
        Load bot settings from config object.
        The init parameter indicates whether this is a first-time initialization or a reload.
        """
        logger.info('Configuring jabber bot')

        # Get DB file
        if config.has_option('global', 'dbfile'):
            dbfile = config.get('global', 'dbfile')
            if dbfile:
                self.db_enable(LudolphDB(dbfile), init=True)

        # Get nick name
        if config.has_option('xmpp', 'nick'):
            nick = config.get('xmpp', 'nick').strip()
            if nick:
                self.nick = nick

        # If you are working with an OpenFire server, you will
        # need to use a different SSL version:
        if config.has_option('xmpp', 'sslv3') and config.getboolean('xmpp', 'sslv3'):
            self.ssl_version = ssl.PROTOCOL_SSLv3

        # Read comma-separated option and return a list of JIDs
        def read_jid_array(section, option, keywords=()):
            jids = set()

            if config.has_option(section, option):
                for jid in config.get(section, option).strip().split(','):
                    jid = jid.strip()

                    if not jid:
                        continue

                    if '@' in jid:
                        if jid.startswith('@'):
                            kwd = jid[1:]
                            if kwd in keywords:
                                jids.update(getattr(self, kwd))
                            else:
                                logger.warn('Skipping invalid keyword "%s" from setting "%s"', jid, option)
                        else:
                            jids.add(jid)
                    else:
                        logger.warn('Skipping invalid JID "%s" from setting "%s"', jid, option)

            return jids

        # Users
        self.users.clear()
        self.users.update(read_jid_array('xmpp', 'users'))
        logger.info('Current users: %s', ', '.join(self.users))

        # Admins
        self.admins.clear()
        self.admins.update(read_jid_array('xmpp', 'admins', keywords=('users',)))
        logger.info('Current admins: %s', ', '.join(self.admins))

        # Admins vs. users
        if not self.admins.issubset(self.users):
            for i in self.admins.difference(self.users):
                logger.error('Admin "%s" is not specified in users. '
                             'This may lead to unexpected behaviour.', i)

        # MUC room
        if config.has_option('xmpp', 'room'):
            self.room = config.get('xmpp', 'room').strip()
        else:
            self.room = None

        if config.has_option('xmpp', 'enabled'):
            self.room_invites = config.getboolean('xmpp', 'room_invites')
        else:
            self.room_invites = True

        # MUC room users
        self.room_users.clear()
        if self.room:
            self.room_users.update(read_jid_array('xmpp', 'room_users', keywords=('users', 'admins')))
            logger.info('Current room users: %s', ', '.join(self.room_users))

        # MUC room admins
        self.room_admins.clear()
        if self.room:
            self.room_admins.update(read_jid_array('xmpp', 'room_admins', keywords=('users', 'admins', 'room_users')))
            logger.info('Current room admins: %s', ', '.join(self.room_admins))

        # Room admins vs. users
        if not self.room_admins.issubset(self.room_users):
            for i in self.room_admins.difference(self.room_users):
                logger.error('Room admin "%s" is not specified in room_users. '
                             'This may lead to unexpected behaviour.', i)

        # Room users vs. room_users_invited
        if self.room_users_invited:
            self.room_users_invited.intersection_update(self.room_users)

        # Web server (any change in configuration requires restart)
        if init and not self.webserver:
            if config.has_option('webserver', 'host') and config.has_option('webserver', 'port'):
                host = config.get('webserver', 'host').strip()
                port = config.getint('webserver', 'port')

                if host and port:  # Enable server (will be started in __init__)
                    self.webserver = WebServer(host, port)

        # Cron (any change in configuration requires restart)
        if init and not self.cron:
            if config.has_option('cron', 'enabled') and config.getboolean('cron', 'enabled'):
                self.cron = Cron(db=self.db)

        if self._reloaded:
            if self.cron and self.db is None:  # DB support was disabled during reload
                self.cron.db_disable()

    def _load_plugins(self, config, plugins, init=False):
        """
        Initialize plugins.
        The init parameter indicates whether this is a first-time initialization or a reload.
        """
        if init:
            # First-time plugin initialization -> include ourself to plugins dict
            self.xmpp = self
            self.plugins.clear()
            self.plugins[__name__] = self
        else:
            # Bot reload - remove disabled plugins
            for enabled_plugin in tuple(self.plugins.keys()):  # Copy for python 3
                if enabled_plugin == __name__:
                    continue  # Skip ourself

                if enabled_plugin not in plugins:
                    logger.info('Disabling plugin: %s', enabled_plugin)
                    del self.plugins[enabled_plugin]

        if plugins:
            for plugin in plugins:
                modname = plugin.module

                if init or modname not in self.plugins:
                    logger.info('Initializing plugin: %s', modname)
                    reinit = False
                else:
                    logger.info('Reloading plugin: %s', modname)
                    del self.plugins[modname]
                    reinit = True

                try:
                    cfg = config.items(plugin.name)  # Get only plugin config section as list of (name, value) tuples
                    obj = plugin.cls(self, cfg, reinit=reinit)
                except Exception as ex:
                    logger.critical('Could not load plugin: %s', modname)
                    logger.exception(ex)
                else:
                    self.plugins[modname] = obj

        # Update commands cache
        if self.commands.all(reset=True):
            logger.info('Registered commands:\n%s\n', '\n'.join(self.commands.display()))
        else:
            logger.warning('NO commands registered')

        if self.webserver:
            if self.webserver.webhooks:
                logger.info('Registered webhooks:\n%s\n', '\n'.join(self.webserver.display_webhooks()))
            else:
                logger.warning('NO webhooks registered')
        else:
            logger.warning('Web server support disabled - webhooks will not work')

        if self.cron:
            if self.cron.crontab:
                logger.info('Registered cron jobs:\n%s\n', '\n'.join(self.cron.display_cronjobs()))
            else:
                logger.warning('NO cron jobs registered')
        else:
            logger.warning('Cron support disabled - cron jobs will not work')

    def _room_members(self):
        """
        Change multi-user chat room member list.
        """
        query = ET.Element('{http://jabber.org/protocol/muc#admin}query')
        qitem = '{http://jabber.org/protocol/muc#admin}item'
        query.append(ET.Element(qitem, {'affiliation': 'owner', 'jid': self.boundjid.bare}))

        for jid in self.room_users:
            if jid in self.room_admins:
                affiliation = 'admin'
            else:
                affiliation = 'member'

            item = ET.Element(qitem, {'affiliation': affiliation, 'jid': jid})
            query.append(item)

        iq = self.make_iq_set(query)
        iq['to'] = self.room
        iq['from'] = ''
        iq.send()

    def _room_config(self):
        """
        Configure multi-user chat room.
        """
        logger.info('Getting current configuration for MUC room %s', self.room)

        try:
            self.room_config = self.muc.getRoomConfig(self.room)
        except ValueError:
            logger.error('Could not get MUC room configuration. Maybe the room is not (properly) initialized.')
            return

        if self.room_users:
            self.room_config['fields']['muc#roomconfig_membersonly']['value'] = True
            self.room_config['fields']['members_by_default']['value'] = False
        else:
            self.room_config['fields']['muc#roomconfig_membersonly']['value'] = False
            self.room_config['fields']['members_by_default']['value'] = True

        logger.info('Setting new configuration for MUC room %s', self.room)
        try:
            self.muc.setRoomConfig(self.room, self.room_config)
        except IqError as e:
            logger.error('Could not configure MUC room. Error was: %s (condition=%s, etype=%s)',
                         e.text, e.condition, e.etype)

        logger.info('Setting member list for MUC room %s', self.room)
        try:
            self._room_members()
        except IqError as e:
            logger.error('Could not configure MUC room member list. Error was: %s (condition=%s, etype=%s)',
                         e.text, e.condition, e.etype)

    def _is_jid_in_room(self, jid):
        """Determine if jid is present in chat room"""
        for nick in self.muc.rooms[self.room]:
            entry = self.muc.rooms[self.room][nick]

            if entry is not None and entry['jid'].bare == jid:
                return True

        return False

    def _update_room_users_last_seen(self, jid):
        """Update last seen timestamp of user in chat room"""
        self.room_users_last_seen[jid] = datetime.now()

    def _jid_entered_room(self, jid):
        """User joined the chat room"""
        self._update_room_users_last_seen(jid)

    def _jid_left_room(self, jid):
        """User left the chat room"""
        self._update_room_users_last_seen(jid)

    def _handle_new_subscription(self, pres):
        """
        xmpp.auto_authorize is True by default, which is fine. But we want to
        restrict this to users only (if set). We do this by overriding the
        automatic subscription mechanism.
        """
        user = pres['from']

        if not self.users or user in self.users:
            logger.info('Allowing user "%s" to auto subscribe', user)
            return super(LudolphBot, self)._handle_new_subscription(pres)
        else:
            logger.warning('User "%s" is not allowed to subscribe', user)

    def get_jid(self, msg, bare=True):
        """
        Helper method for retrieving jid from message.
        """
        if msg['type'] == 'groupchat' and self.room:
            jid = self.muc.getJidProperty(msg['mucroom'], msg['mucnick'], 'jid')
        else:
            jid = msg['from']

        if bare and jid:
            return jid.bare

        return jid

    # noinspection PyUnusedLocal
    def session_start(self, event):
        """
        Process the session_start event.
        """
        self.get_roster()
        self.roster_cleanup()
        self.send_presence(pnick=self.nick)

        if self.room and self.muc:
            logger.info('Initializing multi-user chat room %s', self.room)
            self.muc.joinMUC(self.room, self.nick, maxhistory=self.maxhistory)

    def roster_cleanup(self):
        """
        Remove roster items with none subscription.
        """
        roster = self.client_roster
        logger.info('Current roster: %s', ', '.join(roster.keys()))

        # Remove users with none subscription from roster
        # Also remove users that are not in users setting (if set)
        for i in roster.keys():
            if roster[i]['subscription'] == 'none' or (self.users and i not in self.users):
                logger.warning('Roster item: %s (%s) - removing!', i, roster[i]['subscription'])
                self.send_presence(pto=i, ptype='unsubscribe')
                self.del_roster_item(i)
            elif roster[i]['subscription'] == 'to':
                logger.info('Roster item: %s (%s) - sending presence subscription', i, roster[i]['subscription'])
                self.send_presence_subscription(i)
            else:
                logger.info('Roster item: %s (%s) - ok', i, roster[i]['subscription'])

    def muc_online(self, presence):
        """
        Process an online presence stanza from a chat room.
        """
        # Configure room and say hello from jabber bot if this is a presence stanza
        if presence['from'] == '%s/%s' % (self.room, self.nick):
            self._room_config()
            self.msg_send(self.room, '%s is here!' % self.nick, mtype='groupchat')
            self._muc_ready = True
            self.send_presence(pto=presence['from'])
            logger.info('People in MUC room: %s', ', '.join(self.muc.getRoster(self.room)))

            # Reminder: We cannot use presence stanzas here because they are asynchronous
            # Reminder: We cannot use roster information here, because roster may not be ready at this point and
            #           roster_users != room_users
            # Save last seen info and send invitation to all users; unless an invitation was sent in the past
            for user in self.room_users:
                if self._is_jid_in_room(user):
                    logger.info('User "%s" already in MUC room', user)
                    self._update_room_users_last_seen(user)
                elif user != self.room:
                    if user in self.room_users_last_seen:
                        logger.info('User "%s" is not currently present in MUC room, but was last seen %s',
                                    user, self.room_users_last_seen[user].isoformat())
                    else:
                        logger.info('User "%s" is not present in MUC room', user)

                    if self.room_invites:
                        if user in self.room_users_invited:
                            logger.info('User "%s" was already invited to MUC room', user)
                        else:
                            logger.info('Inviting "%s" to MUC room', user)
                            self.muc.invite(self.room, user)
                            self.room_users_invited.add(user)

        else:
            # Say hello to new user
            muc = presence['muc']
            logger.info('User "%s" with nick "%s", role "%s" and affiliation "%s" is joining MUC room',
                        muc['jid'], muc['nick'], muc['role'], muc['affiliation'])
            self._jid_entered_room(muc['jid'])
            self.msg_send(presence['from'].bare, 'Hello %s!' % muc['nick'], mtype='groupchat')

    def muc_offline(self, presence):
        """
        Process a offline presence stanza from a chat room.
        """
        # Log user last seen status
        muc = presence['muc']
        logger.info('User "%s" with nick "%s", role "%s" and affiliation "%s" is leaving MUC room',
                    muc['jid'], muc['nick'], muc['role'], muc['affiliation'])
        self._jid_left_room(muc['jid'])
        self.msg_send(presence['from'].bare, 'Bye bye %s' % muc['nick'], mtype='groupchat')

    def message(self, msg, types=('chat', 'normal')):
        """
        Incoming message handler.
        """
        if msg['type'] not in types:
            return

        # Wrap around the Message object
        msg = IncomingLudolphMessage.wrap_msg(msg)
        # Seek received text in available commands and get command
        cmd = self.commands.get_command(msg['body'].split()[0].strip())

        if cmd:
            start_time = time.time()
            # Get and run command
            out = cmd.get_fun(self)(msg)

            if out:
                cmd_time = time.time() - start_time
                logger.info('Command %s.%s finished in %g seconds', cmd.module, cmd.name, cmd_time)

            return out
        else:
            # Send message that command was not understood and what to do
            return msg.reply('Sorry, I don\'t understand "%s"\n'
                             'Please type "help" for more info' % msg['body']).send()

    def muc_message(self, msg):
        """
        MUC Incoming message handler.
        """
        if not self._muc_ready:
            return

        if msg['mucnick'] == self.nick:
            return

        # Respond to the message only if the bots nickname is mentioned
        # And only if we can get user's JID
        nick = self.nick + ':'
        if msg['body'].startswith(nick) and self.get_jid(msg):
            msg['body'] = msg['body'].lstrip(nick).lstrip()
            return self.message(msg, types=('groupchat',))

    # noinspection PyUnusedLocal
    def shutdown(self, signalnum, handler):
        """
        Shutdown signal handler.
        """
        logger.info('Requested shutdown (%s)', signalnum)

        if self.shutting_down:
            logger.warn('Shutdown is already in progress...')
            return

        self.shutting_down = True

        try:
            if self.webserver:
                self.webserver.stop()
        except Exception as e:
            logger.error('Webserver shutdown failed: %s', e)

        try:
            if self.cron:
                self.cron.stop()
        except Exception as e:
            logger.error('Cron shutdown failed: %s', e)

        try:
            if self.db is not None:
                self._db_set_items()
                self.db.close()
                self.db_disable()
        except Exception as e:
            logger.critical('Persistent DB file could not be properly closed: %s', e)

        try:
            self.abort()
        except Exception as e:
            # Unhandled exception in SleekXMPP when socket is not connected and shutdown is requested
            if not self.socket:
                logger.exception(e)
                raise SystemExit(99)
            raise

    def prereload(self):
        """
        Cleanup during reload phase. Runs before plugin loading in main.
        """
        self.commands.reset()

        if self.webserver:
            self.webserver.reset_webhooks()
            self.webserver.reset_webapp()

        if self.cron:
            self.cron.reset()

        if self.db is not None:
            self._db_set_items()
            self.db.close()
            self.db_disable()

    def reload(self, config, plugins=None):
        """
        Reload bot configuration and plugins.
        """
        logger.info('Requested reload')
        self._reloaded = True
        self._load_config(config, init=False)
        self._load_plugins(config, plugins, init=False)

        if self.room and self.muc:
            self._muc_ready = False
            self.muc.leaveMUC(self.room, self.nick)
            logger.info('Reinitializing multi-user chat room %s', self.room)
            self.muc.joinMUC(self.room, self.nick, maxhistory=self.maxhistory)

    @staticmethod
    def msg_copy(msg, **kwargs):
        """
        Create copy of message stanza.
        """
        msg = copy.copy(msg)

        for key, val in kwargs.items():
            msg[key] = val

        return msg

    def msg_send(self, mto, mbody, **kwargs):
        """
        Create message and send it.
        """
        return OutgoingLudolphMessage.create(mbody, **kwargs).send(self, mto)

    # noinspection PyMethodMayBeStatic
    def msg_reply(self, msg, mbody, preserve_msg=False, **kwargs):
        """
        Set message reply text and html, and send it.
        """
        if mbody is None:
            return None  # Command performs custom message sending

        if preserve_msg:
            msg = self.msg_copy(msg)

        return OutgoingLudolphMessage.create(mbody, **kwargs).reply(msg)

    def msg_broadcast(self, mbody, **kwargs):
        """
        Send message to all users in roster.
        """
        msg = OutgoingLudolphMessage.create(mbody, **kwargs)

        for jid in self.client_roster:
            msg.send(self, jid)

        return len(self.client_roster)
