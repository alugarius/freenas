# +
# Copyright 2010 iXsystems, Inc.
# All rights reserved
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted providing that the following conditions
# are met:
# 1. Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE AUTHOR ``AS IS'' AND ANY EXPRESS OR
# IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED.  IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY
# DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS
# OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
# HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT,
# STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING
# IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
#
#####################################################################
import dateutil
import logging
import os
import uuid
import signal

from dateutil import parser as dtparser

from django.conf import settings
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models
from django.utils.translation import ugettext_lazy as _

from freenasUI import choices
from freenasUI.freeadmin.models import ConfigQuerySet, NewManager, NewModel, Model, UserField
from freenasUI.middleware.notifier import notifier
from freenasUI.storage.models import Volume

log = logging.getLogger('system.models')


class Alert(Model):
    message_id = models.CharField(
        unique=True,
        max_length=100,
        )
    dismiss = models.BooleanField(
        default=True,
        )


class Settings(NewModel):
    stg_guiprotocol = models.CharField(
            max_length=120,
            choices=choices.PROTOCOL_CHOICES,
            default="http",
            verbose_name=_("Protocol")
            )
    stg_guicertificate = models.ForeignKey(
            "Certificate",
            verbose_name=_("Certificate"),
            on_delete=models.SET_NULL,
            blank=True,
            null=True
            )
    stg_guiaddress = models.CharField(
            max_length=120,
            blank=True,
            default='0.0.0.0',
            verbose_name=_("WebGUI IPv4 Address")
            )
    stg_guiv6address = models.CharField(
            max_length=120,
            blank=True,
            default='::',
            verbose_name=_("WebGUI IPv6 Address")
            )
    stg_guiport = models.IntegerField(
        verbose_name=_("WebGUI HTTP Port"),
        validators=[MinValueValidator(1), MaxValueValidator(65535)],
        default=80,
    )
    stg_guihttpsport = models.IntegerField(
        verbose_name=_("WebGUI HTTPS Port"),
        validators=[MinValueValidator(1), MaxValueValidator(65535)],
        default=443,
    )
    stg_guihttpsredirect = models.BooleanField(
        verbose_name=_('WebGUI HTTP -> HTTPS Redirect'),
        default=True,
        help_text=_(
            'Redirect HTTP (port 80) to HTTPS when only the HTTPS protocol is '
            'enabled'
        ),
    )
    stg_language = models.CharField(
            max_length=120,
            choices=settings.LANGUAGES,
            default="en",
            verbose_name=_("Language")
            )
    stg_kbdmap = models.CharField(
            max_length=120,
            choices=choices.KBDMAP_CHOICES(),
            verbose_name=_("Console Keyboard Map"),
            blank=True,
            )
    stg_timezone = models.CharField(
            max_length=120,
            choices=choices.TimeZoneChoices(),
            default="America/Los_Angeles",
            verbose_name=_("Timezone")
            )
    stg_syslogserver = models.CharField(
            default='',
            blank=True,
            max_length=120,
            verbose_name=_("Syslog server")
            )
    stg_wizardshown = models.BooleanField(
        editable=False,
        default=False,
    )
    stg_pwenc_check = models.CharField(
        max_length=100,
        editable=False,
    )

    objects = NewManager(qs_class=ConfigQuerySet)

    class Meta:
        verbose_name = _("General")

    class Middleware:
        configstore = True

    @classmethod
    def _load(cls):
        from freenasUI.middleware.connector import connection as dispatcher
        sysui = dispatcher.call_sync('system.ui.get_config')
        sysgen = dispatcher.call_sync('system.general.get_config')

        protocol = sysui.get('webui_procotol', [])
        if 'HTTP' in protocol and 'HTTPS' in protocol:
            protocol = 'httphttps'
        elif 'HTTP' in protocol:
            protocol = 'http'
        elif 'HTTPS' in protocol:
            protocol = 'https'

        certificate = sysui.get('webui_https_certificate')
        if certificate:
            try:
                certificate = Certificate.objects.get(pk=certificate)
            except Certificate.DoesNotExist:
                certificate = None

        # FIXME: only first address accounted for
        listenv4 = None
        listenv6 = None
        for i in sysui.get('webui_listen', []):
            if ':' in i and not listenv6:
                listenv6 = i.strip('[]')
            elif not listenv4:
                listenv4 = i

        user_attrs = dispatcher.call_sync(
            'users.query',
            [('id', '=', 0)],
            {'single': True}
        ).get('attributes')

        return cls(**dict(
            id=1,
            stg_guiprotocol=protocol,
            stg_guicertificate=certificate,
            stg_guiport=sysui.get('webui_http_port'),
            stg_guihttpsport=sysui.get('webui_https_port'),
            stg_guiaddress=listenv4,
            stg_guiv6address=listenv6,
            stg_guihttpsredirect=sysui.get('webui_http_redirect_https'),
            stg_timezone=sysgen.get('timezone'),
            stg_language=sysgen.get('language'),
            stg_kbdmap=sysgen.get('console_keymap'),
            stg_syslogserver=sysgen.get('syslog_server'),
            stg_wizardshown=user_attrs.get('gui_wizard_shown', False),
        ))

    def _save(self, *args, **kwargs):
        from freenasUI.middleware.connector import connection as dispatcher
        if self.stg_guiprotocol == 'httphttps':
            protocol = ['HTTP', 'HTTPS']
        elif self.stg_guiprotocol == 'https':
            protocol = ['HTTPS']
        else:
            protocol = ['HTTP']

        listen = []
        if self.stg_guiaddress:
            listen.append(self.stg_guiaddress)
        if self.stg_guiv6address:
            listen.append('[{0}]'.format(self.stg_guiv6address))

        if self.stg_guicertificate:
            certificate = self.stg_guicertificate.id
        else:
            certificate = None

        data = {
            'webui_protocol': protocol,
            'webui_listen': listen,
            'webui_http_redirect_https': self.stg_guihttpsredirect or False,
            'webui_https_certificate': certificate,
        }
        if self.stg_guiport:
            data['webui_http_port'] = self.stg_guiport
        if self.stg_guihttpsport:
            data['webui_https_port'] = self.stg_guihttpsport

        self._save_task_call('system.ui.configure', data)

        data = {
            'language': self.stg_language,
            'timezone': self.stg_timezone,
            'console_keymap': self.stg_kbdmap,
            'syslog_server': self.stg_syslogserver,
        }
        self._save_task_call('system.general.configure', data)

        user = dispatcher.call_sync('users.query', [('id', '=', 0)], {'single': True})
        user_attrs = user['attributes']
        new_user_attrs = user_attrs.copy()
        new_user_attrs.update({
            'gui_wizard_shown': self.stg_wizardshown,
        })
        if user_attrs != new_user_attrs:
            self._save_task_call('users.update', user['id'], {'attributes': new_user_attrs})

        return True


