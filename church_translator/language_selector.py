"""Native searchable language list with only valid catalog selections."""
from PySide6.QtCore import Qt, QPointF
from PySide6.QtGui import QPainter, QPen, QColor
from PySide6.QtWidgets import QComboBox, QCompleter
from .languages import OUTPUT_LANGUAGES


class LanguageSelector(QComboBox):
    def __init__(self, parent=None, include_primary=False):
        super().__init__(parent)
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.NoInsert)
        self.setMaxVisibleItems(14)
        self.setMinimumContentsLength(12)
        self.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.setAccessibleName('Search output languages')
        self.lineEdit().setPlaceholderText('Search languages…')
        for code, (name, _, _) in OUTPUT_LANGUAGES.items():
            if include_primary or code != 'en':
                self.addItem(name, code)
        self.completer().setCompletionMode(QCompleter.PopupCompletion)
        self.completer().setCaseSensitivity(Qt.CaseInsensitive)
        self.completer().setFilterMode(Qt.MatchContains)
        self.view().setMinimumWidth(330)
        self.completer().popup().setMinimumWidth(330)
        self.lineEdit().editingFinished.connect(self._commit_search)
        self.currentIndexChanged.connect(lambda: self.setToolTip(self.currentText()))

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(QColor('#94A3B8' if self.isEnabled() else '#475569'), 1.5))
        x, y = self.width() - 14, self.height() / 2
        painter.drawPolyline([QPointF(x - 4, y - 2), QPointF(x, y + 2), QPointF(x + 4, y - 2)])

    def _commit_search(self):
        text = self.currentText().strip()
        index = next((i for i in range(self.count()) if self.itemText(i).casefold() == text.casefold()), -1)
        if index >= 0:
            self.setCurrentIndex(index)
        # Never display arbitrary typed text while routing to another language.
        self.setEditText(self.itemText(self.currentIndex()))
