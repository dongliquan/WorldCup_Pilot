# Security policy

## Reporting a vulnerability

Please report vulnerabilities **privately** with GitHub's *Report a vulnerability* button (Security tab) instead of a
public issue. Include what you found, how to reproduce it, and the version or commit.

## What the app does

- The local server listens on `127.0.0.1` only. It is meant for the desktop window on the same PC.
- The app has no telemetry. It calls free public services (ESPN, TheSportsDB, Wikipedia, Open-Meteo, flagcdn,
  YouTube) from your PC, and the optional AI providers (Groq, OpenAI, GitHub Models) only if you add a key.
- **Keep your keys private.** AI keys live in your local `config.json`, which is git-ignored. Never commit it, and
  remove real keys before you share a build folder.
