"""Explicit Abaqus/CAE entry point for preserving independently built HSS CAEs."""
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(
    globals().get('__file__', sys._getframe().f_code.co_filename)))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from abaqus_hss_pipeline import main

# Abaqus may execute noGUI scripts outside the __main__ namespace.
print('[HSS CAE DRIVER] Started', flush=True)
try:
    main()
except SystemExit as error:
    # CAE can report argparse's SystemExit(2) as launcher exit code zero.
    if error.code not in (None, 0):
        raise RuntimeError('HSS CAE argument parsing failed (exit %s); see HSS_CAE_save.log and abaqus.rpy' %
                           error.code) from error
