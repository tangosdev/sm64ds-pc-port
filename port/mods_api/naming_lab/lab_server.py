"""Naming lab server: the dashboard half of the naming lab.

    python port/mods_api/naming_lab/lab_server.py [--exe build/port-trace/walk_window.exe] [--http 47620]
    then open the URL it prints (it carries this run's access token).

It talks to the naming-lab plugin inside the running port (UDP 127.0.0.1:47621)
and turns what the plugin reports -- raw addresses -- into the repository's own
names:

  * functions: walk_window.symtab (next to the exe) gives every MSVC and Itanium
    name the build defines; MSVC names are undecorated with dbghelp, Itanium ones
    with tools/demangle.py, and each is tied to its ROM address through
    config/**/symbols.txt and to its source file through tools/srcpath.py;
  * classes: an actor's vtable pointer is a ??_7Class@@6B@ symbol (or, for a
    ROM-data vtable, a _ZTV row in symbols.txt); failing both, its actor id
    names it through the cartridge's spawn table (g_profile_* -> <Class>_classInit);
  * fields: include/<Class>.h, its "/* 0xNNN */" offset comments, followed down
    the base-class chain;
  * assets: build/assets/files.tsv (tools/asset_catalog.py) and the extracted ROM
    tree, BMD textures decoded by bmd.py;
  * class names and hierarchy: the cartridge's own RTTI (tools/rtti_extract.py),
    tied to the tree's names through the _ZTV symbol at each record's vtable, so
    the dashboard can show a coined name next to the name the ROM carries.

SECURITY. The server binds 127.0.0.1 only, and every /api/ and /events request
must carry this run's random token (the X-Lab-Token header, or ?token= for the
EventSource stream) and a Host of 127.0.0.1/localhost, so another web page in
the same browser cannot drive it (no CSRF, no DNS rebinding). It does not
compile or load code: hot patches are a separate command-line step
(patch.py), run by hand.

Everything it learns from the port is a CANDIDATE. The port runs the decomp's
own source, so it can only suggest; a name is confirmed by the real cartridge
(the melonDS recorder in the name-scrub workflow). Proposals and the verify
queue record that on every entry. Notes go to ~/sm64ds-lab/naming-lab/, never
into the repository.
"""
import argparse
import base64
import bisect
import collections
import ctypes
import hmac
import json
import os
import queue
import re
import secrets
import socket
import struct
import sys
import threading
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from lab_roots import PORT_ROOT, DECOMP_ROOT  # noqa: E402
REPO = DECOMP_ROOT   # src/, include/, config/, tools/, extracted/ and the git history
sys.path.insert(0, os.path.join(REPO, 'tools'))
sys.path.insert(0, HERE)
import demangle as ITA  # noqa: E402
import bmd  # noqa: E402
import narc  # noqa: E402
try:
    import srcpath as SRCPATH  # noqa: E402
except Exception:  # srcpath needs config/; the lab still runs without source links
    SRCPATH = None

NOTES_DIR = os.path.join(os.path.expanduser('~'), 'sm64ds-lab', 'naming-lab')
PLUGIN = ('127.0.0.1', 47621)
# commands the dashboard may forward to the plugin; detour/undetour are not here
ALLOWED_CMDS = {'status', 'actors', 'hello', 'peek', 'poke', 'watch', 'unwatch', 'trace', 'focus', 'hot',
                'cap', 'uncap', 'tp', 'pause', 'resume', 'step', 'assets'}


# ------------------------------------------------------------------ symbols
class Undecorator:
    """MSVC names through dbghelp's own undecorator (the one the debugger uses)."""
    def __init__(self):
        self.fn = None
        try:
            dll = ctypes.WinDLL('dbghelp')
            fn = dll.UnDecorateSymbolName
            fn.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32, ctypes.c_uint32]
            fn.restype = ctypes.c_uint32
            self.fn = fn
        except Exception:
            pass
        self.buf = ctypes.create_string_buffer(2048)

    def __call__(self, name, flags):
        if not self.fn:
            return name
        n = self.fn(name.encode('latin-1'), self.buf, 2048, flags)
        return self.buf.value[:n].decode('latin-1') if n else name


UND = Undecorator()
UNDNAME_NAME_ONLY = 0x1000
UNDNAME_COMPLETE = 0


class Sym:
    __slots__ = ('addr', 'names', 'qual', 'cls', 'method', 'sig', 'thiscall', 'rom', 'obj', 'kind')


def msvc_is_thiscall(name):
    # ?Method@Class@@<access><cv>E... : member functions with a this pointer
    # carry a cv letter and then the calling convention; E is __thiscall.
    m = re.search(r'@@([A-X])([A-D])([A-Z])', name)
    return bool(m and m.group(1) not in 'CDKLST' and m.group(3) == 'E')


def describe(name):
    """-> (qualified, class, method, signature, thiscall) for one symtab name."""
    if name.startswith('?'):
        full = UND(name, UNDNAME_COMPLETE)
        qual = UND(name, UNDNAME_NAME_ONLY)
        if '::' in qual:
            cls, meth = qual.rsplit('::', 1)
        else:
            cls, meth = '', qual
        return qual, cls, meth, full, msvc_is_thiscall(name)
    base = re.sub(r'__hg_\w+$', '', name)
    if base.startswith('__Z'):
        d = ITA.demangle(base[1:])
        if d:
            q = d.get('qualified') or base[1:]
            args = ', '.join(d.get('args') or [])
            return q, d.get('class') or '', d.get('method') or q, f'{q}({args})', False
    if base.startswith('_'):
        base = base[1:]
    return base, '', base, base, False


class RomSymbols:
    """config/**/symbols.txt: the ROM's addresses for the repository's names."""
    SPAWN_TABLE, N_ACTORS, ARM9_BASE = 0x02090864, 391, 0x02004000

    def __init__(self):
        self.by_name = {}
        self.at_addr = collections.defaultdict(list)   # (module, addr) -> names
        self.by_qual = collections.defaultdict(list)
        self.ztv = []
        cfg = os.path.join(REPO, 'config')
        rx = re.compile(r'^(\S+) kind:(\w+)\(([^)]*)\) addr:0x([0-9a-f]+)')
        for dp, _, fs in os.walk(cfg):
            if 'symbols.txt' not in fs:
                continue
            mod = os.path.basename(dp) if os.path.basename(dp).startswith('ov') else 'arm9'
            for line in open(os.path.join(dp, 'symbols.txt'), encoding='latin-1'):
                m = rx.match(line)
                if not m:
                    continue
                name, kind, info, addr = m.group(1), m.group(2), m.group(3), int(m.group(4), 16)
                size = re.search(r'size=0x([0-9a-f]+)', info)
                rec = (addr, mod, int(size.group(1), 16) if size else 0)
                self.by_name[name] = rec
                self.at_addr[(mod, addr)].append(name)
                if kind == 'function' and name.startswith('_Z'):
                    try:
                        d = ITA.demangle(name)
                    except Exception:
                        d = None
                    if d and d.get('qualified'):
                        self.by_qual[d['qualified']].append((name, rec))
                if name.startswith('_ZTV'):
                    self.ztv.append((addr, rec[2], name))
        self.ztv.sort()
        self.actor_class = self._spawn_table()

    def _spawn_table(self):
        """actor id -> (class, profile) from the cartridge's own spawn table:
        ACTOR_SPAWN_TABLE[id] is a profile (g_profile_STAR), whose first word is
        the spawn function (daStar_c_classInit_STAR). Read from extracted/, so
        it is empty without a ROM dump."""
        out = {}
        try:
            arm9 = open(os.path.join(REPO, 'extracted', 'arm9_dec.bin'), 'rb').read()
            mods = {'arm9': (self.ARM9_BASE, arm9)}
            y = open(os.path.join(REPO, 'extracted', 'dsd', 'arm9_overlays', 'overlays.yaml')).read()
            for m in re.finditer(r'- id: (\d+)\n\s+base_address: (\d+)', y):
                f = os.path.join(REPO, 'extracted', 'overlays', f'overlay_{int(m.group(1)):04d}.bin')
                if os.path.exists(f):
                    mods[f'ov{int(m.group(1)):03d}'] = (int(m.group(2)), open(f, 'rb').read())
        except OSError:
            return out

        def word(mod, a):
            base, blob = mods[mod]
            return struct.unpack_from('<I', blob, a - base)[0] if 0 <= a - base <= len(blob) - 4 else None
        for aid in range(self.N_ACTORS):
            prof = word('arm9', self.SPAWN_TABLE + 4 * aid)
            # an overlay address is shared by many overlays: take the one that names it a profile
            mod, pname = next(((m, n) for m in mods for n in self.at_addr.get((m, prof), [])
                               if n.startswith('g_profile_')), (None, None))
            if not mod:
                continue
            fn = word(mod, prof)
            for n in self.at_addr.get((mod, fn), []) + self.at_addr.get(('arm9', fn), []):
                c = re.match(r'(\w+?_c)_classInit', n)
                if c:
                    out[aid] = (c.group(1), pname[len('g_profile_'):])
                    break
            else:
                out[aid] = (None, pname[len('g_profile_'):])
        return out

    def lookup(self, names, qual):
        for n in names:
            base = re.sub(r'__hg_\w+$', '', n)
            for cand in (base[1:] if base.startswith('_') else base, base):
                if cand in self.by_name:
                    a, mod, size = self.by_name[cand]
                    return {'addr': a, 'module': mod, 'size': size, 'name': cand}
            m = re.search(r'func_(?:(ov\d+)_)?([0-9a-f]{8})', base)
            if m:
                return {'addr': int(m.group(2), 16), 'module': m.group(1) or 'arm9', 'size': 0, 'name': m.group(0)}
        hits = self.by_qual.get(qual)
        if hits and len(hits) == 1:
            name, (a, mod, size) = hits[0]
            return {'addr': a, 'module': mod, 'size': size, 'name': name}
        return None

    def vtable_class(self, rom_vptr):
        i = bisect.bisect_right(self.ztv, (rom_vptr, 1 << 40, '')) - 1
        if i >= 0:
            a, size, name = self.ztv[i]
            if a <= rom_vptr < a + max(size, 0x100):
                d = re.match(r'_ZTV(\d+)(\w+)', name)
                if d:
                    return d.group(2)[:int(d.group(1))]
        return None


