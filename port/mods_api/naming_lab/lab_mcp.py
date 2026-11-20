"""sm64ds-lab as MCP tools, so any Claude session (or other MCP client) can work
alongside the person playing the port: read what is going on, look up a
function and its source, inspect and change an actor, set a breakpoint, and
see the game window.

    claude mcp add --scope user sm64ds-lab -- python C:/tmp/sm64ds-naming-lab/port/mods_api/naming_lab/lab_mcp.py

It is a thin layer over labctl.py: each tool runs the same command and returns
the text labctl would print, so the two never disagree. It needs the lab
server (naming-lab.cmd) running on this machine; it never starts one.
"""
import contextlib
import io
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import labctl  # noqa: E402

from mcp.server.fastmcp import FastMCP, Image  # noqa: E402

from lab_roots import DECOMP_ROOT as REPO  # noqa: E402  (lab_source paths are relative to the decomp)

mcp = FastMCP('sm64ds-lab', log_level='WARNING', instructions="""\
Live debugger for the Super Mario 64 DS PC port while a person plays it, next
to the dashboard at http://127.0.0.1:47620. Start every exchange with
lab_state: it shows the level, the focused actor and the functions running on
it per frame, the nearest actors (with English names), the busiest functions,
any breakpoint stop, and what the person last clicked on the dashboard. "This
method" or "this one" means that selection or the focused actor.

lab_function gives a function's ROM address, src/ file:line and calls per
frame; lab_source shows the code. lab_screenshot shows you the game.

Ask the person before lab_break, lab_set_field, lab_pause or lab_step: they
freeze or change the game they are playing. Never leave it frozen: after a
breakpoint, finish with lab_go("off") unless they want to stay stopped.
Far-away actors often skip Behavior, so a breakpoint on one may never fire.

Everything here is a candidate: the port runs the decomp's own source. A name
or behaviour is settled on the real cartridge (melonDS), and renames in src/
are matching work with the repo's byte gates. If lab_state warns that the game
was rebuilt, names and breakpoints point at the old build until the lab server
is restarted.""")

_lock = threading.Lock()


def _run(*argv):
    """labctl <argv>, returning what it prints (its errors included)"""
    buf = io.StringIO()
    old = sys.argv
    with _lock:
        sys.argv = ['labctl', *[str(a) for a in argv]]
        try:
            with contextlib.redirect_stdout(buf):
                labctl.main()
        except SystemExit as e:
            if e.code not in (None, 0):
                buf.write(str(e.code) if isinstance(e.code, str) else f'(exit {e.code})')
        finally:
            sys.argv = old
    return buf.getvalue().strip() or '(no output)'


@mcp.tool()
def lab_state() -> str:
    """What is going on in the game right now: tick, level, the focused actor and
    the functions running on it per frame, the nearest actors, the busiest game
    functions, any breakpoint stop (args, call stack), and the function/class
    the person last selected on the dashboard."""
    return _run('state')


@mcp.tool()
def lab_function(query: str) -> str:
    """Look up a function: signature, class (with its English name), ROM module,
    address and symbol, src/ file:line, port address, calls per frame now.
    Takes func_02043880, 0x02043880, daKrb_c::Behavior, or a bare method name
    (which lists the matches)."""
    return _run('fn', query)


@mcp.tool()
def lab_source(query: str, lines: int = 60) -> str:
    """The source of a function from src/, starting at its definition (as
    lab_function locates it). Says so when it is still ROM bytes."""
    import re
    try:
        out = labctl.call('/api/find?q=' + labctl.urllib.request.quote(query))
    except SystemExit as e:
        return str(e.code)
    if 'matches' in out:
        return f"{len(out['matches'])} functions match; be more specific:\n" + \
            '\n'.join(m['name'] for m in out['matches'])
    if 'error' in out:
        return out['error']
    src = out.get('source')
    if not src:
        return f"{out['name']} has no src/ file yet: it is still ROM bytes ({labctl.fmt_rom(out.get('rom'))})"
    m = re.match(r'(.*):(\d+)$', src)
    path, line = (m.group(1), int(m.group(2))) if m else (src, 1)
    try:
        text = open(os.path.join(REPO, path), encoding='utf-8', errors='replace').read().split('\n')
    except OSError as e:
        return f'{src}: {e}'
    lo = max(0, line - 1)
    body = '\n'.join(f'{i + 1:>6}  {text[i]}' for i in range(lo, min(len(text), lo + max(5, min(lines, 400)))))
    return f"{out['name']}  ({src})\n{body}"


