# YT Downloader

A YouTube search and download web app with REST API backend.

## Features
- YouTube video search (youtubesearchpython)
- yt-dlp powered download info extraction
- Auto-detects all available formats from yt-dlp
- Hardcoded YouTube cookies for authenticated access
- REST API endpoints for bots/automation

## API Endpoints

### Video Info + All Formats
`GET /download/video/<youtube_url>`

### Best Audio Format
`GET /download/audio/<youtube_url>`

### Search
`GET /search?q=<query>`

## Stack
- Python Flask backend
- yt-dlp for format extraction
- youtubesearchpython for search
- Vanilla JS + HTML frontend

## Deployment
- Render: uses `render.yaml` and `Procfile`
- Start: `gunicorn app:app --bind 0.0.0.0:$PORT --timeout 120 --workers 2`

## User Preferences
- Cookies hardcoded in cookies.txt (Netscape format)
- No environment variables for secrets
- JSON responses from all API endpoints
- Auto format detection (no hardcoded formats)