class SymDB:
    def stale(self):
        """the exe was rebuilt after these symbols were read: every port address
        (breakpoints, timeline names) now points at the wrong code"""
        try:
            return os.path.getmtime(self.exe) != self.exe_mtime
        except OSError:
            return False

    def __init__(self, exe):
        self.exe = exe
        self.exe_mtime = os.path.getmtime(exe)
        root = os.path.dirname(exe)
        self.text_lo, self.text_hi = self._text_range(exe)
        by_addr = collections.defaultdict(list)
        for line in open(os.path.join(root, 'walk_window.symtab'), encoding='latin-1'):
            p = line.rstrip('\n').split('\t')
            if len(p) == 2:
                try:
                    by_addr[int(p[1], 16)].append(p[0])
                except ValueError:
                    pass
        self.objs = self._map_objects(os.path.join(root, 'walk_window.map'))
        self.rom = RomSymbols()
        self.fn = {}            # entry address -> Sym (functions in .text)
        self.data = {}          # address -> [names] (everything else)
        for a, names in by_addr.items():
            if self.text_lo <= a < self.text_hi:
                self.fn[a] = self._make(a, names)
            else:
                self.data[a] = names
        # the port often defines a method twice: an extern "C" __ZN... shim that
        # carries the ROM name and the MSVC thiscall body it jumps to. Both are
        # the same ROM function, so the body inherits the shim's ROM address.
        rom_by_qual = {}
        for s in self.fn.values():
            if s.rom and s.cls:
                rom_by_qual.setdefault(s.qual, s.rom)
        for s in self.fn.values():
            if not s.rom and s.qual in rom_by_qual:
                s.rom = dict(rom_by_qual[s.qual], via='same name')
        self.fn_addrs = sorted(self.fn)
        self.data_addrs = sorted(self.data)
        self.by_class = collections.defaultdict(list)
        for s in self.fn.values():
            if s.cls:
                self.by_class[s.cls].append(s)
        self.vt_class = {}
        for a, names in self.data.items():
            for n in names:
                m = re.match(r'\?\?_7(.+?)@@6B@$', n)
                if m:
                    self.vt_class[a] = '::'.join(reversed(m.group(1).split('@')))

    @staticmethod
    def _text_range(exe):
        d = open(exe, 'rb').read(4096)
        pe = struct.unpack_from('<I', d, 0x3c)[0]
        nsec = struct.unpack_from('<H', d, pe + 6)[0]
        opt = struct.unpack_from('<H', d, pe + 20)[0]
        base = struct.unpack_from('<I', d, pe + 24 + 28)[0]
        for i in range(nsec):
            o = pe + 24 + opt + 40 * i
            if d[o:o + 5] == b'.text':
                vs, va = struct.unpack_from('<II', d, o + 8)
                return base + va, base + va + vs
        return base, base + 0x1000000

    @staticmethod
    def _map_objects(path):
        objs = {}
        rx = re.compile(r'^\s*[0-9a-f]{4}:[0-9a-f]{8}\s+(\S+)\s+([0-9a-f]{8})\s+(?:f\s+)?(?:i\s+)?(\S+)\s*$')
        try:
            for line in open(path, encoding='latin-1'):
                m = rx.match(line)
                if m:
                    objs.setdefault(int(m.group(2), 16), m.group(3))
        except OSError:
            pass
        return objs

    def _make(self, a, names):
        s = Sym()
        s.addr, s.names = a, names
        best = None
        thiscall = False
        for n in names:
            d = describe(n)
            thiscall |= d[4]
            score = (2 if n.startswith('?') else 1 if n.startswith('__Z') else 0, bool(d[1]))
            if best is None or score > best[0]:
                best = (score, d)
        q, c, m, sig, _ = best[1]
        s.qual, s.cls, s.method, s.sig, s.thiscall = q, c, m, sig, thiscall
        s.rom = self.rom.lookup(names, q)
        s.obj = self.objs.get(a, '')
        s.kind = 'thiscall' if thiscall else 'cdecl'
        return s

    def entry(self, callee_plus5):
        """the trace records entry + 5 (the return address of `call _penter`)"""
        return self.fn.get(callee_plus5 - 5) or self.at(callee_plus5)

    def at(self, addr):
        i = bisect.bisect_right(self.fn_addrs, addr) - 1
        return self.fn[self.fn_addrs[i]] if i >= 0 else None

    def find(self, q):
        """functions matching q: a ROM or port address (hex), a symbol, a
        qualified name (Class::Method), or a bare method/function name"""
        q = q.strip()
        try:
            a = int(q[2:] if q.lower().startswith('0x') else q, 16)
        except ValueError:
            a = None
        out = []
        if a is not None:
            out = [x for x in self.fn.values() if x.rom and x.rom['addr'] == a]
            if not out and self.text_lo <= a < self.text_hi:
                out = [self.at(a)]
        if not out:
            ql = q.lower()
            exact = [x for x in self.fn.values()
                     if ql in (x.qual.lower(), (x.method or '').lower()) or q in x.names or ('_' + q) in x.names
                     or (x.rom and x.rom['name'] == q)]
            out = exact or [x for x in self.fn.values() if ql in x.qual.lower()][:40]
        # one ROM function is often two port symbols (the extern "C" shim and the
        # body), and a member-pointer thunk (@pmf_face_...) shares its address
        if any(not x.qual.startswith('@') for x in out if x):
            out = [x for x in out if x and not x.qual.startswith('@')]
        seen, uniq = set(), []
        for x in sorted(out, key=lambda x: (not x.thiscall, x.addr)):
            if x and x.qual not in seen:
                seen.add(x.qual)
                uniq.append(x)
        return uniq

    def data_at(self, addr):
        i = bisect.bisect_right(self.data_addrs, addr) - 1
        if i < 0:
            return None, 0
        a = self.data_addrs[i]
        return self.data[a], addr - a

    def data_addr(self, name):
        if not hasattr(self, '_dname'):
            self._dname = {}
            for a, names in self.data.items():
                for n in names:
                    self._dname[n[1:] if n.startswith('_') else n] = a
        return self._dname.get(name)

    def class_of_vtable(self, vt):
        if vt in self.vt_class:
            return self.vt_class[vt], 'vtable symbol'
        names, off = self.data_at(vt)
        for n in names or []:
            m = re.match(r'_?data_(?:(ov\d+)_)?([0-9a-f]{8})$', n)
            if m:
                c = self.rom.vtable_class(int(m.group(2), 16) + off)
                if c:
                    return c, 'ROM _ZTV'
        return None, None


def ztv_class(name):
    """_ZTV7daKrb_c -> daKrb_c, _ZTVN8daDemo_c7param_cE -> daDemo_c::param_c"""
    s = name[4:]
    nested = s.startswith('N') and s.endswith('E')
    if nested:
        s = s[1:-1]
    parts = []
    while s:
        m = re.match(r'(\d+)', s)
        if not m:
            return None
        k = len(m.group(1))
        n = int(m.group(1))
        parts.append(s[k:k + n])
        s = s[k + n:]
        if not nested:
            break
    return '::'.join(parts) if parts and all(parts) else None


