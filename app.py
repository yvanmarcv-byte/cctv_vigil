import logging
import os
import re
import secrets
import socket
import threading
import time as _time

from datetime import datetime, timedelta, timezone
from functools import wraps
from urllib.parse import urlparse

import cloudinary
import cloudinary.uploader
import cv2
from dotenv import load_dotenv
from flask import Flask, abort, jsonify, redirect, render_template, request, url_for, Response
from flask_login import LoginManager, UserMixin, current_user, login_required, login_user, logout_user
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash
from flask_wtf import CSRFProtect

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("cctv_vigil")

BAN_DURATION = timedelta(minutes=30)
ALLOWED_RECORDING_FILENAME = re.compile(r"^recording-\d{10,}\.webm$")

db = SQLAlchemy()
login_manager = LoginManager()
login_manager.login_view = "login"


def create_app():
    app = Flask(__name__)
    secret_key = os.getenv("SECRET_KEY")
    if not secret_key or secret_key == "dev-only-change-me":
        secret_file = os.path.join(app.root_path, ".secret_key")
        if os.path.exists(secret_file):
            with open(secret_file, "r") as f:
                secret_key = f.read().strip()
        else:
            secret_key = secrets.token_hex(32)
            with open(secret_file, "w") as f:
                f.write(secret_key)
    app.config["SECRET_KEY"] = secret_key
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = os.getenv("FLASK_DEBUG", "false").lower() != "true"
    database_url = os.getenv("DATABASE_URL")
    if database_url and database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql+psycopg://", 1)
    elif database_url and database_url.startswith("postgresql://"):
        database_url = database_url.replace("postgresql://", "postgresql+psycopg://", 1)
    app.config["SQLALCHEMY_DATABASE_URI"] = database_url or "sqlite:///cctv.sqlite3"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["MAX_CONTENT_LENGTH"] = 250 * 1024 * 1024

    db.init_app(app)
    login_manager.init_app(app)

    csrf = CSRFProtect()
    csrf.init_app(app)

    migrate = Migrate(app, db)

    cloudinary.config(
        cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
        api_key=os.getenv("CLOUDINARY_API_KEY"),
        api_secret=os.getenv("CLOUDINARY_API_SECRET"),
        secure=True,
    )

    with app.app_context():
        db.create_all()
        seed_defaults()

    register_routes(app)
    return app


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="operator")
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    logs = db.relationship("AccessLog", backref="user", lazy=True)
    recordings = db.relationship("Recording", backref="user", lazy=True)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def is_admin(self):
        return self.role == "admin"


class AccessLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    username = db.Column(db.String(80), nullable=False, default="anonymous")
    action = db.Column(db.String(120), nullable=False)
    ip_address = db.Column(db.String(80), nullable=False)
    timestamp = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    detail = db.Column(db.String(255), nullable=True)


class LoginAttempt(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    ip_address = db.Column(db.String(80), unique=True, nullable=False, index=True)
    failed_count = db.Column(db.Integer, nullable=False, default=0)
    banned = db.Column(db.Boolean, nullable=False, default=False)
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


class Recording(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(160), nullable=False)
    source_signature = db.Column(db.String(120), nullable=False)
    file_url = db.Column(db.String(500), nullable=False)
    cloudinary_public_id = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)