class NTPServer(NewModel):
    id = models.CharField(editable=False, max_length=120, primary_key=True)
    ntp_address = models.CharField(
            verbose_name=_("Address"),
            max_length=120,
            )
    ntp_burst = models.BooleanField(
            verbose_name=_("Burst"),
            default=False,
            help_text=_("When the server is reachable, send a burst of eight "
                "packets instead of the usual one. This is designed to improve"
                " timekeeping quality with the server command and s addresses."
                ),
            )
    ntp_iburst = models.BooleanField(
            verbose_name=_("IBurst"),
            default=True,
            help_text=_("When the server is unreachable, send a burst of eight"
                " packets instead of the usual one. This is designed to speed "
                "the initial synchronization acquisition with the server "
                "command and s addresses."),
            )
    ntp_prefer = models.BooleanField(
            verbose_name=_("Prefer"),
            default=False,
            help_text=_("Marks the server as preferred. All other things being"
                " equal, this host will be chosen for synchronization among a "
                "set of correctly operating hosts."),
            )
    ntp_minpoll = models.IntegerField(
            verbose_name=_("Min. Poll"),
            default=6,
            validators=[MinValueValidator(4)],
            help_text=_("The minimum poll interval for NTP messages, as a "
                "power of 2 in seconds. Defaults to 6 (64 s), but can be "
                "decreased to a lower limit of 4 (16 s)"),
            )
    ntp_maxpoll = models.IntegerField(
            verbose_name=_("Max. Poll"),
            default=10,
            validators=[MaxValueValidator(17)],
            help_text=_("The maximum poll interval for NTP messages, as a "
                "power of 2 in seconds. Defaults to 10 (1,024 s), but can be "
                "increased to an upper limit of 17 (36.4 h)"),
            )

    def __unicode__(self):
        return self.ntp_address

    def delete(self):
        super(NTPServer, self).delete()
        notifier().start("ix-ntpd")
        notifier().restart("ntpd")

    class Meta:
        verbose_name = _("NTP Server")
        verbose_name_plural = _("NTP Servers")
        ordering = ["ntp_address"]

    class FreeAdmin:
        icon_model = u"NTPServerIcon"
        icon_object = u"NTPServerIcon"
        icon_view = u"ViewNTPServerIcon"
        icon_add = u"AddNTPServerIcon"

    class Middleware:
        field_mapping = (
            ('id', 'id'),
            ('ntp_address', 'address'),
            ('ntp_burst', 'burst'),
            ('ntp_iburst', 'iburst'),
            ('ntp_prefer', 'prefer'),
            ('ntp_minpoll', 'minpoll'),
            ('ntp_maxpoll', 'maxpoll'),
        )
        provider_name = 'ntpservers'


