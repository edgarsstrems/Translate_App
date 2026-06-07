# Translate App

Translate App is a Windows desktop tool for live church sermon translation.
It listens to Latvian speech, transcribes it, translates it into English and/or Russian, and plays the result through selected output devices.

## What it does

- Live speech capture with pause-aware chunking
- Latvian speech-to-text with either OpenAI or local `faster-whisper`
- Translation with Gemini or Google Cloud Translate
- Text-to-speech output with Google Cloud Text-to-Speech
- Separate language toggles, output devices, and volume controls
- Glossary-based church term corrections for consistent wording
- Saved settings so the app remembers devices and volumes between launches

## Requirements

- Windows 10 or newer
- Python 3.10 or newer for local development
- API keys and credentials for the services you choose to use

## Setup for development

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

Then edit `.env` and fill in the values you want to use.

If you use Google Cloud Text-to-Speech or Google Cloud Translation, place your service account JSON inside:

```text
credentials\google-service-account.json
```

## Public build notes

The public repo does not include any API keys or credential files.
Only `.env.example` is provided as a template.

## Configuration

The main settings live in `.env`:

- `GEMINI_API_KEY`
- `OPENAI_API_KEY`
- `GOOGLE_TRANSLATE_API_KEY`
- `GOOGLE_APPLICATION_CREDENTIALS`

The glossary lives in `glossary.json`. Edit it to add source-term corrections and translation terms for church vocabulary.

## Running the app

```powershell
python run.py
```

## Building a Windows app

Use the packaging script to create a Windows bundle:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\package_windows_app.ps1
```

For a public package without embedded secrets:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\package_windows_app.ps1 -Public
```

## License

Add a license file before publishing the repository if you want others to reuse or modify the code.
