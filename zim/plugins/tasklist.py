# -*- coding: utf-8 -*-

# Copyright 2009 Jaap Karssenberg <jaap.karssenberg@gmail.com>

from __future__ import with_statement

import gobject
import gtk
import pango
import logging
import re

import zim.datetimetz as datetime
from zim.parsing import parse_date
from zim.plugins import PluginClass
from zim.notebook import Path
from zim.gui.widgets import ui_environment, \
	Dialog, MessageDialog, \
	InputEntry, Button, IconButton, MenuButton, \
	BrowserTreeView, SingleClickTreeView
from zim.async import DelayedCallback
from zim.formats import get_format, UNCHECKED_BOX, CHECKED_BOX, XCHECKED_BOX
from zim.config import check_class_allow_empty

from zim.plugins.calendar import daterange_from_path

logger = logging.getLogger('zim.plugins.tasklist')


ui_actions = (
	# name, stock id, label, accelerator, tooltip, read only
	('show_task_list', 'zim-task-list', _('Task List'), '', _('Task List'), True), # T: menu item
)

ui_xml = '''
<ui>
	<menubar name='menubar'>
		<menu action='view_menu'>
			<placeholder name="plugin_items">
				<menuitem action="show_task_list" />
			</placeholder>
		</menu>
	</menubar>
	<toolbar name='toolbar'>
		<placeholder name='tools'>
			<toolitem action='show_task_list'/>
		</placeholder>
	</toolbar>
</ui>
'''

SQL_FORMAT_VERSION = (0, 4)
SQL_FORMAT_VERSION_STRING = "0.4"

SQL_CREATE_TABLES = '''
create table if not exists tasklist (
	id INTEGER PRIMARY KEY,
	source INTEGER,
	parent INTEGER,
	open BOOLEAN,
	actionable BOOLEAN,
	prio INTEGER,
	due TEXT,
	description TEXT
);
'''


tag_re = re.compile(r'(?<!\S)@(\w+)\b', re.U)
date_re = re.compile(r'\s*\[d:(.+)\]')


_NO_DATE = '9999' # Constant for empty due date - value chosen for sorting properties


# FUTURE: add an interface for this plugin in the WWW frontend

# TODO allow more complex queries for filter, in particular (NOT tag AND tag)


