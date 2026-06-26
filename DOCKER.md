# Running with Docker Compose

One command builds and runs everything (Flask app under gunicorn).

## Local

```bash
cd zoom-interview-portal
cp .env.example .env          # DEMO_MODE=true → sample data, no AWS needed
docker compose up -d --build
```

Open **http://localhost:8000** → sign in `naman` / `NamanPass123`.

```bash
docker compose logs -f        # watch logs
docker compose down           # stop
docker compose up -d --build  # apply code/.env changes
```

Created users persist in the `portal-data` volume across rebuilds.

---

## On EC2 (real S3)

### 1. Install Docker (Ubuntu)
```bash
sudo apt update
sudo apt install -y docker.io docker-compose-v2
sudo usermod -aG docker $USER
newgrp docker        # or log out/in
```

### 2. Get the code + configure
```bash
git clone <your-repo> ~/zoom-interview-portal   # or scp the folder up
cd ~/zoom-interview-portal
cp .env.example .env
nano .env        # SECRET_KEY (random), ADMIN_USERS, DEMO_MODE=false, AWS_REGION=us-east-1
```

### 3. ⚠️ Let the container reach the IAM role (the one Docker gotcha)
The app gets AWS creds from the EC2 instance role via the metadata service.
Containers add a network hop, and IMDSv2's default hop limit of **1** blocks
them. Bump it to **2** once per instance:

```bash
TOKEN=$(curl -s -X PUT "http://169.254.169.254/latest/api/token" \
  -H "X-aws-ec2-metadata-token-ttl-seconds: 60")
IID=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" \
  http://169.254.169.254/latest/meta-data/instance-id)

aws ec2 modify-instance-metadata-options \
  --instance-id "$IID" \
  --http-put-response-hop-limit 2 \
  --http-tokens required
```

(Attach the IAM role from `deploy/iam-policy.json` to the instance first — see
README §3. No AWS keys go in `.env` on EC2.)

If you'd rather not touch IMDS, the alternative is `network_mode: host` on the
`portal` service in `docker-compose.yml` — but bumping the hop limit is cleaner.

### 4. Run
```bash
docker compose up -d --build
docker compose logs -f
```

### 5. Expose it
- To serve on **port 80**: change the ports line in `docker-compose.yml` to
  `"80:8000"`, then `docker compose up -d`.
- Open that port in the instance **security group**, source restricted to your
  **office IP / VPN** (this tool exposes recordings — don't use `0.0.0.0/0`).
- Add HTTPS before any public exposure (Caddy/Traefik/nginx + certbot).

---

## Tuning (optional)
gunicorn workers/threads are env-driven — set in `.env`, no rebuild needed:
```ini
GUNICORN_WORKERS=2      # default 3; use 2 on a t3.micro (1 GB)
GUNICORN_THREADS=4
```
