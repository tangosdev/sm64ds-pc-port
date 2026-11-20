// FUNCTION-ENTRY TRACE FOR THE PC PORT (lane TRACEPORT, run link100).
//
// WHAT THIS IS FOR. Lane ROMTRACE built an instrumented melonDS that writes down
// every function call the REAL cartridge's ARM9 makes and resolves the addresses
// to this repository's own names. This file is the other half: the same list, for
// the port, so the two can be lined up and the first disagreement read off.
//
// HOW IT ATTACHES, AND WHY NOTHING ELSE IN THE TREE CHANGES. MSVC's /Gh switch
// makes the compiler emit "call _penter" as the FIRST instruction of every
// function it compiles, before the prologue. So the hook below is reached from
// every function in the build without one line of any other source file being
// touched, and the address it records is (function start + 5), which the map
// resolves by "largest symbol address at or below". Measured on this box's
// compiler, cl 19.44 for x86, /O2 /Oy-:
//
//   - a __declspec(naked) _penter is NOT itself instrumented, so there is no
//     runaway recursion (the obvious failure of this whole idea, checked first);
//   - TAIL JUMPS SURVIVE: a one-call forwarder still compiles to "call _penter /
//     push ebp / mov ebp,esp / pop ebp / jmp target", so the roughly fifty ov007
//     rows that are correct only because MSVC turns a forwarder into a jmp, and
//     that port/tools/tailjump_guard.py exists to protect, are not disturbed;
//   - INLINING IS NOT INHIBITED, so the hook fires once per REAL call;
//   - the cost is 0.8 nanoseconds per hook (20,000,000 real calls: 0.0204 s
//     plain, 0.0364 s traced). The cartridge makes about 1,800 calls per frame on
//     the menu page and about 27,500 on the boot title screen, so at sixty frames
//     a second the hook costs on the order of one millisecond per second of game.
//
// OFF BY DEFAULT, TWICE OVER. This file is only compiled when the build is
// configured with -DSM64DS_PORT_TRACE=ON, which also adds /Gh; a normal build
// neither compiles it nor carries a single hook instruction. And even in a traced
// build nothing is recorded until SM64DS_FN_TRACE names an output file, because
// ntr_fn_trace_on starts at zero and only the frame hook can raise it.
//
// THE RECORD. Three 32-bit words, always:
//   w0 = a return address inside the CALLEE      (its entry + 5)
//   w1 = a return address inside the CALLER      (the call site + 5)
//   w2 = the caller's esp
// or, for a frame marker, w0 = 0xFFFFFFFF, w1 = the frame number, w2 = 0.
//
// WHY THE CALLER IS RECORDED AND NOT INFERRED. The first version of this file
// wrote two words and left the caller to be reconstructed from esp: an entry at
// a lower esp is nested inside the one before it. THAT RULE IS WRONG ON x86 AND
// THE ERROR IS NOT RARE. Arguments are pushed before the call, so the stack
// pointer at a call site depends on how many arguments THAT call takes, and two
// sibling calls out of the same function land at different stack pointers.
// Measured on the port's own title screen: func_0204be40 calls MTX_MultVec43 at
// esp 0x001aefbc and then G3_MultMtx43 at esp 0x001aefa8, twenty bytes lower,
// so the sp rule filed the second call inside the first. The cartridge has both
// as direct children of func_0204be40, 403 calls each, and every edge below
// them was mis-parented the same way.
//
// The fix costs one instruction. At _penter's entry the callee's own prologue
// has not run yet, so the word above _penter's return address is still the
// return address the CALLER's own `call` pushed. Reading it by value instead of
// taking its address gives the call site itself, which is exactly what the
// instrumented melonDS records when it hooks BL. The two halves then name the
// caller the same way and neither is guessing.
//
// esp is still recorded as w2. It no longer decides nesting, but it tells a
// recursive call from a repeated one and it costs nothing to keep.

#include <windows.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

