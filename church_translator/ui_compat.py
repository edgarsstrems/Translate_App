"""Use the same Qt rendering and dark fallback palette on Windows 10 and 11."""
from PySide6.QtGui import QColor, QPalette, QTextCursor

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
