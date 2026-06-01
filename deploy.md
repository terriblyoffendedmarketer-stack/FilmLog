# Deploying FilmLog to PythonAnywhere (Free Tier)

## First-time setup

### 1. Create account
Go to [pythonanywhere.com](https://www.pythonanywhere.com) and sign up for a free account (Beginner plan, no card needed).

Your app will be at: `https://<username>.pythonanywhere.com`

### 2. Clone the repo
Open a **Bash console** from the PythonAnywhere dashboard:

```bash
cd ~
git clone https://github.com/prashil3k/FilmLog.git
cd FilmLog
```

### 3. Create virtual environment
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 4. Set up environment variables
Create the `.env` file with your API keys:

```bash
cat > ~/FilmLog/.env << 'EOF'
TMDB_API_KEY=your_tmdb_key_here
ANTHROPIC_API_KEY=your_anthropic_key_here
FILMLOG_PASSWORD=your_owner_password_here
FLASK_SECRET_KEY=any_random_string_here
EOF
```

The `FILMLOG_PASSWORD` protects write access — visitors can browse your library but only you can edit. `FLASK_SECRET_KEY` keeps your login session alive across server restarts.

### 5. Configure the web app
1. Go to the **Web** tab
2. Click **Add a new web app**
3. Choose **Manual configuration** (not Flask — we need custom WSGI)
4. Pick **Python 3.10** (or latest available)
5. Set these values:
   - **Source code**: `/home/<username>/FilmLog`
   - **Working directory**: `/home/<username>/FilmLog`
   - **Virtualenv**: `/home/<username>/FilmLog/venv`

6. Edit the **WSGI configuration file** — replace its entire content with:

```python
import sys
import os
from pathlib import Path

project_home = '/home/<username>/FilmLog'
if project_home not in sys.path:
    sys.path.insert(0, project_home)

from dotenv import load_dotenv
load_dotenv(os.path.join(project_home, '.env'), override=True)

from app import app as application
```

7. Click **Reload** (the big green button)

### 6. Visit your app
Go to `https://<username>.pythonanywhere.com` — you should see the enrichment screen.

---

## Updating after development

When you've made changes on your laptop and pushed to GitHub:

1. Open a **Bash console** on PythonAnywhere:
```bash
cd ~/FilmLog
git pull
source venv/bin/activate
pip install -r requirements.txt  # only if requirements changed
```

2. Go to the **Web** tab → click **Reload**

**Important**: `git pull` updates code only. Your `filmlog.db`, `taste_model.md`, and `taste_log.md` on PythonAnywhere are independent — they keep all your phone-based edits. The `.env` file is gitignored, so keys stay safe.

## Syncing data between laptop and phone

Your PythonAnywhere instance has its own `filmlog.db`. Edits you make on your phone (enrichment notes, ratings, etc.) live there.

To pull that data back to your laptop for a development session:
1. Download `filmlog.db` from PythonAnywhere Files tab
2. Replace your local `filmlog.db`
3. Work on features, push code
4. Upload the updated `filmlog.db` back (only if schema changed)

Most updates (new features, UI changes) don't touch the database schema, so you rarely need to sync the DB — just `git pull` + reload.

## Free tier notes
- **Always-on**: PythonAnywhere free tier keeps your app running 24/7 (no cold starts)
- **API whitelist**: Both `api.themoviedb.org` and `api.anthropic.com` are on PythonAnywhere's free-tier whitelist
- **Storage**: 512MB disk — plenty for SQLite + posters are loaded from TMDB CDN
- **Daily task**: Free tier allows one scheduled task — could be used for periodic taste model refresh
- **Renewal**: Free web apps expire after 3 months — click "Run until 3 months from today" on the Web tab to renew (free, takes 5 seconds)