class Device(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    host = db.Column(db.String(120), nullable=False)
    device_type = db.Column(db.String(60), nullable=False, default="camera")
    last_status = db.Column(db.String(30), nullable=False, default="unknown")
    last_checked = db.Column(db.DateTime, nullable=True)


class IPCamera(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    rtsp_url = db.Column(db.String(500), nullable=False)
    location = db.Column(db.String(200), nullable=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    last_status = db.Column(db.String(30), nullable=False, default="unknown")
    last_checked = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


def seed_defaults():
    if User.query.count() == 0:
        admin_user = os.getenv("ADMIN_USERNAME", "").strip()
        admin_pass = os.getenv("ADMIN_PASSWORD", "").strip()
        if admin_user and admin_pass:
            db.session.add(User(username=admin_user, password_hash=generate_password_hash(admin_pass), role="admin"))
        else:
            setup_token = secrets.token_urlsafe(32)
            db.session.add(User(username="setup", password_hash=generate_password_hash(setup_token), role="admin"))
            print(f"\n{'='*60}")
            print(f"SETUP MODE: No ADMIN_USERNAME/ADMIN_PASSWORD set.")
            print(f"Use username 'setup' with token: {setup_token}")
            print(f"Change this password immediately after first login!")
            print(f"{'='*60}\n")
    if Device.query.count() == 0:
        hosts = [host.strip() for host in os.getenv("DEVICE_HOSTS", "").split(",") if host.strip()]
        for index, host in enumerate(hosts or ["127.0.0.1", "192.168.1.1", "192.168.1.20"], start=1):
            db.session.add(Device(name=f"Network Device {index}", host=host, device_type="camera"))
    db.session.commit()


def client_ip():
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.headers.get("X-Real-IP") or request.remote_addr or "unknown"


def log_action(action, username=None, detail=None):
    user = current_user if current_user.is_authenticated else None
    db.session.add(
        AccessLog(
            user_id=user.id if user else None,
            username=username or (user.username if user else "anonymous"),
            action=action,
            ip_address=client_ip(),
            detail=detail,
        )
    )
    db.session.commit()


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for("login"))
        if not current_user.is_admin:
            log_action("blocked_admin_page", detail=request.path)
            abort(403)
        return view(*args, **kwargs)

    return wrapped


def check_host(host, port=80, timeout=0.35):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return "online"
    except OSError:
        if host in {"127.0.0.1", "localhost"}:
            return "online"
        return "offline"


def check_camera_rtsp(rtsp_url, timeout=5):
    try:
        cap = cv2.VideoCapture(rtsp_url)
        cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, timeout * 1000)
        ret, frame = cap.read()
        cap.release()
        return "online" if ret else "offline"
    except Exception:
        return "offline"


def generate_camera_frames(rtsp_url):
    cap = cv2.VideoCapture(rtsp_url)
    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                break
            _, buffer = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
            frame_bytes = buffer.tobytes()
            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n"
                + frame_bytes
                + b"\r\n"
            )
            _time.sleep(0.03)
    finally:
        cap.release()


COMMON_RTSP_PORTS = [554, 8554, 80, 8080]
COMMON_RTSP_PATHS = [
    "/stream1",
    "/Streaming/Channels/101",
    "/Streaming/Channels/1",
    "/cam/realmonitor?channel=1&subtype=0",
    "/axis-media/media.amp",
    "/live",
    "/live/0",
    "/h264Preview_01_main",
    "/cgi-bin/stream.cgi",
]
COMMON_CREDENTIALS = [
    ("admin", "admin"),
    ("admin", "password"),
    ("admin", "12345"),
    ("admin", "123456"),
    ("admin", "admin123"),
    ("admin", ""),
    ("root", "root"),
    ("root", "pass"),
]


def scan_network_for_cameras(subnet=None):
    if not subnet:
        try:
            hostname = socket.gethostname()
            ip = socket.gethostbyname(hostname)
            subnet = ".".join(ip.split(".")[:-1])
        except Exception:
            subnet = "192.168.1"

    found_cameras = []

    def check_ip(ip):
        try:
            sock = socket.create_connection((ip, 554), timeout=0.5)
            sock.close()
            for user, pwd in COMMON_CREDENTIALS:
                for path in COMMON_RTSP_PATHS:
                    rtsp_url = f"rtsp://{user}:{pwd}@{ip}:554{path}"
                    if check_camera_rtsp(rtsp_url, timeout=3) == "online":
                        return {"ip": ip, "rtsp_url": rtsp_url, "user": user, "pwd": pwd}
            return {"ip": ip, "rtsp_url": None}
        except (socket.timeout, OSError):
            return None

    threads = []
    results = []

    for i in range(1, 255):
        ip = f"{subnet}.{i}"

        def worker(ip=ip, results=results):
            result = check_ip(ip)
            if result:
                results.append(result)

        t = threading.Thread(target=worker)
        t.daemon = True
        threads.append(t)
        t.start()

    for t in threads:
        t.join(timeout=10)

    return results


def recording_filename_from_url(file_url):
    filename = os.path.basename(urlparse(file_url).path)
    if not ALLOWED_RECORDING_FILENAME.match(filename):
        return None
    return filename


