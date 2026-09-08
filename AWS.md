# AWS EC2 Deployment Guide

Deploys the containerized API so it survives reboots via `systemctl enable`. Assumes an
Ubuntu EC2 instance and an existing SSH key pair. Replace `<EC2_IP>` and `<KEY.pem>`
throughout.

## 1. Launch the instance

- Ubuntu 22.04/24.04 LTS, t3.small or larger (this app is I/O-bound, not CPU-bound — see
  `CLAUDE.md`'s measured latency numbers; a small instance is fine).
- Security group: allow inbound **22** (SSH, your IP only) and **8001** (or put a
  reverse proxy/ALB in front on 443 and keep 8001 internal — see §6). Outbound: allow
  HTTPS (443) to the internet — this app calls 8+ external weather/geocoding APIs.
- Note the instance's public IP/DNS and confirm you can SSH in before continuing.

## 2. Install Docker on the instance

```bash
ssh -i <KEY.pem> ubuntu@<EC2_IP>
sudo apt-get update && sudo apt-get install -y ca-certificates curl gnupg
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update && sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo usermod -aG docker ubuntu
```
Log out and back in (or `newgrp docker`) so your session picks up the docker group.
Confirm Docker starts on boot (usually already enabled by the package):
```bash
sudo systemctl enable --now docker
```

## 3. Copy the repo to the instance

From your **local machine** (not the EC2 instance):
```bash
ssh -i <KEY.pem> ubuntu@<EC2_IP> "sudo mkdir -p /opt/weathergpt && sudo chown ubuntu:ubuntu /opt/weathergpt"
scp -i <KEY.pem> -r ~/weathergpt/* ubuntu@<EC2_IP>:/opt/weathergpt/
```
The plain `scp -r` above copies everything, **including your local `.env` if it's
present** — double-check it's the `.env` you actually want deployed (production keys,
not local dev ones) before running it, and it also drags along `.venv`/`.git`/
`__pycache__`, which is wasteful but harmless. To skip those (plain `scp` has no
`--exclude`; `rsync` does), use this instead:
```bash
rsync -avz -e "ssh -i <KEY.pem>" --exclude='.venv' --exclude='.git' --exclude='__pycache__' ~/weathergpt/ ubuntu@<EC2_IP>:/opt/weathergpt/
```
Either way, if you excluded `.env`, create it directly on the instance in the next step
instead.

## 4. Set up `.env` on the instance

```bash
ssh -i <KEY.pem> ubuntu@<EC2_IP>
cd /opt/weathergpt
nano .env   # or vim — fill in production values, see .env.example for every key
```
At minimum for production: `WEATHERGPT_API_KEYS` (generate a real key — see §7),
`WEATHERGPT_CORS_ORIGINS` if a browser client will call this directly,
`WEATHERGPT_LOG_JSON=true`, and whichever of `SMALL_LLM_*`/`GEOAPIFY_API_KEY`/
`STORMGLASS_API_KEY`/`IMD_API_KEY`/`CAP_FEED_URL` you're using. Never commit this file —
it's already gitignored.

## 5. Build and start

```bash
cd /opt/weathergpt
docker compose up -d --build
docker compose logs -f   # watch startup, Ctrl-C to stop watching (container keeps running)
curl -s http://localhost:8001/health | python3 -m json.tool
```

## 6. Make it survive a reboot (systemd)

```bash
sudo cp /opt/weathergpt/weathergpt.service /etc/systemd/system/weathergpt.service
sudo systemctl daemon-reload
sudo systemctl enable weathergpt
sudo systemctl start weathergpt
sudo systemctl status weathergpt
```
`enable` makes it start on every boot; `start` starts it now. Test it for real:
```bash
sudo reboot
# wait ~30s, then reconnect:
ssh -i <KEY.pem> ubuntu@<EC2_IP>
sudo systemctl status weathergpt   # should be active
curl -s http://localhost:8001/health
```

## 7. Give your friend access

Generate a key and add it to `.env`'s `WEATHERGPT_API_KEYS` (comma-separated if more
than one caller), then restart:
```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
# add the output to WEATHERGPT_API_KEYS= in .env, then:
sudo systemctl restart weathergpt
```
Hand them: the base URL (`http://<EC2_IP>:8001` or your domain), the key, and point them
at `http://<EC2_IP>:8001/openapi.json` (or `/docs` if 8001 is reachable from their
browser) for the contract. They call `POST /query` with
`Authorization: Bearer <key>`.

## 8. Operational notes (see CLAUDE.md's outstanding-work register)

- Run **one** uvicorn worker (already the default — don't add `--workers`) until the
  in-process caches move to Redis; multiple workers would silently split state.
- `WEATHERGPT_MET_NORWAY_USER_AGENT` must identify your deployment or api.met.no 403s it.
- The systemd unit's `WorkingDirectory=/opt/weathergpt` must match wherever you actually
  put the repo — edit `weathergpt.service` before copying it if you used a different path.
- `docker compose down && docker compose up -d --build` to deploy an update; the systemd
  unit doesn't need touching for routine updates, only for path/service changes.

## GFS/GRIB2 — closed 2026-09-08

The Docker image now installs `requirements-full.txt` (cfgrib/eccodes/xarray) plus the
system `libeccodes0`/`libeccodes-data` packages the pip `eccodes` package needs at import
time (confirmed live: it installs cleanly without them, then raises `RuntimeError: Cannot
find the ecCodes library` on import). Live-verified by building the image and running a
real fetch — GFS decodes 6 variables (temperature, precipitation, wind speed/direction,
humidity, pressure). Nothing further to do here for a standard deploy.
