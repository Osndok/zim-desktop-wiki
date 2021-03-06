# -*- coding: utf-8 -*-

# Copyright 2009 Jaap Karssenberg <jaap.karssenberg@gmail.com>

import subprocess
import gtk

from zim.plugins.base.imagegenerator import ImageGeneratorPlugin, ImageGeneratorClass
from zim.fs import File, TmpFile
from zim.config import data_file
from zim.applications import Application, ApplicationError


# TODO put these commands in preferences
dotargs = ['-Tsvg', '-Nfontname=DejaVuSansMono', '-Efontname=DejaVuSansMono', '-Gfontname=DejaVuSansMono', '-o']

class InsertDiagramPlugin(ImageGeneratorPlugin):

	plugin_info = {
		'name': _('Insert Diagram'), # T: plugin name
		'description': _('''\
This plugin provides a diagram editor for zim based on GraphViz.

This is a core plugin shipping with zim.
'''), # T: plugin description
		'help': 'Plugins:Diagram Editor',
		'author': 'Jaap Karssenberg',
	}

	object_type = 'diagram'
	short_label = _('Dia_gram...') # T: menu item
	insert_label = _('Insert diagram') # T: menu item
	edit_label = _('_Edit diagram') # T: menu item
	syntax = 'dot'

	@classmethod
	def check_dependencies(klass):
		has_dotcmd = Application(("dot")).tryexec()
		return has_dotcmd, [("GraphViz", has_dotcmd, True)]


class DiagramGenerator(ImageGeneratorClass):

	uses_log_file = False

	object_type = 'diagram'
	scriptname = 'diagram.dot'
	imagename = 'diagram.svg'

	def __init__(self, plugin):
		ImageGeneratorClass.__init__(self, plugin)
		self.dotfile = TmpFile(self.scriptname)
		self.dotfile.touch()
		#self.logfile = TmpFile(self.scriptname+'.log')
		self.svgfile = File(self.dotfile.path[:-4] + '.svg') # len('.dot') == 4

	def get_default_text(self):
		return (
				"digraph %s\n"
				"{\n"
				"\t#rankdir=LR;\n"
				'\tnode[shape=rect, style="rounded,filled", fillcolor=white, margin="0.2,0.055"];\n'
				'\n'
				'\tsubgraph Green\n'
				'\t{\n'
				'\t\tnode[fillcolor=green];\n'
				'\t\tGreenNode\n'
				'\t}\n'
				'\n'
				'\t\n'
				"}\n"
				) % (self.page.name.replace(":",""));

	def generate_image(self, text):
		# Write to tmp file
		self.dotfile.write(text)

		firstLine=text.split('\n', 1)[0];

		if (firstLine == "#circular") or (firstLine == "#circo"):
			exe = ["circo"]
		elif (firstLine == "#cluster" ) or (firstLine == "#osage"):
			exe = ["osage"]
		elif (firstLine == "#radial") or (firstLine == "#twopi"):
			exe = ["twopi"]
		elif (firstLine == "#spring") or (firstLine == "#neato"):
			exe = ["neato"]
		elif (firstLine == "#spring2") or (firstLine == "#fdp"):
			exe = ["fdp"]
		elif (firstLine == "#large") or (firstLine == "#sfdp"):
			exe = ["sfdp"]
		elif (firstLine == "#squaretree") or (firstLine == "#patchwork"):
			exe = ["patchwork"]
		else:
			exe = ["dot"]

		# Call GraphViz
		argv = exe + dotargs + list(map(unicode, (self.svgfile.path, self.dotfile.path)));
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