class ClassIndex:
    """The cartridge's classes: every RTTI record (its name, its module, its
    base), and what the tree calls the same class (the _ZTV symbol sitting on
    the record's vtable). status is 'rom' when the tree uses the cartridge's
    name, 'coined' when the cartridge contradicts it, 'unnamed' when the tree
    has no _ZTV there. Classes with no RTTI record are listed separately: the
    cartridge does not name those at all."""
    def __init__(self, syms):
        self.nodes, self.by_tree, self.other, self.error = [], {}, [], None
        try:
            import rtti_extract
            d = rtti_extract.build()
        except Exception as e:  # no extracted ROM: the lab still runs, without ROM class names
            self.error = f'no RTTI ({e}); run tools/rtti_extract.py --report to see why'
            return
        try:
            import rtti_reference
            gl = json.load(open(os.path.join(REPO, 'config', 'rom-name-glossary.json'), encoding='utf-8'))
            decompose = lambda n: rtti_reference.decompose(n, gl)
        except Exception:
            gl, decompose = {}, None
        at = collections.defaultdict(list)
        for name, (addr, mod, _) in syms.rom.by_name.items():
            if name.startswith('_ZTV'):
                c = ztv_class(name)
                if c:
                    at[(mod, addr)].append(c)
        base = {}
        for e in sorted(d['edges'], key=lambda e: e.get('offset') or 0):
            base.setdefault(e['derived_key'], e['base_key'])   # the primary (offset 0) base
        for key, r in d['records'].items():
            vt = int(r['vtable'], 16) if r['vtable'] else None
            names = (at.get((r['vtable_module'], vt)) or at.get((r['vtable_module'], vt - 8)) or []) if vt else []
            status = 'rom' if r['name'] in names else 'coined' if names else 'unnamed'
            tree = r['name'] if status != 'coined' else names[0]
            node = {'key': key, 'rom': r['name'], 'tree': tree, 'aliases': sorted(set(names) - {tree}),
                    'status': status, 'module': r['module'], 'kind': r['kind'], 'base': base.get(key),
                    'vtable': vt, 'gloss': self.gloss(r['name'], gl, decompose),
                    'en': self.english(r['name'], gl, decompose)}
            self.nodes.append(node)
            for n in [tree] + names:
                self.by_tree.setdefault(n, node)
        for cls, fns in syms.by_class.items():
            if cls not in self.by_tree and any(f.rom for f in fns):
                self.other.append({'tree': cls, 'gloss': self.gloss(cls, gl, decompose),
                                   'en': self.english(cls, gl, decompose),
                                   'fns': len({f.qual for f in fns if f.rom})})
        self.other.sort(key=lambda o: o['tree'])

    def en_of(self, cls):
        if not hasattr(self, '_en'):
            self._en = {}
            for n in self.nodes:
                for t in [n['tree'], n.get('rom')] + list(n.get('aliases') or []):
                    if t:
                        self._en.setdefault(t, n.get('en'))
            for o in self.other:
                self._en.setdefault(o['tree'], o.get('en'))
        e = self._en.get(cls)
        return e and (e[0] + (f' ({e[1]})' if e[1] else ''))

    @staticmethod
    def english(name, gl, decompose):
        """a short English name for the dashboard's hover: daKrb_c -> ['Goomba',
        'actor'], daObjHatenaBlock_c -> ['Question Mark Block', 'object actor'].
        Each part of the name is the glossary's English when it has one,
        otherwise the part itself split into words."""
        try:
            _, sub, _, parts = decompose(name) if decompose else (None, None, None, [])
        except Exception:
            sub, parts = None, []
        subg = gl.get('subsystem', {}).get(sub, {}).get('gloss', '') if sub else ''
        kind = subg.split(' (')[0]
        words = []
        for p, g, _, _ in parts:
            w = p
            if g:
                w = g.split('--')[-1].strip() if '--' in g else g.split(' / ')[0].strip()
                w = re.sub(r'^(the|a|an) ', '', re.sub(r'\s*\([^)]*\)', '', w))
                if len(w.split()) > 3:
                    w = p
            words.append(w)
        if not parts:
            core = re.sub(r'_c$', '', name.split('::')[-1])
            words = [subg.split(' (')[0]] if subg else [core]
            kind = ''
        text = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', ' '.join(words))
        out, seen = [], set()
        for x in text.split():   # two parts often gloss to the same word: say it once
            if x.lower() not in seen:
                seen.add(x.lower())
                out.append(x[:1].upper() + x[1:])
        return [' '.join(out), kind]

    @staticmethod
    def gloss(name, gl, decompose):
        if not decompose:
            return ''
        try:
            _, sub, _, parts = decompose(name)
        except Exception:
            return ''
        out = [gl['subsystem'][sub]['gloss']] if sub and sub in gl.get('subsystem', {}) else []
        out += [f'{p}: {g}' for p, g, _, _ in parts if g]
        return ' · '.join(out)

    def summary(self):
        return {'nodes': self.nodes, 'other': self.other, 'error': self.error,
                'counts': {'rtti': len(self.nodes),
                           'coined': sum(1 for n in self.nodes if n['status'] == 'coined'),
                           'unnamed': sum(1 for n in self.nodes if n['status'] == 'unnamed'),
                           'no_rtti': len(self.other)}}


# ------------------------------------------------------------------ headers
TYPE_SIZES = {'s8': 1, 'u8': 1, 'bool': 1, 'char': 1, 'signed char': 1, 'unsigned char': 1,
              's16': 2, 'u16': 2, 'short': 2, 'unsigned short': 2,
              's32': 4, 'u32': 4, 'int': 4, 'unsigned int': 4, 'unsigned': 4, 'long': 4, 'float': 4,
              'Fix12i': 4, 'Fix12<int>': 4, 'Fix12<s32>': 4, 'Vector3': 12, 'Vector3_16': 6, 'VecFx32': 12}
FIELD_RX = re.compile(r'^\s*((?:const\s+|unsigned\s+|signed\s+|struct\s+)*[\w:<>]+(?:\s*\*+)?)\s*(\**)\s*(\w+)\s*'
                      r'((?:\[[^\]]*\])*)\s*;\s*/\*\s*(0x[0-9a-fA-F]+)\b\s*(?:--\s*)?(.*?)\s*(?:\*/)?\s*$')


ANON_BLOCK_RX = re.compile(r'^\s*(?:union|struct)\s*\{\s*$')


class GitInclude:
    """include/ as it is at a git revision (no checkout needed)"""
    def __init__(self, rev):
        import subprocess
        self.rev, self.sp = rev, subprocess
        r = subprocess.run(['git', '-C', REPO, 'ls-tree', '--name-only', rev, 'include/'],
                           capture_output=True, text=True)
        self.names = {n.split('/', 1)[1] for n in r.stdout.split()} if r.returncode == 0 else set()
        self.texts = {}

    def exists(self, f):
        return f in self.names

    def list(self):
        return sorted(self.names)

    def _prefetch(self):
        # The port's decomp\ submodule is usually a partial clone (blob:none), and
        # cat-file fetches each missing blob on its own round trip: hundreds of
        # them for one header lookup, which timed lab_focus out. Ask for every
        # missing header blob in one fetch first. Best effort: a full clone has
        # nothing missing, and offline the lookups below simply come back empty.
        r = self.sp.run(['git', '-C', REPO, 'ls-tree', '-r', self.rev, 'include/'],
                        capture_output=True, text=True)
        oids = [ln.split()[2] for ln in r.stdout.splitlines() if len(ln.split()) >= 3 and ln.split()[1] == 'blob']
        if not oids:
            return
        env = dict(os.environ, GIT_NO_LAZY_FETCH='1')
        r = self.sp.run(['git', '-C', REPO, 'cat-file', '--batch-check'], capture_output=True, text=True,
                        input=''.join(o + '\n' for o in oids), env=env)
        missing = [ln.split()[0] for ln in r.stdout.splitlines() if ln.endswith(' missing')]
        if not missing:
            return
        print(f'lab: fetching {len(missing)} header blobs of {self.rev} (partial clone)', flush=True)
        for k in range(0, len(missing), 2000):
            self.sp.run(['git', '-C', REPO, '-c', 'fetch.negotiationAlgorithm=noop', 'fetch', '-q', '--no-tags',
                         '--no-write-fetch-head', '--recurse-submodules=no', '--filter=blob:none', 'origin',
                         *missing[k:k + 2000]], capture_output=True)

    def _load_all(self):
        # one `git cat-file --batch` for every header: a `git show` per file took
        # over 30 s the first time a class had to be looked up in the index
        names = sorted(self.names)
        self._prefetch()
        r = self.sp.run(['git', '-C', REPO, 'cat-file', '--batch'], capture_output=True,
                        input=''.join(f'{self.rev}:include/{n}\n' for n in names).encode())
        out, pos = r.stdout, 0
        for n in names:
            nl = out.find(b'\n', pos)
            if nl < 0:
                break
            head = out[pos:nl].split()
            pos = nl + 1
            if len(head) < 3 or head[1] != b'blob':
                self.texts[n] = ''
                continue
            size = int(head[2])
            self.texts[n] = out[pos:pos + size].decode('utf-8', 'replace')
            pos += size + 1

    def read(self, f):
        if not self.texts and self.names:
            self._load_all()
        if f not in self.texts:
            r = self.sp.run(['git', '-C', REPO, 'show', f'{self.rev}:include/{f}'], capture_output=True)
            self.texts[f] = r.stdout.decode('utf-8', 'replace') if r.returncode == 0 else ''
        return self.texts[f]


