/* hal/mod_hooks.h -- the two seams a code plugin hangs on: once per frame and
   once per level load. They are the same seams, at the same two call sites, as
   the custom line's plugin loader uses, so a plugin written against
   mods_api/sm64ds_mod.h behaves the same on both lines.

   Each is a plain function pointer that stays null unless a loader installs
   one, so a build with no loader (the shipped build, and every smoke target)
   pays one load and one test per call and needs nothing else. They are
   selectany, so every translation unit that mentions one shares a single
   zero-initialised copy and no source file has to own them.

   The loader in this repo is hal/lab_plugins.cpp, compiled only into the
   tracing build (PORT_FN_TRACE), for the naming lab (mods_api/naming_lab). */
#ifndef PORT_HAL_MOD_HOOKS_H
#define PORT_HAL_MOD_HOOKS_H

#ifdef __cplusplus
extern "C" {
#endif

/* once per loop frame, at the end of the frame's actor work (the level loop
   and the scene loop both): hal/actor_registry.cpp, port_actor_frame_end */
__declspec(selectany) void (*port_mod_hook_frame)(void) = 0;

/* a level has finished booting: hal/level_boot.cpp, end of port_stage_a_boot */
__declspec(selectany) void (*port_mod_hook_level)(int level_id) = 0;

#ifdef __cplusplus
}
#endif

#endif
