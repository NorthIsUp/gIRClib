# -*- coding: utf-8 -*-
"""
    ircliblet.irc
    ~~~~~~~~~~~~~

    *Under the hood* implementation of the IRC protocol handling.


    :copyright: © 2011 UfSoft.org - :email:`Pedro Algarvio (pedro@algarvio.me)`
    :license: BSD, see LICENSE for more details.
"""

import time
import socket
import random
import logging
import eventlet
import ircliblet
from string import letters, digits, punctuation
from ircliblet import signals
from ircliblet.exceptions import IRCBadMessage, IRCBadModes, UnhandledCommand
from ircliblet.helpers import (parse_modes, _int_or_default, split, ascii,
                               nick_from_netmask, ctcp_stringify, ctcp_extract,
                               X_DELIM, CHANNEL_PREFIXES, MAX_COMMAND_LENGTH,
                               parse_raw_irc_command, native,
                               _CommandDispatcherMixin)

log = logging.getLogger(__name__)

class IRCTransport(object):
    """
    IRC transport implementation, responsible for connecting, receiving and
    sending data to and from an IRC server.
    """

    encoding = "utf-8"
    network_host = None
    network_port = None
    use_ssl = False

    def connect(self, network_host, network_port=6667, use_ssl=False):
        self.network_host = network_host
        self.network_port = network_port
        self.use_ssl = use_ssl
        if self.use_ssl:
            raise NotImplementedError, "SSL support not properly tested yet"
            self.socket = eventlet.wrap_ssl(
                eventlet.connect((self.network_host, self.network_port))
            )
        else:
            self.socket = eventlet.connect(
                (self.network_host, self.network_port)
            )
#        signals.on_connected.send(self)
        eventlet.spawn_after(0.5, signals.on_connected.send, self)
        eventlet.spawn_n(self.__read_socket)

    def send(self, msg, *args, **kwargs):
        encoding = kwargs.get('encoding') or self.encoding
        bargs = []
        bkwargs = {}
        for arg in args:
            if isinstance(arg, str) or type(arg).__name__ == 'unicode':
                bargs.append(native(arg, encoding))
            elif isinstance(arg, native):
                bargs.append(arg)
            else:
                log.warning(
                    'Refusing to send one of the args from provided: %s',
                    repr([(type(arg), arg) for arg in args])
                )
        for key, value in kwargs.iteritems():
            if isinstance(value, str) or type(arg).__name__ == 'unicode':
                bkwargs[key] = native(value, encoding)
            elif isinstance(value, native):
                bkwargs[key] = arg
            else:
                log.warning(
                    'Refusing to send one of the args from provided: %s', kwargs
                )
        msg = (msg.replace("%s", "%%s") % bkwargs % tuple(bargs)).encode(encoding)
        log.debug("Sending message: \"%s\"", msg)
        self.socket.send("%s\r\n" % msg)

    def disconnect(self):
        self.socket.close()

    def __read_socket(self):
        buffer = ascii('')
        while True:
            try:
                buffer += self.socket.recv(2048)
            except socket.error, e:
                try:  # a little dance of compatibility to get the errno
                    errno = e.errno
                except AttributeError:
                    errno = e[0]
                if errno == 11:
                    print 1234567890, e, '\n\n\n'
#                    eventlet.spawn_after(0.1, self.__read_socket)
                else:
                    raise e
            else:
                data = buffer.replace(ascii('\r'), ascii('')).split(ascii("\n"))
                buffer = data.pop()
                for el in data:
                    eventlet.spawn(self.on_data_available, el)

    def on_data_available(self, data):
        raise NotImplementedError