class Headers:
    """Class layouts from include/. With `names_from` (another Headers, e.g. read
    from origin/main), a field this tree still calls unk_ takes the other tree's
    name for the same class and offset as `named`, so the lab never asks you to
    name what the repo has already named since this branch was cut."""
    def __init__(self, git_rev=None, names_from=None):
        self.cache = {}
        self.inc = os.path.join(REPO, 'include')
        self.git = GitInclude(git_rev) if git_rev else None
        self.names_from = names_from
        self.index = None

    def _exists(self, f):
        return self.git.exists(f) if self.git else os.path.exists(os.path.join(self.inc, f))

    def _read(self, f):
        if self.git:
            return self.git.read(f)
        try:
            return open(os.path.join(self.inc, f), encoding='utf-8', errors='replace').read()
        except OSError:
            return ''

    def _find(self, cls):
        if self._exists(cls + '.h'):
            return cls + '.h'
        if self.index is None:
            self.index = {}
            rx = re.compile(r'^\s*(?:struct|class)\s+(\w+)\s*(?::[^{;]*)?\{', re.M)
            for f in (self.git.list() if self.git else os.listdir(self.inc)):
                if f.endswith('.h'):
                    for m in rx.finditer(self._read(f)):
                        self.index.setdefault(m.group(1), f)
        return self.index.get(cls)

    def layout(self, cls, depth=0):
        """-> {'fields': [...], 'chain': [cls, base, ...], 'size': n or None, 'header': path}"""
        if cls in self.cache:
            return self.cache[cls]
        out = {'fields': [], 'chain': [cls], 'size': None, 'header': None}
        path = self._find(cls) if cls else None
        if not path or depth > 8:
            self.cache[cls] = out
            return out
        text = self._read(path)
        m = re.search(r'^\s*(?:struct|class)\s+%s\s*(?::\s*(?:public\s+)?([\w:]+))?\s*\{' % re.escape(cls), text, re.M)
        out['header'] = 'include/' + path
        sz = re.search(r'allocates?\s+(0x[0-9a-fA-F]+)\s+bytes', text)
        if sz:
            out['size'] = int(sz.group(1), 16)
        if not m:
            self.cache[cls] = out
            return out
        base = m.group(1)
        fields = []
        if base:
            b = self.layout(base, depth + 1)
            fields = [dict(f) for f in b['fields']]
            out['chain'] += b['chain']
        i, nest = m.end(), 1
        start = i
        while i < len(text) and nest:
            c = text[i]
            if c == '{':
                nest += 1
            elif c == '}':
                nest -= 1
            i += 1
        body = text[start:i - 1]
        # the blocks enclosing a line: an anonymous `union {` / `struct {` is part of
        # the class's own layout (dActor_c's mPos overlays mPosX/Y/Z that way), so
        # its fields count; a method body or a named nested type does not. Each
        # anonymous block keeps the field list as it was at its `{`: one that
        # closes with a declarator (`} alpha;`) is a named member after all, so
        # its fields are dropped again rather than flattened into the class
        blocks = []
        doc = []
        for line in body.split('\n'):
            s = line.strip()
            if all(blocks):
                fm = FIELD_RX.match(line)
                if fm and not s.startswith(('//', '/*', '*', 'virtual', 'static', 'typedef', 'return')):
                    fields = [f for f in fields if f['offset'] != int(fm.group(5), 16)]
                    fields.append({'offset': int(fm.group(5), 16), 'type': (fm.group(1) + fm.group(2)).strip(),
                                   'name': fm.group(3), 'array': fm.group(4), 'note': fm.group(6),
                                   'doc': ' '.join(doc)[-600:], 'owner': cls})
                    doc = []
                elif s.startswith(('/*', '*', '//')):
                    doc.append(re.sub(r'^(/\*+|\*+/?|//)\s*', '', s).replace('*/', '').strip())
                elif s:
                    doc = []
            anon = bool(ANON_BLOCK_RX.match(line))
            for k, ch in enumerate(line):
                if ch == '{':
                    blocks.append((list(fields),) if anon else None)
                elif ch == '}' and blocks:
                    b = blocks.pop()
                    if b and re.match(r'\s*[A-Za-z_]', line[k + 1:]):
                        fields = b[0]
        fields.sort(key=lambda f: f['offset'])
        if not any(f['offset'] == 0 for f in fields):
            fields.insert(0, {'offset': 0, 'type': 'void **', 'name': '__vtable', 'array': '',
                              'note': 'vtable pointer', 'doc': '', 'owner': out['chain'][-1]})
        for k, f in enumerate(fields):
            nxt = fields[k + 1]['offset'] if k + 1 < len(fields) else None
            f['size'] = self._size(f, nxt)
        out['fields'] = fields
        if self.names_from and depth == 0:
            self._overlay(cls, fields)
        if out['size'] is None and fields:
            last = fields[-1]
            out['size_guess'] = last['offset'] + max(last['size'], 4)
        self.cache[cls] = out
        return out

    def _overlay(self, cls, fields):
        try:
            other = {f['offset']: f for f in self.names_from.layout(cls)['fields']}
        except Exception:  # a header the regexes cannot read is just no overlay
            return
        for f in fields:
            o = other.get(f['offset'])
            if f['name'].startswith('unk') and o and not o['name'].startswith(('unk', '__')):
                f['named'] = {'name': o['name'], 'type': o['type'] + o['array'], 'owner': o['owner'],
                              'doc': o['doc'], 'rev': self.names_from.git.rev if self.names_from.git else ''}

    @staticmethod
    def _size(f, nxt):
        t = f['type'].replace('const ', '').strip()
        one = 4 if '*' in t else TYPE_SIZES.get(t)
        n = 1
        for dim in re.findall(r'\[([^\]]*)\]', f['array']):
            try:
                n *= int(dim, 0)
            except ValueError:
                n = 0
        gap = (nxt - f['offset']) if nxt is not None else None
        if one and n:
            return one * n if gap is None else min(one * n, gap)
        return min(gap, 64) if gap else 4


# ------------------------------------------------------------------ assets
class Assets:
    def __init__(self):
        self.files = {}
        p = os.path.join(PORT_ROOT, 'build', 'assets', 'files.tsv')
        if os.path.exists(p):
            for line in open(p, encoding='utf-8'):
                c = line.rstrip('\n').split('\t')
                if c and c[0].isdigit() and len(c) >= 5:
                    self.files[int(c[0])] = {'id': int(c[0]), 'path': c[2], 'kind': c[3], 'size': int(c[4])}
        self.root = os.path.normpath(os.path.join(REPO, 'extracted', 'dsd', 'files'))
        # ov0's handle table: handle -> path (tools/asset_catalog.py)
        self.handles = {}
        p = os.path.join(PORT_ROOT, 'build', 'assets', 'handles.tsv')
        if os.path.exists(p):
            for line in open(p, encoding='utf-8'):
                c = line.rstrip('\n').split('\t')
                if c and c[0].isdigit() and len(c) >= 7:
                    self.handles[int(c[0])] = {'id': int(c[2]), 'path': c[4], 'kind': c[5], 'size': int(c[6])}
        self.game_ids = None   # game file id -> handle, read from the running game
        self.archives = []     # [(first, end, path)], read from the running game
        self._narc = {}        # path -> [(member path, bytes)]

    def load_game_ids(self, lab):
        """The game asks for a file by an id that is either a FAT id (< 0x8000)
        or a member of one of its NARC archives (>= 0x8000). Overlay 0 builds a
        handle -> id table at boot (data_0209d3b8, data_0209d3b0 entries;
        port/hal/s4ov0_ov0table.cpp); read it from the game and invert it, so an
        archive member gets the path of the handle that names it."""
        tab = lab.syms.data_addr('data_0209d3b8')
        cnt = lab.syms.data_addr('data_0209d3b0')
        if not tab or not cnt:
            return
        def peek(a, n):
            r = lab.ask(f'peek {a:x} {n}', 'mem', timeout=1.0)
            for d in r:
                q = d.split()
                if len(q) >= 3 and int(q[1], 16) == a and q[2] != '!fault':
                    return bytes.fromhex(q[2])
            return None
        ptr, n = peek(tab, 4), peek(cnt, 4)
        if not ptr or not n:
            return
        ptr, n = struct.unpack('<I', ptr)[0], struct.unpack('<i', n)[0]
        if not ptr or not 0 < n < 0x4000:
            return
        raw = b''
        while len(raw) < n * 2:
            chunk = peek(ptr + len(raw), min(4096, n * 2 - len(raw)))
            if not chunk:
                return
            raw += chunk
        ids = {}
        for h in range(n):
            ids.setdefault(struct.unpack_from('<H', raw, h * 2)[0], h)
        self.game_ids = ids
        # the archive table (data_0208ecf4): 13 entries of 0x14 bytes, u16 first
        # and end id at +8/+0xa, path string pointer at +0x10
        arc = lab.syms.data_addr('data_0208ecf4')
        raw = peek(arc, 13 * 0x14) if arc else None
        out = []
        for i in range(13 if raw else 0):
            first, end = struct.unpack_from('<HH', raw, i * 0x14 + 8)
            sp = peek(struct.unpack_from('<I', raw, i * 0x14 + 0x10)[0], 32)
            if sp and 0x8000 <= first < end:
                out.append((first, end, sp.split(b'\0')[0].decode('latin-1').lstrip('/')))
        self.archives = out

    def narc(self, path):
        if path not in self._narc:
            p = os.path.normpath(os.path.join(self.root, path))
            ok = p.startswith(self.root + os.sep) and os.path.isfile(p)
            self._narc[path] = narc.members(open(p, 'rb').read()) if ok else []
        return self._narc[path]

    def member(self, gid):
        """-> (archive path, index, member path, bytes) for an archive id, else None"""
        for first, end, path in self.archives:
            if first <= gid < end:
                m = self.narc(path)
                i = gid - first
                if i < len(m):
                    return path, i, m[i][0], m[i][1]
        return None

    def resolve(self, gid):
        """game file id -> {path, kind, size, how}"""
        if gid >= 0x8000:
            m = self.member(gid)
            if m:
                name = m[2] or f'{m[0]}#{m[1]}'
                return {'path': name, 'kind': os.path.splitext(name)[1].lstrip('.') or '?',
                        'size': len(m[3]), 'how': f'{m[0]} member {m[1]}'}
        if self.game_ids and gid in self.game_ids and self.game_ids[gid] in self.handles:
            h = self.handles[self.game_ids[gid]]
            return dict(h, how=f'handle 0x{self.game_ids[gid]:04x}' + (' (archive member)' if gid >= 0x8000 else ''))
        if gid in self.files:
            return dict(self.files[gid], how='FAT id')
        return {'path': f'? id 0x{gid:04x}', 'kind': '?', 'size': 0, 'how': 'unresolved'}

    def textures(self, fid):
        f = self.resolve(fid)
        if not f or not f['path'].lower().endswith('.bmd'):
            return []
        m = self.member(fid) if fid >= 0x8000 else None
        if m:
            data = m[3]
        else:
            p = os.path.normpath(os.path.join(self.root, f['path']))
            if not p.startswith(self.root + os.sep) or not os.path.isfile(p):
                return []
            data = open(p, 'rb').read()
        out = []
        for t in bmd.textures(data):
            t = dict(t)
            t['png'] = base64.b64encode(t['png']).decode() if t['png'] else None
            out.append(t)
        return out