def sync_local_recordings(app):
    recordings_dir = os.path.join(app.root_path, "recordings")
    if not os.path.isdir(recordings_dir):
        return

    existing_files = {
        recording_filename_from_url(recording.file_url)
        for recording in Recording.query.all()
        if recording.file_url
    }

    changed = False
    for filename in sorted(os.listdir(recordings_dir)):
        if not filename.lower().endswith(".webm") or filename in existing_files:
            continue

        path = os.path.join(recordings_dir, filename)
        created_at = datetime.fromtimestamp(os.path.getmtime(path), timezone.utc)
        db.session.add(
            Recording(
                title=f"Recovered Recording {created_at.strftime('%Y-%m-%d %H:%M:%S')}",
                source_signature="local-browser-cache",
                file_url=url_for("recording_file", filename=filename, _external=True),
                cloudinary_public_id=None,
                created_at=created_at,
                user_id=current_user.id if current_user.is_authenticated else None,
            )
        )
        changed = True

    if changed:
        db.session.commit()


def register_routes(app):
    @app.after_request
    def set_security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob: https://res.cloudinary.com"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        return response

    @app.errorhandler(403)
    def forbidden(_):
        return render_template("403.html"), 403

    @app.errorhandler(404)
    def not_found(_):
        return render_template("404.html"), 404

    @app.errorhandler(500)
    def server_error(_):
        return render_template("500.html"), 500

    @app.route("/", methods=["GET", "POST"])
    def login():

        ip = client_ip()

        attempt = LoginAttempt.query.filter_by(ip_address=ip).first()

        if attempt and attempt.banned:
            if attempt.updated_at and (datetime.now(timezone.utc) - attempt.updated_at.replace(tzinfo=timezone.utc)) > BAN_DURATION:
                attempt.banned = False
                attempt.failed_count = 0
                db.session.commit()
            else:
                return render_template(
                    "login.html",
                    error="This IP address is temporarily banned. Try again later."
                ), 403

        if request.method == "POST":

            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")

            user = User.query.filter_by(username=username).first()

            if user and user.check_password(password):

                if attempt:
                    attempt.failed_count = 0
                    attempt.banned = False
                    attempt.updated_at = datetime.now(timezone.utc)

                login_user(user)

                db.session.commit()

                log_action("login_success", username=user.username)

                return redirect(url_for("dashboard"))

            if not attempt:
                attempt = LoginAttempt(ip_address=ip)
                db.session.add(attempt)

            attempt.failed_count = (attempt.failed_count or 0) + 1
            attempt.banned = attempt.failed_count >= 10
            attempt.updated_at = datetime.now(timezone.utc)

            remaining_attempts = max(0, 10 - attempt.failed_count)

            db.session.commit()

            log_action(
                "login_failed",
                username=username or "anonymous",
                detail=f"attempts={attempt.failed_count}"
            )

            if attempt.banned:
                return render_template(
                    "login.html",
                    error="Too many incorrect attempts. This IP address is now banned."
                ), 403

            return render_template(
                "login.html",
                error=f"Invalid username or password. {remaining_attempts} attempt(s) remaining before IP ban."
            )

        return render_template("login.html")

    @app.route("/logout", methods=["POST"])
    @login_required
    def logout():
        log_action("logout")
        logout_user()
        return redirect(url_for("login"))

    @app.route("/dashboard")
    @login_required
    def dashboard():
        return render_template(
            "dashboard.html",
            recent_logs=AccessLog.query.order_by(AccessLog.timestamp.desc()).limit(8).all(),
            camera_count=IPCamera.query.count(),
            db_status="connected",
            server_status="online",
        )

    @app.route("/camera")
    @login_required
    def camera():
        return render_template("camera_monitoring.html")

    @app.route("/recordings")
    @login_required
    def recordings():
        sync_local_recordings(app)
        items = Recording.query.order_by(Recording.created_at.desc()).all()
        return render_template("recordings_gallery.html", recordings=items)

    @app.route("/logs")
    @login_required
    def logs():
        entries = AccessLog.query.order_by(AccessLog.timestamp.desc()).limit(200).all()
        return render_template("login_logs.html", logs=entries)

    @app.route("/devices")
    @admin_required
    def devices():
        return render_template("device_management.html", devices=Device.query.order_by(Device.name.asc()).all())

    @app.route("/api/status")
    @login_required
    def api_status():
        return jsonify(
            {
                "server": "online",
                "database": "connected",
                "cameras": IPCamera.query.count(),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )

    @app.route("/api/events")
    @login_required
    def api_events():
        logs = AccessLog.query.order_by(AccessLog.timestamp.desc()).limit(12).all()
        return jsonify(
            [
                {
                    "action": log.action,
                    "username": log.username,
                    "ip": log.ip_address,
                    "timestamp": log.timestamp.isoformat(),
                    "detail": log.detail,
                }
                for log in logs
            ]
        )

    @app.route("/api/recording/start", methods=["POST"])
    @login_required
    def api_recording_start():
        source = request.json.get("source", "browser-webcam") if request.is_json else "browser-webcam"
        log_action("recording_initialized", detail=source)
        return jsonify({"ok": True})

    @app.route("/api/recordings", methods=["POST"])
    @login_required
    def api_recordings():
        file = request.files.get("video")
        if not file:
            return jsonify({"error": "No video file supplied."}), 400

        allowed_types = {"video/webm", "video/mp4", "video/ogg", "application/octet-stream"}
        if file.content_type and file.content_type not in allowed_types:
            return jsonify({"error": "Invalid file type. Only video files (webm, mp4) are allowed."}), 400

        source = request.form.get("source_signature", "browser-webcam")
        title = request.form.get("title") or f"Recording {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"

        if os.getenv("CLOUDINARY_CLOUD_NAME"):
            upload = cloudinary.uploader.upload_large(file, resource_type="video", folder="cctv-recordings")
            file_url = upload["secure_url"]
            public_id = upload["public_id"]
        else:
            recordings_dir = os.path.join(app.root_path, "recordings")
            os.makedirs(recordings_dir, exist_ok=True)
            filename = f"recording-{int(datetime.now().timestamp())}.webm"
            path = os.path.join(recordings_dir, filename)
            file.save(path)
            file_url = url_for("recording_file", filename=filename, _external=True)
            public_id = None

        recording = Recording(
            title=title,
            source_signature=source,
            file_url=file_url,
            cloudinary_public_id=public_id,
            user_id=current_user.id,
        )
        db.session.add(recording)
        db.session.commit()
        log_action("recording_uploaded", detail=source)
        return jsonify({"ok": True, "id": recording.id})

    @app.route("/recordings/file/<filename>")
    @login_required
    def recording_file(filename):
        from flask import send_from_directory

        if not ALLOWED_RECORDING_FILENAME.match(filename):
            abort(404)
        return send_from_directory(os.path.join(app.root_path, "recordings"), filename)

    @app.route("/recordings/<int:recording_id>/delete", methods=["POST"])
    @admin_required
    def delete_recording(recording_id):
        recording = db.session.get(Recording, recording_id)
        if not recording:
            abort(404)
        title = recording.title
        if recording.cloudinary_public_id:
            cloudinary.uploader.destroy(recording.cloudinary_public_id, resource_type="video", invalidate=True)
        else:
            filename = recording_filename_from_url(recording.file_url)
            if filename:
                local_path = os.path.join(app.root_path, "recordings", filename)
                if os.path.exists(local_path):
                    os.remove(local_path)
        db.session.delete(recording)
        db.session.commit()
        log_action("recording_permanent_delete", detail=title)
        return redirect(url_for("recordings"))

    @app.route("/devices/poll", methods=["POST"])
    @admin_required
    def poll_devices():
        for device in Device.query.all():
            device.last_status = check_host(device.host)
            device.last_checked = datetime.now(timezone.utc)
        db.session.commit()
        log_action("device_poll_completed")
        return redirect(url_for("devices"))

    @app.route("/ip-cameras")
    @login_required
    def ip_cameras():
        cameras = IPCamera.query.order_by(IPCamera.created_at.desc()).all()
        return render_template("ip_cameras.html", cameras=cameras)

    @app.route("/ip-cameras/add", methods=["GET", "POST"])
    @login_required
    def add_ip_camera():
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            rtsp_url = request.form.get("rtsp_url", "").strip()
            location = request.form.get("location", "").strip()

            if not name or not rtsp_url:
                return render_template("add_ip_camera.html", error="Name and RTSP URL are required.")

            last_status = check_camera_rtsp(rtsp_url)

            camera = IPCamera(
                name=name,
                rtsp_url=rtsp_url,
                location=location or None,
                last_status=last_status,
                last_checked=datetime.now(timezone.utc),
                user_id=current_user.id,
            )
            db.session.add(camera)
            db.session.commit()
            log_action("ip_camera_added", detail=name)
            return redirect(url_for("ip_cameras"))

        return render_template("add_ip_camera.html")

    @app.route("/ip-cameras/<int:camera_id>/delete", methods=["POST"])
    @admin_required
    def delete_ip_camera(camera_id):
        camera = db.session.get(IPCamera, camera_id)
        if not camera:
            abort(404)
        name = camera.name
        db.session.delete(camera)
        db.session.commit()
        log_action("ip_camera_deleted", detail=name)
        return redirect(url_for("ip_cameras"))

    @app.route("/ip-cameras/<int:camera_id>/stream")
    @login_required
    def camera_stream(camera_id):
        camera = db.session.get(IPCamera, camera_id)
        if not camera:
            abort(404)
        return Response(
            generate_camera_frames(camera.rtsp_url),
            mimetype="multipart/x-mixed-replace; boundary=frame",
        )

    @app.route("/ip-cameras/poll", methods=["POST"])
    @admin_required
    def poll_ip_cameras():
        for camera in IPCamera.query.all():
            camera.last_status = check_camera_rtsp(camera.rtsp_url)
            camera.last_checked = datetime.now(timezone.utc)
        db.session.commit()
        log_action("ip_camera_poll_completed")
        return redirect(url_for("ip_cameras"))

    @app.route("/ip-cameras/scan", methods=["GET", "POST"])
    @admin_required
    def scan_cameras():
        if request.method == "POST":
            subnet = request.form.get("subnet", "").strip() or None
            log_action("network_scan_started", detail=subnet or "auto")
            cameras = scan_network_for_cameras(subnet)
            log_action("network_scan_completed", detail=f"found={len(cameras)}")
            return render_template("scan_results.html", cameras=cameras, subnet=subnet)
        return render_template("scan_cameras.html")

    @app.route("/users")
    @admin_required
    def users():
        all_users = User.query.order_by(User.created_at.desc()).all()
        return render_template("users.html", users=all_users)

    @app.route("/users/add", methods=["GET", "POST"])
    @admin_required
    def add_user():
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            role = request.form.get("role", "operator").strip()

            if not username or not password:
                return render_template("add_user.html", error="Username and password are required.")

            if len(password) < 8:
                return render_template("add_user.html", error="Password must be at least 8 characters.")

            if User.query.filter_by(username=username).first():
                return render_template("add_user.html", error="Username already exists.")

            if role not in ("admin", "operator"):
                role = "operator"

            user = User(
                username=username,
                password_hash=generate_password_hash(password),
                role=role,
            )
            db.session.add(user)
            db.session.commit()
            log_action("user_created", detail=username)
            return redirect(url_for("users"))

        return render_template("add_user.html")

    @app.route("/users/<int:user_id>/edit", methods=["GET", "POST"])
    @admin_required
    def edit_user(user_id):
        user = db.session.get(User, user_id)
        if not user:
            abort(404)

        if request.method == "POST":
            new_role = request.form.get("role", user.role).strip()
            new_password = request.form.get("password", "").strip()

            if new_role in ("admin", "operator"):
                user.role = new_role

            if new_password:
                if len(new_password) < 8:
                    return render_template("edit_user.html", user=user, error="Password must be at least 8 characters.")
                user.password_hash = generate_password_hash(new_password)

            db.session.commit()
            log_action("user_updated", detail=user.username)
            return redirect(url_for("users"))

        return render_template("edit_user.html", user=user)

    @app.route("/users/<int:user_id>/delete", methods=["POST"])
    @admin_required
    def delete_user(user_id):
        user = db.session.get(User, user_id)
        if not user:
            abort(404)
        if user.id == current_user.id:
            return redirect(url_for("users"))
        username = user.username
        db.session.delete(user)
        db.session.commit()
        log_action("user_deleted", detail=username)
        return redirect(url_for("users"))

    @app.route("/change-password", methods=["GET", "POST"])
    @login_required
    def change_password():
        if request.method == "POST":
            current_password = request.form.get("current_password", "")
            new_password = request.form.get("new_password", "")
            confirm_password = request.form.get("confirm_password", "")

            if not current_user.check_password(current_password):
                return render_template("change_password.html", error="Current password is incorrect.")

            if len(new_password) < 8:
                return render_template("change_password.html", error="New password must be at least 8 characters.")

            if new_password != confirm_password:
                return render_template("change_password.html", error="New passwords do not match.")

            current_user.password_hash = generate_password_hash(new_password)
            db.session.commit()
            log_action("password_changed")
            return redirect(url_for("dashboard"))

        return render_template("change_password.html")


app = create_app()

if __name__ == "__main__":
    app.run(debug=os.getenv("FLASK_DEBUG", "false").lower() == "true")