class ServerSupportedFeatures(_CommandDispatcherMixin):
    """
    Handle ISUPPORT messages.

    Feature names match those in the ISUPPORT RFC draft identically.

    Information regarding the specifics of ISUPPORT was gleaned from
    `draft-brocklesby-irc-isupport-03.txt`.

    .. _draft-brocklesby-irc-isupport-03.txt: http://www.irc.org/tech_docs/draft-brocklesby-irc-isupport-03.txt
    """
    prefix = 'isupport'

    def __init__(self):
        self._features = {
            'CHANNELLEN': 200,
            'CHANTYPES': tuple('#&'),
            'MODES': 3,
            'NICKLEN': 9,
            'PREFIX': self._parse_prefix_param('(ovh)@+%'),
            # The ISUPPORT draft explicitly says that there is no default for
            # CHANMODES, but we're defaulting it here to handle the case where
            # the IRC server doesn't send us any ISUPPORT information, since
            # IRCClient.getChannelModeParams relies on this value.
            'CHANMODES': self._parse_chan_modes_param(['b', '', 'lk'])}

    @classmethod
    def _split_param_args(cls, params, value_processor=None):
        """
        Split ISUPPORT parameter arguments.

        Values can optionally be processed by ``value_processor``.

        For example::

            >>> ServerSupportedFeatures._split_param_args(['A:1', 'B:2'])
            (('A', '1'), ('B', '2'))

        :type params: ``iterable`` of ``str``

        :type value_processor: ``callable`` taking ``str``
        :param value_processor: Callable to process argument values, or ``None``
            to perform no processing

        :rtype: ``list`` of ``(str, object)``
        :return: Sequence of ``(name, processed_value)``
        """
        if value_processor is None:
            value_processor = lambda x: x

        def _parse():
            for param in params:
                if ':' not in param:
                    param += ':'
                a, b = param.split(':', 1)
                yield a, value_processor(b)
        return list(_parse())


    @classmethod
    def _unescape_param_value(cls, value):
        """
        Un-escape an ``ISUPPORT`` parameter.

        The only form of supported escape is ``\\xHH``, where HH must be a valid
        2-digit hexadecimal number.

        :rtype: ``str``
        """
        def _unescape():
            parts = value.split('\\x')
            # The first part can never be preceeded by the escape.
            yield parts.pop(0)
            for s in parts:
                octet, rest = s[:2], s[2:]
                try:
                    octet = int(octet, 16)
                except ValueError:
                    raise ValueError('Invalid hex octet: %r' % (octet,))
                yield chr(octet) + rest

        if '\\x' not in value:
            return value
        return ''.join(_unescape())

    @classmethod
    def _split_param(cls, param):
        """
        Split an ISUPPORT parameter.

        :type param: ``str``

        :rtype: ``(str, list)``
        :returns: ``(key, arguments)``

        """
        if '=' not in param:
            param += '='
        key, value = param.split('=', 1)
        return key, map(cls._unescape_param_value, value.split(','))


    @classmethod
    def _parse_prefix_param(cls, prefix):
        """
        Parse the ISUPPORT "PREFIX" parameter.

        The order in which the parameter arguments appear is significant, the
        earlier a mode appears the more privileges it gives.

        :rtype: ``dict`` mapping ``str`` to ``(str, int)``
        :return: A dictionary mapping a mode character to a two-tuple of
            ``(symbol, priority)``, the lower a priority (the lowest being
            ``0``) the more privileges it gives
        """
        if not prefix:
            return None
        if prefix[0] != '(' and ')' not in prefix:
            raise ValueError('Malformed PREFIX parameter')
        modes, symbols = prefix.split(')', 1)
        symbols = zip(symbols, xrange(len(symbols)))
        modes = modes[1:]
        return dict(zip(modes, symbols))


    @classmethod
    def _parse_chan_modes_param(self, params):
        """
        Parse the ISUPPORT "CHANMODES" parameter.

        See :meth:`~ircliblet.client.ServerSupportedFeatures.isupport_CHANMODES`
        for a detailed explanation of this parameter.
        """
        names = ('addressModes', 'param', 'setParam', 'noParam')
        if len(params) > len(names):
            raise ValueError(
                'Expecting a maximum of %d channel mode parameters, got %d' % (
                    len(names), len(params)))
        items = map(lambda key, value: (key, value or ''), names, params)
        return dict(items)


    def get_feature(self, feature, default=None):
        """
        Get a server supported feature's value.

        A feature with the value ``None`` is equivalent to the feature being
        unsupported.

        :type feature: ``str``
        :param feature: Feature name

        :type default: ``object``
        :param default: The value to default to, assuming that ``feature``
            is not supported

        :return: Feature value
        """
        return self._features.get(feature, default)


    def has_feature(self, feature):
        """
        Determine whether a feature is supported or not.

        :rtype: ``bool``
        """
        return self.get_feature(feature) is not None


    def parse(self, params):
        """
        Parse ISUPPORT parameters.

        If an unknown parameter is encountered, it is simply added to the
        dictionary, keyed by its name, as a tuple of the parameters provided.

        :type params: ``iterable`` of ``str``
        :param params: Iterable of ISUPPORT parameters to parse
        """
        for param in params:
            key, value = self._split_param(param)
            if key.startswith('-'):
                self._features.pop(key[1:], None)
            else:
                self._features[key] = self.dispatch(key, value)


    def isupport_unknown(self, command, params):
        """
        Unknown ISUPPORT parameter.
        """
        return tuple(params)


    def isupport_CHANLIMIT(self, params):
        """
        The maximum number of each channel type a user may join.
        """
        return self._split_param_args(params, _int_or_default)


    def isupport_CHANMODES(self, params):
        """
        Available channel modes.

        There are 4 categories of channel mode:

        * **param**: Modes that change a setting on a channel, these modes
          always take a parameter.

        * **noParam**: Modes that change a setting on a channel, these modes
          never take a parameter.

        * **setParam**: Modes that change a setting on a channel, these modes
          only take a parameter when being set.

        * **addressModes**: Modes that add or remove an address to or from a
          list, these modes always take a parameter.

        """
        try:
            return self._parse_chan_modes_param(params)
        except ValueError:
            return self.get_feature('CHANMODES')


    def isupport_CHANNELLEN(self, params):
        """
        Maximum length of a channel name a client may create.
        """
        return _int_or_default(params[0], self.get_feature('CHANNELLEN'))


    def isupport_CHANTYPES(self, params):
        """
        Valid channel prefixes.
        """
        return tuple(params[0])


    def isupport_EXCEPTS(self, params):
        """
        Mode character for "ban exceptions".

        The presence of this parameter indicates that the server supports
        this functionality.
        """
        return params[0] or 'e'


    def isupport_IDCHAN(self, params):
        """
        Safe channel identifiers.

        The presence of this parameter indicates that the server supports
        this functionality.
        """
        return self._split_param_args(params)


    def isupport_INVEX(self, params):
        """
        Mode character for "invite exceptions".

        The presence of this parameter indicates that the server supports
        this functionality.
        """
        return params[0] or 'I'


    def isupport_KICKLEN(self, params):
        """
        Maximum length of a kick message a client may provide.
        """
        return _int_or_default(params[0])


    def isupport_MAXLIST(self, params):
        """
        Maximum number of "list modes" a client may set on a channel at once.

        List modes are identified by the "addressModes" key in CHANMODES.
        """
        return self._split_param_args(params, _int_or_default)


    def isupport_MODES(self, params):
        """
        Maximum number of modes accepting parameters that may be sent, by a
        client, in a single MODE command.
        """
        return _int_or_default(params[0])


    def isupport_NETWORK(self, params):
        """
        IRC network name.
        """
        return params[0]


    def isupport_NICKLEN(self, params):
        """
        Maximum length of a nickname the client may use.
        """
        return _int_or_default(params[0], self.get_feature('NICKLEN'))


    def isupport_PREFIX(self, params):
        """
        Mapping of channel modes that clients may have to status flags.
        """
        try:
            return self._parse_prefix_param(params[0])
        except ValueError:
            return self.get_feature('PREFIX')


    def isupport_SAFELIST(self, params):
        """
        Flag indicating that a client may request a LIST without being
        disconnected due to the large amount of data generated.
        """
        return True


    def isupport_STATUSMSG(self, params):
        """
        The server supports sending messages to only to clients on a channel
        with a specific status.
        """
        return params[0]


    def isupport_TARGMAX(self, params):
        """
        Maximum number of targets allowable for commands that accept multiple
        targets.
        """
        return dict(self._split_param_args(params, _int_or_default))


    def isupport_TOPICLEN(self, params):
        """
        Maximum length of a topic that may be set.
        """
        return _int_or_default(params[0])


