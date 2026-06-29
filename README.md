# Interview-Success Recording Portal

A small Flask web app to **search candidate interview recordings** in the
`zoom-automation-bucket` S3 bucket and **download** them — one file at a time or
as a single zip. Includes an **admin page** to create login users.

It reads the exact path layout your Lambda writes:

```
Interview-Success/{Host}/{Year}/{Month}/{Candidate}/{Company}/{Date}/{Round}/{MeetingID}/{FileType}/{file}
```

- **Search** by candidate name (typo/underscore tolerant).
- **Filter** by company, date (e.g. `2026-06-10` or `2026-06`), meeting ID, and file type (MP4 / M4A / TRANSCRIPT / CHAT…).
- **Download** individual files (direct from S3 via pre-signed URLs) or **all filtered results as a zip**.
- **Admin** accounts come from `.env`; admins create normal **users** stored (hashed) in `users.json`.

---

## 1. Run locally (no AWS needed)

> **Prefer Docker?** Skip to `DOCKER.md` — `docker compose up -d --build` runs the whole thing.

```bash
cd zoom-interview-portal
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # DEMO_MODE=true by default → sample data
python app.py
```

Open **http://localhost:8000** and sign in with an admin from `.env`:

```
username: naman     password: NamanPass123
```

In demo mode you get bundled sample candidates so the whole UI (search,
filters, individual + zip download, admin create/delete user) works offline.
Downloads return small placeholder files.

> Shortcut: `./run_local.sh` does all of the above.

### Test against the REAL bucket locally
1. Set `DEMO_MODE=false` in `.env`.
2. Give boto3 credentials one of these ways:
   - `aws configure` (recommended), **or**
   - uncomment `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` in `.env`.
3. The IAM user/role needs the permissions in `deploy/iam-policy.json`.
4. Restart `python app.py`.

---

## 2. The `.env` accounts

```ini
# Admins: plaintext, comma-separated user:password pairs. Add as many as you like.
ADMIN_USERS=naman:NamanPass123,parth:ParthPass123
```

- **Admins** → defined here, can open `/admin` and create/delete users.
- **Users** → created by an admin in the UI, saved to `users.json` with
  **hashed** passwords. They can search/download but not open `/admin`.

Always set a real `SECRET_KEY`:
```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

---

## 3. Connect to S3 — what to do on AWS

The app needs read access to the bucket. **Don't put access keys on the
server** — attach an IAM role to the EC2 instance instead.

### a) Create the IAM policy
IAM → Policies → Create policy → JSON → paste `deploy/iam-policy.json`
(it allows `ListBucket` on the bucket + `GetObject` on `Interview-Success/*`
only). Name it `recording-portal-s3-read`.

### b) Attach it to the EC2 instance via a role
- IAM → Roles → Create role → trusted entity **AWS service → EC2** → attach
  `recording-portal-s3-read` → name it `recording-portal-ec2-role`.
- EC2 → your instance → **Actions → Security → Modify IAM role** → select
  `recording-portal-ec2-role`.

boto3 picks up the role automatically — no keys in `.env`.

### c) Region
Set `AWS_REGION=us-east-1` in `.env` (your bucket is in N. Virginia). This
matters for pre-signed URL signatures.

> Pre-signed URLs and the bulk-zip both work with just `GetObject` on
> `Interview-Success/*`. No S3 CORS config is needed — individual downloads
> redirect the browser straight to S3, and zips are streamed through the app.

---

## 4. Deploy on EC2

```bash
# on the instance (Ubuntu)
sudo apt update && sudo apt install -y python3-venv nginx
git clone <your-repo> /home/ubuntu/zoom-interview-portal   # or scp the folder
cd /home/ubuntu/zoom-interview-portal
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
nano .env        # set SECRET_KEY, ADMIN_USERS, DEMO_MODE=false, region
```

### Run it as a service (gunicorn + systemd)
```bash
sudo cp deploy/recording-portal.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now recording-portal
sudo systemctl status recording-portal
journalctl -u recording-portal -f      # logs
```
The app now runs on `0.0.0.0:8000`.

### (Recommended) Put nginx in front
```bash
sudo cp deploy/nginx.conf /etc/nginx/sites-available/recording-portal
sudo ln -s /etc/nginx/sites-available/recording-portal /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
```

### Security group
Open the port you expose (8000 if no nginx, **80** with nginx) in the
instance's security group. **Restrict the source to your office IP / VPN**
range — this tool exposes recordings, so don't open it to `0.0.0.0/0`.
Add HTTPS (443 + a certificate, e.g. Caddy or certbot) before any public use.

---

## 5. How it works (quick map)

| File | Role |
|------|------|
| `app.py` | Flask routes: login, search, downloads, admin API |
| `s3_service.py` | Lists `Interview-Success/`, parses keys, caches, search/filter, pre-signed URLs, zip builder, demo data |
| `auth.py` | Admins from `.env`, users in `users.json` (hashed) |
| `templates/` | `login`, `search`, `admin` pages |
| `static/` | CSS + JS |
| `deploy/` | IAM policy, systemd unit, nginx config |

The bucket is listed once and cached for `CACHE_TTL_SEC` (default 5 min). New
recordings appear after the cache expires, or immediately via the **Refresh
index** button.

### Notes / limits
- **Bulk zip** streams each object through the app (uses EC2 bandwidth). Fine
  for a handful of files; for very large multi-GB selections prefer individual
  downloads (those go browser→S3 directly and don't touch the server).
- `users.json` is a flat file — good for a team-sized tool. Swap for a DB if
  you outgrow it. Each user record carries `departments` (which top-level folders
  they may browse) and `can_download` (download vs view-only).
- Departments are the top-level bucket folders listed in `DEPARTMENTS` (`.env`).
  The portal scans them all and an admin grants each user a subset on the Admin
  page. Access is enforced server-side on every search/download/view request.
- **View-only** users get an in-browser preview (`/api/view`, inline presigned
  URL) and the download/zip buttons are removed. Note this is a soft control:
  inline streaming can never be made fully un-saveable by a determined user — it
  stops casual downloads, not a screen recorder or devtools.
