#!/usr/bin/env python2
# -*- coding: utf-8 -*-
from __future__ import print_function

import itertools as it, operator as op, functools as ft
from contextlib import contextmanager, closing
from collections import namedtuple, defaultdict
from subprocess import Popen, PIPE, STDOUT
import os, sys, types, re, base64, struct
import socket, fcntl, stat, tempfile, random, warnings

import netaddr


libnacl = nacl = None
try: import libnacl
except ImportError:
	try: import nacl
	except ImportError:
		raise ImportError( 'Either libnacl or pynacl module'
			' is required for this tool, neither one can be imported.' )

key_id_len = 28
sig_len = 64
msg_data_fmt = '!{}sd'.format(key_id_len)
msg_data_len = struct.calcsize(msg_data_fmt)
msg_fmt = '{}s{}s'.format(msg_data_len, sig_len)
msg_len = struct.calcsize(msg_fmt)

default_bind = '::'
default_port = 5533

update_mtime_tries = 5
if libnacl:
	from libnacl.sign import Signer, Verifier
	from libnacl.utils import rand_nonce

	b64_encode = base64.urlsafe_b64encode
	b64_decode = lambda s:\
		base64.urlsafe_b64decode(s) if '-' in s or '_' in s else s.decode('base64')

	def key_encode(key):
		if isinstance(key, Signer): string = key.seed
		elif isinstance(key, Verifier): string = key.vk
		else: raise ValueError(key)
		return b64_encode(string)

	def key_decode_sk(string): return Signer(b64_decode(string))
	def key_decode_vk(string): return Verifier(b64_decode(string).encode('hex'))

	def key_get_vk(key): return Verifier(key.hex_vk())

	def key_get_id(key):
		if isinstance(key, Signer): key = key_get_vk(key)
		return '{{:>{}s}}'.format(key_id_len).format(
			b64_encode(libnacl.crypto_hash_sha256(key.vk))[:key_id_len] )

	def key_sign(key, msg_data):
		return key.signature(msg_data)

	def key_sign_check(key, msg_data, msg_sig):
		try: key.verify(msg_sig + msg_data)
		except ValueError: return False
		else: return True

	def key_generate():
		return libnacl.sign.Signer()

if nacl:
	with warnings.catch_warnings(record=True): # cffi warnings
		from nacl.exceptions import BadSignatureError
		from nacl.signing import SigningKey, VerifyKey
		from nacl.hash import sha256

	class URLSafeBase64Encoder(object): # in upstream PyNaCl post-0.2.3
		encode = staticmethod(lambda d: base64.urlsafe_b64encode(d))
		decode = staticmethod(lambda d: base64.urlsafe_b64decode(d))

	def key_encode(key):
		return key.encode(URLSafeBase64Encoder)

	def key_decode_sk(string): return SigningKey(string, URLSafeBase64Encoder)
	def key_decode_vk(string): return VerifyKey(string, URLSafeBase64Encoder)

	def key_get_vk(key): return key.verify_key

	def key_get_id(key):
		if isinstance(key, SigningKey): key = key.verify_key
		return '{{:>{}s}}'.format(key_id_len).format(
			sha256(key.encode(), URLSafeBase64Encoder)[:key_id_len] )

	def key_sign(key, msg_data):
		return key.sign(msg_data).signature

	def key_sign_check(key, msg_data, msg_sig):
		try: key.verify(msg_data, msg_sig)
		except BadSignatureError: return False
		else: return True

	def key_generate(): return SigningKey.generate()

def key_test( msg='test',
		key_enc='pbb6wrDXlLWOFMXYH4a9YHh7nGGD1VnStVYQBe9MyVU=' ):
	from hashlib import sha256
	key = key_generate()
	assert key_sign_check(key_get_vk(key), msg, key_sign(key, msg))
	key = key_decode_sk(key_enc)
	key_vk = key_get_vk(key)
	sign = key_sign(key, msg)
	assert key_sign_check(key_vk, msg, sign)
	print(sha256(''.join([
		key_encode(key), key_encode(key_vk),
		key_encode(key_decode_vk(key_encode(key_vk))),
		key_get_id(key), key_get_id(key_vk),
		msg, sign ])).hexdigest())


class AddressError(Exception): pass