@mcp.tool()
def lab_actors(filter: str = '') -> str:
    """Live actors, nearest to Mario first: distance, class, actor id, pointer,
    position, English name. The filter matches class, English name or id."""
    return _run('actors', *([filter] if filter else []))


@mcp.tool()
def lab_focus(query: str) -> str:
    """Inspect an actor (the dashboard switches to it too). Takes a class
    (daKrb_c), an English name (goomba), an actor id, or a 0x pointer; the
    nearest match wins."""
    return _run('focus', query)


@mcp.tool()
def lab_fields(filter: str = '') -> str:
    """The focused actor's fields from include/, decoded live: offset, owning
    class, type, name, value. The filter matches the field or owning class."""
    return _run('fields', *([filter] if filter else []))


@mcp.tool()
def lab_who_writes(field: str, secs: float = 1.0) -> str:
    """Which functions write a field of the focused actor (writer <- caller <-
    caller, with ROM addresses), by name or 0x offset. Uses a CPU data
    breakpoint for `secs` seconds; the game keeps running."""
    return _run('who', field, '--secs', min(max(secs, 0.2), 10))


@mcp.tool()
def lab_set_field(field: str, value: str) -> str:
    """Write a field of the focused actor: an int, a Fix12 as a decimal (1.5),
    or x,y,z for a vector. Ask the person first: it changes their game."""
    return _run('set', field, value)


@mcp.tool()
def lab_break(function: str, this: str = 'any') -> str:
    """Freeze the game the next time `function` runs. `this` = "any", "focus"
    (only on the focused actor) or a 0x object pointer. Ask the person first.
    Then lab_state shows the stop; lab_go resumes."""
    return _run('brk', function, '--this', this)


@mcp.tool()
def lab_go(how: str = 'next') -> str:
    """Resume from a breakpoint stop: "next" (stop at the next call), "ret" (run
    to this call's return: shows eax, and fields show what changed) or "off"
    (clear the breakpoint and let the game go)."""
    return _run('go', how if how in ('next', 'ret', 'off') else 'next')


@mcp.tool()
def lab_break_clear() -> str:
    """Disarm the breakpoint without resuming a stop (lab_go("off") does both)."""
    return _run('brk', 'clear')


@mcp.tool()
def lab_pause(resume: bool = False) -> str:
    """Freeze the game between frames (or resume=True to let it run again)."""
    return _run('resume' if resume else 'pause')


@mcp.tool()
def lab_step(frames: int = 1) -> str:
    """Advance a paused game by n frames."""
    return _run('step', max(1, min(frames, 600)))


@mcp.tool()
def lab_screenshot() -> Image:
    """A picture of the game window, even when other windows cover it. It is
    also saved under ~/sm64ds-lab/naming-lab/shots/."""
    import time
    p = os.path.join(labctl.NOTES, 'shots', time.strftime('shot-%H%M%S.png'))
    with _lock:
        try:
            labctl.screenshot(p)
        except SystemExit as e:     # labctl reports "no game window" by exiting
            raise RuntimeError(str(e.code)) from None
    # the PNG stays on disk for the person; the model gets a JPEG a fraction the size
    from PIL import Image as PImage
    im = PImage.open(p).convert('RGB')
    if im.width > 1280:
        im = im.resize((1280, round(im.height * 1280 / im.width)))
    jb = io.BytesIO()
    im.save(jb, 'JPEG', quality=85)
    return Image(data=jb.getvalue(), format='jpeg')


if __name__ == '__main__':
    mcp.run()
