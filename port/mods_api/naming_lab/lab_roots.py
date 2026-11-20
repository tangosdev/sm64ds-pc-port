"""The two roots the lab reads from.

PORT_ROOT is the checkout that holds port/ and build/: the game, its symbol
table and the asset catalog live under PORT_ROOT/build. DECOMP_ROOT is the decomp
the game was compiled from: src/, include/, config/, tools/ and extracted/, and
the git history the field-name overlay reads. In this repo that is the decomp\\
submodule. The rule follows port\\decomp_root.cmd's environment and folder steps:
  1. SM64DS_DECOMP_ROOT from the environment;
  2. PORT_ROOT/decomp when it holds src/ and include/ (this repo's layout);
  3. PORT_ROOT itself (the old layout, port/ inside a decomp checkout).
A build configured with -DDECOMP_ROOT=<path> leaves no trace the lab can read:
set SM64DS_DECOMP_ROOT to the same path, or the lab reads the submodule.
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
PORT_ROOT = os.path.normpath(os.path.join(HERE, '..', '..', '..'))


def _decomp_root():
    env = os.environ.get('SM64DS_DECOMP_ROOT')
    if env:
        return os.path.normpath(os.path.abspath(env))
    sub = os.path.join(PORT_ROOT, 'decomp')
    if os.path.isdir(os.path.join(sub, 'src')) and os.path.isdir(os.path.join(sub, 'include')):
        return sub
    return PORT_ROOT


DECOMP_ROOT = _decomp_root()