def get_socket_info( host,
		port=0, family=0, socktype=0, protocol=0,
		force_unique_address=None, pick_random=False ):
	log_params = [port, family, socktype, protocol]
	log.debug('Resolving addr: %r (params: %s)', host, log_params)
	host = re.sub(r'^\[|\]$', '', host)
	try:
		addrinfo = socket.getaddrinfo(host, port, family, socktype, protocol)
		if not addrinfo: raise socket.gaierror('No addrinfo for host: {}'.format(host))
	except (socket.gaierror, socket.error) as err:
		raise AddressError( 'Failed to resolve host:'
			' {!r} (params: {}) - {} {}'.format(host, log_params, type(err), err) )

	ai_af, ai_addr = set(), list()
	for family, _, _, hostname, addr in addrinfo:
		ai_af.add(family)
		ai_addr.append((addr[0], family))

	if pick_random: return random.choice(ai_addr)

	if len(ai_af) > 1:
		af_names = dict((v, k) for k,v in vars(socket).viewitems() if k.startswith('AF_'))
		ai_af_names = list(af_names.get(af, str(af)) for af in ai_af)
		if socket.AF_INET not in ai_af:
			log.fatal(
				'Ambiguous socket host specification (matches address famlies: %s),'
					' refusing to pick one at random - specify socket family instead. Addresses: %s',
				', '.join(ai_af_names), ', '.join(ai_addr) )
			raise AddressError
		(log.warn if force_unique_address is None else log.info)\
			( 'Specified host matches more than one address'
				' family (%s), using it as IPv4 (AF_INET)', ai_af_names )
		af = socket.AF_INET
	else: af = list(ai_af)[0]

	for addr, family in ai_addr:
		if family == af: break
	else: raise AddressError
	ai_addr_unique = set(ai_addr)
	if len(ai_addr_unique) > 1:
		if force_unique_address:
			raise AddressError('Address matches more than one host: {}'.format(ai_addr_unique))
		log.warn( 'Specified host matches more than'
			' one address (%s), using first one: %s', ai_addr_unique, addr )

	return af, addr

it_ngrams = lambda seq, n: zip(*(it.islice(seq, i, None) for i in range(n)))
it_adjacent = lambda seq, n: zip(*([iter(seq)] * n))

@contextmanager
def safe_replacement(path, mode=None):
	if mode is None:
		try: mode = stat.S_IMODE(os.lstat(path).st_mode)
		except (OSError, IOError): pass
	kws = dict( delete=False,
		dir=os.path.dirname(path), prefix=os.path.basename(path)+'.' )
	with tempfile.NamedTemporaryFile(**kws) as tmp:
		try:
			if mode is not None: os.fchmod(tmp.fileno(), mode)
			yield tmp
			if not tmp.closed: tmp.flush()
			os.rename(tmp.name, path)
		finally:
			try: os.unlink(tmp.name)
			except (OSError, IOError): pass

def with_src_lock(shared=False):
	lock = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
	def _decorator(func):
		@ft.wraps(func)
		def _wrapper(src, *args, **kws):
			fcntl.lockf(src, lock)
			try: return func(src, *args, **kws)
			finally:
				try: fcntl.lockf(src, fcntl.LOCK_UN)
				except (OSError, IOError) as err:
					log.exception('Failed to unlock file object: %s', err)
		return _wrapper
	return _decorator

def parse_uid_spec(uid_spec):
	import pwd, grp
	def parse_id(spec, name_func):
		spec = bytes(spec)
		if spec.isdigit(): return int(spec)
		return name_func(spec)
	uid_spec = uid_spec.rstrip(':')
	if ':' not in uid_spec:
		uid = parse_id( uid_spec,
			lambda spec: pwd.getpwnam(spec).pw_uid )
		gid = pwd.getpwuid(uid).pw_gid
	else:
		uid, gid = uid_spec.split(':', 1)
		uid = parse_id( uid,
			lambda spec: pwd.getpwnam(spec).pw_uid )
		gid = parse_id( gid,
			lambda spec: grp.getgrnam(spec).gr_gid )
	return uid, gid

def drop_privileges(uid_spec=None, uid=None, gid=None):
	if uid_spec:
		assert not (uid or gid), [uid_spec, uid, gid]
		uid, gid = parse_uid_spec(uid_spec)
	assert uid is not None and gid is not None, [uid_spec, uid, gid]
	os.setresgid(gid, gid, gid)
	os.setresuid(uid, uid, uid)
	log.debug('Switched uid/gid to %s:%s', uid, gid)


