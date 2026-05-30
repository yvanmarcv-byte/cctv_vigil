# CCTV Vigil

A browser-based CCTV monitoring web application with real-time motion detection, face detection, IP camera support, and comprehensive access logging.

## Features

- **Live Camera Monitoring** - Browser webcam with motion and face detection
- **IP Camera Support** - Connect physical CCTV cameras via RTSP streams
- **Network Scanner** - Auto-discover IP cameras on your network
- **Video Recording** - Record and store clips locally or on Cloudinary
- **User Management** - Admin/operator roles with full CRUD
- **Access Logging** - All activity tracked with timestamps and IP addresses
- **Device Management** - Monitor network devices (routers, switches, cameras)
- **Security** - Password hashing, CSRF protection, brute-force prevention

## Network Diagram

```
                                    ┌─────────────────┐
                                    │   Internet      │
                                    └────────┬────────┘
                                             │
                                    ┌────────┴────────┐
                                    │     Router      │
                                    │  (DHCP Server)  │
                                    └────────┬────────┘
                                             │
                                    ┌────────┴────────┐
                                    │  Network Switch │
                                    └──┬─────┬─────┬──┘
                                       │     │     │
                              ┌────────┘     │     └────────┐
                              │              │              │
                    ┌─────────┴──┐  ┌───────┴──────┐  ┌────┴─────────┐
                    │  Server PC │  │  IP Camera   │  │  IP Camera   │
                    │ (CCTV Vigil)│  │  (RTSP Feed) │  │  (RTSP Feed) │
                    │  192.168.1.2│  │ 192.168.1.10 │  │ 192.168.1.11 │
                    └────────────┘  └──────────────┘  └──────────────┘
```

## Tech Stack

| Component | Technology |
|-----------|------------|
| Backend | Python, Flask |
| Database | PostgreSQL (Render) |
| Frontend | HTML, CSS, JavaScript |
| Authentication | Flask-Login, Werkzeug |
| Camera | OpenCV, Browser MediaDevices API |
| Cloud Storage | Cloudinary |
| Deployment | Render |

## Setup Instructions

### Prerequisites
- Python 3.10+
- pip
- Git

### Local Development

1. **Clone the repository:**
   ```bash
   git clone https://github.com/YOUR_USERNAME/cctv-vigil.git
   cd cctv-vigil
   ```

2. **Create virtual environment:**
   ```bash
   python -m venv .venv
   .venv\Scripts\activate  # Windows
   source .venv/bin/activate  # Mac/Linux
   ```

3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

4. **Configure environment:**
   ```bash
   copy .env.example .env
   ```
   Edit `.env` and set:
   - `ADMIN_USERNAME` - Your admin username
   - `ADMIN_PASSWORD` - Your admin password (min 8 characters)
   - `SECRET_KEY` - Random secret key

5. **Run the application:**
   ```bash
   python app.py
   ```

6. **Open in browser:**
   ```
   http://127.0.0.1:5000
   ```

### Production Deployment (Render)

1. **Create PostgreSQL database on Render**
2. **Create Web Service connected to GitHub repo**
3. **Set environment variables:**
   - `DATABASE_URL` - Internal PostgreSQL URL from Render
   - `SECRET_KEY` - Generate in Render dashboard
   - `ADMIN_USERNAME` - Your admin username
   - `ADMIN_PASSWORD` - Your admin password
   - `FLASK_DEBUG` - `false`
4. **Deploy** - Render auto-deploys on push

## Usage

### Login
- Use credentials set in `.env` or auto-generated setup token

### Camera Monitoring
1. Go to **Camera Monitoring** tab
2. Browser webcam activates automatically
3. Motion and face detection run in real-time
4. Click **Start Recording** to capture clips

### IP Cameras
1. Go to **IP Cameras** tab
2. Click **Add Camera**
3. Enter RTSP URL: `rtsp://username:password@camera_ip:port/path`
4. Or use **Scan Network** to auto-discover cameras

### Device Management
1. Go to **Devices** tab (admin only)
2. Click **Poll Hardware Assets** to check device status
3. Devices configured via `DEVICE_HOSTS` in `.env`

## Project Structure

```
cctv-vigil/
├── app.py                  # Main application
├── requirements.txt        # Python dependencies
├── .env.example           # Environment template
├── .gitignore             # Git exclusions
├── static/
│   ├── css/style.css      # Stylesheet
│   └── js/script.js       # Frontend JavaScript
├── templates/
│   ├── base.html          # Base layout
│   ├── login.html         # Login page
│   ├── dashboard.html     # Main dashboard
│   ├── camera_monitoring.html  # Live camera view
│   ├── ip_cameras.html    # IP camera management
│   ├── recordings_gallery.html # Video recordings
│   ├── login_logs.html    # Activity logs
│   ├── device_management.html  # Network devices
│   ├── users.html         # User management
│   ├── 403.html           # Access denied
│   ├── 404.html           # Not found
│   └── 500.html           # Server error
├── recordings/            # Local video storage
└── instance/
    └── cctv.sqlite3       # Local database (dev)
```

## Security Features

- Password hashing (PBKDF2)
- CSRF protection on all forms
- Brute-force prevention (IP ban after 10 failed attempts)
- Role-based access control (admin/operator)
- Security headers (CSP, HSTS, X-Frame-Options)
- Session security (HttpOnly, SameSite, Secure)

## License

Educational project for IT 2102 - Computer Networks