class IRCProtocol(IRCTransport):

    channels = {}
    _pings = None
    _MAX_PINGRING = 12
    _attempted_nick = None


    # ---- CTCP Abstraction Start ----------------------------------------------
    userinfo     = None

    def ctcp_query(self, user, channel, messages):
        """Dispatch method for any CTCP queries received.
        """
        for m in messages:
            method = getattr(self, "ctcp_query_%s" % m[0], None)
            if method:
                method(user, channel, m[1])
            else:
                self.ctcp_unknown_query(user, channel, m[0], m[1])

    def ctcp_query_ACTION(self, user, channel, data):
        signals.on_action.send(self, user=user, channel=channel, data=data)

    def ctcp_query_PING(self, user, channel, data):
        signals.on_ctcp_query_ping.send(
            self, user=user, channel=channel, data=data
        )

    def ctcp_query_FINGER(self, user, channel, data):
        if data is not None:
            self.quirky_message("Why did %s send '%s' with a FINGER query?"
                               % (user, data))

        signals.on_ctcp_query_finger.send(
            self, user=user, channel=channel, data=data
        )

    def ctcp_query_VERSION(self, user, channel, data):
        if data is not None:
            self.quirky_message("Why did %s send '%s' with a VERSION query?"
                               % (user, data))

        signals.on_ctcp_query_version.send(
            self, user=user, channel=channel, data=data
        )

    def ctcp_query_SOURCE(self, user, channel, data):
        if data is not None:
            self.quirky_message("Why did %s send '%s' with a SOURCE query?"
                               % (user, data))
        signals.on_ctcp_query_source.send(
            self, user=user, channel=channel, data=data
        )

    def ctcp_query_USERINFO(self, user, channel, data):
        if data is not None:
            self.quirky_message("Why did %s send '%s' with a USERINFO query?"
                                % (user, data))
        if self.userinfo:
            self.ctcp_make_reply(nick_from_netmask(user),
                                 [('USERINFO', self.userinfo)])

    def ctcp_query_CLIENTINFO(self, user, channel, data):
        """A master index of what CTCP tags this client knows.

        If no arguments are provided, respond with a list of known tags.
        If an argument is provided, provide human-readable help on
        the usage of that tag.
        """
        nick = nick_from_netmask(user)
        if not data:
            names = []
            for name in dir(self):
                if not name.startswith('ctcp_query_'):
                    continue
                elif not callable(getattr(self, name)):
                    continue
                names.append(name.lstrip('ctcp_query_'))

            self.ctcp_make_reply(nick, [
                ('CLIENTINFO', ascii(' ').join(names))
            ])
        else:
            args = data.split(ascii('\n'))
            method = getattr(self, 'ctcp_query_%s' % (args[0],), None)
            if not method:
                self.ctcp_make_reply(nick, [
                    ('ERRMSG', "CLIENTINFO %s :" "Unknown query '%s'"
                     % (data, args[0]))
                ])
                return
            doc = getattr(method, '__doc__', '')
            self.ctcp_make_reply(nick, [('CLIENTINFO', doc)])


    def ctcp_query_ERRMSG(self, user, channel, data):
        # Yeah, this seems strange, but that's what the spec says to do
        # when faced with an ERRMSG query (not a reply).
        self.ctcp_make_reply(nick_from_netmask(user), [
            ('ERRMSG', "%s :No error has occoured." % data)
        ])

    def ctcp_query_TIME(self, user, channel, data):
        if data is not None:
            self.quirky_message("Why did %s send '%s' with a TIME query?"
                                % (user, data))
        self.ctcp_make_reply(nick_from_netmask(user), [
            ('TIME', ':%s' % time.asctime(time.localtime(time.time())))
        ])

    def ctcp_unknown_query(self, user, channel, tag, data):
        self.ctcp_make_reply(nick_from_netmask(user), [
            ('ERRMSG', "%s %s: Unknown query '%s'" % (tag, data, tag))
        ])
        log.warn("Unknown CTCP query from %s: %s %s", user, tag, data)

    def ctcp_make_reply(self, user, messages):
        """
        Send one or more ``extended messages`` as a CTCP reply.

        :type messages: a list of extended messages.  An extended message is a
                        ``(tag, data)`` tuple, where 'data' may be ``None``.

        """
        self.notice(user, ctcp_stringify(messages))

    ### client CTCP query commands

    def ctcp_make_query(self, user, messages):
        """
        Send one or more ``extended messages`` as a CTCP query.

        :type messages: a list of extended messages.  An extended
                        message is a ``(tag, data)`` tuple, where 'data'
                        may be ``None``.

        """
        self.msg(user, ctcp_stringify(messages))

    ### Receiving a response to a CTCP query (presumably to one we made)
    ### You may want to add methods here, or override UnknownReply.

    def ctcp_reply(self, user, channel, messages):
        """
        Dispatch method for any CTCP replies received.
        """
        for msg in messages:
            method = getattr(self, "ctcp_reply_%s" % msg[0], None)
            if method:
                method(user, channel, msg[1])
            else:
                self.ctcp_unknown_reply(user, channel, msg[0], msg[1])

    def ctcp_reply_PING(self, user, channel, data):
        nick = nick_from_netmask(user)
        if (not self._pings) or (not self._pings.has_key((nick, data))):
            raise IRCBadMessage, "Bogus PING response from %s: %s" % (user, data)

        t0 = self._pings[(nick, data)]
        self.pong(user, time.time() - t0)

    def ctcp_unknown_reply(self, user, channel, tag, data):
        """Called when a fitting ``ctcp_reply_`` method is not found.

        :attention: If the client makes arbitrary CTCP queries, this method
                    should probably show the responses to them instead of
                    treating them as anomalies.

        """
        log.warn("Unknown CTCP reply from %s: %s %s", user, tag, data)

    # ---- CTCP Abstraction Ended ----------------------------------------------

    def quirky_message(self, msg):
        """This is called when I receive a message which is peculiar,
        but not wholly indecipherable.
        """
        log.warn("Quirky Message: \"%s\"", msg)

    # ---- IRC Abstraction Start -----------------------------------------------
    def irc_ERR_NICKNAMEINUSE(self, prefix, params):
        """
        Called when we try to register or change to a nickname that is already
        taken.
        """
        # TODO: signals
        signals.on_nickname_in_use.send(self, nickname=self._attempted_nick)

    def irc_ERR_ERRONEUSNICKNAME(self, prefix, params):
        """
        Called when we try to register or change to an illegal nickname.

        The server should send this reply when the nickname contains any
        disallowed characters.  The bot will stall, waiting for RPL_WELCOME, if
        we don't handle this during sign-on.

        :note: The method uses the spelling *erroneus*, as it appears in
            the RFC, section 6.1.

        """
        # TODO: signals
        log.warn("Tried to set a nick to an invalid nick. Setting it to fallback")
        signals.on_erroneous_nickname.send(self, nickname=self._attempted_nick)

    def irc_ERR_PASSWDMISMATCH(self, prefix, params):
        """
        Called when the login was incorrect.
        """
        log.error("Your login was incorrect!")
        signals.on_password_mismatch.send(self)

    def irc_ERR_NOTREGISTERED(self, prefix, params):
        """
        Called when we have not yet registered with the network
        """
        log.warn(params[-1])

    def irc_RPL_WELCOME(self, prefix, params):
        """
        Called when we have received the welcome from the server.
        """
        self._registered = True
        self.nickname = self._attempted_nick
        signals.on_signed_on.send(self)

    def irc_JOIN(self, prefix, params):
        """
        Called when a user joins a channel.
        """
        nick = nick_from_netmask(prefix)
        channel = params[-1]
        if nick == self.nickname:
            signals.on_joined.send(self, channel=channel)
        else:
            signals.on_user_joined.send(self, channel=channel, user=nick)

    def irc_PART(self, prefix, params):
        """
        Called when a user leaves a channel.
        """
        nick = nick_from_netmask(prefix)
        channel = params[0]
        if nick == self.nickname:
            signals.on_left.send(self, channel=channel)
        else:
            self.channels[channel].remove_user(nick)
            signals.on_user_left.send(self, user=nick, channel=channel)

    def irc_QUIT(self, prefix, params):
        """
        Called when a user has quit.
        """
        nick = nick_from_netmask(prefix)
        for channel in self.channels.iterkeys():
            if nick in self.channels[channel].names:
                self.channels[channel].remove_user(nick)
                break
        signals.on_user_quit.send(
            self, user=nick_from_netmask(prefix), message=params[0]
        )


    def irc_MODE(self, user, params):
        """
        Parse a server mode change message.
        """
        channel, modes, args = params[0], params[1], params[2:]

        if modes[0] not in '-+':
            modes = '+' + modes

        if channel == self.nickname:
            # This is a mode change to our individual user, not a channel mode
            # that involves us.
            param_modes = ['', '']
        else:
            prefixes = self.supported.get_feature('PREFIX', {})
            param_modes[0] = param_modes[1] = ''.join(prefixes.iterkeys())

            chanmodes = self.supported.get_feature('CHANMODES')
            if chanmodes is not None:
                param_modes[0] += chanmodes.get('addressModes', '')
                param_modes[0] += chanmodes.get('param', '')
                param_modes[1] = param_modes[0]
                param_modes[0] += chanmodes.get('setParam', '')

        try:
            added, removed = parse_modes(modes, args, param_modes)
        except IRCBadModes:
            log.error('An error occured while parsing the following MODE '
                      'message: MODE %s', ' '.join(params))
        else:
            if added:
                modes, params = zip(*added)
                signals.on_mode_changed.send(
                    self, user=user, channel=channel, set=True,
                    modes=ascii('').join(modes), args=params
                )

            if removed:
                modes, params = zip(*removed)
                signals.on_mode_changed.send(
                    self, user=user, channel=channel, set=False,
                    modes=ascii('').join(modes), args=params
                )


    def irc_PING(self, prefix, params):
        """
        Called when some has pinged us.
        """
        self.send("PONG %s", params[-1])

    def irc_PRIVMSG(self, prefix, params):
        """
        Called when we get a message.
        """
        user = prefix
        channel = params[0]
        message = params[-1]

        if not message:
            # don't raise an exception if some idiot sends us a blank message
            return

        if message[0]==X_DELIM:
            m = ctcp_extract(message)
            if m['extended']:
                self.ctcp_query(user, channel, m['extended'])

            if not m['normal']:
                return

            message = ascii(' ').join(m['normal'])
        signals.on_privmsg.send(self, user=user, channel=channel, message=message)

    def irc_NOTICE(self, prefix, params):
        """
        Called when a user gets a notice.
        """
        user = prefix
        channel = params[0]
        message = params[-1]

        if message[0]==X_DELIM:
            m = ctcp_extract(message)
            if m['extended']:
                self.ctcp_reply(user, channel, m['extended'])

            if not m['normal']:
                return
            message = ascii(' ').join(m['normal'])

        signals.on_notice.send(self, user=user, channel=channel, message=message)

    def irc_NICK(self, prefix, params):
        """
        Called when a user changes their nickname.
        """
        nick = nick_from_netmask(prefix)
        if nick == self.nickname:
            signals.on_nick_changed.send(self, nickname=nick)
        else:
            signals.on_user_renamed.send(self, oldname=nick, newname=params[0])

    def irc_KICK(self, prefix, params):
        """
        Called when a user is kicked from a channel.
        """
        kicker = nick_from_netmask(prefix)
        channel = params[0]
        kicked = params[1]
        message = params[-1]
        if ascii(kicked).lower() == ascii(self.nickname).lower():
            # Yikes!
            signals.on_kicked.send(self, channel=channel, kicker=kicker, message=message)
        else:
            signals.on_user_kicked.send(self, channel=channel, kicked=kicked,
                                        kicker=kicker, message=message)

    def irc_TOPIC(self, prefix, params):
        """
        Someone in the channel set the topic.
        """
        nick = nick_from_netmask(prefix)
        channel = params[0]
        newtopic = params[1]
        signals.on_topic_changed.send(self, user=nick, channel=channel,
                                      new_topic=newtopic)

    def irc_RPL_TOPIC(self, prefix, params):
        """
        Called when the topic for a channel is initially reported or when it
        subsequently changes.
        """
        nick = nick_from_netmask(prefix)
        channel = params[1]
        newtopic = params[2]
        signals.on_rpl_topic.send(self, user=nick, channel=channel,
                                  new_topic=newtopic)

    def irc_RPL_NOTOPIC(self, prefix, params):
        nick = nick_from_netmask(prefix)
        channel = params[1]
        signals.on_rpl_topic.send(self, user=nick, channel=channel)

    def irc_RPL_MOTDSTART(self, prefix, params):
        if params[-1].startswith("- "):
            params[-1] = params[-1][2:]
        self.motd = [params[-1]]

    def irc_RPL_MOTD(self, prefix, params):
        if params[-1].startswith("- "):
            params[-1] = params[-1][2:]
        if self.motd is None:
            self.motd = []
        self.motd.append(params[-1])

    def irc_RPL_ENDOFMOTD(self, prefix, params):
        """
        ``RPL_ENDOFMOTD`` indicates the end of the message of the day messages.
        """
        motd = self.motd
        self.motd = None
        signals.on_motd.send(self, motd=motd)

    def irc_RPL_CREATED(self, prefix, params):
        signals.on_rpl_created.send(self, when=params[1])

    def irc_RPL_YOURHOST(self, prefix, params):
        signals.on_rpl_yourhost.send(self, info=params[1])

    def irc_RPL_MYINFO(self, prefix, params):
        info = params[1].split(None, 3)
        while len(info) < 4:
            info.append(None)
        signals.on_rpl_myinfo.send(self, servername=info[0], version=info[1],
                              umodes=info[2], cmodes=info[3])

    def irc_RPL_BOUNCE(self, prefix, params):
        signals.on_rpl_bounce.send(self, info=params[1])

    def irc_RPL_ISUPPORT(self, prefix, params):
        args = params[1:-1]
        # Several ISUPPORT messages, in no particular order, may be sent
        # to the client at any given point in time (usually only on connect,
        # though.) For this reason, ServerSupportedFeatures.parse is intended
        # to mutate the supported feature list.
        self.supported.parse(args)
        signals.on_rpl_isupport.send(self, options=args)

    def irc_RPL_LUSERCLIENT(self, prefix, params):
        signals.on_rpl_luserclient.send(self, info=params[1])

    def irc_RPL_LUSEROP(self, prefix, params):
        try:
            signals.on_rpl_luserop.send(self, ops=int(params[1]))
        except ValueError:
            pass

    def irc_RPL_LUSERCHANNELS(self, prefix, params):
        try:
            signals.on_rpl_luserchannels.send(self, channels=int(params[1]))
        except ValueError:
            pass

    def irc_RPL_LUSERME(self, prefix, params):
        signals.on_rpl_luserme.send(self, info=params[1])

    def irc_RPL_NAMREPLY(self, prefix, params):
        """
        Receive channel users.
        """
        channel = params[2]
        nicks = params[3].split(ascii(' '))
        self.channels[channel].set_channel_privacy(params[1])
        self.channels[channel].add_users(set(nicks))

    def irc_RPL_ENDOFNAMES(self, prefix, params):
        """
        Finished receiving channel users.
        """
        channel = params[1]
        log.debug("Finished receiving channel users. %s",
                  self.channels[channel].users)
        signals.on_channel_users_available.send(
            self, channel_users=self.channels[channel].users
        )

    def handle_command(self, prefix, command, params):
        """
        Determine the function to call for the given command and call it with
        the given arguments.
        """
        method = getattr(self, "irc_%s" % command, None)
        try:
            if method is not None:
                method(prefix, params)
            else:
                self.irc_unknown(prefix, command, params)
        except Exception, err:
            log.exception(err)

    def irc_unknown(self, prefix, command, params):
        log.warn("Un%s IRC Command. Prefix: %s; Command: %s; Params: %s;",
                 command.isdigit() and "known" or "handled", prefix,
                 command, params)

