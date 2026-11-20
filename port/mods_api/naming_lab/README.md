# Naming Lab

Play the port and see, live, which functions each actor runs, how its fields
change, which files it loads, and what its textures look like. Then name things
and hot-patch functions to test what they do, while the game keeps running.

**The port only proposes.** Everything the lab shows is how the *port* behaves.
That is good evidence for a guess and is never proof of what the original game
does. Every note the lab writes says `evidence_source: port (naming lab): a
candidate until the ROM agrees`. A name lands in `src/` or `include/` only after
the ROM agrees: a melonDS recording, or the ROM bytes and RTTI.

## Run it

After the port's own one-time setup (`port\docs\BUILDING.md`: the `decomp`
submodule, `decomp\extracted`, the assets from your cartridge):

```bat
port\build-port-trace.cmd                       :: the /Gh traced build -> build\port-trace
port\mods_api\naming_lab\build.cmd              :: the plugin -> build\port-trace\mods\naming-lab
port\naming-lab.cmd                             :: Bob-omb Battlefield; port\naming-lab.cmd <level id>
```

The server prints the dashboard address and writes it to
`%USERPROFILE%\sm64ds-lab\naming-lab\dashboard-url.txt`. The address includes
this run's access token. Open it in a browser next to the game window.

**Where it runs.** Only the tracing build loads plugins: `port\hal\lab_plugins.cpp`
is compiled into `build\port-trace` alone (`PORT_FN_TRACE`), and the shipped
`build\port` has no loader. It offers plugin API v1 (`port\mods_api\sm64ds_mod.h`,
the custom line's API) minus `on_file` and `replace_actor`, which the lab does not
use, so the same `naming_lab.dll` also runs on the custom line. The build also
writes `walk_window.symtab` beside the exe (`port\tools\mk_symtab.py`), which
`api->symbol()` reads. The lab reads source, headers and ROM names from the
decomp the port was built from: the `decomp\` submodule, or `SM64DS_DECOMP_ROOT`
(`lab_roots.py`).

To keep up with the port, pull `main` (and its decomp pin), then rerun the two
build commands and restart the game and the server. The server reads the exe's
symbols once, at start. If the exe
is rebuilt under a running server, the dashboard's link light reads
"game rebuilt: restart the lab server" and `labctl state` warns. Until then,
breakpoints and function names point at the old build.

`naming-lab.cmd` presses A three times just after boot (`SM64DS_HOST_PAD`) to
close the level's star-name dialog, because no actor runs its `Behavior` while
the dialog is open. When you play by hand, just press A yourself.

## What you get

| panel | what it shows | where it comes from |
|---|---|---|
| Actors | every live actor, its class and its distance from the player; **tp** teleports Mario next to it | the plugin walks the actor list; the class comes from the vtable (MSVC `??_7` symbols or the ROM `_ZTV`). If neither names it, the actor id does, through the cartridge's spawn table (`g_profile_STAR` → `daStar_c_classInit` → `daStar_c`) |
| Timeline | 300 frames of every function that ran with the focused actor as `this` (or as the first argument of the `extern "C"` shims), coloured by its own class, base classes, or other | `_penter` lab mode in `port/hal/fn_trace.cpp` (4 words per call: callee, call site, ecx, arg0) |
| Function info | port signature, **ROM address and symbol**, port address, a link to the source file | `walk_window.symtab` and `config/**/symbols.txt`; a thiscall body inherits the ROM address of its `__ZN` shim |
| Fields | the class layout from `include/` (`/* 0xNNN */` offsets, base chain, `unk_` gaps), live values, change counts, "changing only"; click a value to change it (vectors take three numbers). A field this tree still calls `unk_` but `origin/main` has named since is shown under main's name, in green italics | `peek`/`poke` through the plugin; main's names come from `git show origin/main:include/...` (`--names-from <rev>`, `""` to turn it off), matched by class and offset |
| Fields → **who?** | every function that wrote that field during one second of play, with the two callers above it, and the ROM address of each | a CPU data breakpoint (DR0–DR1, set by the plugin on the game thread) and a vectored exception handler that records the writing instruction and walks the frame-pointer chain. Only the game's own writes stop it, so the game runs at full speed |
| Hot | the game's busiest functions over the last second (port-only code is hidden); click one to find it in Systems | the dashboard asks for `hot 6 all`: every callee's count, every 6 frames |
| Systems | every class the cartridge's RTTI records, in the cartridge's own hierarchy. Each row has a strip of calls per frame over about 10 seconds. A collapsed class counts itself and everything derived from it; open it to see one strip per method, then its subclasses. Clicking a class shows its base chain, gloss and live instances (**focus** opens one in the Inspector) | `tools/rtti_extract.py` for the classes, `config/rom-name-glossary.json` for the gloss, `hot 6 all` for the counts |
| Assets | every file loaded, the class that owns it, the function that asked for it, and texture thumbnails for `.bmd`; a filter box | an entry hook on each `LoadFile` (dExtSimpleModel_c, dExtAnmTexPat_c, dBgW_Kc, dExtFrameCtrl_c, and `SharedFilePtr::Load`, since main inlines `SharedFilePtr::LoadFile`; release's Model/TextureSequence/SharedFilePtr spellings stay as fallbacks), independent of the trace buffer, so a level load is complete (Bob-omb Battlefield: 184 files, 180 in the load frame, 0 unresolved). File ids resolve through ov0's handle table and the 13 NARC archive ranges (`data_0208ecf4`), both read from the running game. Textures are decoded locally from `extracted/` and never stored |
| Names | proposals (field / function / class / asset), a "queue for ROM verify" list, and the cartridge's verdict on each queued class | `~/sm64ds-lab/naming-lab/*.jsonl`, outside the repo; verdicts from `verify.py` |

Pause, step 1 and step 10 freeze the game between frames so you can read a
state. Drag the borders between the columns to resize them; widths, the
middle tab are remembered.

## English names

Every class name on the page is spelled as the source spells it (`daKrb_c`,
`daObjHatenaBlock_c`). Hover one for an English name: *Goomba (actor)*,
*Question Mark Block (object actor)*. If the tree uses a coined name, the hover
also gives the cartridge's RTTI name. The English comes from
`config/rom-name-glossary.json` through `tools/rtti_reference.decompose`. Each
part of the name takes its gloss, and the subsystem prefix gives the kind. It
is a reading aid, not a proposed name. The filters in Actors and Systems
match it too, so typing `goomba` finds `daKrb_c`.

## Who writes this field?

Click **who?** on any field row. On Bob-omb Battlefield, Mario standing next to
a Goomba, one second each:

| field | writer ← caller ← caller | frames |
|---|---|---|
| `mPosX` (+0x5c) | `AddVec3` ← `dActor_c::UpdatePos` ← `daKrb_c::Behavior` | 28 of 29 |
| | `dActor_c::UpdatePos` ← `daKrb_c::Behavior` ← `fBase_c::Process` | 28 |
| | `dBgCh_Actr::UpdateDiscreteNoLava` ← `dEnemyBase_c::UpdateWMClsn` ← `daKrb_c::Behavior` | 3 |
| `unk_0a4`, `unk_0ac` | `dActor_c::UpdatePos` only | 31 of 32 |
| `unk_1f4` | `dBgPi::RecordHit` ← `dBgW_Kc::DetectClsn` ← `dBgCh_SphCrr` | 31 |

A gap that only `UpdatePos` writes is probably part of a motion vector. A gap
that `RecordHit` writes is collision state. These are leads for the ROM to
settle, not names.

The first lead was checked against the ROM. `origin/main` already names 0x0a4
and 0x0ac `mSpeedX` and `mSpeedZ`. The byte-matched
`dActor_c::UpdatePosWithHorzSpeedAndAng` (ROM 0x02010c5c), which
`UpdatePos` (0x02010c30) calls, stores both. The lab branch was cut before that
naming landed, which is why the dashboard now overlays main's names.

Two fields can be watched at once (the CPU has four debug registers, and
breakpoints use the other two), and each must be 1, 2 or 4 bytes, aligned. The server arms the breakpoint, waits, reads
the hits and clears the breakpoint again, so nothing stays armed. Raw commands:
`wwatch <addr> [len]`, `writers`, `wwatch clear`.

## Breakpoints

Select a function (in Timeline, Systems or Hot) and click **break on #206
daBmb_c** to stop only for the focused actor, or **break on any** to stop for
every object. The next time the function runs, the game freezes at its first
instruction. The bar at the top shows the function, the object it was called
on, the arguments and the call stack, each named. Fields stay live while it is
stopped, and you can click a value to change it before the function runs.

| button | key | what it does |
|---|---|---|
| continue | F8 | run until the next call |
| run to return | F10 | stop when this call returns. The bar shows the return value (`eax`), and Fields marks every field the call changed with old → new |
| remove & run | | clear the breakpoint and let the game go |

One stop of `daBmb_c::Behavior` on Bob-omb Battlefield returned 1 and changed
`mFuse` 10 → 9 and `mTurnTimer` 402 → 401, along with the position, speed and
collision fields. A field that counts down by one per call is a timer, and
that is a lead for the ROM to confirm.

How it works: the plugin puts the function's address in an execute debug
register (DR2/DR3) on the game thread. The exception handler parks the game
thread inside the exception. While parked it keeps answering the dashboard
(peek, poke, snapshots) and pumps window messages, so Windows does not mark the
game as hung. To resume, it disarms the register, single-steps one
instruction, then re-arms. Run to return arms the return address and checks
that `esp` is above the entry `esp`, so a recursive call does not stop it
early. A stop that lasts 10 minutes resumes on its own. Raw commands:
`brk <this> <addr> [addr]`, `brk clear`, `go next|ret|off`.

Limits: actors far from Mario often skip `Behavior` entirely, so a
breakpoint on one of them may never fire. The game window does not repaint
while stopped. There is no line stepping. A stop is at function entry or
return, not inside the body.

## Working with Claude (MCP tools, `labctl.py`)

A Claude session can see what you see, so you can ask "what does
func_02043880 do?" or "why does this Goomba stop?" while you play.

**MCP.** `lab_mcp.py` serves the lab as MCP tools. Register it once:

```
claude mcp add --scope user sm64ds-lab -- python C:/tmp/sm64ds-naming-lab/port/mods_api/naming_lab/lab_mcp.py
```

Every session then has these tools:

- `lab_state`
- `lab_function`
- `lab_source`: the function's code from its `src/` file:line
- `lab_actors`
- `lab_focus`
- `lab_fields`
- `lab_who_writes`
- `lab_set_field`
- `lab_break`
- `lab_go`
- `lab_break_clear`
- `lab_pause`
- `lab_step`
- `lab_screenshot`: the game window as an image the model sees directly

The server's MCP instructions carry the house rules. Each tool runs the
matching labctl command, so the two always agree. It needs the lab server
running and never starts one.

**Command line.** `labctl.py` is the same thing for a shell. It reads the same server as the
dashboard (the token comes from `~/sm64ds-lab/naming-lab/dashboard-token`), and
it answers in names, ROM addresses and `src/` file:line, not port addresses.

```
python port/mods_api/naming_lab/labctl.py state           # overview: level, focus, nearest actors, hot functions, any stop
python port/mods_api/naming_lab/labctl.py fn func_02043880 # signature, class, ROM address, src/ file:line, calls/frame now
python port/mods_api/naming_lab/labctl.py focus goomba     # the nearest Goomba; the dashboard follows
python port/mods_api/naming_lab/labctl.py fields mPos      # the focused actor's fields, decoded
python port/mods_api/naming_lab/labctl.py who mPosY        # which functions write that field (writer <- caller <- caller)
python port/mods_api/naming_lab/labctl.py brk Player::Behavior --this focus
python port/mods_api/naming_lab/labctl.py go ret           # run to the return; go off clears and resumes
python port/mods_api/naming_lab/labctl.py shot             # PNG of the game window, even when covered
```

`state` also reports what you last clicked on the dashboard (a function in
Timeline, Systems or Hot, or a class in Systems). So "this method" means the
same thing to both of you. `set`, `pause`, `resume` and `step [n]` are there
too, and `--json` prints the server's raw answer. The API behind it is
`GET /api/state`, `/api/find?q=`, `/api/fields`, `/api/actors`, and `POST /api/ui`.

The `naming-lab` Claude Code skill (in `~/.claude/skills/naming-lab/`) teaches
a session these commands and the rules: read `state` before guessing, ask
before freezing or changing the game, never leave it stopped, and treat what
the port shows as a candidate until melonDS confirms it on the cartridge.

## Checking it on the cartridge (`verify.py`)

The port only proposes. **queue for ROM verify** records the focused class and
every function the timeline saw run on it, with their ROM addresses.
`verify.py` asks the real game the same question, in the controller melonDS
from the `melonds-ai-interface` skill:

```bat
powershell C:\tmp\melonds-ai-controller\tools\start_control.ps1 -Rom <checkout>\sm64.nds
python port\mods_api\naming_lab\verify.py list
python port\mods_api\naming_lab\verify.py run --state <a savestate in that level> [--class daKrb_c] [--frames 600]
```

1. It loads the savestate, then stops in one of the class's own methods with
   `r0`'s vtable equal to the class's `_ZTV`. That gives it a live object, and
   it moves the player next to that object, the same as the lab's **tp**.
   `--no-near` skips this.
2. It counts every queued address with execution coverage. For members of the
   class and its bases, a logging breakpoint also counts the calls whose `r0`
   is an object of the class.
3. Each function gets one verdict:
   - `confirmed: ran on a <class>`
   - `ran, never on a <class>`
   - `not seen`
   - `inconclusive: overlay N not loaded`

The results go to `verify-results.jsonl`. The dashboard's Names tab shows the
latest verdict per class and lists every function that was not confirmed.
verify.py never starts or stops melonDS.

On Bob-omb Battlefield (`S3_bob_next_to_luigi_goomba.ml`, 600 frames):

| run | confirmed |
|---|---|
| savestate as it is | 15 of 33. Goombas far from the player skip movement, collision and rendering |
| player moved next to a Goomba | 32 of 33. `daKrb_c::Behavior`, `Render`, `UpdatePos`, `UpdateWMClsn` and the cap functions all ran on Goombas |
| after fixing the lab (below) | 39 of 39 |

The one miss was the lab's fault. `dActor_c::Next` ran 206 times "on" the
Goomba in the port and never on the cartridge. The trace showed the caller was
`naming_lab.dll` itself: its per-frame actor walk calls the game's `Next`. The
plugin now pauses tracing for that walk, and the drain drops any call whose
call site is inside the plugin's own image.

## Hot-patching a function (command line)

```bat
python port\mods_api\naming_lab\patch.py find daKrb_c::Be
python port\mods_api\naming_lab\patch.py new daKrb_c::Behavior -o my_patch.c
python port\mods_api\naming_lab\patch.py apply daKrb_c::Behavior my_patch.c
python port\mods_api\naming_lab\patch.py undo daKrb_c::Behavior        :: or: undo all
```

`new` writes a C skeleton with the right calling convention: a thiscall member
becomes `__fastcall(self, edx, ...)`, and an `extern "C"` shim becomes
`__cdecl(self, ...)`. Each parameter is a one-word placeholder annotated with
its real type, and the skeleton warns when a parameter is not one word.
`apply` compiles the file with MSVC x86 into `build\naming_lab\patches\`. The
plugin then replaces the function's `call _penter` (5 bytes) with a `jmp` to
your `lab_patch`. It passes `orig` (entry + 5, the rest of the real body), so a
patch can run code before or after the original, or replace it. The DLL is
loaded from a temp copy, so you can edit, rebuild and apply again while the
game runs.

`examples/krb_freeze.c` skips every Goomba's `Behavior`. Measured on Bob-omb
Battlefield with Mario teleported next to a Goomba, over 1.5 s windows:

| | Goombas whose position moved | words changed per Goomba (first 0x400 bytes) |
|---|---|---|
| before | 2 of 15 | 28 and 39 for the two near Mario, 3–4 for the rest |
| patched | 0 of 15 | 3 for every Goomba |
| after undo | 2 of 15 | 45 and 23, then 3–4 |

The 3 words that still change are written outside `Behavior`. Finding that
split is what the patch is for.

## Security

The server is for one person on one machine.

- It binds 127.0.0.1 only, and the plugin's UDP port is 127.0.0.1 only.
- Every API call needs the per-run token, sent as a header or `?token=`.
- The `Host` header must be `127.0.0.1:<port>` or `localhost:<port>`, and any
  `Origin` must match (DNS-rebinding and CSRF guard). POST bodies must be JSON,
  at most 1 MB.
- The commands the dashboard may forward to the game are whitelisted.
- **Compiling and loading code never goes through HTTP.** `patch.py` is a
  separate command you run yourself. The dashboard reads, watches, pokes
  fields and steps frames.
- Notes are written under `~/sm64ds-lab/naming-lab/`, never into the repo.
  Texture PNGs exist only in HTTP responses. Nothing from the ROM is copied
  anywhere.

## Pieces

| file | role |
|---|---|
| `naming_lab.c` | the plugin (mod API): UDP command loop, actor list, watch/peek/poke, data breakpoints (`wwatch`), lab-mode trace drain, focus filter, hot counts (`hot N` = the top 400 callees, `hot N all` = every callee), asset-loader hooks, tp/pause/step, detours |
| `lab_server.py` | stdlib HTTP + SSE server. Symbols (MSVC undecoration via dbghelp, Itanium via `tools/demangle.py`), ROM symbols, header layouts, asset resolution, notes |
| `lab.html` | the dashboard (one file, no build step) |
| `labctl.py` | the command line a Claude session (or you) uses to read state, find functions, set breakpoints and take screenshots |
| `lab_mcp.py` | the same as MCP tools (`claude mcp add --scope user sm64ds-lab -- python .../lab_mcp.py`) |
| `patch.py` | the hot-patch CLI |
| `verify.py` | checks the ROM-verify queue on the cartridge in melonDS (coverage + `r0` vtable breakpoints) |
| `bmd.py`, `narc.py` | BMD texture decoding (LZ77, all 7 NDS formats) and NARC member names and bytes |
| `probe.py` | the smallest UDP client: `python probe.py status actors` |
| `lab_roots.py` | where the port (`build\`) and the decomp (`src\`, `include\`, `config\`, `tools\`, `extracted\`) are |
| `port\hal\lab_plugins.cpp` | the tracing build's plugin loader; `port\hal\mod_hooks.h` holds the frame and level seams it hangs plugins on |

## Known limits

- The trace buffer holds 4M calls a frame, and a level load exceeds that
  (`status`: `maxcalls=4194304@1 dropframes=1`). The timeline, hot counts and
  focus hits for that one frame are therefore cut short. Assets are not: the
  loader hooks hand each call back to `_penter` and record it themselves.
  During the load frame the instance column falls back to the caller's class,
  because the trace that would name the running actor is full.
- A patch's parameters are placeholders. Doubles, 64-bit values and structs
  passed by value need hand-written types, and `new` flags them.
- `verify.py` verdicts hold only for the savestate and frame budget used. `not
  seen` means not in that run; it does not mean never.
- **who?** names the callers by walking `ebp`. A function built without a
  frame pointer is skipped in that chain, so a caller can be one level higher
  than it looks. The writer itself (the faulting instruction) is always exact.
  Writes from other threads, and by the kernel (`ReadFile` into a buffer), are
  not seen.
- `orig` is valid only on the traced build, where every function starts with
  `call _penter`. `apply` warns when the first byte is not `e8`.
