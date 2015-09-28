#!/usr/bin/env python
#
# Electrum - lightweight Bitcoin client
# Copyright (C) 2012 thomasv@gitorious
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.

import sys, time, threading
import os.path, json, traceback
import shutil
import socket
import webbrowser
import csv
from decimal import Decimal
import base64

import PyQt4
from PyQt4.QtGui import *
from PyQt4.QtCore import *
import PyQt4.QtCore as QtCore

import icons_rc

from electrum.bitcoin import MIN_RELAY_TX_FEE, COIN, is_valid
from electrum.plugins import run_hook
from electrum.i18n import _
from electrum.util import block_explorer, block_explorer_info, block_explorer_URL
from electrum.util import print_error, print_msg
from electrum.util import format_satoshis, format_satoshis_plain, format_time, NotEnoughFunds, StoreDict
from electrum import Transaction
from electrum import mnemonic
from electrum import util, bitcoin, commands, Wallet
from electrum import SimpleConfig, Wallet, WalletStorage
from electrum import Imported_Wallet
from electrum import paymentrequest
from electrum.contacts import Contacts

from amountedit import AmountEdit, BTCAmountEdit, MyLineEdit, BTCkBEdit
from network_dialog import NetworkDialog
from qrcodewidget import QRCodeWidget, QRDialog
from qrtextedit import ScanQRTextEdit, ShowQRTextEdit
from transaction_dialog import show_transaction





from electrum import ELECTRUM_VERSION
import re

from util import *


class StatusBarButton(QPushButton):
    def __init__(self, icon, tooltip, func):
        QPushButton.__init__(self, icon, '')
        self.setToolTip(tooltip)
        self.setFlat(True)
        self.setMaximumWidth(25)
        self.clicked.connect(self.onPress)
        self.func = func
        self.setIconSize(QSize(25,25))

    def onPress(self, checked=False):
        '''Drops the unwanted PyQt4 "checked" argument'''
        self.func()

    def keyPressEvent(self, e):
        if e.key() == QtCore.Qt.Key_Return:
            self.func()


from electrum.paymentrequest import PR_UNPAID, PR_PAID, PR_UNKNOWN, PR_EXPIRED
from electrum.paymentrequest import PaymentRequest, InvoiceStore, get_payment_request

pr_icons = {
    PR_UNPAID:":icons/unpaid.png",
    PR_PAID:":icons/confirmed.png",
    PR_EXPIRED:":icons/expired.png"
}

pr_tooltips = {
    PR_UNPAID:_('Pending'),
    PR_PAID:_('Paid'),
    PR_EXPIRED:_('Expired')
}

expiration_values = [
    (_('1 hour'), 60*60),
    (_('1 day'), 24*64*64),
    (_('1 week'), 7*24*60*60),
    (_('Never'), None)
]