class TaskListPlugin(PluginClass):

	# define signals we want to use - (closure type, return type and arg types)
	__gsignals__ = {
		'tasklist-changed': (gobject.SIGNAL_RUN_LAST, None, ()),
	}

	plugin_info = {
		'name': _('Task List'), # T: plugin name
		'description': _('''\
This plugin adds a dialog showing all open tasks in
this notebook. Open tasks can be either open checkboxes
or items marked with tags like "TODO" or "FIXME".

This is a core plugin shipping with zim.
'''), # T: plugin description
		'author': 'Jaap Karssenberg',
		'help': 'Plugins:Task List'
	}

	plugin_preferences = (
		# key, type, label, default
		('all_checkboxes', 'bool', _('Consider all checkboxes as tasks'), True),
			# T: label for plugin preferences dialog
		('tag_by_page', 'bool', _('Turn page name into tags for task items'), False),
			# T: label for plugin preferences dialog
		('deadline_by_page', 'bool', _('Implicit deadline for task items in calendar pages'), False),
			# T: label for plugin preferences dialog
		('labels', 'string', _('Labels marking tasks'), 'FIXME, TODO', check_class_allow_empty),
			# T: label for plugin preferences dialog - labels are e.g. "FIXME", "TODO", "TASKS"
	)
	_rebuild_on_preferences = ['all_checkboxes', 'labels','deadline_by_page']
		# Rebuild database table if any of these preferences changed.
		# But leave it alone if others change.

	def __init__(self, ui):
		PluginClass.__init__(self, ui)
		self.task_labels = None
		self.task_label_re = None
		self.db_initialized = False

	def initialize_ui(self, ui):
		if ui.ui_type == 'gtk':
			ui.add_actions(ui_actions, self)
			ui.add_ui(ui_xml, self)

	def finalize_notebook(self, notebook):
		# This is done regardsless of the ui type of the application
		self.index = notebook.index
		self.index.connect_after('initialize-db', self.initialize_db)
		self.index.connect('page-indexed', self.index_page)
		self.index.connect('page-deleted', self.remove_page)
		# We don't care about pages that are moved

		db_version = self.index.properties['plugin_tasklist_format']
		if db_version == SQL_FORMAT_VERSION_STRING:
			self.db_initialized = True

		self._set_preferences()

	def initialize_db(self, index):
		with index.db_commit:
			index.db.executescript(SQL_CREATE_TABLES)
		self.index.properties['plugin_tasklist_format'] = SQL_FORMAT_VERSION_STRING
		self.db_initialized = True

	def do_preferences_changed(self):
		new_preferences = self._serialize_rebuild_on_preferences()
		if new_preferences != self._current_preferences:
			self._drop_table()
		self._set_preferences()

	def _set_preferences(self):
		self._current_preferences = self._serialize_rebuild_on_preferences()

		string = self.preferences['labels'].strip(' ,')
		if string:
			self.task_labels = [s.strip() for s in self.preferences['labels'].split(',')]
		else:
			self.task_labels = []
		regex = '^(' + '|'.join(map(re.escape, self.task_labels)) + ')\\b'
		self.task_label_re = re.compile(regex)

	def _serialize_rebuild_on_preferences(self):
		# string mapping settings that influence building the table
		string = ''
		for pref in self._rebuild_on_preferences:
			string += str(self.preferences[pref])
		return string

	def disconnect(self):
		self._drop_table()
		PluginClass.disconnect(self)

	def _drop_table(self):
		self.index.properties['plugin_tasklist_format'] = 0
		if self.db_initialized:
			try:
				self.index.db.execute('DROP TABLE "tasklist"')
			except:
				logger.exception('Could not drop table:')
			else:
				self.db_initialized = False
		else:
			try:
				self.index.db.execute('DROP TABLE "tasklist"')
			except:
				pass

	def index_page(self, index, path, page):
		if not self.db_initialized: return
		#~ print '>>>>>', path, page, page.hascontent
		tasksfound = self.remove_page(index, path, _emit=False)

		parsetree = page.get_parsetree()
		if not parsetree:
			return

		if page._ui_object:
			# FIXME - HACK - dump and parse as wiki first to work
			# around glitches in pageview parsetree dumper
			# make sure we get paragraphs and bullets are nested properly
			# Same hack in gui clipboard code
			dumper = get_format('wiki').Dumper()
			text = ''.join( dumper.dump(parsetree) ).encode('utf-8')
			parser = get_format('wiki').Parser()
			parsetree = parser.parse(text)

		#~ print '!! Checking for tasks in', path
		dates = daterange_from_path(path)
		if dates and self.preferences['deadline_by_page']:
			deadline = dates[2]
		else:
			deadline = None
		tasks = self.extract_tasks(parsetree, deadline)
		if tasks:
			tasksfound = True

			# Much more efficient to do insert here at once for all tasks
			# rather than do it one by one while parsing the page.
			with self.index.db_commit:
				self.index.db.executemany(
					'insert into tasklist(source, parent, open, actionable, prio, due, description)'
					'values (%i, 0, ?, ?, ?, ?, ?)' % path.id,
					tasks
				)

		if tasksfound:
			self.emit('tasklist-changed')

	def extract_tasks(self, parsetree, deadline=None):
		'''Extract all tasks from a parsetree.
		Returns tuples for each tasks with following properties:
		C{(open, actionable, prio, due, description)}
		'''
		tasks = []

		for node in parsetree.findall('p'):
			lines = self._flatten_para(node)
			# Check first line for task list header
			istasklist = False
			globaltags = []
			if len(lines) >= 2 \
			and isinstance(lines[0], basestring) \
			and isinstance(lines[1], tuple) \
			and self.task_labels and self.task_label_re.match(lines[0]):
				for word in lines[0].split()[1:]:
					if word.startswith('@'):
						globaltags.append(word)
					else:
						# not a header after all
						globaltags = []
						break
				else:
					# no break occurred - all OK
					lines.pop(0)
					istasklist = True

			# Check line by line
			for item in lines:
				if isinstance(item, tuple):
					# checkbox
					if istasklist or self.preferences['all_checkboxes'] \
					or (self.task_labels and self.task_label_re.match(item[2])):
						open = item[0] == UNCHECKED_BOX
						tasks.append(self._parse_task(item[2], level=item[1], open=open, tags=globaltags, deadline=deadline))
				else:
					# normal line
					if self.task_labels and self.task_label_re.match(item):
						tasks.append(self._parse_task(item, tags=globaltags, deadline=deadline))

		return tasks

	def _flatten_para(self, para):
		# Returns a list which is a mix of normal lines of text and
		# tuples for checkbox items. Checkbox item tuples consist of
		# the checkbox type, the indenting level and the text.
		items = []

		text = para.text or ''
		for child in para.getchildren():
			if child.tag == 'strike':
				continue # Ignore strike out text
			elif child.tag == 'ul':
				if text:
					items += text.splitlines()
				items += self._flatten_list(child)
				text = child.tail or ''
			else:
				text += self._flatten(child)
				text += child.tail or ''

		if text:
			items += text.splitlines()

		return items

	def _flatten_list(self, list, list_level=0):
		# Handle bullet lists
		items = []
		for node in list.getchildren():
			if node.tag == 'ul':
				items += self._flatten_list(node, list_level+1) # recurs
			elif node.tag == 'li':
				bullet = node.get('bullet')
				text = self._flatten(node)
				if bullet in (UNCHECKED_BOX, CHECKED_BOX, XCHECKED_BOX):
					items.append((bullet, list_level, text))
				else:
					items.append(text)
			else:
				pass # should not occur - ignore silently
		return items

	def _flatten(self, node):
		# Just flatten everything to text
		text = node.text or ''
		for child in node.getchildren():
			text += self._flatten(child) # recurs
			text += child.tail or ''
		return text

	def _parse_task(self, text, level=0, open=True, tags=None, deadline=None):
		# TODO - determine if actionable or not
		prio = text.count('!')

		global date # FIXME
		date = _NO_DATE

		def set_date(match):
			global date
			mydate = parse_date(match.group(0))
			if mydate and date == _NO_DATE:
				date = '%04i-%02i-%02i' % mydate # (y, m, d)
				#~ return match.group(0) # TEST
				return ''
			else:
				# No match or we already had a date
				return match.group(0)

		if tags:
			for tag in tags:
				if not tag in text:
					text += ' ' + tag

		text = date_re.sub(set_date, text)

		if deadline and date == _NO_DATE:
			date = deadline

		return (open, True, prio, date, text)
			# (open, actionable, prio, due, description)


	def remove_page(self, index, path, _emit=True):
		if not self.db_initialized: return

		tasksfound = False
		with index.db_commit:
			cursor = index.db.cursor()
			cursor.execute(
				'delete from tasklist where source=?', (path.id,) )
			tasksfound = cursor.rowcount > 0

		if tasksfound and _emit:
			self.emit('tasklist-changed')

		return tasksfound

	def list_tasks(self):
		if self.db_initialized:
			cursor = self.index.db.cursor()
			cursor.execute('select * from tasklist')
			for row in cursor:
				yield row

	def get_path(self, task):
		return self.index.lookup_id(task['source'])

	def show_task_list(self):
		if not self.db_initialized:
			MessageDialog(self.ui, (
				_('Need to index the notebook'),
				# T: Short message text on first time use of task list plugin
				_('This is the first time the task list is opened.\n'
				  'Therefore the index needs to be rebuild.\n'
				  'Depending on the size of the notebook this can\n'
				  'take up to several minutes. Next time you use the\n'
				  'task list this will not be needed again.' )
				# T: Long message text on first time use of task list plugin
			) ).run()
			logger.info('Tasklist not initialized, need to rebuild index')
			finished = self.ui.reload_index(flush=True)
			# Flush + Reload will also initialize task list
			if not finished:
				self.db_initialized = False
				return

		dialog = TaskListDialog.unique(self, plugin=self)
		dialog.present()

