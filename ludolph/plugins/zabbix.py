"""
Ludolph: Monitoring Jabber bot
Copyright (C) 2012-13 Erigones s. r. o.
This file is part of Ludolph.

See the file LICENSE for copying permission.
"""
import logging
from datetime import datetime, timedelta

from ludolph.command import command, parameter_required
from ludolph.message import tabulate
from ludolph.plugins.plugin import LudolphPlugin
from ludolph.plugins.zabbix_api import ZabbixAPI, ZabbixAPIException

TIMEOUT = 10

logger = logging.getLogger(__name__)


def zabbix_command(f):
    """
    Decorator for executing zabbix API commands and checking zabbix API errors.
    """
    def wrap(obj, msg, *args, **kwargs):
        def api_error(errmsg='Zabbix API not available'):
            # Log and reply with error message
            logger.error(errmsg)
            msg.reply(errmsg).send()
            return None

        # Was never logged in. Repair authentication and restart Ludolph.
        if not obj.zapi.logged_in():
            return api_error()

        try:
            return f(obj, msg, *args, **kwargs)
        except ZabbixAPIException as ex:
            # API command problem
            return api_error('Zabbix API error (%s)' % ex)

    return wrap


class Zabbix(LudolphPlugin):
    """
    Zabbix API connector for LudolphBot.

    Zabbix >= 2.0.6 is required.
    https://www.zabbix.com/documentation/2.0/manual/appendix/api/api
    """
    zapi = None

    def __init__(self, config, *args, **kwargs):
        """
        Login to zabbix.
        """
        self.init(config)

    def init(self, config):
        """
        Initialize zapi and try to login.
        """
        # HTTP authentication?
        httpuser = None
        httppasswd = None
        if config.has_option('zabbix', 'httpuser'):
            httpuser = config.get('zabbix', 'httpuser')
        if config.has_option('zabbix', 'httppasswd'):
            httppasswd = config.get('zabbix', 'httppasswd')

        self.zapi = ZabbixAPI(server=config.get('zabbix', 'server'),
                              user=httpuser, passwd=httppasswd, timeout=TIMEOUT,
                              log_level=logging.getLevelName(config.get('global', 'loglevel')))

        # Login and save zabbix credentials
        try:
            logger.info('Zabbix API login')
            self.zapi.login(config.get('zabbix', 'username'),
                            config.get('zabbix', 'password'), save=True)
        except ZabbixAPIException as e:
            logger.critical('Zabbix API login error (%s)', e)

    def reload(self, config):
        """
        Logout and login zapi.
        """
        self.zapi.auth = ''  # Logout
        self.init(config)

    @zabbix_command
    @command
    def zabbix_version(self, msg):
        """
        Show version of Zabbix API.

        Usage: zabbix-version
        """
        return 'Zabbix API version: ' + self.zapi.api_version()

    @zabbix_command
    @command
    def alerts(self, msg, notes=False):
        """
        Show a list of current zabbix alerts.

        Use optional "notes" parameter to display all notes attached to every event ID.

        Usage: alerts [notes]
        """
        # Get triggers
        triggers = self.zapi.trigger.get({
                'groupids': None,
                'hostids': None,
                'monitored': True,
                'maintenance': False,
                'skipDependent': True,
                'filter': {'priority': None, 'value': 1},  # TRIGGER_VALUE_TRUE
                'selectHosts': ['hostid', 'name', 'maintenance_status', 'maintenance_type', 'maintenanceid'],
                'selectLastEvent':  'extend',  # API_OUTPUT_EXTEND
                'output': ['triggerid', 'value_flags', 'error', 'url', 'expression', 'description', 'priority', 'type'],
                'sortfield': 'lastchange',
                'sortorder': 'DESC',  # ZBX_SORT_DOWN
        })

        # Get notes = event acknowledges
        if notes:
            events = self.zapi.event.get({
                    'eventids': [t['lastEvent']['eventid'] for t in triggers if t['lastEvent']],
                    'output': 'extend',
                    'select_acknowledges': 'extend',
            })

        # Output
        headers = ['EventID', 'Severity', 'Host', 'Issue', 'Age', 'Ack']
        table = []

        for trigger in triggers:
            # If trigger is lost (broken expression) we skip it
            if not trigger['hosts']:
                continue

            # Event
            event = trigger['lastEvent']
            if event:
                eventid = '*%s*' % event['eventid']
                # Ack
                if int(event['acknowledged']):
                    ack = '^*ACK*^'
                else:
                    ack = ''
            else:
                # WTF?
                eventid = '*????*'
                ack = ''

            # Host and hostname
            host = trigger['hosts'][0]
            hostname = host['name']
            if int(host['maintenance_status']):
                hostname += '+'  # some kind of maintenance

            # Trigger description
            desc = str(trigger['description'])
            if trigger['error'] or int(trigger['value_flags']):
                desc += ' *+*'  # some kind of trigger error

            # Priority
            prio = self.zapi.get_severity(trigger['priority']).ljust(12)

            # Last change and age
            dt = self.zapi.get_datetime(trigger['lastchange'])
            #last = self.zapi.convert_datetime(dt)
            age = '^%s^' % self.zapi.get_age(dt)

            row = [eventid, prio, hostname, desc, age, ack]

            if notes:
                acknowledges = ''
                for i, e in enumerate(events):
                    if e['eventid'] == event['eventid']:
                        for a in e['acknowledges']:
                            acknowledges = '\n\t\t%s: %s' % (self.zapi.get_datetime(a['clock']), a['message']) + acknowledges
                        del events[i]
                        break

                row.append(acknowledges)

            table.append(row)

        if table:
            out = str(tabulate(table, headers=headers)) + '\n\n'
        else:
            out = ''

        out += '*%d* issues are shown.\n%s' % (len(triggers), self.zapi.server)

        return out

    @zabbix_command
    @parameter_required(1)
    @command
    def ack(self, msg, eventid, note=None):
        """
        Acknowledge event with optional note.

        Usage: ack <event ID> [note]
        """
        try:
            eventid = int(eventid)
        except ValueError:
            return 'Integer required'

        message = '%s: ' % self.xmpp.get_jid(msg)

        if note:
            message += note
        else:
            message += 'ack'

        self.zapi.event.acknowledge({
            'eventids': [eventid],
            'message': message,
        })

        return 'Event ID *%s* acknowledged' % eventid

    def _outage_del(self, msg, mid):
        """
        Delete maintenance period specified by maintenance ID.

        Usage: outage-del <maintenance ID>
        """
        try:
            mid = int(mid)
        except ValueError:
            return 'Integer required'

        self.zapi.maintenance.delete([mid])

        return 'Maintenance ID *%s* deleted' % mid

    def _outage_add(self, msg, host_or_group, duration):
        """
        Set maintenance period for specified host and time.

        Usage: outage-add <host/group name> <duration in minutes>
        """
        # Get start and end time
        try:
            duration = int(duration)
        except ValueError:
            return 'Integer required'
        else:
            period = timedelta(minutes=duration)
            _now = datetime.now()
            _end = _now + period
            now = _now.strftime('%s')
            end = _end.strftime('%s')

        jid = self.xmpp.get_jid(msg)

        options = {
                'active_since': now,
                'active_till': end,
                'description': str(jid),
                'maintenance_type': 0,  # with data collection
                'timeperiods': [{
                    'timeperiod_type': 0,  # one time only
                    'start_date': now,
                    'period': period.seconds,
                }],
        }

        # Get hosts
        hosts = self.zapi.host.get({
            'filter': {'name': [host_or_group]},
            'output': ['hostid', 'name'],
        })

        if hosts:
            options['hostids'] = [i['hostid'] for i in hosts]
            names = [i['name'] for i in hosts]

        if not hosts:
            # Get groups
            groups = self.zapi.hostgroup.get({
                'filter': {'name': [host_or_group]},
                'output': ['groupids', 'name'],
            })

            if groups:
                options['groupids'] = [i['groupid'] for i in groups]
                names = [i['name'] for i in groups]
            else:
                return "Host/Group not found"

        names = ','.join(names)
        options['name'] = 'Maintenance for %s - %s' % (names, now)

        # Create maintenance period
        res = self.zapi.maintenance.create(options)

        return 'Added maintenance ID *%s* for %s %s' % (res['maintenanceids'][0],
                                                        'host' if hosts else 'group', names)

    @zabbix_command
    @command
    def outage(self, msg, mid_or_host_or_group=None, duration=None):
        """
        Show, create or delete maintenance periods.

        Show all maintenance periods.
        Usage: outage

        Set maintenance period for specified host and time.
        Usage: outage <host/group name> <duration in minutes>

        Delete maintenance period specified by maintenance ID.
        Usage: outage <maintenance ID>
        """
        if duration:
            return self._outage_add(msg, mid_or_host_or_group, duration)
        elif mid_or_host_or_group:
            return self._outage_del(msg, mid_or_host_or_group)

        # Display list of maintenances
        maintenances = self.zapi.maintenance.get({
            'output': 'extend',
            'sortfield': ['maintenanceid', 'name'],
            'sortorder': 'ASC',
            'selectHosts': 'extend',
            'selectGroups': 'extend',
        })

        table = []
        headers = ['ID', 'Name', 'Desc', 'Hosts', 'Groups', 'Since - Till']

        for i in maintenances:
            if i['hosts']:
                hosts = '^%s^' % ', '.join([h['name'] for h in i['hosts']])
            else:
                hosts = ''

            if i['groups']:
                groups = '^%s^' % ', '.join([g['name'] for g in i['groups']])
            else:
                groups = ''

            since = self.zapi.timestamp_to_datetime(i['active_since'])
            until = self.zapi.timestamp_to_datetime(i['active_till'])

            table.append([
                '*%s*' % i['maintenanceid'],
                i['name'],
                i['description'],
                hosts,
                groups,
                '\n\t%s - %s' % (since, until),
            ])

        if table:
            out = str(tabulate(table, headers=headers)) + '\n\n'
        else:
            out = ''

        out += '*%d* maintenances are shown.\n%s' % (len(maintenances), self.zapi.server + '/maintenance.php?groupid=0')

        return out