class Advanced(NewModel):
    adv_consolemenu = models.BooleanField(
        verbose_name=_("Enable Console CLI"),
        default=False,
    )
    adv_serialconsole = models.BooleanField(
        verbose_name=_("Use Serial Console"),
        default=False,
    )
    adv_serialport = models.CharField(
        max_length=120,
        default="0x2f8",
        help_text=_(
            "Set this to match your serial port address (0x3f8, 0x2f8, etc.)"
        ),
        verbose_name=_("Serial Port Address"),
    )
    adv_serialspeed = models.CharField(
            max_length=120,
            choices=choices.SERIAL_SPEED,
            default="9600",
            help_text=_("Set this to match your serial port speed"),
            verbose_name=_("Serial Port Speed")
            )
    adv_consolescreensaver = models.BooleanField(
        verbose_name=_("Enable screen saver"),
        default=False,
    )
    adv_powerdaemon = models.BooleanField(
        verbose_name=_("Enable powerd (Power Saving Daemon)"),
        default=False,
    )
    adv_swapondrive = models.IntegerField(
            validators=[MinValueValidator(0)],
            verbose_name=_("Swap size on each drive in GiB, affects new disks "
                "only. Setting this to 0 disables swap creation completely "
                "(STRONGLY DISCOURAGED)."),
            default=2)
    adv_consolemsg = models.BooleanField(
            verbose_name=_("Show console messages in the footer"),
            default=True)
    adv_traceback = models.BooleanField(
            verbose_name=_("Show tracebacks in case of fatal errors"),
            default=True)
    adv_advancedmode = models.BooleanField(
        verbose_name=_("Show advanced fields by default"),
        default=False,
        help_text=_(
            "By default only essential fields are shown. Fields considered "
            "advanced can be displayed through the Advanced Mode button."
        ),
    )
    adv_autotune = models.BooleanField(
        verbose_name=_("Enable autotune"),
        default=False,
        help_text=_(
            "Attempt to automatically tune the network and ZFS system control "
            "variables based on memory available."
        ),
    )
    adv_debugkernel = models.BooleanField(
        verbose_name=_("Enable debug kernel"),
        default=False,
        help_text=_(
            "The kernel built with debug symbols will be booted instead."
        ),
    )
    adv_uploadcrash = models.BooleanField(
        verbose_name=_("Enable automatic upload of kernel crash dumps and daily telemetry"),
        default=True,
    )
    # TODO: need geom_eli in kernel
    #adv_encswap = models.BooleanField(
    #        verbose_name = _("Encrypt swap space"),
    #        default=False)
    adv_motd = models.TextField(
        max_length=1024,
        verbose_name=_("MOTD banner"),
        default='Welcome',
    )
    adv_boot_scrub = models.IntegerField(
        default=35,
        editable=False,
    )
    adv_periodic_notifyuser = UserField(
        default="root",
        verbose_name=_("Periodic Notification User"),
        help_text=_("If you wish periodic emails to be sent to a different email address than "
                    "the alert emails are set to (root) set an email address for a user and "
                    "select that user in the dropdown.")
    )

    objects = NewManager(qs_class=ConfigQuerySet)

    class Meta:
        verbose_name = _("Advanced")

    class FreeAdmin:
        deletable = False

    class Middleware:
        configstore = True

    @classmethod
    def _load(cls):
        from freenasUI.account.models import bsdUsers
        from freenasUI.middleware.connector import connection as dispatcher
        adv = dispatcher.call_sync('system.advanced.get_config')
        user_attrs = dispatcher.call_sync(
            'users.query',
            [('id', '=', 0)],
            {'single': True}
        ).get('attributes')
        try:
            user = bsdUsers.objects.get(id=adv['periodic_notify_user'])
        except bsdUsers.DoesNotExist:
            user = bsdUsers.objects.order_by('id')[0]
        return cls(**dict(
            adv_consolemenu=adv['console_cli'],
            adv_serialconsole=adv['serial_console'],
            adv_serialport=adv['serial_port'],
            adv_serialspeed=str(adv['serial_speed']),
            adv_consolescreensaver=adv['console_screensaver'],
            adv_powerdaemon=adv['powerd'],
            adv_swapondrive=adv['swapondrive'],
            adv_consolemsg=user_attrs.get('gui_messages_footer', False),
            adv_traceback=user_attrs.get('gui_traceback', False),
            adv_advancedmode=user_attrs.get('gui_advancedmode', False),
            adv_autotune=adv['autotune'],
            adv_debugkernel=adv['debugkernel'],
            adv_uploadcrash=adv['uploadcrash'],
            adv_motd=adv['motd'],
            adv_boot_scrub=adv['boot_scrub_internal'],
            adv_periodic_notifyuser=user,
        ))

    def _save(self, *args, **kwargs):
        from freenasUI.account.models import bsdUsers
        from freenasUI.middleware.connector import connection as dispatcher
        try:
            userid = bsdUsers.objects.get(
                bsdusr_username=self.adv_periodic_notifyuser).id
        except bsdUsers.DoesNotExist:
            userid = 0
        user = dispatcher.call_sync('users.query', [('id', '=', 0)], {'single': True})
        user_attrs = user['attributes']
        new_user_attrs = user_attrs.copy()
        new_user_attrs.update({
            'gui_messages_footer': self.adv_consolemsg,
            'gui_traceback': self.adv_traceback,
            'gui_advancedmode': self.adv_advancedmode,
        })
        if user_attrs != new_user_attrs:
            self._save_task_call('users.update', user['id'], {'attributes': new_user_attrs})

        data = {
            'console_cli': self.adv_consolemenu,
            'serial_console': self.adv_serialconsole,
            'serial_port': self.adv_serialport,
            'serial_speed': int(self.adv_serialspeed),
            'console_screensaver': self.adv_consolescreensaver,
            'powerd': self.adv_powerdaemon,
            'swapondrive': self.adv_swapondrive,
            'autotune': self.adv_autotune,
            'debugkernel': self.adv_debugkernel,
            'uploadcrash': self.adv_uploadcrash,
            'motd': self.adv_motd,
            'boot_scrub_internal': self.adv_boot_scrub,
            'periodic_notify_user': userid,
        }
        self._save_task_call('system.advanced.configure', data)
        return True


