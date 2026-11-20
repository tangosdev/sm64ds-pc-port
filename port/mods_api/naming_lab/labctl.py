"""labctl: the naming lab from the command line, for a Claude session working
alongside the person playing.

    python port/mods_api/naming_lab/labctl.py state            # what is going on right now
    python port/mods_api/naming_lab/labctl.py fn func_02043880 # a function: ROM, source, calls/frame
    python port/mods_api/naming_lab/labctl.py actors [filter]  # live actors, nearest first
    python port/mods_api/naming_lab/labctl.py focus goomba     # inspect an actor (the page follows)
    python port/mods_api/naming_lab/labctl.py fields [filter]  # its fields, decoded, right now
    python port/mods_api/naming_lab/labctl.py who mPosX        # which functions write that field
    python port/mods_api/naming_lab/labctl.py set mPosY 1200   # change a field (int, Fix12 or x,y,z)
    python port/mods_api/naming_lab/labctl.py brk daKrb_c::Behavior [--this focus|any|0x...]
    python port/mods_api/naming_lab/labctl.py go next|ret|off  # continue / run to return / remove & run
    python port/mods_api/naming_lab/labctl.py pause | resume | step [n]
    python port/mods_api/naming_lab/labctl.py shot [out.png]   # a screenshot of the game window

It talks to lab_server.py (127.0.0.1:47620) with the token the server keeps in
~/sm64ds-lab/naming-lab/dashboard-token, so it works only on this machine.
--json prints the server's raw answer.

Everything the port shows is a CANDIDATE: the port runs this tree's own source.
A name is settled on the cartridge (melonDS), not here.
"""
import argparse
import json
import os
import re
import struct
import sys
import time
import urllib.error
import urllib.request

PORT = 47620
NOTES = os.path.join(os.path.expanduser('~'), 'sm64ds-lab', 'naming-lab')


# ------------------------------------------------------------------ http
def token():
    try:
        return open(os.path.join(NOTES, 'dashboard-token')).read().strip()
    except OSError:
        sys.exit('labctl: no dashboard token; is lab_server.py running?')


