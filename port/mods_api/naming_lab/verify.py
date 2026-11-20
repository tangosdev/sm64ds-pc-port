"""verify.py -- check the naming lab's ROM-verify queue on the real cartridge.

The lab only proposes: what it shows is how the PC port behaves. This command
takes what you queued with "queue for ROM verify" and asks the cartridge,
running in the controller melonDS (the melonds-ai-interface build), the same
question:

  * did each queued function run at all?  (execution coverage, every
    instruction counted, no breakpoints)
  * was it called on an object of the queued class?  For a member function of
    the class or one of its bases, a logging breakpoint counts the calls where
    r0's vtable word is the class's _ZTV (the repo's symbol is the address point).
  * was the function's overlay even loaded?  If not, the answer is
    "inconclusive", never "did not run".

Results are appended to ~/sm64ds-lab/naming-lab/verify-results.jsonl, which
the dashboard's Names panel shows next to the queue. A covered negative is a
statement about this savestate and frame budget, not about the whole game.

  python verify.py list
  python verify.py run --state <file.ml> [--class daKrb_c] [--frames 600]

melonDS has to be running already (one instance only; this never starts or
stops one):

  powershell <controller>\\tools\\start_control.ps1 -Rom <checkout>\\sm64.nds

The controller is found through MELONDS_AI_ROOT, else C:\\tmp\\melonds-ai-controller.
"""
import argparse
import json
import os
import re
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from lab_roots import DECOMP_ROOT as REPO  # noqa: E402
NOTES_DIR = os.path.join(os.path.expanduser('~'), 'sm64ds-lab', 'naming-lab')
CTRL = os.environ.get('MELONDS_AI_ROOT', r'C:\tmp\melonds-ai-controller')


def read_jsonl(name):
    p = os.path.join(NOTES_DIR, name)
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
    os.makedirs(NOTES_DIR, exist_ok=True)
    with open(os.path.join(NOTES_DIR, name), 'a', encoding='utf-8') as f:
        f.write(json.dumps(rec) + '\n')


def rom_symbols():
    """name -> (addr, module) from config/**/symbols.txt"""
    rx = re.compile(r'^(\S+) kind:(\w+)\(([^)]*)\) addr:0x([0-9a-f]+)')
    out = {}
    for dp, _, fs in os.walk(os.path.join(REPO, 'config')):
        if 'symbols.txt' in fs:
            mod = os.path.basename(dp) if os.path.basename(dp).startswith('ov') else 'arm9'
            for line in open(os.path.join(dp, 'symbols.txt'), encoding='latin-1'):
                m = rx.match(line)
                if m:
                    out[m.group(1)] = (int(m.group(4), 16), mod)
    return out


def latest_per_class(queue):
    last = {}
    for rec in queue:
        last[rec.get('cls')] = rec
    return last


def client():
    sys.path.insert(0, os.path.join(CTRL, 'tools'))
    try:
        from control_client import ControlClient
    except ImportError:
        sys.exit(f'no control_client.py under {CTRL}\\tools (set MELONDS_AI_ROOT)')
    token = os.environ.get('MELONDS_CONTROL_TOKEN', 'sm64-research')
    port = int(os.environ.get('MELONDS_CONTROL_PORT', '45987'))
    c = ControlClient(port=port, token=token)
    try:
        c.connect()
        c.ping()
    except Exception as e:
        sys.exit(f'no controller melonDS answers on 127.0.0.1:{port} ({e}).\n'
                 f'Start one yourself: powershell {CTRL}\\tools\\start_control.ps1 -Rom <checkout>\\sm64.nds')
    return c


def class_of(qual):
    return qual.rsplit('::', 1)[0] if '::' in qual else ''


PLAYER_TABLE, PLAYER_INDEX, POS = 0x0209f394, 0x0209f250, 0x5c   # see melonds-ai-interface notes


def teleport_near(c, cls, fns, vt):
    """Do what the lab's `tp` did in the port: put the player next to a live
    object of the class, so the functions that only run near the player
    (movement, collision, rendering) get the same chance on the cartridge.
    The object is found by halting in one of the class's own methods with r0's
    vtable equal to the class's; returns {'obj', 'pos'} or None."""
    own = [f['rom']['addr'] for f in fns if class_of(f['key'].split('(')[0]) == cls]
    for ad in own[:3]:
        r = c.run_until(ad, max_frames=120, condition=f'u32[r0] == {vt} || u32[r0] == {vt + 8}')
        hit = r.get('hit')
        c.breakpoint_remove(ad)
        if not hit:
            continue
        regs = c.registers().get('registers', {})
        obj = int(regs['r0'], 16)
        c.step(1)                                   # finish the halted frame
        x, y, z = struct.unpack('<3i', c.read_memory(obj + POS, 12))
        idx = c.read_value(PLAYER_INDEX)
        player = c.read_value(PLAYER_TABLE + 4 * idx)
        c.write_memory(player + POS, struct.pack('<3i', x + 300 * 4096, y + 100 * 4096, z))
        return {'obj': obj, 'player': player, 'pos': [x >> 12, y >> 12, z >> 12]}
    return None