class Email(NewModel):
    em_fromemail = models.CharField(
            max_length=120,
            verbose_name=_("From email"),
            help_text=_("An email address that the system will use for the "
                "sending address for mail it sends, eg: freenas@example.com"),
            default='',
            )
    em_outgoingserver = models.CharField(
            max_length=120,
            verbose_name=_("Outgoing mail server"),
            help_text=_("A hostname or ip that will accept our mail, for "
                "instance mail.example.org, or 192.168.1.1"),
            blank=True
            )
    em_port = models.IntegerField(
            default=25,
            validators=[MinValueValidator(1), MaxValueValidator(65535)],
            help_text=_("An integer from 1 - 65535, generally will be 25, "
                "465, or 587"),
            verbose_name=_("Port to connect to")
            )
    em_security = models.CharField(
            max_length=120,
            choices=choices.SMTPAUTH_CHOICES,
            default="plain",
            help_text=_("encryption of the connection"),
            verbose_name=_("TLS/SSL")
            )
    em_smtp = models.BooleanField(
            verbose_name=_("Use SMTP Authentication"),
            default=False
            )
    em_user = models.CharField(
            blank=True,
            null=True,
            max_length=120,
            verbose_name=_("Username"),
            help_text=_("A username to authenticate to the remote server"),
            )
    em_pass = models.CharField(
            blank=True,
            null=True,
            max_length=120,
            verbose_name=_("Password"),
            help_text=_("A password to authenticate to the remote server"),
            )

    objects = NewManager(qs_class=ConfigQuerySet)

    class Meta:
        verbose_name = _("Email")

    class FreeAdmin:
        deletable = False

    class Middleware:
        configstore = True

    @classmethod
    def _load(cls):
        from freenasUI.middleware.connector import connection as dispatcher
        mail = dispatcher.call_sync('mail.get_config')
        security = 'plain'
        if mail['encryption'] in ('PLAIN', 'SSL', 'TLS'):
            security = mail['encryption'].lower()
        return cls(**dict(
            id=1,
            em_fromemail=mail['from'],
            em_outgoingserver=mail['server'],
            em_port=mail['port'],
            em_smtp=mail['auth'],
            em_security=security,
            em_user=mail['user'],
            em_pass=mail['pass'],
        ))

    def _save(self, *args, **kwargs):
        encryption = 'PLAIN'
        if self.em_security in ('plain', 'tls', 'ssl'):
            encryption = self.em_security.upper()
        data = {
            'from': self.em_fromemail,
            'server': self.em_outgoingserver,
            'port': self.em_port,
            'auth': self.em_smtp,
            'encryption': encryption,
            'user': self.em_user,
            'pass': self.em_pass,
        }
        self._save_task_call('mail.configure', data)
        return True


