# -*- coding: utf-8 -*-

# Copyright 2009 Jaap Karssenberg <jaap.karssenberg@gmail.com>

import subprocess
import gtk

from zim.plugins.base.imagegenerator import ImageGeneratorPlugin, ImageGeneratorClass
from zim.fs import File, TmpFile
from zim.config import data_file
from zim.applications import Application, ApplicationError


# TODO put these commands in preferences
dotcmd = ('mscgen', '-Tsvg', '-o')

class InsertSequenceDiagramPlugin(ImageGeneratorPlugin):

	plugin_info = {
		'name': _('Insert Sequence Diagram'), # T: plugin name
		'description': _('''\
This plugin provides a diagram editor for zim based on GraphViz.

This is based on the diagrameditor core plugin that ships with zim, but
obscures the default sequencediagrameditor that would ordinarily be used.
'''), # T: plugin description
		'help': 'Plugins:Diagram Editor',
		'author': 'Jaap Karssenberg',
	}

	object_type = 'mscdiagram'
	short_label = _('Sequence Diagram...') # T: menu item
	insert_label = _('Insert Sequence Diagram') # T: menu item
	edit_label = _('_Edit Sequence Diagram') # T: menu item
	syntax = 'dot'

	@classmethod
	def check_dependencies(klass):
		has_dotcmd = Application(dotcmd).tryexec()
		return has_dotcmd, [("mscgen", has_dotcmd, True)]


class SequenceDiagramGenerator(ImageGeneratorClass):

	uses_log_file = False

	object_type = 'mscdiagram'
	scriptname = 'diagram.msc'
	imagename = 'diagram.svg'

	def __init__(self, plugin):
		ImageGeneratorClass.__init__(self, plugin)
		self.dotfile = TmpFile(self.scriptname)
		self.dotfile.touch()
		#self.logfile = TmpFile(self.scriptname+'.log')
		self.svgfile = File(self.dotfile.path[:-4] + '.svg') # len('.dot') == 4

	def generate_image(self, text):
		# Write to tmp file
		self.dotfile.write(text)

		# Call GraphViz

		argv = dotcmd + tuple(map(unicode, (self.svgfile.path, self.dotfile.path)));
		p = subprocess.Popen(argv, cwd=None, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
		stdout, stderr = p.communicate(text)
		
		output = self._read_all(stdout)
		diagnostics = self._read_all(stderr)
		
		#self.logfile.write(stderr)
		
		if p.returncode == 0:
			if self.svgfile.exists():
				return self.svgfile, None
			else:
				return None, None;
		else:
			# Present a [modal?] dialog box with the diagnostic output & exit code.
			m = gtk.MessageDialog(
								  gtk.Window(),
								  gtk.DIALOG_MODAL,
								  gtk.MESSAGE_ERROR,
								  gtk.BUTTONS_NONE,
								  "Could not create image"
								  )
			m.format_secondary_text("\n".join(diagnostics + ["Code: %s" % p.returncode]))
			m.show()
			return None, None;

	def _read_all(self, stdout):
		text = [unicode(line + '\n', errors='replace') for line in stdout.splitlines()]
		if text and text[-1].endswith('\n') and not stdout.endswith('\n'):
			text[-1] = text[-1][:-1] # strip additional \n
		return text

	def cleanup(self):
		self.dotfile.remove()
		self.svgfile.remove()