def run(a):
    queue = latest_per_class(read_jsonl('verify-queue.jsonl'))
    if a.cls:
        queue = {k: v for k, v in queue.items() if k == a.cls}
    if not queue:
        sys.exit('nothing queued' + (f' for {a.cls}' if a.cls else '') + ': use "queue for ROM verify" in the dashboard')
    syms = rom_symbols()
    c = client()
    st = c.status()
    if st.get('state') == 'breakpoint':
        c.step(1)
    for cls, rec in queue.items():
        fns = [f for f in rec.get('functions', []) if f.get('rom')]
        ztv = syms.get(f'_ZTV{len(cls)}{cls}')
        vt = ztv[0] if ztv else None
        bases = set(rec.get('chain') or [cls])
        print(f'{cls}: {len(fns)} functions, vtable {hex(vt) if vt else "unknown"}, state {a.state}, {a.frames} frames')

        c.breakpoint_clear()
        c.load_state(a.state)
        resident = set(c.overlays(resident_only=True).get('resident_ids', []))
        near = teleport_near(c, cls, fns, vt) if a.near and vt else None
        if near:
            print(f'  player moved next to the {cls} at {near["obj"]:#x} ({near["pos"]}), {a.settle} frames to settle')
            c.step(a.settle)
        elif a.near:
            print(f'  no live {cls} found to stand next to; counting from the savestate as it is')
        addrs = sorted({f['rom']['addr'] for f in fns})
        c.coverage_start(addresses=addrs)
        checked = {}
        for f in fns:
            ad = f['rom']['addr']
            # r0 is `this` only for a member function of this class or a base
            if vt and class_of(f['key'].split('(')[0]) in bases and ad not in checked:
                # the repo puts _ZTV at the address point (what objects hold);
                # +8 also matches a symbol placed at the vtable's start
                c.breakpoint_add(ad, condition=f'u32[r0] == {vt} || u32[r0] == {vt + 8}', action='log')
                checked[ad] = f['key']
        t0 = time.time()
        left = a.frames
        while left > 0:
            r = c.step(min(left, 120))
            left -= r.get('frames_run', 120) or 120
        num = lambda v: int(v, 16) if isinstance(v, str) else v  # the protocol sends '0x...' strings
        cov = {num(x['address']): x for x in c.coverage_read().get('counts', [])}
        bps = {num(b['address']): b for b in c.breakpoint_list().get('breakpoints', [])}
        c.coverage_stop(clear=True)
        c.breakpoint_clear()

        rows = []
        for f in fns:
            ad, mod = f['rom']['addr'], f['rom'].get('module', 'arm9')
            ov = int(mod[2:]) if mod.startswith('ov') else None
            n = cov.get(ad, {}).get('count', 0)
            bp = bps.get(ad)
            recv = bp.get('hits', 0) if bp else None
            if ov is not None and ov not in resident:
                verdict = 'inconclusive: overlay %d not loaded' % ov
            elif n == 0:
                verdict = 'not seen'
            elif recv == 0:
                verdict = f'ran, never on a {cls}'
            elif recv:
                verdict = f'confirmed: ran on a {cls}'
            else:
                verdict = 'confirmed: ran'
            rows.append({'key': f['key'], 'rom': f['rom'], 'port_calls': f.get('calls'), 'rom_count': n,
                         'rom_on_class': recv, 'first_word': cov.get(ad, {}).get('first_word'), 'verdict': verdict})
            print(f'  {verdict:34} rom {n:6}  on-class {"-" if recv is None else recv:>6}  port {f.get("calls", "?"):>5}  '
                  f'{f["rom"]["addr"]:08x} {f["key"]}')
        out = {'time': time.strftime('%Y-%m-%d %H:%M:%S'), 'cls': cls, 'queued': rec.get('time'),
               'state': os.path.abspath(a.state), 'frames': a.frames, 'player_moved_next_to': near, 'resident_overlays': sorted(resident),
               'vtable': vt, 'functions': rows, 'seconds': round(time.time() - t0, 1),
               'evidence_source': 'melonDS (cartridge): coverage + r0-vtable logging breakpoints; '
                                  'a negative covers this savestate and frame budget only'}
        append_jsonl('verify-results.jsonl', out)
        ok = sum(1 for r in rows if r['verdict'].startswith('confirmed'))
        print(f'  -> {ok} of {len(rows)} confirmed on the cartridge; written to '
              f'{os.path.join(NOTES_DIR, "verify-results.jsonl")}')


def show(_a):
    for cls, rec in latest_per_class(read_jsonl('verify-queue.jsonl')).items():
        print(f'{cls}: {len(rec.get("functions", []))} functions, queued {rec.get("time")}, level {rec.get("level")}')


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('list').set_defaults(fn=show)
    r = sub.add_parser('run')
    r.add_argument('--state', required=True, help='a melonDS .ml savestate in the queued level')
    r.add_argument('--class', dest='cls')
    r.add_argument('--frames', type=int, default=600, help='emulator frames to run (60 per second)')
    r.add_argument('--no-near', dest='near', action='store_false',
                   help='do not move the player next to an object of the class first')
    r.add_argument('--settle', type=int, default=60, help='frames to run after moving the player')
    r.set_defaults(fn=run)
    a = ap.parse_args()
    a.fn(a)


if __name__ == '__main__':
    main()