class Tunable(NewModel):
    id = models.CharField(editable=False, max_length=120, primary_key=True)
    tun_var = models.CharField(
            max_length=50,
            unique=True,
            verbose_name=_("Variable"),
            )
    tun_value = models.CharField(
            max_length=50,
            verbose_name=_("Value"),
            )
    tun_type = models.CharField(
        verbose_name=_('Type'),
        max_length=20,
        choices=choices.TUNABLE_TYPES,
        default='LOADER',
    )
    tun_comment = models.CharField(
            max_length=100,
            verbose_name=_("Comment"),
            blank=True,
            )
    tun_enabled = models.BooleanField(
            default=True,
            verbose_name=_("Enabled"),
            )

    def __unicode__(self):
        return unicode(self.tun_var)

    def delete(self):
        super(Tunable, self).delete()
        try:
            if self.instance.tun_type == 'RC':
                os.unlink('/var/tmp/freenas_config.md5')
                notifier()._system("sh /etc/rc.conf.local")
        except:
            pass

    class Meta:
        verbose_name = _("Tunable")
        verbose_name_plural = _("Tunables")
        ordering = ["tun_var"]

    class FreeAdmin:
        icon_model = u"TunableIcon"
        icon_object = u"TunableIcon"
        icon_add = u"AddTunableIcon"
        icon_view = u"ViewTunableIcon"

    class Middleware:
        field_mapping = (
            ('id', 'id'),
            ('tun_var', 'var'),
            ('tun_value', 'value'),
            ('tun_type', 'type'),
            ('tun_comment', 'comment'),
            ('tun_enabled', 'enabled'),
        )
        provider_name = 'tunables'


