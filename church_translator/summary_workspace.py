"""Compact, readable summary reports, scoped to the summary dialog."""
import math
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QTextBlockFormat, QTextCursor
from PySide6.QtWidgets import (QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QComboBox, QProgressBar, QStackedWidget, QScrollArea, QWidget, QFrame,
    QLineEdit, QPlainTextEdit, QTextEdit, QToolButton, QMenu, QSizePolicy)

LABELS = {
    'en': {'heading': 'AI Sermon Summary', 'subtitle': 'A brief account of what was said, based on the transcript.',
        'overview': 'EXECUTIVE OVERVIEW', 'points': 'Key Takeaways & Core Points',
        'highlights': 'Main Speech Highlights & Key Insights', 'generate': 'Generate Summary',
        'regenerate': 'Regenerate', 'edit': 'Edit', 'done': 'Done Editing', 'copy': 'Copy',
        'export': 'Export', 'save': 'Save', 'saved': 'Saved', 'copied': 'Copied', 'close': 'Close && Return to Dashboard',
        'add_point': '+ Add point', 'add_highlight': '+ Add highlight', 'cancel': 'Cancel',
        'raw': 'Edit full summary text…', 'transcript': 'Edit transcript…', 'back': 'Back to Summary',
        'empty_title': 'Your sermon, in a few clear points',
        'empty': 'Generate a short overview, the main points and memorable highlights from the saved transcript.',
        'words': 'summary words'},
    'lv': {'heading': 'AI sprediķa kopsavilkums', 'subtitle': 'Īss pārskats par teikto, balstīts transkriptā.',
        'overview': 'ĪSS PĀRSKATS', 'points': 'Galvenās atziņas un pamatdomas',
        'highlights': 'Spilgtākie momenti un būtiskās atziņas', 'generate': 'Veidot kopsavilkumu',
        'regenerate': 'Veidot vēlreiz', 'edit': 'Rediģēt', 'done': 'Pabeigt rediģēšanu', 'copy': 'Kopēt',
        'export': 'Eksportēt', 'save': 'Saglabāt', 'saved': 'Saglabāts', 'copied': 'Nokopēts', 'close': 'Aizvērt un atgriezties panelī',
        'add_point': '+ Pievienot punktu', 'add_highlight': '+ Pievienot atziņu', 'cancel': 'Atcelt',
        'raw': 'Rediģēt pilnu kopsavilkumu…', 'transcript': 'Rediģēt transkriptu…', 'back': 'Atpakaļ uz kopsavilkumu',
        'empty_title': 'Sprediķis dažos skaidros punktos',
        'empty': 'Izveidojiet īsu pārskatu, pamatdomas un spilgtākos momentus no saglabātā transkripta.',
        'words': 'vārdi kopsavilkumā'},
}


class ReportTextEdit(QTextEdit):
    """Wrapping plain text that grows with its content inside one outer scroll."""
    def __init__(self, text='', parent=None):
        super().__init__(parent)
        self.setAcceptRichText(False)
        self.setReadOnly(True)
        self.setFrameShape(QFrame.NoFrame)
        # QTextEdit paints through an internal viewport.  Turn off its native
        # base fill so the reading cards show through instead of a black block.
        self.setAutoFillBackground(False)
        self.viewport().setAutoFillBackground(False)
        self.viewport().setStyleSheet('background: transparent;')
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.document().setDocumentMargin(1)
        self.document().documentLayout().documentSizeChanged.connect(self._fit_height)
        self.setPlainText(text)

    def setPlainText(self, text):
        super().setPlainText(text)
        cursor = QTextCursor(self.document())
        cursor.select(QTextCursor.Document)
        spacing = QTextBlockFormat()
        spacing.setLineHeight(135, QTextBlockFormat.ProportionalHeight.value)
        cursor.mergeBlockFormat(spacing)
        self._fit_height()

    def text(self):
        return self.toPlainText()

    def setText(self, text):
        self.setPlainText(text)

    def _fit_height(self, *_):
        height = max(26, math.ceil(self.document().size().height()) + 4)
        if height != self.height():
            self.setFixedHeight(height)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_height()


def labels(language):
    return LABELS.get(language, LABELS['en'])


