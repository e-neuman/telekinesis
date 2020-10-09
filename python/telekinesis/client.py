import logging
import time
import traceback
import os
import asyncio
import bson
import zlib
from collections import deque, OrderedDict

import websockets
import ujson

from .cryptography import PrivateKey, PublicKey, SharedKey, Token, InvalidSignature

class Connection:
    def __init__(self, session, url='ws://localhost:8776'):
        self.MAX_PAYLOAD_LEN = 2**19
        self.MAX_COMPRESSION_LEN = 2**19
        self.SUGGESTED_MAX_OUTBOX = 2**4
        self.RESEND_TIMEOUT = 2 # sec
        self.MAX_SEND_RETRIES = 3

        self.session = session
        self.url = url
        self.logger = logging.getLogger(__name__)
        self.websocket = None
        self.t_offset = 0
        self.broker_id = None

        self.is_connnecting_lock = asyncio.Event()
        self.awaiting_ack = OrderedDict()
        self.out_queue = deque()

        session.connections.add(self)

        self.listener = asyncio.get_event_loop().create_task(self.listen())

    async def reconnect(self):
        if self.is_connnecting_lock.is_set():
            self.is_connnecting_lock.clear()
            if self.listener:
                self.listener.cancel()

            self.listener = asyncio.get_event_loop().create_task(self.listen())

        await self.is_connnecting_lock.wait()

    async def _connect(self):
        if self.websocket:
            await self.websocket.close()

        self.websocket = await websockets.connect(self.url)
        
        challenge = await self.websocket.recv()
        t_broker = int.from_bytes(challenge[-4:], 'big')
        
        self.t_offset = int(time.time()) - t_broker
        signature = self.session.session_key.sign(challenge)

        pk = self.session.session_key.public_serial().encode()

        sent_challenge = os.urandom(32)
        await self.websocket.send(signature + pk + sent_challenge)

        m = await asyncio.wait_for(self.websocket.recv(), 15)

        broker_signature, broker_id = m[:64], m[64:152].decode()
        PublicKey(broker_id).verify(broker_signature, sent_challenge)

        self.broker_id = broker_id
        
        headers = []
        for token, prev_token in self.session.issued_tokens.values():
            headers.append(('token', ('issue', token.encode(), prev_token and prev_token.encode())))
        for channel in self.session.channels.values():
            listen_dict = channel.route.to_dict()
            listen_dict['is_public'] = channel.is_public
            listen_dict.pop('tokens')
            headers.append(('listen', listen_dict))
        await self.send(headers)

        self.is_connnecting_lock.set()

        return self

    async def send(self, header, payload=b'', bundle_id=None, ack_message_id=None):
        for action, _ in header:
            self.logger.info('%s Sending %s: %s', self.session.session_key.public_serial()[:4],
                             action, len(payload) if action == 'send' else 0)
        
        def encode(header, payload, bundle_id, message_id, retry):
            h = ujson.dumps(header).encode()
            r = (retry).to_bytes(1, 'big') + (message_id or b'')
            m = len(h).to_bytes(2, 'big') + len(r+payload).to_bytes(3, 'big') + h + r + payload
            t = int(time.time() - self.t_offset - 4).to_bytes(4, 'big')
            s = self.session.session_key.sign(t + m)
            return s, t + m

        s, mm = encode(header, payload, bundle_id, ack_message_id, 255 if ack_message_id else 0)
        message_id = s
        
        expect_ack = 'send' in set(a for a, _ in header) and not ack_message_id
        if expect_ack:
            lock = asyncio.Event()
            if not self.awaiting_ack:
                lock.set()
            self.awaiting_ack[message_id or s] = (header, bundle_id, lock)

        for retry in range(self.MAX_SEND_RETRIES):
            if not self.websocket or self.websocket.closed:
                if self.is_connnecting_lock.is_set():
                    await self.reconnect()
                else:
                    await self.is_connnecting_lock.wait()

            await self.websocket.send(s + mm)
            
            if not expect_ack or await self.expect_ack(message_id, lock):
                return

            if retry < (self.MAX_SEND_RETRIES - 1): 
                s, mm = encode(header, payload, bundle_id, message_id, retry)

        self.clear(bundle_id)
        raise Exception('Max send retries reached')

    async def expect_ack(self, message_id, lock):
        await lock.wait()
        if message_id not in self.awaiting_ack:
            return True
        lock.clear()
        try:
            await asyncio.wait_for(lock.wait(), self.RESEND_TIMEOUT)
        except asyncio.TimeoutError:
            lock.set()
        if message_id not in self.awaiting_ack:
            return True

        return False

    def clear(self, bundle_id):
        if bundle_id:
            for k, v in self.awaiting_ack.items():
                if v[2] == bundle_id:
                    self.awaiting_ack.pop(k)

    async def listen(self):
        n_tries = 0
        while True:
            try:
                await self._connect()
                while True:
                    await self.recv()
                    n_tries = 0
            except Exception:
                self.logger.error('Connection.listen', exc_info=True)
            
            self.is_connnecting_lock.clear()
            await asyncio.sleep(1)

            if n_tries > 10:
                raise Exception('Max tries reached')
            n_tries += 1
            
    async def recv(self):
        if not self.websocket or self.websocket.closed:
            if self.is_connnecting_lock.is_set():
                await self.reconnect()
            else:
                await self.is_connnecting_lock.wait()

        message = await self.websocket.recv()

        signature, timestamp = message[:64], int.from_bytes(message[64:68], 'big')
        
        if self.session.check_no_repeat(signature, timestamp + self.t_offset):

            len_h, len_p = [int.from_bytes(x, 'big') for x in [message[68:70], message[70:73]]]
            header = ujson.loads(message[73:73+len_h])
            full_payload = message[73+len_h:73+len_h+len_p]
            for action, content in header:
                self.logger.info('%s Received %s: %s', self.session.session_key.public_serial()[:4], 
                                 action, len(full_payload) if action == 'send' else 0)
                if action == 'send':
                    source, destination = Route(**content['source']), Route(**content['destination'])
                    PublicKey(source.session).verify(signature, message[64:])
                    if self.session.channels.get(destination.channel):
                        channel = self.session.channels.get(destination.channel)
                        if full_payload[0] == 255:
                            self.ack(source.session, full_payload[1:65])
                        else:
                            ret_signature = signature if full_payload[0] == 0 else full_payload[1:65]
                            payload = full_payload[1:] if full_payload[0] == 0 else full_payload[65:]
                            await self.send((('send', {'destination': content['source'], 'source': content['destination']}), ), 
                                            b'', None, ret_signature)
                            # print(self.session.session_key.public_serial()[:4], 'sent ack', ret_signature[:4])
                            channel.handle_message(source, destination, payload)

    def ack(self, source_id, message_id):
        # print(self.session.session_key.public_serial()[:4], 'received ack', message_id[:4], 'from', source_id[:4])
        header = self.awaiting_ack.get(message_id, [[]])[0]
        for action, content in header:
            if action == 'send':
                if content['destination']['session'] == source_id:
                    t = self.awaiting_ack.pop(message_id)
                    t[2].set()
                    if self.awaiting_ack:
                        self.awaiting_ack[list(self.awaiting_ack.keys())[0]][2].set()