class Registration(Model):
    reg_firstname = models.CharField(
            max_length=120,
            verbose_name=_("First Name")
            )
    reg_lastname = models.CharField(
            max_length=120,
            verbose_name=_("Last Name")
            )
    reg_company = models.CharField(
            max_length=120,
            verbose_name=_("Company"),
            blank=True,
            null=True
            )
    reg_address = models.CharField(
            max_length=120,
            verbose_name=_("Address"),
            blank=True,
            null=True
            )
    reg_city = models.CharField(
            max_length=120,
            verbose_name=_("City"),
            blank=True,
            null=True
            )
    reg_state = models.CharField(
            max_length=120,
            verbose_name=_("State"),
            blank=True,
            null=True
            )
    reg_zip = models.CharField(
            max_length=120,
            verbose_name=_("Zip"),
            blank=True,
            null=True
            )
    reg_email = models.CharField(
            max_length=120,
            verbose_name=_("Email")
            )
    reg_homephone = models.CharField(
            max_length=120,
            verbose_name=_("Home Phone"),
            blank=True,
            null=True
            )
    reg_cellphone = models.CharField(
            max_length=120,
            verbose_name=_("Cell Phone"),
            blank=True,
            null=True
            )
    reg_workphone = models.CharField(
            max_length=120,
            verbose_name=_("Work Phone"),
            blank=True,
            null=True
            )

    class Meta:
        verbose_name = _("Registration")

    class FreeAdmin:
        deletable = False


class Update(NewModel):
    upd_autocheck = models.BooleanField(
        verbose_name=_('Check Automatically For Updates'),
        default=True,
    )
    upd_train = models.CharField(
        max_length=50,
        blank=True,
    )
    upd_server = models.CharField(
        max_length=255,
        blank=True,
    )

    objects = NewManager(qs_class=ConfigQuerySet)

    class Meta:
        verbose_name = _('Update')

    class Middleware:
        configstore = True

    @classmethod
    def _load(cls):
        from freenasUI.middleware.connector import connection as dispatcher
        config = dispatcher.call_sync('update.get_config')
        return cls(**dict(
            upd_autocheck=config['check_auto'],
            upd_train=config['train'],
            upd_server=config['update_server'],
        ))

    def _save(self, *args, **kwargs):
        self._save_task_call('update.configure', {
            'check_auto': self.upd_autocheck,
            'train': self.upd_train,
        })
        return True


class CertificateMiddleware:
    field_mapping = (
        ('id', 'id'),
        ('cert_type', 'type'),
        ('cert_name', 'name'),
        ('cert_certificate', 'certificate'),
        ('cert_privatekey', 'privatekey'),
        ('cert_CSR', 'csr'),
        ('cert_key_length', 'key_length'),
        ('cert_digest_algorithm', 'digest_algorithm'),
        ('cert_lifetime', 'lifetime'),
        ('cert_country', 'country'),
        ('cert_state', 'state'),
        ('cert_city', 'city'),
        ('cert_organization', 'organization'),
        ('cert_email', 'email'),
        ('cert_common', 'common'),
        ('cert_serial', 'serial'),
        ('cert_signedby', 'signedby'),
        ('cert_DN', 'dn'),
        ('cert_valid_from', 'valid_from'),
        ('cert_valid_until', 'valid_until'),
        ('cert_certificate_path', 'certificate_path'),
        ('cert_privatekey_path', 'privatekey_path'),
        ('cert_CSR_path', 'csr_path'),
    )
    provider_name = 'crypto.certificates'