# ------------------------------------------------------------------ the live link
class Lab:
    def __init__(self, syms, headers, assets):
        self.syms, self.headers, self.assets = syms, headers, assets
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
        self.sock.bind(('127.0.0.1', 0))
        self.sock.settimeout(0.2)
        self.clients = []
        self.lock = threading.Lock()
        self.status = {}
        self.hello = {}
        self.actors = []
        self.player = 0
        self.focus = None
        self.fn_ids = {}
        self.fn_meta = []
        self.waiters = collections.defaultdict(list)
        self.last_seen = 0
        self.hot = []
        self.pending = {}
        self.brk = None        # the armed function breakpoint: {fn, this, addrs}
        self.brk_stop = None   # the entry stop that a "ret" stop returns from
        # for /api/state and labctl.py: what the dashboard is showing, and the
        # latest of each stream (the browser keeps the history, the server the last)
        self.ui = {}           # the function / class last selected on the page
        self.snap = None       # (tick, hex) of the focused actor
        self.hot_last = None   # (tick, frames, Counter fn id -> calls)
        self.focus_fns = collections.deque(maxlen=30)   # (tick, {fn id: calls}) of the focused actor
        self.stop = None       # the last breakpoint stop, as pushed to the page
        self.stopped = False

    def send(self, cmd):
        self.sock.sendto(cmd.encode(), PLUGIN)

    def ask(self, cmd, prefix, timeout=1.5, settle=None):
        """send and wait for the reply whose first word is `prefix`; with settle,
        keep collecting datagrams of a multi-part reply for that long"""
        ev = {'event': threading.Event(), 'data': []}
        with self.lock:
            self.waiters[prefix].append(ev)
        self.send(cmd)
        ev['event'].wait(timeout)
        if settle and ev['data']:
            time.sleep(settle)
        with self.lock:
            if ev in self.waiters[prefix]:
                self.waiters[prefix].remove(ev)
        return ev['data']

    def push(self, obj):
        msg = 'data: ' + json.dumps(obj, separators=(',', ':')) + '\n\n'
        with self.lock:
            for q in list(self.clients):
                try:
                    q.put_nowait(msg)
                except queue.Full:
                    pass

    def run(self):
        last_hello = 0
        while True:
            if time.time() - last_hello > 2:
                self.send('hello')
                last_hello = time.time()
            try:
                data = self.sock.recv(70000).decode('latin-1')
            except socket.timeout:
                self.flush_act()
                continue
            except OSError:  # ICMP port unreachable while the game is down
                time.sleep(0.2)
                continue
            self.last_seen = time.time()
            try:
                self.handle(data)
            except Exception as e:  # one bad datagram must not end the link
                print('lab: bad message:', repr(data[:80]), e, flush=True)

    def handle(self, data):
        head, _, rest = data.partition('\n')
        kind = head.split(' ', 1)[0]
        with self.lock:
            for w in self.waiters.get(kind, ()):
                w['data'].append(data)
                w['event'].set()
        if kind == 'hi':
            if not self.hello:
                # first contact since this server started: a game that outlived an
                # earlier server can still hold that server's breakpoint (and be
                # parked in it), which this one would then misreport
                self.send('brk clear')
                self.send('go off')
            self.hello =dict(kv.split('=', 1) for kv in head.split()[1:] if '=' in kv)
        elif kind == 'status':
            self.status = dict(kv.split('=', 1) for kv in head.split()[1:] if '=' in kv)
            self.push({'t': 'status', 's': self.status, 'link': True, 'stale': self.syms.stale()})
        elif kind == 'actors':
            self.on_actors(head, rest)
        elif kind == 'hits':
            self.on_hits(head, rest)
        elif kind == 'snap':
            _, tick, ptr, hexs = data.split(' ', 3)
            if self.focus and int(ptr, 16) == self.focus['ptr']:
                self.snap = (int(tick), hexs.strip())
                self.push({'t': 'snap', 'tick': int(tick), 'hex': hexs.strip()})
        elif kind == 'hot':
            self.on_hot(head, rest)
        elif kind == 'brk' and len(head.split()) == 3:
            self.on_brk(head, rest)
        elif kind == 'level':
            self.focus = None
            self.push({'t': 'level', 'level': head.split()[1]})

    def on_brk(self, head, rest):
        """the game stopped at a function breakpoint ("entry") or at the return
        of the call it stopped in ("ret"): name the function, its object and
        the call stack for the dashboard"""
        _, where, tick = head.split()
        regs, stack = None, []
        for line in rest.split('\n'):
            q = line.split()
            if len(q) == 10 and q[0] == 'r':
                regs = [int(x, 16) for x in q[1:]]
            elif len(q) == 2 and q[0] == 's':
                stack.append(int(q[1], 16))
        if not regs:
            return
        eip, ecx, eax, esp, ebp, *args = regs

        def frame(a):
            f = self.syms.at(a) if a else None
            if not f or not (self.syms.text_lo <= a < self.syms.text_hi):
                return {'addr': a}
            return {'addr': a, 'id': self.fn_id(f, f.addr + 5), 'off': a - f.addr}
        if where == 'entry':
            f = self.syms.at(eip)
            thiscall = bool(f and f.kind == 'thiscall')
            self.brk_stop = {'fn': self.fn_id(f, f.addr + 5) if f else None,
                             'this': ecx if thiscall else args[0], 'thiscall': thiscall, 'args': args}
        st = self.brk_stop or {}
        obj = st.get('this')
        a = next((x for x in self.actors if x['ptr'] == obj), None)
        self.stop = {'t': 'brk', 'kind': where, 'tick': int(tick), 'fn': st.get('fn'), 'this': obj,
                   'thiscall': st.get('thiscall'), 'actor': a and {k: a[k] for k in ('ptr', 'cls', 'aid', 'uid')},
                   'eip': eip, 'ecx': ecx, 'eax': eax, 'esp': esp, 'args': st.get('args', args),
                   'stack': [frame(x) for x in stack]}
        self.stopped = True    # until /api/go: the plugin's status line lags by up to a second
        self.push(self.stop)

    def on_actors(self, head, rest):
        _, tick, n, player = head.split()
        rows = []
        for line in rest.split('\n'):
            p = line.split()
            if len(p) != 13 or p[0] != 'a':
                continue
            ptr, vt = int(p[1], 16), int(p[2], 16)
            cls, how = self.syms.class_of_vtable(vt)
            if not cls:
                cls, prof = self.syms.rom.actor_class.get(int(p[4]), (None, None))
                cls, how = (cls or prof), 'spawn table'
            rows.append({'ptr': ptr, 'vt': vt, 'uid': int(p[3]), 'aid': int(p[4]), 'alive': int(p[5]),
                         'x': int(p[6]), 'y': int(p[7]), 'z': int(p[8]), 'ang': int(p[9]),
                         'flags': int(p[10], 16), 'area': int(p[11]), 'calls': int(p[12]),
                         'cls': cls or f'actor#{int(p[4])}', 'clsHow': how})
        # one actors message can arrive as several datagrams of the same tick
        if self.pending.get('actors_tick') != tick:
            self.pending['actors_tick'] = tick
            self.pending['actors'] = []
        self.pending['actors'] += rows
        # rows that fault on read are skipped by the plugin and there is no end
        # marker, so publish what has arrived of this tick on every datagram
        if True:
            self.actors = self.pending['actors']
            self.player = int(player, 16) if not player.isdigit() else int(player)
            self.push({'t': 'actors', 'tick': int(tick), 'player': self.player, 'list': self.actors})

    def fn_id(self, s, callee):
        key = s.qual if s else hex(callee)
        if key not in self.fn_ids:
            self.fn_ids[key] = len(self.fn_meta)
            self.fn_meta.append({'id': len(self.fn_meta), 'key': key, 'cls': s.cls if s else '',
                                 'method': s.method if s else key, 'sig': s.sig if s else '', 'addrs': [],
                                 'rom': s.rom if s else None, 'names': s.names[:6] if s else [],
                                 'obj': s.obj if s else ''})
        meta = self.fn_meta[self.fn_ids[key]]
        a = s.addr if s else callee - 5
        if a not in meta['addrs']:
            meta['addrs'].append(a)
            self.push({'t': 'fn', 'fn': meta})
        return self.fn_ids[key]

    def on_hits(self, head, rest):
        p = head.split()
        if not self.focus:
            return
        counts = collections.Counter()
        for line in rest.split('\n'):
            q = line.split()
            if len(q) != 4:
                continue
            ptr, callee, kind, n = int(q[0], 16), int(q[1], 16), int(q[2]), int(q[3])
            if ptr != self.focus['ptr']:
                continue
            s = self.syms.entry(callee)
            # a thiscall member gets its object in ecx; anything else takes it as
            # the first stack argument. Either register can hold the pointer by
            # accident, so each callee is only counted on its own convention.
            if s and s.thiscall and not kind & 1:
                continue
            if s and not s.thiscall and not kind & 2:
                continue
            counts[self.fn_id(s, callee)] += n
        self.focus_fns.append((int(p[1]), dict(counts)))
        self.push({'t': 'hits', 'tick': int(p[1]), 'calls': int(p[2]), 'fns': sorted(counts.items())})

    def on_hot(self, head, rest):
        """whole-game call counts for the last `frames` frames ("hot N all": every
        callee, several datagrams for one window). The dashboard keeps a ring
        of these: the Hot list, and the per-class strips of the Systems view."""
        _, tick, frames = head.split()[:3]
        if self.pending.get('hot_tick') != tick:      # a new window: send the last one
            self.flush_act()
            self.pending['hot_tick'] = tick
            self.pending['hot'] = (int(tick), int(frames), collections.Counter())
        agg = self.pending['hot'][2]
        for line in rest.split('\n'):
            q = line.split()
            if len(q) != 2:
                continue
            callee = int(q[0], 16)
            agg[self.fn_id(self.syms.entry(callee), callee)] += int(q[1])
        # the plugin fills each datagram to ~62000 bytes before starting the
        # next, so a shorter one is the window's last (a new tick also ends it)
        if len(head) + len(rest) < 61900:
            self.flush_act()

    def flush_act(self):
        h = self.pending.pop('hot', None)
        if h and h[2]:
            self.hot_last = h
            self.push({'t': 'act', 'tick': h[0], 'frames': h[1], 'fns': sorted(h[2].items())})

    def set_focus(self, ptr):
        a = next((r for r in self.actors if r['ptr'] == ptr), None)
        if not a:
            return None
        lay = self.headers.layout(a['cls'])
        size = lay['size'] or lay.get('size_guess') or 0x200
        size = max(0xd0, min(0x2000, (size + 0x3f) & ~0x3f))
        self.focus = {'ptr': ptr, 'cls': a['cls'], 'uid': a['uid'], 'size': size}
        self.send('unwatch')
        self.send(f'watch {ptr:x} {size}')
        self.send(f'focus {ptr:x}')
        return {'focus': self.focus, 'layout': lay, 'class': self.class_info(a['cls'])}

    def class_info(self, cls):
        out, seen = [], set()
        for s in sorted(self.syms.by_class.get(cls, []), key=lambda s: s.rom['addr'] if s.rom else 1 << 40):
            if s.qual in seen:
                continue
            seen.add(s.qual)
            out.append({'key': s.qual, 'method': s.method, 'sig': s.sig, 'rom': s.rom, 'obj': s.obj})
        return {'methods': out}


