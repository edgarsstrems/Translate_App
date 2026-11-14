# Project backup analysis and proposal

Analysis: 2026-11-14. Counts include hidden files and Git; sizes are logical bytes, in GiB. Reparse points were not followed. External app data and remote services are outside these figures.

| Project | Files | GiB | Largest contributors |
|---|---:|---:|---|
| My-Portfolio-main | 7,226 | 0.075 | node_modules: 7,200 files, 0.074 GiB |
| Translate_App-main | 244,325 | 69.38 | build: 186,866 files, 60.71 GiB; dist-app: 15,700 files, 4.31 GiB; .venv: 40,553 files, 4.19 GiB; .git: 881 files, 0.16 GiB |
| WebTranslate-main | 14,802 | 0.194 | frontend/node_modules: 7,591 files, 0.112 GiB; backend/node_modules: 6,731 files, 0.078 GiB |

## Classification and review

A means development/restore essential; B, runtime or persistent data; C, potentially reproducible once verified; D, demonstrably disposable. Preserve uncertain files.

- My-Portfolio: A: React/JSX source and public assets. B or uncertain: installed dependencies. The root has no package.json, lockfile, index.html, build configuration or Git directory. Preserve all files, including node_modules; investigate whether a complete project exists elsewhere. A working restore is unverified.
- Translate_App: A: Python/PySide6 source, run.py, manifests, scripts, tests, documentation, assets and complete Git history, branches and local changes. B: dist-app runtime, configuration, credentials, local data, databases, recordings and offline models. The venv uses Python 3.11.9 with a machine-specific base installation; requirements are not fully locked. Preserve the venv until recreation and local package changes are verified. Build contains prior release/rollback bundles, settings, test artifacts and caches; review individually and retain uncertain data. Models, settings and durable queues may also exist outside the project in local app data. Preserve installed Yoruba and Whisper models; their presence and size remain unverified. SQLite snapshots must account for WAL/SHM and active writers.
- WebTranslate: A: TypeScript backend, React/Vite frontend, package manifests and lockfiles, SQL schema, desktop Python source, assets, configuration, documentation and Git metadata. B: runtime dependencies, settings, credentials and live PostgreSQL data. The schema is not a database backup; a remote database and deployment configuration need a separately authorized export. Node dependencies are C only if clean restoration, patch comparison and build succeed. Handle frontend production configuration privately. Full restoration is unverified.
- D: No entire directory has been established as disposable. Inspect caches and repeated build artifacts individually; matching names and sizes alone do not establish identical contents.

## Proposed file-count reduction

Pack the Portfolio node_modules, both WebTranslate node_modules directories, and Translate_App .venv into separate lossless fast/store-mode 7z archives inside their clean copies. Verify each archive and its contents. This preserves dependencies, including possible local changes, while reducing physical entries to transfer. The estimates assume one archive per directory, exclude added guides, and assume no compression savings:

| Project | Estimated physical entries | Entry reduction | Estimated byte reduction |
|---|---:|---:|---:|
| My-Portfolio | 27 | 7,199 (99.6%) | approximately 0% |
| Translate_App | 203,773 | 40,552 (16.6%) | approximately 0% |
| WebTranslate | 482 | 14,320 (96.7%) | approximately 0% |

These are physical entry counts, not reductions in recoverable logical files. Archive overhead and compression will affect sizes. The 60.71 GiB Translate_App build directory is the largest remaining opportunity, but it contains potentially valuable data. A path-and-size comparison found 161,560 files totaling 36.02 GiB under build bundle _internal directories with a same-relative-path, same-length file in the current packaged runtime. This is a candidate ceiling, not confirmed savings: contents have not been hashed, older releases may matter, and omission would require a verified reconstruction map. After approval, examine and selectively pack verified generated bundles; only omit individual files proven unnecessary or reproducible, preserving anything uncertain. No blanket exclusion of build, models, databases, venvs or node_modules is proposed.

## Execution after approval

1. Create My_Projects_Clean_Backup without overwriting prior backups. Preserve source, Git metadata and local changes, settings, data, credentials and runtime binaries. Do not follow links outside project roots. Check destination capacity for both copies and final archives.
2. Inventory relevant external app data and models, then use consistent database snapshots when inactive or via a safe database backup mechanism. Do not touch remote or production databases without separate authorization. Report missing external resources as incomplete.
3. Pack dependencies, compare archive manifests and hashes with originals, and write each project's RESTORE_INSTRUCTIONS.md covering software versions, installation, models, databases, credentials and known limitations. Keep private content off GitHub; use encrypted storage or an encrypted archive with a password supplied securely.
4. Verify copies and archives by counts and hashes. Run isolated offline restoration checks where practical, without paid APIs, production changes, or external services. Record all unverified requirements and missing files.
5. Create and test one fast 7z archive per project and retain clean copies. Report actual sizes, counts, exclusions and risks. 7-Zip is installed. Restoration has not yet been verified.

No backup, archive, dependency installation, build, paid request or restore test was performed in this analysis. Counts may change as working files change. Existing application changes are excluded from the report commit.