class CertificateBase(NewModel):

    id = models.CharField(editable=False, max_length=120, primary_key=True)
    cert_type = models.CharField(max_length=64)
    cert_name = models.CharField(
            max_length=120,
            verbose_name=_("Name"),
            help_text=_("Descriptive Name"),
            unique=True
            )
    cert_certificate = models.TextField(
            blank=True,
            null=True,
            verbose_name=_("Certificate"),
            help_text=_("Cut and paste the contents of your certificate here")
            )
    cert_privatekey = models.TextField(
            blank=True,
            null=True,
            verbose_name=_("Private Key"),
            help_text=_("Cut and paste the contents of your private key here")
            )
    cert_CSR = models.TextField(
            blank=True,
            null=True,
            verbose_name=_("Signing Request"),
            help_text=_("Cut and paste the contents of your CSR here")
            )
    cert_key_length = models.IntegerField(
            blank=True,
            null=True,
            verbose_name=_("Key length"),
            default=2048
            )
    cert_digest_algorithm = models.CharField(
            blank=True,
            null=True,
            max_length=120,
            verbose_name=_("Digest Algorithm"),
            default='SHA256'
            )
    cert_lifetime = models.IntegerField(
            blank=True,
            null=True,
            verbose_name=_("Lifetime"),
            default=3650
            )
    cert_country = models.CharField(
            blank=True,
            null=True,
            max_length=120,
            verbose_name=_("Country"),
            help_text=_("Country Name (2 letter code)")
            )
    cert_state = models.CharField(
            blank=True,
            null=True,
            max_length=120,
            verbose_name=_("State"),
            help_text=_("State or Province Name (full name)")
            )
    cert_city = models.CharField(
            blank=True,
            null=True,
            max_length=120,
            verbose_name=_("Locality"),
            help_text=_("Locality Name (eg, city)")
            )
    cert_organization = models.CharField(
            blank=True,
            null=True,
            max_length=120,
            verbose_name=_("Organization"),
            help_text=_("Organization Name (eg, company)")
            )
    cert_email = models.CharField(
            blank=True,
            null=True,
            max_length=120,
            verbose_name=_("Email Address"),
            help_text=_("Email Address")
            )
    cert_common = models.CharField(
            blank=True,
            null=True,
            max_length=120,
            verbose_name=_("Common Name"),
            help_text=_("Common Name (eg, FQDN of FreeNAS server or service)")
            )
    cert_serial = models.IntegerField(
            blank=True,
            null=True,
            max_length=120,
            verbose_name=_("Serial"),
            help_text=_("Serial for next certificate")
            )
    cert_signedby = models.ForeignKey(
            "CertificateAuthority",
            blank=True,
            null=True,
            verbose_name=_("Signing Certificate Authority")
            )
    cert_DN = models.CharField(
        max_length=2000,
        blank=True,
        editable=False,
    )
    cert_valid_from = models.CharField(
        max_length=200,
        blank=True,
        editable=False,
    )
    cert_valid_until = models.CharField(
        max_length=200,
        blank=True,
        editable=False,
    )
    cert_certificate_path = models.CharField(
        max_length=200,
        blank=True,
        editable=False,
    )
    cert_privatekey_path = models.CharField(
        max_length=200,
        blank=True,
        editable=False,
    )
    cert_CSR_path = models.CharField(
        max_length=200,
        blank=True,
        editable=False,
    )

    def get_certificate_path(self):
        return self.cert_certificate_path

    def get_privatekey_path(self):
        return self.cert_privatekey_path

    def __unicode__(self):
        return self.cert_name

    @property
    def cert_internal(self):
        internal = "YES"

        if self.cert_type == 'CA_EXISTING':
            internal = "NO"
        elif self.cert_type == 'CERT_EXISTING':
            internal = "NO"

        return internal

    @property
    def cert_issuer(self):
        issuer = None

        if self.cert_type in ('CA_EXISTING', 'CA_INTERMEDIATE',
            'CERT_EXISTING'):
            issuer = "external"
        elif self.cert_type == 'CA_INTERNAL':
            issuer = "self-signed"
        elif self.cert_type == 'CERT_INTERNAL':
            issuer = self.cert_signedby
        elif self.cert_type == 'CERT_CSR':
            issuer = "external - signature pending"

        return issuer

    @property
    def cert_ncertificates(self):
        count = 0
        certs = Certificate.objects.all()
        for cert in certs:
            try:
                if self.cert_name == cert.cert_signedby.cert_name:
                    count += 1
            except:
                pass
        return count

    #
    # Returns ASN1 GeneralizedTime - Need to parse it...
    #
    @property
    def cert_from(self):
        try:
            before = self.cert_valid_from
            t1 = dtparser.parse(before)
            t2 = t1.astimezone(dateutil.tz.tzutc())
            before = t2.ctime()
        except Exception as e:
            before = None

        return before

    #
    # Returns ASN1 GeneralizedTime - Need to parse it...
    #
    @property
    def cert_until(self):
        try:
            after = self.cert_valid_until
            t1 = dtparser.parse(after)
            t2 = t1.astimezone(dateutil.tz.tzutc())
            after = t2.ctime()
        except Exception as e:
            after = None

        return after

    @property
    def cert_type_existing(self):
        ret = False
        if self.cert_type == 'CERT_EXISTING':
            ret = True
        return ret

    @property
    def cert_type_internal(self):
        ret = False
        if self.cert_type == 'CERT_INTERNAL':
            ret = True
        return ret

    @property
    def cert_type_CSR(self):
        ret = False
        if self.cert_type == 'CERT_CSR':
            ret = True
        return ret

    @property
    def CA_type_existing(self):
        ret = False
        if self.cert_type == 'CA_EXISTING':
            ret = True
        return ret

    @property
    def CA_type_internal(self):
        ret = False
        if self.cert_type == 'CA_INTERNAL':
            ret = True
        return ret

    @property
    def CA_type_intermediate(self):
        ret = False
        if self.cert_type == 'CA_INTERMEDIATE':
            ret = True
        return ret

    class Meta:
        abstract = True