class ZBlock(dict): pass
class ZEntry(dict): pass

def zone_addr_format(addr):
	if addr.version == 4: return addr.format()
	else: return ''.join('{:04x}'.format(n) for n in addr.words)

@with_src_lock(shared=True)
def zone_parse(src):
	block, bol_next, entries = None, 0, defaultdict(list)
	src_ts = os.fstat(src.fileno()).st_mtime

	src.seek(0)
	for n, line in enumerate(iter(src.readline, ''), 1):
		bol, bol_next = bol_next, src.tell()

		match = re.search(r'^\s*#\s*dynamic:\s*((\d+(\.\d+)?)\s.*)$', line)
		if match:
			data, ts_span = match.group(1).split(), tuple(bol+v for v in match.span(2))
			keys = map(key_decode_vk, data[1:])
			key_ids = map(key_get_id, keys)
			block = ZBlock(
				ts=float(data[0]), ts_span=ts_span,
				lineno=n, keys=dict(zip(key_ids, keys)), names=list() )
			for key_id in key_ids: entries[key_id].append(block)
			continue
		elif re.search(r'^\s*#', line): line = ''

		line = line.strip()
		if not line: block = None
		if not block: continue

		t = line[0]
		if t in '+6':
			name, addr_raw = line[1:].split(':', 2)[:2]
			addr, ver = addr_raw, None
			if t == '6': ver, addr = 6, ':'.join(''.join(v) for v in it_adjacent(addr, 4))
			elif t == '+': ver = 4
			else: raise NotImplementedError()
			block['names'].append(ZEntry(
				name=name, t=t, bol=bol,
				addr_raw=addr_raw, addr=netaddr.IPAddress(addr) ))

	return src_ts, entries

def zone_parse_file(src_path):
	with open(src_path, 'rb') as src:
		return zone_parse(src)


ZUAddr = namedtuple('ZoneUpdateAddr', 'pos obj addr')
ZUTime = namedtuple('ZoneUpdateTime', 'pos obj ts')
ZUPatch = namedtuple('ZoneUpdatePatch', 'chunk obj vals')

class InvalidPacket(Exception): pass
class ZoneMtimeUpdated(Exception): pass

@with_src_lock(shared=False)
def zone_update_file(src, src_ts, updates):
	def src_stat_check():
		src_ts_now = os.fstat(src.fileno()).st_mtime
		if abs(src_ts_now - src_ts) > 1:
			raise ZoneMtimeUpdated([src_ts_now, src_ts])
	src_stat_check()
	src.seek(0)
	res, src_buff = list(), src.read()

	pos, pos_used = None, set()
	updates.sort(reverse=True) # last update pos first
	for u in updates:
		if isinstance(u, ZUAddr):
			log.debug( 'Updating zone entry for name %r (type: %s):'
				' %s -> %s', u.obj['name'], u.obj['t'], u.obj['addr'], u.addr )
			a = u.obj['bol']
			b = src_buff.find('\n', a)
			res.append(src_buff[b:pos])
			line_src = src_buff[a:b]
			regexp = r'(?<=:){}((?=[:\s])|$)'.format(re.escape(u.obj['addr_raw']))
			addr_dst = zone_addr_format(u.addr)
			line_dst = re.sub(regexp, addr_dst, src_buff[a:b])
			assert line_src != line_dst, [line_src, regexp, addr_dst]
			res.append(ZUPatch( line_dst,
				u.obj, dict(addr=u.addr, addr_raw=addr_dst) ))
		elif isinstance(u, ZUTime):
			log.debug( 'Updating zone block'
				' (line: %s) ts: %.2f -> %.2f', u.obj['lineno'], u.obj['ts'], u.ts )
			a, b = u.obj['ts_span']
			res.append(src_buff[b:pos])
			res.append(ZUPatch('{:.2f}'.format(u.ts), u.obj, dict(ts=u.ts)))
		else: raise ValueError(u)
		pos = a
		assert pos not in pos_used, pos
		pos_used.add(pos)
	res.append(src_buff[:pos])

	pos, src_buff, updates = 0, list(), list()
	for u in reversed(res):
		if isinstance(u, ZUPatch):
			if isinstance(u.obj, ZEntry):
				u.vals['pos_diff'] = pos - u.obj['bol']
			elif isinstance(u.obj, ZBlock):
				u.vals['pos_diff'] = pos + len(u.chunk) - u.obj['ts_span'][1]
			else: raise ValueError(u.obj)
			updates.append(u)
			u = u.chunk
		src_buff.append(u)
		pos += len(u)
	src_buff, res = None, ''.join(src_buff)

	with safe_replacement(src.name) as tmp:
		tmp.write(res)
		tmp.close()
		src_stat_check()
		src_ts = os.stat(tmp.name).st_mtime
		# For things that already have old file opened
		src.seek(0)
		src.truncate()
		src.write(res)

	return src_ts, updates