# Need to register classes defining gobject signals
gobject.type_register(TaskListPlugin)


class TaskListDialog(Dialog):

	def __init__(self, plugin):
		if ui_environment['platform'] == 'maemo':
			defaultsize = (800, 480)
		else:
			defaultsize = (550, 400)

		Dialog.__init__(self, plugin.ui, _('Task List'), # T: dialog title
			buttons=gtk.BUTTONS_CLOSE, help=':Plugins:Task List',
			defaultwindowsize=defaultsize )
		self.plugin = plugin
		if ui_environment['platform'] == 'maemo':
			self.resize(800,480)
			# Force maximum dialog size under maemo, otherwise
			# we'll end with a too small dialog and no way to resize it
		hbox = gtk.HBox(spacing=5)
		self.vbox.pack_start(hbox, False)
		self.hpane = gtk.HPaned()
		self.uistate.setdefault('hpane_pos', 75)
		self.hpane.set_position(self.uistate['hpane_pos'])
		self.vbox.add(self.hpane)

		# Task list
		self.task_list = TaskListTreeView(self.ui, plugin)
		self.task_list.set_headers_visible(True) # Fix for maemo
		scrollwindow = gtk.ScrolledWindow()
		scrollwindow.set_policy(gtk.POLICY_AUTOMATIC, gtk.POLICY_AUTOMATIC)
		scrollwindow.set_shadow_type(gtk.SHADOW_IN)
		scrollwindow.add(self.task_list)
		self.hpane.add2(scrollwindow)

		# Tag list
		self.tag_list = TagListTreeView(self.task_list)
		scrollwindow = gtk.ScrolledWindow()
		scrollwindow.set_policy(gtk.POLICY_NEVER, gtk.POLICY_AUTOMATIC)
		scrollwindow.set_shadow_type(gtk.SHADOW_IN)
		scrollwindow.add(self.tag_list)
		self.hpane.add1(scrollwindow)

		# Filter input
		hbox.pack_start(gtk.Label(_('Filter')+': '), False) # T: Input label
		filter_entry = InputEntry()
		filter_entry.set_icon_to_clear()
		hbox.pack_start(filter_entry, False)
		filter_cb = DelayedCallback(500,
			lambda o: self.task_list.set_filter(filter_entry.get_text()))
		filter_entry.connect('changed', filter_cb)

		# Dropdown with options - TODO
		#~ menu = gtk.Menu()
		#~ showtree = gtk.CheckMenuItem(_('Show _Tree')) # T: menu item in options menu
		#~ menu.append(showtree)
		#~ menu.append(gtk.SeparatorMenuItem())
		#~ showall = gtk.RadioMenuItem(None, _('Show _All Items')) # T: menu item in options menu
		#~ showopen = gtk.RadioMenuItem(showall, _('Show _Open Items')) # T: menu item in options menu
		#~ menu.append(showall)
		#~ menu.append(showopen)
		#~ menubutton = MenuButton(_('_Options'), menu) # T: Button label
		#~ hbox.pack_start(menubutton, False)

		# Statistics label
		self.statistics_label = gtk.Label()
		hbox.pack_end(self.statistics_label, False)

		def set_statistics(o):
			total, stats = self.task_list.get_statistics()
			text = ngettext('%i open item', '%i open items', total) % total
				# T: Label for statistics in Task List, %i is the number of tasks
			text += ' (' + '/'.join(map(str, stats)) + ')'
			self.statistics_label.set_text(text)

		set_statistics(self.task_list)
		self.plugin.connect('tasklist-changed', set_statistics)
			# Make sure this is connected after the task list connected to same signal

	def do_response(self, response):
		self.uistate['hpane_pos'] = self.hpane.get_position()
		Dialog.do_response(self, response)