class ElectrumWindow(QMainWindow):
    labelsChanged = pyqtSignal()

    def __init__(self, config, network, gui_object):
        QMainWindow.__init__(self)

        self.config = config
        self.network = network
        self.wallet = None

        self.gui_object = gui_object
        self.tray = gui_object.tray
        self.go_lite = gui_object.go_lite
        self.lite = None
        self.app = gui_object.app

        self.invoices = InvoiceStore(self.config)
        self.contacts = Contacts(self.config)

        self.create_status_bar()
        self.need_update = threading.Event()

        self.decimal_point = config.get('decimal_point', 5)
        self.num_zeros     = int(config.get('num_zeros',0))

        self.completions = QStringListModel()

        self.tabs = tabs = QTabWidget(self)
        tabs.addTab(self.create_history_tab(), _('History') )
        tabs.addTab(self.create_send_tab(), _('Send') )
        tabs.addTab(self.create_receive_tab(), _('Receive') )
        tabs.addTab(self.create_addresses_tab(), _('Addresses') )
        tabs.addTab(self.create_contacts_tab(), _('Contacts') )
        tabs.addTab(self.create_console_tab(), _('Console') )
        tabs.setMinimumSize(600, 400)
        tabs.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setCentralWidget(tabs)

        try:
            self.setGeometry(*self.config.get("winpos-qt"))
        except:
            self.setGeometry(100, 100, 840, 400)

        if self.config.get("is_maximized"):
            self.showMaximized()

        self.setWindowIcon(QIcon(":icons/electrum.png"))
        self.init_menubar()

        QShortcut(QKeySequence("Ctrl+W"), self, self.close)
        QShortcut(QKeySequence("Ctrl+Q"), self, self.close)
        QShortcut(QKeySequence("Ctrl+R"), self, self.update_wallet)
        QShortcut(QKeySequence("Ctrl+PgUp"), self, lambda: tabs.setCurrentIndex( (tabs.currentIndex() - 1 )%tabs.count() ))
        QShortcut(QKeySequence("Ctrl+PgDown"), self, lambda: tabs.setCurrentIndex( (tabs.currentIndex() + 1 )%tabs.count() ))

        for i in range(tabs.count()):
            QShortcut(QKeySequence("Alt+" + str(i + 1)), self, lambda i=i: tabs.setCurrentIndex(i))

        self.connect(self, QtCore.SIGNAL('payment_request_ok'), self.payment_request_ok)
        self.connect(self, QtCore.SIGNAL('payment_request_error'), self.payment_request_error)
        self.labelsChanged.connect(self.update_tabs)
        self.history_list.setFocus(True)

        # network callbacks
        if self.network:
            self.network.register_callback('updated', lambda: self.need_update.set())
            self.network.register_callback('new_transaction', self.new_transaction)
            self.register_callback('status', self.update_status)
            self.register_callback('close', self.close)
            self.register_callback('banner', self.console.showMessage)
            self.register_callback('verified', self.history_list.update_item)

            # set initial message
            self.console.showMessage(self.network.banner)

        self.payment_request = None
        self.qr_window = None
        self.not_enough_funds = False
        self.pluginsdialog = None
        self.fetch_alias()
        self.require_fee_update = False
        self.tx_notifications = []


    def register_callback(self, name, method):
        """ run callback in the qt thread """
        self.connect(self, QtCore.SIGNAL(name), method)
        self.network.register_callback(name, lambda *params: self.emit(QtCore.SIGNAL(name), *params))


    def fetch_alias(self):
        self.alias_info = None
        alias = self.config.get('alias')
        if alias:
            alias = str(alias)
            def f():
                self.alias_info = self.contacts.resolve_openalias(alias)
                self.emit(SIGNAL('alias_received'))
            t = threading.Thread(target=f)
            t.setDaemon(True)
            t.start()

    def update_account_selector(self):
        # account selector
        accounts = self.wallet.get_account_names()
        self.account_selector.clear()
        if len(accounts) > 1:
            self.account_selector.addItems([_("All accounts")] + accounts.values())
            self.account_selector.setCurrentIndex(0)
            self.account_selector.show()
        else:
            self.account_selector.hide()

    def close_wallet(self):
        self.wallet.stop_threads()
        run_hook('close_wallet')

    def load_wallet(self, wallet):
        import electrum
        self.wallet = wallet
        # backward compatibility
        self.update_wallet_format()
        self.import_old_contacts()
        # address used to create a dummy transaction and estimate transaction fee
        a = self.wallet.addresses(False)
        self.dummy_address = a[0] if a else None
        self.accounts_expanded = self.wallet.storage.get('accounts_expanded',{})
        self.current_account = self.wallet.storage.get("current_account", None)
        title = 'Myriadcoin Electrum %s  -  %s' % (self.wallet.electrum_version, self.wallet.basename())
        if self.wallet.is_watching_only():
            title += ' [%s]' % (_('watching only'))
        self.setWindowTitle( title )
        self.update_history_tab()
        self.need_update.set()
        # Once GUI has been initialized check if we want to announce something since the callback has been called before the GUI was initialized
        self.notify_transactions()
        self.update_account_selector()
        # update menus
        self.new_account_menu.setVisible(self.wallet.can_create_accounts())
        self.private_keys_menu.setEnabled(not self.wallet.is_watching_only())
        self.password_menu.setEnabled(self.wallet.can_change_password())
        self.seed_menu.setEnabled(self.wallet.has_seed())
        self.mpk_menu.setEnabled(self.wallet.is_deterministic())
        self.import_menu.setVisible(self.wallet.can_import())
        self.export_menu.setEnabled(self.wallet.can_export())
        self.update_lock_icon()
        self.update_buttons_on_seed()
        self.update_console()
        self.clear_receive_tab()
        self.update_receive_tab()
        self.show()
        if self.wallet.is_watching_only():
            msg = ' '.join([
                _("This wallet is watching-only."),
                _("This means you will not be able to spend Myriadcoin with it."),
                _("Make sure you own the seed phrase or the private keys, before you request Myriadcoin to be sent to this wallet.")
            ])
            QMessageBox.warning(self, _('Information'), msg, _('OK'))
        run_hook('load_wallet', wallet, self)

    def import_old_contacts(self):
        # backward compatibility: import contacts
        addressbook = set(self.wallet.storage.get('contacts', []))
        for k in addressbook:
            l = self.wallet.labels.get(k)
            if bitcoin.is_address(k) and l:
                self.contacts[l] = ('address', k)
        self.wallet.storage.put('contacts', None)

    def update_wallet_format(self):
        # convert old-format imported keys
        if self.wallet.imported_keys:
            password = self.password_dialog(_("Please enter your password in order to update imported keys")) if self.wallet.use_encryption else None
            try:
                self.wallet.convert_imported_keys(password)
            except Exception as e:
                traceback.print_exc(file=sys.stdout)
                self.show_message(str(e))
        # call synchronize to regenerate addresses in case we are offline
        if self.wallet.get_master_public_keys() and self.wallet.addresses() == []:
            self.wallet.synchronize()

    def open_wallet(self):
        wallet_folder = self.wallet.storage.path
        filename = unicode(QFileDialog.getOpenFileName(self, "Select your wallet file", wallet_folder))
        if not filename:
            return
        self.load_wallet_file(filename)

    def load_wallet_file(self, filename):
        try:
            storage = WalletStorage(filename)
        except Exception as e:
            self.show_message(str(e))
            return
        if not storage.file_exists:
            self.show_message(_("File not found") + ' ' + filename)
            recent = self.config.get('recently_open', [])
            if filename in recent:
                recent.remove(filename)
                self.config.set_key('recently_open', recent)
            return
        # read wizard action
        try:
            wallet = Wallet(storage)
        except BaseException as e:
            traceback.print_exc(file=sys.stdout)
            QMessageBox.warning(None, _('Warning'), str(e), _('OK'))
            return
        action = wallet.get_action()
        # run wizard
        if action is not None:
            self.hide()
            wallet = self.gui_object.run_wizard(storage, action)
            # keep current wallet
            if not wallet:
                self.show()
                return
        else:
            wallet.start_threads(self.network)
        # close current wallet
        self.close_wallet()
        # load new wallet in gui
        self.load_wallet(wallet)
        # save path
        if self.config.get('wallet_path') is None:
            self.config.set_key('gui_last_wallet', filename)
        # add to recently visited
        self.update_recently_visited(filename)

    def backup_wallet(self):
        path = self.wallet.storage.path
        wallet_folder = os.path.dirname(path)
        filename = unicode( QFileDialog.getSaveFileName(self, _('Enter a filename for the copy of your wallet'), wallet_folder) )
        if not filename:
            return

        new_path = os.path.join(wallet_folder, filename)
        if new_path != path:
            try:
                shutil.copy2(path, new_path)
                QMessageBox.information(None,"Wallet backup created", _("A copy of your wallet file was created in")+" '%s'" % str(new_path))
            except (IOError, os.error), reason:
                QMessageBox.critical(None,"Unable to create backup", _("Myriadcoin Electrum was unable to copy your wallet file to the specified location.")+"\n" + str(reason))


    def new_wallet(self):
        import installwizard
        wallet_folder = os.path.dirname(os.path.abspath(self.wallet.storage.path))
        i = 1
        while True:
            filename = "wallet_%d"%i
            if filename in os.listdir(wallet_folder):
                i += 1
            else:
                break
        filename = line_dialog(self, _('New Wallet'), _('Enter file name') + ':', _('OK'), filename)
        if not filename:
            return
        full_path = os.path.join(wallet_folder, filename)
        storage = WalletStorage(full_path)
        if storage.file_exists:
            QMessageBox.critical(None, "Error", _("File exists"))
            return
        self.hide()
        wizard = installwizard.InstallWizard(self.config, self.network, storage, self.app)
        action, wallet_type = wizard.restore_or_create()
        if not action:
            self.show()
            return
        # close current wallet, but keep a reference to it
        self.close_wallet()
        wallet = wizard.run(action, wallet_type)
        if wallet:
            self.load_wallet(wallet)
        else:
            self.wallet.start_threads(self.network)
            self.load_wallet(self.wallet)
        self.show()

    def update_recently_visited(self, filename=None):
        recent = self.config.get('recently_open', [])
        if filename:
            if filename in recent:
                recent.remove(filename)
            recent.insert(0, filename)
            recent = recent[:5]
            self.config.set_key('recently_open', recent)
        self.recently_visited_menu.clear()
        for i, k in enumerate(sorted(recent)):
            b = os.path.basename(k)
            def loader(k):
                return lambda: self.load_wallet_file(k)
            self.recently_visited_menu.addAction(b, loader(k)).setShortcut(QKeySequence("Ctrl+%d"%(i+1)))
        self.recently_visited_menu.setEnabled(len(recent))

    def init_menubar(self):
        menubar = QMenuBar()

        file_menu = menubar.addMenu(_("&File"))
        self.recently_visited_menu = file_menu.addMenu(_("&Recently open"))
        file_menu.addAction(_("&Open"), self.open_wallet).setShortcut(QKeySequence.Open)
        file_menu.addAction(_("&New/Restore"), self.new_wallet).setShortcut(QKeySequence.New)
        file_menu.addAction(_("&Save Copy"), self.backup_wallet).setShortcut(QKeySequence.SaveAs)
        file_menu.addSeparator()
        file_menu.addAction(_("&Quit"), self.close)
        self.update_recently_visited()

        wallet_menu = menubar.addMenu(_("&Wallet"))
        wallet_menu.addAction(_("&New contact"), self.new_contact_dialog)
        self.new_account_menu = wallet_menu.addAction(_("&New account"), self.new_account_dialog)

        wallet_menu.addSeparator()

        self.password_menu = wallet_menu.addAction(_("&Password"), self.change_password_dialog)
        self.seed_menu = wallet_menu.addAction(_("&Seed"), self.show_seed_dialog)
        self.mpk_menu = wallet_menu.addAction(_("&Master Public Keys"), self.show_master_public_keys)

        wallet_menu.addSeparator()
        labels_menu = wallet_menu.addMenu(_("&Labels"))
        labels_menu.addAction(_("&Import"), self.do_import_labels)
        labels_menu.addAction(_("&Export"), self.do_export_labels)

        self.private_keys_menu = wallet_menu.addMenu(_("&Private keys"))
        self.private_keys_menu.addAction(_("&Sweep"), self.sweep_key_dialog)
        self.import_menu = self.private_keys_menu.addAction(_("&Import"), self.do_import_privkey)
        self.export_menu = self.private_keys_menu.addAction(_("&Export"), self.export_privkeys_dialog)
        wallet_menu.addAction(_("&Export History"), self.export_history_dialog)
        wallet_menu.addAction(_("Search"), self.toggle_search).setShortcut(QKeySequence("Ctrl+S"))

        tools_menu = menubar.addMenu(_("&Tools"))

        # Settings / Preferences are all reserved keywords in OSX using this as work around
        tools_menu.addAction(_("Myriadcoin Electrum preferences") if sys.platform == 'darwin' else _("Preferences"), self.settings_dialog)
        tools_menu.addAction(_("&Network"), self.run_network_dialog)
        tools_menu.addAction(_("&Plugins"), self.plugins_dialog)
        tools_menu.addSeparator()
        tools_menu.addAction(_("&Sign/verify message"), self.sign_verify_message)
        tools_menu.addAction(_("&Encrypt/decrypt message"), self.encrypt_message)
        tools_menu.addSeparator()

        paytomany_menu = tools_menu.addAction(_("&Pay to many"), self.paytomany)

        raw_transaction_menu = tools_menu.addMenu(_("&Load transaction"))
        raw_transaction_menu.addAction(_("&From file"), self.do_process_from_file)
        raw_transaction_menu.addAction(_("&From text"), self.do_process_from_text)
        raw_transaction_menu.addAction(_("&From the blockchain"), self.do_process_from_txid)
        raw_transaction_menu.addAction(_("&From QR code"), self.read_tx_from_qrcode)
        self.raw_transaction_menu = raw_transaction_menu

        help_menu = menubar.addMenu(_("&Help"))
        help_menu.addAction(_("&About"), self.show_about)
        help_menu.addAction(_("&Official website"), lambda: webbrowser.open("http://electrum.org"))
        help_menu.addSeparator()
        help_menu.addAction(_("&Documentation"), lambda: webbrowser.open("http://electrum.orain.org/")).setShortcut(QKeySequence.HelpContents)
        help_menu.addAction(_("&Report Bug"), self.show_report_bug)

        self.setMenuBar(menubar)

    def show_about(self):
        QMessageBox.about(self, "Myriadcoin Electrum",
            _("Version")+" %s" % (self.wallet.electrum_version) + "\n\n" + _("Myriadcoin Electrum's focus is speed, with low resource usage and simplifying Myriadcoin. You do not need to perform regular backups, because your wallet can be recovered from a secret phrase that you can memorize or write on paper. Startup times are instant because it operates in conjunction with high-performance servers that handle the most complicated parts of the Myriadcoin system.") + "\n\n" + _("Thank you to the Bitcoin Electrum team at https://electrum.org for their continued development of Electrum for Bitcoin.") + "\n\n" + _("Myriadcoin Electrum has been altered from Bitcoin Electrum by cryptapus. Donation are welcome (MYR): MCrypTZRTRk8RGjSt3MZ3atSEwSLPicePR"))

    def show_report_bug(self):
        QMessageBox.information(self, "Myriadcoin Electrum - " + _("Reporting Bugs"),
            _("Please report any bugs as issues on github:")+" <a href=\"https://github.com/spesmilo/electrum/issues\">https://github.com/spesmilo/electrum/issues</a>")


    def new_transaction(self, tx):
        self.tx_notifications.append(tx)

    def notify_transactions(self):
        if not self.network or not self.network.is_connected():
            return
        print_error("Notifying GUI")
        if len(self.tx_notifications) > 0:
            # Combine the transactions if there are more then three
            tx_amount = len(self.tx_notifications)
            if(tx_amount >= 3):
                total_amount = 0
                for tx in self.tx_notifications:
                    is_relevant, is_mine, v, fee = self.wallet.get_wallet_delta(tx)
                    if(v > 0):
                        total_amount += v
                self.notify(_("%(txs)s new transactions received. Total amount received in the new transactions %(amount)s %(unit)s") \
                            % { 'txs' : tx_amount, 'amount' : self.format_amount(total_amount), 'unit' : self.base_unit()})
                self.tx_notifications = []
            else:
              for tx in self.tx_notifications:
                  if tx:
                      self.tx_notifications.remove(tx)
                      is_relevant, is_mine, v, fee = self.wallet.get_wallet_delta(tx)
                      if(v > 0):
                          self.notify(_("New transaction received. %(amount)s %(unit)s") % { 'amount' : self.format_amount(v), 'unit' : self.base_unit()})

    def notify(self, message):
        if self.tray:
            self.tray.showMessage("Myriadcoin Electrum", message, QSystemTrayIcon.Information, 20000)



    # custom wrappers for getOpenFileName and getSaveFileName, that remember the path selected by the user
    def getOpenFileName(self, title, filter = ""):
        directory = self.config.get('io_dir', unicode(os.path.expanduser('~')))
        fileName = unicode( QFileDialog.getOpenFileName(self, title, directory, filter) )
        if fileName and directory != os.path.dirname(fileName):
            self.config.set_key('io_dir', os.path.dirname(fileName), True)
        return fileName

    def getSaveFileName(self, title, filename, filter = ""):
        directory = self.config.get('io_dir', unicode(os.path.expanduser('~')))
        path = os.path.join( directory, filename )
        fileName = unicode( QFileDialog.getSaveFileName(self, title, path, filter) )
        if fileName and directory != os.path.dirname(fileName):
            self.config.set_key('io_dir', os.path.dirname(fileName), True)
        return fileName

    def close(self):
        if self.qr_window:
            self.qr_window.close()
        QMainWindow.close(self)
        run_hook('close_main_window')

    def connect_slots(self, sender):
        self.connect(sender, QtCore.SIGNAL('timersignal'), self.timer_actions)

    def timer_actions(self):
        if self.need_update.is_set():
            self.update_wallet()
            self.need_update.clear()
        # resolve aliases
        self.payto_e.resolve()
        # update fee
        if self.require_fee_update:
            self.do_update_fee()
            self.require_fee_update = False
        run_hook('timer_actions')

    def format_amount(self, x, is_diff=False, whitespaces=False):
        return format_satoshis(x, is_diff, self.num_zeros, self.decimal_point, whitespaces)

    def get_decimal_point(self):
        return self.decimal_point

    def base_unit(self):
        assert self.decimal_point in [2, 5, 8]
        if self.decimal_point == 2:
            return 'uMYR'
        if self.decimal_point == 5:
            return 'mMYR'
        if self.decimal_point == 8:
            return 'MYR'
        raise Exception('Unknown base unit')

    def update_status(self):
        if not self.wallet:
            return

        if self.network is None or not self.network.is_running():
            text = _("Offline")
            icon = QIcon(":icons/status_disconnected.png")

        elif self.network.is_connected():
            server_height = self.network.get_server_height()
            server_lag = self.network.get_local_height() - server_height
            # Server height can be 0 after switching to a new server
            # until we get a headers subscription request response.
            # Display the synchronizing message in that case.
            if not self.wallet.up_to_date or server_height == 0:
                text = _("Synchronizing...")
                icon = QIcon(":icons/status_waiting.png")
            elif server_lag > 1:
                text = _("Server is lagging (%d blocks)"%server_lag)
                icon = QIcon(":icons/status_lagging.png")
            else:
                c, u, x = self.wallet.get_account_balance(self.current_account)
                text =  _("Balance" ) + ": %s "%(self.format_amount(c)) + self.base_unit()
                if u:
                    text +=  " [%s unconfirmed]"%(self.format_amount(u, True).strip())
                if x:
                    text +=  " [%s unmatured]"%(self.format_amount(x, True).strip())
                # append fiat balance and price from exchange rate plugin
                r = {}
                run_hook('get_fiat_status_text', c+u, r)
                quote = r.get(0)
                if quote:
                    text += "%s"%quote

                if self.tray:
                    self.tray.setToolTip("%s (%s)" % (text, self.wallet.basename()))
                icon = QIcon(":icons/status_connected.png")
        else:
            text = _("Not connected")
            icon = QIcon(":icons/status_disconnected.png")

        self.balance_label.setText(text)
        self.status_button.setIcon( icon )


    def update_wallet(self):
        self.update_status()
        if self.wallet.up_to_date or not self.network or not self.network.is_connected():
            self.update_tabs()

    def update_tabs(self):
        self.update_history_tab()
        self.update_receive_tab()
        self.update_address_tab()
        self.update_contacts_tab()
        self.update_completions()
        self.update_invoices_list()

    def create_history_tab(self):
        from history_widget import HistoryWidget
        self.history_list = l = HistoryWidget(self)
        return l

    def show_address(self, addr):
        import address_dialog
        d = address_dialog.AddressDialog(addr, self)
        d.exec_()

    def show_transaction(self, tx, tx_desc = None):
        '''tx_desc is set only for txs created in the Send tab'''
        show_transaction(tx, self, tx_desc)

    def update_history_tab(self):
        domain = self.wallet.get_account_addresses(self.current_account)
        h = self.wallet.get_history(domain)
        self.history_list.update(h)

    def create_receive_tab(self):

        self.receive_grid = grid = QGridLayout()
        grid.setColumnMinimumWidth(3, 300)

        self.receive_address_e = ButtonsLineEdit()
        self.receive_address_e.addCopyButton(self.app)
        self.receive_address_e.setReadOnly(True)
        msg = _('Myriadcoin address where the payment should be received. Note that each payment request uses a different Bitcoin address.')
        self.receive_address_label = HelpLabel(_('Receiving address'), msg)
        self.receive_address_e.textChanged.connect(self.update_receive_qr)
        self.receive_address_e.setFocusPolicy(Qt.NoFocus)
        grid.addWidget(self.receive_address_label, 0, 0)
        grid.addWidget(self.receive_address_e, 0, 1, 1, 4)

        self.receive_message_e = QLineEdit()
        grid.addWidget(QLabel(_('Description')), 1, 0)
        grid.addWidget(self.receive_message_e, 1, 1, 1, 4)
        self.receive_message_e.textChanged.connect(self.update_receive_qr)

        self.receive_amount_e = BTCAmountEdit(self.get_decimal_point)
        grid.addWidget(QLabel(_('Requested amount')), 2, 0)
        grid.addWidget(self.receive_amount_e, 2, 1, 1, 2)
        self.receive_amount_e.textChanged.connect(self.update_receive_qr)

        self.expires_combo = QComboBox()
        self.expires_combo.addItems(map(lambda x:x[0], expiration_values))
        self.expires_combo.setCurrentIndex(1)
        msg = ' '.join([
            _('Expiration date of your request.'),
            _('This information is seen by the recipient if you send them a signed payment request.'),
            _('Expired requests have to be deleted manually from your list, in order to free the corresponding Myriadcoin addresses'),
        ])
        grid.addWidget(HelpLabel(_('Expires in'), msg), 3, 0)
        grid.addWidget(self.expires_combo, 3, 1)
        self.expires_label = QLineEdit('')
        self.expires_label.setReadOnly(1)
        self.expires_label.setFocusPolicy(Qt.NoFocus)
        self.expires_label.hide()
        grid.addWidget(self.expires_label, 3, 1, 1, 2)

        self.save_request_button = QPushButton(_('Save'))
        self.save_request_button.clicked.connect(self.save_payment_request)

        self.new_request_button = QPushButton(_('New'))
        self.new_request_button.clicked.connect(self.new_payment_request)

        self.receive_qr = QRCodeWidget(fixedSize=200)
        self.receive_qr.mouseReleaseEvent = lambda x: self.toggle_qr_window()
        self.receive_qr.enterEvent = lambda x: self.app.setOverrideCursor(QCursor(Qt.PointingHandCursor))
        self.receive_qr.leaveEvent = lambda x: self.app.setOverrideCursor(QCursor(Qt.ArrowCursor))

        self.receive_buttons = buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.save_request_button)
        buttons.addWidget(self.new_request_button)

        self.receive_requests_label = QLabel(_('My Requests'))
        self.receive_list = MyTreeWidget(self, self.receive_list_menu, [_('Date'), _('Account'), _('Address'), '', _('Description'), _('Amount'), _('Status')], 4)
        self.receive_list.currentItemChanged.connect(self.receive_item_changed)
        self.receive_list.itemClicked.connect(self.receive_item_changed)
        self.receive_list.setSortingEnabled(True)
        self.receive_list.setColumnWidth(0, 180)
        self.receive_list.hideColumn(1)
        self.receive_list.hideColumn(2)

        # layout
        vbox_g = QVBoxLayout()
        vbox_g.addLayout(grid)
        vbox_g.addLayout(buttons)

        hbox = QHBoxLayout()
        hbox.addLayout(vbox_g)
        hbox.addStretch()
        hbox.addWidget(self.receive_qr)

        w = QWidget()
        vbox = QVBoxLayout(w)
        vbox.addLayout(hbox)
        vbox.addStretch(1)
        vbox.addWidget(self.receive_requests_label)
        vbox.addWidget(self.receive_list)

        return w

    def receive_item_changed(self, item):
        if item is None:
            return
        if not self.receive_list.isItemSelected(item):
            return
        addr = str(item.text(2))
        req = self.wallet.receive_requests[addr]
        expires = util.age(req['time'] + req['exp']) if req.get('exp') else _('Never')
        amount = req['amount']
        message = self.wallet.labels.get(addr, '')
        self.receive_address_e.setText(addr)
        self.receive_message_e.setText(message)
        self.receive_amount_e.setAmount(amount)
        self.expires_combo.hide()
        self.expires_label.show()
        self.expires_label.setText(expires)
        self.new_request_button.setEnabled(True)

    def delete_payment_request(self, item):
        addr = str(item.text(2))
        self.wallet.remove_payment_request(addr, self.config)
        self.update_receive_tab()
        self.clear_receive_tab()

    def get_request_URI(self, addr):
        req = self.wallet.receive_requests[addr]
        message = self.wallet.labels.get(addr, '')
        amount = req['amount']
        URI = util.create_URI(addr, amount, message)
        if req.get('time'):
            URI += "&time=%d"%req.get('time')
        if req.get('exp'):
            URI += "&exp=%d"%req.get('exp')
        if req.get('name') and req.get('sig'):
            sig = req.get('sig').decode('hex')
            sig = bitcoin.base_encode(sig, base=58)
            URI += "&name=" + req['name'] + "&sig="+sig
        return str(URI)

    def receive_list_menu(self, position):
        item = self.receive_list.itemAt(position)
        addr = str(item.text(2))
        req = self.wallet.receive_requests[addr]
        menu = QMenu()
        menu.addAction(_("Copy Address"), lambda: self.view_and_paste(_('Address'), '', addr))
        menu.addAction(_("Copy URI"), lambda: self.view_and_paste('URI', '', self.get_request_URI(addr)))
        menu.addAction(_("Save as BIP70 file"), lambda: self.export_payment_request(addr))
        menu.addAction(_("Delete"), lambda: self.delete_payment_request(item))
        run_hook('receive_list_menu', menu, addr)
        menu.exec_(self.receive_list.viewport().mapToGlobal(position))

    def sign_payment_request(self, addr):
        alias = self.config.get('alias')
        alias_privkey = None
        if alias and self.alias_info:
            alias_addr, alias_name, validated = self.alias_info
            if alias_addr:
                if self.wallet.is_mine(alias_addr):
                    msg = _('This payment request will be signed.') + '\n' + _('Please enter your password')
                    password = self.password_dialog(msg)
                    if password:
                        try:
                            self.wallet.sign_payment_request(addr, alias, alias_addr, password)
                        except Exception as e:
                            QMessageBox.warning(self, _('Error'), str(e), _('OK'))
                            return
                    else:
                        return
                else:
                    return


    def save_payment_request(self):
        addr = str(self.receive_address_e.text())
        amount = self.receive_amount_e.get_amount()
        message = unicode(self.receive_message_e.text())
        if not message and not amount:
            QMessageBox.warning(self, _('Error'), _('No message or amount'), _('OK'))
            return False
        i = self.expires_combo.currentIndex()
        expiration = map(lambda x: x[1], expiration_values)[i]
        req = self.wallet.make_payment_request(addr, amount, message, expiration)
        self.wallet.add_payment_request(req, self.config)
        self.sign_payment_request(addr)
        self.update_receive_tab()
        self.update_address_tab()
        self.save_request_button.setEnabled(False)

    def view_and_paste(self, title, msg, data):
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        vbox = QVBoxLayout()
        label = QLabel(msg)
        label.setWordWrap(True)
        vbox.addWidget(label)
        pr_e = ShowQRTextEdit(text=data)
        vbox.addWidget(pr_e)
        vbox.addLayout(Buttons(CopyCloseButton(pr_e.text, self.app, dialog)))
        dialog.setLayout(vbox)
        dialog.exec_()

    def export_payment_request(self, addr):
        r = self.wallet.receive_requests.get(addr)
        pr = paymentrequest.serialize_request(r).SerializeToString()
        name = r['id'] + '.bip70'
        fileName = self.getSaveFileName(_("Select where to save your payment request"), name, "*.bip70")
        if fileName:
            with open(fileName, "wb+") as f:
                f.write(str(pr))
            self.show_message(_("Request saved successfully"))
            self.saved = True

    def new_payment_request(self):
        addr = self.wallet.get_unused_address(self.current_account)
        if addr is None:
            if isinstance(self.wallet, Imported_Wallet):
                self.show_message(_('No more addresses in your wallet.'))
                return
            if not self.question(_("Warning: The next address will not be recovered automatically if you restore your wallet from seed; you may need to add it manually.\n\nThis occurs because you have too many unused addresses in your wallet. To avoid this situation, use the existing addresses first.\n\nCreate anyway?")):
                return
            addr = self.wallet.create_new_address(self.current_account, False)
        self.set_receive_address(addr)
        self.expires_label.hide()
        self.expires_combo.show()
        self.new_request_button.setEnabled(False)
        self.receive_message_e.setFocus(1)

    def set_receive_address(self, addr):
        self.receive_address_e.setText(addr)
        self.receive_message_e.setText('')
        self.receive_amount_e.setAmount(None)

    def clear_receive_tab(self):
        addr = self.wallet.get_unused_address(self.current_account)
        self.receive_address_e.setText(addr if addr else '')
        self.receive_message_e.setText('')
        self.receive_amount_e.setAmount(None)
        self.expires_label.hide()
        self.expires_combo.show()

    def toggle_qr_window(self):
        import qrwindow
        if not self.qr_window:
            self.qr_window = qrwindow.QR_Window(self)
            self.qr_window.setVisible(True)
            self.qr_window_geometry = self.qr_window.geometry()
        else:
            if not self.qr_window.isVisible():
                self.qr_window.setVisible(True)
                self.qr_window.setGeometry(self.qr_window_geometry)
            else:
                self.qr_window_geometry = self.qr_window.geometry()
                self.qr_window.setVisible(False)
        self.update_receive_qr()


    def receive_at(self, addr):
        if not bitcoin.is_address(addr):
            return
        self.tabs.setCurrentIndex(2)
        self.receive_address_e.setText(addr)
        self.new_request_button.setEnabled(True)

    def update_receive_tab(self):

        # hide receive tab if no receive requests available
        b = len(self.wallet.receive_requests) > 0
        self.receive_list.setVisible(b)
        self.receive_requests_label.setVisible(b)
        if not b:
            self.expires_label.hide()
            self.expires_combo.show()

        # check if it is necessary to show the account
        self.receive_list.setColumnHidden(1, len(self.wallet.get_accounts()) == 1)

        # update the receive address if necessary
        current_address = self.receive_address_e.text()
        domain = self.wallet.get_account_addresses(self.current_account, include_change=False)
        addr = self.wallet.get_unused_address(self.current_account)
        if not current_address in domain and addr:
            self.set_receive_address(addr)
        self.new_request_button.setEnabled(addr != current_address)

        # clear the list and fill it again
        self.receive_list.clear()
        for req in self.wallet.get_sorted_requests(self.config):
            address = req['address']
            if address not in domain:
                continue
            timestamp = req.get('time', 0)
            amount = req.get('amount')
            expiration = req.get('exp', None)
            message = req.get('memo', '')
            date = format_time(timestamp)
            status = req.get('status')
            signature = req.get('sig')
            requestor = req.get('name', '')
            amount_str = self.format_amount(amount) if amount else ""
            account = ''
            item = QTreeWidgetItem([date, account, address, '', message, amount_str, pr_tooltips.get(status,'')])
            if signature is not None:
                item.setIcon(3, QIcon(":icons/seal.png"))
                item.setToolTip(3, 'signed by '+ requestor)
            if status is not PR_UNKNOWN:
                item.setIcon(6, QIcon(pr_icons.get(status)))
            self.receive_list.addTopLevelItem(item)


    def update_receive_qr(self):
        addr = str(self.receive_address_e.text())
        amount = self.receive_amount_e.get_amount()
        message = unicode(self.receive_message_e.text()).encode('utf8')
        self.save_request_button.setEnabled((amount is not None) or (message != ""))
        uri = util.create_URI(addr, amount, message)
        self.receive_qr.setData(uri)
        if self.qr_window and self.qr_window.isVisible():
            self.qr_window.set_content(addr, amount, message, uri)

    def show_before_broadcast(self):
        return self.config.get('show_before_broadcast', False)

    def set_show_before_broadcast(self, show):
        self.config.set_key('show_before_broadcast', bool(show))
        self.set_send_button_text()

    def set_send_button_text(self):
        if self.show_before_broadcast():
            text = _("Send...")
        elif self.wallet and self.wallet.is_watching_only():
            text = _("Send...")
        else:
            text = _("Send")
        self.send_button.setText(text)

    def create_send_tab(self):
        self.send_grid = grid = QGridLayout()
        grid.setSpacing(8)
        grid.setColumnMinimumWidth(3,300)
        grid.setColumnStretch(5,1)
        grid.setRowStretch(8, 1)

        from paytoedit import PayToEdit
        self.amount_e = BTCAmountEdit(self.get_decimal_point)
        self.payto_e = PayToEdit(self)
        msg = _('Recipient of the funds.') + '\n\n'\
              + _('You may enter a Myriadcoin address, a label from your list of contacts (a list of completions will be proposed), or an alias (email-like address that forwards to a Bitcoin address)')
        payto_label = HelpLabel(_('Pay to'), msg)
        grid.addWidget(payto_label, 1, 0)
        grid.addWidget(self.payto_e, 1, 1, 1, 3)

        completer = QCompleter()
        completer.setCaseSensitivity(False)
        self.payto_e.setCompleter(completer)
        completer.setModel(self.completions)

        msg = _('Description of the transaction (not mandatory).') + '\n\n'\
              + _('The description is not sent to the recipient of the funds. It is stored in your wallet file, and displayed in the \'History\' tab.')
        description_label = HelpLabel(_('Description'), msg)
        grid.addWidget(description_label, 2, 0)
        self.message_e = MyLineEdit()
        grid.addWidget(self.message_e, 2, 1, 1, 3)

        self.from_label = QLabel(_('From'))
        grid.addWidget(self.from_label, 3, 0)
        self.from_list = MyTreeWidget(self, self.from_list_menu, ['',''])
        self.from_list.setHeaderHidden(True)
        self.from_list.setMaximumHeight(80)
        grid.addWidget(self.from_list, 3, 1, 1, 3)
        self.set_pay_from([])

        msg = _('Amount to be sent.') + '\n\n' \
              + _('The amount will be displayed in red if you do not have enough funds in your wallet.') + ' ' \
              + _('Note that if you have frozen some of your addresses, the available funds will be lower than your total balance.') + '\n\n' \
              + _('Keyboard shortcut: type "!" to send all your coins.')
        amount_label = HelpLabel(_('Amount'), msg)
        grid.addWidget(amount_label, 4, 0)
        grid.addWidget(self.amount_e, 4, 1, 1, 2)

        msg = _('Myriadcoin transactions are in general not free. A transaction fee is paid by the sender of the funds.') + '\n\n'\
              + _('The amount of fee can be decided freely by the sender. However, transactions with low fees take more time to be processed.') + '\n\n'\
              + _('A suggested fee is automatically added to this field. You may override it. The suggested fee increases with the size of the transaction.')
        self.fee_e_label = HelpLabel(_('Fee'), msg)
        self.fee_e = BTCAmountEdit(self.get_decimal_point)
        grid.addWidget(self.fee_e_label, 5, 0)
        grid.addWidget(self.fee_e, 5, 1, 1, 2)

        self.send_button = EnterButton(_("Send"), self.do_send)
        self.clear_button = EnterButton(_("Clear"), self.do_clear)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.send_button)
        buttons.addWidget(self.clear_button)

        def on_shortcut():
            sendable = self.get_sendable_balance()
            inputs = self.get_coins()
            for i in inputs:
                self.wallet.add_input_info(i)
            addr = self.payto_e.payto_address if self.payto_e.payto_address else self.dummy_address
            output = ('address', addr, sendable)
            dummy_tx = Transaction.from_io(inputs, [output])
            if self.fee_e.get_amount() is None:
                fee_per_kb = self.wallet.fee_per_kb(self.config)
                self.fee_e.setAmount(self.wallet.estimated_fee(dummy_tx, fee_per_kb))
            self.amount_e.setAmount(max(0, sendable - self.fee_e.get_amount()))
            # emit signal for fiat_amount update
            self.amount_e.textEdited.emit("")

        self.amount_e.shortcut.connect(on_shortcut)

        self.payto_e.textChanged.connect(self.update_fee)
        self.amount_e.textEdited.connect(self.update_fee)
        self.fee_e.textEdited.connect(self.update_fee)
        # This is so that when the user blanks the fee and moves on,
        # we go back to auto-calculate mode and put a fee back.
        self.fee_e.editingFinished.connect(self.update_fee)

        def entry_changed():
            text = ""
            if self.not_enough_funds:
                amt_color, fee_color = RED_FG, RED_FG
                text = _( "Not enough funds" )
                c, u, x = self.wallet.get_frozen_balance()
                if c+u+x:
                    text += ' (' + self.format_amount(c+u+x).strip() + ' ' + self.base_unit() + ' ' +_("are frozen") + ')'

            elif self.fee_e.isModified():
                amt_color, fee_color = BLACK_FG, BLACK_FG
            elif self.amount_e.isModified():
                amt_color, fee_color = BLACK_FG, BLUE_FG
            else:
                amt_color, fee_color = BLUE_FG, BLUE_FG

            self.statusBar().showMessage(text)
            self.amount_e.setStyleSheet(amt_color)
            self.fee_e.setStyleSheet(fee_color)

        self.amount_e.textChanged.connect(entry_changed)
        self.fee_e.textChanged.connect(entry_changed)

        self.invoices_label = QLabel(_('Invoices'))
        self.invoices_list = MyTreeWidget(self, self.invoices_list_menu,
                                          [_('Expires'), _('Requestor'), _('Description'), _('Amount'), _('Status')], 2)
        self.invoices_list.header().setResizeMode(1, QHeaderView.Interactive)
        self.invoices_list.setColumnWidth(1, 200)

        vbox0 = QVBoxLayout()
        vbox0.addLayout(grid)
        vbox0.addLayout(buttons)
        vbox0.addStretch(1)
        hbox = QHBoxLayout()
        hbox.addLayout(vbox0)
        hbox.addStretch(1)
        w = QWidget()
        vbox = QVBoxLayout(w)
        vbox.addLayout(hbox)
        vbox.addStretch()
        vbox.addWidget(self.invoices_label)
        vbox.addWidget(self.invoices_list)

        # Defer this until grid is parented to avoid ugly flash during startup
        self.update_fee_edit()

        run_hook('create_send_tab', grid)
        return w

    def update_fee(self):
        self.require_fee_update = True

    def do_update_fee(self):
        '''Recalculate the fee.  If the fee was manually input, retain it, but
        still build the TX to see if there are enough funds.
        '''
        freeze_fee = (self.fee_e.isModified()
                      and (self.fee_e.text() or self.fee_e.hasFocus()))
        outputs = self.payto_e.get_outputs()
        amount = self.amount_e.get_amount()
        if amount is None:
            if not freeze_fee:
                self.fee_e.setAmount(None)
            self.not_enough_funds = False
        else:
            fee = self.fee_e.get_amount() if freeze_fee else None
            if not outputs:
                addr = self.payto_e.payto_address if self.payto_e.payto_address else self.dummy_address
                outputs = [('address', addr, amount)]
            try:
                tx = self.wallet.make_unsigned_transaction(self.get_coins(), outputs, self.config, fee)
                self.not_enough_funds = False
            except NotEnoughFunds:
                self.not_enough_funds = True
            if not freeze_fee:
                fee = None if self.not_enough_funds else self.wallet.get_tx_fee(tx)
                self.fee_e.setAmount(fee)

    def update_fee_edit(self):
        b = self.config.get('can_edit_fees', False)
        self.fee_e.setVisible(b)
        self.fee_e_label.setVisible(b)

    def from_list_delete(self, item):
        i = self.from_list.indexOfTopLevelItem(item)
        self.pay_from.pop(i)
        self.redraw_from_list()

    def from_list_menu(self, position):
        item = self.from_list.itemAt(position)
        menu = QMenu()
        menu.addAction(_("Remove"), lambda: self.from_list_delete(item))
        menu.exec_(self.from_list.viewport().mapToGlobal(position))

    def set_pay_from(self, domain = None):
        self.pay_from = [] if domain == [] else self.wallet.get_spendable_coins(domain)
        self.redraw_from_list()

    def redraw_from_list(self):
        self.from_list.clear()
        self.from_label.setHidden(len(self.pay_from) == 0)
        self.from_list.setHidden(len(self.pay_from) == 0)

        def format(x):
            h = x.get('prevout_hash')
            return h[0:8] + '...' + h[-8:] + ":%d"%x.get('prevout_n') + u'\t' + "%s"%x.get('address')

        for item in self.pay_from:
            self.from_list.addTopLevelItem(QTreeWidgetItem( [format(item), self.format_amount(item['value']) ]))

    def get_contact_payto(self, key):
        _type, value = self.contacts.get(key)
        return key + '  <' + value + '>' if _type == 'address' else key

    def update_completions(self):
        l = [self.get_contact_payto(key) for key in self.contacts.keys()]
        self.completions.setStringList(l)

    def protected(func):
        '''Password request wrapper.  The password is passed to the function
        as the 'password' named argument.  Return value is a 2-element
        tuple: (Cancelled, Result) where Cancelled is True if the user
        cancels the password request, otherwise False.  Result is the
        return value of the wrapped function, or None if cancelled.
        '''
        def request_password(self, *args, **kwargs):
            parent = kwargs.get('parent', self)
            if self.wallet.use_encryption:
                while True:
                    password = self.password_dialog(parent=parent)
                    if not password:
                        return True, None
                    try:
                        self.wallet.check_password(password)
                        break
                    except Exception as e:
                        QMessageBox.warning(parent, _('Error'), str(e), _('OK'))
                        continue
            else:
                password = None

            kwargs['password'] = password
            return False, func(self, *args, **kwargs)
        return request_password

    def read_send_tab(self):
        if self.payment_request and self.payment_request.has_expired():
            QMessageBox.warning(self, _('Error'), _('Payment request has expired'), _('OK'))
            return
        label = unicode( self.message_e.text() )

        if self.payment_request:
            outputs = self.payment_request.get_outputs()
        else:
            errors = self.payto_e.get_errors()
            if errors:
                self.show_warning(_("Invalid Lines found:") + "\n\n" + '\n'.join([ _("Line #") + str(x[0]+1) + ": " + x[1] for x in errors]))
                return
            outputs = self.payto_e.get_outputs()

            if self.payto_e.is_alias and self.payto_e.validated is False:
                alias = self.payto_e.toPlainText()
                msg = _('WARNING: the alias "%s" could not be validated via an additional security check, DNSSEC, and thus may not be correct.'%alias) + '\n'
                msg += _('Do you wish to continue?')
                if not self.question(msg):
                    return

        if not outputs:
            QMessageBox.warning(self, _('Error'), _('No outputs'), _('OK'))
            return

        for _type, addr, amount in outputs:
            if addr is None:
                QMessageBox.warning(self, _('Error'), _('Myriadcoin Address is None'), _('OK'))
                return
            if _type == 'address' and not bitcoin.is_address(addr):
                QMessageBox.warning(self, _('Error'), _('Invalid Myriadcoin Address'), _('OK'))
                return
            if amount is None:
                QMessageBox.warning(self, _('Error'), _('Invalid Amount'), _('OK'))
                return

        fee = self.fee_e.get_amount()
        if fee is None:
            QMessageBox.warning(self, _('Error'), _('Invalid Fee'), _('OK'))
            return

        amount = sum(map(lambda x:x[2], outputs))
        confirm_amount = self.config.get('confirm_amount', COIN)
        if amount >= confirm_amount:
            o = '\n'.join(map(lambda x:x[1], outputs))
            if not self.question(_("send %(amount)s to %(address)s?")%{ 'amount' : self.format_amount(amount) + ' '+ self.base_unit(), 'address' : o}):
                return

        coins = self.get_coins()
        return outputs, fee, label, coins


    def do_send(self):
        if run_hook('before_send'):
            return
        r = self.read_send_tab()
        if not r:
            return
        outputs, fee, tx_desc, coins = r
        try:
            tx = self.wallet.make_unsigned_transaction(coins, outputs, self.config, fee)
        except NotEnoughFunds:
            self.show_message(_("Insufficient funds"))
            return
        except BaseException as e:
            traceback.print_exc(file=sys.stdout)
            self.show_message(str(e))
            return

        if tx.get_fee() < MIN_RELAY_TX_FEE and tx.requires_fee(self.wallet):
            QMessageBox.warning(self, _('Error'), _("This transaction requires a higher fee, or it will not be propagated by the network."), _('OK'))
            return

        if not self.config.get('can_edit_fees', False):
            if not self.question(_("A fee of %(fee)s will be added to this transaction.\nProceed?")%{ 'fee' : self.format_amount(fee) + ' '+ self.base_unit()}):
                return
        else:
            confirm_fee = self.config.get('confirm_fee', 100000)
            if fee >= confirm_fee:
                if not self.question(_("The fee for this transaction seems unusually high.\nAre you really sure you want to pay %(fee)s in fees?")%{ 'fee' : self.format_amount(fee) + ' '+ self.base_unit()}):
                    return

        if self.show_before_broadcast():
            self.show_transaction(tx, tx_desc)
        else:
            def sign_done(success):
                if success:
                    if not tx.is_complete():
                        self.show_transaction(tx)
                        self.do_clear()
                    else:
                        self.broadcast_transaction(tx, tx_desc)
            self.sign_tx(tx, sign_done)


    @protected
    def sign_tx(self, tx, callback, password, parent=None):
        '''Sign the transaction in a separate thread.  When done, calls
        the callback with a success code of True or False.
        '''
        if parent == None:
            parent = self
        self.send_button.setDisabled(True)

        # call hook to see if plugin needs gui interaction
        run_hook('sign_tx', tx)

        # sign the tx
        success = [False]  # Array to work around python scoping
        def sign_thread():
            if not self.wallet.is_watching_only():
                self.wallet.sign_transaction(tx, password)
        def on_sign_successful(ret):
            success[0] = True
        def on_dialog_close():
            self.send_button.setDisabled(False)
            callback(success[0])

        # keep a reference to WaitingDialog or the gui might crash
        self.waiting_dialog = WaitingDialog(parent, 'Signing transaction...', sign_thread, on_sign_successful, on_dialog_close)
        self.waiting_dialog.start()


    def broadcast_transaction(self, tx, tx_desc, parent=None):

        def broadcast_thread():
            # non-GUI thread
            pr = self.payment_request
            if pr is None:
                return self.wallet.sendtx(tx)
            if pr.has_expired():
                self.payment_request = None
                return False, _("Payment request has expired")
            status, msg =  self.wallet.sendtx(tx)
            if not status:
                return False, msg
            key = pr.get_id()
            self.invoices.set_paid(key, tx.hash())
            self.payment_request = None
            refund_address = self.wallet.addresses()[0]
            ack_status, ack_msg = pr.send_ack(str(tx), refund_address)
            if ack_status:
                msg = ack_msg
            return status, msg

        def broadcast_done(status, msg):
            # GUI thread
            if status:
                if tx_desc is not None and tx.is_complete():
                    self.wallet.set_label(tx.hash(), tx_desc)
                QMessageBox.information(parent, '', _('Payment sent.') + '\n' + msg, _('OK'))
                self.update_invoices_list()
                self.do_clear()
            else:
                QMessageBox.warning(parent, _('Error'), msg, _('OK'))
            self.send_button.setDisabled(False)

        if parent == None:
            parent = self
        self.waiting_dialog = WaitingDialog(parent, 'Broadcasting transaction...', broadcast_thread, broadcast_done)
        self.waiting_dialog.start()



    def prepare_for_payment_request(self):
        self.tabs.setCurrentIndex(1)
        self.payto_e.is_pr = True
        for e in [self.payto_e, self.amount_e, self.message_e]:
            e.setFrozen(True)
        self.payto_e.setText(_("please wait..."))
        return True

    def payment_request_ok(self):
        pr = self.payment_request
        key = self.invoices.add(pr)
        status = self.invoices.get_status(key)
        self.update_invoices_list()
        if status == PR_PAID:
            self.show_message("invoice already paid")
            self.do_clear()
            self.payment_request = None
            return

        self.payto_e.is_pr = True
        if not pr.has_expired():
            self.payto_e.setGreen()
        else:
            self.payto_e.setExpired()

        self.payto_e.setText(pr.get_requestor())
        self.amount_e.setText(format_satoshis_plain(pr.get_amount(), self.decimal_point))
        self.message_e.setText(pr.get_memo())
        # signal to set fee
        self.amount_e.textEdited.emit("")

    def payment_request_error(self):
        self.show_message(self.payment_request.error)
        self.payment_request = None
        self.do_clear()

    def pay_to_URI(self, URI):
        if not URI:
            return
        try:
            out = util.parse_URI(unicode(URI))
        except Exception as e:
            QMessageBox.warning(self, _('Error'), _('Invalid myriadcoin URI:') + '\n' + str(e), _('OK'))
            return
        self.tabs.setCurrentIndex(1)

        r = out.get('r')
        sig = out.get('sig')
        name = out.get('name')
        if r or (name and sig):
            def get_payment_request_thread():
                if name and sig:
                    from electrum import paymentrequest
                    pr = paymentrequest.serialize_request(out).SerializeToString()
                    self.payment_request = paymentrequest.PaymentRequest(pr)
                else:
                    self.payment_request = get_payment_request(r)
                if self.payment_request.verify(self.contacts):
                    self.emit(SIGNAL('payment_request_ok'))
                else:
                    self.emit(SIGNAL('payment_request_error'))
            t = threading.Thread(target=get_payment_request_thread)
            t.setDaemon(True)
            t.start()
            self.prepare_for_payment_request()
            return

        address = out.get('address')
        amount = out.get('amount')
        label = out.get('label')
        message = out.get('message')
        if label:
            if self.wallet.labels.get(address) != label:
                if self.question(_('Save label "%(label)s" for address %(address)s ?'%{'label':label,'address':address})):
                    if address not in self.wallet.addressbook and not self.wallet.is_mine(address):
                        self.wallet.addressbook.append(address)
                        self.wallet.set_label(address, label)
        else:
            label = self.wallet.labels.get(address)
        if address:
            self.payto_e.setText(label + '  <'+ address +'>' if label else address)
        if message:
            self.message_e.setText(message)
        if amount:
            self.amount_e.setAmount(amount)
            self.amount_e.textEdited.emit("")


    def do_clear(self):
        self.not_enough_funds = False
        self.payment_request = None
        self.payto_e.is_pr = False
        for e in [self.payto_e, self.message_e, self.amount_e, self.fee_e]:
            e.setText('')
            e.setFrozen(False)
        self.set_pay_from([])
        self.update_status()
        run_hook('do_clear')

    def set_frozen_state(self, addrs, freeze):
        self.wallet.set_frozen_state(addrs, freeze)
        self.update_address_tab()
        self.update_fee()

    def create_list_tab(self, l):
        w = QWidget()
        vbox = QVBoxLayout()
        w.setLayout(vbox)
        vbox.setMargin(0)
        vbox.setSpacing(0)
        vbox.addWidget(l)
        buttons = QWidget()
        vbox.addWidget(buttons)
        return w

    def create_addresses_tab(self):
        l = MyTreeWidget(self, self.create_receive_menu, [ _('Address'), _('Label'), _('Balance'), _('Tx')], 1)
        l.setSelectionMode(QAbstractItemView.ExtendedSelection)
        l.setSortingEnabled(False)
        self.address_list = l
        return self.create_list_tab(l)

    def create_contacts_tab(self):
        l = MyTreeWidget(self, self.create_contact_menu, [_('Key'), _('Value'), _('Type')], 1)
        self.contacts_list = l
        return self.create_list_tab(l)

    def update_invoices_list(self):
        inv_list = self.invoices.sorted_list()
        l = self.invoices_list
        l.clear()
        for pr in inv_list:
            key = pr.get_id()
            status = self.invoices.get_status(key)
            requestor = pr.get_requestor()
            exp = pr.get_expiration_date()
            date_str = util.format_time(exp) if exp else _('Never')
            item = QTreeWidgetItem( [ date_str, requestor, pr.memo, self.format_amount(pr.get_amount(), whitespaces=True), pr_tooltips.get(status,'')] )
            item.setIcon(4, QIcon(pr_icons.get(status)))
            item.setData(0, Qt.UserRole, key)
            item.setFont(1, QFont(MONOSPACE_FONT))
            item.setFont(3, QFont(MONOSPACE_FONT))
            l.addTopLevelItem(item)
        l.setCurrentItem(l.topLevelItem(0))
        self.invoices_list.setVisible(len(inv_list))
        self.invoices_label.setVisible(len(inv_list))

    def delete_imported_key(self, addr):
        if self.question(_("Do you want to remove")+" %s "%addr +_("from your wallet?")):
            self.wallet.delete_imported_key(addr)
            self.update_address_tab()
            self.update_history_tab()

    def edit_account_label(self, k):
        text, ok = QInputDialog.getText(self, _('Rename account'), _('Name') + ':', text = self.wallet.labels.get(k,''))
        if ok:
            label = unicode(text)
            self.wallet.set_label(k,label)
            self.update_address_tab()

    def account_set_expanded(self, item, k, b):
        item.setExpanded(b)
        self.accounts_expanded[k] = b

    def create_account_menu(self, position, k, item):
        menu = QMenu()
        exp = item.isExpanded()
        menu.addAction(_("Minimize") if exp else _("Maximize"), lambda: self.account_set_expanded(item, k, not exp))
        menu.addAction(_("Rename"), lambda: self.edit_account_label(k))
        if self.wallet.seed_version > 4:
            menu.addAction(_("View details"), lambda: self.show_account_details(k))
        if self.wallet.account_is_pending(k):
            menu.addAction(_("Delete"), lambda: self.delete_pending_account(k))
        menu.exec_(self.address_list.viewport().mapToGlobal(position))

    def delete_pending_account(self, k):
        self.wallet.delete_pending_account(k)
        self.update_address_tab()
        self.update_account_selector()

    def create_receive_menu(self, position):
        # fixme: this function apparently has a side effect.
        # if it is not called the menu pops up several times
        #self.address_list.selectedIndexes()

        selected = self.address_list.selectedItems()
        multi_select = len(selected) > 1
        addrs = [unicode(item.text(0)) for item in selected]
        if not multi_select:
            item = self.address_list.itemAt(position)
            if not item:
                return
            addr = addrs[0]
            if not is_valid(addr):
                k = str(item.data(0,32).toString())
                if k:
                    self.create_account_menu(position, k, item)
                else:
                    item.setExpanded(not item.isExpanded())
                return

        menu = QMenu()
        if not multi_select:
            menu.addAction(_("Copy to clipboard"), lambda: self.app.clipboard().setText(addr))
            menu.addAction(_("Request payment"), lambda: self.receive_at(addr))
            menu.addAction(_("Edit label"), lambda: self.address_list.edit_label(item))
            menu.addAction(_('History'), lambda: self.show_address(addr))
            menu.addAction(_('Public Keys'), lambda: self.show_public_keys(addr))
            if self.wallet.can_export():
                menu.addAction(_("Private key"), lambda: self.show_private_key(addr))
            if not self.wallet.is_watching_only():
                menu.addAction(_("Sign/verify message"), lambda: self.sign_verify_message(addr))
                menu.addAction(_("Encrypt/decrypt message"), lambda: self.encrypt_message(addr))
            if self.wallet.is_imported(addr):
                menu.addAction(_("Remove from wallet"), lambda: self.delete_imported_key(addr))
            addr_URL = block_explorer_URL(self.config, 'addr', addr)
            if addr_URL:
                menu.addAction(_("View on block explorer"), lambda: webbrowser.open(addr_URL))

        if any(not self.wallet.is_frozen(addr) for addr in addrs):
            menu.addAction(_("Freeze"), lambda: self.set_frozen_state(addrs, True))
        if any(self.wallet.is_frozen(addr) for addr in addrs):
            menu.addAction(_("Unfreeze"), lambda: self.set_frozen_state(addrs, False))

        def can_send(addr):
            return not self.wallet.is_frozen(addr) and sum(self.wallet.get_addr_balance(addr)[:2])
        if any(can_send(addr) for addr in addrs):
            menu.addAction(_("Send From"), lambda: self.send_from_addresses(addrs))

        run_hook('receive_menu', menu, addrs)
        menu.exec_(self.address_list.viewport().mapToGlobal(position))


    def get_sendable_balance(self):
        return sum(map(lambda x:x['value'], self.get_coins()))


    def get_coins(self):
        if self.pay_from:
            return self.pay_from
        else:
            domain = self.wallet.get_account_addresses(self.current_account)
            return self.wallet.get_spendable_coins(domain)


    def send_from_addresses(self, addrs):
        self.set_pay_from(addrs)
        self.tabs.setCurrentIndex(1)
        self.update_fee()

    def paytomany(self):
        self.tabs.setCurrentIndex(1)
        self.payto_e.paytomany()

    def payto(self, addr):
        if not addr:
            return
        self.tabs.setCurrentIndex(1)
        self.payto_e.setText(addr)
        self.amount_e.setFocus()

    def delete_contact(self, x):
        if not self.question(_("Do you want to remove")+" %s "%x +_("from your list of contacts?")):
            return
        self.contacts.pop(x)
        self.update_history_tab()
        self.update_contacts_tab()
        self.update_completions()

    def create_contact_menu(self, position):
        item = self.contacts_list.itemAt(position)
        menu = QMenu()
        if not item:
            menu.addAction(_("New contact"), lambda: self.new_contact_dialog())
        else:
            key = unicode(item.text(0))
            menu.addAction(_("Copy to Clipboard"), lambda: self.app.clipboard().setText(key))
            menu.addAction(_("Pay to"), lambda: self.payto(self.get_contact_payto(key)))
            menu.addAction(_("Delete"), lambda: self.delete_contact(key))
            addr_URL = block_explorer_URL(self.config, 'addr', unicode(item.text(1)))
            if addr_URL:
                menu.addAction(_("View on block explorer"), lambda: webbrowser.open(addr_URL))

        run_hook('create_contact_menu', menu, item)
        menu.exec_(self.contacts_list.viewport().mapToGlobal(position))


    def show_invoice(self, key):
        pr = self.invoices.get(key)
        pr.verify(self.contacts)
        self.show_pr_details(pr)

    def show_pr_details(self, pr):
        d = QDialog(self)
        d.setWindowTitle(_("Invoice"))
        vbox = QVBoxLayout(d)
        grid = QGridLayout()
        grid.addWidget(QLabel(_("Requestor") + ':'), 0, 0)
        grid.addWidget(QLabel(pr.get_requestor()), 0, 1)
        grid.addWidget(QLabel(_("Expires") + ':'), 1, 0)
        grid.addWidget(QLabel(format_time(pr.get_expiration_date())), 1, 1)
        grid.addWidget(QLabel(_("Memo") + ':'), 2, 0)
        grid.addWidget(QLabel(pr.get_memo()), 2, 1)
        grid.addWidget(QLabel(_("Signature") + ':'), 3, 0)
        grid.addWidget(QLabel(pr.get_verify_status()), 3, 1)
        grid.addWidget(QLabel(_("Payment URL") + ':'), 4, 0)
        grid.addWidget(QLabel(pr.payment_url), 4, 1)
        grid.addWidget(QLabel(_("Outputs") + ':'), 5, 0)
        outputs_str = '\n'.join(map(lambda x: x[1] + ' ' + self.format_amount(x[2])+ self.base_unit(), pr.get_outputs()))
        grid.addWidget(QLabel(outputs_str), 5, 1)
        if pr.tx:
            grid.addWidget(QLabel(_("Transaction ID") + ':'), 6, 0)
            l = QLineEdit(pr.tx)
            l.setReadOnly(True)
            grid.addWidget(l, 6, 1)
        vbox.addLayout(grid)
        vbox.addLayout(Buttons(CloseButton(d)))
        d.exec_()
        return


    def do_pay_invoice(self, key):
        pr = self.invoices.get(key)
        self.payment_request = pr
        self.prepare_for_payment_request()
        if pr.verify(self.contacts):
            self.payment_request_ok()
        else:
            self.payment_request_error()


    def invoices_list_menu(self, position):
        item = self.invoices_list.itemAt(position)
        if not item:
            return
        key = str(item.data(0, 32).toString())
        pr = self.invoices.get(key)
        status = self.invoices.get_status(key)
        menu = QMenu()
        menu.addAction(_("Details"), lambda: self.show_invoice(key))
        if status == PR_UNPAID:
            menu.addAction(_("Pay Now"), lambda: self.do_pay_invoice(key))
        def delete_invoice(key):
            self.invoices.remove(key)
            self.update_invoices_list()
        menu.addAction(_("Delete"), lambda: delete_invoice(key))
        menu.exec_(self.invoices_list.viewport().mapToGlobal(position))


    def update_address_tab(self):
        l = self.address_list
        item = l.currentItem()
        current_address = item.data(0, Qt.UserRole).toString() if item else None
        l.clear()
        accounts = self.wallet.get_accounts()
        if self.current_account is None:
            account_items = sorted(accounts.items())
        else:
            account_items = [(self.current_account, accounts.get(self.current_account))]
        for k, account in account_items:
            if len(accounts) > 1:
                name = self.wallet.get_account_name(k)
                c, u, x = self.wallet.get_account_balance(k)
                account_item = QTreeWidgetItem([ name, '', self.format_amount(c + u + x), ''])
                account_item.setExpanded(self.accounts_expanded.get(k, True))
                account_item.setData(0, Qt.UserRole, k)
                l.addTopLevelItem(account_item)
            else:
                account_item = l
            sequences = [0,1] if account.has_change() else [0]
            for is_change in sequences:
                if len(sequences) > 1:
                    name = _("Receiving") if not is_change else _("Change")
                    seq_item = QTreeWidgetItem( [ name, '', '', '', ''] )
                    account_item.addChild(seq_item)
                    if not is_change:
                        seq_item.setExpanded(True)
                else:
                    seq_item = account_item
                used_item = QTreeWidgetItem( [ _("Used"), '', '', '', ''] )
                used_flag = False
                addr_list = account.get_addresses(is_change)
                for address in addr_list:
                    num = len(self.wallet.history.get(address,[]))
                    is_used = self.wallet.is_used(address)
                    label = self.wallet.labels.get(address,'')
                    c, u, x = self.wallet.get_addr_balance(address)
                    balance = self.format_amount(c + u + x)
                    item = QTreeWidgetItem( [ address, label, balance, "%d"%num] )
                    item.setFont(0, QFont(MONOSPACE_FONT))
                    item.setData(0, Qt.UserRole, address)
                    item.setData(0, Qt.UserRole+1, True) # label can be edited
                    if self.wallet.is_frozen(address):
                        item.setBackgroundColor(0, QColor('lightblue'))
                    if self.wallet.is_beyond_limit(address, account, is_change):
                        item.setBackgroundColor(0, QColor('red'))
                    if is_used:
                        if not used_flag:
                            seq_item.insertChild(0, used_item)
                            used_flag = True
                        used_item.addChild(item)
                    else:
                        seq_item.addChild(item)
                    if address == current_address:
                        l.setCurrentItem(item)


    def update_contacts_tab(self):
        l = self.contacts_list
        item = l.currentItem()
        current_key = item.data(0, Qt.UserRole).toString() if item else None
        l.clear()
        for key in sorted(self.contacts.keys()):
            _type, value = self.contacts[key]
            item = QTreeWidgetItem([key, value, _type])
            item.setData(0, Qt.UserRole, key)
            l.addTopLevelItem(item)
            if key == current_key:
                l.setCurrentItem(item)
        run_hook('update_contacts_tab', l)


    def create_console_tab(self):
        from console import Console
        self.console = console = Console()
        return console


    def update_console(self):
        console = self.console
        console.history = self.config.get("console-history",[])
        console.history_index = len(console.history)

        console.updateNamespace({'wallet' : self.wallet, 'network' : self.network, 'gui':self})
        console.updateNamespace({'util' : util, 'bitcoin':bitcoin})

        c = commands.Commands(self.config, self.wallet, self.network, lambda: self.console.set_json(True))
        methods = {}
        def mkfunc(f, method):
            return lambda *args: apply( f, (method, args, self.password_dialog ))
        for m in dir(c):
            if m[0]=='_' or m in ['network','wallet']: continue
            methods[m] = mkfunc(c._run, m)

        console.updateNamespace(methods)


    def change_account(self,s):
        if s == _("All accounts"):
            self.current_account = None
        else:
            accounts = self.wallet.get_account_names()
            for k, v in accounts.items():
                if v == s:
                    self.current_account = k
        self.update_history_tab()
        self.update_status()
        self.update_address_tab()
        self.update_receive_tab()

    def create_status_bar(self):

        sb = QStatusBar()
        sb.setFixedHeight(35)
        qtVersion = qVersion()

        self.balance_label = QLabel("")
        sb.addWidget(self.balance_label)

        from version_getter import UpdateLabel
        self.updatelabel = UpdateLabel(self.config, sb)

        self.account_selector = QComboBox()
        self.account_selector.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.connect(self.account_selector,SIGNAL("activated(QString)"),self.change_account)
        sb.addPermanentWidget(self.account_selector)

        self.search_box = QLineEdit()
        self.search_box.textChanged.connect(self.do_search)
        self.search_box.hide()
        sb.addPermanentWidget(self.search_box)

        if (int(qtVersion[0]) >= 4 and int(qtVersion[2]) >= 7):
            sb.addPermanentWidget( StatusBarButton( QIcon(":icons/switchgui.png"), _("Switch to Lite Mode"), self.go_lite ) )

        self.lock_icon = QIcon()
        self.password_button = StatusBarButton( self.lock_icon, _("Password"), self.change_password_dialog )
        sb.addPermanentWidget( self.password_button )

        sb.addPermanentWidget( StatusBarButton( QIcon(":icons/preferences.png"), _("Preferences"), self.settings_dialog ) )
        self.seed_button = StatusBarButton( QIcon(":icons/seed.png"), _("Seed"), self.show_seed_dialog )
        sb.addPermanentWidget( self.seed_button )
        self.status_button = StatusBarButton( QIcon(":icons/status_disconnected.png"), _("Network"), self.run_network_dialog )
        sb.addPermanentWidget( self.status_button )
        run_hook('create_status_bar', sb)
        self.setStatusBar(sb)

    def update_lock_icon(self):
        icon = QIcon(":icons/lock.png") if self.wallet.use_encryption else QIcon(":icons/unlock.png")
        self.password_button.setIcon( icon )

    def update_buttons_on_seed(self):
        self.seed_button.setVisible(self.wallet.has_seed())
        self.password_button.setVisible(self.wallet.can_change_password())
        self.set_send_button_text()

    def change_password_dialog(self):
        from password_dialog import PasswordDialog
        d = PasswordDialog(self.wallet, self)
        d.run()
        self.update_lock_icon()

    def toggle_search(self):
        self.search_box.setHidden(not self.search_box.isHidden())
        if not self.search_box.isHidden():
            self.search_box.setFocus(1)
        else:
            self.do_search('')

    def do_search(self, t):
        i = self.tabs.currentIndex()
        if i == 0:
            self.history_list.filter(t, [2, 3, 4])  # Date, Description, Amount
        elif i == 1:
            self.invoices_list.filter(t, [0, 1, 2, 3]) # Date, Requestor, Description, Amount
        elif i == 2:
            self.receive_list.filter(t, [0, 1, 2, 3, 4]) # Date, Account, Address, Description, Amount
        elif i == 3:
            self.address_list.filter(t, [0,1, 2])  # Address, Label, Balance
        elif i == 4:
            self.contacts_list.filter(t, [0, 1])  # Key, Value


    def new_contact_dialog(self):
        d = QDialog(self)
        d.setWindowTitle(_("New Contact"))
        vbox = QVBoxLayout(d)
        vbox.addWidget(QLabel(_('New Contact') + ':'))
        grid = QGridLayout()
        line1 = QLineEdit()
        line2 = QLineEdit()
        grid.addWidget(QLabel(_("Address")), 1, 0)
        grid.addWidget(line1, 1, 1)
        grid.addWidget(QLabel(_("Name")), 2, 0)
        grid.addWidget(line2, 2, 1)

        vbox.addLayout(grid)
        vbox.addLayout(Buttons(CancelButton(d), OkButton(d)))

        if not d.exec_():
            return

        address = str(line1.text())
        label = unicode(line2.text())

        if not is_valid(address):
            QMessageBox.warning(self, _('Error'), _('Invalid Address'), _('OK'))
            return

        self.contacts[label] = ('address', address)

        self.update_contacts_tab()
        self.update_history_tab()
        self.update_completions()
        self.tabs.setCurrentIndex(3)


    @protected
    def new_account_dialog(self, password):
        dialog = QDialog(self)
        dialog.setModal(1)
        dialog.setWindowTitle(_("New Account"))
        vbox = QVBoxLayout()
        vbox.addWidget(QLabel(_('Account name')+':'))
        e = QLineEdit()
        vbox.addWidget(e)
        msg = _("Note: Newly created accounts are 'pending' until they receive myriadcoin.") + " " \
            + _("You will need to wait for 2 confirmations until the correct balance is displayed and more addresses are created for that account.")
        l = QLabel(msg)
        l.setWordWrap(True)
        vbox.addWidget(l)
        vbox.addLayout(Buttons(CancelButton(dialog), OkButton(dialog)))
        dialog.setLayout(vbox)
        r = dialog.exec_()
        if not r:
            return
        name = str(e.text())
        self.wallet.create_pending_account(name, password)
        self.update_address_tab()
        self.update_account_selector()
        self.tabs.setCurrentIndex(3)


    def show_master_public_keys(self):

        dialog = QDialog(self)
        dialog.setModal(1)
        dialog.setWindowTitle(_("Master Public Keys"))

        mpk_dict = self.wallet.get_master_public_keys()
        vbox = QVBoxLayout()
        # only show the combobox in case multiple accounts are available
        if len(mpk_dict) > 1:
            gb = QGroupBox(_("Master Public Keys"))
            vbox.addWidget(gb)
            group = QButtonGroup()
            first_button = None
            for key in sorted(mpk_dict.keys()):
                is_mine = self.wallet.master_private_keys.has_key(key)
                b = QRadioButton(gb)
                name = 'Self' if is_mine else 'Cosigner'
                b.setText(name + ' (%s)'%key)
                b.key = key
                group.addButton(b)
                vbox.addWidget(b)
                if not first_button:
                    first_button = b

            mpk_text = ShowQRTextEdit()
            mpk_text.setMaximumHeight(170)
            vbox.addWidget(mpk_text)

            def show_mpk(b):
                mpk = mpk_dict.get(b.key, "")
                mpk_text.setText(mpk)

            group.buttonReleased.connect(show_mpk)
            first_button.setChecked(True)
            show_mpk(first_button)
        elif len(mpk_dict) == 1:
            mpk = mpk_dict.values()[0]
            mpk_text = ShowQRTextEdit(text=mpk)
            mpk_text.setMaximumHeight(170)
            vbox.addWidget(mpk_text)

        mpk_text.addCopyButton(self.app)
        vbox.addLayout(Buttons(CloseButton(dialog)))
        dialog.setLayout(vbox)
        dialog.exec_()

    @protected
    def show_seed_dialog(self, password):
        if not self.wallet.has_seed():
            QMessageBox.information(self, _('Message'), _('This wallet has no seed'), _('OK'))
            return

        try:
            mnemonic = self.wallet.get_mnemonic(password)
        except BaseException as e:
            QMessageBox.warning(self, _('Error'), str(e), _('OK'))
            return
        from seed_dialog import SeedDialog
        d = SeedDialog(self, mnemonic, self.wallet.has_imported_keys())
        d.exec_()



    def show_qrcode(self, data, title = _("QR code")):
        if not data:
            return
        d = QRDialog(data, self, title)
        d.exec_()

    def show_public_keys(self, address):
        if not address: return
        try:
            pubkey_list = self.wallet.get_public_keys(address)
        except Exception as e:
            traceback.print_exc(file=sys.stdout)
            self.show_message(str(e))
            return

        d = QDialog(self)
        d.setMinimumSize(600, 200)
        d.setModal(1)
        d.setWindowTitle(_("Public key"))
        vbox = QVBoxLayout()
        vbox.addWidget( QLabel(_("Address") + ': ' + address))
        vbox.addWidget( QLabel(_("Public key") + ':'))
        keys_e = ShowQRTextEdit(text='\n'.join(pubkey_list))
        keys_e.addCopyButton(self.app)
        vbox.addWidget(keys_e)
        vbox.addLayout(Buttons(CloseButton(d)))
        d.setLayout(vbox)
        d.exec_()

    @protected
    def show_private_key(self, address, password):
        if not address: return
        try:
            pk_list = self.wallet.get_private_key(address, password)
        except Exception as e:
            traceback.print_exc(file=sys.stdout)
            self.show_message(str(e))
            return

        d = QDialog(self)
        d.setMinimumSize(600, 200)
        d.setModal(1)
        d.setWindowTitle(_("Private key"))
        vbox = QVBoxLayout()
        vbox.addWidget( QLabel(_("Address") + ': ' + address))
        vbox.addWidget( QLabel(_("Private key") + ':'))
        keys_e = ShowQRTextEdit(text='\n'.join(pk_list))
        keys_e.addCopyButton(self.app)
        vbox.addWidget(keys_e)
        vbox.addLayout(Buttons(CloseButton(d)))
        d.setLayout(vbox)
        d.exec_()


    @protected
    def do_sign(self, address, message, signature, password):
        message = unicode(message.toPlainText())
        message = message.encode('utf-8')
        try:
            sig = self.wallet.sign_message(str(address.text()), message, password)
            sig = base64.b64encode(sig)
            signature.setText(sig)
        except Exception as e:
            self.show_message(str(e))

    def do_verify(self, address, message, signature):
        message = unicode(message.toPlainText())
        message = message.encode('utf-8')
        sig = base64.b64decode(str(signature.toPlainText()))
        if bitcoin.verify_message(address.text(), sig, message):
            self.show_message(_("Signature verified"))
        else:
            self.show_message(_("Error: wrong signature"))


    def sign_verify_message(self, address=''):
        d = QDialog(self)
        d.setModal(1)
        d.setWindowTitle(_('Sign/verify Message'))
        d.setMinimumSize(410, 290)

        layout = QGridLayout(d)

        message_e = QTextEdit()
        layout.addWidget(QLabel(_('Message')), 1, 0)
        layout.addWidget(message_e, 1, 1)
        layout.setRowStretch(2,3)

        address_e = QLineEdit()
        address_e.setText(address)
        layout.addWidget(QLabel(_('Address')), 2, 0)
        layout.addWidget(address_e, 2, 1)

        signature_e = QTextEdit()
        layout.addWidget(QLabel(_('Signature')), 3, 0)
        layout.addWidget(signature_e, 3, 1)
        layout.setRowStretch(3,1)

        hbox = QHBoxLayout()

        b = QPushButton(_("Sign"))
        b.clicked.connect(lambda: self.do_sign(address_e, message_e, signature_e))
        hbox.addWidget(b)

        b = QPushButton(_("Verify"))
        b.clicked.connect(lambda: self.do_verify(address_e, message_e, signature_e))
        hbox.addWidget(b)

        b = QPushButton(_("Close"))
        b.clicked.connect(d.accept)
        hbox.addWidget(b)
        layout.addLayout(hbox, 4, 1)
        d.exec_()


    @protected
    def do_decrypt(self, message_e, pubkey_e, encrypted_e, password):
        try:
            decrypted = self.wallet.decrypt_message(str(pubkey_e.text()), str(encrypted_e.toPlainText()), password)
            message_e.setText(decrypted)
        except BaseException as e:
            traceback.print_exc(file=sys.stdout)
            self.show_warning(str(e))


    def do_encrypt(self, message_e, pubkey_e, encrypted_e):
        message = unicode(message_e.toPlainText())
        message = message.encode('utf-8')
        try:
            encrypted = bitcoin.encrypt_message(message, str(pubkey_e.text()))
            encrypted_e.setText(encrypted)
        except BaseException as e:
            traceback.print_exc(file=sys.stdout)
            self.show_warning(str(e))


    def encrypt_message(self, address = ''):
        d = QDialog(self)
        d.setModal(1)
        d.setWindowTitle(_('Encrypt/decrypt Message'))
        d.setMinimumSize(610, 490)

        layout = QGridLayout(d)

        message_e = QTextEdit()
        layout.addWidget(QLabel(_('Message')), 1, 0)
        layout.addWidget(message_e, 1, 1)
        layout.setRowStretch(2,3)

        pubkey_e = QLineEdit()
        if address:
            pubkey = self.wallet.get_public_keys(address)[0]
            pubkey_e.setText(pubkey)
        layout.addWidget(QLabel(_('Public key')), 2, 0)
        layout.addWidget(pubkey_e, 2, 1)

        encrypted_e = QTextEdit()
        layout.addWidget(QLabel(_('Encrypted')), 3, 0)
        layout.addWidget(encrypted_e, 3, 1)
        layout.setRowStretch(3,1)

        hbox = QHBoxLayout()
        b = QPushButton(_("Encrypt"))
        b.clicked.connect(lambda: self.do_encrypt(message_e, pubkey_e, encrypted_e))
        hbox.addWidget(b)

        b = QPushButton(_("Decrypt"))
        b.clicked.connect(lambda: self.do_decrypt(message_e, pubkey_e, encrypted_e))
        hbox.addWidget(b)

        b = QPushButton(_("Close"))
        b.clicked.connect(d.accept)
        hbox.addWidget(b)

        layout.addLayout(hbox, 4, 1)
        d.exec_()


    def question(self, msg):
        return QMessageBox.question(self, _('Message'), msg, QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes

    def show_message(self, msg):
        QMessageBox.information(self, _('Message'), msg, _('OK'))

    def show_warning(self, msg):
        QMessageBox.warning(self, _('Warning'), msg, _('OK'))

    def password_dialog(self, msg=None, parent=None):
        if parent == None:
            parent = self
        d = QDialog(parent)
        d.setModal(1)
        d.setWindowTitle(_("Enter Password"))
        pw = QLineEdit()
        pw.setEchoMode(2)
        vbox = QVBoxLayout()
        if not msg:
            msg = _('Please enter your password')
        vbox.addWidget(QLabel(msg))
        grid = QGridLayout()
        grid.setSpacing(8)
        grid.addWidget(QLabel(_('Password')), 1, 0)
        grid.addWidget(pw, 1, 1)
        vbox.addLayout(grid)
        vbox.addLayout(Buttons(CancelButton(d), OkButton(d)))
        d.setLayout(vbox)
        run_hook('password_dialog', pw, grid, 1)
        if not d.exec_(): return
        return unicode(pw.text())


    def tx_from_text(self, txt):
        "json or raw hexadecimal"
        txt = txt.strip()
        try:
            txt.decode('hex')
            is_hex = True
        except:
            is_hex = False

        if is_hex:
            try:
                return Transaction(txt)
            except:
                traceback.print_exc(file=sys.stdout)
                QMessageBox.critical(None, _("Unable to parse transaction"), _("Myriadcoin Electrum was unable to parse your transaction"))
                return

        try:
            tx_dict = json.loads(str(txt))
            assert "hex" in tx_dict.keys()
            tx = Transaction(tx_dict["hex"])
            #if tx_dict.has_key("input_info"):
            #    input_info = json.loads(tx_dict['input_info'])
            #    tx.add_input_info(input_info)
            return tx
        except Exception:
            traceback.print_exc(file=sys.stdout)
            QMessageBox.critical(None, _("Unable to parse transaction"), _("Myriadcoin Electrum was unable to parse your transaction"))


    def read_tx_from_qrcode(self):
        from electrum import qrscanner
        try:
            data = qrscanner.scan_qr(self.config)
        except BaseException, e:
            QMessageBox.warning(self, _('Error'), _(e), _('OK'))
            return
        if not data:
            return
        # if the user scanned a bitcoin URI
        if data.startswith("myriadcoin:"):
            self.pay_to_URI(data)
            return
        # else if the user scanned an offline signed tx
        # transactions are binary, but qrcode seems to return utf8...
        data = data.decode('utf8')
        z = bitcoin.base_decode(data, length=None, base=43)
        data = ''.join(chr(ord(b)) for b in z).encode('hex')
        tx = self.tx_from_text(data)
        if not tx:
            return
        self.show_transaction(tx)


    def read_tx_from_file(self):
        fileName = self.getOpenFileName(_("Select your transaction file"), "*.txn")
        if not fileName:
            return
        try:
            with open(fileName, "r") as f:
                file_content = f.read()
        except (ValueError, IOError, os.error), reason:
            QMessageBox.critical(None, _("Unable to read file or no transaction found"), _("Myriadcoin Electrum was unable to open your transaction file") + "\n" + str(reason))

        return self.tx_from_text(file_content)


    def do_process_from_text(self):
        text = text_dialog(self, _('Input raw transaction'), _("Transaction:"), _("Load transaction"))
        if not text:
            return
        tx = self.tx_from_text(text)
        if tx:
            self.show_transaction(tx)

    def do_process_from_file(self):
        tx = self.read_tx_from_file()
        if tx:
            self.show_transaction(tx)

    def do_process_from_txid(self):
        from electrum import transaction
        txid, ok = QInputDialog.getText(self, _('Lookup transaction'), _('Transaction ID') + ':')
        if ok and txid:
            try:
                r = self.network.synchronous_get([('blockchain.transaction.get',[str(txid)])])[0]
            except BaseException as e:
                self.show_message(str(e))
                return
            tx = transaction.Transaction(r)
            self.show_transaction(tx)


    @protected
    def export_privkeys_dialog(self, password):
        if self.wallet.is_watching_only():
            self.show_message(_("This is a watching-only wallet"))
            return

        try:
            self.wallet.check_password(password)
        except Exception as e:
            QMessageBox.warning(self, _('Error'), str(e), _('OK'))
            return

        d = QDialog(self)
        d.setWindowTitle(_('Private keys'))
        d.setMinimumSize(850, 300)
        vbox = QVBoxLayout(d)

        msg = "%s\n%s\n%s" % (_("WARNING: ALL your private keys are secret."),
                              _("Exposing a single private key can compromise your entire wallet!"),
                              _("In particular, DO NOT use 'redeem private key' services proposed by third parties."))
        vbox.addWidget(QLabel(msg))

        e = QTextEdit()
        e.setReadOnly(True)
        vbox.addWidget(e)

        defaultname = 'electrum-private-keys.csv'
        select_msg = _('Select file to export your private keys to')
        hbox, filename_e, csv_button = filename_field(self, self.config, defaultname, select_msg)
        vbox.addLayout(hbox)

        b = OkButton(d, _('Export'))
        b.setEnabled(False)
        vbox.addLayout(Buttons(CancelButton(d), b))

        private_keys = {}
        addresses = self.wallet.addresses(True)
        done = False
        def privkeys_thread():
            for addr in addresses:
                time.sleep(0.1)
                if done:
                    break
                private_keys[addr] = "\n".join(self.wallet.get_private_key(addr, password))
                d.emit(SIGNAL('computing_privkeys'))
            d.emit(SIGNAL('show_privkeys'))

        def show_privkeys():
            s = "\n".join( map( lambda x: x[0] + "\t"+ x[1], private_keys.items()))
            e.setText(s)
            b.setEnabled(True)

        d.connect(d, QtCore.SIGNAL('computing_privkeys'), lambda: e.setText("Please wait... %d/%d"%(len(private_keys),len(addresses))))
        d.connect(d, QtCore.SIGNAL('show_privkeys'), show_privkeys)
        threading.Thread(target=privkeys_thread).start()

        if not d.exec_():
            done = True
            return

        filename = filename_e.text()
        if not filename:
            return

        try:
            self.do_export_privkeys(filename, private_keys, csv_button.isChecked())
        except (IOError, os.error), reason:
            export_error_label = _("Myriadcoin Electrum was unable to produce a private key-export.")
            QMessageBox.critical(None, _("Unable to create csv"), export_error_label + "\n" + str(reason))

        except Exception as e:
            self.show_message(str(e))
            return

        self.show_message(_("Private keys exported."))


    def do_export_privkeys(self, fileName, pklist, is_csv):
        with open(fileName, "w+") as f:
            if is_csv:
                transaction = csv.writer(f)
                transaction.writerow(["address", "private_key"])
                for addr, pk in pklist.items():
                    transaction.writerow(["%34s"%addr,pk])
            else:
                import json
                f.write(json.dumps(pklist, indent = 4))


    def do_import_labels(self):
        labelsFile = self.getOpenFileName(_("Open labels file"), "*.dat")
        if not labelsFile: return
        try:
            f = open(labelsFile, 'r')
            data = f.read()
            f.close()
            for key, value in json.loads(data).items():
                self.wallet.set_label(key, value)
            QMessageBox.information(None, _("Labels imported"), _("Your labels were imported from")+" '%s'" % str(labelsFile))
        except (IOError, os.error), reason:
            QMessageBox.critical(None, _("Unable to import labels"), _("Myriadcoin Electrum was unable to import your labels.")+"\n" + str(reason))


    def do_export_labels(self):
        labels = self.wallet.labels
        try:
            fileName = self.getSaveFileName(_("Select file to save your labels"), 'electrum_labels.dat', "*.dat")
            if fileName:
                with open(fileName, 'w+') as f:
                    json.dump(labels, f)
                QMessageBox.information(None, _("Labels exported"), _("Your labels where exported to")+" '%s'" % str(fileName))
        except (IOError, os.error), reason:
            QMessageBox.critical(None, _("Unable to export labels"), _("Myriadcoin Electrum was unable to export your labels.")+"\n" + str(reason))


    def export_history_dialog(self):
        d = QDialog(self)
        d.setWindowTitle(_('Export History'))
        d.setMinimumSize(400, 200)
        vbox = QVBoxLayout(d)
        defaultname = os.path.expanduser('~/electrum-history.csv')
        select_msg = _('Select file to export your wallet transactions to')
        hbox, filename_e, csv_button = filename_field(self, self.config, defaultname, select_msg)
        vbox.addLayout(hbox)
        vbox.addStretch(1)
        hbox = Buttons(CancelButton(d), OkButton(d, _('Export')))
        vbox.addLayout(hbox)
        run_hook('export_history_dialog', self, hbox)
        self.update()
        if not d.exec_():
            return
        filename = filename_e.text()
        if not filename:
            return
        try:
            self.do_export_history(self.wallet, filename, csv_button.isChecked())
        except (IOError, os.error), reason:
            export_error_label = _("Myriadcoin Electrum was unable to produce a transaction export.")
            QMessageBox.critical(self, _("Unable to export history"), export_error_label + "\n" + str(reason))
            return
        QMessageBox.information(self,_("History exported"), _("Your wallet history has been successfully exported."))


    def do_export_history(self, wallet, fileName, is_csv):
        history = wallet.get_history()
        lines = []
        for item in history:
            tx_hash, confirmations, value, timestamp, balance = item
            if confirmations:
                if timestamp is not None:
                    time_string = format_time(timestamp)
                else:
                    time_string = "unknown"
            else:
                time_string = "pending"

            if value is not None:
                value_string = format_satoshis(value, True)
            else:
                value_string = '--'

            if tx_hash:
                label, is_default_label = wallet.get_label(tx_hash)
                label = label.encode('utf-8')
            else:
                label = ""

            if is_csv:
                lines.append([tx_hash, label, confirmations, value_string, time_string])
            else:
                lines.append({'txid':tx_hash, 'date':"%16s"%time_string, 'label':label, 'value':value_string})

        with open(fileName, "w+") as f:
            if is_csv:
                transaction = csv.writer(f, lineterminator='\n')
                transaction.writerow(["transaction_hash","label", "confirmations", "value", "timestamp"])
                for line in lines:
                    transaction.writerow(line)
            else:
                import json
                f.write(json.dumps(lines, indent = 4))


    def sweep_key_dialog(self):
        d = QDialog(self)
        d.setWindowTitle(_('Sweep private keys'))
        d.setMinimumSize(600, 300)

        vbox = QVBoxLayout(d)
        vbox.addWidget(QLabel(_("Enter private keys")))

        keys_e = QTextEdit()
        keys_e.setTabChangesFocus(True)
        vbox.addWidget(keys_e)

        h, address_e = address_field(self.wallet.addresses(False))
        vbox.addLayout(h)

        vbox.addStretch(1)
        button = OkButton(d, _('Sweep'))
        vbox.addLayout(Buttons(CancelButton(d), button))
        button.setEnabled(False)

        def get_address():
            addr = str(address_e.text())
            if bitcoin.is_address(addr):
                return addr

        def get_pk():
            pk = str(keys_e.toPlainText()).strip()
            if Wallet.is_private_key(pk):
                return pk.split()

        f = lambda: button.setEnabled(get_address() is not None and get_pk() is not None)
        keys_e.textChanged.connect(f)
        address_e.textChanged.connect(f)
        if not d.exec_():
            return

        fee = self.wallet.fee_per_kb(self.config)
        tx = Transaction.sweep(get_pk(), self.network, get_address(), fee)
        if not tx:
            self.show_message(_('No inputs found. (Note that inputs need to be confirmed)'))
            return
        self.show_transaction(tx)


    @protected
    def do_import_privkey(self, password):
        if not self.wallet.has_imported_keys():
            r = QMessageBox.question(None, _('Warning'), '<b>'+_('Warning') +':\n</b><br/>'+ _('Imported keys are not recoverable from seed.') + ' ' \
                                         + _('If you ever need to restore your wallet from its seed, these keys will be lost.') + '<p>' \
                                         + _('Are you sure you understand what you are doing?'), 3, 4)
            if r == 4: return

        text = text_dialog(self, _('Import private keys'), _("Enter private keys")+':', _("Import"))
        if not text: return

        text = str(text).split()
        badkeys = []
        addrlist = []
        for key in text:
            try:
                addr = self.wallet.import_key(key, password)
            except Exception as e:
                badkeys.append(key)
                continue
            if not addr:
                badkeys.append(key)
            else:
                addrlist.append(addr)
        if addrlist:
            QMessageBox.information(self, _('Information'), _("The following addresses were added") + ':\n' + '\n'.join(addrlist))
        if badkeys:
            QMessageBox.critical(self, _('Error'), _("The following inputs could not be imported") + ':\n'+ '\n'.join(badkeys))
        self.update_address_tab()
        self.update_history_tab()


    def settings_dialog(self):
        self.need_restart = False
        d = QDialog(self)
        d.setWindowTitle(_('Preferences'))
        d.setModal(1)
        vbox = QVBoxLayout()
        tabs = QTabWidget()
        gui_widgets = []
        tx_widgets = []
        id_widgets = []

        # language
        lang_help = _('Select which language is used in the GUI (after restart).')
        lang_label = HelpLabel(_('Language') + ':', lang_help)
        lang_combo = QComboBox()
        from electrum.i18n import languages
        lang_combo.addItems(languages.values())
        try:
            index = languages.keys().index(self.config.get("language",''))
        except Exception:
            index = 0
        lang_combo.setCurrentIndex(index)
        if not self.config.is_modifiable('language'):
            for w in [lang_combo, lang_label]: w.setEnabled(False)
        def on_lang(x):
            lang_request = languages.keys()[lang_combo.currentIndex()]
            if lang_request != self.config.get('language'):
                self.config.set_key("language", lang_request, True)
                self.need_restart = True
        lang_combo.currentIndexChanged.connect(on_lang)
        gui_widgets.append((lang_label, lang_combo))

        nz_help = _('Number of zeros displayed after the decimal point. For example, if this is set to 2, "1." will be displayed as "1.00"')
        nz_label = HelpLabel(_('Zeros after decimal point') + ':', nz_help)
        nz = QSpinBox()
        nz.setMinimum(0)
        nz.setMaximum(self.decimal_point)
        nz.setValue(self.num_zeros)
        if not self.config.is_modifiable('num_zeros'):
            for w in [nz, nz_label]: w.setEnabled(False)
        def on_nz():
            value = nz.value()
            if self.num_zeros != value:
                self.num_zeros = value
                self.config.set_key('num_zeros', value, True)
                self.update_history_tab()
                self.update_address_tab()
        nz.valueChanged.connect(on_nz)
        gui_widgets.append((nz_label, nz))

        msg = _('Fee per kilobyte of transaction.') + '\n' \
              + _('If you enable dynamic fees, this parameter will be used as upper bound.')
        fee_label = HelpLabel(_('Transaction fee per kb') + ':', msg)
        fee_e = BTCkBEdit(self.get_decimal_point)
        fee_e.setAmount(self.config.get('fee_per_kb', bitcoin.RECOMMENDED_FEE))
        def on_fee(is_done):
            if self.config.get('dynamic_fees'):
                return
            v = fee_e.get_amount() or 0
            self.config.set_key('fee_per_kb', v, is_done)
            self.update_fee()
        fee_e.editingFinished.connect(lambda: on_fee(True))
        fee_e.textEdited.connect(lambda: on_fee(False))
        tx_widgets.append((fee_label, fee_e))

        dynfee_cb = QCheckBox(_('Dynamic fees'))
        dynfee_cb.setChecked(self.config.get('dynamic_fees', False))
        dynfee_cb.setToolTip(_("Use a fee per kB value recommended by the server."))
        dynfee_sl = QSlider(Qt.Horizontal, self)
        dynfee_sl.setValue(self.config.get('fee_factor', 50))
        dynfee_sl.setToolTip("Fee Multiplier. Min = 50%, Max = 150%")
        tx_widgets.append((dynfee_cb, dynfee_sl))

        def update_feeperkb():
            fee_e.setAmount(self.wallet.fee_per_kb(self.config))
            b = self.config.get('dynamic_fees')
            dynfee_sl.setHidden(not b)
            fee_e.setEnabled(not b)
        def fee_factor_changed(b):
            self.config.set_key('fee_factor', b, False)
            update_feeperkb()
        def on_dynfee(x):
            dynfee = x == Qt.Checked
            self.config.set_key('dynamic_fees', dynfee)
            update_feeperkb()
        dynfee_cb.stateChanged.connect(on_dynfee)
        dynfee_sl.valueChanged[int].connect(fee_factor_changed)
        update_feeperkb()

        msg = _('OpenAlias record, used to receive coins and to sign payment requests.') + '\n\n'\
              + _('The following alias providers are available:') + '\n'\
              + '\n'.join(['https://cryptoname.co/', 'http://xmr.link']) + '\n\n'\
              + 'For more information, see http://openalias.org'
        alias_label = HelpLabel(_('OpenAlias') + ':', msg)
        alias = self.config.get('alias','')
        alias_e = QLineEdit(alias)
        def set_alias_color():
            if not self.config.get('alias'):
                alias_e.setStyleSheet("")
                return
            if self.alias_info:
                alias_addr, alias_name, validated = self.alias_info
                alias_e.setStyleSheet(GREEN_BG if validated else RED_BG)
            else:
                alias_e.setStyleSheet(RED_BG)
        def on_alias_edit():
            alias_e.setStyleSheet("")
            alias = str(alias_e.text())
            self.config.set_key('alias', alias, True)
            if alias:
                self.fetch_alias()
        set_alias_color()
        self.connect(self, SIGNAL('alias_received'), set_alias_color)
        alias_e.editingFinished.connect(on_alias_edit)
        id_widgets.append((alias_label, alias_e))

        # SSL certificate
        msg = ' '.join([
            _('SSL certificate used to sign payment requests.'),
            _('Use setconfig to set ssl_chain and ssl_privkey.'),
        ])
        if self.config.get('ssl_privkey') or self.config.get('ssl_chain'):
            try:
                SSL_identity = paymentrequest.check_ssl_config(self.config)
                SSL_error = None
            except BaseException as e:
                SSL_identity = "error"
                SSL_error = str(e)
        else:
            SSL_identity = ""
            SSL_error = None
        SSL_id_label = HelpLabel(_('SSL certificate') + ':', msg)
        SSL_id_e = QLineEdit(SSL_identity)
        SSL_id_e.setStyleSheet(RED_BG if SSL_error else GREEN_BG if SSL_identity else '')
        if SSL_error:
            SSL_id_e.setToolTip(SSL_error)
        SSL_id_e.setReadOnly(True)
        id_widgets.append((SSL_id_label, SSL_id_e))

        units = ['MYR', 'mMYR', 'uMYR']
        msg = _('Base unit of your wallet.')\
              + '\n1MYR=1000mMYR.\n' \
              + _(' These settings affects the fields in the Send tab')+' '
        unit_label = HelpLabel(_('Base unit') + ':', msg)
        unit_combo = QComboBox()
        unit_combo.addItems(units)
        unit_combo.setCurrentIndex(units.index(self.base_unit()))
        def on_unit(x):
            unit_result = units[unit_combo.currentIndex()]
            if self.base_unit() == unit_result:
                return
            if unit_result == 'MYR':
                self.decimal_point = 8
            elif unit_result == 'mMYR':
                self.decimal_point = 5
            elif unit_result == 'uMYR':
                self.decimal_point = 2
            else:
                raise Exception('Unknown base unit')
            self.config.set_key('decimal_point', self.decimal_point, True)
            self.update_history_tab()
            self.update_receive_tab()
            self.update_address_tab()
            fee_e.setAmount(self.wallet.fee_per_kb(self.config))
            self.update_status()
        unit_combo.currentIndexChanged.connect(on_unit)
        gui_widgets.append((unit_label, unit_combo))

        block_explorers = sorted(block_explorer_info.keys())
        msg = _('Choose which online block explorer to use for functions that open a web browser')
        block_ex_label = HelpLabel(_('Online Block Explorer') + ':', msg)
        block_ex_combo = QComboBox()
        block_ex_combo.addItems(block_explorers)
        block_ex_combo.setCurrentIndex(block_explorers.index(block_explorer(self.config)))
        def on_be(x):
            be_result = block_explorers[block_ex_combo.currentIndex()]
            self.config.set_key('block_explorer', be_result, True)
        block_ex_combo.currentIndexChanged.connect(on_be)
        gui_widgets.append((block_ex_label, block_ex_combo))

        from electrum import qrscanner
        system_cameras = qrscanner._find_system_cameras()
        qr_combo = QComboBox()
        qr_combo.addItem("Default","default")
        for camera, device in system_cameras.items():
            qr_combo.addItem(camera, device)
        #combo.addItem("Manually specify a device", config.get("video_device"))
        index = qr_combo.findData(self.config.get("video_device"))
        qr_combo.setCurrentIndex(index)
        msg = _("Install the zbar package to enable this.\nOn linux, type: 'apt-get install python-zbar'")
        qr_label = HelpLabel(_('Video Device') + ':', msg)
        qr_combo.setEnabled(qrscanner.zbar is not None)
        on_video_device = lambda x: self.config.set_key("video_device", str(qr_combo.itemData(x).toString()), True)
        qr_combo.currentIndexChanged.connect(on_video_device)
        gui_widgets.append((qr_label, qr_combo))

        usechange_cb = QCheckBox(_('Use change addresses'))
        usechange_cb.setChecked(self.wallet.use_change)
        if not self.config.is_modifiable('use_change'): usechange_cb.setEnabled(False)
        def on_usechange(x):
            usechange_result = x == Qt.Checked
            if self.wallet.use_change != usechange_result:
                self.wallet.use_change = usechange_result
                self.wallet.storage.put('use_change', self.wallet.use_change)
        usechange_cb.stateChanged.connect(on_usechange)
        usechange_cb.setToolTip(_('Using change addresses makes it more difficult for other people to track your transactions.'))
        tx_widgets.append((usechange_cb, None))

        showtx_cb = QCheckBox(_('View transaction before signing'))
        showtx_cb.setChecked(self.show_before_broadcast())
        showtx_cb.stateChanged.connect(lambda x: self.set_show_before_broadcast(showtx_cb.isChecked()))
        showtx_cb.setToolTip(_('Display the details of your transactions before signing it.'))
        tx_widgets.append((showtx_cb, None))

        can_edit_fees_cb = QCheckBox(_('Set transaction fees manually'))
        can_edit_fees_cb.setChecked(self.config.get('can_edit_fees', False))
        def on_editfees(x):
            self.config.set_key('can_edit_fees', x == Qt.Checked)
            self.update_fee_edit()
        can_edit_fees_cb.stateChanged.connect(on_editfees)
        can_edit_fees_cb.setToolTip(_('This option lets you edit fees in the send tab.'))
        tx_widgets.append((can_edit_fees_cb, None))

        tabs_info = [
            (tx_widgets, _('Transactions')),
            (gui_widgets, _('Appearance')),
            (id_widgets, _('Identity')),
        ]
        for widgets, name in tabs_info:
            tab = QWidget()
            grid = QGridLayout(tab)
            grid.setColumnStretch(0,1)
            for a,b in widgets:
                i = grid.rowCount()
                if b:
                    grid.addWidget(a, i, 0)
                    grid.addWidget(b, i, 1)
                else:
                    grid.addWidget(a, i, 0, 1, 2)
            tabs.addTab(tab, name)

        vbox.addWidget(tabs)
        vbox.addStretch(1)
        vbox.addLayout(Buttons(CloseButton(d)))
        d.setLayout(vbox)

        # run the dialog
        d.exec_()
        self.disconnect(self, SIGNAL('alias_received'), set_alias_color)

        run_hook('close_settings_dialog')
        if self.need_restart:
            QMessageBox.warning(self, _('Success'), _('Please restart Myriadcoin Electrum to activate the new GUI settings'), _('OK'))



    def run_network_dialog(self):
        if not self.network:
            QMessageBox.warning(self, _('Offline'), _('You are using Myriadcoin Electrum in offline mode.\nRestart Myriadcoin Electrum if you want to get connected.'), _('OK'))
            return
        NetworkDialog(self.wallet.network, self.config, self).do_exec()

    def closeEvent(self, event):
        self.config.set_key("is_maximized", self.isMaximized())
        if not self.isMaximized():
            g = self.geometry()
            self.config.set_key("winpos-qt", [g.left(),g.top(),g.width(),g.height()])
        self.config.set_key("console-history", self.console.history[-50:], True)
        self.wallet.storage.put('accounts_expanded', self.accounts_expanded)
        event.accept()


    def plugins_dialog(self):
        from electrum.plugins import plugins, descriptions, is_available, loader

        self.pluginsdialog = d = QDialog(self)
        d.setWindowTitle(_('Myriadcoin Electrum Plugins'))
        d.setModal(1)

        vbox = QVBoxLayout(d)

        # plugins
        scroll = QScrollArea()
        scroll.setEnabled(True)
        scroll.setWidgetResizable(True)
        scroll.setMinimumSize(400,250)
        vbox.addWidget(scroll)

        w = QWidget()
        scroll.setWidget(w)
        w.setMinimumHeight(len(plugins)*35)

        grid = QGridLayout()
        grid.setColumnStretch(0,1)
        w.setLayout(grid)

        def do_toggle(cb, name, w):
            p = plugins.get(name)
            if p:
                p.disable()
                p.close()
                plugins.pop(name)
            else:
                module = loader(name)
                plugins[name] = p = module.Plugin(self.config, name)
                p.enable()
                p.wallet = self.wallet
                p.load_wallet(self.wallet, self)
                p.init_qt(self.gui_object)
            r = p.is_enabled()
            cb.setChecked(r)
            if w: w.setEnabled(r)

        def mk_toggle(cb, name, w):
            return lambda: do_toggle(cb, name, w)

        for i, descr in enumerate(descriptions):
            name = descr['name']
            p = plugins.get(name)
            if descr.get('registers_wallet_type'):
                continue
            try:
                cb = QCheckBox(descr['fullname'])
                cb.setEnabled(is_available(name, self.wallet))
                cb.setChecked(p is not None and p.is_enabled())
                grid.addWidget(cb, i, 0)
                if p and p.requires_settings():
                    w = p.settings_widget(self)
                    w.setEnabled(p.is_enabled())
                    grid.addWidget(w, i, 1)
                else:
                    w = None
                cb.clicked.connect(mk_toggle(cb, name, w))
                msg = descr['description']
                if descr.get('requires'):
                    msg += '\n\n' + _('Requires') + ':\n' + '\n'.join(map(lambda x: x[1], descr.get('requires')))
                grid.addWidget(HelpButton(msg), i, 2)
            except Exception:
                print_msg("Error: cannot display plugin", name)
                traceback.print_exc(file=sys.stdout)
        grid.setRowStretch(i+1,1)
        vbox.addLayout(Buttons(CloseButton(d)))
        d.exec_()

    def show_account_details(self, k):
        account = self.wallet.accounts[k]

        d = QDialog(self)
        d.setWindowTitle(_('Account Details'))
        d.setModal(1)

        vbox = QVBoxLayout(d)
        name = self.wallet.get_account_name(k)
        label = QLabel('Name: ' + name)
        vbox.addWidget(label)

        vbox.addWidget(QLabel(_('Address type') + ': ' + account.get_type()))

        vbox.addWidget(QLabel(_('Derivation') + ': ' + k))

        vbox.addWidget(QLabel(_('Master Public Key:')))

        text = QTextEdit()
        text.setReadOnly(True)
        text.setMaximumHeight(170)
        vbox.addWidget(text)
        mpk_text = '\n'.join( account.get_master_pubkeys() )
        text.setText(mpk_text)
        vbox.addLayout(Buttons(CloseButton(d)))
        d.exec_()