# ------------------------------------------------------------------ agent API
# /api/state, /api/find and /api/fields answer in names, not addresses, so a
# Claude session (labctl.py) can follow along with the person playing.
def _actor_brief(a, pp=None):
    out = {'ptr': hex(a['ptr']), 'cls': a['cls'], 'en': LAB.classes.en_of(a['cls']), 'aid': a['aid'], 'uid': a['uid'],
           'pos': [a[k] for k in ('x', 'y', 'z')], 'calls': a['calls']}
    if pp:
        out['dist'] = round(((a['x'] - pp['x']) ** 2 + (a['y'] - pp['y']) ** 2 + (a['z'] - pp['z']) ** 2) ** .5)
    return out


def _fn_brief(fid, calls=None):
    m = LAB.fn_meta[fid]
    out = {'name': m['key'], 'rom': m['rom'] and f"{m['rom']['module']} 0x{m['rom']['addr']:08x} {m['rom']['name']}"}
    if calls is not None:
        out['calls'] = calls
    return out


def agent_state():
    st = LAB.status
    pp = next((a for a in LAB.actors if a['ptr'] == LAB.player), None)
    near = sorted((a for a in LAB.actors if a is not pp),
                  key=lambda a: (a['x'] - pp['x']) ** 2 + (a['y'] - pp['y']) ** 2 + (a['z'] - pp['z']) ** 2) if pp else []
    out = {'link': time.time() - LAB.last_seen < 3, 'release': LAB.hello.get('release'),
           'tick': st.get('tick'), 'level': st.get('level'), 'area': st.get('area'), 'paused': st.get('paused'),
           'stopped_at_breakpoint': LAB.stopped, 'actor_count': len(LAB.actors),
           'symbols_stale': LAB.syms.stale(),
           'player': pp and _actor_brief(pp), 'nearest': [_actor_brief(a, pp) for a in near[:12]]}
    if LAB.focus:
        a = next((x for x in LAB.actors if x['ptr'] == LAB.focus['ptr']), None)
        agg = collections.Counter()
        for _, c in LAB.focus_fns:
            agg.update(c)
        frames = len(LAB.focus_fns) or 1        # one hits message per frame
        out['focus'] = dict(_actor_brief(a, pp) if a else {'ptr': hex(LAB.focus['ptr']), 'cls': LAB.focus['cls']},
                            header=LAB.headers.layout(LAB.focus['cls']).get('header'),
                            functions_per_frame=[dict(_fn_brief(f), per_frame=round(n / frames, 2))
                                                 for f, n in agg.most_common(80) if LAB.fn_meta[f]['rom']][:25])
    ui = LAB.ui
    if ui.get('fn') is not None and 0 <= int(ui['fn']) < len(LAB.fn_meta):
        out['selected_function'] = agent_fn(LAB.fn_meta[int(ui['fn'])]['key'], brief=True)
    if ui.get('cls'):
        out['selected_class'] = {'cls': ui['cls'], 'en': LAB.classes.en_of(ui['cls'])}
    if LAB.hot_last:
        tick, frames, c = LAB.hot_last
        # the game's own functions (a ROM address), as the page's Hot list shows them
        game = [(f, n) for f, n in c.most_common(400) if LAB.fn_meta[f]['rom']][:20]
        out['hot'] = [dict(_fn_brief(f), per_frame=round(n / max(frames, 1), 1)) for f, n in game]
    if LAB.brk:
        out['breakpoint'] = {'fn': LAB.fn_meta[LAB.brk['fn']]['key'], 'this': hex(LAB.brk['this']) if LAB.brk['this'] else 'any'}
    if LAB.stop and out['stopped_at_breakpoint']:
        sp = LAB.stop
        out['stop'] = {'kind': sp['kind'], 'tick': sp['tick'], 'fn': sp['fn'] is not None and LAB.fn_meta[sp['fn']]['key'],
                       'this': sp['this'] and hex(sp['this']), 'actor': sp['actor'],
                       'args': [hex(x) for x in sp['args']], 'eax': hex(sp['eax']),
                       'stack': [(LAB.fn_meta[f['id']]['key'] + f'+0x{f["off"]:x}') if 'id' in f else hex(f['addr'])
                                 for f in sp['stack'][:12]]}
    return out


def agent_fn(q, brief=False):
    hits = LAB.syms.find(q)
    if not hits:
        return {'error': f'no function matches {q!r}'}
    if len(hits) > 1 and not brief:
        return {'matches': [{'name': x.qual, 'rom': x.rom and f"0x{x.rom['addr']:08x}"} for x in hits[:40]]}
    x = hits[0]
    fid = LAB.fn_ids.get(x.qual)
    out = {'name': x.qual, 'sig': x.sig, 'class': x.cls, 'class_en': LAB.classes.en_of(x.cls) if x.cls else None,
           'rom': x.rom, 'port_addr': hex(x.addr), 'port_object': x.obj, 'convention': x.kind}
    if LAB.hot_last and fid is not None:
        tick, frames, c = LAB.hot_last
        out['calls_per_frame_now'] = round(c.get(fid, 0) / max(frames, 1), 2)
    src = source_for(x.rom['name']) if x.rom else None
    if src:
        out['source'] = f"{src['path']}:{src['line']}"
    return out


