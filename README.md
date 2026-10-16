# IA → SSPI static catalog (GitHub Pages)

A GitHub Action queries Archive.org once a day, builds a static SSPI source file, and publishes it to
GitHub Pages. **Your PC and your Wi-Fi quota are not involved**: the PS4 downloads packages straight
from Archive.org, and GitHub only serves a small JSON file plus cover art.

## What gets published

| File | Purpose |
|---|---|
| `source.json` / `source.gssource` | The catalog (identical content). Package links are direct `https://iaNNNNNN.us.archive.org/...` storage-node URLs |
| `art/<item>/{icon,cover,background}.*` | Cover art copied from Archive.org so the console loads it from Pages |
| `assets/placeholder_*.png` | Used when an item has no usable art |
| `index.html` | Landing page showing your source URL and item list |

Packages are never stored on GitHub (Pages is for small static files).

## One-time setup

1. **Create a GitHub account** and a **new public repository** (e.g. `ps4-source`). Pages on the free
   plan requires a public repo. Leave it empty (no README/license).
2. **Edit `config.yaml`**: set `contact` and replace the placeholder `sources` with the Archive.org
   items/collections/uploaders you want. Only list content you have the right to distribute or use.
3. **Push the project** (from inside this folder; install Git first if needed):
   ```bash
   git init
   git add .
   git commit -m "Initial catalog generator"
   git branch -M main
   git remote add origin https://github.com/<your-username>/<your-repo>.git
   git push -u origin main
   ```
   When asked to sign in, use your GitHub username and a **personal access token** as the password
   (GitHub → Settings → Developer settings → Personal access tokens, with `repo` scope), or use
   GitHub Desktop / `gh auth login` instead.
4. **Turn on Pages**: repo → *Settings* → *Pages* → *Build and deployment* → Source: **GitHub Actions**.
5. **Run it once**: repo → *Actions* → *Refresh catalog* → *Run workflow*. When both jobs are green
   your catalog is live at:

   `https://<your-username>.github.io/<your-repo>/source.json`

6. In SSPI add that URL as a custom source. From now on it refreshes itself every day (03:17 UTC);
   edit `config.yaml` and push to change sources, which also triggers a rebuild.

## Run locally (optional, to test)

```bash
pip install -r requirements.txt
python generate_static_catalog.py --out site
```
Locally it needs `pages_base_url` in `config.yaml` (e.g. `https://user.github.io/repo`) to produce
absolute art URLs. This uses Archive.org lookups (metadata and small images), not package downloads.

## Safety nets

- If more than `max_error_ratio` of lookups fail, or a source query fails, or zero entries are
  produced, the script exits non-zero and the workflow **does not deploy**, so your last good catalog
  stays online.
- Incomplete split sets (e.g. `.z02` missing) are skipped, as are items whose first/last file can't be
  verified on Archive.org with a HEAD request. Dead links don't reach the console.
- `collection` and `query` sources only include items that carry a license URL (override per source
  with `require_licenseurl`).
- Art: your override → art inside the item → Archive.org's thumbnail → hosted placeholder. Background
  falls back to the cover first.

## Things to know

- **Node URLs can go stale.** Archive.org occasionally moves items between storage nodes. The daily
  rebuild re-resolves them, so a link may be wrong for up to a day after a move. If that bites,
  set `link_style: canonical` (`archive.org/download/...`; Archive.org redirects the console straight to
  the node, so still nothing goes through your PC), provided SSPI follows redirects.
- **SSPI field names are unverified.** Compare `source.json` with a source file known to work on your
  console and adjust key names in `to_sspi()` in `generate_static_catalog.py`.
- **GitHub Pages is HTTPS-only.** If your PS4/SSPI can't fetch the HTTPS URL, that's a console TLS
  limitation and the catalog host would need to change.
- GitHub pauses scheduled workflows after ~60 days without repository activity; re-enable from the
  Actions tab or push any commit.
- Workflow action versions (`checkout@v4`, `deploy-pages@v4`, ...) may need bumping eventually.