def zone_update( src_path, src_ts, blocks, key_id,
		ts, addr, update_cmd=None, force_updates=False ):
	updates, updates_addr, pos_idx = list(), False, list()
	for block in blocks:
		for entry in block['names']:
			pos, obj = entry['bol'], entry
			pos_idx.append((pos, obj))
			if entry['addr'].version == addr.version and entry['addr'] != addr:
				updates.append(ZUAddr(pos, obj, addr))
				updates_addr = True # don't bother bumping timestamps only
		pos, obj = block['ts_span'][1], block
		pos_idx.append((pos, obj))
		updates.append(ZUTime(pos, obj, ts))

	if not force_updates and not updates_addr:
		log.debug( 'No address changes in valid update'
			' packet: key_id=%s ts=%.2f addr=%s', key_id, ts, addr )
		return
	elif not updates: return

	with open(src_path, 'a+') as src:
		src_ts, updates = zone_update_file(src, src_ts, updates)

	pos_idx.sort()
	pos_diff, updates = 0, dict((id(u.obj), u) for u in updates)
	for pos, obj in pos_idx:
		k = id(obj)
		if k in updates:
			updates[k], u = None, updates[k]
			pos_diff = u.vals.pop('pos_diff')
			obj.update(u.vals)
		if isinstance(obj, ZEntry): obj['bol'] += pos_diff
		elif isinstance(obj, ZBlock):
			obj['ts_span'] = tuple((v + pos_diff) for v in obj['ts_span'])
		else: raise ValueError(obj)

	if update_cmd:
		log.debug('Running post-update cmd: %s', ' '.join(update_cmd))
		err = Popen(update_cmd).wait()
		log.debug('Post-update cmd finished (code: %s)', err)
		if err:
			log.error('Post-update command'
				' exited with non-zero code: %s', err)

	return src_ts

def zone_update_loop( src_path, sock,
		update_cmd=None, force_updates=False ):
	src_ts, entries = zone_parse_file(src_path)

	while True:
		pkt, ep = sock.recvfrom(65535)

		try:
			if len(pkt) != msg_len:
				raise InvalidPacket('size mismatch (must be %s): %s', len(pkt), msg_len)
			try:
				msg_data, msg_sig = struct.unpack(msg_fmt, pkt)
				key_id, ts = struct.unpack(msg_data_fmt, msg_data)
			except struct.error as err:
				raise InvalidPacket('unpacking error: %s', err)
			blocks = entries.get(key_id)
			if not blocks: raise InvalidPacket('unrecognized key id: %s', key_id)
			blocks = filter(lambda b: b['ts'] < ts, blocks)
			if not blocks:
				raise InvalidPacket('repeated/old ts: %s', ts)
			key, msg_data = blocks[0]['keys'][key_id], pkt[:msg_data_len]
			if not key_sign_check(key, msg_data, msg_sig):
				raise InvalidPacket( 'signature check failed'
					' (key: %r, sig: %r): %r', key_encode(key), msg_data, msg_sig )
		except InvalidPacket as err:
			log.debug('Invalid packet - ' + err.args[0], *err.args[1:])
			continue

		addr_raw, port = ep[:2]
		addr = netaddr.IPAddress(addr_raw)
		if addr.is_ipv4_mapped(): addr = addr.ipv4()

		for n in xrange(update_mtime_tries):
			try:
				src_ts_new = zone_update(
					src_path, src_ts, blocks, key_id, ts, addr,
					update_cmd=update_cmd, force_updates=force_updates )
			except ZoneMtimeUpdated as err:
				log.info( 'Reloading zone_file (%r)'
					' due to mtime change: %.2f -> %.2f', src_path, *err.args[0] )
				src_ts, entries = zone_parse_file(src_path)
				blocks = filter(lambda b: b['ts'] < ts, entries.get(key_id))
			else:
				if src_ts_new is not None: src_ts = src_ts_new
				break
		else:
			log.fatal( 'Unable to get exclusive access'
				' to zone_file (%r) in %s tries', src_path, update_mtime_tries )
			sys.exit(1)