class TagListTreeView(SingleClickTreeView):
	'''TreeView with a single column 'Tags' which shows all tags available
	in a TaskListTreeView. Selecting a tag will filter the task list to
	only show tasks with that tag.
	'''

	_type_separator = 0
	_type_label = 1
	_type_tag = 2

	def __init__(self, task_list):
		model = gtk.ListStore(str, int, int, int) # tag name, number of tasks, type, weight
		SingleClickTreeView.__init__(self, model)
		self.get_selection().set_mode(gtk.SELECTION_MULTIPLE)
		self.task_list = task_list

		column = gtk.TreeViewColumn(_('Tags'))
			# T: Column header for tag list in Task List dialog
		self.append_column(column)

		cr1 = gtk.CellRendererText()
		cr1.set_property('ellipsize', pango.ELLIPSIZE_END)
		column.pack_start(cr1, True)
		column.set_attributes(cr1, text=0, weight=3) # tag name, weight

		cr2 = self.get_cell_renderer_number_of_items()
		column.pack_start(cr2, False)
		column.set_attributes(cr2, text=1) # number of tasks

		self.set_row_separator_func(lambda m, i: m[i][2] == self._type_separator)

		self.get_selection().connect('changed', self.on_selection_changed)

		self.refresh(task_list)
		task_list.plugin.connect('tasklist-changed', lambda o: self.refresh(task_list))
			# Make sure this is connected after the task list connected to same signal

	def get_tags(self):
		'''Returns current selected tags, or None for all tags'''
		tags = []
		for row in self._get_selected():
			if row[2] == self._type_tag:
				tags.append(row[0])
		return tags or None

	def get_labels(self):
		'''Returns current selected labels'''
		labels = []
		for row in self._get_selected():
			if row[2] == self._type_label:
				labels.append(row[0])
		return labels or None

	def _get_selected(self):
		model, paths = self.get_selection().get_selected_rows()
		if not paths or (0,) in paths:
			return []
		else:
			return [model[path] for path in paths]

	def refresh(self, task_list):
		# FIXME make sure selection is not reset when refreshing
		model = self.get_model()
		model.clear()

		n_all = self.task_list.get_n_tasks()
		model.append((_('All Tasks'), n_all, self._type_label, pango.WEIGHT_BOLD)) # T: "tag" for showing all tasks

		labels = self.task_list.get_labels()
		for label in self.task_list.plugin.task_labels: # explicitly keep sorting from preferences
			if label in labels:
				model.append((label, labels[label], self._type_label, pango.WEIGHT_BOLD))

		model.append(('', 0, self._type_separator, 0)) # separator

		tags = self.task_list.get_tags()
		for tag in sorted(tags):
			model.append((tag, tags[tag], self._type_tag, pango.WEIGHT_NORMAL))

	def on_selection_changed(self, selection):
		tags = self.get_tags()
		labels = self.get_labels()
		self.task_list.set_tag_filter(tags)
		self.task_list.set_label_filter(labels)