def call(path, body=None, timeout=30):
    req = urllib.request.Request(f'http://127.0.0.1:{PORT}{path}',
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={'X-Lab-Token': token(), 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except ValueError:
            sys.exit(f'labctl: {path}: HTTP {e.code}')
    except (urllib.error.URLError, ConnectionError) as e:
        sys.exit(f'labctl: the lab server is not answering on {PORT} ({e}). Start it with port\\naming-lab.cmd.')


def cmd(c, reply=None):
    return call('/api/cmd', {'cmd': c, **({'reply': reply} if reply else {})})


# ------------------------------------------------------------------ printing
def actor_line(a):
    en = f"  ({a['en']})" if a.get('en') else ''
    d = f"{a['dist']:>6}  " if 'dist' in a else ''
    return f"{d}{a['cls']:<24} #{a['aid']:<4} {a['ptr']}  pos {a['pos']}{en}"


def print_state(s):
    if not s.get('link'):
        print('game: NOT connected (start it with port\\naming-lab.cmd)')
    if s.get('symbols_stale'):
        print('WARNING: walk_window.exe was rebuilt after the lab server started. Function names and '
              'breakpoints point at the old build until the server is restarted.')
    flags = [k for k in ('paused', 'stopped_at_breakpoint') if s.get(k) in ('1', True)]
    print(f"tick {s.get('tick')}  level {s.get('level')} area {s.get('area')}  {s.get('actor_count')} actors"
          + (f"  [{', '.join(flags)}]" if flags else '') + f"  release {s.get('release')}")
    if s.get('player'):
        print(f"player at {s['player']['pos']}")
    if s.get('stop'):
        st = s['stop']
        a = st.get('actor')
        who = f"  ({a['cls']} #{a['aid']})" if isinstance(a, dict) and a.get('cls') else ''
        print(f"\nSTOPPED at {st['kind']} of {st['fn']}  this={st['this']}{who}")
        print(f"  args {st['args']}  eax {st['eax']}")
        for f in st['stack']:
            print(f'  <- {f}')
    elif s.get('breakpoint'):
        b = s['breakpoint']
        print(f"\nbreakpoint armed: {b['fn']} on {b['this']}")
    if s.get('selected_function'):
        f = s['selected_function']
        print(f"\nselected on the page: {f['name']}  {f.get('sig') or ''}")
        print(f"  ROM {fmt_rom(f.get('rom'))}  source {f.get('source') or '-'}  calls/frame {f.get('calls_per_frame_now', '-')}")
    if s.get('selected_class'):
        c = s['selected_class']
        print(f"selected class: {c['cls']}  {c.get('en') or ''}")
    if s.get('focus'):
        f = s['focus']
        print(f"\nfocused: {actor_line(f)}  header {f.get('header')}")
        for fn in f.get('functions_per_frame', [])[:15]:
            print(f"  {fn['per_frame']:>6}/frame  {fn['name']:<48} {fn['rom'] or 'port code'}")
    if s.get('nearest'):
        print('\nnearest actors:')
        for a in s['nearest'][:10]:
            print('  ' + actor_line(a))
    if s.get('hot'):
        print('\nbusiest functions in the whole game:')
        for f in s['hot'][:12]:
            print(f"  {f['per_frame']:>7}/frame  {f['name']:<48} {f['rom'] or 'port code'}")


def fmt_rom(r):
    return f"{r['module']} 0x{r['addr']:08x} {r['name']}" if r else 'none (port code)'


# ------------------------------------------------------------------ actors and fields
def find_actor(q):
    """an actor by pointer, actor id, class or English name; the nearest wins"""
    acts = call_actors()
    q = q.strip().lower()
    if q.startswith('0x'):
        hit = [a for a in acts if a['ptr'] == int(q, 16)]
    elif q.isdigit():
        hit = [a for a in acts if a['aid'] == int(q)]
    else:
        hit = [a for a in acts if q == a['cls'].lower()] or \
              [a for a in acts if q in a['cls'].lower() or q in (a.get('en') or '').lower()]
    if not hit:
        sys.exit(f'labctl: no live actor matches {q!r}')
    hit.sort(key=lambda a: a.get('dist', 0))    # /api/actors is nearest first already
    return hit[0], len(hit)


def call_actors():
    return call('/api/actors')['actors']


def field_row(name):
    """a field of the focused actor, by name or by offset (0x5c)"""
    f = call('/api/fields?all=1')
    if 'error' in f:
        sys.exit('labctl: ' + f['error'])
    n = name.lower()
    off = hex(int(n, 16)) if re.fullmatch(r'0x[0-9a-f]+', n) else None
    hit = [r for r in f['fields'] if r['name'].lower() == n or r['off'] == off]
    if not hit:
        sys.exit(f"labctl: {f['actor']} has no field {name!r} (labctl fields lists them)")
    return f, hit[0]


TYPE_SIZE = {'u8': 1, 's8': 1, 'bool': 1, 'char': 1, 'u16': 2, 's16': 2, 'short': 2}


def encode(t, txt):
    t = re.sub(r'^const\s+', '', t).strip()
    if t in ('Vector3', 'VecFx32', 'Vector3_16'):
        v = [float(x) for x in re.split(r'[,\s]+', txt.strip()) if x]
        if len(v) != 3:
            sys.exit('labctl: a vector takes three numbers: x,y,z')
        return struct.pack('<3h', *map(int, v)) if t == 'Vector3_16' else struct.pack('<3i', *(round(x * 4096) for x in v))
    if t.startswith('Fix12'):
        return struct.pack('<i', round(float(txt) * 4096))
    if t == 'float':
        return struct.pack('<f', float(txt))
    v = int(txt, 0)
    n = TYPE_SIZE.get(t, 4)
    return (v & ((1 << (8 * n)) - 1)).to_bytes(n, 'little')


# ------------------------------------------------------------------ screenshot
def screenshot(out):
    """the game window (walk_window.exe), even when another window covers it"""
    import ctypes
    from ctypes import wintypes as W
    try:
        from PIL import Image
    except ImportError:
        sys.exit('labctl: shot needs Pillow (pip install pillow)')
    u32, g32, k32 = ctypes.windll.user32, ctypes.windll.gdi32, ctypes.windll.kernel32
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass
    found = []

    @ctypes.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
    def each(h, _):
        if not u32.IsWindowVisible(h):
            return True
        pid = W.DWORD()
        u32.GetWindowThreadProcessId(h, ctypes.byref(pid))
        hp = k32.OpenProcess(0x1000, False, pid.value)
        if hp:
            buf = ctypes.create_unicode_buffer(520)
            n = W.DWORD(520)
            if k32.QueryFullProcessImageNameW(hp, 0, buf, ctypes.byref(n)) and \
                    os.path.basename(buf.value).lower() == 'walk_window.exe':
                r = W.RECT()
                u32.GetClientRect(h, ctypes.byref(r))
                found.append((r.right * r.bottom, h, r.right, r.bottom))
            k32.CloseHandle(hp)
        return True
    u32.EnumWindows(each, 0)
    if not found:
        sys.exit('labctl: no game window (walk_window.exe) found')
    _, h, w, hgt = max(found)
    hdc = u32.GetDC(h)
    mdc = g32.CreateCompatibleDC(hdc)
    bmp = g32.CreateCompatibleBitmap(hdc, w, hgt)
    g32.SelectObject(mdc, bmp)
    ok = u32.PrintWindow(h, mdc, 3)          # PW_CLIENTONLY | PW_RENDERFULLCONTENT

    class BMI(ctypes.Structure):
        _fields_ = [('biSize', W.DWORD), ('biWidth', W.LONG), ('biHeight', W.LONG), ('biPlanes', W.WORD),
                    ('biBitCount', W.WORD), ('biCompression', W.DWORD), ('biSizeImage', W.DWORD),
                    ('biXPelsPerMeter', W.LONG), ('biYPelsPerMeter', W.LONG), ('biClrUsed', W.DWORD),
                    ('biClrImportant', W.DWORD)]
    bi = BMI(ctypes.sizeof(BMI), w, -hgt, 1, 32, 0, 0, 0, 0, 0, 0)
    data = ctypes.create_string_buffer(w * hgt * 4)
    g32.GetDIBits(mdc, bmp, 0, hgt, data, ctypes.byref(bi), 0)
    g32.DeleteObject(bmp)
    g32.DeleteDC(mdc)
    u32.ReleaseDC(h, hdc)
    img = Image.frombuffer('RGBA', (w, hgt), data, 'raw', 'BGRA', 0, 1).convert('RGB')
    box = img.getbbox()      # display scaling can leave the frame in one corner of a black bitmap
    if box and box[:2] == (0, 0):
        img = img.crop(box)
    if not ok or not img.getbbox():
        # PrintWindow came back black (some GPU paths): grab the screen where the window is
        from PIL import ImageGrab
        pt = W.POINT(0, 0)
        u32.ClientToScreen(h, ctypes.byref(pt))
        img = ImageGrab.grab(bbox=(pt.x, pt.y, pt.x + w, pt.y + hgt), all_screens=True)
    if img.width > 1600:
        img = img.resize((1600, round(img.height * 1600 / img.width)))
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    img.save(out)
    return out


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser(description='the naming lab from the command line', usage=__doc__)
    ap.add_argument('what')
    ap.add_argument('args', nargs='*')
    ap.add_argument('--this', default='any', help='brk: focus, any, or an object pointer')
    ap.add_argument('--secs', type=float, default=1.0, help='who: how long to watch')
    ap.add_argument('--json', action='store_true', help='print the raw answer')
    a = ap.parse_args()
    w, rest = a.what, ' '.join(a.args)
    out = None

    if w == 'state':
        out = call('/api/state')
        if not a.json:
            print_state(out)
            return
    elif w in ('fn', 'find', 'source'):
        out = call('/api/find?q=' + urllib.request.quote(rest))
        if not a.json:
            if 'matches' in out:
                print(f"{len(out['matches'])} matches:")
                for m in out['matches']:
                    print(f"  {m['name']:<56} {m['rom'] or ''}")
            elif 'error' in out:
                print(out['error'])
            else:
                print(f"{out['name']}  [{out['convention']}]\n  {out.get('sig') or ''}")
                if out.get('class'):
                    print(f"  class {out['class']}  {out.get('class_en') or ''}")
                print(f"  ROM    {fmt_rom(out.get('rom'))}")
                print(f"  source {out.get('source') or '- (no src/ file yet: still ROM bytes)'}")
                print(f"  port   {out['port_addr']} in {out.get('port_object') or '?'}")
                if 'calls_per_frame_now' in out:
                    print(f"  calls/frame now {out['calls_per_frame_now']}")
            return
    elif w == 'actors':
        out = call('/api/actors')
        if not a.json:
            q = rest.lower()
            for x in out['actors']:
                if not q or q in x['cls'].lower() or q in (x.get('en') or '').lower() or q == str(x['aid']):
                    print(actor_line(x))
            return
    elif w == 'focus':
        act, n = find_actor(rest)
        out = call('/api/focus', {'ptr': int(act['ptr'], 16), 'from': 'agent'})
        if not a.json:
            print(f"focused {actor_line(act)}" + (f'  (nearest of {n})' if n > 1 else ''))
            print(f"header {out.get('layout', {}).get('header')}; the dashboard shows it too")
            return
    elif w == 'fields':
        out = call('/api/fields')
        if not a.json:
            if 'error' in out:
                print(out['error'])
                return
            print(f"{out['actor']} {out['ptr']} at tick {out['tick']}  ({out['header']})")
            q = rest.lower()
            for r in out['fields']:
                if not q or q in r['name'].lower() or q in r['owner'].lower():
                    print(f"  {r['off']:>6}  {r['owner']:<16} {r['type']:<14} {r['name']:<24} {r['value']}")
            return
    elif w == 'who':
        f, row = field_row(a.args[0])
        size = {1: 1, 2: 2}.get(TYPE_SIZE.get(row['type'], 4), 4)
        addr = int(f['ptr'], 16) + int(row['off'], 16)
        out = call('/api/whowrites', {'addr': addr, 'len': size, 'secs': a.secs}, timeout=15)
        if not a.json:
            if 'error' in out:
                print(out['error'])
                return
            print(f"writes to {f['actor']}.{row['name']} ({row['off']}) over {out.get('frames')} frames:")
            for h in out['hits']:
                chain = [h[k]['qual'] if h.get(k) else '?' for k in ('writer', 'caller', 'caller2')]
                rom = h['writer'] and h['writer'].get('rom')
                print(f"  {h['n']:>4}x  {' <- '.join(chain)}" + (f"   [ROM 0x{rom['addr']:08x}]" if rom else ''))
            if not out['hits']:
                print('  nothing wrote it')
            return
    elif w == 'set':
        f, row = field_row(a.args[0])
        b = encode(row['type'], ' '.join(a.args[1:]))
        addr = int(f['ptr'], 16) + int(row['off'], 16)
        out = cmd(f'poke {addr:x} {b.hex()}', 'poked')
        if not a.json:
            print(f"{f['actor']}.{row['name']}: {row['value']} -> {' '.join(a.args[1:])}  ({b.hex()})")
            return
    elif w == 'brk':
        if rest in ('clear', 'off'):
            out = call('/api/brk', {'clear': True})
        else:
            this = 0
            if a.this == 'focus':
                s = call('/api/state')
                if not s.get('focus'):
                    sys.exit('labctl: --this focus but nothing is focused')
                this = int(s['focus']['ptr'], 16)
            elif a.this != 'any':
                this = int(a.this, 16)
            out = call('/api/brk', {'q': rest, 'this': this})
        if not a.json:
            print(out.get('error') or out.get('reply'), *(out.get('matches') or []), sep='\n  ')
            if out.get('brk'):
                print('armed: the game stops the next time it runs. `labctl state` shows the stop; `labctl go ...` resumes.')
            return
    elif w == 'go':
        out = call('/api/go', {'how': rest or 'next'})
        if rest in ('next', 'ret', '') and not a.json:
            # wait (briefly) for the next stop: the return, or the next call
            s = {}
            for _ in range(20):
                time.sleep(0.15)
                s = call('/api/state')
                if s.get('stop'):
                    break
            if s.get('stop'):
                st = s['stop']
                print(f"STOPPED at {st['kind']} of {st['fn']}  this={st['this']}  eax {st['eax']}  (tick {st['tick']})")
                print('labctl fields shows the object now; labctl go next|ret|off to go on')
            else:
                print('running (no stop within 3 s)')
            return
    elif w in ('pause', 'resume'):
        out = cmd(w)
    elif w == 'step':
        out = cmd(f"step {int(rest or 1)}")
    elif w == 'shot':
        p = rest or os.path.join(NOTES, 'shots', time.strftime('shot-%H%M%S.png'))
        print(screenshot(p))
        return
    else:
        ap.print_usage()
        sys.exit(2)
    print(json.dumps(out, indent=1))


if __name__ == '__main__':
    main()