class CertificateAuthority(CertificateBase):

    def delete(self):
        temp_cert_name = self.cert_name
        super(CertificateAuthority, self).delete()
        # If this was a malformed CA then delete its alert sentinel file
        try:
            os.unlink('/tmp/alert_invalidCA_{0}'.format(temp_cert_name))
            try:
                with open("/var/run/alertd.pid", "r") as f:
                    alertd_pid = int(f.read())
                os.kill(alertd_pid, signal.SIGUSR1)
            except:
                # alertd not running?
                pass
        except OSError:
            # It was not a malformed CA after all!
            pass

    class Meta:
        verbose_name = _("CA")

    class Middleware(CertificateMiddleware):
        default_filters = [
            ('type', 'in', ['CA_EXISTING', 'CA_INTERMEDIATE', 'CA_INTERNAL']),
        ]


class Certificate(CertificateBase):

    def delete(self):
        temp_cert_name = self.cert_name
        super(Certificate, self).delete()
        # If this was a malformed CA then delete its alert sentinel file
        try:
            os.unlink('/tmp/alert_invalidcert_{0}'.format(temp_cert_name))
            try:
                with open("/var/run/alertd.pid", "r") as f:
                    alertd_pid = int(f.read())
                os.kill(alertd_pid, signal.SIGUSR1)
            except:
                # alertd not running?
                pass
        except OSError:
            # It was not a malformed CA after all!
            pass

    class Meta:
        verbose_name = _("Certificate")

    class Middleware(CertificateMiddleware):
        default_filters = [
            ('type', 'nin', ['CA_EXISTING', 'CA_INTERMEDIATE', 'CA_INTERNAL']),
        ]


class Backup(Model):
    bak_finished = models.BooleanField(
        default=False,
        verbose_name=_("Finished")
    )

    bak_failed = models.BooleanField(
        default=False,
        verbose_name=_("Failed")
    )

    bak_acknowledged = models.BooleanField(
        default=False,
        verbose_name=_("Acknowledged")
    )

    bak_worker_pid = models.IntegerField(
        verbose_name=_("Backup worker PID"),
        null=True
    )

    bak_started_at = models.DateTimeField(
        verbose_name=_("Started at")
    )

    bak_finished_at = models.DateTimeField(
        verbose_name=_("Finished at"),
        null=True
    )

    bak_destination = models.CharField(
        max_length=1024,
        blank=True,
        verbose_name=_("Destination")
    )

    bak_status = models.CharField(
        max_length=1024,
        blank=True,
        verbose_name=_("Status")
    )

    class FreeAdmin:
        deletable = False

    class Meta:
        verbose_name = _("System Backup")

