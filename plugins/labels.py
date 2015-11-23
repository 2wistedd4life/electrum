import socket
import requests
import threading
import hashlib
import json
import sys
import traceback
from functools import partial

import aes
import base64

import electrum
from electrum.plugins import BasePlugin, hook
from electrum.i18n import _


class Plugin(BasePlugin):

    def __init__(self, parent, config, name):
        BasePlugin.__init__(self, parent, config, name)
        self.target_host = 'sync.bytesized-hosting.com:9090'
        self.wallets = {}

    def encode(self, wallet, msg):
        password, iv, wallet_id = self.wallets[wallet]
        encrypted = electrum.bitcoin.aes_encrypt_with_iv(password, iv,
                                                         msg.encode('utf8'))
        return base64.b64encode(encrypted)

    def decode(self, wallet, message):
        password, iv, wallet_id = self.wallets[wallet]
        decoded = base64.b64decode(message)
        decrypted = electrum.bitcoin.aes_decrypt_with_iv(password, iv, decoded)
        return decrypted.decode('utf8')

    def get_nonce(self, wallet):
        # nonce is the nonce to be used with the next change
        nonce = wallet.storage.get('wallet_nonce')
        if nonce is None:
            nonce = 1
            self.set_nonce(wallet, nonce)
        return nonce

    def set_nonce(self, wallet, nonce, force_write=True):
        self.print_error("set", wallet.basename(), "nonce to", nonce)
        wallet.storage.put("wallet_nonce", nonce, force_write)

    @hook
    def set_label(self, wallet, item, label):
        if not wallet in self.wallets:
            return
        nonce = self.get_nonce(wallet)
        wallet_id = self.wallets[wallet][2]
        bundle = {"walletId": wallet_id,
                  "walletNonce": nonce,
                  "externalId": self.encode(wallet, item),
                  "encryptedLabel": self.encode(wallet, label)}
        t = threading.Thread(target=self.do_request,
                             args=["POST", "/label", False, bundle])
        t.setDaemon(True)
        t.start()
        # Caller will write the wallet
        self.set_nonce(wallet, nonce + 1, force_write=False)

    def do_request(self, method, url = "/labels", is_batch=False, data=None):
        url = 'https://' + self.target_host + url
        kwargs = {'headers': {}}
        if method == 'GET' and data:
            kwargs['params'] = data
        elif method == 'POST' and data:
            kwargs['data'] = json.dumps(data)
            kwargs['headers']['Content-Type'] = 'application/json'
        response = requests.request(method, url, **kwargs)
        if response.status_code != 200:
            raise BaseException(response.status_code, response.text)
        response = response.json()
        if "error" in response:
            raise BaseException(response["error"])
        return response

    def push_thread(self, wallet):
        wallet_id = self.wallets[wallet][2]
        bundle = {"labels": [],
                  "walletId": wallet_id,
                  "walletNonce": self.get_nonce(wallet)}
        for key, value in wallet.labels.iteritems():
            try:
                encoded_key = self.encode(wallet, key)
                encoded_value = self.encode(wallet, value)
            except:
                self.print_error('cannot encode', repr(key), repr(value))
                continue
            bundle["labels"].append({'encryptedLabel': encoded_value,
                                     'externalId': encoded_key})
        self.do_request("POST", "/labels", True, bundle)

    def pull_thread(self, wallet, force):
        wallet_id = self.wallets[wallet][2]
        nonce = 1 if force else self.get_nonce(wallet) - 1
        self.print_error("asking for labels since nonce", nonce)
        try:
            response = self.do_request("GET", ("/labels/since/%d/for/%s" % (nonce, wallet_id) ))
            if response["labels"] is None:
                self.print_error('no new labels')
                return
            result = {}
            for label in response["labels"]:
                try:
                    key = self.decode(wallet, label["externalId"])
                    value = self.decode(wallet, label["encryptedLabel"])
                except:
                    continue
                try:
                    json.dumps(key)
                    json.dumps(value)
                except:
                    self.print_error('error: no json', key)
                    continue
                result[key] = value

            for key, value in result.items():
                if force or not wallet.labels.get(key):
                    wallet.labels[key] = value

            self.print_error("received %d labels" % len(response))
            # do not write to disk because we're in a daemon thread
            wallet.storage.put('labels', wallet.labels, False)
            self.set_nonce(wallet, response["nonce"] + 1, False)
            self.on_pulled(wallet)

        except Exception as e:
            traceback.print_exc(file=sys.stderr)
            self.print_error("could not retrieve labels")





from PyQt4.QtGui import *
from PyQt4.QtCore import *
import PyQt4.QtCore as QtCore
import PyQt4.QtGui as QtGui
from electrum_gui.qt import HelpButton, EnterButton
from electrum_gui.qt.util import ThreadedButton, Buttons, CancelButton, OkButton

class QtPlugin(Plugin):

    def __init__(self, *args):
        Plugin.__init__(self, *args)
        self.obj = QObject()

    def requires_settings(self):
        return True

    def settings_widget(self, window):
        return EnterButton(_('Settings'),
                           partial(self.settings_dialog, window))

    def settings_dialog(self, window):
        d = QDialog(window)
        vbox = QVBoxLayout(d)
        layout = QGridLayout()
        vbox.addLayout(layout)
        layout.addWidget(QLabel("Label sync options: "), 2, 0)
        self.upload = ThreadedButton("Force upload",
                                     partial(self.push_thread, window.wallet),
                                     self.done_processing)
        layout.addWidget(self.upload, 2, 1)
        self.download = ThreadedButton("Force download",
                                       partial(self.pull_thread, window.wallet, True),
                                       self.done_processing)
        layout.addWidget(self.download, 2, 2)
        self.accept = OkButton(d, _("Done"))
        vbox.addLayout(Buttons(CancelButton(d), self.accept))
        if d.exec_():
            return True
        else:
            return False

    def on_pulled(self, wallet):
        self.obj.emit(SIGNAL('labels_changed'), wallet)

    def done_processing(self):
        QMessageBox.information(None, _("Labels synchronised"),
                                _("Your labels have been synchronised."))

    @hook
    def on_new_window(self, window):
        window.connect(window.app, SIGNAL('labels_changed'), window.update_tabs)
        wallet = window.wallet
        nonce = self.get_nonce(wallet)
        self.print_error("wallet", wallet.basename(), "nonce is", nonce)
        mpk = ''.join(sorted(wallet.get_master_public_keys().values()))
        if not mpk:
            return
        password = hashlib.sha1(mpk).digest().encode('hex')[:32]
        iv = hashlib.sha256(password).digest()[:16]
        wallet_id = hashlib.sha256(mpk).digest().encode('hex')
        self.wallets[wallet] = (password, iv, wallet_id)
        # If there is an auth token we can try to actually start syncing
        t = threading.Thread(target=self.pull_thread, args=(wallet, False))
        t.setDaemon(True)
        t.start()

    @hook
    def on_close_window(self, window):
        self.wallets.pop(window.wallet)

