"""Synthetic recoverable trash and ambiguous-response checks; no live accounts."""
import json
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from imap_mail.trash import TrashService
from imap_mail.core import DraftError
import test_flags as fixtures

class TrashReader(fixtures.FlagsReader):
    capabilities = ('MOVE',)
    def folders(self):
        return {'folders': [{'name':'Trash','flags':['\\Trash'],'selectable':True}]}
    def uid(self, command, *args):
        if command == 'MOVE':
            self.server.moves += 1
            if self.server.timeout_before:
                raise TimeoutError('private failure')
            for uid in args[0].split(','):
                raw = self.server.messages[self.mailbox].pop(uid)
                self.server.messages['Trash'][uid] = raw
                self.server.flags[('Trash',uid)] = []
            if self.server.timeout_after:
                raise TimeoutError('private failure')
            return 'OK', [b'moved']
        if command == 'SEARCH':
            if args[1] == 'UID':
                ids = [args[2]] if args[2] in self.server.messages[self.mailbox] else []
            else:
                needle = args[3].strip('"').encode()
                ids = [u for u,b in self.server.messages[self.mailbox].items() if needle in b]
            return 'OK', [' '.join(ids).encode()]
        return super().uid(command, *args)

class TrashTests(unittest.TestCase):
    json = fixtures.FlagTests.json
    save_config = fixtures.FlagTests.save_config
    error = fixtures.FlagTests.error
    def setUp(self):
        fixtures.FlagTests.setUp(self)
        self.entry['trash_write'] = True
        self.save_config()
        self.server.validity['Trash'] = '11'
        self.server.messages['Trash'] = {}
        self.server.moves = 0
        for uid, raw in self.server.messages['INBOX'].items():
            self.server.messages['INBOX'][uid] = b'Message-ID: <'+uid.encode()+b'@example.test>\r\n'+raw
        self.service = TrashService(config_path=self.config_path, reader_factory=lambda p: TrashReader(self.server,p))
    def preview(self):
        return self.service.trash_preview('one',[self.ref])
    def apply(self,p):
        return self.service.trash_apply(p['preview_id'],p['preview_hash'])
    def test_readonly_preview_and_verified_recoverable_move(self):
        p=self.preview(); self.assertEqual(self.server.moves,0)
        r=self.apply(p); self.assertEqual(r['status'],'verified')
        self.assertNotIn('13',self.server.messages['INBOX'])
        self.assertIn('14',self.server.messages['INBOX'])
        self.assertIn('13',self.server.messages['Trash'])
        self.assertEqual(r['permanent_deletions'],0)
        self.apply(p); self.assertEqual(self.server.moves,1)
    def test_lost_success_response_is_verified_without_replay(self):
        p=self.preview(); self.server.timeout_after=True
        self.assertEqual(self.apply(p)['status'],'verified')
        self.apply(p); self.assertEqual(self.server.moves,1)
    def test_timeout_before_move_never_replayed_or_new_preview(self):
        p=self.preview(); self.server.timeout_before=True
        self.assertEqual(self.apply(p)['status'],'uncertain')
        self.apply(p); self.assertEqual(self.server.moves,1)
        self.error('TRASH_SOURCE_OPERATION_UNRESOLVED',self.preview)
    def test_changed_source_refused_before_move(self):
        p=self.preview(); self.server.messages['INBOX']['13'] += b'changed'
        self.error('TRASH_SOURCE_CHANGED',lambda:self.apply(p))
        self.assertEqual(self.server.moves,0)
    def test_wrong_hash_refused(self):
        p=self.preview()
        self.error('PREVIEW_HASH_MISMATCH', lambda:self.service.trash_apply(p['preview_id'],'0'*64))
        self.assertEqual(self.server.moves,0)
    def test_permission_and_identity_are_pinned(self):
        self.entry.pop('trash_write');self.save_config()
        self.error('TRASH_NOT_AUTHORIZED',self.preview)
    def test_uidvalidity_change_refused(self):
        p=self.preview();self.server.validity['INBOX']='8'
        self.error('UIDVALIDITY_CHANGED',lambda:self.apply(p))
        self.assertEqual(self.server.moves,0)
    def test_no_move_support_has_no_fallback(self):
        original=TrashReader.capabilities
        try:
            TrashReader.capabilities=()
            self.error('TRASH_REQUIRES_UID_MOVE',self.preview)
            self.assertEqual(self.server.moves,0)
        finally: TrashReader.capabilities=original
    def test_status_after_permission_revoked_is_readonly(self):
        p=self.preview();self.apply(p)
        self.entry.pop('trash_write');self.save_config()
        r=self.service.trash_status(p['preview_id'])
        self.assertEqual(r['status'],'verified');self.assertEqual(self.server.moves,1)