class IRCChannel(object):
    def __init__(self, name, public=False, private=False, secret=False):
        self.name = name
        self.public = public
        self.private = private
        self.secret = secret
        self.users = set()

    def set_channel_privacy(self, mode):
        if mode == ascii("@"):
            self.secret = True
            self.private = False
            self.public = False
        elif mode == ascii("*"):
            self.secret = False
            self.private = True
            self.public = False
        elif mode == ascii("="):
            self.secret = False
            self.private = False
            self.public = True
        else:
            log.error("Tried to set channel \"%s\" private to an unknown "
                      "value: %s", self.name, mode)
            self.secret = False
            self.private = False
            self.public = False

    def add_user(self, nick):
        self.users.add(nick)

    def add_users(self, nicks):
        self.users = self.users.union(nicks)

    def remove_user(self, nick):
        self.users.remove(nick)

class IRCCommandsHelper(IRCProtocol):
    ### user input commands, client->server
    ### Your client will want to invoke these.
    def join(self, channel, key=None):
        """
        Join a channel.

        :type channel: ``str``
        :param channel: The name of the channel to join. If it has no prefix,
            ``#`` will be prepended to it.
        :type key: ``str``
        :param key: If specified, the key used to join the channel.
        """
        if channel[0] not in CHANNEL_PREFIXES:
            channel = '#' + channel
        self.channels[channel] = IRCChannel(channel)
        if key:
            self.send("JOIN %s %s", channel, key)
        else:
            self.send("JOIN %s", channel)

    def leave(self, channel, reason=None):
        """
        Leave a channel.

        :type channel: ``str``
        :param channel: The name of the channel to leave. If it has no prefix,
            ``#`` will be prepended to it.
        :type reason: ``str``
        :param reason: If given, the reason for leaving.
        """
        if channel[0] not in CHANNEL_PREFIXES:
            channel = '#' + channel
        if reason:
            self.send("PART %s :%s", channel, reason)
        else:
            self.send("PART %s", channel)

    def kick(self, channel, user, reason=None):
        """
        Attempt to kick a user from a channel.

        :type channel: ``str``
        :param channel: The name of the channel to kick the user from. If it has
            no prefix, ``#`` will be prepended to it.
        :type user: ``str``
        :param user: The nick of the user to kick.
        :type reason: ``str``
        :param reason: If given, the reason for kicking the user.
        """
        if channel[0] not in CHANNEL_PREFIXES:
            channel = '#' + channel
        if reason:
            self.send("KICK %s %s :%s", channel, user, reason)
        else:
            self.send("KICK %s %s", channel, user)

    part = leave

    def topic(self, channel, topic=None):
        """
        Attempt to set the topic of the given channel, or ask what it is.

        If topic is None, then I sent a topic query instead of trying to set the
        topic. The server should respond with a TOPIC message containing the
        current topic of the given channel.

        :type channel: ``str``
        :param channel: The name of the channel to change the topic on. If it
            has no prefix, ``#`` will be prepended to it.
        :type topic: ``str``
        :param topic: If specified, what to set the topic to.
        """
        # << TOPIC #xtestx :fff
        if channel[0] not in CHANNEL_PREFIXES:
            channel = '#' + channel
        if topic != None:
            self.send("TOPIC %s :%s", channel, topic)
        else:
            self.send("TOPIC %s", channel)

    def mode(self, chan, set, modes, limit=None, user=None, mask=None):
        """
        Change the modes on a user or channel.

        The ``limit``, ``user``, and ``mask`` parameters are mutually exclusive.

        :type chan: ``str``
        :param chan: The name of the channel to operate on.
        :type set: ``bool``
        :param set: True to give the user or channel permissions and False to
            remove them.
        :type modes: ``str``
        :param modes: The mode flags to set on the user or channel.
        :type limit: ``int``
        :param limit: In conjunction with the ``l`` mode flag, limits the
             number of users on the channel.
        :type user: ``str``
        :param user: The user to change the mode on.
        :type mask: ``str``
        :param mask: In conjunction with the ``b`` mode flag, sets a mask of
            users to be banned from the channel.
        """
        if set:
            line = 'MODE %s +%s' % (chan, modes)
        else:
            line = 'MODE %s -%s' % (chan, modes)
        if limit is not None:
            line = '%s %d' % (line, limit)
        elif user is not None:
            line = '%s %s' % (line, user)
        elif mask is not None:
            line = '%s %s' % (line, mask)
        self.send(line)


    def say(self, channel, message, length=None):
        """
        Send a message to a channel

        :type channel: ``str``
        :param channel: The channel to say the message on. If it has no prefix,
            ``#`` will be prepended to it.
        :type message: ``str``
        :param message: The message to say.
        :type length: ``int``
        :param length: The maximum number of octets to send at a time.  This has
            the effect of turning a single call to ``msg()`` into multiple
            commands to the server.  This is useful when long messages may be
            sent that would otherwise cause the server to kick us off or
            silently truncate the text we are sending.  If ``None`` is passed,
            the entire message is always send in one command.
        """
        if channel[0] not in CHANNEL_PREFIXES:
            channel = '#' + channel
        self.msg(channel, message, length)


    def msg(self, user, message, length=MAX_COMMAND_LENGTH):
        """
        Send a message to a user or channel.

        The message will be split into multiple commands to the server if:
         - The message contains any newline characters
         - Any span between newline characters is longer than the given
           line-length.

        :param user: The username or channel name to which to direct the
            message.
        :type user: ``str``

        :param message: The text to send.
        :type message: ``str``

        :param length: The maximum number of octets to send in a single
            command, including the IRC protocol framing. If not supplied,
            defaults to ``MAX_COMMAND_LENGTH``.
        :type length: ``int``
        """
        fmt = "PRIVMSG %s :%%s" % (user,)

        if length is None:
            length = MAX_COMMAND_LENGTH

        # NOTE: minimum_length really equals len(fmt) - 2 (for '%s') + 2
        # (for the line-terminating CRLF)
        minimum_length = len(fmt)
        if length <= minimum_length:
            raise ValueError("Maximum length must exceed %d for message "
                             "to %s" % (minimum_length, user))
        for line in split(message, length - minimum_length):
            self.send(fmt, line)


    def notice(self, user, message):
        """
        Send a notice to a user.

        Notices are like normal message, but should never get automated
        replies.

        :type user: ``str``
        :param user: The user to send a notice to.
        :type message: ``str``
        :param message: The contents of the notice to send.
        """
        self.send("NOTICE %s :%s", user, message)


    def away(self, message=''):
        """
        Mark this client as away.

        :type message: ``str``
        :param message: If specified, the away message.
        """
        self.send("AWAY :%s", message)


    def back(self):
        """
        Clear the away status.
        """
        # An empty away marks us as back
        self.away()


    def whois(self, nickname, server=None):
        """
        Retrieve user information about the given nick name.

        :type nickname: ``str``
        :param nickname: The nick name about which to retrieve information.

        """
        if server is None:
            self.send('WHOIS %s', nickname)
        else:
            self.send('WHOIS %s %s', server, nickname)

    def register(self, nickname, hostname="foo", servername="bar"):
        """
        Login to the server.

        :type nickname: ``str``
        :param nickname: The nickname to register.
        :type hostname: ``str``
        :param hostname: If specified, the hostname to logon as.
        :type servername: ``str``
        :param servername: If specified, the servername to logon as.
        """
        if self.password is not None:
            self.send("PASS %s", self.password)
        self.set_nick(nickname)
        if self.username is None:
            self.username = nickname
        self.send("USER %s %s %s :%s", self.username, hostname, servername,
                  self.realname)

    def set_nick(self, nickname):
        """
        Set this client's nickname.

        :type nickname: ``str``
        :param nickname: The nickname to change to.
        """
        self._attempted_nick = nickname
        self.send("NICK %s", nickname)

    def quit(self, message=''):
        """
        Disconnect from the server

        :type message: ``str``
        :param message: If specified, the message to give when quitting the
            server.
        """
        self.send("QUIT :%s", message)

    ### user input commands, client->client
    def describe(self, channel, action):
        """
        Strike a pose.

        :type channel: ``str``
        :param channel: The name of the channel to have an action on. If it
            has no prefix, it is sent to the user of that name.
        :type action: ``str``
        :param action: The action to preform.

        """
        self.ctcp_make_query(channel, [('ACTION', action)])


    def ping(self, user, text=None):
        """
        Measure round-trip delay to another IRC client.
        """
        if self._pings is None:
            self._pings = {}

        if text is None:
            chars = letters + digits + punctuation
            key = ''.join([random.choice(chars) for i in range(12)])
        else:
            key = str(text)
        self._pings[(user, key)] = time.time()
        self.ctcp_make_query(user, [('PING', key)])

        if len(self._pings) > self._MAX_PINGRING:
            # Remove some of the oldest entries.
            byValue = [(v, k) for (k, v) in self._pings.items()]
            byValue.sort()
            excess = self._MAX_PINGRING - len(self._pings)
            for i in xrange(excess):
                del self._pings[byValue[i][1]]

class BaseIRCClient(IRCCommandsHelper):
    def __init__(self):
        self.pool = eventlet.GreenPool()
        self.pile = eventlet.GreenPile(self.pool)
        self.supported = ServerSupportedFeatures()

    def on_data_available(self, data):
        log.debug("Data %r", data)
        prefix, command, args = parse_raw_irc_command(data)
        log.debug('Incomming message. Prefix: "%s" Command: "%s" '
                  'Args: "%s"', prefix, command, args)
        self.pile.spawn(self.handle_command, prefix, command, args)