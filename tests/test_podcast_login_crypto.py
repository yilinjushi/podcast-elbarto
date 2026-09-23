import base64
import json
import os
import unittest
from unittest.mock import patch
from podcast_login_crypto import seal,open_envelope,LoginCryptoError,MAX_BYTES


class LoginCryptoTests(unittest.TestCase):
    def setUp(self):
        self.key=base64.b64encode(b'k'*32).decode()
        self.payload={'kind':'state','run_id':'123456','expires_at':1100,'storageState':{'cookies':[{'secret':'private-value'}]}}
        self.envelope=seal(self.payload,key=self.key,now=1000)

    def decrypt(self,envelope=None,**kwargs):
        options={'expected_kind':'state','run_id':'123456','now':1000,'key':self.key};options.update(kwargs)
        return open_envelope(self.envelope if envelope is None else envelope,**options)

    def test_roundtrip_random_nonce_and_environment_key(self):
        self.assertEqual(self.decrypt(),self.payload)
        self.assertNotEqual(self.envelope['nonce'],seal(self.payload,key=self.key,now=1000)['nonce'])
        with patch.dict(os.environ,{'PODCAST_LOGIN_KEY':self.key}):
            self.assertEqual(open_envelope(json.dumps(self.envelope),expected_kind='state',run_id='123456',now=1000),self.payload)

    def test_tamper_wrong_key_run_kind_and_expiry_fail(self):
        altered=dict(self.envelope);data=bytearray(base64.b64decode(altered['ciphertext']));data[0]^=1;altered['ciphertext']=base64.b64encode(data).decode()
        for envelope,options in [(altered,{}),(self.envelope,{'key':base64.b64encode(b'x'*32).decode()}),(self.envelope,{'run_id':'654321'}),(self.envelope,{'expected_kind':'connection'}),(self.envelope,{'now':1100})]:
            with self.assertRaisesRegex(LoginCryptoError,'^invalid_login_envelope$'):self.decrypt(envelope,**options)

    def test_bad_envelope_versions_base64_lengths_and_duplicates_fail(self):
        for changes in ({'version':2},{'version':True},{'nonce':'***'},{'nonce':base64.b64encode(b'a'*11).decode()},{'ciphertext':base64.b64encode(b'a'*15).decode()},{'extra':'value'}):
            with self.assertRaises(LoginCryptoError):self.decrypt({**self.envelope,**changes})
        with self.assertRaises(LoginCryptoError):self.decrypt('{"version":1,"version":1}')
        with self.assertRaises(LoginCryptoError):self.decrypt(key=base64.b64encode(b'x'*31).decode())

    def test_invalid_payloads_and_size_limit_fail_without_content(self):
        for changes in ({'run_id':123},{'run_id':'12/3'},{'kind':'other'},{'expires_at':True},{'expires_at':float('inf')},{'expires_at':999},{'data':'x'*MAX_BYTES}):
            with self.assertRaisesRegex(LoginCryptoError,'^invalid_login_envelope$'):seal({**self.payload,**changes},key=self.key,now=1000)


if __name__=='__main__':unittest.main()
