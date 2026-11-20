/* hal/lab_plugins.cpp -- the tracing build's code-plugin loader (plugin API v1,
   mods_api/sm64ds_mod.h), for the naming lab (mods_api/naming_lab).

   WHAT AND WHERE. Compiled into walk_window ONLY when PORT_FN_TRACE is on, so
   the shipped build has no loader and no plugin can ever reach a player's game
   through it. In a tracing build it loads each code mod in mods/<id>/ beside
   the exe: mod.json says "kind": "code" and "api": 1, and plugin/ holds exactly
   one DLL exporting sm64ds_mod_main. No mods/ folder (the usual case for a
   tracing run) means nothing is loaded and the two hooks cost one test each.
   SM64DS_MODS=0 switches it off, the same switch the custom line uses.

   THE SAME API AS THE CUSTOM LINE, A SUBSET OF ITS POWERS. The struct, the
   callback signatures and when they run are the custom line's
   (hal/mod_plugins.cpp there), so a plugin built for one runs on the other.
   What this loader offers: log, setting, on_frame, on_level_load, symbol,
   alloc, free. What it refuses, with a log line: on_file and replace_actor
   (they need the custom line's file layer and actor registry seams, and the
   lab uses neither). Not here either: launcher priority order (folders load in
   name order), minGameVersion, the multiplayer switch-off and the code-patch
   journal, which only matter where there is multiplayer and a launcher.

   THE SEAMS are hal/mod_hooks.h's two pointers, called at the end of each
   frame's actor work (hal/actor_registry.cpp) and when a level has booted
   (hal/level_boot.cpp). They are set at static initialisation; the plugins
   themselves load on the first call of either, when the game is up.

   EVERY CALL INTO A PLUGIN IS GUARDED (structured exception handling): a
   plugin that faults is switched off for the rest of the run with one log
   line, and the game carries on. A plugin that blocks or corrupts memory
   cannot be guarded against.

   THIS FILE IS ITSELF TRACED. The tracing build turns /Gh on for the whole
   directory (add_compile_options) and MSVC has no per-file way back, so the
   loader's own few calls a frame (frame_hook, the guarded callers, the api_*
   functions a plugin calls) land in the trace like any game function. They
   never carry an actor as `this` or first argument, so the lab's per-actor
   views do not show them; its whole-game hot counts do. */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <malloc.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <map>
#include <string>
#include <unordered_map>
#include <vector>

#include "mod_hooks.h"
#include "../mods_api/sm64ds_mod.h"