class Session:
    def __init__(self, session_key_file=None, compile_signatures=True):
        self.session_key = PrivateKey(session_key_file)
        self.channels = {}
        self.connections = set()
        self.compile_signatures = compile_signatures
        self.seen_messages = (set(), set(), 0)
        self.issued_tokens = {}

    def check_no_repeat(self, signature, timestamp):
        now = int(time.time())

        lead = now//60%2
        if self.seen_messages[2] != lead:
            self.seen_messages[lead].clear()

        if (now - 60) <= timestamp <= now:
            if signature not in self.seen_messages[0].union(self.seen_messages[1]):
                self.seen_messages[lead].add(signature)
                return True
        return False

    def issue_token(self, target, receiver, max_depth=None):
        if isinstance(target, Token):
            token_type = 'extension'
            prev_token = target
            asset = target.signature
        else:
            token_type = 'root'
            prev_token = None
            asset = target

        token = Token(self.session_key.public_serial(), [x.broker_id for x in self.connections], 
                      receiver, asset, token_type, max_depth)
        signature = token.sign(self.session_key)
        
        self.issued_tokens[signature] = token, prev_token
        return ('token', ('issue', token.encode(), prev_token and prev_token.encode()))

    def revoke_token(self, token_id):
        self.issued_tokens.pop(token_id)
        return ('token', ('revoke', token_id))
    
    def extend_route(self, route, receiver, max_depth=None):

        if route.session == self.session_key.public_serial():
            token_header = self.issue_token(route.channel, receiver, max_depth)
            route.tokens = [token_header[1][1]]
            return token_header

        for i, enc_token in enumerate(route.tokens):
            token = Token.decode(enc_token)
            if token.receiver == self.session_key.public_serial():
                route.tokens = route.tokens[:i+1]

        token = Token.decode(route.tokens[-1], False)
        token_header = self.issue_token(token, receiver, max_depth)
        route.tokens.append(token_header[1][1])
        return token_header
    
    def clear(self, bundle_id):
        for connection in self.connections:
            connection.clear(bundle_id)

    async def send(self, header, payload=b'', bundle_id=None):
        for connection in self.connections:
            await connection.send(header, payload, bundle_id)