def main(args=None):
	import argparse
	parser = argparse.ArgumentParser(
		usage='%(prog)s [options] [ [--] arguments ]', # argparse fails to build that for $REASONS
		description='Tool to generate and keep tinydns'
			' zone file with dynamic dns entries for remote hosts.')

	parser.add_argument('zone_file', nargs='?',
		help='Path to tinydns zone file with client Ed25519 (base64-encoded)'
				' pubkeys and timestamps in comments before entries.'
			' Basically any line with IPs that has comment in the form of'
				' "dynamic: <ts> <pubkey> <pubkey2> ..." immediately before it (no empty lines'
				' or other comments separating these) can be updated by packet with'
				' proper ts/signature.')

	parser.add_argument('update_command', nargs='*',
		help='Optional command to run on zone file updates and its arguments (if any).'
			' If --uid is specified, all commands will be run after dropping privileges.'
			' Use "--" before it to make sure that none of its args get interpreted by this script.')

	parser.add_argument('-g', '--genkey', action='store_true',
		help='Generate a new random signing/verify'
			' Ed25519 keypair, print both keys to stdout and exit.')

	parser.add_argument('-b', '--bind',
		metavar='[host:]port', default=bytes(default_port),
		help='Host/port to bind listening socket to (default: %(default)s).')
	parser.add_argument('-v', '--ip-af',
		metavar='{ 4 | 6 }', choices=('4', '6'), default=socket.AF_UNSPEC,
		help='Resolve hostname(s) (if any) using specified address family version.'
			' Either "4" or "6", no restriction is appled by default.')

	parser.add_argument('--systemd', action='store_true',
		help='Receive socket fd from systemd, send systemd startup notification.'
			' This allows for systemd socket activation and running the'
				' app from unprivileged uid right from systemd service file.'
			' Requires systemd python bindings.')
	parser.add_argument('-u', '--uid', metavar='uid[:gid]',
		help='User (name) or uid (and optional group/gid) to run as after opening socket.'
			' WARNING: not a proper daemonization - does not do setsid, chdir,'
				' closes fds, sets optional gids, etc - just setresuid/setresgid. Use systemd for that.')

	parser.add_argument('--update-timestamps', action='store_true',
		help='Usually, when no addresses are changed, zone file does not get updated.'
			' This option forces updates to timestamps in addr-block headers.')

	parser.add_argument('-d', '--debug', action='store_true', help='Verbose operation mode.')
	opts = parser.parse_args(sys.argv[1:] if args is None else args)

	global log
	import logging
	logging.basicConfig(level=logging.DEBUG if opts.debug else logging.WARNING)
	log = logging.getLogger()

	if opts.genkey:
		signing_key = key_generate()
		print('Signing key (for client only):\n  ', key_encode(signing_key), '\n')
		print('Verify key (for this script):\n  ', key_encode(key_get_vk(signing_key)), '\n')
		return

	if not opts.zone_file: parser.error('Zone file path must be specified')

	if isinstance(opts.ip_af, types.StringTypes):
		opts.ip_af = {'4': socket.AF_INET, '6': socket.AF_INET6}[opts.ip_af]

	try: host, port = opts.bind.rsplit(':', 1)
	except ValueError: host, port = default_bind, opts.bind
	socktype, port = socket.SOCK_DGRAM, int(port)
	af, addr = get_socket_info(host, port, family=opts.ip_af, socktype=socktype)

	sock = None
	if opts.systemd:
		from systemd import daemon
		try: sock, = daemon.listen_fds()
		except ValueError as err:
			log.info('Unable to get socket from systemd, will create it manually')
		else: sock = socket.fromfd(af, socktype)
		daemon.notify('READY=1')
		daemon.notify('STATUS=Listening for update packets')
	if not sock:
		log.debug('Binding to: %r (port: %s, af: %s, socktype: %s)', addr, port, af, socktype)
		sock = socket.socket(af, socktype)
		sock.bind((addr, port))
	if opts.uid: drop_privileges(opts.uid)

	with open(opts.zone_file, 'rb'): pass # access check
	zone_update_loop(
		opts.zone_file, sock, opts.update_command,
		force_updates=opts.update_timestamps )

if __name__ == '__main__': sys.exit(main())
