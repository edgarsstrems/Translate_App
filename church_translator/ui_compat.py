"""Use the same Qt rendering and dark fallback palette on Windows 10 and 11."""
from PySide6.QtGui import QColor, QPalette, QTextCursor
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QPlainTextEdit


class FollowingLog(QPlainTextEdit):
    """Follow incoming logs until the reader scrolls up, including while hidden."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self._following = True
        self._appending = False
        self.verticalScrollBar().valueChanged.connect(self._reader_scrolled)

    def _reader_scrolled(self, value):
        if not self._appending:
            self._following = value >= self.verticalScrollBar().maximum() - 4

    def appendHtml(self, text):
        bar = self.verticalScrollBar()
        position = bar.value()
        self._appending = True
        try:
            super().appendHtml(text)
            bar.setValue(bar.maximum() if self._following else position)
        finally:
            self._appending = False

    def showEvent(self, event):
        following = self._following
        position = getattr(self, '_hidden_position', self.verticalScrollBar().value())
        super().showEvent(event)
        self._following = following
        def restore():
            self._appending = True
            self.verticalScrollBar().setValue(self.verticalScrollBar().maximum() if self._following else position)
            self._appending = False
        QTimer.singleShot(0, restore)

    def hideEvent(self, event):
        self._hidden_position = self.verticalScrollBar().value()
        super().hideEvent(event)

    def _follow_latest(self):
        if self._following:
            self._appending = True
            self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())
            self._appending = False

    def clear(self):
        super().clear()
        self._following = True

CONTROL_QSS = """
QAbstractSpinBox, QLineEdit, QComboBox, QMenu, QComboBox QAbstractItemView {
    background-color: #070C12; color: #F8FAFC;
    selection-background-color: #0D9488; selection-color: #FFFFFF;
}
QAbstractSpinBox { border: 1px solid #334155; border-radius: 6px; padding: 5px; }
QAbstractSpinBox:focus, QLineEdit:focus, QComboBox:focus { border-color: #14B8A6; }
QAbstractSpinBox:disabled, QLineEdit:disabled, QComboBox:disabled { color: #94A3B8; background: #111827; }
QMenu::item:selected, QComboBox QAbstractItemView::item:selected { background: #0D9488; color: white; }
QSlider::add-page:horizontal { background: #1E293B; border-radius: 3px; }
QSlider::handle:horizontal:hover, QSlider::handle:horizontal:pressed { background: #99F6E4; }
QSlider::handle:horizontal:disabled { background: #94A3B8; border-color: #475569; }
QSlider:focus { border: 1px solid #14B8A6; border-radius: 4px; }
"""


def apply_dark_style(app):
    if app.property('churchDarkStyle'):
        return
    app.setStyle('Fusion')
    palette = QPalette()
    for role, color in ((QPalette.Window, '#0B0F17'), (QPalette.WindowText, '#F8FAFC'),
                        (QPalette.Base, '#070C12'), (QPalette.AlternateBase, '#111827'),
                        (QPalette.Text, '#F8FAFC'), (QPalette.Button, '#1E293B'),
                        (QPalette.ButtonText, '#F8FAFC'), (QPalette.Highlight, '#0D9488'),
                        (QPalette.HighlightedText, '#FFFFFF'), (QPalette.PlaceholderText, '#94A3B8'),
                        (QPalette.ToolTipBase, '#111827'), (QPalette.ToolTipText, '#F8FAFC')):
        palette.setColor(role, QColor(color))
    for role in (QPalette.Text, QPalette.ButtonText, QPalette.WindowText):
        palette.setColor(QPalette.Disabled, role, QColor('#94A3B8'))
    app.setPalette(palette)
    app.setStyleSheet(app.styleSheet() + CONTROL_QSS)
    app.setProperty('churchDarkStyle', True)


def append_live_text(panel, text):
    """Insert at the document end without moving the reader's cursor/focus."""
    if not text:
        return
    bar = panel.verticalScrollBar()
    position = bar.value()
    follow = position >= bar.maximum() - 4
    horizontal = panel.horizontalScrollBar().value()
    reader = panel.textCursor()
    anchor, cursor_position = reader.anchor(), reader.position()
    writer = QTextCursor(panel.document())
    writer.movePosition(QTextCursor.End)
    writer.beginEditBlock()
    if not panel.document().isEmpty():
        writer.insertBlock()
    writer.insertText(text)
    writer.endEditBlock()
    # A selection ending at EOF must not expand to include the incoming text.
    if (panel.textCursor().anchor(), panel.textCursor().position()) != (anchor, cursor_position):
        reader.setPosition(anchor)
        reader.setPosition(cursor_position, QTextCursor.KeepAnchor)
        panel.setTextCursor(reader)
    bar.setValue(bar.maximum() if follow else position)
    panel.horizontalScrollBar().setValue(horizontal)