class Channel:
    def __init__(self, session, channel_key_file=None, is_public=False):
        self.session = session
        self.channel_key = PrivateKey(channel_key_file)
        self.is_public = is_public
        self.route = Route(list(set(c.broker_id for c in self.session.connections)),
                           self.session.session_key.public_serial(),
                           self.channel_key.public_serial())
        self.header_buffer = []
        self.chunks = {}
        self.messages = deque()
        self.lock = asyncio.Event()
        session.channels[self.channel_key.public_serial()] = self

        self.telekinesis = None
    
    def route_dict(self):
        return self.route.to_dict()

    def handle_message(self, source, destination, raw_payload):
        if self.validate_token_chain(source.session, destination.tokens):
            shared_key = SharedKey(self.channel_key, PublicKey(source.channel))
            payload = shared_key.decrypt(raw_payload[16:], raw_payload[:16])

            if payload[:4] == b'\x00'*4:
                if payload[4] == 0:
                    self.messages.appendleft((source, bson.loads(payload[5:])))
                elif payload[4] == 255:
                    self.messages.appendleft((source, bson.loads(zlib.decompress(payload[5:]))))
                else:
                    raise Exception(f'Received message with different encoding')

                self.lock.set()
            else:
                ir, nr, mid, chunk = payload[:2], payload[2:4], payload[4:8], payload[8:]
                i, n = int.from_bytes(ir, 'big'), int.from_bytes(nr, 'big')
                if mid not in self.chunks:
                    self.chunks[mid] = {}
                self.chunks[mid][i] = chunk

                if len(self.chunks[mid]) == n:
                    chunks = self.chunks.pop(mid)
                    payload = b''.join(chunks[ii] for ii in range(n))
                    if payload[0] == 0:
                        self.messages.appendleft((source, bson.loads(payload[1:])))
                    elif payload[0] == 255:
                        self.messages.appendleft((source, bson.loads(zlib.decompress(payload[1:]))))
                    else:
                        raise Exception('Received message with different encoding')
                    self.lock.set()
    
    async def recv(self):
        if not self.messages:
            self.lock.clear()
            await self.lock.wait()
        
        return self.messages.pop()
    
    def listen(self):
        listen_dict = self.route.to_dict()
        listen_dict['is_public'] = self.is_public
        listen_dict.pop('tokens')
        self.header_buffer.append(('listen', listen_dict))

        return self
    
    async def send(self, destination, payload_obj):
        def encrypt_slice(payload, max_payload, shared_key, mid, n, i):
            if i < n:
                if n == 1:
                    chunk = b'\x00'*4 + payload
                else:
                    if n > 2**16:
                        raise Exception(f'Payload size {len(payload)/2**20} MiB too large')
                    chunk = i.to_bytes(2, 'big') + n.to_bytes(2, 'big') + mid + payload[i*max_payload:(i+1)*max_payload]
                
                nonce = os.urandom(16)
                yield nonce + shared_key.encrypt(chunk, nonce)
                yield from encrypt_slice(payload, max_payload, shared_key, mid, n, i+1)
        
        async def execute(header, encrypted_slice_generator, mid):
            for encrypted_slice in encrypted_slice_generator:
                await self.execute(header, encrypted_slice, mid)

        source_route = self.route.clone()
        self.header_buffer.append(self.session.extend_route(source_route, destination.session))
        self.listen()
        
        payload = bson.dumps(payload_obj)

        max_compression = list(self.session.connections)[0].MAX_COMPRESSION_LEN

        if len(payload) < max_compression:
            payload = b'\xff' + zlib.compress(payload)
        else:
            payload = b'\x00' + payload

        conn = list(self.session.connections)[0]

        max_payload = conn.MAX_PAYLOAD_LEN
        max_outbox = conn.SUGGESTED_MAX_OUTBOX * len(self.session.connections)

        mid = os.urandom(4)
        shared_key = SharedKey(self.channel_key, PublicKey(destination.channel))

        header = ('send', {'source': source_route.to_dict(), 'destination': destination.to_dict()})

        n = (len(payload) - 1) // max_payload + 1
        n_tasks = min(n, max_outbox)
        gen = encrypt_slice(payload, max_payload, shared_key, mid, n, 0)
        
        try:
            await asyncio.gather(*(execute(header, gen, mid) for _ in range(n_tasks)))
        except asyncio.CancelledError:
            self.session.clear(mid)
            raise asyncio.CancelledError

        return self

    async def execute(self, header=None, payload=b'', bundle_id=None):
        await self.session.send([h for h in (self.header_buffer + [header]) if h], payload, bundle_id)
        self.header_buffer = []

        return self

    def __await__(self):
        return self.execute().__await__()

    def close(self):
        self.header_buffer.append(('close', self.route.to_dict()))

        return self

    def validate_token_chain(self, source_id, tokens):
        if self.is_public or (source_id == self.session.session_key.public_serial()):
            return True
        if not tokens:
            return False

        asset = self.channel_key.public_serial()
        last_receiver = self.session.session_key.public_serial()
        max_depth = None
        
        for depth, token_string in enumerate(tokens):
            try:
                token = Token.decode(token_string)
            except InvalidSignature:
                return False
            if (token.asset == asset) and (token.issuer == last_receiver):
                if token.issuer == self.session.session_key.public_serial():
                    if token.signature not in self.session.issued_tokens:
                        return False
                if token.max_depth:
                    if not max_depth or (token.max_depth + depth) < max_depth:
                        max_depth = token.max_depth + depth
                if not max_depth or depth <= max_depth:
                    last_receiver = token.receiver
                    asset = token.signature
                    if last_receiver == source_id:
                        return True
                    continue
            return False
        return False

class Route:
    def __init__(self, brokers, session, channel, tokens=None):
        self.brokers = brokers
        self.session = session
        self.channel = channel
        self.tokens = tokens or []

    def to_dict(self):
        return {
            'brokers': self.brokers,
            'session': self.session,
            'channel': self.channel,
            'tokens': self.tokens
        }

    def clone(self):
        return Route(**self.to_dict())