def build_summary_workspace(d):
    d.setObjectName('aiSummaryReport')
    d.setFont(QFont('Segoe UI', 10))
    d.setStyleSheet('''
QDialog#aiSummaryReport { background: #0B101A; color: #E2E8F0; }
QWidget { font-family: 'Segoe UI'; }
QLabel { background: transparent; border: none; color: #9AAEC5; }
QLabel#reportHeading { color: #F1F5F9; font-size: 20px; font-weight: 600; }
QLabel#reportSubtitle { color: #91A4BA; font-size: 12px; }
QFrame#overviewPanel { background: #10252E; border: 1px solid #20535C; border-radius: 12px; }
QFrame#pointRow { background: #111D2C; border: 1px solid #23344A; border-radius: 10px; }
QFrame#reportDivider { background: #233247; border: none; max-height: 1px; }
QPushButton, QToolButton, QComboBox { background: #182638; color: #DCE7F3; border: 1px solid #2B3D53; border-radius: 8px; padding: 7px 12px; height: 20px; min-height: 20px; font-size: 12px; }
QPushButton:hover, QToolButton:hover, QComboBox:hover { background: #203348; border-color: #477588; }
QPushButton:pressed, QToolButton:pressed { background: #0C4B51; }
QPushButton:focus, QToolButton:focus, QComboBox:focus { border-color: #5EEAD4; }
QPushButton:disabled, QToolButton:disabled { color: #62758A; border-color: #223044; }
QPushButton#primaryAction { background: #0D9488; border-color: #14B8A6; color: white; font-weight: 600; }
QPushButton#primaryAction:hover { background: #0FACA0; }
QToolButton:checked { background: #123C43; border-color: #2DD4BF; color: #99F6E4; }
QToolButton#editButton { min-width: 96px; min-height: 28px; padding: 7px 14px; }
QToolButton#editButton::menu-button { border-left: 1px solid #2B3D53; width: 22px; }
QMenu { background: #132032; color: #E2E8F0; border: 1px solid #324A60; padding: 5px; }
QMenu::item { padding: 8px 14px; }
QMenu::item:selected { background: #155E63; }
QTextEdit, QAbstractScrollArea::viewport { background: transparent; color: #DFE8F3; border: none; padding: 0; font-size: 13px; selection-background-color: #155E63; }
QTextEdit[editing="true"], QTextEdit[editing="true"]::viewport { background: #0D1928; border: 1px solid #365166; border-radius: 5px; }
QLineEdit#reportTitle { background: transparent; color: #F1F5F9; border: none; font-size: 17px; font-weight: 600; padding: 0; }
QLineEdit#reportTitle[editing="true"] { background: #0D1928; border: 1px solid #365166; border-radius: 5px; padding: 4px; }
QPlainTextEdit { background: #0E1927; color: #E2E8F0; border: 1px solid #2B3D53; border-radius: 8px; padding: 12px; }
QScrollArea, QStackedWidget, QWidget#reportContent { background: transparent; border: none; }
QScrollBar:vertical { background: transparent; width: 8px; margin: 0; }
QScrollBar::handle:vertical { background: #334C60; border-radius: 4px; min-height: 28px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QProgressBar { background: #182A3C; border: none; border-radius: 3px; min-height: 4px; max-height: 4px; }
QProgressBar::chunk { background: #2DD4BF; border-radius: 3px; }
''')
    root = QVBoxLayout(d)
    root.setContentsMargins(22, 20, 22, 18)
    root.setSpacing(10)
    header = QHBoxLayout()
    header.setSpacing(12)
    icon = QLabel('✦')
    icon.setAlignment(Qt.AlignCenter)
    icon.setFixedSize(40, 40)
    icon.setStyleSheet('color: #5EEAD4; background: #10323C; border: 1px solid #23515B; border-radius: 12px; font-size: 26px;')
    header.addWidget(icon)
    titles = QVBoxLayout()
    titles.setSpacing(3)
    d.report_heading = QLabel()
    d.report_heading.setObjectName('reportHeading')
    d.report_heading.setWordWrap(True)
    d.report_subtitle = QLabel()
    d.report_subtitle.setObjectName('reportSubtitle')
    d.report_subtitle.setWordWrap(True)
    titles.addWidget(d.report_heading)
    titles.addWidget(d.report_subtitle)
    header.addLayout(titles, 1)
    d.lang_combo = QComboBox()
    d.lang_combo.setAccessibleName('Summary language')
    d.lang_combo.addItem('English', 'en')
    d.lang_combo.addItem('Latviešu', 'lv')
    d.lang_combo.setCurrentIndex(d.lang_combo.findData(d._loaded_lang))
    d.lang_combo.setMinimumWidth(112)
    d.lang_combo.currentIndexChanged.connect(d._on_lang_changed)
    header.addWidget(d.lang_combo, alignment=Qt.AlignTop)
    root.addLayout(header)

    def divider():
        line = QFrame()
        line.setObjectName('reportDivider')
        line.setFixedHeight(1)
        root.addWidget(line)
    divider()
    actions = QHBoxLayout()
    actions.setSpacing(8)
    d.generate_btn = QPushButton()
    d.generate_btn.setObjectName('primaryAction')
    d.generate_btn.clicked.connect(lambda: d._generate_summary('gemini'))
    actions.addWidget(d.generate_btn)
    d.edit_btn = QToolButton()
    d.edit_btn.setObjectName('editButton')
    d.edit_btn.setMinimumWidth(96)
    d.edit_btn.setMinimumHeight(36)
    d.edit_btn.setCheckable(True)
    d.edit_btn.setPopupMode(QToolButton.MenuButtonPopup)
    d.edit_btn.clicked.connect(d._toggle_edit_mode)
    menu = QMenu(d.edit_btn)
    d.raw_edit_action = menu.addAction('')
    d.raw_edit_action.triggered.connect(d._switch_to_raw_view)
    d.transcript_btn = menu.addAction('')
    d.transcript_btn.triggered.connect(d._edit_transcript)
    d.edit_btn.setMenu(menu)
    actions.addWidget(d.edit_btn)
    d.cancel_btn = QPushButton()
    d.cancel_btn.clicked.connect(d._cancel_generation)
    d.cancel_btn.hide()
    actions.addWidget(d.cancel_btn)
    actions.addStretch()
    d.meta_words_pill = QLabel()
    d.meta_words_pill.setStyleSheet('font-size: 11px; color: #6F8FA7;')
    actions.addWidget(d.meta_words_pill)
    root.addLayout(actions)
    d.status_label = QLabel()
    d.status_label.setWordWrap(True)
    d.status_label.setTextFormat(Qt.PlainText)
    d.status_label.setStyleSheet('font-size: 11px; color: #8AA6BB;')
    root.addWidget(d.status_label)
    d.progress_bar = QProgressBar()
    d.progress_bar.setRange(0, 100)
    d.progress_bar.setTextVisible(False)
    d.progress_bar.hide()
    root.addWidget(d.progress_bar)

    d.view_stack = QStackedWidget()
    d.report_scroll = QScrollArea()
    d.report_scroll.setFrameShape(QFrame.NoFrame)
    d.report_scroll.setWidgetResizable(True)
    d.report_scroll.viewport().setObjectName('reportViewport')
    d.report_scroll.viewport().setStyleSheet('QWidget#reportViewport { background: #0B101A; }')
    content = QWidget()
    content.setObjectName('reportContent')
    content.setStyleSheet('QWidget#reportContent { background: #0B101A; }')
    d.dashboard_layout = QVBoxLayout(content)
    d.dashboard_layout.setContentsMargins(0, 0, 6, 4)
    d.dashboard_layout.setSpacing(14)
    d.empty_panel = QWidget()
    empty = QVBoxLayout(d.empty_panel)
    empty.setContentsMargins(12, 45, 12, 45)
    d.empty_title = QLabel()
    d.empty_title.setAlignment(Qt.AlignCenter)
    d.empty_title.setStyleSheet('font-size: 18px; font-weight: 600; color: #DBE8F3;')
    d.empty_title.setWordWrap(True)
    d.empty_label = QLabel()
    d.empty_label.setAlignment(Qt.AlignCenter)
    d.empty_label.setWordWrap(True)
    empty.addWidget(d.empty_title)
    empty.addWidget(d.empty_label)
    d.dashboard_layout.addWidget(d.empty_panel)
    d.card_title_edit = QLineEdit()
    d.card_title_edit.setObjectName('reportTitle')
    d.card_title_edit.setStyleSheet('QLineEdit { background: transparent; color: #F1F5F9; border: none; font-size: 17px; font-weight: 600; padding: 0; } QLineEdit[editing="true"] { background: #0D1928; border: 1px solid #365166; border-radius: 5px; padding: 4px; }')
    d.card_title_edit.setReadOnly(True)
    d.card_title_edit.setAccessibleName('Sermon title')
    d.card_title_edit.textChanged.connect(d._on_dashboard_field_changed)
    d.dashboard_layout.addWidget(d.card_title_edit)
    d.section_cards = {}
    d.section_headings = {}
    overview = QFrame()
    overview.setObjectName('overviewPanel')
    overview.setStyleSheet('QFrame#overviewPanel { background: #10252E; border: 1px solid #20535C; border-radius: 12px; }')
    overview.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
    over_layout = QVBoxLayout(overview)
    over_layout.setContentsMargins(18, 14, 18, 14)
    over_layout.setSpacing(6)
    d.overview_heading = QLabel()
    d.overview_heading.setStyleSheet('font-size: 11px; font-weight: 600; color: #67E8DB; letter-spacing: 1px;')
    over_layout.addWidget(d.overview_heading)
    d.card_main_msg = ReportTextEdit()
    d.card_main_msg.setAccessibleName('Executive overview')
    d.card_main_msg.textChanged.connect(d._on_dashboard_field_changed)
    over_layout.addWidget(d.card_main_msg)
    d.dashboard_layout.addWidget(overview)
    d.section_cards['main_message'] = overview

    def point_section(name, color):
        section = QWidget()
        section.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        layout = QVBoxLayout(section)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(9)
        row = QHBoxLayout()
        dot = QLabel('●')
        dot.setStyleSheet(f'color: {color}; font-size: 12px;')
        dot.setFixedWidth(10)
        heading = QLabel()
        heading.setStyleSheet('color: #E8F0F8; font-size: 13px; font-weight: 600;')
        heading.setWordWrap(True)
        row.addWidget(dot)
        row.addWidget(heading, 1)
        layout.addLayout(row)
        rows = QVBoxLayout()
        rows.setSpacing(8)
        rows.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(rows)
        add = QPushButton()
        add.setStyleSheet('border: none; color: #72DACE; background: transparent; padding: 4px;')
        add.clicked.connect(lambda: d._add_report_point(name))
        layout.addWidget(add, alignment=Qt.AlignLeft)
        d._point_tools.append(add)
        d.section_cards[name] = section
        d.section_headings[name] = heading
        d.dashboard_layout.addWidget(section)
        return rows, add
    d.teachings_layout, d.add_point_btn = point_section('key_teachings', '#2DD4BF')
    d.highlights_layout, d.add_highlight_btn = point_section('highlights', '#FBBF24')
    d.dashboard_layout.addStretch(1)
    d.report_scroll.setWidget(content)
    d.view_stack.addWidget(d.report_scroll)
    d.summary_text = QPlainTextEdit()
    d.summary_text.setAccessibleName('Full summary text')
    d.summary_text.textChanged.connect(d._on_raw_text_changed)
    d.view_stack.addWidget(d.summary_text)
    root.addWidget(d.view_stack, 1)
    d.transcript_meta = QLabel()
    d.transcript_meta.setStyleSheet('font-size: 11px; color: #7189A1;')
    d.transcript_meta.setWordWrap(True)
    metadata = QHBoxLayout()
    metadata.addWidget(d.transcript_meta, 1)
    d.provider_info_label = QLabel()
    d.provider_info_label.setTextFormat(Qt.PlainText)
    d.provider_info_label.setWordWrap(True)
    d.provider_info_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
    d.provider_info_label.setStyleSheet('font-size: 10px; color: #7189A1;')
    metadata.addWidget(d.provider_info_label, 1)
    root.addLayout(metadata)
    divider()
    footer = QHBoxLayout()
    footer.setSpacing(8)
    d.copy_btn = QPushButton()
    d.copy_btn.clicked.connect(d._copy_summary)
    d.download_btn = QPushButton()
    d.download_btn.clicked.connect(d._download_summary)
    d.save_btn = QPushButton()
    d.save_btn.clicked.connect(d._save_summary)
    for button in (d.copy_btn, d.download_btn, d.save_btn):
        footer.addWidget(button)
    footer.addStretch()
    d.close_btn = QPushButton()
    d.close_btn.clicked.connect(d.close)
    footer.addWidget(d.close_btn)
    root.addLayout(footer)
    d._apply_report_language()