HIGH_COLOR = '#EF5151' # red (derived from Tango style guide - #EF2929)
MEDIUM_COLOR = '#FCB956' # orange ("idem" - #FCAF3E)
ALERT_COLOR = '#FCEB65' # yellow ("idem" - #FCE94F)
# FIXME: should these be configurable ?


class TaskListTreeView(BrowserTreeView):

	VIS_COL = 0 # visible
	PRIO_COL = 1
	TASK_COL = 2
	DATE_COL = 3
	PAGE_COL = 4
	ACT_COL = 5 # actionable - no children
	OPEN_COL = 6 # item not closed

	def __init__(self, ui, plugin):
		self.filter = None
		self.tag_filter = None
		self.label_filter = None
		self.real_model = gtk.TreeStore(bool, int, str, str, str, bool, bool)
			# VIS_COL, PRIO_COL, TASK_COL, DATE_COL, PAGE_COL, ACT_COL, OPEN_COL
		model = self.real_model.filter_new()
		model.set_visible_column(self.VIS_COL)
		model = gtk.TreeModelSort(model)
		model.set_sort_column_id(self.PRIO_COL, gtk.SORT_DESCENDING)
		BrowserTreeView.__init__(self, model)
		self.ui = ui
		self.plugin = plugin

		# Add some rendering for the Prio column
		def render_prio(col, cell, model, i):
			prio = model.get_value(i, self.PRIO_COL)
			cell.set_property('text', str(prio))
			if prio >= 3: color = HIGH_COLOR
			elif prio == 2: color = MEDIUM_COLOR
			elif prio == 1: color = ALERT_COLOR
			else: color = None
			cell.set_property('cell-background', color)

		cell_renderer = gtk.CellRendererText()
		#~ column = gtk.TreeViewColumn(_('Prio'), cell_renderer)
			# T: Column header Task List dialog
		column = gtk.TreeViewColumn(' ! ', cell_renderer)
		column.set_cell_data_func(cell_renderer, render_prio)
		column.set_sort_column_id(self.PRIO_COL)
		self.append_column(column)

		# Rendering for task description column
		cell_renderer = gtk.CellRendererText()
		cell_renderer.set_property('ellipsize', pango.ELLIPSIZE_END)
		column = gtk.TreeViewColumn(_('Task'), cell_renderer, text=self.TASK_COL)
				# T: Column header Task List dialog
		column.set_resizable(True)
		column.set_sort_column_id(self.TASK_COL)
		column.set_expand(True)
		if ui_environment['platform'] == 'maemo':
			column.set_min_width(250) # don't let this column get too small
		else:
			column.set_min_width(300) # don't let this column get too small
		self.append_column(column)

		if gtk.gtk_version >= (2, 12, 0):
			self.set_tooltip_column(self.TASK_COL)

		# Rendering of the Date column
		today    = str( datetime.date.today() )
		tomorrow = str( datetime.date.today() + datetime.timedelta(days=1))
		dayafter = str( datetime.date.today() + datetime.timedelta(days=2))
		def render_date(col, cell, model, i):
			date = model.get_value(i, self.DATE_COL)
			if date == _NO_DATE:
				cell.set_property('text', '')
			else:
				cell.set_property('text', date)
				# TODO allow strftime here

			if date <= today: color = HIGH_COLOR
			elif date == tomorrow: color = MEDIUM_COLOR
			elif date == dayafter: color = ALERT_COLOR
			else: color = None
			cell.set_property('cell-background', color)

		cell_renderer = gtk.CellRendererText()
		column = gtk.TreeViewColumn(_('Date'), cell_renderer)
			# T: Column header Task List dialog
		column.set_cell_data_func(cell_renderer, render_date)
		column.set_sort_column_id(self.DATE_COL)
		self.append_column(column)

		# Rendering for page name column
		cell_renderer = gtk.CellRendererText()
		column = gtk.TreeViewColumn(_('Page'), cell_renderer, text=self.PAGE_COL)
				# T: Column header Task List dialog
		column.set_sort_column_id(self.PAGE_COL)
		self.append_column(column)

		# Finalize
		self.refresh()
		self.plugin.connect_object('tasklist-changed', self.__class__.refresh, self)

		# HACK because we can not register ourselves :S
		self.connect('row_activated', self.__class__.do_row_activated)

	def refresh(self):
		self.real_model.clear() # flush

		# First cache + sort tasks to ensure stability of the list
		rows = list(self.plugin.list_tasks())
		paths = {}
		for row in rows:
			if not row['source'] in paths:
				paths[row['source']] = self.plugin.get_path(row)

		rows.sort(key=lambda r: paths[r['source']].name)

		for row in rows:
			path = paths[row['source']]
			modelrow = [False, row['prio'], row['description'], row['due'], path.name, row['actionable'], row['open']]
						# VIS_COL, PRIO_COL, TASK_COL, DATE_COL, PAGE_COL, ACT_COL, OPEN_COL
			modelrow[0] = self._filter_item(modelrow)
			self.real_model.append(None, modelrow)

	def set_filter(self, string):
		# TODO allow more complex queries here - same parse as for search
		if string:
			inverse = False
			if string.lower().startswith('not '):
				# Quick HACK to support e.g. "not @waiting"
				inverse = True
				string = string[4:]
			self.filter = (inverse, string.strip().lower())
		else:
			self.filter = None
		self._eval_filter()

	def get_labels(self):
		'''Get all labels that are in use
		@returns: a dict with labels as keys and the number of tasks
		per label as value
		'''
		labels = {}
		def collect(model, path, iter):
			row = model[iter]
			if not row[self.OPEN_COL]:
				return # only count open items

			desc = row[self.TASK_COL].decode('utf-8')
			match = self.plugin.task_label_re.match(desc)
			if match:
				label = match.group(0)
				if not label in labels:
					labels[label] = 1
				else:
					labels[label] += 1

		self.real_model.foreach(collect)

		return labels

	def get_tags(self):
		'''Get all tags that are in use
		@returns: a dict with tags as keys and the number of tasks
		per tag as value
		'''
		tags = {}

		def collect(model, path, iter):
			row = model[iter]
			if not row[self.OPEN_COL]:
				return # only count open items

			desc = row[self.TASK_COL].decode('utf-8')
			for match in tag_re.findall(desc):
				if not match in tags:
					tags[match] = 1
				else:
					tags[match] += 1

			if self.plugin.preferences['tag_by_page']:
				name = row[self.PAGE_COL].decode('utf-8')
				for part in name.split(':'):
					if not part in tags:
						tags[part] = 1
					else:
						tags[part] += 1

		self.real_model.foreach(collect)

		return tags

	def get_n_tasks(self):
		'''Get the number of tasks in the list
		@returns: total number as a list
		'''
		return self.real_model.iter_n_children(None)

	def get_statistics(self):
		statsbyprio = {}

		def count(model, path, iter):
			# only count open items
			row = model[iter]
			if row[self.OPEN_COL]:
				prio = row[self.PRIO_COL]
				statsbyprio.setdefault(prio, 0)
				statsbyprio[prio] += 1

		self.real_model.foreach(count)

		if statsbyprio:
			total = reduce(int.__add__, statsbyprio.values())
			highest = max([0] + statsbyprio.keys())
			stats = [statsbyprio.get(k, 0) for k in range(highest+1)]
			stats.reverse() # highest first
			return total, stats
		else:
			return 0, []

	def set_tag_filter(self, tags):
		if tags:
			self.tag_filter = [tag.lower() for tag in tags]
		else:
			self.tag_filter = None
		self._eval_filter()

	def set_label_filter(self, labels):
		if labels:
			self.label_filter = labels
		else:
			self.label_filter = None
		self._eval_filter()

	def _eval_filter(self):
		logger.debug('Filtering with labels: %s tags: %s, filter: %s', self.label_filter, self.tag_filter, self.filter)

		def filter(model, path, iter):
			visible = self._filter_item(model[iter])
			model[iter][self.VIS_COL] = visible

		self.real_model.foreach(filter)

	def _filter_item(self, modelrow):
		# This method filters case insensitive because both filters and
		# text are first converted to lower case text.
		visible = True

		if not (modelrow[self.ACT_COL] and modelrow[self.OPEN_COL]):
			visible = False

		if visible and self.label_filter:
			# Any labels need to be present
			description = modelrow[self.TASK_COL]
			for label in self.label_filter:
				if label in description:
					break
			else:
				visible = False # no label found

		description = modelrow[self.TASK_COL].lower()
		pagename = modelrow[self.PAGE_COL].lower()

		if visible and self.tag_filter:
			# And any tag should match (or pagename if tag_by_page)
			for tag in self.tag_filter:
				if self.plugin.preferences['tag_by_page']:
					if '@'+tag in description \
					or tag in pagename.split(':'):
						break # keep visible True
				else:
					if '@'+tag in description:
						break # keep visible True
			else:
				visible = False # no tag found

		if visible and self.filter:
			# And finally the filter string should match
			inverse, string = self.filter
			match = string in description or string in pagename
			if (not inverse and not match) or (inverse and match):
				visible = False

		return visible

	def do_row_activated(self, path, column):
		model = self.get_model()
		page = Path( model[path][self.PAGE_COL] )
		task = unicode(model[path][self.TASK_COL])
		self.ui.open_page(page)
		self.ui.mainwindow.pageview.find(task)

	def do_initialize_popup(self, menu):
		item = gtk.MenuItem(_("_Copy")) # T: menu item in context menu
		item.connect_object('activate', self.__class__.copy_to_clipboard, self)
		menu.append(item)

	def copy_to_clipboard(self):
		'''Exports currently visible elements from the tasks list'''
		logger.debug('Exporting to clipboard current view of task list.')
		text = self.get_visible_data_as_csv()
		gtk.Clipboard().set_text(text.decode("UTF-8"))

	def get_visible_data_as_csv(self):
		text = ""
		for prio, desc, date, page in self.get_visible_data():
			prio = str(prio)
			desc = '"' + desc.replace('"', '""') + '"'
			text += ",".join((prio, desc, date, page)) + "\n"
		return text

	def get_visible_data_as_html(self):
		html = '''\
<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01 Transitional//EN" "http://www.w3.org/TR/html4/loose.dtd">
<html>
	<head>
		<meta http-equiv="Content-Type" content="text/html; charset=UTF-8">
		<title>Task List - Zim</title>
		<meta name='Generator' content='Zim [%% zim.version %%]'>
		<style type='text/css'>
			table.tasklist {
				border-width: 1px;
				border-spacing: 2px;
				border-style: solid;
				border-color: gray;
				border-collapse: collapse;
			}
			table.tasklist th {
				border-width: 1px;
				padding: 1px;
				border-style: solid;
				border-color: gray;
			}
			table.tasklist td {
				border-width: 1px;
				padding: 1px;
				border-style: solid;
				border-color: gray;
			}
			.high {background-color: %s}
			.medium {background-color: %s}
			.alert {background-color: %s}
		</style>
	</head>
	<body>

<h1>Task List - Zim</h1>

<table class="tasklist">
<tr><th>Prio</th><th>Task</th><th>Date</th><th>Page</th></tr>
''' % (HIGH_COLOR, MEDIUM_COLOR, ALERT_COLOR)

		today    = str( datetime.date.today() )
		tomorrow = str( datetime.date.today() + datetime.timedelta(days=1))
		dayafter = str( datetime.date.today() + datetime.timedelta(days=2))
		for prio, desc, date, page in self.get_visible_data():
			if prio >= 3: prio = '<td class="high">%s</td>' % prio
			elif prio == 2: prio = '<td class="medium">%s</td>' % prio
			elif prio == 1: prio = '<td class="alert">%s</td>' % prio
			else: prio = '<td>%s</td>' % prio

			if date and date <= today: date = '<td class="high">%s</td>' % date
			elif date == tomorrow: date = '<td class="medium">%s</td>' % date
			elif date == dayafter: date = '<td class="alert">%s</td>' % date
			else: date = '<td>%s</td>' % date

			desc = '<td>%s</td>' % desc
			page = '<td>%s</td>' % page

			html += '<tr>' + prio + desc + date + page + '</tr>'

		html += '''\
</table>

	</body>

</html>
'''
		return html

	def get_visible_data(self):
		rows = []
		model = self.get_model()
		for row in model:
			prio = row[self.PRIO_COL]
			desc = row[self.TASK_COL]
			date = row[self.DATE_COL]
			page = row[self.PAGE_COL]

			if date == _NO_DATE:
				date = ''

			rows.append((prio, desc, date, page))
		return rows

# Need to register classes defining gobject signals
#~ gobject.type_register(TaskListTreeView)
# NOTE: enabling this line causes this treeview to have wrong theming under default ubuntu them !???