extern "C" {

// The hook reads these four and nothing else. They are plain globals rather than
// a struct so the inline assembly can name them directly and stay short.
unsigned int   ntr_fn_trace_on   = 0;  // 1 while recording. Starts at 0, always.
unsigned int * ntr_fn_trace_buf  = 0;  // base of the record array
unsigned int   ntr_fn_trace_head = 0;  // next free WORD index
unsigned int   ntr_fn_trace_cap  = 0;  // capacity in WORDS

// THE HOOK. Kept to a compare and a branch on the disabled path, and to three
// pushes and five stores on the enabled one. It must be naked: a compiler-
// generated prologue here would itself be instrumented under /Gh.
//
// THE CAPACITY TEST IS ON head+3, NOT ON head. The two-word version compared
// head against cap and then wrote at head and head+1, so a head exactly one
// word below cap wrote one word past the end of the buffer. Nothing ever
// noticed because the buffer is a whole number of megabytes and the old record
// was two words, so head could never stop on an odd word -- but the record is
// three words now and that alignment argument is gone.
//
// THE CLAIM IS ONE LOCKED XADD (catchup1 Stage D round 4, TRACE4 section Q).
// SDL's input thread is instrumented too, and the read-head / write / store-head
// sequence this used to be let two threads take the same slot, so one record
// was overwritten: fold 9's four-player match row lost st_render at f322 under its own
// recorded child. Each caller now owns the three words its xadd returned. A
// claim that would run past cap is handed back, so head stays within
// cap + 3 x (threads in the hook) and write_out clamps it to whole records.
__declspec(naked) void __cdecl _penter(void)
{
    __asm {
        cmp     dword ptr [ntr_fn_trace_on], 0
        je      SHORT fnt_off
        push    eax
        push    ecx
        push    edx
        mov     eax, dword ptr [ntr_fn_trace_on]   // read the mode ONCE: the lab
        cmp     eax, 2                             // plugin switches it between
        je      fnt_lab                            // 0 and 2 from the game thread
        test    eax, eax
        je      SHORT fnt_full
        mov     ecx, 3
        lock xadd dword ptr [ntr_fn_trace_head], ecx   // claim 3 words; ecx = old head
        lea     eax, [ecx+3]
        cmp     eax, dword ptr [ntr_fn_trace_cap]
        ja      SHORT fnt_over
        mov     edx, dword ptr [ntr_fn_trace_buf]
        mov     eax, dword ptr [esp+12]     // return into the CALLEE: fn + 5
        mov     dword ptr [edx+ecx*4], eax
        mov     eax, dword ptr [esp+16]     // return into the CALLER: the call site
        mov     dword ptr [edx+ecx*4+4], eax
        lea     eax, [esp+16]               // the caller's esp before its call
        mov     dword ptr [edx+ecx*4+8], eax
        jmp     SHORT fnt_full
fnt_over:
        lock sub dword ptr [ntr_fn_trace_head], 3      // give the claim back
fnt_full:
        pop     edx
        pop     ecx
        pop     eax
fnt_off:
        ret
fnt_lab:
        // LAB MODE (ntr_fn_trace_on == 2, set only by the naming-lab plugin,
        // port/mods_api/naming_lab): four words a record -- callee+5, the call
        // site, ecx (`this` for a thiscall method: the callee's prologue has
        // not run, so ecx is still what the caller loaded) and the first stack
        // argument (`self` for the extern "C" functions that take the object
        // first). The plugin owns the buffer and drains it every frame; this
        // file never writes it to disk. ntr_fn_trace_frame leaves mode 2 alone
        // unless SM64DS_FN_TRACE is also set, which the lab never does.
        //
        // THE CLAIM IS A COMPARE-AND-SWAP, NOT AN XADD. The plugin zeroes head
        // from the game thread after each drain; an xadd that overshot cap and
        // handed its claim back with a `lock sub` could land that sub after the
        // zero, leaving head at 0xFFFFFFFC and the next record 16 bytes before
        // the buffer. Here a claim is only ever made when it fits, so head never
        // passes cap and there is nothing to hand back; a full buffer drops the
        // record. If head moved under us (another thread, or the drain's zero),
        // cmpxchg reloads eax and we try again.
        mov     eax, dword ptr [ntr_fn_trace_head]
fnt_lab_retry:
        lea     edx, [eax+4]
        cmp     edx, dword ptr [ntr_fn_trace_cap]
        ja      fnt_full                           // full: record nothing
        lock cmpxchg dword ptr [ntr_fn_trace_head], edx   // head == eax ? head = edx : eax = head
        jne     fnt_lab_retry
        mov     edx, dword ptr [ntr_fn_trace_buf]
        lea     edx, [edx+eax*4]
        mov     ecx, dword ptr [esp+12]     // return into the CALLEE: fn + 5
        mov     dword ptr [edx], ecx
        mov     ecx, dword ptr [esp+16]     // return into the CALLER: the call site
        mov     dword ptr [edx+4], ecx
        mov     ecx, dword ptr [esp+4]      // the caller's ecx, as pushed above
        mov     dword ptr [edx+8], ecx
        mov     ecx, dword ptr [esp+20]     // the first stack argument
        mov     dword ptr [edx+12], ecx
        jmp     fnt_full
    }
}

} // extern "C"

