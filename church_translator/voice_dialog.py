"""Cloud voice preferences; all network and playback work runs off the UI thread."""
from dataclasses import replace
from PySide6.QtCore import QThread, Signal, QTimer
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QFormLayout, QHBoxLayout,
    QComboBox, QDoubleSpinBox, QPushButton, QLabel, QMessageBox, QLineEdit)
from .services import TextToSpeech
from .languages import OUTPUT_LANGUAGES, default_voice
from .language_selector import LanguageSelector
from .reliability import safe_error


class VoiceTask(QThread):
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, work, parent):
        super().__init__(parent)
        self.work = work

    def run(self):
        try:
            self.done.emit(self.work())
        except Exception as exc:
            self.failed.emit(safe_error(exc))


class VoiceSettingsDialog(QDialog):
    def __init__(self, config, settings, output_device, volume, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Cloud Voice Settings")
        from .dialogs import DIALOG_STYLESHEET
        self.setStyleSheet(DIALOG_STYLESHEET)
        self.resize(650, 360)
        self.config = config
        self.settings = {k: dict(v) for k, v in settings.items() if isinstance(v, dict)}
        self.output_device, self.volume = output_device, volume
        self._task = None
        self._language = "en"
        self._catalog = {}
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.language = LanguageSelector(include_primary=True)
        self.language.setCurrentIndex(self.language.findData('en'))
        form.addRow("Language:", self.language)
        self.voice = QComboBox()
        self.voice.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.voice.setMinimumContentsLength(20)
        form.addRow("Voice:", self.voice)
        self.rate = QDoubleSpinBox()
        self.rate.setRange(.5, 2)
        self.rate.setSingleStep(.05)
        self.pitch = QDoubleSpinBox()
        self.pitch.setRange(-10, 10)
        self.pitch.setSuffix(" semitones")
        self.gain = QDoubleSpinBox()
        self.gain.setRange(-12, 6)
        self.gain.setSuffix(" dB")
        form.addRow("Speaking speed:", self.rate)
        form.addRow("Pitch (where supported):", self.pitch)
        form.addRow("Voice volume gain:", self.gain)
        self.sample_text = QLineEdit()
        self.sample_text.setPlaceholderText('Enter a short preview in the selected language')
        form.addRow('Preview text:', self.sample_text)
        self.rate.setToolTip('1.0 is normal speed. Available only for voices supporting speaking rate.')
        self.pitch.setToolTip('Pitch change in semitones; available only for compatible Cloud voices.')
        self.gain.setToolTip('Synthesis volume in decibels, separate from the output device volume.')
        self.reset = QPushButton('Reset voice settings')
        self.reset.clicked.connect(self._reset)
        form.addRow('', self.reset)
        layout.addLayout(form)
        self.note = QLabel("Load the catalog to discover voices available through your configured account.")
        self.note.setWordWrap(True)
        layout.addWidget(self.note)
        row = QHBoxLayout()
        self.discover = QPushButton("Refresh Voice Catalog")
        self.preview = QPushButton("Preview Voice")
        self.save = QPushButton("Save Voice Settings")
        for b in (self.discover, self.preview, self.save):
            row.addWidget(b)
        layout.addLayout(row)
        self.discover.clicked.connect(self._discover)
        self.preview.clicked.connect(self._preview)
        self.save.clicked.connect(self._save)
        self.language.currentIndexChanged.connect(self._change_language)
        self.voice.currentIndexChanged.connect(self._voice_changed)
        self._load_language()
        self._discovery_timer = QTimer(self)
        self._discovery_timer.setSingleShot(True)
        self._discovery_timer.timeout.connect(self._discover)
        if not self.config.google_application_credentials:
            self.note.setText('Free web speech uses an automatic voice for each language. Voice selection, speed, pitch and gain require configured Google Cloud TTS. Availability of web speech varies by language.')
            for control in (self.voice, self.rate, self.pitch, self.gain, self.reset, self.discover, self.preview, self.save):
                control.setEnabled(False)

    def showEvent(self, event):
        super().showEvent(event)
        if self.config.google_application_credentials and self._language not in self._catalog:
            self._discovery_timer.start(0)

    def _reset(self):
        self.rate.setValue(0.95 if self._language == "en" else 1.08)
        self.pitch.setValue(0)
        self.gain.setValue(0)
        self._voice_changed()

    def _stash(self):
        name = self.voice.currentData()
        if name:
            self.settings[self._language] = {"voice": name, "rate": self.rate.value(), "pitch": self.pitch.value(), "gain": self.gain.value()}

    def _change_language(self):
        self._stash()
        self._language = self.language.currentData()
        self._load_language()
        if self.config.google_application_credentials and self._language not in self._catalog:
            self._discover()

    def _load_language(self):
        samples = {'en': 'May hope bring peace to your heart.', 'lv': 'Lai cerības vēsts nes mieru jūsu sirdī.',
                   'lt': 'Tegul viltis atneša ramybę jūsų širdžiai.', 'ru': 'Пусть надежда принесёт мир вашему сердцу.'}
        self.sample_text.setText(samples.get(self._language, ''))
        current = self.settings.get(self._language, {})
        default = default_voice(self.config, self._language)
        selected = current.get("voice", default)
        self.voice.blockSignals(True)
        self.voice.clear()
        catalog = self._catalog.get(self._language, [])
        if not catalog:
            self.voice.addItem(selected + " (configured; refresh to verify)", selected)
        for item in catalog:
            name = item['name']
            gender = str(item.get('gender', 'Unspecified')).replace('SSML_VOICE_GENDER_', '').title()
            label = name.removeprefix(OUTPUT_LANGUAGES[self._language][1] + '-')
            self.voice.addItem(f'{label} — {gender}', name)
            self.voice.setItemData(self.voice.count() - 1, name + '\n' + item.get('price', ''), 3)
        selected_index = self.voice.findData(selected)
        self.voice.setCurrentIndex(selected_index if selected_index >= 0 else 0)
        self.voice.blockSignals(False)
        self.rate.setValue(current.get("rate", 0.95 if self._language == "en" else 1.08))
        self.pitch.setValue(current.get("pitch", 0))
        self.gain.setValue(current.get("gain", 0))
        self._voice_changed()

    def _voice_changed(self):
        name = self.voice.currentData() or ""
        caps = TextToSpeech.voice_capabilities(name)
        self.rate.setEnabled(caps["rate"] and bool(self.config.google_application_credentials))
        self.pitch.setEnabled(caps["pitch"] and bool(self.config.google_application_credentials))
        if not caps["rate"]:
            self.rate.setValue(1)
        if not caps["pitch"]:
            self.pitch.setValue(0)
        self.note.setText(TextToSpeech.voice_price(name) + ". Preview and sermon synthesis use your Google Cloud billing account. No expressive style control is exposed for these voices.")

    def _run(self, work, done):
        for b in (self.preview, self.discover, self.save, self.language):
            b.setEnabled(False)
        self._task = VoiceTask(work, self)
        self._task.done.connect(done)
        self._task.failed.connect(lambda error: self.note.setText("Voice request failed: " + error))
        self._task.finished.connect(self._finished)
        self._task.start()

    def _finished(self):
        for b in (self.preview, self.discover, self.save, self.language):
            b.setEnabled(True)

    def _discover(self):
        if self._task and self._task.isRunning():
            return
        self._stash()
        language = self._language
        self.note.setText("Discovering supported Cloud voices…")
        def loaded(result):
            self._catalog[language] = result
            self._load_language()
            if not result:
                self.note.setText("No supported voices were returned for this language.")
        self._run(lambda: TextToSpeech(self.config).list_voices(language), loaded)

    def _preview(self):
        parent = self.parent()
        if parent and getattr(parent, "engine", None):
            self.note.setText("Stop the active sermon before previewing to avoid overlapping listener audio.")
            return
        self._stash()
        language = self._language
        sample = self.sample_text.text().strip()
        if not sample:
            self.note.setText('Enter preview text in the selected language first.')
            return
        selected = self.voice.currentData()
        if not any(v["name"] == selected for v in self._catalog.get(language, [])):
            self.note.setText("Refresh the voice catalog and select a verified voice before previewing.")
            return
        if QMessageBox.question(self, "Cloud Voice Preview", TextToSpeech.voice_price(selected) + ".\nSynthesize and play a short sample? Google Cloud usage may incur charges.", QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel) != QMessageBox.Yes:
            return
        config = replace(self.config, voice_settings=self.settings)
        device, volume = self.output_device(language), self.volume(language)
        def play():
            import io
            import soundfile as sf
            import sounddevice as sd
            audio = TextToSpeech(config).synthesize(sample[:300], language, strict=True)
            data, rate = sf.read(io.BytesIO(audio), dtype="float32", always_2d=True)
            # A dedicated output stream preserves routing without sd.play's global stream.
            with sd.OutputStream(samplerate=rate, device=device, channels=data.shape[1], dtype="float32") as stream:
                stream.write(data * volume)
            return len(data) / rate
        self.note.setText("Synthesizing and playing preview…")
        self._run(play, lambda duration: self.note.setText(f"Preview complete ({duration:.1f}s)."))

    def _save(self):
        self._stash()
        for language, value in self.settings.items():
            original = self.config.voice_settings.get(language, {})
            default = default_voice(self.config, language)
            new_premium_default = not original and '-Chirp3-HD-' in value['voice']
            if value["voice"] != original.get("voice", default) or new_premium_default:
                if not any(v["name"] == value["voice"] for v in self._catalog.get(language, [])):
                    self.note.setText("Refresh the catalog before saving a new voice.")
                    return
                if not any(f"-{family}-" in value["voice"].lower() for family in ("standard", "wavenet")):
                    if QMessageBox.question(self, "Premium Cloud Voice", TextToSpeech.voice_price(value["voice"]) + ".\nApply this voice to future sermons?", QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel) != QMessageBox.Yes:
                        return
        self.accept()

    def closeEvent(self, event):
        self._discovery_timer.stop()
        if self._task and self._task.isRunning():
            self.note.setText("Wait for the bounded voice request to finish before closing.")
            event.ignore()
            return
        super().closeEvent(event)

    def reject(self):
        if not self._task or not self._task.isRunning():
            super().reject()
