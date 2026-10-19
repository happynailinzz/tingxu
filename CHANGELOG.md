# Changelog

## Unreleased

- Add optional Feishu document publishing after summaries and a manual publish action.
- Strip archive frontmatter from published Markdown while retaining local metadata.
- Persist document links and display a compact Feishu document entry in recording details.
- Align recording toolbar controls and use a two-column layout on phones.
- Fix npm CLI startup under launchd by including the Node installation directory in PATH.

## 0.1.0 — 2026-10-17

- Introduce Tingxu (听序), a local recording workspace for QS668 devices and imported audio.
- Add wired/BLE import, audio library, playback, transcript/summary reading, and recoverable local removal.
- Add model and prompt settings, task stages, vocabulary groups/weights, literal corrections, and local voiceprint enrollment with manually confirmed suggestions.
- Adapt navigation and reading to phone, tablet, and desktop screen widths.
- Include launchd setup, regression tests, and private-data exclusions for public source distribution.

Hardware behavior, biometric accuracy, and visual layouts require validation on target devices. Automated model tests use synthetic fixtures or mocked inference.