namespace {

char         g_path[512];
bool         g_configured = false;   // the environment has been read
bool         g_enabled    = false;   // SM64DS_FN_TRACE named a file
bool         g_written    = false;   // the file is on disk; never record again
unsigned int g_from       = 0;
unsigned int g_to         = 0xFFFFFFFFu;

unsigned int env_uint(const char *name, unsigned int fallback)
{
    const char *v = getenv(name);
    if (v == 0 || v[0] == 0) return fallback;
    return (unsigned int) strtoul(v, 0, 0);
}

void configure(void)
{
    g_configured = true;

    const char *out = getenv("SM64DS_FN_TRACE");
    if (out == 0 || out[0] == 0) return;

    // 8 bytes a record. The default holds about six hundred frames of the menu
    // page; SM64DS_FN_TRACE_MB moves it. A full buffer stops recording quietly
    // and the header says how many records were dropped, so a short buffer is
    // visible in the output instead of being a silent truncation.
    unsigned int mb = env_uint("SM64DS_FN_TRACE_MB", 96);
    if (mb < 1)    mb = 1;
    if (mb > 1024) mb = 1024;

    // A 32-bit process often has no contiguous 768 or 1024 MiB left by the time
    // this runs, and a refused VirtualAlloc used to end the trace with no file
    // and no word (catchup1 Stage D round 5). Step down 32 MiB at a time to the
    // largest buffer that fits, and say so on stderr either way.
    const unsigned int asked = mb;
    void *p = 0;
    size_t bytes = 0;
    for (; mb >= 1; mb = (mb > 32u) ? mb - 32u : mb - 1u) {
        bytes = (size_t) mb * 1024u * 1024u;
        p = VirtualAlloc(0, bytes, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
        if (p != 0) break;
    }
    if (p == 0) {
        fprintf(stderr, "[fn_trace] could not allocate even 1 MiB for the trace buffer "
                        "(asked %u MiB, error %lu): no trace will be written\n",
                asked, (unsigned long) GetLastError());
        fflush(stderr);
        return;
    }
    if (mb != asked)
        fprintf(stderr, "[fn_trace] SM64DS_FN_TRACE_MB=%u could not be allocated in this "
                        "process; recording into %u MiB instead\n", asked, mb);
    else
        fprintf(stderr, "[fn_trace] recording into %u MiB\n", mb);
    fflush(stderr);

    strncpy(g_path, out, sizeof(g_path) - 1);
    g_path[sizeof(g_path) - 1] = 0;

    ntr_fn_trace_buf  = (unsigned int *) p;
    ntr_fn_trace_cap  = (unsigned int) (bytes / 4u);
    ntr_fn_trace_head = 0;

    g_from    = env_uint("SM64DS_FN_TRACE_FROM", 0);
    g_to      = env_uint("SM64DS_FN_TRACE_TO", 0xFFFFFFFFu);
    g_enabled = true;
}

void write_out(void)
{
    if (!g_enabled || g_written) return;
    g_written = true;
    ntr_fn_trace_on = 0;

    FILE *f = fopen(g_path, "wb");
    if (f == 0) {
        fprintf(stderr, "[fn_trace] cannot open %s for writing: the trace is lost\n", g_path);
        fflush(stderr);
        return;
    }
    // A 24-byte header so the reader can refuse a file it does not understand,
    // can see a truncation rather than guess at one, and can undo ASLR. THE
    // IMAGE BASE MATTERS: walk_window.map is written against the preferred load
    // address 0x00400000 and Windows relocates the image somewhere else on every
    // run, so without this word every address in the file resolves to the wrong
    // name, or to no name, and the whole trace reads as a build that called
    // nothing this repository knows.
    unsigned int hdr[6];
    hdr[0] = 0x50544633u;              // "PTF3": three words a record
    hdr[1] = 12;                       // bytes per record
    // head can sit a claim or two past cap while a thread hands back a refused
    // claim; only whole records below cap were written.
    const unsigned int head = InterlockedCompareExchange(
        (volatile LONG *) &ntr_fn_trace_head, 0, 0);
    const unsigned int words = (head + 3u > ntr_fn_trace_cap)
        ? (ntr_fn_trace_cap / 3u) * 3u : head;
    hdr[2] = words / 3u;               // records written
    // buffer filled: the record path's own refusal test (head + 3 > cap). The
    // old head >= cap never fired for a whole-MiB cap, which is not a multiple
    // of three words, so head stopped one or two words short of cap.
    hdr[3] = (head + 3u > ntr_fn_trace_cap) ? 1u : 0u;
    hdr[4] = (unsigned int) (size_t) GetModuleHandleA(0);  // where it really loaded
    hdr[5] = 0;
    fwrite(hdr, 4, 6, f);
    fwrite(ntr_fn_trace_buf, 4, words, f);
    fclose(f);
}

} // namespace

extern "C" void ntr_fn_trace_frame(unsigned int frame)
{
    if (!g_configured) configure();
    if (!g_enabled || g_written) return;

    if (frame > g_to) {
        write_out();
        return;
    }
    if (frame < g_from) {
        ntr_fn_trace_on = 0;
        return;
    }

    // The marker is written with the recorder off so the frame hook's own entry
    // does not land between the marker and the first call of the frame.
    ntr_fn_trace_on = 0;
    // Claimed the way _penter claims, since another instrumented thread may be
    // inside the hook right now.
    {
        const unsigned int at = (unsigned int) InterlockedExchangeAdd(
            (volatile LONG *) &ntr_fn_trace_head, 3);
        if (at + 3u <= ntr_fn_trace_cap) {
            ntr_fn_trace_buf[at + 0] = 0xFFFFFFFFu;
            ntr_fn_trace_buf[at + 1] = frame;
            ntr_fn_trace_buf[at + 2] = 0u;
        } else {
            InterlockedExchangeAdd((volatile LONG *) &ntr_fn_trace_head, -3);
        }
    }
    ntr_fn_trace_on = 1;
}

// Last resort: a run that ends before the frame window closes still leaves a
// readable file. atexit is registered on the first frame rather than at static
// init so an untraced run registers nothing.
extern "C" void ntr_fn_trace_flush(void) { write_out(); }
