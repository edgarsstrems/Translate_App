# Church Sermon Live Translator 🎙️✝️

[![Platform](https://img.shields.io/badge/Platform-Windows%2010%20%2F%2011-blue.svg)](https://www.microsoft.com/windows)
[![Python](https://img.shields.io/badge/Python-3.10%2B-green.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A high-performance Windows desktop application engineered specifically for **real-time live church sermon translation**. 

It captures spoken Latvian audio directly from a mixer, soundboard, or microphone, performs fast speech-to-text, contextually translates theological speech into English and Russian simultaneously using AI, and synthesizes concurrent spoken voice output (Text-to-Speech) routed to separate headphone or wireless assistive listening transmitter channels.

---

## 🚀 Quick Start: 1-Click Automated Setup (Recommended)

You do **not** need to manually configure Python, virtual environments, or terminal commands. The included automated builder handles everything for you.

### 1. Download the Project
* Click the green **`< > Code`** button at the top of this GitHub page and select **Download ZIP**.
* Extract the downloaded `.zip` file into a folder on your computer.

### 2. Build the Standalone `.exe` App
Double-click **`build.bat`** (or simply double-click **`run.bat`**).

> 💡 **What `build.bat` does automatically:**
> 1. Checks if Python 3.11+ is installed on your Windows PC (and automatically installs it via Microsoft `winget` if missing).
> 2. Creates an isolated environment (`.venv`) and installs all audio, AI, and GUI dependencies.
> 3. Compiles a native Windows standalone application: **`dist-app\ChurchTranslator\ChurchTranslator.exe`**.
> 4. Generates your configuration files and sets up all assets.

### 3. Launch the Application Anytime
Once built, you can start the translator with a single click:
* Double-click **`run.bat`** in the main project folder, **or**
* Open **`dist-app\ChurchTranslator\`** and double-click **`ChurchTranslator.exe`**.

That's it! The application launches like a regular Windows app with no terminal windows required.

---

## 🌟 Key Features

### 🎙️ 1. Real-Time Speech Recognition (STT)
* **Cloud AI Speech-to-Text**: Powered by OpenAI (`gpt-4o-mini-transcribe` / `gpt-4o-transcribe`) for low-latency, accurate sermon transcription.
* **Offline / Local Whisper**: Built-in support for `faster-whisper` (`small`, `medium`, `large-v3-turbo`) with CPU and NVIDIA GPU (CUDA) acceleration for offline transcription.
* **Smart Voice Activity Detection (VAD)**: Pause-aware audio chunking with automatic silence-flush (0.4s) to capture natural sermon sentences and keep latency minimal.

### 🧠 2. Context-Aware Theological Translation
* **Gemini AI Translation**: High-speed, context-rich translation powered by Google Gemini (`gemini-2.0-flash`, `gemini-2.0-flash-lite`, etc.).
* **Theological Prompting & Continuity**: Translates with deep awareness of Christian sermon context and maintains rolling historical context (preventing pronoun drift and ensuring biblical references like *"Tas Kungs"* $\rightarrow$ *"The Lord"* and *"ar Viņu"* $\rightarrow$ *"with Him"*).
* **Dual Joint Translation (Free Tier Optimized)**: Translates Latvian into both English and Russian simultaneously in a single API call to minimize latency, conserve quota, and eliminate rate limits.
* **Custom Glossary Normalization**: Built-in acoustic and phonetic correction engine (`glossary.json`) to repair spoken homophones, church terminology, and biblical names.

### 🔊 3. Multi-Channel Spoken Audio Output (TTS)
* **Built-in Offline Speech Engine**: Instant zero-configuration local speech synthesis out of the box.
* **Google Cloud Text-to-Speech**: Studio-grade neural voices (`en-US`, `ru-RU`) with configurable voice styles.
* **Independent Output Hardware Routing**: Route English translation to one USB sound card / wireless audio transmitter channel and Russian translation to another.
* **Independent Volume & Mute Controls**: Easily balance and toggle audio levels for each language during live services.

### 💻 4. Modern Desktop GUI
* **Clean Dark Theme UI**: Built with PySide6 for high responsiveness during live operation.
* **Live Audio VU Meters**: Real-time microphone input visualizer with dynamic green/yellow/red levels and decibel readouts.
* **Live Transcript Feed**: Synchronized three-column or unified transcript cards displaying Latvian source, English translation, and Russian translation.
* **Audio Diagnostic Tester**: Built-in audio test panel to quickly verify microphone input and headphone outputs before services start.
* **In-App Key Setup**: Paste and test Gemini and OpenAI API keys directly in the app without editing configuration files manually.

---

## ⚙️ Configuration & API Keys

You can configure API keys directly inside the desktop app via the **Settings / API Keys** menu, or by editing the `.env` file (in `dist-app\ChurchTranslator\.env` or `.env` in the root folder):

```env
# ------------------------------------------------------------------------------
# 1. Gemini AI Translation (Recommended)
# ------------------------------------------------------------------------------
# Get your free key at: https://aistudio.google.com/app/apikey
GEMINI_API_KEY=your_gemini_api_key_here
GEMINI_MODEL=gemini-2.0-flash
TRANSLATION_PROVIDER=gemini
FREE_TIER_MODE=true

# ------------------------------------------------------------------------------
# 2. Speech Recognition (STT)
# ------------------------------------------------------------------------------
# Backend options: "openai" (cloud) or "local" (faster-whisper)
SPEECH_RECOGNITION_BACKEND=openai
OPENAI_API_KEY=your_openai_api_key_here
OPENAI_TRANSCRIPTION_MODEL=gpt-4o-mini-transcribe

# ------------------------------------------------------------------------------
# 3. Audio & Chunking Tuning
# ------------------------------------------------------------------------------
CHUNK_SECONDS=4.5
MIN_CHUNK_SECONDS=2.5
EARLY_FLUSH_SILENCE_SECONDS=0.4
```

---

## 📖 Glossary Customization

The app includes a specialized dictionary in `glossary.json` designed for church terminology and acoustic corrections:

* **`source_replacements`**: Corrects phonetic or acoustic mishearings from the microphone before translation (e.g. fixing homophones or preacher-specific vocal nuances).
* **`theological_terms`**: Enforces strict Christian theological mappings for words across Latvian, English, and Russian.

Example from `glossary.json`:
```json
{
  "theological_terms": {
    "Svētais Gars": {
      "en": "Holy Spirit",
      "ru": "Святой Дух"
    },
    "Tas Kungs": {
      "en": "The Lord",
      "ru": "Господь"
    }
  }
}
```

---

## 🛠️ Manual Installation (For Developers)

If you prefer to develop or run from source without building the standalone `.exe`:

```powershell
# 1. Clone repository
git clone https://github.com/edgarsstrems/Translate_App.git
cd Translate_App

# 2. Create and activate virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 3. Install core dependencies
pip install --upgrade pip
pip install -r requirements.txt

# 4. Create environment file
copy .env.example .env

# 5. Run the application
python run.py
```

### Optional: Local Offline Whisper Setup
To run local offline transcription on your PC instead of cloud OpenAI STT:
```powershell
pip install -r requirements-local-whisper.txt
```

---

## 🧪 Running Automated Tests

Run the full test suite to verify pipeline chunking, theological glossary rules, deduplication, and translation integrity:

```powershell
.\.venv\Scripts\python.exe -m unittest discover tests
```

---

## 🔒 Security & Privacy

* **No Credentials Stored in Git**: The repository strictly ignores `.env`, private keys, and credential tokens via `.gitignore`.
* **Safe Template Provided**: Only `.env.example` is committed as a reference.
* **Local Audio Processing**: Audio buffers are processed in memory and are not stored to disk unless `SAVE_DEBUG_AUDIO=true` is explicitly enabled.

---

## 📄 License

This project is licensed under the [MIT License](LICENSE).
