# 🎥 Social Media Video Downloader & Dynamic Extractor Web Service

> High-performance FastAPI extraction service and remote dynamic Python script server for the Social Media Downloader Android application.

---

## 🌟 Architecture Overview

This web service fulfills **two critical roles** in the Social Media Downloader ecosystem:

```
                            ┌───────────────────────────────────────────────┐
                            │           User Device (Android App)          │
                            └───────┬───────────────────────────────┬───────┘
                                    │                               │
             1. On App Launch       │                               │ 2. Cloud Fallback
       (Checks for parser updates)  │                               │    (If on-device fails)
                                    ▼                               ▼
                      ┌───────────────────────────┐   ┌───────────────────────────┐
                      │ GET /api/extractors/latest│   │    GET /download?url=...  │
                      │                           │   │    POST /extract          │
                      │  Serves extract_video.py  │   │  Runs yt-dlp on Server    │
                      └───────────────────────────┘   └───────────────────────────┘
```

1. **Dynamic Extractor Host (`/api/extractors/latest`)**:
   Serves the latest `extract_video.py` parser script to Android clients. This allows updating website extractors (YouTube, Instagram, PornHub, Facebook, TikTok) in **under 2 minutes** without requiring an APK rebuild or Google Play Store update.
2. **Cloud Extraction API (`/download` & `/extract`)**:
   Provides backend fallback extraction powered by `FastAPI` + `yt-dlp` for clients when local device extraction is unavailable or fails.

---

## 🚀 Why This Architecture? (Residential IP vs. Datacenter IP)

| Problem | Server-Only Extraction | Static APK Extractor | **Dynamic Hybrid (This Architecture)** |
|---|---|---|---|
| **Bot Blocking & Captchas** | ❌ Blocked by Cloudflare/Instagram/YouTube on datacenter IPs | ✅ Runs on device's mobile IP | ✅ **Runs on device's mobile IP** |
| **Speed to Fix Broken Sites** | ✅ Fast (Update server) | ❌ Slow (2–3 day Google Play review) | ✅ **Fast (Push to server repo)** |
| **Offline / Resiliency** | ❌ Fails when server down | ✅ Fails safe to local code | ✅ **Fails safe to bundled APK script** |
| **Google Play Compliance** | ✅ Compliant | ✅ Compliant | ✅ **Compliant** |

By serving the Python script dynamically, the app downloads the latest parser from this server and executes it inside Chaquopy on the user's phone, utilizing their real residential mobile IP (bypassing cloud IP bans).

---

## 🛠️ How to Update Website Extractors (SOP)

When a social media platform updates its website or API:

### 1. Edit the Extractor
Modify [`extract_video.py`](extract_video.py) in this repository with updated regex, player configurations, or API signatures.

### 2. Bump the Version Number
In [`main.py`](main.py), increment `EXTRACTOR_VERSION`:
```python
# Increment version (e.g., from 2 to 3)
EXTRACTOR_VERSION = int(os.getenv("EXTRACTOR_VERSION", "3"))
```

### 3. Verify Syntax Locally
```bash
python3 -m py_compile extract_video.py
python3 -m py_compile main.py
```

### 4. Commit and Push
```bash
git add extract_video.py main.py
git commit -m "Fix extractor for <Platform> and bump version to 3"
git push origin main
```

### 5. Render Auto-Deployment
Render automatically deploys the updated code within 1–2 minutes.
Verify the endpoint:
```bash
curl -s "https://social-media-video-downloader-2va3.onrender.com/api/extractors/latest" | head -c 200
```
**Done!** Android clients will automatically download and activate the new extractor on their next launch.

---

## 📡 API Endpoints

### 1. Dynamic Extractor Script
- **Endpoint**: `GET /api/extractors/latest`
- **Description**: Returns the latest `extract_video.py` script and its version for Android apps.
- **Response**:
```json
{
  "version": 2,
  "filename": "extract_video.py",
  "script": "import yt_dlp\n...",
  "timestamp": 1727122390
}
```

### 2. Media Extraction (Normalized v2)
- **Endpoint**: `POST /extract`
- **Body**:
```json
{
  "url": "https://www.instagram.com/reel/...",
  "cookies": "optional_cookies"
}
```

### 3. Media Extraction (Legacy v1 Backward-Compat)
- **Endpoint**: `GET /download?url=<URL>`
- **Header**: `X-User-Cookie: <cookies>` (optional)

### 4. Health Check
- **Endpoint**: `GET /health`
- **Response**: `{"status": "ok"}`

---

## 📦 Local Installation & Development

### Requirements
- Python 3.11+
- [uv](https://github.com/astral-sh/uv) (recommended) or standard `pip`

### Setup
```bash
# Clone
git clone https://github.com/amit8899/social-media-downloader-web-service.git
cd social-media-downloader-web-service

# Create virtual environment & install dependencies
uv venv
source .venv/bin/activate   # On Windows: .venv\Scripts\activate
uv pip install -r requirements.txt

# Start local server
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

---

## ⚠️ Disclaimer
This service is intended for **personal and educational use**. Downloading copyrighted media without the copyright holder's authorization may violate terms of service and applicable laws. Use responsibly.
