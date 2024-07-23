#!/usr/bin/python2

# This script is a wrapper around zim.main.main() for running zim as
# an application.


import sys
import logging
import os

# Check if we run the correct python version
try:
	version_info = sys.version_info
	assert version_info >= (2, 6)
	assert version_info < (3, 0)
except:
	print >> sys.stderr, 'ERROR: zim needs python >= 2.6   (but < 3.0)'
	sys.exit(1)


# python 3.3 way to enable faulthandler to dump the tracebacks on a specific signal (e.g., SIGUSR1)
#import faulthandler
#import signal
#faulthandler.register(signal.SIGUSR1)

# python 2.7 way
import sys
import threading
import traceback
import signal

def dump_threads(signum, frame):
    print("\n\nSignal received, dumping all threads:")
    for thread_id, frame in sys._current_frames().items():
        print("\n\nThread ID: %s" % thread_id)
        traceback.print_stack(frame)

# Register the signal handler
signal.signal(signal.SIGUSR1, dump_threads)


# Win32: must setup log file or it tries to write to $PROGRAMFILES
# See http://www.py2exe.org/index.cgi/StderrLog
# If startup is OK, this will be overruled in zim/main with per user log file
if os.name == "nt" and (
	sys.argv[0].endswith('.exe')
	or sys.executable.endswith('pythonw.exe')
):
	import tempfile
	dir = tempfile.gettempdir()
	if not os.path.isdir(dir):
		os.makedirs(dir)
	err_stream = open(dir + "\\zim.exe.log", "w")
	sys.stdout = err_stream
	sys.stderr = err_stream

# Preliminary initialization of logging because modules can throw warnings at import
logging.basicConfig(level=logging.WARN, format='%(levelname)s: %(message)s')
logging.captureWarnings(True)

# Try importing our modules
try:
	import zim
	import zim.main
except ImportError:
	sys.excepthook(*sys.exc_info())
	print >>sys.stderr, 'ERROR: Could not find python module files in path:'
	print >>sys.stderr, ' '.join(map(str, sys.path))
	print >>sys.stderr, '\nTry setting PYTHONPATH'
	sys.exit(1)


# Run the application and handle some exceptions
try:
	#encoding = sys.getfilesystemencoding() # not 100% sure this is correct
	#argv = [arg.decode(encoding) for arg in sys.argv]
	argv = [arg.decode('utf-8') for arg in sys.argv]
	exitcode = zim.main.main(*argv)
	sys.exit(exitcode)
except zim.main.GetoptError as err:
	print >>sys.stderr, sys.argv[0] + ':', err
	sys.exit(1)
except zim.main.UsageError as err:
	print >>sys.stderr, err.msg
	sys.exit(1)
except KeyboardInterrupt: # e.g. <Ctrl>C while --server
	print >>sys.stderr, 'Interrupt'
	sys.exit(1)
else:
	sys.exit(0)