def agent_find(q):
    return agent_fn(q)


_DEC = {'u8': '<B', 's8': '<b', 'bool': '<B', 'char': '<b', 'unsigned char': '<B', 'signed char': '<b',
        'u16': '<H', 's16': '<h', 'short': '<h', 'unsigned short': '<H',
        'u32': '<I', 'unsigned': '<I', 'unsigned int': '<I', 's32': '<i', 'int': '<i', 'long': '<i', 'float': '<f'}


def _decode(t, raw):
    t = re.sub(r'^const\s+', '', t).strip()
    if '*' in t and len(raw) >= 4:
        v = struct.unpack('<I', raw[:4])[0]
        a = next((x for x in LAB.actors if x['ptr'] == v), None)
        return ('null' if not v else hex(v)) + (f" -> {a['cls']}" if a else '')
    if t in ('Vector3', 'VecFx32') and len(raw) >= 12:
        return [round(v / 4096, 3) for v in struct.unpack('<3i', raw[:12])]
    if t == 'Vector3_16' and len(raw) >= 6:
        return list(struct.unpack('<3h', raw[:6]))
    if t.startswith('Fix12') and len(raw) >= 4:
        return round(struct.unpack('<i', raw[:4])[0] / 4096, 3)
    fmt = _DEC.get(t)
    if fmt and len(raw) >= struct.calcsize(fmt):
        return struct.unpack(fmt, raw[:struct.calcsize(fmt)])[0]
    return raw.hex() if len(raw) <= 24 else raw[:24].hex() + '...'


def agent_fields(all_fields=False):
    """the focused actor's fields with their current values"""
    if not LAB.focus:
        return {'error': 'nothing focused: focus an actor first (labctl focus <class|id|ptr>)'}
    if not LAB.snap:
        return {'error': 'no snapshot yet; try again in a moment'}
    tick, hx = LAB.snap
    b = bytes.fromhex(hx)
    lay = LAB.headers.layout(LAB.focus['cls'])
    rows = []
    for f in lay['fields']:
        if f['offset'] >= len(b):
            break
        if not all_fields and f['name'] == '__vtable':
            continue
        raw = b[f['offset']:f['offset'] + max(1, f['size'])]
        rows.append({'off': hex(f['offset']), 'owner': f['owner'], 'type': f['type'] + (f['array'] or ''),
                     'name': f['name'], 'value': _decode(f['type'], raw) if not f['array'] else raw[:24].hex()})
    return {'actor': LAB.focus['cls'], 'ptr': hex(LAB.focus['ptr']), 'tick': tick, 'header': lay.get('header'),
            'fields': rows}


# ------------------------------------------------------------------ notes
WW_LOCK = threading.Lock()


def who_writes(addr, length, secs):
    """Arm a data breakpoint on addr for secs of game time and name every
    function that wrote there (the writing function, its caller, and the one
    above). The plugin's breakpoints stay armed only for this call."""
    secs = max(0.1, min(secs, 5.0))
    length = length if length in (1, 2, 4) else 4
    with WW_LOCK:
        LAB.ask('wwatch clear', 'wwatch')
        r = LAB.ask(f'wwatch {addr:x} {length}', 'wwatch')
        ok = bool(r) and r[0].split()[-1] == 'ok'
        if ok:
            time.sleep(secs)
        rep = LAB.ask('writers', 'writers', settle=0.15) if ok else []
        LAB.ask('wwatch clear', 'wwatch')
    if not ok:
        return {'error': 'could not arm the breakpoint: ' + (r[0] if r else 'no reply from the game')}

    def fn(a):
        s = LAB.syms.at(a) if a else None
        if not s or not (LAB.syms.text_lo <= a < LAB.syms.text_hi):
            return None
        return {'qual': s.qual, 'cls': s.cls, 'sig': s.sig, 'rom': s.rom, 'off': a - s.addr}
    head, hits = None, []
    for d in rep:
        for line in d.split('\n'):
            p = line.split()
            if p and p[0] == 'writers':
                head = {'frames': int(p[2]), 'lost': int(p[3])}
            elif p and p[0] == 'h' and len(p) == 6:
                eip, r1, r2, n = int(p[2], 16), int(p[3], 16), int(p[4], 16), int(p[5])
                hits.append({'eip': eip, 'n': n, 'writer': fn(eip), 'caller': fn(r1), 'caller2': fn(r2)})
    hits.sort(key=lambda h: -h['n'])
    return {'addr': addr, 'len': length, 'secs': secs, **(head or {}), 'hits': hits}


def notes_path(name):
    os.makedirs(NOTES_DIR, exist_ok=True)
    return os.path.join(NOTES_DIR, name)


def read_jsonl(name):
    p = notes_path(name)
    if not os.path.exists(p):
        return []
    out = []
    for line in open(p, encoding='utf-8'):
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def append_jsonl(name, rec):
    with open(notes_path(name), 'a', encoding='utf-8') as f:
        f.write(json.dumps(rec) + '\n')


# ------------------------------------------------------------------ source
def source_for(rom_name):
    if not SRCPATH or not rom_name:
        return None
    try:
        p = SRCPATH.path_for(rom_name)
    except Exception:
        return None
    if not p:
        return None
    p = os.path.normpath(os.path.join(REPO, str(p)))
    if not p.startswith(os.path.join(REPO, 'src') + os.sep) or not os.path.isfile(p):
        return None
    text = open(p, encoding='utf-8', errors='replace').read()
    line = 1
    d = None
    if rom_name.startswith('_Z'):
        try:
            d = ITA.demangle(rom_name)
        except Exception:
            d = None
    pat = (r'^[^\n;]*\b%s::%s\s*\(' % (re.escape(d.get('class') or ''), re.escape(d['method']))
           if d and d.get('method') and d.get('class') else r'^[^\n;]*\b%s\s*\(' % re.escape(rom_name))
    m = re.search(pat, text, re.M)
    if m:
        line = text.count('\n', 0, m.start()) + 1
    return {'path': os.path.relpath(p, REPO).replace('\\', '/'), 'line': line, 'text': text}


# ------------------------------------------------------------------ http
LAB = None
TOKEN = secrets.token_urlsafe(24)
PORT = 47620


