# Roost — NYC Apartment Radar

Private, self-hosted NYC apartment search with StreetEasy via API.SH, saved searches, transit filtering, map view, favorites, and optional email alerts.

## Initial source upload

The initial repository contains the secret-safe `.gitignore`. The Roost application source is not yet pushed. From the folder containing the **GitHub-ready Roost source** (not the folder containing private `.env` and `scout.db`), run:

```powershell
git clone https://github.com/RyanZheng6/roost-nyc-apartment-finder.git
# Copy the app's source files into the cloned folder.
# Do NOT copy .env, scout.db, .venv, or backups.
cd roost-nyc-apartment-finder
git add .
git status
git commit -m "feat: initial Roost app"
git push origin main
```

**Before pushing**, verify that `git status` does not show any keys, passwords, database files, or backups. The app should keep its `.env` and `scout.db` exclusively on the local machine.

## Updating an existing installation

Once the source has been committed, updates can be retrieved from the Roost folder with `git pull`, followed by dependency installation and a server restart. Back up your local SQLite database before code/schema changes.

## Security

Keep the repository private. Never commit `.env`, database files, secret tokens, API keys, or Gmail app passwords. Rotate any credentials previously shared in a chat.
