# SmartFrame

*A self-hosted photo & video frame server for repurposed Lenovo Smart Frames (or any Fully Kiosk Browser display)*

![Docker](https://img.shields.io/badge/Docker-ready-blue)
![Python](https://img.shields.io/badge/Python-3.12-green)
![License](https://img.shields.io/badge/License-MIT-yellow)

---

## Features

- **Collage layouts**: Random grid-based collages (single full-screen, two stacked, big + small tiles) with smooth cross-fades
- **Smart object-fit**: Images use full-bleed `cover` in multi-tile layouts and fall back to `contain` (letterbox) when cropping would be too severe
- **Video support**: Plays MP4 clips inline, with configurable max file size
- **HEIC/HEIF support**: Processes Apple HEIC photos automatically via pillow-heif
- **Image caching**: EXIF-corrected, resized to 1080x1920, compressed JPEG cache for instant loads
- **Weather overlay**: Live weather from Open-Meteo with configurable location and temperature offset
- **Home Assistant integration**: Per-frame motion/display entities (dim & pause when "off") and up to 3 status lines per frame
- **Browser-based upload**: LAN upload page optimised for mobile; supports ZIP archives with automatic extraction and SHA-256 deduplication
- **Multi-frame**: Run multiple frames from one container or separate containers on different ports
- **Reconnect resilience**: Overlay badge on server outage; soft refresh of the media list on recovery, no page reload
- **Health endpoint**: `GET /health` for Docker health checks and monitoring
- **Optional upload auth**: HTTP Basic auth on the upload page (off by default)

## Requirements

- **Docker** (with Docker Compose)
- **Fully Kiosk Browser** (on the Lenovo Smart Frame or any Android/tablet display)
- **Home Assistant** (optional, for motion control and status lines)

## Quick Start

```bash
# 1. Clone the repository
git clone https://github.com/derekcentrico/smartframe-for-connected-screens.git
cd smartframe-for-connected-screens

# 2. Create your environment file
cp .env.example .env
# Edit .env with your real values (HA token, coordinates, media path, etc.)

# 3. Build and start
docker compose up -d

# 4. Point Fully Kiosk Browser to:
#    http://<host-ip>:8383/frame/<frame-name>
#    e.g. http://<host-ip>:8383/frame/upstairs
```

## Frame Configuration

SmartFrame uses an environment variable naming convention to configure each frame. For a frame named `upstairs` (accessed at `/frame/upstairs`), the prefix is `UPSTAIRS`:

| Variable | Purpose |
|---|---|
| `HA_UPSTAIRS_MOTION_ENTITY` | HA entity controlling dimming and slideshow pause |
| `HA_UPSTAIRS_STATUS_ENTITY_1` | First status line entity (e.g. temperature sensor) |
| `HA_UPSTAIRS_STATUS_LABEL_1` | Label for the first status line |
| `HA_UPSTAIRS_STATUS_ENTITY_2` | Second status line entity |
| `HA_UPSTAIRS_STATUS_LABEL_2` | Label for the second status line |
| `HA_UPSTAIRS_STATUS_ENTITY_3` | Third status line entity |
| `HA_UPSTAIRS_STATUS_LABEL_3` | Label for the third status line |

Repeat the pattern for any frame name: `BASEMENT`, `KITCHEN`, `OFFICE`, etc.

If a per-frame motion entity is not set, the app falls back to a global `HA_MOTION_ENTITY` variable.

## Upload

Access the upload page at `http://<host-ip>:8383/upload` or target a specific frame at `/upload/<frame-name>`.

- **Supported formats**: JPG, JPEG, PNG, MP4, HEIC, HEIF
- **ZIP archives**: Upload a `.zip` containing supported files; they are extracted automatically (Mac metadata like `__MACOSX/` and `.DS_Store` are skipped)
- **Deduplication**: Files with the same SHA-256 hash as existing files are skipped
- **Optional auth**: Set `UPLOAD_AUTH_ENABLED=1` in `.env` and configure `UPLOAD_USERNAME` / `UPLOAD_PASSWORD`

Uploaded images are pre-cached in a background thread so they display immediately on the next slideshow cycle.

## Home Assistant Integration

1. Create a long-lived access token in Home Assistant (Profile > Long-Lived Access Tokens)
2. Set `HA_URL` and `HA_TOKEN` in your `.env`
3. Configure per-frame motion and status entities (see Frame Configuration above)

**Motion control**: When the motion entity is `off` or `unavailable`, the frame dims to black and pauses the slideshow. When it returns to `on`, the frame resumes.

**Status lines**: Up to 3 status lines per frame, showing any HA sensor value (temperature, energy, occupancy, etc.). If a label is not set, the entity's `friendly_name` is used.

## Multi-Frame Setup

Run multiple frames from a single container by accessing different `/frame/<name>` URLs, or run separate containers on different ports:

```yaml
# In docker-compose.yml
services:
  smartframe_upstairs:
    build: .
    ports:
      - "8383:8383"
    env_file: .env
    # ... upstairs-specific HA entities in environment:

  smartframe_basement:
    build: .
    ports:
      - "8384:8383"
    env_file: .env
    # ... basement-specific HA entities in environment:
```

Each frame reads media from `MEDIA_BASE/<frame-name>/` (e.g. `/media/smartframes/upstairs/`).

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `MEDIA_BASE` | `/media/smartframes` | Root folder containing per-frame subdirectories |
| `CACHE_BASE` | `/cache` | Root folder for resized image cache |
| `SLIDE_INTERVAL` | `25` | Seconds between collage changes |
| `WEATHER_LAT` | *(none)* | Latitude for weather lookup |
| `WEATHER_LON` | *(none)* | Longitude for weather lookup |
| `WEATHER_TZ` | `UTC` | Timezone for weather API |
| `WEATHER_OFFSET_C` | `0.0` | Celsius offset applied to reported temperature |
| `HA_URL` | *(none)* | Home Assistant base URL |
| `HA_TOKEN` | *(none)* | Home Assistant long-lived access token |
| `MAX_VIDEO_MB` | `12` | Maximum MP4 file size in MB (larger files are skipped) |
| `ENABLE_VIDEOS` | `1` | Set to `0` to exclude all MP4 files |
| `ENABLE_HEIC` | `1` | Set to `0` to disable HEIC/HEIF support |
| `MAX_UPLOAD_MB` | `200` | Maximum upload size in MB |
| `UPLOAD_AUTH_ENABLED` | `0` | Set to `1` to require HTTP Basic auth on upload |
| `UPLOAD_USERNAME` | *(none)* | Upload auth username (required when auth is on) |
| `UPLOAD_PASSWORD` | *(none)* | Upload auth password (required when auth is on) |

## Architecture

```
app/
├── __init__.py     # Flask application factory (create_app)
├── config.py       # All environment variable configuration
├── weather.py      # Open-Meteo weather fetching & 10-min cache
├── media.py        # Media listing, image caching/resizing, HEIC support
├── ha.py           # Home Assistant helpers (motion, status, parallel fetch)
├── upload.py       # Upload blueprint (dedup, ZIP, optional auth)
├── routes.py       # Main routes blueprint (frame, img, media, motion, etc.)
└── templates/
    ├── frame.html  # Slideshow display page
    └── upload.html # Upload form page
```

## Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/my-feature`)
3. Commit your changes (`git commit -am 'Add my feature'`)
4. Push to the branch (`git push origin feature/my-feature`)
5. Open a Pull Request

## License

MIT. See [LICENSE](LICENSE) for details.