namespace {

enum { kMaxCb = 16 };

struct FrameCb { SM64DSFrameFn fn; void *user; };
struct LevelCb { SM64DSLevelFn fn; void *user; };

struct Plugin {
    std::string id, folder;
    HMODULE dll;
    SM64DSModApi api;
    bool dead;
    std::vector<FrameCb> frame;
    std::vector<LevelCb> level;
    std::map<std::string, std::string> settings;
    std::string values;
    bool values_read;
    Plugin() : dll(0), dead(false), values_read(false) { memset(&api, 0, sizeof api); }
};

std::vector<Plugin *> g_plugins;
bool g_loaded;
std::string g_exe_dir;

/* ---- guarded calls: tiny functions with no C++ objects, so __try is legal ---- */

DWORD g_fault_code;

int fault_filter(unsigned long code)
{
    g_fault_code = code;
    return EXCEPTION_EXECUTE_HANDLER;
}

/* a stack overflow consumed the guard page; put it back or the next one is a
   plain crash */
void after_fault(void)
{
    if (g_fault_code == EXCEPTION_STACK_OVERFLOW)
        _resetstkoflw();
}

int g_main_rc;
typedef int (__cdecl *MainFn)(const SM64DSModApi *);

bool seh_main(MainFn fn, const SM64DSModApi *api)
{
    __try { g_main_rc = fn(api); return true; }
    __except (fault_filter(GetExceptionCode())) { after_fault(); return false; }
}

bool seh_frame(SM64DSFrameFn fn, void *user)
{
    __try { fn(user); return true; }
    __except (fault_filter(GetExceptionCode())) { after_fault(); return false; }
}

bool seh_level(SM64DSLevelFn fn, int level, void *user)
{
    __try { fn(level, user); return true; }
    __except (fault_filter(GetExceptionCode())) { after_fault(); return false; }
}

void kill_plugin(Plugin *p, const char *where)
{
    if (p->dead) return;
    p->dead = true;
    fprintf(stderr, "[plugins] %s faulted in %s (exception %08lx): switched off for the rest of this run\n",
            p->id.c_str(), where, (unsigned long)g_fault_code);
    fflush(stderr);
}

/* ---- small helpers ---- */

char *slurp(const std::string &path)
{
    FILE *f = fopen(path.c_str(), "rb");
    if (!f) return 0;
    if (fseek(f, 0, SEEK_END) != 0) { fclose(f); return 0; }
    long n = ftell(f);
    if (n < 0 || n > (1 << 20)) { fclose(f); return 0; }
    fseek(f, 0, SEEK_SET);
    char *b = (char *)malloc((size_t)n + 1);
    if (!b) { fclose(f); return 0; }
    size_t got = fread(b, 1, (size_t)n, f);
    fclose(f);
    b[got] = 0;
    return b;
}

/* The value of "key" in a flat JSON object, as text: a string without its
   quotes (no escapes beyond \" and \\), or a bare number / true / false. Enough
   for mod.json and settings.values.json, which are flat; returns false when the
   key is absent. */
bool json_value(const char *text, const char *key, std::string *out)
{
    const std::string want = std::string("\"") + key + "\"";
    for (const char *p = strstr(text, want.c_str()); p; p = strstr(p + 1, want.c_str())) {
        const char *q = p + want.size();
        while (*q == ' ' || *q == '\t' || *q == '\r' || *q == '\n') ++q;
        if (*q != ':') continue;
        ++q;
        while (*q == ' ' || *q == '\t' || *q == '\r' || *q == '\n') ++q;
        out->clear();
        if (*q == '"') {
            for (++q; *q && *q != '"'; ++q) {
                char c = *q;
                if (c == '\\' && q[1]) {
                    c = *++q;
                    if (c == 'n') c = '\n';
                    else if (c == 't') c = '\t';
                    else if (c == 'r') c = '\r';
                }
                out->push_back(c);
            }
            return true;
        }
        while (*q && *q != ',' && *q != '}' && *q != ' ' && *q != '\r' && *q != '\n' && *q != '\t')
            out->push_back(*q++);
        return !out->empty() && *out != "null";   /* null: as good as absent */
    }
    return false;
}

bool hexval(const char *s, size_t n, unsigned *v)
{
    if (!n || n > 8) return false;
    unsigned r = 0;
    for (size_t i = 0; i < n; ++i) {
        const char c = s[i];
        r <<= 4;
        if (c >= '0' && c <= '9') r |= (unsigned)(c - '0');
        else if (c >= 'a' && c <= 'f') r |= (unsigned)(c - 'a' + 10);
        else if (c >= 'A' && c <= 'F') r |= (unsigned)(c - 'A' + 10);
        else return false;
    }
    *v = r;
    return true;
}

/* ---- symbol(): the link's names, from walk_window.symtab or the map ---- */

std::unordered_map<std::string, unsigned> g_syms;
unsigned g_sym_base = 0x400000;
bool g_syms_tried;

/* one table into g_syms: walk_window.symtab (tab-separated, with a build stamp
   that has to match the running exe) or the linker's map; false, with g_syms
   left empty, when the file is missing or was made for another build */
bool read_syms(const std::string &path, bool from_tab)
{
    FILE *f = fopen(path.c_str(), "rb");
    if (!f) return false;
    const unsigned char *im = (const unsigned char *)GetModuleHandleA(0);
    const IMAGE_NT_HEADERS32 *nt = (const IMAGE_NT_HEADERS32 *)(im + ((const IMAGE_DOS_HEADER *)im)->e_lfanew);
    const unsigned run_stamp = nt->FileHeader.TimeDateStamp, run_size = nt->OptionalHeader.SizeOfImage;
    bool have_stamp = false, stamp_ok = true;
    char line[4096];
    while (fgets(line, sizeof line, f)) {
        if (from_tab) {
            if (line[0] == '#') {
                const char *b = strstr(line, "base="), *ts = strstr(line, "stamp="), *sz = strstr(line, "size=");
                unsigned v, t, z;
                if (b && hexval(b + 5, strspn(b + 5, "0123456789abcdefABCDEF"), &v)) g_sym_base = v;
                if (ts && sz && hexval(ts + 6, strspn(ts + 6, "0123456789abcdefABCDEF"), &t) &&
                    hexval(sz + 5, strspn(sz + 5, "0123456789abcdefABCDEF"), &z)) {
                    have_stamp = true;
                    stamp_ok = (t == run_stamp && z == run_size);
                }
                continue;
            }
            char *tab = strchr(line, '\t');
            if (!tab) continue;
            *tab = 0;
            unsigned a;
            if (hexval(tab + 1, strcspn(tab + 1, "\r\n"), &a))
                g_syms.insert(std::make_pair(std::string(line), a));
        } else {
            /* " 0001:0008b150       _name      0048c150 f   file.obj" */
            const char *pl = strstr(line, "Preferred load address is ");
            unsigned v;
            if (pl && hexval(pl + 26, 8, &v)) { g_sym_base = v; continue; }
            char sec[32], name[2048], addr[32];
            if (sscanf(line, " %31s %2047s %31s", sec, name, addr) != 3) continue;
            if (strlen(sec) != 13 || sec[4] != ':') continue;
            unsigned a;
            if (strlen(addr) != 8 || !hexval(addr, 8, &a)) continue;
            g_syms.insert(std::make_pair(std::string(name), a));
        }
    }
    fclose(f);
    if (from_tab && (!have_stamp || !stamp_ok)) {
        fprintf(stderr, "[plugins] walk_window.symtab %s; trying walk_window.map\n",
                have_stamp ? "was made for another build (timestamp or image size differ)" : "has no build stamp");
        g_syms.clear();
        return false;
    }
    return true;
}

void load_syms(void)
{
    if (g_syms_tried) return;
    g_syms_tried = true;
    /* walk_window.symtab (port/tools/mk_symtab.py, written beside the exe by the
       tracing build) first; the map the same link wrote is the fallback, when the
       table is missing or stale (mk_symtab failed after a relink) */
    const char *from = "walk_window.symtab";
    if (!read_syms(g_exe_dir + from, true)) {
        g_sym_base = 0x400000;
        from = "walk_window.map";
        if (!read_syms(g_exe_dir + from, false)) {
            fprintf(stderr, "[plugins] no usable walk_window.symtab or .map beside the exe: symbol() answers NULL\n");
            return;
        }
    }
    fprintf(stderr, "[plugins] symbol table: %u names from %s\n", (unsigned)g_syms.size(), from);
}

/* ---- the API the plugin sees ---- */

Plugin *of(const SM64DSModApi *api) { return (Plugin *)api->game_private; }

void __cdecl api_log(const SM64DSModApi *api, const char *fmt, ...)
{
    char buf[1024];
    va_list ap;
    va_start(ap, fmt);
    _vsnprintf(buf, sizeof buf - 1, fmt ? fmt : "", ap);
    va_end(ap);
    buf[sizeof buf - 1] = 0;
    fprintf(stderr, "[mod:%s] %s\n", of(api)->id.c_str(), buf);
    fflush(stderr);
}

const char *__cdecl api_setting(const SM64DSModApi *api, const char *key)
{
    Plugin *p = of(api);
    if (!key || !*key) return 0;
    if (!p->values_read) {
        p->values_read = true;
        char *t = slurp(p->folder + "/settings.values.json");
        if (t) { p->values = t; free(t); }
    }
    std::map<std::string, std::string>::const_iterator it = p->settings.find(key);
    if (it != p->settings.end()) return it->second.c_str();
    std::string v;
    if (p->values.empty() || !json_value(p->values.c_str(), key, &v)) return 0;
    return (p->settings[key] = v).c_str();
}

int __cdecl api_on_frame(const SM64DSModApi *api, SM64DSFrameFn cb, void *user)
{
    Plugin *p = of(api);
    if (!cb || (int)p->frame.size() >= kMaxCb) return 1;
    FrameCb c = { cb, user };
    p->frame.push_back(c);
    return 0;
}

int __cdecl api_on_level_load(const SM64DSModApi *api, SM64DSLevelFn cb, void *user)
{
    Plugin *p = of(api);
    if (!cb || (int)p->level.size() >= kMaxCb) return 1;
    LevelCb c = { cb, user };
    p->level.push_back(c);
    return 0;
}

int __cdecl api_on_file(const SM64DSModApi *api, const char *rom_path, SM64DSFileFn filter, void *user)
{
    (void)filter; (void)user;
    api_log(api, "on_file(%s): not offered by the tracing build's loader (no file layer here)", rom_path ? rom_path : "");
    return 1;
}

SM64DSActorFactory __cdecl api_replace_actor(const SM64DSModApi *api, unsigned id, SM64DSActorFactory factory)
{
    (void)factory;
    api_log(api, "replace_actor(%u): not offered by the tracing build's loader", id);
    return 0;
}

void *__cdecl api_symbol(const SM64DSModApi *api, const char *name)
{
    (void)api;
    if (!name || !*name) return 0;
    load_syms();
    std::unordered_map<std::string, unsigned>::const_iterator it = g_syms.find(name);
    if (it == g_syms.end() && name[0] != '_' && name[0] != '?')
        it = g_syms.find(std::string("_") + name);
    if (it == g_syms.end()) return 0;
    return (void *)(size_t)(it->second - g_sym_base + (unsigned)(size_t)GetModuleHandleA(0));
}

std::map<void *, size_t> g_allocs;

void *__cdecl api_alloc(const SM64DSModApi *api, size_t size)
{
    (void)api;
    void *r = malloc(size ? size : 1);
    if (r) g_allocs[r] = size;
    return r;
}

void __cdecl api_free(const SM64DSModApi *api, void *ptr)
{
    if (!ptr) return;
    std::map<void *, size_t>::iterator it = g_allocs.find(ptr);
    if (it == g_allocs.end()) {
        api_log(api, "free(%p): not a block from api->alloc (or already freed), ignored", ptr);
        return;
    }
    g_allocs.erase(it);
    free(ptr);
}

/* ---- loading ---- */

/* the one DLL in <folder>/plugin/; empty + a reason when there is not exactly one */
std::string find_dll(const std::string &folder, std::string *why)
{
    WIN32_FIND_DATAA fd;
    const std::string dir = folder + "\\plugin";
    HANDLE h = FindFirstFileA((dir + "\\*.dll").c_str(), &fd);
    std::string found;
    int n = 0;
    if (h != INVALID_HANDLE_VALUE) {
        do {
            if (fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) continue;
            if (!n) found = dir + "\\" + fd.cFileName;
            ++n;
        } while (FindNextFileA(h, &fd));
        FindClose(h);
    }
    if (n == 0) { *why = "no plugin\\*.dll"; return ""; }
    if (n > 1) { *why = "more than one DLL in plugin\\ (one per mod)"; return ""; }
    return found;
}

/* by full path, searching only the DLL's own folder and System32 for its imports */
HMODULE load_plugin_dll(const std::string &dll)
{
    HMODULE h = LoadLibraryExA(dll.c_str(), 0, LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_SYSTEM32);
    /* only where the LOAD_LIBRARY_SEARCH_* flags are not understood (a Windows
       without KB2533623); any other failure stays a failure, so a missing import
       is never looked for in the working folder or on PATH */
    if (!h && GetLastError() == ERROR_INVALID_PARAMETER)
        h = LoadLibraryExA(dll.c_str(), 0, LOAD_WITH_ALTERED_SEARCH_PATH);
    return h;
}

void load_one(const std::string &id, const std::string &folder)
{
    char *mj = slurp(folder + "\\mod.json");
    if (!mj) return;   /* not a mod folder */
    std::string kind, api;
    const bool have_kind = json_value(mj, "kind", &kind), have_api = json_value(mj, "api", &api);
    free(mj);
    if (!have_kind || kind != "code") return;   /* a files mod: nothing for this loader */
    const int want = have_api ? atoi(api.c_str()) : 0;
    if (want < SM64DS_MOD_API_MIN_VERSION || want > SM64DS_MOD_API_VERSION) {
        fprintf(stderr, "[plugins] skipped %s: mod.json api %s, this game offers %d..%d\n", id.c_str(),
                have_api ? api.c_str() : "(missing)", SM64DS_MOD_API_MIN_VERSION, SM64DS_MOD_API_VERSION);
        return;
    }
    std::string why;
    const std::string dll = find_dll(folder, &why);
    if (dll.empty()) { fprintf(stderr, "[plugins] skipped %s: %s\n", id.c_str(), why.c_str()); return; }
    HMODULE h = load_plugin_dll(dll);
    if (!h) {
        fprintf(stderr, "[plugins] skipped %s: LoadLibrary failed (error %lu)\n", id.c_str(), (unsigned long)GetLastError());
        return;
    }
    MainFn fn = (MainFn)GetProcAddress(h, "sm64ds_mod_main");
    if (!fn) fn = (MainFn)GetProcAddress(h, "_sm64ds_mod_main");
    if (!fn) {
        fprintf(stderr, "[plugins] skipped %s: no sm64ds_mod_main export\n", id.c_str());
        FreeLibrary(h);
        return;
    }
    Plugin *p = new Plugin;
    p->id = id;
    p->folder = folder;
    p->dll = h;
    SM64DSModApi &a = p->api;
    a.size = sizeof(SM64DSModApi);
    a.api_version = SM64DS_MOD_API_VERSION;
    a.game_release = "plain (tracing build)";
    a.mod_id = p->id.c_str();
    a.mod_folder = p->folder.c_str();
    a.game_private = p;
    a.log = api_log;
    a.setting = api_setting;
    a.on_frame = api_on_frame;
    a.on_level_load = api_on_level_load;
    a.on_file = api_on_file;
    a.replace_actor = api_replace_actor;
    a.symbol = api_symbol;
    a.alloc = api_alloc;
    a.free = api_free;
    g_plugins.push_back(p);
    if (!seh_main(fn, &p->api)) { kill_plugin(p, "sm64ds_mod_main"); return; }
    if (g_main_rc != 0) {
        p->dead = true;
        fprintf(stderr, "[plugins] %s: sm64ds_mod_main returned %d, plugin off\n", id.c_str(), g_main_rc);
        return;
    }
    fprintf(stderr, "[plugins] loaded %s (%s)\n", id.c_str(), dll.c_str());
}

void load_all(void)
{
    if (g_loaded) return;
    g_loaded = true;
    char exe[MAX_PATH];
    const DWORD n = GetModuleFileNameA(0, exe, sizeof exe);
    if (!n || n >= sizeof exe) return;
    g_exe_dir.assign(exe, n);
    g_exe_dir.erase(g_exe_dir.find_last_of("\\/") + 1);
    const std::string mods = g_exe_dir + "mods";
    WIN32_FIND_DATAA fd;
    HANDLE h = FindFirstFileA((mods + "\\*").c_str(), &fd);
    if (h == INVALID_HANDLE_VALUE) return;
    std::vector<std::string> ids;
    do {
        if (!(fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) || fd.cFileName[0] == '.') continue;
        ids.push_back(fd.cFileName);
    } while (FindNextFileA(h, &fd));
    FindClose(h);
    for (size_t i = 0; i < ids.size(); ++i)
        load_one(ids[i], mods + "\\" + ids[i]);
    fflush(stderr);
}

void frame_hook(void)
{
    load_all();
    for (size_t i = 0; i < g_plugins.size(); ++i) {
        Plugin *p = g_plugins[i];
        for (size_t k = 0; k < p->frame.size() && !p->dead; ++k)
            if (!seh_frame(p->frame[k].fn, p->frame[k].user))
                kill_plugin(p, "on_frame");
    }
}

void level_hook(int level_id)
{
    load_all();
    for (size_t i = 0; i < g_plugins.size(); ++i) {
        Plugin *p = g_plugins[i];
        for (size_t k = 0; k < p->level.size() && !p->dead; ++k)
            if (!seh_level(p->level[k].fn, level_id, p->level[k].user))
                kill_plugin(p, "on_level_load");
    }
}

/* installed at static initialisation; the plugins load on the first call */
struct InstallHooks {
    InstallHooks()
    {
        const char *off = getenv("SM64DS_MODS");
        if (off && off[0] == '0' && !off[1]) return;
        port_mod_hook_frame = frame_hook;
        port_mod_hook_level = level_hook;
    }
};
InstallHooks g_install_hooks;

}  /* namespace */