class H(BaseHTTPRequestHandler):
    def _json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(b)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *a):
        pass

    def _host_ok(self):
        host = (self.headers.get('Host') or '').lower()
        if host not in (f'127.0.0.1:{PORT}', f'localhost:{PORT}'):
            return False
        origin = self.headers.get('Origin')
        return origin is None or origin.lower() in (f'http://127.0.0.1:{PORT}', f'http://localhost:{PORT}')

    def _authorized(self, q):
        if not self._host_ok():
            return False
        tok = self.headers.get('X-Lab-Token') or q.get('token') or ''
        return hmac.compare_digest(tok, TOKEN)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path in ('/', '/index.html'):
            if not self._host_ok():
                self.send_error(403)
                return
            b = open(os.path.join(HERE, 'lab.html'), 'rb').read()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(b)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Frame-Options', 'DENY')
            self.end_headers()
            self.wfile.write(b)
            return
        if not self._authorized(q):
            self.send_error(403, 'missing or wrong lab token')
            return
        if u.path == '/events':
            self.events()
        elif u.path == '/api/classes':
            self._json(LAB.classes.summary())
        elif u.path == '/api/class':
            cls = q.get('name', '')
            self._json({'layout': LAB.headers.layout(cls), 'class': LAB.class_info(cls)})
        elif u.path == '/api/source':
            self._json(source_for(q.get('rom')) or {'error': 'no source file for that symbol'})
        elif u.path == '/api/assets':
            self._json(self.assets())
        elif u.path == '/api/textures':
            self._json(LAB.assets.textures(int(q.get('file', '-1'))))
        elif u.path == '/api/notes':
            self._json({'proposals': read_jsonl('proposals.jsonl'), 'verify': read_jsonl('verify-queue.jsonl'),
                        'results': read_jsonl('verify-results.jsonl'), 'dir': NOTES_DIR})
        elif u.path == '/api/state':
            self._json(agent_state())
        elif u.path == '/api/actors':
            pp = next((x for x in LAB.actors if x['ptr'] == LAB.player), None)
            rows = [_actor_brief(x, pp) for x in LAB.actors]
            rows.sort(key=lambda r: r.get('dist', 0))
            self._json({'actors': rows})
        elif u.path == '/api/find':
            self._json(agent_find(q.get('q', '')))
        elif u.path == '/api/fields':
            self._json(agent_fields(q.get('all') == '1'))
        elif u.path == '/api/sym':
            s = LAB.syms.at(int(q.get('addr', '0'), 16))
            self._json({'qual': s.qual, 'sig': s.sig, 'rom': s.rom, 'names': s.names, 'obj': s.obj} if s else {})
        else:
            self.send_error(404)

    def events(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        qq = queue.Queue(maxsize=2000)
        with LAB.lock:
            LAB.clients.append(qq)
        try:
            init = {'t': 'hello', 'hello': LAB.hello, 'status': LAB.status, 'fns': LAB.fn_meta,
                    'focus': LAB.focus, 'link': time.time() - LAB.last_seen < 3, 'brk': LAB.brk}
            self.wfile.write(('data: ' + json.dumps(init) + '\n\n').encode())
            if LAB.actors:
                self.wfile.write(('data: ' + json.dumps({'t': 'actors', 'tick': 0, 'player': LAB.player,
                                                         'list': LAB.actors}) + '\n\n').encode())
            self.wfile.flush()
            while True:
                try:
                    msg = qq.get(timeout=3)
                except queue.Empty:
                    msg = ': keepalive\n\n'
                    if time.time() - LAB.last_seen > 3:
                        msg = 'data: {"t":"status","link":false}\n\n'
                self.wfile.write(msg.encode())
                self.wfile.flush()
        except (ConnectionError, OSError):
            pass
        finally:
            with LAB.lock:
                if qq in LAB.clients:
                    LAB.clients.remove(qq)

    def assets(self):
        A = LAB.assets
        if A.game_ids is None:
            A.load_game_ids(LAB)
        data = LAB.ask('assets', 'assets', timeout=1.0, settle=0.15)
        recs = []
        for d in data:
            for line in d.split('\n')[1:]:
                p = line.split()
                if len(p) != 6:
                    continue
                try:
                    recs.append((int(p[0]), int(p[1], 16), int(p[2], 16), int(p[3], 16), int(p[4], 16), int(p[5])))
                except ValueError:
                    pass
        loaders = {r[1] for r in recs}
        # one load runs through several loaders (Model::LoadFile ->
        # SharedFilePtr::LoadFile -> Load); keep the call made from game code
        # outside the loaders, which names who wanted the file
        groups = collections.OrderedDict()
        for r in recs:
            groups.setdefault((r[0], r[3]), []).append(r)
        rows, seen = [], {}
        for (tick, sfp), rs in groups.items():
            outer = None
            for r in rs:
                cs = LAB.syms.at(r[2])
                if not cs or cs.addr + 5 not in loaders:
                    outer = r
                    break
            outer = outer or rs[0]
            _, loader, caller, _, actor, fid = outer
            cs, ls = LAB.syms.at(caller), LAB.syms.entry(loader)
            acls = None
            if actor:
                a = next((x for x in LAB.actors if x['ptr'] == actor), None)
                acls = a['cls'] if a else None
            key = (fid, cs.qual if cs else caller)
            if key in seen:
                seen[key]['count'] += 1
                continue
            f = A.resolve(fid)
            row = {'tick': tick, 'file': fid, 'path': f['path'], 'kind': f['kind'], 'size': f['size'], 'how': f['how'],
                   'loader': ls.qual if ls else hex(loader), 'caller': cs.qual if cs else hex(caller),
                   'callerRom': cs.rom if cs else None, 'cls': (cs.cls if cs else '') or '', 'actorCls': acls or '',
                   'actor': actor, 'count': 1}
            seen[key] = row
            rows.append(row)
        return rows

    def do_POST(self):
        u = urlparse(self.path)
        if not self._authorized({}) or self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            self.send_error(403)
            return
        n = min(int(self.headers.get('Content-Length', 0)), 1 << 20)
        try:
            b = json.loads(self.rfile.read(n) or b'{}')
        except ValueError:
            self.send_error(400)
            return
        if u.path == '/api/cmd':
            cmd = str(b.get('cmd', ''))
            if cmd.split(' ', 1)[0] not in ALLOWED_CMDS or '\n' in cmd:
                self._json({'error': 'command not allowed from the dashboard'}, 403)
                return
            want = b.get('reply')
            if want:
                self._json({'reply': LAB.ask(cmd, str(want))})
            else:
                LAB.send(cmd)
                self._json({'ok': True})
        elif u.path == '/api/focus':
            r = LAB.set_focus(int(b['ptr']))
            if r and b.get('from') == 'agent':
                LAB.push({'t': 'focusreq', 'ptr': int(b['ptr'])})
            self._json(r or {'error': 'no such actor now'})
        elif u.path == '/api/unfocus':
            LAB.focus = None
            LAB.send('unwatch')
            LAB.send('focus')
            self._json({'ok': True})
        elif u.path == '/api/ui':
            # the dashboard reports what the person selected, so "this method" means something
            LAB.ui = {k: b[k] for k in ('fn', 'cls', 'where') if k in b}
            LAB.ui['time'] = time.time()
            self._json({'ok': True})
        elif u.path == '/api/brk':
            # arm (fn = a function id the dashboard has, or q = a name / ROM
            # address, this = an object or 0) or clear
            if 'q' in b and 'fn' not in b:
                hit = LAB.syms.find(str(b['q']))
                if len(hit) != 1:
                    self._json({'error': f'{len(hit)} functions match {b["q"]!r}',
                                'matches': [x.qual for x in hit[:20]]}, 400)
                    return
                b['fn'] = LAB.fn_id(hit[0], hit[0].addr + 5)
                for x in LAB.syms.find(hit[0].qual):    # the shim and the body: arm both
                    LAB.fn_id(x, x.addr + 5)
            if b.get('clear'):
                LAB.brk = None
                r = LAB.ask('brk clear', 'brk')
            else:
                fid = int(b['fn'])
                if not 0 <= fid < len(LAB.fn_meta):
                    self._json({'error': 'no such function'}, 400)
                    return
                meta = LAB.fn_meta[fid]
                addrs = meta['addrs'][:2]
                this = int(b.get('this') or 0)
                r = LAB.ask(f'brk {this:x} ' + ' '.join(f'{x:x}' for x in addrs), 'brk')
                ok = bool(r) and r[0].split()[1] == 'ok'
                LAB.brk = {'fn': fid, 'this': this, 'addrs': addrs} if ok else None
            LAB.push({'t': 'brkset', 'brk': LAB.brk})
            self._json({'reply': r[0] if r else 'no reply from the game', 'brk': LAB.brk})
        elif u.path == '/api/go':
            how = b.get('how') if b.get('how') in ('next', 'ret', 'off') else 'next'
            if how == 'off':
                LAB.brk = None
                LAB.push({'t': 'brkset', 'brk': None})
            LAB.stopped = False
            r = LAB.ask('go ' + how, 'go')
            self._json({'reply': r[0] if r else 'no reply from the game'})
        elif u.path == '/api/whowrites':
            self._json(who_writes(int(b['addr']), int(b.get('len', 4)), float(b.get('secs', 1.0))))
        elif u.path in ('/api/propose', '/api/verify'):
            rec = {k: b[k] for k in list(b)[:32]}
            rec['time'] = time.strftime('%Y-%m-%d %H:%M:%S')
            rec['evidence_source'] = 'port (naming lab): a candidate until the ROM agrees'
            rec['game_release'] = LAB.hello.get('release')
            if u.path == '/api/verify':
                rec['why'] = ('Port observation. Confirm on the cartridge before renaming: record this class in '
                              'melonDS with the name-scrub recorder (logging breakpoints on these ROM addresses, '
                              'condition r0 == this).')
            name = 'proposals.jsonl' if u.path == '/api/propose' else 'verify-queue.jsonl'
            append_jsonl(name, rec)
            self._json({'ok': True, 'file': notes_path(name)})
        else:
            self.send_error(404)


def main():
    global LAB, PORT
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--exe', default=os.path.join(PORT_ROOT, 'build', 'port-trace', 'walk_window.exe'))
    ap.add_argument('--http', type=int, default=47620)
    ap.add_argument('--names-from', default='origin/main',
                    help='a git revision whose include/ names fields this tree still calls unk_ ("" = off)')
    a = ap.parse_args()
    PORT = a.http
    global TOKEN
    tp = notes_path('dashboard-token')
    try:
        TOKEN = open(tp).read().strip() or TOKEN
    except OSError:
        pass
    with open(tp, 'w') as f:
        f.write(TOKEN)
    t0 = time.time()
    syms = SymDB(a.exe)
    print(f'lab: {len(syms.fn)} functions, {len(syms.vt_class)} vtables, {len(syms.rom.by_name)} ROM names '
          f'({time.time() - t0:.1f}s)', flush=True)
    names_from = Headers(git_rev=a.names_from) if a.names_from else None
    if names_from and not names_from.git.names:
        print(f'lab: no include/ at {a.names_from}; field names come from this tree only')
        names_from = None
    LAB = Lab(syms, Headers(names_from=names_from), Assets())
    LAB.classes = ClassIndex(syms)
    c = LAB.classes.summary()['counts']
    print(f'lab: {LAB.classes.error}' if LAB.classes.error else
          f'lab: cartridge RTTI {c["rtti"]} classes, {c["coined"]} coined tree names, '
          f'{c["no_rtti"]} tree classes with no RTTI record', flush=True)
    threading.Thread(target=LAB.run, daemon=True).start()
    srv = ThreadingHTTPServer(('127.0.0.1', PORT), H)
    srv.daemon_threads = True
    url = f'http://127.0.0.1:{PORT}/#token={TOKEN}'
    with open(notes_path('dashboard-url.txt'), 'w') as f:
        f.write(url + '\n')
    print(f'lab: dashboard {url}', flush=True)
    srv.serve_forever()


if __name__ == '__main__':
    main()
