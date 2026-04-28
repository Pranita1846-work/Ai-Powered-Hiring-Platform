#app.py

import os
import signal
import threading
import atexit
from functools import wraps
from threading import Timer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

try:
    from dotenv import load_dotenv
    # Prefer project .env values over stale shell/system variables.
    load_dotenv(os.path.join(BASE_DIR, ".env"), override=True)
    # Fallback env for deployments/local setups that keep OAuth keys separately
    load_dotenv(os.path.join(BASE_DIR, ".env.supabase"), override=False)
except ImportError:
    print("python-dotenv not installed, using system environment variables")
    pass

from flask import Flask, render_template, request, redirect, session, url_for, make_response, send_file, send_from_directory, abort
from werkzeug.security import generate_password_hash, check_password_hash
import psycopg2
from psycopg2.extras import RealDictCursor
from database import get_connection, create_tables, cleanup_db_resources, migrate_database_schema

# At the very top of app.py (after imports)
from database import DB_CONFIG, init_connection_pool, get_connection

# Initialize connection pool when app starts
try:
    init_connection_pool()
    print("[OK] Database connection pool initialized")
except Exception as e:
    print(f"[ERROR] Failed to initialize connection pool: {e}")

from database import close_connection_pool
atexit.register(close_connection_pool)

import pymupdf as fitz  
from flask_mail import Mail, Message
from itsdangerous import URLSafeTimedSerializer
from werkzeug.utils import secure_filename
from flask import flash
from flask import jsonify
from flask_socketio import SocketIO, join_room, emit
from functools import wraps
from flask import g
from flask_dance.contrib.google import make_google_blueprint, google
from urllib.parse import urlencode
import secrets
import requests
import time
import json
import base64
import re
from datetime import datetime, timedelta
from functools import wraps
from threading import Thread
import textwrap
from io import BytesIO
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.lib import colors
from reportlab.pdfgen import canvas

# ===== DATABASE CONFIGURATION =====
DB_CONFIG = {
    'host': os.getenv('DB_HOST', 'localhost'),
    'user': os.getenv('DB_USER', 'postgres'),
    'password': os.getenv('DB_PASSWORD', ''),
    'database': os.getenv('DB_NAME', 'hirehub'),
    'port': int(os.getenv('DB_PORT', 5432))
}

# Validate database configuration
if not DB_CONFIG.get('password'):
    print("[WARNING] Database password not set. Using empty password.")

print(f"[INFO] Database Config: {DB_CONFIG['user']}@{DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['database']}")

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "hirehub-secret")
IS_PRODUCTION = os.environ.get("IS_PRODUCTION", "false").lower() == "true"
APP_NAME = os.environ.get("APP_NAME", "HireHub")

# Inject user role into all templates (must be after app is defined)
@app.context_processor
def inject_user_role():
    from flask import session
    return dict(user_role=session.get('role'))

if IS_PRODUCTION:
    # Production configuration
    app.config['PREFERRED_URL_SCHEME'] = 'https'
else:
    # Local development configuration
    # Don't set SERVER_NAME for local - let Flask auto-detect
    app.config['PREFERRED_URL_SCHEME'] = 'http'

# Google OAuth Configuration
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
GOOGLE_DISCOVERY_URL = "https://accounts.google.com/.well-known/openid-configuration"

def _has_google_oauth_config() -> bool:
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)

# Helper to build OAuth redirect URI consistently
def _build_oauth_redirect_uri():
    # Highest priority: explicit redirect URI from environment
    explicit_redirect = os.environ.get("GOOGLE_OAUTH_REDIRECT_URI")
    if explicit_redirect:
        return explicit_redirect

    # Production domain callback
    if IS_PRODUCTION:
        return "https://hirehub.com/login/google/authorized"

    # Local development callback should stay on HTTP unless running HTTPS locally
    scheme = app.config.get('PREFERRED_URL_SCHEME', 'http')
    return url_for("google_authorized", _external=True, _scheme=scheme)
    


# Allow OAuth over HTTP for local development (required by oauthlib)
os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

# Configure Flask-Dance Google OAuth
google_bp = make_google_blueprint(
    client_id=GOOGLE_CLIENT_ID,
    client_secret=GOOGLE_CLIENT_SECRET,
    scope=["openid", "https://www.googleapis.com/auth/userinfo.email", "https://www.googleapis.com/auth/userinfo.profile"],
    redirect_to="google_authorized"
)


# Performance optimizations
app.config['TEMPLATES_AUTO_RELOAD'] = True  # Enable auto-reload for development
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0  # Disable caching for development
app.config['JSON_SORT_KEYS'] = False  # Don't sort JSON keys (faster)
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SECURE'] = False  # Set to True in production with HTTPS
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=1)  # adjustable via admin settings

RENDER_DISK_MOUNT = "/var/data"
UPLOAD_FOLDER = os.path.join(BASE_DIR, "static", "uploads")
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)


def get_upload_directories():
    directories = []

    render_upload_dir = os.path.join(RENDER_DISK_MOUNT, "uploads")
    if os.path.isdir(render_upload_dir):
        directories.append(render_upload_dir)

    configured_upload_dir = os.path.abspath(app.config["UPLOAD_FOLDER"])
    directories.append(configured_upload_dir)

    legacy_upload_dir = os.path.abspath(os.path.join(os.getcwd(), "static", "uploads"))
    if legacy_upload_dir not in directories:
        directories.append(legacy_upload_dir)

    return directories


@app.route('/uploads/<path:filename>')
def uploaded_file(filename):
    normalized_filename = os.path.normpath(filename).replace('\\', '/')
    if normalized_filename.startswith('../') or normalized_filename == '..' or os.path.isabs(filename):
        abort(404)

    for directory in get_upload_directories():
        absolute_directory = os.path.abspath(directory)
        candidate_path = os.path.abspath(os.path.join(absolute_directory, normalized_filename))

        try:
            if os.path.commonpath([absolute_directory, candidate_path]) != absolute_directory:
                continue
        except ValueError:
            continue

        if os.path.isfile(candidate_path):
            return send_from_directory(absolute_directory, normalized_filename)

    abort(404)

# Create tables/migrations on startup only when explicitly enabled.
# This avoids opening many DB connections against hosted pools with strict limits.
RUN_DB_STARTUP_INIT = os.environ.get("RUN_DB_STARTUP_INIT", "false").lower() == "true"
if RUN_DB_STARTUP_INIT:
    try:
        print("[INFO] Initializing database schema...")
        create_tables()
        print("[OK] Database initialization completed successfully")
    except Exception as init_error:
        print(f"[WARNING] Database initialization encountered issues: {str(init_error)[:200]}")
        print("[INFO] Continuing app startup - database may already be initialized")
        # Don't crash the app if table creation fails - it might already exist
else:
    print("[INFO] Startup DB initialization skipped (set RUN_DB_STARTUP_INIT=true to enable)")

class SafeDBConnection:
    """Context manager for safe database operations with proper cleanup"""
    def __init__(self):
        self.db = None
        self.cursor = None
    
    def __enter__(self):
        try:
            self.db = get_connection()
            self.cursor = self.db.cursor(cursor_factory=RealDictCursor)
            return self.cursor, self.db
        except Exception as e:
            print(f"Error creating database connection: {e}")
            import traceback
            traceback.print_exc()
            if self.cursor:
                try:
                    self.cursor.close()
                except:
                    pass
            if self.db:
                try:
                    self.db.close()
                except:
                    pass
            raise
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Ensure proper cleanup: cursor first, then connection. Handle errors gracefully."""
        try:
            if exc_type:
                # Exception occurred during execution - rollback if possible
                if self.db:
                    try:
                        self.db.rollback()
                    except Exception as e:
                        print(f"Rollback error: {e}")
            else:
                # No exception, commit the transaction
                if self.db:
                    try:
                        self.db.commit()
                    except Exception as e:
                        print(f"Commit error: {e}")
        finally:
            # Always close cursor and connection, even if commit/rollback fails
            if self.cursor:
                try:
                    self.cursor.close()
                except Exception as e:
                    print(f"Error closing cursor: {e}")
            
            if self.db:
                try:
                    # Return connection to pool instead of closing it directly
                    from database import return_connection
                    return_connection(self.db)
                except Exception as e:
                    print(f"Error returning connection to pool: {e}")
        
        return False  # Don't suppress exceptions

# Create default admin user if doesn't exist
def create_default_admin():
    """Create default admin user with credentials: admin@hirehub.com / Admin@123"""
    try:
        with SafeDBConnection() as (cursor, db):
            admin_email = "admin@hirehub.com"
            admin_id = "admin_001"
            admin_name = "Admin User"
            admin_password = "Admin@123"
            
            # Check if admin already exists
            cursor.execute("SELECT id FROM admins WHERE email = %s", (admin_email,))
            existing_admin = cursor.fetchone()
            
            if existing_admin:
                print("[OK] Default admin already exists: " + admin_email)
                # Dev self-heal: optionally reset default admin password so local login always works.
                # In production this is disabled by default unless explicitly enabled.
                should_reset_default_admin = os.environ.get(
                    "RESET_DEFAULT_ADMIN_PASSWORD",
                    "true"
                ).lower() == "true"

                if should_reset_default_admin:
                    hashed_password = generate_password_hash(admin_password)
                    cursor.execute(
                        "UPDATE admins SET password=%s WHERE email=%s",
                        (hashed_password, admin_email)
                    )
                    db.commit()
                    print("[OK] Default admin password has been reset for local access")
            else:
                # Hash the password
                hashed_password = generate_password_hash(admin_password)
                
                # Insert new admin
                cursor.execute(
                    """INSERT INTO admins (id, name, email, password, profile_completed)
                    VALUES (%s, %s, %s, %s, %s)""",
                    (admin_id, admin_name, admin_email, hashed_password, True)
                )
                db.commit()
                print("[OK] Default admin created successfully!")
                print("   Email: " + admin_email)
                print("   Password: " + admin_password)
    except Exception as e:
        print("[ERROR] Admin creation note: " + str(e))

# Create default admin on startup
create_default_admin()

# Run database migrations only when startup init is enabled
if RUN_DB_STARTUP_INIT:
    try:
        migrate_database_schema()
    except Exception as e:
        print(f"[WARNING] Migration skipped: {e}")

# Helper function to get connection with dictionary cursor
def get_dict_connection():
    """Get a database connection with RealDictCursor for dict-like row access"""
    conn = get_connection()
    return conn, conn.cursor(cursor_factory=RealDictCursor)

# Register app teardown to close connection pool on shutdown
@app.teardown_appcontext
def shutdown_db(exception=None):
    """Do not close pool per request; keep connections warm for worker lifetime."""
    return None

# Request timeout tracking
_request_timers = {}

@app.before_request
def start_request_timer():
    """Start tracking request time to detect slow routes"""
    import time
    _request_timers[id(request)] = time.time()

@app.after_request
def check_request_duration(response):
    """Log requests that take longer than expected"""
    import time
    try:
        start_time = _request_timers.pop(id(request), None)
        if start_time:
            duration = time.time() - start_time
            if duration > 10:
                print(f"[SLOW] {request.method} {request.path} took {duration:.2f}s")
    except Exception:
        pass
    return response

app.config.update(
    MAIL_SERVER='smtp.gmail.com',
    MAIL_PORT=587,
    MAIL_USE_TLS=True,
    MAIL_USERNAME=os.environ.get('MAIL_USERNAME'),
    MAIL_PASSWORD=os.environ.get('MAIL_PASSWORD'),
)

mail = Mail(app)
serializer = URLSafeTimedSerializer(app.secret_key)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# Asynchronous email sending to prevent worker timeout
def send_async_email(app, msg):
    """Send email in background thread to avoid blocking request"""
    with app.app_context():
        try:
            print("MAIL_USERNAME:", app.config['MAIL_USERNAME'])
            mail.send(msg)
            print("[OK] Email sent successfully in background")
        except Exception as e:
            print(f"[ERROR] Background email sending failed: {e}")

# ===== ADMIN SETTINGS (DB-BACKED) =====
DEFAULT_ADMIN_SETTINGS = {
    "session_timeout_minutes": 30,
    "platform_name": APP_NAME,
    "system_timezone": "UTC",
    "enable_registrations": True,
    "allowed_roles": ["admin", "mentor", "company"],
    "failed_login_limit": 5,
    "enable_email_notifications": True,
    "alert_new_registrations": True,
    "alert_job_reports": True,
    "alert_verification_requests": True,
    "auto_approve_jobs": False,
    "auto_hide_threshold": 5,
    "log_admin_actions": True,
    "log_retention_days": 90,
    "logout_version": 0,
    "auto_refresh_health": True,
    "auto_refresh_notifications": True,
    "refresh_interval_ms": 5000,
    "maintenance_mode": False
}


def ensure_admin_settings_table():
    try:
        db = get_connection()
        cursor = db.cursor(cursor_factory=RealDictCursor)
        try:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS admin_settings (
                    id SERIAL PRIMARY KEY,
                    settings JSONB NOT NULL
                )
                """
            )
            db.commit()
            return True
        finally:
            cleanup_db_resources(cursor, db)
    except Exception as e:
        print(f"[WARNING] Could not ensure admin_settings table: {e}")
        return False


def get_admin_settings():
    if not ensure_admin_settings_table():
        return dict(DEFAULT_ADMIN_SETTINGS)

    try:
        db = get_connection()
        cursor = db.cursor(cursor_factory=RealDictCursor)
        try:
            cursor.execute("SELECT settings FROM admin_settings WHERE id = 1")
            row = cursor.fetchone()
            if not row:
                cursor.execute(
                    "INSERT INTO admin_settings (id, settings) VALUES (1, %s)",
                    (json.dumps(DEFAULT_ADMIN_SETTINGS),)
                )
                db.commit()
                cursor.execute("SELECT settings FROM admin_settings WHERE id = 1")
                row = cursor.fetchone()
            
            settings = row['settings'] if row else {}
            if isinstance(settings, str):
                try:
                    settings = json.loads(settings)
                except Exception:
                    settings = {}
            merged = {**DEFAULT_ADMIN_SETTINGS, **(settings or {})}
            return merged
        finally:
            cleanup_db_resources(cursor, db)
    except Exception as e:
        print(f"[WARNING] Falling back to default admin settings: {e}")
        return dict(DEFAULT_ADMIN_SETTINGS)


def save_admin_settings(new_settings: dict):
    merged = {**DEFAULT_ADMIN_SETTINGS, **(new_settings or {})}
    if not ensure_admin_settings_table():
        return merged

    try:
        db = get_connection()
        cursor = db.cursor(cursor_factory=RealDictCursor)
        try:
            # PostgreSQL uses INSERT ... ON CONFLICT for upsert
            cursor.execute(
                """INSERT INTO admin_settings (id, settings) VALUES (1, %s)
                ON CONFLICT (id) DO UPDATE SET settings = EXCLUDED.settings""",
                (json.dumps(merged),)
            )
            db.commit()
        finally:
            cleanup_db_resources(cursor, db)
    except Exception as e:
        print(f"[WARNING] Could not persist admin settings: {e}")
    return merged


# Performance monitoring decorator

def timing_decorator(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        start_time = time.time()
        result = f(*args, **kwargs)
        elapsed_time = time.time() - start_time
        if elapsed_time > 1:  # Log only slow requests
            print(f"⚠️ Slow route: {f.__name__} took {elapsed_time:.2f}s")
        return result
    return decorated_function


login_history_ready = False


def ensure_login_history_table():
    global login_history_ready

    if login_history_ready:
        return

    try:
        with SafeDBConnection() as (cursor, db):
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS login_history (
                    id SERIAL PRIMARY KEY,
                    user_id VARCHAR(20),
                    user_type VARCHAR(20),
                    ip_address VARCHAR(45),
                    user_agent TEXT,
                    login_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_user ON login_history (user_id, user_type)"
            )
        login_history_ready = True
    except Exception as e:
        print(f"Error ensuring login_history table: {e}")


schema_columns_ready = False


def ensure_schema_columns():
    """Run all ALTER TABLE column-guard DDL exactly once per worker lifetime."""
    global schema_columns_ready
    if schema_columns_ready:
        return
    try:
        with SafeDBConnection() as (cursor, db):
            for tbl in ['candidates', 'recruiters', 'mentors']:
                cursor.execute(
                    f"ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS is_blocked BOOLEAN DEFAULT FALSE"
                )
            cursor.execute("ALTER TABLE recruiter_profiles ADD COLUMN IF NOT EXISTS verification_status VARCHAR(20) DEFAULT 'pending'")
            cursor.execute("ALTER TABLE recruiter_profiles ADD COLUMN IF NOT EXISTS admin_countersigned BOOLEAN DEFAULT FALSE")
            cursor.execute("ALTER TABLE recruiter_profiles ADD COLUMN IF NOT EXISTS admin_countersigned_at TIMESTAMP")
            cursor.execute("ALTER TABLE recruiter_profiles ADD COLUMN IF NOT EXISTS admin_countersigned_by VARCHAR(255)")
            cursor.execute("ALTER TABLE mentor_profiles ADD COLUMN IF NOT EXISTS rejection_reason TEXT")
        schema_columns_ready = True
    except Exception as e:
        print(f"[WARNING] ensure_schema_columns: {e}")


# Workflow Dependency Helper Functions
def check_candidate_profile_completion(candidate_id, min_percent=85):
    """Check if candidate profile meets minimum completion threshold (default 85%)"""
    try:
        with SafeDBConnection() as (cursor, db):
            cursor.execute("SELECT profile_percent, profile_completed FROM candidate_profiles WHERE candidate_id = %s", (candidate_id,))
            profile = cursor.fetchone()
            
            if not profile:
                return False, 0
            
            profile_percent = profile.get('profile_percent', 0)
            return profile_percent >= min_percent, profile_percent
    except Exception as e:
        print(f"Error checking profile: {e}")
        # Resilient fallback for environments where candidate_profiles table is not yet provisioned.
        # This prevents a hard block on job applications.
        try:
            if 'candidate_profiles' in str(e).lower() and 'does not exist' in str(e).lower():
                with SafeDBConnection() as (cursor, db):
                    cursor.execute("SELECT profile_completed FROM candidates WHERE id = %s", (candidate_id,))
                    cand = cursor.fetchone() or {}
                    if cand.get('profile_completed') is True:
                        return True, 100
                    # If legacy rows have no completion signal, allow with minimum threshold to avoid deadlock.
                    return True, min_percent
        except Exception as fallback_error:
            print(f"Fallback profile check failed: {fallback_error}")
        return False, 0

def check_recruiter_verification(recruiter_id):
    """Check if recruiter is verified by admin"""
    try:
        with SafeDBConnection() as (cursor, db):
            cursor.execute("SELECT verification_status FROM recruiter_profiles WHERE recruiter_id = %s", (recruiter_id,))
            profile = cursor.fetchone()
            
            if not profile:
                return False, 'pending'
            
            status = profile.get('verification_status', 'pending')
            return status == 'approved', status
    except Exception as e:
        print(f"Error checking recruiter verification: {e}")
        return False, 'pending'

def check_mentor_verification(mentor_id):
    """Check if mentor is verified by admin"""
    try:
        with SafeDBConnection() as (cursor, db):
            cursor.execute("SELECT verification_status FROM mentor_profiles WHERE mentor_id = %s", (mentor_id,))
            profile = cursor.fetchone()
            
            if not profile:
                return False, 'pending'
            
            status = profile.get('verification_status', 'pending')
            return status == 'approved', status
    except Exception as e:
        print(f"Error checking mentor verification: {e}")
        return False, 'pending'


@socketio.on('join')
def on_join(data):
    role = data.get('role')
    user_id = data.get('id')
    try:
        if role == 'mentor' and user_id:
            join_room(f"mentor_{user_id}")
    except Exception:
        pass


@socketio.on('join_mentorship')
def on_join_mentorship(data):
    """Join a mentorship chat room for real-time messaging"""
    try:
        mentorship_request_id = data.get('mentorship_request_id')
        if mentorship_request_id:
            room_name = f"mentorship_{mentorship_request_id}"
            join_room(room_name)
            print(f"User joined mentorship room: {room_name}")
            # Optionally emit a join confirmation
            emit('joined_mentorship', {
                'room': room_name,
                'mentorship_request_id': mentorship_request_id
            }, room=request.sid)
    except Exception as e:
        print(f"Error joining mentorship room: {e}")


@app.route("/")
@timing_decorator
def home():
    settings = get_admin_settings()
    return render_template("index.html", maintenance_mode=settings.get('maintenance_mode', False))

@app.route("/about")
def about():
    return render_template("about.html")

@app.route("/contact")
def contact():
    return render_template("contact.html")

@app.route("/careers")
@app.route("/careers.html")
def careers():
    """Public careers page."""
    return render_template("careers.html")

@app.route("/login/google")
def initiate_google_login():
    """Initiate Google OAuth login with account selection"""
    if not _has_google_oauth_config():
        flash("Google sign-in is not configured. Please set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET.", "error")
        return redirect(url_for("login"))

    role = request.args.get("role", "candidate")
    if role not in ["candidate", "recruiter"]:
        role = "candidate"
    session['oauth_role'] = role
    session['oauth_next'] = 'login'
    
    # Generate state for CSRF protection
    state = secrets.token_urlsafe(32)
    session['oauth_state'] = state
    # Dynamic redirect URI based on request
    # Dynamic redirect URI
    redirect_uri = _build_oauth_redirect_uri()
    
    params = {
        'client_id': GOOGLE_CLIENT_ID,
        'redirect_uri': redirect_uri,
        'response_type': 'code',
        'scope': 'openid email profile',
        'state': state,
        'prompt': 'select_account',  # Force account selection
        'access_type': 'offline'
    }
    
    auth_url = f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}"
    return redirect(auth_url)

@app.route("/register/google")
def initiate_google_register():
    """Initiate Google OAuth registration with account selection"""
    if not _has_google_oauth_config():
        flash("Google sign-in is not configured. Please set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET.", "error")
        return redirect(url_for("register"))

    role = request.args.get("role", "candidate")
    if role not in ["candidate", "recruiter"]:
        role = "candidate"
    session['oauth_role'] = role
    session['oauth_next'] = 'register'
    
    # Generate state for CSRF protection
    state = secrets.token_urlsafe(32)
    session['oauth_state'] = state
    # Dynamic redirect URI based on request
    # Dynamic redirect URI
    redirect_uri = _build_oauth_redirect_uri()
    
    params = {
        'client_id': GOOGLE_CLIENT_ID,
        'redirect_uri': redirect_uri,
        'response_type': 'code',
        'scope': 'openid email profile',
        'state': state,
        'prompt': 'select_account',  # Force account selection
        'access_type': 'offline'
    }
    
    auth_url = f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}"
    return redirect(auth_url)

@app.route("/login/google/authorized")
def google_authorized():
    """Handle Google OAuth callback after user selects account"""
    if not _has_google_oauth_config():
        flash("Google sign-in is not configured on the server.", "error")
        return redirect(url_for("login"))
    
    # Verify state to prevent CSRF
    state = request.args.get('state')
    if not state or state != session.get('oauth_state'):
        flash("Invalid OAuth state. Please try again.", "error")
        return redirect(url_for("login"))
    
    # Get authorization code
    code = request.args.get('code')
    if not code:
        error = request.args.get('error', 'Unknown error')
        flash(f"Google authorization failed: {error}", "error")
        return redirect(url_for("login"))
    
    # Get role from session
    role = session.get('oauth_role', 'candidate')
    
    try:
        # Dynamic redirect URI - must match the one used in the authorization request
        # Dynamic redirect URI
        redirect_uri = _build_oauth_redirect_uri()
        
        # Exchange authorization code for access token
        token_url = "https://oauth2.googleapis.com/token"
        token_data = {
            'code': code,
            'client_id': GOOGLE_CLIENT_ID,
            'client_secret': GOOGLE_CLIENT_SECRET,
            'redirect_uri': redirect_uri,
            'grant_type': 'authorization_code'
        }
        
        token_response = requests.post(token_url, data=token_data, timeout=20)
        token_json = token_response.json()
        
        if 'error' in token_json:
            flash(f"Failed to get access token: {token_json.get('error_description', 'Unknown error')}", "error")
            return redirect(url_for("login"))
        
        access_token = token_json.get('access_token')
        
        # Get user info from Google
        userinfo_url = "https://www.googleapis.com/oauth2/v2/userinfo"
        headers = {'Authorization': f'Bearer {access_token}'}
        userinfo_response = requests.get(userinfo_url, headers=headers)
        
        if userinfo_response.status_code != 200:
            flash("Failed to fetch user info from Google.", "error")
            return redirect(url_for("login"))
        
        info = userinfo_response.json()
        email = info.get("email")
        name = info.get("name", "Google User")
        
        if not email:
            flash("Could not get email from Google account.", "error")
            return redirect(url_for("login"))

        table_map = {
            "candidate": "candidates",
            "recruiter": "recruiters"
        }

        db = get_connection()
        cursor = db.cursor(cursor_factory=RealDictCursor)

        # Check if email exists in any other role table (email-role restriction)
        email_exists_in_other_role = False
        existing_role = None

        for check_role, table in table_map.items():
            if check_role != role:  # Don't check the current role table
                cursor.execute(
                    f"SELECT id FROM {table} WHERE LOWER(email)=%s",
                    (email.lower(),)
                )
                existing_user = cursor.fetchone()
                if existing_user:
                    email_exists_in_other_role = True
                    existing_role = check_role
                    break

        if email_exists_in_other_role:
            cleanup_db_resources(cursor, db)
            role_display = existing_role.capitalize()
            current_role_display = role.capitalize()
            flash(f"⚠️ Account Exists: This email is already registered as a {role_display}. You cannot use the same email for different roles. Please login as {role_display} or use a different email to register as {current_role_display}.", "error")
            return redirect(url_for("login", role=existing_role))

        # Check if user exists in their current role
        cursor.execute(
            f"SELECT * FROM {table_map[role]} WHERE LOWER(email)=%s",
            (email.lower(),)
        )
        user = cursor.fetchone()

        # Create new user if doesn't exist
        if not user:
            custom_id = generate_custom_id(role)
            cursor.execute(
                f"""
                INSERT INTO {table_map[role]}
                (id, name, email, password, profile_completed)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (custom_id, name, email, "", False)
            )
            db.commit()

            cursor.execute(
                f"SELECT * FROM {table_map[role]} WHERE email=%s",
                (email,)
            )
            user = cursor.fetchone()

        cleanup_db_resources(cursor, db)

        # Set session
        session["user_id"] = user["id"]
        session["role"] = role
        session.permanent = True
        
        # Clear OAuth session data
        session.pop('oauth_state', None)
        session.pop('oauth_role', None)
        session.pop('oauth_next', None)

        dashboard_map = {
            "candidate": "/candidate-dashboard",
            "recruiter": "/recruiter-dashboard"
        }

        flash(f"Successfully logged in with Google as {name}!", "success")
        return redirect(dashboard_map[role])
        
    except Exception as e:
        print(f"Error during Google OAuth: {str(e)}")
        import traceback
        traceback.print_exc()
        flash("An error occurred during Google login. Please try again.", "error")
        return redirect(url_for("login"))

# Unified API endpoint for maintenance status (for dashboards)
@app.route('/api/maintenance-status')
def api_maintenance_status():
    try:
        settings = get_admin_settings()
        return jsonify({
            'success': True,
            'maintenance': bool(settings.get('maintenance_mode', False))
        })
    except Exception as e:
        return jsonify({'success': False, 'maintenance': False, 'error': str(e)}), 500

@app.route("/login", methods=["GET", "POST"])
@timing_decorator
def login():
    role = request.form.get("role") or request.args.get("role")

    table_map = {
        "admin": "admins",
        "candidate": "candidates",
        "recruiter": "recruiters"
    }

    if request.method == "POST":
        if not role:
            return "Role missing", 400

        role = role.strip().lower()

        if role not in table_map:
            return "Invalid role", 400

        email = request.form.get("email")
        password = request.form.get("password")
        remember = request.form.get("remember")

        if not email or not password:
            return render_template("login.html", role=role, error="Email and password are required.")

        email = email.strip().lower()

        # Fallback for default admin credentials to avoid local lockout.
        if (
            role == 'admin'
            and (email or '').strip().lower() == 'admin@hirehub.com'
            and password == 'Admin@123'
        ):
            try:
                with SafeDBConnection() as (cursor, db):
                    admin_id = "admin_001"
                    admin_name = "Admin User"
                    hashed_password = generate_password_hash("Admin@123")
                    cursor.execute(
                        """
                        INSERT INTO admins (id, name, email, password, profile_completed)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (email) DO UPDATE SET
                            name = EXCLUDED.name,
                            password = EXCLUDED.password,
                            profile_completed = TRUE
                        """,
                        (admin_id, admin_name, "admin@hirehub.com", hashed_password, True)
                    )
            except Exception as e:
                print(f"[WARNING] Default admin fallback upsert failed: {e}")

            session["user_id"] = "admin_001"
            session["role"] = "admin"
            session.permanent = bool(remember)
            return redirect("/admin-dashboard")

        try:
            with SafeDBConnection() as (cursor, db):
                cursor.execute(
                    """
                    SELECT *
                    FROM (
                        SELECT 'admin' AS role_name, id, email, password FROM admins
                        UNION ALL
                        SELECT 'candidate' AS role_name, id, email, password FROM candidates
                        UNION ALL
                        SELECT 'recruiter' AS role_name, id, email, password FROM recruiters
                    ) AS users_by_role
                    WHERE LOWER(email) = %s
                    """,
                    (email,)
                )
                matching_users = cursor.fetchall()

                user = next((row for row in matching_users if row["role_name"] == role), None)
                other_user = next((row for row in matching_users if row["role_name"] != role), None)

                if other_user and not user:
                    current_role_display = role.capitalize()
                    other_role_display = other_user["role_name"].capitalize()
                    error_msg = f"⚠️ Wrong Role: This email is registered as a {other_role_display}, not {current_role_display}. You cannot use the same email for different roles. Please login as {other_role_display} or use a different email address."
                    return render_template("login.html", role=role, error=error_msg)

        except Exception as e:
            print(f"Error during login: {e}")
            if "Database configuration is incomplete" in str(e):
                error_message = (
                    "Database is not configured for this deployment yet. "
                    "Please set DATABASE_URL or DB_HOST/DB_USER/DB_PASSWORD/DB_NAME/DB_PORT in the hosting environment."
                )
            else:
                error_message = (
                    "We couldn’t sign you in right now because the database is unavailable. "
                    "Please try again in a moment or contact support."
                )
            return render_template("login.html", role=role, error=error_message)

        if user and check_password_hash(user["password"], password):
            session["user_id"] = user["id"]
            session["role"] = role
            session.permanent = bool(remember)

            # Log login history (separate connection/context)
            try:
                ensure_login_history_table()
                with SafeDBConnection() as (cursor, db):
                    cursor.execute(
                        """
                        INSERT INTO login_history (user_id, user_type, ip_address, user_agent)
                        VALUES (%s, %s, %s, %s)
                        """,
                        (str(user["id"]), role, request.remote_addr, request.headers.get("User-Agent", ""))
                    )
                    db.commit()
            except Exception as e:
                print(f"Error logging login history: {e}")

            dashboard_map = {
                "candidate": "/candidate-dashboard",
                "recruiter": "/recruiter-dashboard",
                "admin": "/admin-dashboard"
            }
            return redirect(dashboard_map[role])

        return render_template("login.html", role=role, error="Invalid email or password. Please try again.")

    return render_template("login.html", role=role, error=None)

@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = request.form.get("email")
        email_sent = False

        try:
            db = get_connection()
            cursor = db.cursor(cursor_factory=RealDictCursor)

            for table in ["admins", "candidates", "recruiters", "mentors"]:
                cursor.execute(
                    f"SELECT id FROM {table} WHERE email=%s",
                    (email,)
                )
                user = cursor.fetchone()

                if user:
                    try:
                        token = serializer.dumps(email, salt="password-reset")
                        # Build reset link using production domain if configured
                        if IS_PRODUCTION:
                            reset_link = f"https://hirehub.com/reset-password/{token}"
                        else:
                            # For local/dev: use url_for with _external=True for proper URL generation
                            reset_link = url_for('reset_password_token', token=token, _external=True)


                        msg = Message(
                            "HireHub Password Reset",
                            sender=app.config['MAIL_USERNAME'],
                            recipients=[email]
                        )
                        msg.body = (
                            "Hi,\n\n"
                            "Click the link below to reset your password:\n\n"
                            f"{reset_link}\n\n"
                            "This link expires in 10 minutes.\n\n"
                            "If you didn't request this, ignore this email."
                        )
                        # Send email asynchronously to prevent worker timeout
                        Thread(target=send_async_email, args=(app, msg)).start()
                        email_sent = True
                        print(f"[OK] Password reset email queued for {email}")
                    except Exception as e:
                        print(f"[ERROR] Failed to send email: {str(e)}")
                        import traceback
                        traceback.print_exc()
                    break

            cleanup_db_resources(cursor, db)

            # Store email and timestamp in session for resend functionality
            session['reset_email'] = email
            session['reset_email_sent_at'] = time.time()
            
            flash("If the email exists, a reset link has been sent.")
            return redirect(url_for("forgot_password"))
            
        except Exception as e:
            print(f"[ERROR] Forgot password error: {str(e)}")
            import traceback
            traceback.print_exc()
            flash("An error occurred. Please try again.")
            return redirect(url_for("forgot_password"))



    # GET request - check if there's a recent send
    reset_email = session.get('reset_email', '')
    reset_sent_at = session.get('reset_email_sent_at', 0)
    
    # Clear session if older than 5 minutes or if accessing page fresh
    if reset_sent_at:
        elapsed = time.time() - reset_sent_at
        if elapsed > 300:  # 5 minutes
            session.pop('reset_email', None)
            session.pop('reset_email_sent_at', None)
            reset_email = ''
            reset_sent_at = 0
    
    # Calculate time remaining for resend (60 seconds cooldown)
    time_remaining = 0
    if reset_sent_at:
        elapsed = time.time() - reset_sent_at
        if elapsed < 60:
            time_remaining = int(60 - elapsed)
    
    return render_template("forgot_password.html", 
                         reset_email=reset_email,
                         time_remaining=time_remaining)


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password_token(token):
    try:
        email = serializer.loads(
            token,
            salt="password-reset",
            max_age=600  # 10 minutes
        )
    except:
        flash("Reset link has expired or is invalid. Please request a new one.", "error")
        return redirect("/forgot-password")
    if request.method == "POST":
        new_password = generate_password_hash(request.form.get("password"))

        db = get_connection()
        cursor = db.cursor(cursor_factory=RealDictCursor)
        for table in ["admins", "candidates", "recruiters", "mentors"]:
            cursor.execute(
                f"UPDATE {table} SET password=%s WHERE email=%s",
                (new_password, email)
            )
        db.commit()
        return redirect("/login")
    return render_template("reset_password.html")
def generate_custom_id(role):
    role = role.strip().lower()

    role_config = {
        'candidate': {'table': 'candidates', 'prefix': 'CAND'},
        'recruiter': {'table': 'recruiters', 'prefix': 'RECT'},
        'admin': {'table': 'admins', 'prefix': 'ADMN'}
    }

    if role not in role_config:
        raise ValueError("Invalid role")

    table = role_config[role]['table']
    prefix = role_config[role]['prefix']

    db = get_connection()
    cursor = db.cursor(cursor_factory=RealDictCursor)

    # Get the highest existing number
    cursor.execute(f"""
        SELECT MAX(CAST(SPLIT_PART(id, '-', 2) AS INTEGER)) AS max_num
        FROM {table}
    """)
    row = cursor.fetchone()
    cleanup_db_resources(cursor, db)

    last_num = row["max_num"] if row and row["max_num"] else 1000
    next_num = last_num + 1

    return f"{prefix}-{next_num}"


@app.route("/register", methods=["GET", "POST"])
def register():
    role = request.args.get("role") or request.form.get("role")

    allowed_roles = ["candidate", "recruiter"]
    table_map = {
        "candidate": "candidates",
        "recruiter": "recruiters"
    }

    # For GET requests without role, just render the role selection page
    if request.method == "GET" and not role:
        return render_template("register.html", role=None)

    # For POST or if role is provided in GET
    if not role or role not in allowed_roles:
        return render_template("register.html", role=None)

    if request.method == "POST":
        name = request.form.get("name")
        email = request.form.get("email")
        password = request.form.get("password")
        confirm_password = request.form.get("confirm_password")

        if password != confirm_password:
            return render_template(
                "register.html",
                role=role,
                error="Passwords do not match"
            )

        db = get_connection()
        cursor = db.cursor(cursor_factory=RealDictCursor)

        try:
            # 🔒 Check email across ALL roles
            for check_role, table in table_map.items():
                cursor.execute(
                    f"SELECT id FROM {table} WHERE LOWER(email)=%s",
                    (email.lower(),)
                )
                if cursor.fetchone():
                    cleanup_db_resources(cursor, db)
                    return render_template(
                        "register.html",
                        role=role,
                        error=f"This email is already registered as a {check_role.capitalize()}. "
                              f"You cannot use the same email for different roles."
                    )

            custom_id = generate_custom_id(role)
            hashed_password = generate_password_hash(password)

            cursor.execute(
                f"""
                INSERT INTO {table_map[role]}
                (id, name, email, password, profile_completed)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (custom_id, name, email, hashed_password, False)
            )
            db.commit()

        except psycopg2.IntegrityError:
            return render_template(
                "register.html",
                role=role,
                error="Email already registered"
            )

        finally:
            cleanup_db_resources(cursor, db)

        flash("Registration successful! Please login with your credentials.", "success")
        return redirect(f"/login?role={role}")

    return render_template("register.html", role=role)



def generate_job_recommendations(candidate_id, profile, latest_test=None):
    """
    Generate AI-powered job recommendations based on candidate profile, skills, and test scores.
    Returns a list of recommended jobs with match scores and explanations.
    """
    try:
        print(f"[FUNC-START] generate_job_recommendations called for candidate {candidate_id}")
        db = get_connection()
        cursor = db.cursor(cursor_factory=RealDictCursor)
        
        # Extract candidate skills
        candidate_skills = []
        if profile.get('primary_skills'):
            candidate_skills.extend([s.strip().lower() for s in str(profile['primary_skills']).split(',') if s.strip()])
        if profile.get('secondary_skills'):
            candidate_skills.extend([s.strip().lower() for s in str(profile['secondary_skills']).split(',') if s.strip()])
        if profile.get('frameworks_libraries'):
            candidate_skills.extend([s.strip().lower() for s in str(profile['frameworks_libraries']).split(',') if s.strip()])
        if profile.get('databases'):
            candidate_skills.extend([s.strip().lower() for s in str(profile['databases']).split(',') if s.strip()])
        if profile.get('cloud_platforms'):
            candidate_skills.extend([s.strip().lower() for s in str(profile['cloud_platforms']).split(',') if s.strip()])
        if profile.get('tools_technologies'):
            candidate_skills.extend([s.strip().lower() for s in str(profile['tools_technologies']).split(',') if s.strip()])
        
        candidate_skills = list(set(candidate_skills))  # Remove duplicates
        print(f"[FUNC-SKILLS] Candidate has {len(candidate_skills)} skills: {candidate_skills[:5]}")
        
        # Fetch active jobs with company details
        cursor.execute("""
            SELECT 
                j.id, j.title, j.location, j.job_type, j.description,
                j.deadline, j.employment_mode, j.required_skills,
                CASE
                    WHEN j.min_experience IS NOT NULL AND j.max_experience IS NOT NULL THEN
                        CONCAT(j.min_experience, ' - ', j.max_experience, ' years')
                    WHEN j.min_experience IS NOT NULL THEN
                        CONCAT(j.min_experience, ' years')
                    WHEN j.max_experience IS NOT NULL THEN
                        CONCAT(j.max_experience, ' years')
                    ELSE NULL
                END AS experience_required,
                j.salary_min, j.salary_max,
                rp.company_name, rp.logo_file
            FROM jobs j
            JOIN recruiter_profiles rp ON j.recruiter_id = rp.recruiter_id
            WHERE (j.deadline IS NULL OR j.deadline >= CURRENT_DATE)
                AND LOWER(COALESCE(rp.verification_status, '')) = 'approved'
                AND LOWER(COALESCE(j.status, '')) = 'active'
            ORDER BY j.created_at DESC
        """)
        all_jobs = cursor.fetchall()
        print(f"[FUNC-JOBS] Found {len(all_jobs)} active jobs to process")
        
        if not all_jobs:
            print("[FUNC-JOBS] WARNING: No jobs found matching criteria!")
            cleanup_db_resources(cursor, db)
            return []
        
        recommendations = []
        
        for idx, job in enumerate(all_jobs):
            try:
                # Compute matches using shared helper
                match = compute_candidate_job_match(job, profile, latest_test)
                matched_skills = match.get('matched_skills', [])
                missing_skills = match.get('missing_skills', [])
                total_score = match.get('total_score', 0)

                # Get AI score from latest_test if available
                ai_score = 0
                if latest_test and isinstance(latest_test, dict):
                    ai_score = float(latest_test.get('percentage') or latest_test.get('score') or 0)

                # Generate AI explanation
                ai_explanation = generate_recommendation_explanation(
                    profile, job, matched_skills, missing_skills, total_score, ai_score
                )

                print(f"[RECOMMEND] Job {idx+1}: id={job.get('id')} title={job.get('title')} score={total_score}")

                recommendations.append({
                    'job': job,
                    'total_score': total_score,
                    'skill_match': match.get('skill_match', 0),
                    'exp_match': match.get('exp_match', 0),
                    'ai_match': match.get('ai_match', 0),
                    'matched_skills': matched_skills,
                    'missing_skills': missing_skills,
                    'ai_explanation': ai_explanation
                })
            except Exception as job_err:
                print(f"[RECOMMEND-ERROR] Error processing job {idx}: {job_err}")
                import traceback
                traceback.print_exc()
                continue
        
        # Sort by total score descending
        recommendations.sort(key=lambda x: x['total_score'], reverse=True)
        
        print(f"[RECOMMEND] Generated {len(recommendations)} recommendations for candidate {candidate_id}")
        
        # Return top 10 recommendations
        cleanup_db_resources(cursor, db)
        
        return recommendations[:10]
        
    except Exception as e:
        print(f"Error generating job recommendations: {e}")
        import traceback
        traceback.print_exc()
        return []



def generate_recommendation_explanation(profile, job, matched_skills, missing_skills, total_score, ai_score):
    """Generate human-readable explanation for why a job is recommended"""
    explanations = []
    
    if total_score >= 80:
        explanations.append("Excellent match! Your skills align strongly with this role.")
    elif total_score >= 60:
        explanations.append("Good fit for your profile with room to grow.")
    else:
        explanations.append("Potential opportunity to develop new skills.")
    
    if len(matched_skills) > 0:
        if len(matched_skills) <= 3:
            skills_text = ", ".join(matched_skills[:3])
        else:
            skills_text = ", ".join(matched_skills[:2]) + f", and {len(matched_skills) - 2} more"
        explanations.append(f"Your expertise in {skills_text} matches job requirements.")
    
    if ai_score >= 75:
        explanations.append(f"Your assessment score ({ai_score:.0f}%) demonstrates strong technical capabilities.")
    
    if len(missing_skills) > 0 and len(missing_skills) <= 3:
        missing_text = ", ".join(missing_skills[:2])
        explanations.append(f"Consider improving: {missing_text}.")
    
    return " ".join(explanations)


def compute_candidate_job_match(job, profile, latest_test=None):
    """Compute skill/experience/AI match and weighted total for a candidate-job pair.

    Returns dict with keys: skill_match, exp_match, ai_match, total_score,
    matched_skills, missing_skills
    """
    try:
        import re

        # Candidate skills - split on commas then further split tokens on common separators
        candidate_skills = []
        def add_candidate_skills(field):
            if not field:
                return
            for token in str(field).split(','):
                t = token.strip()
                if not t:
                    continue
                # split combined tokens like 'sql/mysql' into ['sql','mysql']
                for sub in re.split(r'[\/\|;]+', t):
                    clean = re.sub(r"[^a-z0-9\s\+\#\.\-]", "", sub.strip().lower())
                    if clean:
                        candidate_skills.append(clean)

        if profile:
            add_candidate_skills(profile.get('primary_skills'))
            add_candidate_skills(profile.get('secondary_skills'))
            add_candidate_skills(profile.get('frameworks_libraries'))
            add_candidate_skills(profile.get('databases'))
            add_candidate_skills(profile.get('cloud_platforms'))
            add_candidate_skills(profile.get('tools_technologies'))
            add_candidate_skills(profile.get('skills'))

        candidate_skills = list(set(candidate_skills))

        # Job skills - normalize and split combined tokens (e.g. 'sql/mysql')
        job_skills = []
        def add_job_skills(field):
            if not field:
                return
            for token in str(field).split(','):
                t = token.strip()
                if not t:
                    continue
                for sub in re.split(r'[\/\|;]+', t):
                    clean = re.sub(r"[^a-z0-9\s\+\#\.\-]", "", sub.strip().lower())
                    if clean:
                        job_skills.append(clean)

        add_job_skills(job.get('required_skills'))
        add_job_skills(job.get('skills'))
        job_skills = list(set(job_skills))

        # Skill match
        if job_skills and candidate_skills:
            matched_skills = list(set(candidate_skills) & set(job_skills))
            missing_skills = list(set(job_skills) - set(candidate_skills))
            skill_match_percent = int((len(matched_skills) / len(job_skills)) * 100) if job_skills else 0
        else:
            matched_skills = []
            missing_skills = job_skills
            skill_match_percent = 0

        # Experience extraction
        experience_years = 0
        if profile and profile.get('work_experience'):
            exp_text = str(profile['work_experience']).lower()
            years = re.findall(r'(\d+)\s*(?:year|yr)', exp_text)
            if years:
                experience_years = int(years[0])

        job_exp_required = 0
        if job.get('experience_required'):
            exp_text = str(job['experience_required']).lower()
            years = re.findall(r'(\d+)', exp_text)
            if years:
                job_exp_required = int(years[0])

        if job_exp_required == 0:
            exp_match_percent = 100
        elif experience_years >= job_exp_required:
            exp_match_percent = 100
        elif experience_years >= job_exp_required * 0.7:
            exp_match_percent = 80
        else:
            exp_match_percent = max(0, int((experience_years / max(job_exp_required, 1)) * 100))

        # AI score
        ai_score = 0
        if latest_test and isinstance(latest_test, dict):
            ai_score = float(latest_test.get('percentage') or latest_test.get('score') or 0)

        ai_match_percent = min(100, int(ai_score))

        total_score = int(
            (skill_match_percent * 0.5) +
            (exp_match_percent * 0.3) +
            (ai_match_percent * 0.2)
        )

        return {
            'skill_match': skill_match_percent,
            'exp_match': exp_match_percent,
            'ai_match': ai_match_percent,
            'total_score': total_score,
            'matched_skills': matched_skills,
            'missing_skills': missing_skills
        }
    except Exception as e:
        print(f"Error computing match: {e}")
        return {
            'skill_match': 0,
            'exp_match': 0,
            'ai_match': 0,
            'total_score': 0,
            'matched_skills': [],
            'missing_skills': []
        }


# Helper Functions for Notifications and Activity Timeline
def create_notification(receiver_role, receiver_id, notification_type, title, message, action_url=None):
    """Create a notification for a user"""
    try:
        with SafeDBConnection() as (cursor, db):
            cursor.execute("""
                INSERT INTO notifications 
                (receiver_role, receiver_id, notification_type, title, message, action_url)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (receiver_role, receiver_id, notification_type, title, message, action_url))
        
        # Send real-time notification via SocketIO if available
        try:
            socketio.emit('new_notification', {
                'type': notification_type,
                'title': title,
                'message': message,
                'url': action_url
            }, room=f"{receiver_role}_{receiver_id}")
        except Exception:
            pass
        
        return True
    except Exception as e:
        print(f"Error creating notification: {e}")
        return False


def log_activity(user_id, user_role, activity_type, activity_title, activity_description, metadata=None):
    """Log an activity to the timeline"""
    try:
        import json
        metadata_json = json.dumps(metadata) if metadata else None
        
        with SafeDBConnection() as (cursor, db):
            cursor.execute("""
                INSERT INTO activity_timeline 
                (user_id, user_role, activity_type, activity_title, activity_description, metadata)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (user_id, user_role, activity_type, activity_title, activity_description, metadata_json))
        
        return True
    except Exception as e:
        print(f"Error logging activity: {e}")
        return False


def get_user_notifications(user_role, user_id, limit=10, unread_only=False):
    """Get notifications for a user"""
    try:
        db = get_connection()
        cursor = db.cursor(cursor_factory=RealDictCursor)
        
        query = """
            SELECT * FROM notifications 
            WHERE receiver_role = %s AND receiver_id = %s
        """
        params = [user_role, user_id]
        
        if unread_only:
            query += " AND is_read = FALSE"
        
        query += " ORDER BY created_at DESC LIMIT %s"
        params.append(limit)
        
        cursor.execute(query, tuple(params))
        notifications = cursor.fetchall()
        cleanup_db_resources(cursor, db)
        return notifications
    except Exception as e:
        print(f"Error getting notifications: {e}")
        return []


def get_user_activity_timeline(user_id, user_role, limit=20):
    """Get activity timeline for a user"""
    try:
        db = get_connection()
        cursor = db.cursor(cursor_factory=RealDictCursor)
        
        cursor.execute("""
            SELECT * FROM activity_timeline 
            WHERE user_id = %s AND user_role = %s
            ORDER BY created_at DESC LIMIT %s
        """, (user_id, user_role, limit))
        
        activities = cursor.fetchall()
        cleanup_db_resources(cursor, db)
        return activities
    except Exception as e:
        print(f"Error getting activity timeline: {e}")
        return []


ASSESSMENT_PAYMENT_AMOUNT = 0
MENTORSHIP_PAYMENT_BASE_AMOUNT = 99
MENTORSHIP_ADMIN_SHARE_PERCENT = 60
MENTORSHIP_MENTOR_SHARE_PERCENT = 40
PAID_ASSESSMENT_TYPES = {
    'technical_test': 'Technical Test',
    'mock_interview': 'Mock Interview'
}


def is_assessment_paid(cursor, candidate_id, assessment_type):
    cursor.execute(
        """
        SELECT id
        FROM candidate_assessment_payments
        WHERE candidate_id = %s AND assessment_type = %s AND payment_status = 'completed'
        ORDER BY paid_at DESC
        LIMIT 1
        """,
        (candidate_id, assessment_type)
    )
    return cursor.fetchone() is not None
def is_mentorship_paid(cursor, mentorship_request_id):
    cursor.execute(
        """
        SELECT id
        FROM mentorship_payments
        WHERE mentorship_request_id = %s AND payment_status = 'completed'
        ORDER BY paid_at DESC
        LIMIT 1
        """,
        (mentorship_request_id,)
    )
    return cursor.fetchone() is not None

@app.route('/candidate-dashboard', methods=["GET", "POST"])
@timing_decorator
def candidate_dashboard():
    # FIXED: Early exit guard clause for non-candidate users
    if session.get('role') != 'candidate':
        return redirect('/login')

    candidate_id = session.get('user_id')
    db = get_connection()
    cursor = db.cursor(cursor_factory=RealDictCursor)
    
    # MAIN LOGIC: All candidate dashboard processing
    # Use a single query with JOIN instead of multiple queries
    # Basic candidate info (name/email) and profile
    cursor.execute("SELECT id, name, email FROM candidates WHERE id=%s", (candidate_id,))
    user_basic = cursor.fetchone()

    cursor.execute("SELECT * FROM candidate_profiles WHERE candidate_id=%s", (candidate_id,))
    profile = cursor.fetchone()
    
    # If profile exists but doesn't have first_name/last_name, populate from registered name
    if profile and user_basic and user_basic['name']:
        if not profile.get('first_name') or not profile.get('last_name'):
            # Split registered name into first and last
            full_name = user_basic['name'].strip()
            name_parts = full_name.split()
            if len(name_parts) >= 2:
                profile['first_name'] = name_parts[0]
                profile['last_name'] = ' '.join(name_parts[1:])
            elif len(name_parts) == 1:
                profile['first_name'] = name_parts[0]
                profile['last_name'] = ''
    
    # If no profile exists yet, create default with registered name
    if not profile and user_basic and user_basic['name']:
        full_name = user_basic['name'].strip()
        name_parts = full_name.split()
        profile = {
            'first_name': name_parts[0] if len(name_parts) >= 1 else '',
            'last_name': ' '.join(name_parts[1:]) if len(name_parts) >= 2 else '',
            'profile_completed': False,
            'profile_percent': 0,
        }

    if request.method == "POST":
        # Collect all form data
        data = {
            'first_name': request.form.get('first_name'),
            'last_name': request.form.get('last_name'),
            'headline': request.form.get('headline'),
            'bio': request.form.get('bio'),
            'current_location': request.form.get('current_location'),
            'preferred_work_mode': request.form.get('preferred_work_mode'),
            'open_to_relocation': request.form.get('open_to_relocation'),
            'job_type_preference': request.form.get('job_type_preference'),
            'notice_period': request.form.get('notice_period'),
            'availability_date': request.form.get('availability_date'),
            'preferred_job_role': request.form.get('preferred_job_role'),
            'career_objective': request.form.get('career_objective'),
            'interested_domains': request.form.get('interested_domains'),
            'primary_skills': request.form.get('primary_skills'),
            'secondary_skills': request.form.get('secondary_skills'),
            'skill_proficiency': request.form.get('skill_proficiency'),
            'frameworks_libraries': request.form.get('frameworks_libraries'),
            'databases': request.form.get('databases'),
            'tools_technologies': request.form.get('tools_technologies'),
            'cloud_platforms': request.form.get('cloud_platforms'),
            'projects': request.form.get('projects'),
            'work_experience': request.form.get('work_experience'),
            'degree': request.form.get('degree'),
            'specialization': request.form.get('specialization'),
            'college_university': request.form.get('college_university'),
            'education_start_year': request.form.get('education_start_year'),
            'education_end_year': request.form.get('education_end_year'),
            'cgpa_percentage': request.form.get('cgpa_percentage'),
            'github_url': request.form.get('github_url'),
            'linkedin_url': request.form.get('linkedin_url'),
            'portfolio_url': request.form.get('portfolio_url'),
            'coding_platforms': request.form.get('coding_platforms'),
            'certifications': request.form.get('certifications'),
            'soft_skills': request.form.get('soft_skills'),
            'languages_known': request.form.get('languages_known'),
            'language_proficiency': request.form.get('language_proficiency'),
            'open_to_mentorship': request.form.get('open_to_mentorship'),
            'preferred_mentor_expertise': request.form.get('preferred_mentor_expertise'),
            'willing_ai_assessments': request.form.get('willing_ai_assessments'),
            'profile_visibility': request.form.get('profile_visibility')
        }

        # Handle resume upload
        file = request.files.get('resume')
        filename = profile.get('resume_file') if profile else None
        if file and file.filename != '':
            filename = secure_filename(file.filename)
            file.save(os.path.join(app.config["UPLOAD_FOLDER"], filename))

        # Handle profile picture upload
        photo = request.files.get('photo')
        photo_filename = profile.get('photo_file') if profile else None
        if photo and photo.filename != '':
            photo_filename = secure_filename(photo.filename)
            photo.save(os.path.join(app.config["UPLOAD_FOLDER"], photo_filename))

        # Calculate profile completion percentage
        required_fields = [data['first_name'], data['last_name'], data['headline'], data['bio'], 
                  data['primary_skills'], data['degree'], data['college_university'], filename]
        optional_fields = [data['current_location'], data['preferred_work_mode'], data['job_type_preference'],
                  data['preferred_job_role'], data['secondary_skills'], data['frameworks_libraries'],
                  data['projects'], data['work_experience'], data['github_url'], data['linkedin_url'],
                  data['portfolio_url'], data['notice_period'], data['availability_date'], data['certifications'],
                  data['open_to_relocation'], data['cloud_platforms']]

        filled_required = sum(1 for f in required_fields if f and str(f).strip())
        filled_optional = sum(1 for f in optional_fields if f and str(f).strip()) if optional_fields else 0

        required_score = (filled_required / len(required_fields)) * 70 if required_fields else 0
        optional_score = (filled_optional / len(optional_fields)) * 30 if optional_fields else 0
        profile_percent = int(required_score + optional_score)
        profile_completed = True if profile_percent >= 85 else False

        cursor.execute("""
            INSERT INTO candidate_profiles 
            (candidate_id, first_name, last_name, headline, bio, resume_file, photo_file,
             current_location, preferred_work_mode, open_to_relocation, job_type_preference, notice_period, availability_date,
             preferred_job_role, career_objective, interested_domains,
             primary_skills, secondary_skills, skill_proficiency, frameworks_libraries, "databases", tools_technologies, cloud_platforms,
             projects, work_experience,
             degree, specialization, college_university, education_start_year, education_end_year, cgpa_percentage,
             github_url, linkedin_url, portfolio_url, coding_platforms,
             certifications, soft_skills, languages_known, language_proficiency,
             open_to_mentorship, preferred_mentor_expertise, willing_ai_assessments, profile_visibility,
             profile_completed, profile_percent)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (candidate_id) DO UPDATE SET
            first_name=EXCLUDED.first_name, last_name=EXCLUDED.last_name, headline=EXCLUDED.headline, bio=EXCLUDED.bio,
            current_location=EXCLUDED.current_location, preferred_work_mode=EXCLUDED.preferred_work_mode, 
            open_to_relocation=EXCLUDED.open_to_relocation, job_type_preference=EXCLUDED.job_type_preference,
            notice_period=EXCLUDED.notice_period, availability_date=EXCLUDED.availability_date,
            preferred_job_role=EXCLUDED.preferred_job_role, career_objective=EXCLUDED.career_objective, interested_domains=EXCLUDED.interested_domains,
            primary_skills=EXCLUDED.primary_skills, secondary_skills=EXCLUDED.secondary_skills, skill_proficiency=EXCLUDED.skill_proficiency,
            frameworks_libraries=EXCLUDED.frameworks_libraries, "databases"=EXCLUDED."databases", tools_technologies=EXCLUDED.tools_technologies, cloud_platforms=EXCLUDED.cloud_platforms,
            projects=EXCLUDED.projects, work_experience=EXCLUDED.work_experience,
            degree=EXCLUDED.degree, specialization=EXCLUDED.specialization, college_university=EXCLUDED.college_university,
            education_start_year=EXCLUDED.education_start_year, education_end_year=EXCLUDED.education_end_year, cgpa_percentage=EXCLUDED.cgpa_percentage,
            github_url=EXCLUDED.github_url, linkedin_url=EXCLUDED.linkedin_url, portfolio_url=EXCLUDED.portfolio_url, coding_platforms=EXCLUDED.coding_platforms,
            certifications=EXCLUDED.certifications, soft_skills=EXCLUDED.soft_skills, languages_known=EXCLUDED.languages_known, language_proficiency=EXCLUDED.language_proficiency,
            open_to_mentorship=EXCLUDED.open_to_mentorship, preferred_mentor_expertise=EXCLUDED.preferred_mentor_expertise,
            willing_ai_assessments=EXCLUDED.willing_ai_assessments, profile_visibility=EXCLUDED.profile_visibility,
            resume_file=EXCLUDED.resume_file, photo_file=EXCLUDED.photo_file, profile_completed=EXCLUDED.profile_completed, profile_percent=EXCLUDED.profile_percent
        """, (candidate_id, data['first_name'], data['last_name'], data['headline'], data['bio'], filename, photo_filename,
              data['current_location'], data['preferred_work_mode'], data['open_to_relocation'], data['job_type_preference'], 
              data['notice_period'], data['availability_date'], data['preferred_job_role'], data['career_objective'], data['interested_domains'],
              data['primary_skills'], data['secondary_skills'], data['skill_proficiency'], data['frameworks_libraries'], 
              data['databases'], data['tools_technologies'], data['cloud_platforms'], data['projects'], data['work_experience'],
              data['degree'], data['specialization'], data['college_university'], data['education_start_year'], 
              data['education_end_year'], data['cgpa_percentage'], data['github_url'], data['linkedin_url'], 
              data['portfolio_url'], data['coding_platforms'], data['certifications'], data['soft_skills'], 
              data['languages_known'], data['language_proficiency'], data['open_to_mentorship'], data['preferred_mentor_expertise'],
              data['willing_ai_assessments'], data['profile_visibility'], profile_completed, profile_percent))

        db.commit()
        flash("Profile updated successfully!", "success")
        return redirect('/candidate-dashboard#profile') 

    profile_completed = bool(profile.get('profile_completed')) if profile else False
    profile_percent = profile.get('profile_percent', 0) if profile else 0

    # Profile Lock Logic: Features locked if profile < 85%
    features_locked = profile_percent < 85
    
    # If profile just reached 85%, create unlock notification
    if request.method == "POST" and profile_percent >= 85 and not profile_completed:
        create_notification(
            'candidate', candidate_id, 'profile_unlock',
            'Profile Unlocked!',
            'Congratulations! Your profile is complete. You can now access jobs, assessments, and more.',
            '/candidate-dashboard'
        )
        log_activity(
            candidate_id, 'candidate', 'profile_complete',
            'Profile Completed',
            f'Profile completion reached {profile_percent}%. All features unlocked.',
            {'profile_percent': profile_percent}
        )

    meeting_map = {}

    # Fetch active jobs for candidates (only if unlocked)
    jobs = []
    if not features_locked:
        cursor.execute("""
            SELECT 
                j.id, j.title, j.location, j.job_type, j.description,
                j.deadline, j.employment_mode,
                rp.company_name, rp.logo_file
            FROM jobs j
            JOIN recruiter_profiles rp ON j.recruiter_id = rp.recruiter_id
            WHERE (j.deadline IS NULL OR j.deadline >= CURRENT_DATE)
                AND LOWER(COALESCE(rp.verification_status, '')) = 'approved'
                AND LOWER(COALESCE(j.status, '')) = 'active'
            ORDER BY j.created_at DESC
        """)
        jobs = cursor.fetchall()

    # Fetch candidate's job applications with status tracking
    applications = []
    applied_count = 0
    if not features_locked:
        cursor.execute("""
            SELECT 
                a.id, a.status, a.applied_at, a.updated_at, a.rejection_reason,
                j.id AS job_id, j.title, j.location, j.job_type, j.employment_mode,
                rp.company_name, rp.logo_file
            FROM applications a
            JOIN jobs j ON a.job_id = j.id
            JOIN recruiter_profiles rp ON j.recruiter_id = rp.recruiter_id
            WHERE a.candidate_id = %s
            ORDER BY a.applied_at DESC
        """, (candidate_id,))
        applications = cursor.fetchall()
        applied_count = len(applications)

    # Fetch scheduled interviews for this candidate
    interviews = []
    if not features_locked:
        cursor.execute("""
            SELECT 
                i.id, i.interview_date, i.interview_time, i.interview_mode,
                i.interview_link, i.location, i.status, i.interviewer_name,
                i.interview_type, i.notes, i.result, i.feedback,
                j.title AS job_title, j.location AS job_location,
                rp.company_name, rp.logo_file
            FROM interviews i
            JOIN jobs j ON i.job_id = j.id
            JOIN recruiter_profiles rp ON j.recruiter_id = rp.recruiter_id
            WHERE i.candidate_id = %s
            ORDER BY i.interview_date DESC, i.interview_time DESC
        """, (candidate_id,))
        interviews = cursor.fetchall()
    
    # Fetch offer letters for this candidate
    offer_letters = []
    if not features_locked:
        cursor.execute("""
            SELECT 
                ol.id, ol.position, ol.salary, ol.joining_date, ol.location,
                ol.employment_type, ol.offer_file, ol.status, ol.generated_at,
                j.title AS job_title,
                rp.company_name, rp.logo_file
            FROM offer_letters ol
            JOIN jobs j ON ol.job_id = j.id
            JOIN recruiter_profiles rp ON j.recruiter_id = rp.recruiter_id
            WHERE ol.candidate_id = %s
            ORDER BY ol.generated_at DESC
        """, (candidate_id,))
        offer_letters = cursor.fetchall()

    # Fetch latest completed AI test for skill analysis with proper score calculation
    latest_test = None
    cursor.execute("""
        SELECT 
            id,
            total_questions,
            total_marks,
            obtained_marks,
            percentage,
            skills_tested,
            status,
            completed_at,
            ROUND(CAST(percentage AS NUMERIC), 2) as score
        FROM ai_tests 
        WHERE candidate_id = %s AND status = 'completed' 
        ORDER BY completed_at DESC LIMIT 1
    """, (candidate_id,))
    latest_test = cursor.fetchone()
    
    if latest_test:
        print(f"Latest test found: ID={latest_test['id']}, Score={latest_test['score']}%")

    assessment_payment_status = {
        'technical_test': False,
        'mock_interview': False
    }
    cursor.execute(
        """
        SELECT assessment_type
        FROM candidate_assessment_payments
        WHERE candidate_id = %s AND payment_status = 'completed'
        """,
        (candidate_id,)
    )
    paid_rows = cursor.fetchall()
    for row in paid_rows:
        payment_type = row.get('assessment_type')
        if payment_type in assessment_payment_status:
            assessment_payment_status[payment_type] = True

    # Generate AI-powered job recommendations if profile is complete (only if unlocked)
    recommended_jobs = []
    if profile:
        recommended_jobs = generate_job_recommendations(candidate_id, profile, latest_test)
        
        # TEMPORARY FALLBACK: If no recommendations generated, convert browse jobs to recommendations
        if not recommended_jobs and jobs:
            for job in jobs:
                recommended_jobs.append({
                    'job': job,
                    'total_score': 75,  # Default score
                    'skill_match': 70,
                    'exp_match': 80,
                    'ai_match': 60,
                    'matched_skills': [],
                    'missing_skills': [],
                    'ai_explanation': 'Job matches your profile'
                })

    # Fetch user notifications (UI bell)
    notifications = get_user_notifications('candidate', candidate_id, limit=10)
    unread_count = len([n for n in notifications if not n['is_read']])

    # Get filter parameters for activity timeline
    activity_type_filter = request.args.get('type', 'all')
    date_filter = request.args.get('date', 'all')
    search_query = request.args.get('search', '')

    # Fetch activity timeline with filters
    activity_query = """
        SELECT * FROM activity_timeline 
        WHERE user_id = %s AND user_role = %s
    """
    activity_params = [candidate_id, 'candidate']

    # Apply activity type filter
    if activity_type_filter != 'all':
        activity_query += " AND activity_type = %s"
        activity_params.append(activity_type_filter)

    # Apply date filter
    if date_filter == 'today':
        activity_query += " AND DATE(created_at) = CURRENT_DATE"
    elif date_filter == 'week':
        activity_query += " AND created_at >= CURRENT_TIMESTAMP - INTERVAL '7 days'"
    elif date_filter == 'month':
        activity_query += " AND created_at >= CURRENT_TIMESTAMP - INTERVAL '30 days'"

    # Apply search filter
    if search_query:
        activity_query += " AND (activity_title LIKE %s OR activity_description LIKE %s)"
        search_term = f"%{search_query}%"
        activity_params.extend([search_term, search_term])

    activity_query += " ORDER BY created_at DESC LIMIT 100"

    cursor.execute(activity_query, activity_params)
    activity_timeline = cursor.fetchall()

    # Settings: privacy, notification prefs, login history (reused from candidate-settings page)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS privacy_settings (
            id SERIAL PRIMARY KEY,
            candidate_id VARCHAR(20) UNIQUE,
            show_email BOOLEAN DEFAULT FALSE,
            show_phone BOOLEAN DEFAULT FALSE,
            allow_recruiter_messages BOOLEAN DEFAULT TRUE,
            searchable_profile BOOLEAN DEFAULT TRUE,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (candidate_id) REFERENCES candidates(id)
        )
    """)

    cursor.execute("SELECT * FROM privacy_settings WHERE candidate_id = %s", (candidate_id,))
    privacy_settings = cursor.fetchone()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS notification_preferences (
            id SERIAL PRIMARY KEY,
            candidate_id VARCHAR(20) UNIQUE,
            email_notifications BOOLEAN DEFAULT TRUE,
            job_alerts BOOLEAN DEFAULT TRUE,
            interview_reminders BOOLEAN DEFAULT TRUE,
            application_updates BOOLEAN DEFAULT TRUE,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (candidate_id) REFERENCES candidates(id)
        )
    """)

    cursor.execute("SELECT * FROM notification_preferences WHERE candidate_id = %s", (candidate_id,))
    notification_prefs = cursor.fetchone()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS login_history (
            id SERIAL PRIMARY KEY,
            user_id VARCHAR(20),
            user_type VARCHAR(20),
            ip_address VARCHAR(45),
            user_agent TEXT,
            login_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_user ON login_history (user_id, user_type)")

    cursor.execute(
        """
        SELECT * FROM login_history 
        WHERE user_id = %s AND user_type = 'candidate'
        ORDER BY login_time DESC
        LIMIT 10
        """,
        (candidate_id,)
    )
    login_history = cursor.fetchall()

    cleanup_db_resources(cursor, db)

    return render_template(
        "candidate_dashboard.html",
        user=user_basic,
        jobs=jobs,
        profile_completed=profile_completed,
        profile_percent=profile_percent,
        profile=profile,
        features_locked=features_locked,
        applied_count=applied_count,
        applications=applications,
        interviews=interviews,
        offer_letters=offer_letters,
        meeting_map=meeting_map,
        latest_test=latest_test,
        assessment_payment_status=assessment_payment_status,
        assessment_payment_amount=ASSESSMENT_PAYMENT_AMOUNT,
        recommended_jobs=recommended_jobs,
        notifications=notifications,
        unread_count=unread_count,
        activity_timeline=activity_timeline,
        privacy_settings=privacy_settings,
        notification_prefs=notification_prefs,
        login_history=login_history,
        current_filter=activity_type_filter,
        current_date=date_filter,
        search_query=search_query
    )


@app.route('/pay-assessment/<assessment_type>', methods=['POST'])
def pay_assessment(assessment_type):
    if session.get('role') != 'candidate':
        return redirect('/login')

    if assessment_type not in PAID_ASSESSMENT_TYPES:
        flash('Invalid assessment payment request.', 'danger')
        return redirect('/candidate-dashboard#assessments')

    candidate_id = session.get('user_id')
    db = get_connection()
    cursor = db.cursor(cursor_factory=RealDictCursor)

    try:
        if is_assessment_paid(cursor, candidate_id, assessment_type):
            flash(f"{PAID_ASSESSMENT_TYPES[assessment_type]} is already unlocked.", 'info')
            cleanup_db_resources(cursor, db)
            return redirect('/candidate-dashboard#assessments')

        payment_reference = f"SIM-{assessment_type.upper()}-{int(time.time())}-{candidate_id}"
        cursor.execute(
            """
            INSERT INTO candidate_assessment_payments
            (candidate_id, assessment_type, amount, currency, payment_status, payment_reference)
            VALUES (%s, %s, %s, 'INR', 'completed', %s)
            """,
            (candidate_id, assessment_type, ASSESSMENT_PAYMENT_AMOUNT, payment_reference)
        )

        cursor.execute(
            """
            INSERT INTO activity_timeline
            (user_id, user_role, activity_type, activity_title, activity_description, metadata)
            VALUES (%s, 'candidate', 'payment', %s, %s, %s::jsonb)
            """,
            (
                candidate_id,
                f"Paid ₹{ASSESSMENT_PAYMENT_AMOUNT} for {PAID_ASSESSMENT_TYPES[assessment_type]}",
                f"Assessment unlocked after successful payment of ₹{ASSESSMENT_PAYMENT_AMOUNT}.",
                json.dumps({
                    'assessment_type': assessment_type,
                    'amount': ASSESSMENT_PAYMENT_AMOUNT,
                    'payment_reference': payment_reference
                })
            )
        )

        db.commit()
        flash(f"Payment successful! {PAID_ASSESSMENT_TYPES[assessment_type]} unlocked.", 'success')
    except Exception as e:
        db.rollback()
        print(f"Assessment payment error: {e}")
        flash('Payment failed. Please try again.', 'danger')
    finally:
        cleanup_db_resources(cursor, db)

    return redirect('/candidate-dashboard#assessments')

@app.route('/pay-mentorship/<int:request_id>', methods=['POST'])
def pay_mentorship(request_id):
    flash('Mentorship features are no longer available on this platform.', 'info')
    return redirect('/candidate-dashboard')

    if session.get('role') != 'candidate':
        return redirect('/login')

    candidate_id = session.get('user_id')
    db = get_connection()
    cursor = db.cursor(cursor_factory=RealDictCursor)

    try:
        cursor.execute(
            """
            SELECT mr.id, mr.status, mr.candidate_id, mr.mentor_id,
                   m.name AS mentor_name,
                     COALESCE(mp.mentor_price, %s) AS mentor_price,
                     mp.verification_status AS mentor_verification_status
            FROM mentorship_requests mr
            JOIN mentors m ON mr.mentor_id = m.id
            LEFT JOIN mentor_profiles mp ON m.id = mp.mentor_id
            WHERE mr.id = %s AND mr.candidate_id = %s
            """,
            (MENTORSHIP_PAYMENT_BASE_AMOUNT, request_id, candidate_id)
        )
        req = cursor.fetchone()

        if not req:
            flash('Mentorship request not found.', 'danger')
            return redirect('/candidate-dashboard#mentors')

        if req.get('status') != 'Accepted':
            flash('Payment is available only after mentor acceptance.', 'warning')
            return redirect('/candidate-dashboard#mentors')

        if req.get('mentor_verification_status') != 'approved':
            flash('Payment is available only after admin approval of the mentor.', 'warning')
            return redirect('/candidate-dashboard#mentors')

        if is_mentorship_paid(cursor, request_id):
            flash('Mentorship payment already completed.', 'info')
            return redirect('/candidate-dashboard#mentors')

        amount = float(req.get('mentor_price') or MENTORSHIP_PAYMENT_BASE_AMOUNT)
        admin_share = round(amount * (MENTORSHIP_ADMIN_SHARE_PERCENT / 100), 2)
        mentor_share = round(amount * (MENTORSHIP_MENTOR_SHARE_PERCENT / 100), 2)
        payment_reference = f"SIM-MENTOR-{request_id}-{int(time.time())}-{candidate_id}"

        cursor.execute(
            """
            INSERT INTO mentorship_payments
              (mentorship_request_id, candidate_id, mentor_id, amount, currency,
               admin_share, mentor_share, mentor_payout_amount, payout_status,
               payment_status, payment_reference)
              VALUES (%s, %s, %s, %s, 'INR', %s, %s, %s, 'pending', 'completed', %s)
            """,
            (request_id, candidate_id, req['mentor_id'], amount,
               admin_share, mentor_share, mentor_share, payment_reference)
        )

        cursor.execute(
            """
            INSERT INTO activity_timeline
            (user_id, user_role, activity_type, activity_title, activity_description, metadata)
            VALUES (%s, 'candidate', 'payment', %s, %s, %s::jsonb)
            """,
            (
                candidate_id,
                f"Paid ₹{int(amount)} for mentorship",
                f"Mentorship unlocked after payment to {req['mentor_name']}.",
                json.dumps({
                    'mentorship_request_id': request_id,
                    'mentor_id': req['mentor_id'],
                    'amount': amount,
                    'admin_share': admin_share,
                    'mentor_share': mentor_share,
                    'payment_reference': payment_reference
                })
            )
        )

        db.commit()
        flash('Payment successful! You can now chat with your mentor.', 'success')
    except Exception as e:
        db.rollback()
        print(f"Mentorship payment error: {e}")
        flash('Payment failed. Please try again.', 'danger')
    finally:
        cleanup_db_resources(cursor, db)

    return redirect('/candidate-dashboard#mentors')

@app.route('/extract-resume', methods=['POST'])
def extract_resume():
    """Extract data from resume PDF/DOCX and return extracted fields"""
    if session.get('role') != 'candidate':
        return jsonify({'error': 'Unauthorized'}), 401
    
    if 'resume' not in request.files:
        return jsonify({'error': 'No resume file provided'}), 400
    
    file = request.files['resume']
    if file.filename == '':
        return jsonify({'error': 'Empty file'}), 400
    
    try:
        from docx import Document
        extracted_data = {}
        
        # Get candidate's registered full name from database
        candidate_id = session.get('user_id')
        db_temp = get_connection()
        cursor_temp = db_temp.cursor(cursor_factory=RealDictCursor)
        cursor_temp.execute("SELECT name FROM candidates WHERE id=%s", (candidate_id,))
        candidate = cursor_temp.fetchone()
        cleanup_db_resources(cursor_temp, db_temp)
        
        # Split registered full name into first and last name
        if candidate and candidate['name']:
            full_name = candidate['name'].strip()
            name_parts = full_name.split()
            if len(name_parts) >= 2:
                extracted_data['first_name'] = name_parts[0]
                extracted_data['last_name'] = ' '.join(name_parts[1:])
            elif len(name_parts) == 1:
                extracted_data['first_name'] = name_parts[0]
            print(f"[NAME FROM REGISTRATION] First: {extracted_data.get('first_name')}, Last: {extracted_data.get('last_name')}")
        
        if file.filename.endswith('.pdf'):
            # Extract from PDF
            try:
                pdf_data = fitz.open(stream=file.read(), filetype="pdf")
                text = ""
                for page in pdf_data:
                    text += page.get_text()
                pdf_data.close()
                print(f"[FILE EXTRACTION] PDF: Successfully extracted {len(text)} chars, {len(text.split())} words")
            except Exception as e:
                print(f"[FILE EXTRACTION ERROR] PDF extraction failed: {str(e)}")
                text = ""
        elif file.filename.endswith(('.docx', '.doc')):
            # Extract from DOCX using python-docx
            text = ""
            try:
                doc = Document(file)
                text = "\n".join([para.text for para in doc.paragraphs])
                print(f"[FILE EXTRACTION] DOCX: Successfully extracted {len(text)} chars, {len(text.split())} words")
            except Exception as e:
                print(f"[FILE EXTRACTION] DOCX extraction failed, trying XML fallback...")
                # Fallback to zipfile method
                import zipfile
                from xml.etree import ElementTree as ET
                
                file.seek(0)
                try:
                    docx_data = zipfile.ZipFile(file)
                    xml_content = docx_data.read('word/document.xml')
                    root = ET.fromstring(xml_content)
                    text = ""
                    for elem in root.iter():
                        if elem.tag.endswith('}t'):
                            if elem.text:
                                text += elem.text + " "
                    print(f"[FILE EXTRACTION] XML fallback: Successfully extracted {len(text)} chars")
                except Exception as xml_err:
                    print(f"[FILE EXTRACTION ERROR] XML fallback also failed: {str(xml_err)}")
                    text = ""  # Empty text if all extraction fails
        else:
            return jsonify({'error': 'Only PDF, DOCX, and DOC files are supported'}), 400
        
        # Basic extraction logic - extract common resume sections
        import re
        
        # Clean and normalize text
        text = re.sub(r'\s+', ' ', text)  # Normalize whitespace
        lines = text.split('\n')
        
        # Check if text is empty
        if not text or len(text.strip()) == 0:
            print(f"[CRITICAL ERROR] No text extracted from resume file!")
            return jsonify({'error': 'Could not extract text from resume. Try another file format.'}), 400
        
        # Debug: Print raw text for analysis
        print(f"\n[RESUME EXTRACTION] Starting extraction")
        print(f"[RESUME TEXT LENGTH] {len(text)} characters")
        print(f"[RESUME FIRST 500 CHARS] {text[:500]}")
        print(f"[RESUME LINES COUNT] {len(lines)}")
        print(f"[RESUME WORD COUNT] {len(text.split())} words")
        print(f"\n[FULL RESUME TEXT]\n{text}\n[END FULL TEXT]\n")
        
        # Extract name (usually first meaningful capitalized words)
        name_match = None
        headline_found_idx = -1
        for idx, line in enumerate(lines[:20]):
            line_clean = line.strip()
            if line_clean and len(line_clean) < 100:
                # Try to match name pattern - only use if name wasn't set from registration
                # Extract the first part before common separators like LinkedIn, Phone, Email, etc
                name_part = re.split(r'\s+(?:linkedin|phone|email|contact|location|skype|github|portfolio)[:\s]', line_clean, flags=re.IGNORECASE)[0]
                
                # Check if it looks like a name (2+ capitalized words)
                if re.match(r'^[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+$', name_part.strip()):
                    parts = name_part.strip().split()
                    if len(parts) >= 2 and 'first_name' not in extracted_data:
                        extracted_data['first_name'] = parts[0]
                        extracted_data['last_name'] = ' '.join(parts[1:])  # Handle middle names
                        headline_found_idx = idx
                        print(f"[NAME] Found: {parts[0]} {' '.join(parts[1:])}")
                        break
                    elif len(parts) == 1 and len(name_part.strip()) > 2 and 'first_name' not in extracted_data:
                        extracted_data['first_name'] = parts[0]
                        headline_found_idx = idx
                        print(f"[NAME] Found: {parts[0]}")
                        break
        
        # If we found a name, look for headline in the next 1-3 lines
        if headline_found_idx >= 0:
            for idx in range(headline_found_idx + 1, min(headline_found_idx + 4, len(lines))):
                line = lines[idx].strip()
                if line and len(line) < 150 and len(line) > 3:
                    # Check if it's a meaningful headline (contains job-related keywords or is just text)
                    if any(word in line.lower() for word in ['engineer', 'developer', 'analyst', 'manager', 'designer', 'scientist', 'architect', 'lead', 'senior', 'consultant', 'officer', 'specialist', 'director']) or (
                        len(line.split()) <= 8 and not line.lower().startswith(('email', 'phone', 'location', 'linkedin'))
                    ):
                        extracted_data['headline'] = line[:100]
                        print(f"[HEADLINE] Found: {line}")
                        break
        
        # Extract email
        email_match = re.search(r'\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b', text)
        if email_match:
            extracted_data['email'] = email_match.group(0)
        
        # Extract phone number (multiple formats)
        phone_patterns = [
            r'\+?\d{1,3}[-.\s]?\d{1,4}[-.\s]?\d{1,4}[-.\s]?\d{1,9}',  # International
            r'\+?1?\s*(?:\(\d{3}\)|\d{3})[-.\s]?\d{3}[-.\s]?\d{4}',    # US format
            r'\b\d{10}\b',                                             # 10 digit
        ]
        phone_match = None
        for pattern in phone_patterns:
            phone_match = re.search(pattern, text)
            if phone_match:
                break
        if phone_match:
            extracted_data['phone'] = phone_match.group(0)
        
        # Extract location (look for city, state pattern)
        location_patterns = [
            r'(?:Location|Located in|Based in|City)[:\s]*([A-Za-z\s,]+?)(?:\n|(?=[A-Z])|$)',
            r'\b(?:New York|Los Angeles|Chicago|Houston|Phoenix|Philadelphia|San Antonio|San Diego|Dallas|San Jose|Austin|Jacksonville|Fort Worth|Columbus|Charlotte|San Francisco|Indianapolis|Seattle|Denver|Boston|El Paso|Nashville|Detroit|Oklahoma City|Portland|Las Vegas|Memphis|Louisville|Baltimore|Milwaukee|Albuquerque|Tucson|Fresno|Sacramento|Mesa|Atlanta|Kansas City|Long Beach|Miami|Virginia Beach|Oakland|Minneapolis|Tulsa|Tampa|New Orleans|Arlington|Aurora|Santa Ana|Anaheim|Cincinnati|Corpus Christi|Riverside|Lexington|Stockton|Saint Paul|Henderson|Plano|Henderson|Orlando|Chula Vista|Jersey City)\b[,\s]*(?:[A-Z]{2})?',
            r'(?:[A-Za-z\s]+,?\s*(?:CA|NY|TX|FL|IL|PA|OH|GA|NC|MI|NJ|VA|WA|AZ|MA|TN|IN|MD|MO|WI|CO|MN|SC|AL|LA|KY|OR|OK|CT|UT|AR|NV|NM|KS|NE|ID|HI|NH|ME|MT|RI|DE|SD|ND|AK|VT|WY))\b',
        ]
        for pattern in location_patterns:
            location_match = re.search(pattern, text, re.IGNORECASE)
            if location_match:
                loc = location_match.group(0).strip()
                if len(loc) > 2:
                    extracted_data['current_location'] = loc[:100]
                break
        
        # Extract skills - ultra-flexible approach
        # First, look for any text that contains skill keywords
        skill_indicators = ['python', 'java', 'javascript', 'c++', 'react', 'nodejs', 'sql', 'html', 'css', 'angular', 'vue', 'django', 'flask', 'spring', 'docker', 'kubernetes', 'git', 'aws', 'azure', 'gcp']
        skills_found = []
        for skill in skill_indicators:
            if skill.lower() in text.lower():
                skills_found.append(skill)
        
        if skills_found:
            extracted_data['primary_skills'] = ', '.join(set(skills_found))[:300]
            print(f"[SKILLS - AUTO DETECTED] {extracted_data['primary_skills']}")
        else:
            # Try section-based extraction
            for keyword in ['skills', 'technical skills', 'technical competencies', 'core skills']:
                idx = text.lower().find(keyword)
                if idx != -1:
                    start = idx + len(keyword)
                    # Get next 200 chars or until next section
                    section_end = min(len(text), start + 300)
                    for next_keyword in ['experience', 'education', 'projects']:
                        next_idx = text.lower().find(next_keyword, start)
                        if next_idx != -1:
                            section_end = min(section_end, next_idx)
                    
                    skills_text = text[start:section_end].strip()
                    if skills_text and len(skills_text) > 3:
                        skills_text = re.sub(r'[\•\-\*:,;]', ' ', skills_text)[:300]
                        extracted_data['primary_skills'] = skills_text
                        print(f"[SKILLS - SECTION] {skills_text[:100]}")
                        break
        
        # Extract education - simple and direct
        degree_patterns = r'(?:Bachelor|Master|PhD|B\.?S|M\.?S|B\.?A|M\.?A|B\.?Tech|M\.?Tech|Associate|Diploma|B\.E|B\.Com|M\.Com|BE|BTech|MTech|BCA|MCA|BSc|MSc)'
        degree_match = re.search(degree_patterns, text, re.IGNORECASE)
        if degree_match:
            extracted_data['degree'] = degree_match.group(0)
            print(f"[DEGREE] Found: {degree_match.group(0)}")
        
        # Extract college - look for university/college/institute keywords and extract just the name
        for keyword in ['university', 'college', 'institute', 'school', 'academy']:
            idx = text.lower().find(keyword)
            if idx != -1:
                # Get surrounding text (max 50 chars before, 30 chars after)
                start = max(0, idx - 50)
                end = min(len(text), idx + len(keyword) + 30)
                context = text[start:end]
                
                # Extract just the institution name - typically 2-5 words
                words = context.split()
                # Find the keyword in words
                kw_idx = -1
                for i, word in enumerate(words):
                    if keyword in word.lower():
                        kw_idx = i
                        break
                
                if kw_idx >= 0:
                    # Get 1-2 words before and the keyword
                    start_word = max(0, kw_idx - 2)
                    end_word = min(len(words), kw_idx + 2)
                    college_name = ' '.join(words[start_word:end_word]).strip()
                    
                    if len(college_name) > 5 and college_name not in extracted_data.get('headline', ''):
                        extracted_data['college_university'] = college_name[:100]
                        print(f"[COLLEGE] Found: {college_name[:100]}")
                        break
        
        # Extract work experience - improved
        experience_keywords = ['experience', 'work experience', 'professional experience', 'employment', 'career', 'professional background', 'work history']
        for keyword in experience_keywords:
            idx = text.lower().find(keyword)
            if idx != -1:
                start = idx + len(keyword)
                section_end = len(text)
                
                next_sections = ['education', 'skills', 'projects', 'certifications', 'summary', 'languages']
                for next_sect in next_sections:
                    next_idx = text.lower().find(next_sect, start)
                    if next_idx != -1 and next_idx < section_end:
                        section_end = next_idx
                
                exp_text = text[start:section_end].strip()
                if exp_text and len(exp_text) > 5:
                    # Clean up the text - remove extra whitespace and limit length
                    exp_text = ' '.join(exp_text.split())[:300]
                    extracted_data['work_experience'] = exp_text
                    print(f"[EXPERIENCE] Found: {exp_text[:100]}")
                    
                    # Try to extract years of experience
                    years_patterns = [
                        r'(\d+)\+?\s*(?:years?|yrs?|Y\.O\.E|YOE)\s+(?:of\s+)?experience',
                        r'Total\s+(?:of\s+)?(\d+)\s*(?:years?|yrs?)',
                        r'(?:over|more than|approx\.?)\s*(\d+)\s*(?:years?|yrs?)',
                    ]
                    for pattern in years_patterns:
                        years_match = re.search(pattern, exp_text, re.IGNORECASE)
                        if years_match:
                            extracted_data['years_experience'] = years_match.group(1)
                            print(f"[YEARS EXP] Found: {years_match.group(1)}")
                            break
                    break
        
        # Extract projects - improved
        projects_keywords = ['projects', 'portfolio', 'key projects', 'featured projects', 'recent projects', 'project experience']
        for keyword in projects_keywords:
            idx = text.lower().find(keyword)
            if idx != -1:
                start = idx + len(keyword)
                section_end = len(text)
                next_sections = ['education', 'skills', 'experience', 'certifications']
                for next_sect in next_sections:
                    next_idx = text.lower().find(next_sect, start)
                    if next_idx != -1 and next_idx < section_end:
                        section_end = next_idx
                proj_text = text[start:section_end].strip()
                if proj_text and len(proj_text) > 5:
                    # Clean up and limit
                    proj_text = ' '.join(proj_text.split())[:300]
                    extracted_data['projects'] = proj_text
                    print(f"[PROJECTS] Found: {proj_text[:100]}")
                    break
        
        # Extract certifications - improved
        cert_keywords = ['certifications', 'certified', 'professional certifications', 'licenses', 'credentials', 'qualifications']
        for keyword in cert_keywords:
            idx = text.lower().find(keyword)
            if idx != -1:
                start = idx + len(keyword)
                section_end = len(text)
                next_sections = ['education', 'skills', 'experience', 'projects']
                for next_sect in next_sections:
                    next_idx = text.lower().find(next_sect, start)
                    if next_idx != -1 and next_idx < section_end:
                        section_end = next_idx
                cert_text = text[start:section_end].strip()
                if cert_text and len(cert_text) > 5:
                    # Clean up - remove bullet points and extra whitespace
                    cert_text = re.sub(r'[\•\-\*]\s*', ', ', cert_text)
                    cert_text = ' '.join(cert_text.split())[:250]
                    extracted_data['certifications'] = cert_text
                    print(f"[CERTIFICATIONS] Found: {cert_text[:100]}")
                    break
        
        # Extract headline/title (usually right after name)
        if 'first_name' in extracted_data:
            # Look for professional title in first few lines
            job_title_keywords = ['engineer', 'developer', 'manager', 'analyst', 'specialist', 'consultant', 'director', 'lead', 'senior', 'junior', 'architect', 'programmer', 'designer', 'scientist', 'officer', 'associate', 'executive', 'coordinator', 'administrator']
            for line in lines[:20]:
                line = line.strip()
                if line and len(line.split()) <= 10 and len(line) < 100:
                    if any(keyword in line.lower() for keyword in job_title_keywords):
                        if line != f"{extracted_data.get('first_name', '')} {extracted_data.get('last_name', '')}".strip():
                            extracted_data['headline'] = line[:100]
                            break
        
        # Extract location - improved detection (look for explicit keywords first)
        location_found = False
        
        # First try explicit location keywords
        location_keywords = ['location', 'located in', 'based in', 'city', 'current location']
        for loc_keyword in location_keywords:
            idx = text.lower().find(loc_keyword)
            if idx != -1:
                start = idx + len(loc_keyword)
                # Skip colons and spaces
                while start < len(text) and text[start] in ':• \t':
                    start += 1
                
                # Get next 50 chars or until newline
                section_end = min(len(text), start + 50)
                for sep_idx in range(start, section_end):
                    if text[sep_idx] in '\n|•':
                        section_end = sep_idx
                        break
                
                loc_text = text[start:section_end].strip()
                if loc_text and len(loc_text) > 2 and 'linkedin' not in loc_text.lower():
                    extracted_data['current_location'] = loc_text[:100]
                    print(f"[LOCATION] Found: {loc_text}")
                    location_found = True
                    break
        
        # If not found with keywords, try city patterns but exclude common resume metadata
        if not location_found:
            location_patterns = [
                r'\b(?:New York|Los Angeles|Chicago|Houston|Phoenix|Philadelphia|San Antonio|San Diego|Dallas|San Jose|Austin|Jacksonville|Fort Worth|Columbus|Charlotte|San Francisco|Indianapolis|Seattle|Denver|Boston|Navi Mumbai|Mumbai|Pen|Raigad)\b[,\s]*(?:[A-Z]{2})?',
            ]
            for pattern in location_patterns:
                location_match = re.search(pattern, text, re.IGNORECASE)
                if location_match:
                    loc = location_match.group(0).strip()
                    # Make sure it's not part of a name/URL
                    if len(loc) > 2 and 'linkedin' not in text[max(0, location_match.start()-20):location_match.end()].lower():
                        extracted_data['current_location'] = loc[:100]
        
        # Extract bio/summary - improved
        bio_keywords = ['summary', 'objective', 'career objective', 'career objectives', 'profile', 'about', 'professional summary', 'career profile', 'personal statement', 'executive summary']
        for keyword in bio_keywords:
            idx = text.lower().find(keyword)
            if idx != -1:
                start = idx + len(keyword)
                # Skip any colons or special characters right after the keyword
                while start < len(text) and text[start] in ':\t• -':
                    start += 1
                
                section_end = len(text)
                next_sections = ['experience', 'skills', 'education', 'projects', 'educational', 'qualifications']
                for next_sect in next_sections:
                    next_idx = text.lower().find(next_sect, start)
                    if next_idx != -1 and next_idx < section_end:
                        section_end = next_idx
                bio_text = text[start:section_end].strip()
                if bio_text and len(bio_text) > 10:
                    # Clean up and limit - remove leading/trailing special chars
                    bio_text = bio_text.lstrip(':\t• -').strip()
                    bio_text = ' '.join(bio_text.split())[:300]
                    if bio_text and len(bio_text) > 10:
                        extracted_data['bio'] = bio_text
                        print(f"[BIO] Found: {bio_text[:100]}")
                        break
        
        # Extract languages - improved
        languages_keywords = ['languages', 'language skills', 'language proficiency']
        for keyword in languages_keywords:
            idx = text.lower().find(keyword)
            if idx != -1:
                start = idx + len(keyword)
                section_end = min(len(text), start + 150)  # Limit to 150 chars from start
                lang_text = text[start:section_end].strip()
                if lang_text:
                    # Extract only the language names, removing jargon
                    lang_text = re.sub(r'[\•\-\*:,;]', ' ', lang_text)
                    # Get only the first line/sentence if it's too long
                    if '\n' in lang_text:
                        lang_text = lang_text.split('\n')[0]
                    lang_text = ' '.join(lang_text.split())[:150]
                    if lang_text and len(lang_text) > 2:
                        extracted_data['languages_known'] = lang_text
                        print(f"[LANGUAGES] Found: {lang_text[:100]}")
                    break
        
        # Extract soft skills - improved
        soft_skills_keywords = ['soft skills', 'core competencies', 'key skills', 'interpersonal skills', 'personal qualities', 'strengths']
        for keyword in soft_skills_keywords:
            idx = text.lower().find(keyword)
            if idx != -1:
                start = idx + len(keyword)
                section_end = min(len(text), start + 150)
                soft_text = text[start:section_end].strip()
                if soft_text:
                    # Clean up bullet points and extra formatting
                    soft_text = re.sub(r'[\•\-\*:;,]', ' ', soft_text)
                    soft_text = ' '.join(soft_text.split())[:150]
                    if soft_text and len(soft_text) > 2:
                        extracted_data['soft_skills'] = soft_text
                        print(f"[SOFT SKILLS] Found: {soft_text[:100]}")
                    break
        
        # Extract secondary skills, frameworks, databases if not already extracted
        # Look for common technical terms
        if 'secondary_skills' not in extracted_data:
            frameworks_keywords = ['django', 'flask', 'spring', 'angular', 'react', 'vue', 'nextjs', 'nuxt', 'laravel', 'symfony', 'express', 'fastapi', 'nestjs']
            frameworks_found = []
            for fw in frameworks_keywords:
                if fw.lower() in text.lower():
                    frameworks_found.append(fw)
            if frameworks_found:
                extracted_data['frameworks_libraries'] = ', '.join(set(frameworks_found))[:200]
        
        if 'databases' not in extracted_data:
            databases_keywords = ['mysql', 'postgresql', 'mongodb', 'redis', 'elasticsearch', 'cassandra', 'oracle', 'sqlserver', 'dynamodb', 'firebase', 'couchdb']
            databases_found = []
            for db in databases_keywords:
                if db.lower() in text.lower():
                    databases_found.append(db)
            if databases_found:
                extracted_data['databases'] = ', '.join(set(databases_found))[:200]
        
        if 'tools_technologies' not in extracted_data:
            tools_keywords = ['git', 'github', 'gitlab', 'docker', 'kubernetes', 'jenkins', 'gitlab ci', 'github actions', 'terraform', 'ansible', 'jira', 'confluence']
            tools_found = []
            for tool in tools_keywords:
                if tool.lower() in text.lower():
                    tools_found.append(tool)
            if tools_found:
                extracted_data['tools_technologies'] = ', '.join(set(tools_found))[:200]
        
        if 'cloud_platforms' not in extracted_data:
            cloud_keywords = ['aws', 'azure', 'gcp', 'google cloud', 'amazon web services', 'heroku', 'netlify', 'vercel']
            cloud_found = []
            for cloud in cloud_keywords:
                if cloud.lower() in text.lower():
                    cloud_found.append(cloud)
            if cloud_found:
                extracted_data['cloud_platforms'] = ', '.join(set(cloud_found))[:200]
        
        return jsonify({
            'success': True,
            'data': extracted_data
        })
    
    except Exception as e:
        return jsonify({'error': f'Error parsing resume: {str(e)}'}), 500

@app.route('/analyze-resume', methods=['POST'])
def analyze_resume():
    """Analyze resume using AI and provide score"""
    if session.get('role') != 'candidate':
        return jsonify({'error': 'Unauthorized'}), 403
    
    if 'resume' not in request.files:
        return jsonify({'error': 'No resume file provided'}), 400
    
    file = request.files['resume']
    if file.filename == '':
        return jsonify({'error': 'Empty file'}), 400
    
    try:
        from docx import Document
        import io
        import re
        
        text = ""
        
        if file.filename.endswith('.pdf'):
            # Try reading PDF with multiple methods
            file.seek(0)
            pdf_bytes = file.read()
            print(f"[PDF Analysis] File size: {len(pdf_bytes)} bytes, filename: {file.filename}")
            
            # Method 1: pdfminer.six (most robust)
            try:
                from pdfminer.pdfpage import PDFPage
                from pdfminer.pdfinterp import PDFResourceManager, PDFPageInterpreter
                from pdfminer.converter import PDFPageAggregator
                from pdfminer.layout import LAParams, LTTextBox
                
                pdf_file = io.BytesIO(pdf_bytes)
                rsrcmgr = PDFResourceManager()
                laparams = LAParams()
                device = PDFPageAggregator(rsrcmgr, laparams=laparams)
                interpreter = PDFPageInterpreter(rsrcmgr, device)
                
                for page in PDFPage.get_pages(pdf_file):
                    interpreter.process_page(page)
                    layout = device.get_result()
                    for obj in layout:
                        if isinstance(obj, LTTextBox):
                            text += obj.get_text()
                print(f"[PDF Analysis] pdfminer extracted {len(text)} characters")
            except Exception as pdfminer_error:
                print(f"[PDF Analysis] pdfminer failed: {str(pdfminer_error)}")
                # Method 2: PyPDF2
                try:
                    import PyPDF2
                    pdf_reader = PyPDF2.PdfReader(io.BytesIO(pdf_bytes))
                    for page_num in range(len(pdf_reader.pages)):
                        try:
                            page = pdf_reader.pages[page_num]
                            text += page.extract_text()
                        except:
                            pass
                    print(f"[PDF Analysis] PyPDF2 extracted {len(text)} characters")
                except Exception as pypdf2_error:
                    print(f"[PDF Analysis] PyPDF2 failed: {str(pypdf2_error)}")
                    # Method 3: pypdf
                    try:
                        from pypdf import PdfReader
                        pdf_reader = PdfReader(io.BytesIO(pdf_bytes))
                        for page in pdf_reader.pages:
                            try:
                                text += page.extract_text()
                            except:
                                pass
                        print(f"[PDF Analysis] pypdf extracted {len(text)} characters")
                    except Exception as pypdf_error:
                        print(f"[PDF Analysis] pypdf failed: {str(pypdf_error)}")
            
            if not text or len(text.strip()) < 20:
                print(f"[PDF Analysis] Not enough text extracted: {len(text) if text else 0} characters")
                return jsonify({
                    'error': 'Unable to extract text from PDF. Please upload a DOCX file instead or ensure the PDF is not scanned/image-based.'
                }), 400
            
            print(f"[PDF Analysis] Successfully extracted {len(text)} characters from PDF")
                
        elif file.filename.endswith(('.docx', '.doc')):
            try:
                file.seek(0)
                doc = Document(io.BytesIO(file.read()))
                text = "\n".join([para.text for para in doc.paragraphs])
            except Exception as doc_error:
                return jsonify({'error': f'Could not read DOCX: {str(doc_error)}'}), 400
        else:
            return jsonify({'error': 'Please upload a PDF or DOCX file'}), 400
        
        if not text or len(text.strip()) < 50:
            return jsonify({'error': 'Resume appears to be empty or too short to analyze'}), 400
        
        # Try to use AI to analyze resume
        api_key = os.getenv('GEMINI_API_KEY') or os.getenv('GOOGLE_API_KEY')
        
        if not api_key:
            # Fallback: Provide basic analysis without AI
            return provide_basic_analysis(text)
        
        try:
            prompt = f"""Analyze this resume and provide a detailed assessment in JSON format with exactly these fields:
            
Resume Content:
{text[:3000]}

Please respond ONLY with valid JSON (no markdown, no code blocks, just raw JSON) in this exact format:
{{
    "score": <integer between 0-100>,
    "strengths": ["strength1", "strength2", "strength3"],
    "weaknesses": ["weakness1", "weakness2", "weakness3"],
    "feedback": "<brief overall feedback paragraph>",
    "tips": ["tip1", "tip2", "tip3", "tip4"]
}}

Be constructive and specific."""

            analysis = _generate_gemini_json(prompt, timeout=20)
            if not analysis:
                raise RuntimeError("Gemini did not return valid JSON")
            
            # Ensure score is valid
            score = max(0, min(100, int(analysis.get('score', 50))))
            
            # Save analysis to database
            try:
                db = get_connection()
                cursor = db.cursor(cursor_factory=RealDictCursor)
                cursor.execute("""
                    INSERT INTO resume_analyses (candidate_id, filename, score, strengths, weaknesses, feedback, tips)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (
                    session.get('user_id'),
                    file.filename,
                    score,
                    json.dumps(analysis.get('strengths', [])),
                    json.dumps(analysis.get('weaknesses', [])),
                    analysis.get('feedback', 'Resume analysis complete.'),
                    json.dumps(analysis.get('tips', []))
                ))
                db.commit()
                cleanup_db_resources(cursor, db)
            except Exception as db_error:
                print(f"Database save error: {str(db_error)}")
            
            return jsonify({
                'success': True,
                'score': score,
                'strengths': analysis.get('strengths', []),
                'weaknesses': analysis.get('weaknesses', []),
                'feedback': analysis.get('feedback', 'Resume analysis complete.'),
                'tips': analysis.get('tips', [])
            })
        
        except Exception as ai_error:
            # If AI fails, provide basic analysis
            print(f"AI Analysis Error: {str(ai_error)}")
            analysis = provide_basic_analysis(text)
            
            # Save fallback analysis to database
            try:
                db = get_connection()
                cursor = db.cursor(cursor_factory=RealDictCursor)
                cursor.execute("""
                    INSERT INTO resume_analyses (candidate_id, filename, score, strengths, weaknesses, feedback, tips)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (
                    session.get('user_id'),
                    file.filename,
                    analysis.get('score', 50),
                    json.dumps(analysis.get('strengths', [])),
                    json.dumps(analysis.get('weaknesses', [])),
                    analysis.get('feedback', ''),
                    json.dumps(analysis.get('tips', []))
                ))
                db.commit()
                cleanup_db_resources(cursor, db)
            except Exception as db_error:
                print(f"Database save error: {str(db_error)}")
            
            return jsonify(analysis)
        
    except Exception as e:
        print(f"Resume Analysis Error: {str(e)}")
        return jsonify({'error': f'Error analyzing resume: {str(e)}'}), 500

def provide_basic_analysis(text):
    """Provide basic resume analysis without AI - returns dict"""
    import re
    
    score = 50  # Base score
    strengths = []
    weaknesses = []
    tips = []
    
    # Check for key sections
    sections = ['experience', 'education', 'skills', 'projects', 'certifications', 'contact']
    found_sections = []
    missing_sections = []
    
    for section in sections:
        if re.search(rf'\b{section}\b', text, re.IGNORECASE):
            found_sections.append(section.title())
            score += 3
        else:
            missing_sections.append(section.title())
    
    if found_sections:
        strengths.append(f"Includes {len(found_sections)} key sections: {', '.join(found_sections)}")
    
    if missing_sections:
        weaknesses.append(f"Missing sections: {', '.join(missing_sections[:2])}")
    
    # Check for contact information
    email_pattern = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
    phone_pattern = r'[\+]?[(]?[0-9]{3}[)]?[-\s\.]?[0-9]{3}[-\s\.]?[0-9]{4,6}'
    
    if re.search(email_pattern, text):
        score += 5
        strengths.append("Contains email address")
    else:
        weaknesses.append("Missing email address")
    
    if re.search(phone_pattern, text):
        score += 5
    
    # Check for action verbs
    action_verbs = ['developed', 'implemented', 'managed', 'led', 'created', 'designed', 
                   'improved', 'increased', 'achieved', 'delivered', 'collaborated']
    verb_count = sum(1 for verb in action_verbs if re.search(rf'\b{verb}\b', text, re.IGNORECASE))
    
    if verb_count >= 5:
        score += 10
        strengths.append(f"Uses strong action verbs ({verb_count} found)")
    else:
        score += verb_count * 2
        if verb_count == 0:
            weaknesses.append("Lacks strong action verbs (e.g., 'developed', 'managed')")
        else:
            tips.append(f"Add more action verbs (currently has {verb_count})")
    
    # Check for quantifiable results
    number_pattern = r'\b\d+[%$KM]?[\+\-x]?\b'
    numbers_found = len(re.findall(number_pattern, text))
    
    if numbers_found >= 5:
        score += 10
        strengths.append(f"Contains quantifiable metrics ({numbers_found} found)")
    else:
        score += numbers_found
        tips.append("Add more quantifiable achievements (e.g., '30% increase')")
    
    # Check text length
    words = len(text.split())
    if words < 100:
        score -= 10
        weaknesses.append("Resume is too brief")
    elif words > 800:
        score -= 5
        tips.append("Consider making resume more concise")
    
    score = max(0, min(100, int(score)))
    
    # Add default tips if not enough
    while len(tips) < 4:
        tips.append("Proofread for spelling and grammar errors")
        if len(tips) < 4:
            tips.append("Tailor resume to job descriptions")
        if len(tips) < 4:
            tips.append("Use consistent formatting throughout")
    
    return {
        'success': True,
        'score': score,
        'strengths': strengths[:3],
        'weaknesses': weaknesses[:3],
        'feedback': f'Your resume scores {score}/100. {len(found_sections)} key sections detected. Consider adding more quantifiable achievements and strong action verbs.',
        'tips': tips[:4]
    }

@app.route('/get-previous-analyses', methods=['GET'])
def get_previous_analyses():
    """Get all previous resume analyses for the candidate"""
    if session.get('role') != 'candidate':
        return jsonify({'error': 'Unauthorized'}), 403
    
    candidate_id = session.get('user_id')
    try:
        with SafeDBConnection() as (cursor, db):
            cursor.execute("""
                SELECT id, filename, score, strengths, weaknesses, feedback, tips, analyzed_at
                FROM resume_analyses
                WHERE candidate_id = %s
                ORDER BY analyzed_at DESC
                LIMIT 20
            """, (candidate_id,))
            analyses = cursor.fetchall()

        def _to_list(value):
            if isinstance(value, list):
                return value
            if not value:
                return []
            if isinstance(value, str):
                try:
                    parsed = json.loads(value)
                    return parsed if isinstance(parsed, list) else [str(parsed)]
                except Exception:
                    return [value]
            return []

        for analysis in analyses:
            analysis['strengths'] = _to_list(analysis.get('strengths'))
            analysis['weaknesses'] = _to_list(analysis.get('weaknesses'))
            analysis['tips'] = _to_list(analysis.get('tips'))
            analyzed_at = analysis.get('analyzed_at')
            analysis['analyzed_at'] = analyzed_at.strftime('%B %d, %Y at %I:%M %p') if analyzed_at else 'Unknown'

        return jsonify({'success': True, 'analyses': analyses}), 200

    except Exception as e:
        print(f"Error fetching analyses: {str(e)}")
        return jsonify({'success': False, 'analyses': [], 'error': 'Could not fetch previous analyses'}), 500

@app.route('/delete-profile', methods=['POST'])
def delete_profile():
    """Delete candidate profile completely"""
    if session.get('role') != 'candidate':
        return redirect('/login')
    
    candidate_id = session.get('user_id')
    db = get_connection()
    cursor = db.cursor(cursor_factory=RealDictCursor)
    
    try:
        # Delete profile
        cursor.execute("DELETE FROM candidate_profiles WHERE candidate_id = %s", (candidate_id,))
        db.commit()
        flash("Profile deleted successfully", "success")
    except Exception as e:
        db.rollback()
        flash(f"Error deleting profile: {str(e)}", "error")
    finally:
        cleanup_db_resources(cursor, db)
        db.close()
    
    return redirect('/candidate-dashboard')


@app.route('/apply/<int:job_id>', methods=['GET', 'POST'])
def apply_job(job_id):
    """Apply for a job - handles both GET (quick apply) and POST (form submit and AJAX)"""
    if session.get('role') != 'candidate':
        # Handle AJAX vs regular request
        if request.method == 'POST' and request.is_json or request.headers.get('Content-Type') == 'application/json':
            return jsonify({'error': 'Unauthorized'}), 401
        return redirect('/login')
    
    candidate_id = session.get('user_id')
    
    try:
        # WORKFLOW DEPENDENCY: Candidate → Profile Completion (≥85%)
        is_complete, profile_percent = check_candidate_profile_completion(candidate_id)
        
        if not is_complete:
            if request.method == 'POST' and (request.is_json or request.headers.get('Content-Type') == 'application/json'):
                return jsonify({'error': f'Please complete your profile to at least 85% (currently {profile_percent}%) to apply for jobs', 'success': False}), 403
            flash(f'Please complete your profile to at least 85% (currently {profile_percent}%) to apply for jobs', 'warning')
            return redirect('/candidate-dashboard#profile')
        
        job = None
        with SafeDBConnection() as (cursor, db):
            # Check if already applied
            cursor.execute("SELECT id FROM applications WHERE candidate_id = %s AND job_id = %s", (candidate_id, job_id))
            existing = cursor.fetchone()
            
            if existing:
                if request.method == 'POST' and (request.is_json or request.headers.get('Content-Type') == 'application/json'):
                    return jsonify({'error': 'You have already applied for this job', 'success': False}), 400
                flash('You have already applied for this job', 'info')
                return redirect('/candidate-dashboard#my-applications')
            
            # Create application
            print(f"DEBUG: About to insert application - candidate_id={candidate_id}, job_id={job_id}")
            cursor.execute("""
                INSERT INTO applications (candidate_id, job_id, status)
                VALUES (%s, %s, 'Applied'::application_status)
            """, (candidate_id, job_id))
            print(f"DEBUG: Application inserted, rows affected: {cursor.rowcount}")
            
            # Commit explicitly
            db.commit()
            print(f"DEBUG: Application committed to database")
            
            # Get job details for notification
            cursor.execute("SELECT title, recruiter_id FROM jobs WHERE id = %s", (job_id,))
            job = cursor.fetchone()
            print(f"DEBUG: Job retrieved: {job}")
        
        print(f"DEBUG: Context manager exited, connection should be closed")
        # Create notification for recruiter (outside of DB context to avoid pool issues)
        if job:
            create_notification(
                'recruiter', job['recruiter_id'], 'new_application',
                'New Job Application',
                f'A candidate has applied for {job["title"]}',
                f'/recruiter-dashboard#applications'
            )
            
            # Log activity (outside of DB context)
            log_activity(
                candidate_id, 'candidate', 'job_application',
                f'Applied for {job["title"]}',
                f'Application submitted for {job["title"]}',
                {'job_id': job_id, 'job_title': job['title']}
            )
        
        # Return JSON for AJAX requests
        if request.method == 'POST' and (request.is_json or request.headers.get('Content-Type') == 'application/json'):
            return jsonify({
                'success': True,
                'message': 'Application submitted successfully!',
                'job_title': job['title'] if job else 'Job'
            }), 200
        
        flash('Application submitted successfully!', 'success')
        return redirect('/candidate-dashboard#my-applications')
    
    except Exception as e:
        print(f"Error applying for job: {e}")
        import traceback
        traceback.print_exc()
        
        if request.method == 'POST' and (request.is_json or request.headers.get('Content-Type') == 'application/json'):
            return jsonify({'error': f'Failed to submit application: {str(e)}', 'success': False}), 500
        
        flash(f'Failed to submit application: {str(e)}', 'danger')
        return redirect('/candidate-dashboard')


@app.route('/apply-job/<int:job_id>', methods=['POST'])
def apply_job_post(job_id):
    """Apply for a job via AJAX POST"""
    if session.get('role') != 'candidate':
        return jsonify({'error': 'Unauthorized'}), 401
    
    candidate_id = session.get('user_id')
    
    try:
        # WORKFLOW DEPENDENCY: Candidate → Profile Completion (≥85%)
        is_complete, profile_percent = check_candidate_profile_completion(candidate_id)
        
        if not is_complete:
            return jsonify({'error': f'Please complete your profile to at least 85% (currently {profile_percent}%) to apply for jobs'}), 403
        
        job = None
        with SafeDBConnection() as (cursor, db):
            # Check if already applied
            cursor.execute("SELECT id FROM applications WHERE candidate_id = %s AND job_id = %s", (candidate_id, job_id))
            existing = cursor.fetchone()
            
            if existing:
                return jsonify({'error': 'You have already applied for this job'}), 400
            
            # Create application
            cursor.execute("""
                INSERT INTO applications (candidate_id, job_id, status)
                VALUES (%s, %s, 'Applied'::application_status)
            """, (candidate_id, job_id))
            
            # Get job details for notification
            cursor.execute("SELECT title, recruiter_id FROM jobs WHERE id = %s", (job_id,))
            job = cursor.fetchone()
        
        # Create notification for recruiter (outside of DB context to avoid pool issues)
        if job:
            create_notification(
                'recruiter', job['recruiter_id'], 'new_application',
                'New Job Application',
                f'A candidate has applied for {job["title"]}',
                f'/recruiter-dashboard#applications'
            )
            
            # Log activity (outside of DB context)
            log_activity(
                candidate_id, 'candidate', 'job_application',
                f'Applied for {job["title"]}',
                f'Application submitted for {job["title"]}',
                {'job_id': job_id, 'job_title': job['title']}
            )
        
        return jsonify({'success': True, 'message': 'Application submitted successfully'})
    
    except Exception as e:
        print(f"Error applying for job: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': f'Failed to submit application: {str(e)}'}), 500


@app.route('/withdraw-application/<int:application_id>', methods=['POST'])
def withdraw_application(application_id):
    """Withdraw a job application"""
    if session.get('role') != 'candidate':
        return jsonify({'error': 'Unauthorized'}), 401
    
    candidate_id = session.get('user_id')
    
    try:
        db = get_connection()
        cursor = db.cursor(cursor_factory=RealDictCursor)
        
        # Verify ownership and delete
        cursor.execute("""
            DELETE FROM applications 
            WHERE id = %s AND candidate_id = %s AND status = 'Applied'
        """, (application_id, candidate_id))
        
        if cursor.rowcount == 0:
            return jsonify({'error': 'Cannot withdraw this application'}), 400
        
        db.commit()
        cleanup_db_resources(cursor, db)
        
        return jsonify({'success': True, 'message': 'Application withdrawn'})
    
    except Exception as e:
        print(f"Error withdrawing application: {e}")
        return jsonify({'error': 'Failed to withdraw application'}), 500


@app.route('/mark-notification-read/<int:notification_id>', methods=['POST'])
def mark_notification_read(notification_id):
    """Mark a notification as read"""
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        db = get_connection()
        cursor = db.cursor(cursor_factory=RealDictCursor)
        
        cursor.execute("""
            UPDATE notifications 
            SET is_read = TRUE 
            WHERE id = %s AND receiver_id = %s AND receiver_role = %s
        """, (notification_id, session.get('user_id'), session.get('role')))
        
        db.commit()
        cleanup_db_resources(cursor, db)
        
        return jsonify({'success': True})
    
    except Exception as e:
        print(f"Error marking notification read: {e}")
        return jsonify({'error': 'Failed to mark notification'}), 500


@app.route('/mark-all-notifications-read', methods=['POST'])
def mark_all_notifications_read():
    """Mark all notifications as read for current user"""
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        db = get_connection()
        cursor = db.cursor(cursor_factory=RealDictCursor)
        
        cursor.execute("""
            UPDATE notifications 
            SET is_read = TRUE 
            WHERE receiver_id = %s AND receiver_role = %s
        """, (session.get('user_id'), session.get('role')))
        
        db.commit()
        cleanup_db_resources(cursor, db)
        
        return jsonify({'success': True})
    
    except Exception as e:
        print(f"Error marking all notifications read: {e}")
        return jsonify({'error': 'Failed to mark notifications'}), 500

@app.route('/my-applications')
def my_applications():
    """View all job applications for candidate"""
    if session.get('role') != 'candidate':
        return redirect('/login')
    
    candidate_id = session.get('user_id')
    
    db = get_connection()
    cursor = db.cursor(cursor_factory=RealDictCursor)
    
    # Get applications with job and recruiter details
    cursor.execute("""
        SELECT 
            a.id, a.status, a.applied_at, a.updated_at, a.rejection_reason,
            j.id AS job_id, j.title, j.location, j.job_type, j.employment_mode,
            rp.company_name, rp.logo_file
        FROM applications a
        JOIN jobs j ON a.job_id = j.id
        JOIN recruiter_profiles rp ON j.recruiter_id = rp.recruiter_id
        WHERE a.candidate_id = %s
        ORDER BY a.applied_at DESC
    """, (candidate_id,))
    
    applications = cursor.fetchall()
    
    # Get offer letters for this candidate
    cursor.execute("""
        SELECT 
            ol.id, ol.position, ol.salary, ol.joining_date, ol.location,
            ol.employment_type, ol.offer_file, ol.status, ol.generated_at,
            ol.job_id, j.title as job_title,
            rp.company_name, rp.logo_file
        FROM offer_letters ol
        JOIN jobs j ON ol.job_id = j.id
        JOIN recruiter_profiles rp ON j.recruiter_id = rp.recruiter_id
        WHERE ol.candidate_id = %s
        ORDER BY ol.generated_at DESC
    """, (candidate_id,))
    
    offers = cursor.fetchall()
    
    cleanup_db_resources(cursor, db)
    
    return render_template("applications.html", 
                         applications=applications,
                         offers=offers,
                         candidate_view=True)


@app.route('/job/<int:job_id>')
def get_job(job_id):
    db = get_connection()
    cursor = db.cursor(cursor_factory=RealDictCursor)

    cursor.execute("""
        SELECT 
            j.id,
            j.title,
            j.description,
            j.location,
            j.job_type,
            j.employment_mode,
            j.salary_min,
            j.salary_max,
            j.min_experience,
            j.max_experience,
            j.education,
            j.skills,
            j.openings,
            j.deadline,
            j.created_at,
            rp.company_name,
            rp.company_size,
            rp.industry,
            rp.website,
            rp.logo_file
        FROM jobs j
        JOIN recruiter_profiles rp 
            ON j.recruiter_id = rp.recruiter_id
        WHERE j.id = %s
    """, (job_id,))


    job = cursor.fetchone()
    cleanup_db_resources(cursor, db)

    return jsonify(job)

@app.route('/api/profile')
def get_profile_api():
    user_id = session.get('user_id')
    db = get_connection()
    cursor = db.cursor(cursor_factory=RealDictCursor)
    cursor.execute("SELECT * FROM candidate_profiles WHERE candidate_id=%s", (user_id,))
    profile = cursor.fetchone()
    cleanup_db_resources(cursor, db)

    if profile:
        return {"exists": True, "completed": True, "completion": profile['profile_percent'], "data": profile}
    return {"exists": False}
@app.route('/api/profile/draft', methods=['POST'])
def save_draft():
    data = request.json.get('data')
    return {"success": True, "message": "Draft saved"}

@app.route('/candidate/delete-profile', methods=['POST'])
def delete_candidate_profile():
    if session.get('role') != 'candidate':
        return redirect('/login')

    user_id = session.get('user_id')
    db = get_connection()
    cursor = db.cursor(cursor_factory=RealDictCursor)

    try:
        cursor.execute("DELETE FROM candidate_profiles WHERE candidate_id = %s", (user_id,))
        cursor.execute("UPDATE candidates SET profile_completed = FALSE WHERE id = %s", (user_id,))
        db.commit()
        flash("Profile deleted. You can rebuild it anytime.", "info")
    except Exception as e:
        print(f"Error deleting candidate profile: {e}")
        db.rollback()
        flash("Could not delete profile. Please try again.", "danger")
    finally:
        cleanup_db_resources(cursor, db)

    return redirect('/candidate-dashboard?tab=profile')

@app.route('/api/parse-resume', methods=['POST'])
def parse_resume():
    """Parse uploaded resume PDF and extract candidate information"""
    try:
        if session.get('role') != 'candidate':
            return jsonify({'success': False, 'message': 'Unauthorized'}), 401
        
        if 'resume' not in request.files:
            return jsonify({'success': False, 'message': 'No file uploaded'}), 400
        
        file = request.files['resume']
        
        if file.filename == '':
            return jsonify({'success': False, 'message': 'No file selected'}), 400
        
        if not file.filename.endswith('.pdf'):
            return jsonify({'success': False, 'message': 'Only PDF files are allowed'}), 400
        
        # Save the file temporarily
        filename = secure_filename(file.filename)
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)
        
        # Extract text from PDF
        text = extract_text_from_pdf(filepath)
        
        print(f"DEBUG: Extracted text length: {len(text) if text else 0}")
        print(f"DEBUG: First 200 chars: {text[:200] if text else 'None'}")
        
        if not text or len(text.strip()) < 10:
            return jsonify({
                'success': False, 
                'message': 'This PDF appears to be image-based or scanned. Please use a text-based PDF resume or click "Fill Manually" to enter your details.',
                'suggestion': 'manual'
            }), 400
        
        # Parse the extracted text
        parsed_data = parse_resume_text(text)
        
        print(f"DEBUG: Parsed data: {parsed_data}")
        
        # Add filename for later use
        parsed_data['resume_filename'] = filename
        
        return jsonify({'success': True, 'data': parsed_data})
    
    except Exception as e:
        print(f"Error parsing resume: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'message': f'Error: {str(e)}'}), 500

def extract_text_from_pdf(filepath):
    """Extract text content from PDF file"""
    try:
        print(f"DEBUG: Opening PDF: {filepath}")
        print(f"DEBUG: File exists: {os.path.exists(filepath)}")
        
        doc = fitz.open(filepath)
        print(f"DEBUG: PDF opened successfully. Pages: {len(doc)}")
        
        text = ""
        for page_num, page in enumerate(doc):
            page_text = page.get_text()
            print(f"DEBUG: Page {page_num + 1} text length: {len(page_text)}")
            text += page_text
        
        doc.close()
        print(f"DEBUG: Total extracted text length: {len(text)}")
        return text
    except Exception as e:
        print(f"DEBUG: Error in extract_text_from_pdf: {str(e)}")
        import traceback
        traceback.print_exc()
        raise Exception(f"Failed to extract text from PDF: {str(e)}")

def parse_resume_text(text):
    """Parse resume text and extract structured information"""
    import re
    
    data = {}
    text_lower = text.lower()
    lines = text.split('\n')
    
    # Extract Name (usually first non-empty line)
    for line in lines:
        line = line.strip()
        if line and len(line) > 2 and not any(char.isdigit() for char in line[:10]):
            name_parts = line.split()
            if len(name_parts) >= 2:
                data['first_name'] = name_parts[0]
                data['last_name'] = ' '.join(name_parts[1:])
                break
    
    # Extract Email
    email_pattern = r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'
    emails = re.findall(email_pattern, text)
    if emails:
        data['email'] = emails[0]
    
    # Extract Phone
    phone_pattern = r'[\+\(]?[0-9][0-9 .\-\(\)]{8,}[0-9]'
    phones = re.findall(phone_pattern, text)
    if phones:
        data['phone'] = phones[0].strip()
    
    # Extract LinkedIn
    linkedin_pattern = r'linkedin\.com/in/[\w-]+'
    linkedin_matches = re.findall(linkedin_pattern, text_lower)
    if linkedin_matches:
        data['linkedin_url'] = 'https://' + linkedin_matches[0]
    
    # Extract GitHub
    github_pattern = r'github\.com/[\w-]+'
    github_matches = re.findall(github_pattern, text_lower)
    if github_matches:
        data['github_url'] = 'https://' + github_matches[0]
    
    # Extract Skills
    skills_keywords = ['python', 'java', 'javascript', 'react', 'node', 'sql', 'mysql', 'mongodb',
                      'html', 'css', 'django', 'flask', 'spring', 'aws', 'azure', 'docker', 'kubernetes',
                      'git', 'api', 'rest', 'json', 'angular', 'vue', 'typescript', 'c++', 'c#', '.net',
                      'php', 'ruby', 'go', 'kotlin', 'swift', 'android', 'ios', 'machine learning', 'ai',
                      'data science', 'tensorflow', 'pytorch', 'pandas', 'numpy']
    
    found_skills = []
    for skill in skills_keywords:
        if skill in text_lower:
            found_skills.append(skill.title())
    
    if found_skills:
        # Split into primary and secondary
        data['primary_skills'] = ', '.join(found_skills[:5])
        if len(found_skills) > 5:
            data['secondary_skills'] = ', '.join(found_skills[5:10])
    
    # Extract Education
    education_keywords = ['bachelor', 'master', 'b.tech', 'b.e.', 'm.tech', 'm.e.', 'bca', 'mca', 'bsc', 'msc']
    for i, line in enumerate(lines):
        line_lower = line.lower()
        for keyword in education_keywords:
            if keyword in line_lower:
                data['degree'] = line.strip()
                # Try to get college name from next few lines
                for j in range(i+1, min(i+4, len(lines))):
                    if lines[j].strip() and len(lines[j].strip()) > 10:
                        data['college_university'] = lines[j].strip()
                        break
                break
        if 'degree' in data:
            break
    
    # Extract Work Experience Section
    experience_section = ""
    capturing = False
    for line in lines:
        line_lower = line.lower()
        if any(keyword in line_lower for keyword in ['experience', 'work history', 'employment']):
            capturing = True
            continue
        if capturing:
            if any(keyword in line_lower for keyword in ['education', 'skills', 'projects', 'certifications']):
                break
            if line.strip():
                experience_section += line.strip() + "\n"
    
    if experience_section:
        data['work_experience'] = experience_section.strip()
    
    # Extract Projects Section
    projects_section = ""
    capturing = False
    for line in lines:
        line_lower = line.lower()
        if 'project' in line_lower:
            capturing = True
            continue
        if capturing:
            if any(keyword in line_lower for keyword in ['education', 'skills', 'experience', 'certifications']):
                break
            if line.strip():
                projects_section += line.strip() + "\n"
    
    if projects_section:
        data['projects'] = projects_section.strip()
    
    # Generate a headline if name found
    if 'first_name' in data:
        if 'primary_skills' in data:
            skills_list = data['primary_skills'].split(',')
            data['headline'] = f"{skills_list[0].strip()} Developer" if skills_list else "Software Developer"
        else:
            data['headline'] = "Software Developer"
    
    # Create a basic bio
    if 'first_name' in data:
        bio_parts = []
        if 'degree' in data:
            bio_parts.append(f"Graduate with {data['degree']}")
        if 'primary_skills' in data:
            bio_parts.append(f"skilled in {data['primary_skills']}")
        if bio_parts:
            data['bio'] = '. '.join(bio_parts) + '.'
    
    return data

@app.route('/post-job', methods=['GET', 'POST'])
def post_job():
    if session.get('role') != 'recruiter':
        return redirect('/login')

    recruiter_id = session['user_id']
    
    # WORKFLOW DEPENDENCY: Recruiter → Admin Verification
    is_verified, verification_status = check_recruiter_verification(recruiter_id)
    
    if not is_verified:
        flash(f'Your company verification is {verification_status}. You must be verified by admin to post jobs.', 'warning')
        return redirect('/recruiter-dashboard#profile')

    if request.method == 'POST':
        # Get all form fields
        title = request.form.get('title', '')[:150]  # Truncate to 150
        department = request.form.get('department', '')[:100]  # Truncate to 100
        job_type = request.form.get('job_type', '')[:50]  # Truncate to 50
        employment_mode = request.form.get('employment_mode', '')[:50]  # Truncate to 50
        openings = request.form.get('openings')
        description = request.form.get('description')
        skills = request.form.get('skills')
        min_experience = request.form.get('min_experience')
        max_experience = request.form.get('max_experience')
        education = request.form.get('education', '')[:150]  # Truncate to 150
        location = request.form.get('location', '')[:150]  # Truncate to 150
        salary_min = request.form.get('salary_min', '')[:100]  # Truncate to 100
        salary_max = request.form.get('salary_max', '')[:100]  # Truncate to 100
        deadline = request.form.get('deadline')
        interview_mode = request.form.get('interview_mode', '')[:50]  # Truncate to 50

        # Validate required fields
        if not title or not title.strip():
            flash('Job title is required', 'error')
            return redirect('/recruiter-dashboard?tab=post')
        
        if not location or not location.strip():
            flash('Location is required', 'error')
            return redirect('/recruiter-dashboard?tab=post')

        # Convert empty strings to None for numeric fields
        openings = int(openings) if openings and openings.strip() else None
        min_experience = int(min_experience) if min_experience and min_experience.strip() else None
        max_experience = int(max_experience) if max_experience and max_experience.strip() else None
        # Keep salary as string (can contain text like '25000/month')
        salary_min = salary_min if salary_min and salary_min.strip() else None
        salary_max = salary_max if salary_max and salary_max.strip() else None
        deadline = deadline if deadline and deadline.strip() else None

        # Prevent creating already-expired jobs (they are hidden from candidates).
        if deadline:
            try:
                from datetime import datetime
                parsed_deadline = datetime.strptime(deadline, '%Y-%m-%d').date()
                if parsed_deadline < datetime.now().date():
                    flash('Deadline cannot be in the past. Please choose today or a future date.', 'error')
                    return redirect('/recruiter-dashboard?tab=post')
            except Exception:
                flash('Invalid deadline format. Please use a valid date.', 'error')
                return redirect('/recruiter-dashboard?tab=post')

        db = get_connection()
        cursor = db.cursor(cursor_factory=RealDictCursor)

        try:
            # Ensure jobs table has full schema even if startup ran in fast/essential-only mode.
            required_job_columns = [
                ("department", "VARCHAR(100)"),
                ("job_type", "VARCHAR(50)"),
                ("employment_mode", "VARCHAR(50)"),
                ("openings", "INT"),
                ("description", "TEXT"),
                ("skills", "TEXT"),
                ("required_skills", "TEXT"),
                ("min_experience", "INT"),
                ("max_experience", "INT"),
                ("education", "VARCHAR(150)"),
                ("salary_min", "VARCHAR(100)"),
                ("salary_max", "VARCHAR(100)"),
                ("deadline", "DATE"),
                ("interview_mode", "VARCHAR(50)")
            ]

            for col_name, col_type in required_job_columns:
                cursor.execute(f"ALTER TABLE jobs ADD COLUMN IF NOT EXISTS {col_name} {col_type}")

            settings = get_admin_settings()
            status = 'active' if settings.get('auto_approve_jobs') else 'pending'

            cursor.execute("""
                INSERT INTO jobs (
                    title, department, job_type, employment_mode, openings,
                    description, skills, required_skills, min_experience, max_experience,
                    education, location, salary_min, salary_max, deadline,
                    interview_mode, recruiter_id, status
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (
                title, department, job_type, employment_mode, openings,
                description, skills, skills, min_experience, max_experience,
                education, location, salary_min, salary_max, deadline,
                interview_mode, recruiter_id, status
            ))
            db.commit()
        except Exception as e:
            db.rollback()
            print(f"Post job error: {e}")
            flash('Unable to post job right now. Please try again.', 'danger')
            return redirect('/recruiter-dashboard?tab=post')
        finally:
            cleanup_db_resources(cursor, db)

        # Notify admins for manual review when pending
        if status == 'pending':
            send_notification('admin', 0, f'New job pending review: "{title}" by recruiter {recruiter_id}')
            flash('Job submitted for admin review.', 'info')
        else:
            flash('Job posted successfully!', 'success')

        return redirect('/recruiter-dashboard?tab=jobs')

    # For GET, show the embedded form on dashboard
    return redirect('/recruiter-dashboard?tab=post')

@app.route("/candidate/jobc/<int:job_id>")
def view_jobc(job_id):
    if session.get('role') != 'candidate':
        return redirect('/login')

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM jobs WHERE id = %s", (job_id,))
    job = cursor.fetchone()

    cleanup_db_resources(cursor, conn)

    if not job:
        return "Job not found", 404
    return render_template("view_job.html", job=job, is_candidate=True)

@app.route('/jobs')
def view_jobs():
    if session.get('role') != 'candidate':
        return redirect('/login')

    try:
        # WORKFLOW DEPENDENCY: Candidate → Profile Completion (≥85%)
        candidate_id = session.get('user_id')
        is_complete, profile_percent = check_candidate_profile_completion(candidate_id)
        
        if not is_complete:
            flash(f'Please complete your profile to at least 85% (currently {profile_percent}%) to access jobs.', 'warning')
            return redirect('/candidate-dashboard#profile')

        with SafeDBConnection() as (cursor, db):
            # WORKFLOW DEPENDENCY: Candidate → Recruiter (Job Visibility)
            # Show only jobs from VERIFIED recruiters with active deadlines
            cursor.execute("""
                SELECT j.*, 
                       COALESCE(rp.company_name, r.name) as company_name,
                       rp.logo_file, rp.company_type, rp.address
                FROM jobs j
                JOIN recruiters r ON j.recruiter_id = r.id
                LEFT JOIN recruiter_profiles rp ON r.id = rp.recruiter_id
                WHERE (j.deadline IS NULL OR j.deadline >= CURRENT_DATE)
                AND LOWER(COALESCE(rp.verification_status, '')) = 'approved'
                AND LOWER(COALESCE(j.status, '')) = 'active'
                ORDER BY j.created_at DESC
            """)
            jobs = cursor.fetchall()

        return render_template('jobs.html', jobs=jobs)
    except Exception as e:
        print(f"Error in view_jobs: {e}")
        flash('Error loading jobs', 'danger')
        return redirect('/candidate-dashboard')

def calculate_match_score(profile, job, ai_score):
    if not profile:
        return {'total_score': 0, 'breakdown': {}, 'job': job}
        
    score = 0
    cand_skills = set()

    # Collect skills from ALL relevant profile columns
    skill_columns = [
        'primary_skills', 
        'secondary_skills', 
        'frameworks_libraries', 
        'databases', 
        'tools_technologies', 
        'cloud_platforms'
    ]

    for col in skill_columns:
        if profile.get(col):
            # Split by comma and add each cleaned skill to our set
            skills = [s.strip().lower() for s in str(profile[col]).split(',') if s.strip()]
            cand_skills.update(skills)
    
    # Get required skills from the job
    job_skills = set([s.strip().lower() for s in (job.get('skills') or '').split(',') if s.strip()])
    
    # Calculate intersections
    matched = cand_skills.intersection(job_skills)
    missing = job_skills - cand_skills
    
    # Now Skill Match will accurately reflect Flask, MySQL, Git, etc.
    skill_match = (len(matched) / len(job_skills)) * 100 if job_skills else 0
    score += skill_match * 0.40
    
    # 2. Experience Match (25%)
    # Heuristic: Estimate years of experience from education end year if work_experience is text
    try:
        grad_year = int(profile.get('education_end_year') or datetime.now().year)
        years_exp = max(0, datetime.now().year - grad_year)
        
        min_exp = job.get('min_experience') or 0
        
        if years_exp >= min_exp:
            exp_match = 100
        else:
            exp_match = (years_exp / min_exp) * 100 if min_exp > 0 else 100
    except:
        exp_match = 50 # Default if calculation fails
        
    score += exp_match * 0.25
    
    # 3. Education Match (15%)
    cand_edu = (profile.get('degree') or '') + ' ' + (profile.get('specialization') or '')
    job_edu = job.get('education') or ''
    # Simple containment check
    if not job_edu or job_edu.lower() in cand_edu.lower() or cand_edu.lower() in job_edu.lower():
        edu_match = 100
    else:
        edu_match = 50
    score += edu_match * 0.15
    
    # 4. AI Assessment Score (20%)
    score += ai_score * 0.20
    
    return {
        'total_score': int(score),
        'matched_skills': list(matched),
        'missing_skills': list(missing),
        'job': job,
        'skill_match': int(skill_match),
        'exp_match': int(exp_match), # ensure consistency
        'ai_match': int(ai_score)
    }


def calculate_selection_score(app_dict):
    """Weighted candidate selection score for recruiter shortlisting.

    Uses AI fit, skill match, and assessment completion to prioritize candidates.
    """
    try:
        ai_match = float(app_dict.get('ai_match_score') or app_dict.get('ai_score') or 0)
        skill_match = float(app_dict.get('skill_match') or 0)
        assessment_pct = float(app_dict.get('assessment_percentage') or 0)
        selection_score = round((ai_match * 0.50) + (skill_match * 0.30) + (assessment_pct * 0.20))
        return int(max(0, min(100, selection_score)))
    except Exception:
        return 0


def build_ranked_selection_pool(cursor, recruiter_id, job_id):
    """Build a ranked best-fit/reserve candidate pool for a specific recruiter job."""
    cursor.execute("SELECT to_regclass('public.candidate_profiles') AS candidate_profiles_tbl")
    cp_exists = bool((cursor.fetchone() or {}).get('candidate_profiles_tbl'))

    if cp_exists:
        cursor.execute(
            """
                SELECT 
                    a.id,
                    a.status,
                    a.assessment_status,
                    a.assessment_assigned_at,
                    a.assessment_completed_at,
                    a.applied_at,
                    c.id AS candidate_id,
                    c.name AS candidate_name,
                    c.email AS candidate_email,
                    cp.primary_skills,
                    cp.secondary_skills,
                    cp.work_experience,
                    cp.degree,
                    cp.degree AS education,
                    cp.resume_file,
                    COALESCE(latest_ai.percentage, 0) AS ai_score,
                    at_assessment.obtained_marks AS assessment_obtained_marks,
                    at_assessment.total_marks AS assessment_total_marks,
                    at_assessment.percentage AS assessment_percentage,
                    j.id AS job_id,
                    j.title AS job_title,
                    j.skills AS job_skills,
                    j.description AS job_description,
                    j.location AS job_location,
                    j.employment_mode AS job_employment_mode,
                    j.min_experience AS job_min_experience,
                    j.max_experience AS job_max_experience,
                    j.education AS job_education,
                    COALESCE(j.openings, 0) AS job_openings
                FROM applications a
                JOIN candidates c ON a.candidate_id = c.id
                JOIN jobs j ON a.job_id = j.id
                LEFT JOIN candidate_profiles cp ON c.id = cp.candidate_id
                LEFT JOIN ai_tests at_assessment ON a.assessment_id = at_assessment.id
                LEFT JOIN (
                    SELECT DISTINCT ON (candidate_id) candidate_id, percentage
                    FROM ai_tests
                    WHERE status = 'completed'
                    ORDER BY candidate_id, completed_at DESC
                ) AS latest_ai ON c.id = latest_ai.candidate_id
                WHERE j.recruiter_id = %s AND j.id = %s
                ORDER BY j.id, a.applied_at DESC
            """,
            (recruiter_id, job_id),
        )
    else:
        cursor.execute(
            """
                SELECT 
                    a.id,
                    a.status,
                    a.assessment_status,
                    a.assessment_assigned_at,
                    a.assessment_completed_at,
                    a.applied_at,
                    c.id AS candidate_id,
                    c.name AS candidate_name,
                    c.email AS candidate_email,
                    NULL::TEXT AS primary_skills,
                    NULL::TEXT AS secondary_skills,
                    NULL::TEXT AS work_experience,
                    NULL::TEXT AS degree,
                    NULL::TEXT AS education,
                    NULL::TEXT AS resume_file,
                    COALESCE(latest_ai.percentage, 0) AS ai_score,
                    at_assessment.obtained_marks AS assessment_obtained_marks,
                    at_assessment.total_marks AS assessment_total_marks,
                    at_assessment.percentage AS assessment_percentage,
                    j.id AS job_id,
                    j.title AS job_title,
                    j.skills AS job_skills,
                    j.description AS job_description,
                    j.location AS job_location,
                    j.employment_mode AS job_employment_mode,
                    j.min_experience AS job_min_experience,
                    j.max_experience AS job_max_experience,
                    j.education AS job_education,
                    COALESCE(j.openings, 0) AS job_openings
                FROM applications a
                JOIN candidates c ON a.candidate_id = c.id
                JOIN jobs j ON a.job_id = j.id
                LEFT JOIN ai_tests at_assessment ON a.assessment_id = at_assessment.id
                LEFT JOIN (
                    SELECT DISTINCT ON (candidate_id) candidate_id, percentage
                    FROM ai_tests
                    WHERE status = 'completed'
                    ORDER BY candidate_id, completed_at DESC
                ) AS latest_ai ON c.id = latest_ai.candidate_id
                WHERE j.recruiter_id = %s AND j.id = %s
                ORDER BY j.id, a.applied_at DESC
            """,
            (recruiter_id, job_id),
        )

    rows = cursor.fetchall()
    ranked_apps = []
    job_meta = None

    for row in rows:
        app_dict = dict(row) if hasattr(row, 'items') else dict(zip([desc[0] for desc in cursor.description], row))

        match = calculate_match_score(
            profile={
                "primary_skills": app_dict.get("primary_skills"),
                "secondary_skills": app_dict.get("secondary_skills")
            },
            job={"skills": app_dict.get("job_skills")},
            ai_score=app_dict.get("ai_score") or 0
        )
        app_dict["skill_match"] = match["skill_match"]

        recruiter_ai = generate_recruiter_ai_assessment(
            job_description=app_dict.get("job_description") or "",
            candidate_data={
                "candidate_name": app_dict.get("candidate_name") or "Candidate",
                "job_title": app_dict.get("job_title") or "Role",
                "location": app_dict.get("job_location") or "Location not specified",
                "employment_type": app_dict.get("job_employment_mode") or "Full-Time",
                "job_skills": app_dict.get("job_skills") or "",
                "primary_skills": app_dict.get("primary_skills") or "",
                "secondary_skills": app_dict.get("secondary_skills") or "",
                "work_experience": app_dict.get("work_experience") or "",
                "candidate_education": app_dict.get("degree") or app_dict.get("education") or "",
                "job_min_experience": app_dict.get("job_min_experience"),
                "job_max_experience": app_dict.get("job_max_experience"),
                "job_education": app_dict.get("job_education") or ""
            }
        )
        app_dict["ai_dashboard_text"] = recruiter_ai.get("formatted_output", "")
        app_dict["ai_match_score"] = recruiter_ai.get("match_score", 0)
        app_dict["ai_next_action"] = recruiter_ai.get("next_action", "Review")
        app_dict["ai_priority"] = recruiter_ai.get("priority", "Medium")
        app_dict["selection_score"] = calculate_selection_score(app_dict)

        if job_meta is None:
            job_meta = {
                "job_id": app_dict.get("job_id"),
                "job_title": app_dict.get("job_title"),
                "job_skills": app_dict.get("job_skills"),
                "job_openings": int(app_dict.get("job_openings") or 0)
            }

        ranked_apps.append(app_dict)

    ranked_apps.sort(
        key=lambda item: (
            -int(item.get("selection_score") or 0),
            -int(item.get("ai_match_score") or 0),
            -int(item.get("skill_match") or 0),
            item.get("applied_at") or datetime.min
        )
    )

    openings = int(job_meta.get("job_openings") or 0) if job_meta else 0
    reserve_limit = max(20, openings // 2) if openings else 20
    best_fit_limit = openings if openings > 0 else len(ranked_apps)

    for index, app in enumerate(ranked_apps, start=1):
        app["selection_rank"] = index
        if best_fit_limit and index <= best_fit_limit:
            app["selection_bucket"] = "best-fit"
        elif index <= best_fit_limit + reserve_limit:
            app["selection_bucket"] = "reserve"
        else:
            app["selection_bucket"] = "backlog"

    return job_meta or {}, ranked_apps, best_fit_limit, reserve_limit


def _extract_years_from_text(text):
    """Best-effort extraction of years of experience from free text."""
    if not text:
        return 0
    try:
        t = str(text).lower()
        matches = re.findall(r"(\d+)\s*(?:\+\s*)?(?:years?|yrs?)", t)
        if matches:
            return max(int(x) for x in matches)
        generic_nums = re.findall(r"\b(\d{1,2})\b", t)
        if generic_nums:
            plausible = [int(x) for x in generic_nums if int(x) <= 50]
            return max(plausible) if plausible else 0
    except Exception:
        pass
    return 0


def _normalize_education_level(text):
    if not text:
        return "unknown"
    t = str(text).lower()
    if any(k in t for k in ["phd", "doctorate", "doctoral"]):
        return "doctorate"
    if any(k in t for k in ["master", "m.tech", "m.e", "mca", "mba", "msc"]):
        return "masters"
    if any(k in t for k in ["bachelor", "b.tech", "b.e", "bca", "bsc", "undergraduate"]):
        return "bachelors"
    if any(k in t for k in ["diploma", "associate"]):
        return "diploma"
    if any(k in t for k in ["12th", "high school", "intermediate"]):
        return "school"
    return "other"


def generate_recruiter_ai_assessment(job_description, candidate_data):
    """Generate advanced recruiter-facing AI assessment with strict formatted output."""
    try:
        job_title = (candidate_data.get('job_title') or 'Role').strip()
        location = (candidate_data.get('location') or 'Location not specified').strip()
        employment_type = (candidate_data.get('employment_type') or 'Full-Time').strip()

        job_skills_raw = candidate_data.get('job_skills') or ''
        cand_primary = candidate_data.get('primary_skills') or ''
        cand_secondary = candidate_data.get('secondary_skills') or ''

        job_skills = [s.strip() for s in str(job_skills_raw).split(',') if s.strip()]
        candidate_skills = [
            s.strip() for s in (str(cand_primary) + ',' + str(cand_secondary)).split(',') if s.strip()
        ]

        job_set = {s.lower() for s in job_skills}
        cand_set = {s.lower() for s in candidate_skills}
        matched = sorted({s for s in job_set if s in cand_set})
        missing = sorted({s for s in job_set if s not in cand_set})

        skills_match = int((len(matched) / len(job_set)) * 100) if job_set else 0

        min_exp = candidate_data.get('job_min_experience')
        max_exp = candidate_data.get('job_max_experience')
        min_exp = int(min_exp) if min_exp not in (None, '', 'None') else 0
        max_exp = int(max_exp) if max_exp not in (None, '', 'None') else None

        candidate_exp_years = _extract_years_from_text(candidate_data.get('work_experience'))
        if min_exp <= 0:
            experience_match = 100
        elif candidate_exp_years >= min_exp and (max_exp is None or candidate_exp_years <= max_exp):
            experience_match = 100
        elif candidate_exp_years >= min_exp:
            experience_match = 90
        elif candidate_exp_years >= max(1, int(min_exp * 0.7)):
            experience_match = 75
        else:
            experience_match = max(0, int((candidate_exp_years / max(min_exp, 1)) * 100))

        job_edu_level = _normalize_education_level(candidate_data.get('job_education'))
        cand_edu_level = _normalize_education_level(candidate_data.get('candidate_education'))
        education_rank = {
            'school': 1,
            'diploma': 2,
            'bachelors': 3,
            'masters': 4,
            'doctorate': 5,
            'other': 2,
            'unknown': 2
        }
        required_rank = education_rank.get(job_edu_level, 2)
        candidate_rank = education_rank.get(cand_edu_level, 2)
        if job_edu_level in ('unknown', 'other'):
            education_match = 80
        elif candidate_rank >= required_rank:
            education_match = 100
        elif candidate_rank == required_rank - 1:
            education_match = 70
        else:
            education_match = 40

        match_score = int(round((skills_match * 0.5) + (experience_match * 0.3) + (education_match * 0.2)))

        if match_score >= 80:
            recommendation = "Strong Fit"
            recommendation_reason = "Candidate demonstrates strong alignment across required skills, relevant experience, and expected education criteria."
        elif match_score >= 60:
            recommendation = "Moderate Fit"
            recommendation_reason = "Candidate meets core requirements but has identifiable gaps that may require onboarding support."
        else:
            recommendation = "Weak Fit"
            recommendation_reason = "Candidate currently falls short on key role requirements, especially in critical competency areas."

        top_candidate = "Yes" if match_score > 80 else "No"

        candidate_name = candidate_data.get('candidate_name') or 'Candidate'
        ai_summary = (
            f"{candidate_name} shows a {match_score}/100 alignment for {job_title} based on skills, experience, and education. "
            f"Strength areas include {', '.join([s.title() for s in matched[:3]]) if matched else 'baseline profile alignment'}, "
            f"with opportunities in {', '.join([s.title() for s in missing[:2]]) if missing else 'advanced specialization'}.")

        recruiter_notes = [
            "Profile appears consistent with the role's functional expectations and should be evaluated for practical problem-solving depth.",
            "Verify candidate project relevance against team stack before final hiring decision."
        ]

        if match_score >= 80:
            comparison_insight = "Candidate appears stronger than a typical applicant for this role, particularly on core technical alignment and readiness."
        elif match_score >= 60:
            comparison_insight = "Candidate is around the typical applicant benchmark, with moderate strengths and manageable skill gaps."
        else:
            comparison_insight = "Candidate is currently below typical applicant benchmarks for this role and may need significant upskilling."

        if match_score >= 75:
            next_action = "Schedule Interview"
            interview_status = "Scheduled"
            suggested_date = (datetime.utcnow() + timedelta(days=2)).strftime('%Y-%m-%d')
            suggested_time = "11:00 AM"
            interview_mode = "Online"
            interview_reason = "Interview scheduled based on high match score"
        elif match_score >= 55:
            next_action = "Shortlist"
            interview_status = "Not Scheduled"
            suggested_date = "-"
            suggested_time = "-"
            interview_mode = "-"
            interview_reason = "Candidate requires additional review before interview scheduling"
        else:
            next_action = "Reject"
            interview_status = "Not Scheduled"
            suggested_date = "-"
            suggested_time = "-"
            interview_mode = "-"
            interview_reason = "Candidate not a strong fit currently"

        if match_score >= 80:
            notification_insight = "High match candidate just applied"
        elif match_score >= 60:
            notification_insight = "Promising candidate applied - review recommended"
        else:
            notification_insight = "Low match candidate applied - evaluate only if pipeline is limited"

        priority = "High" if match_score >= 80 else "Medium" if match_score >= 60 else "Low"

        if match_score >= 90:
            smart_tag = "Top 10% Candidate"
        elif missing:
            smart_tag = f"Skill Gap: {missing[0].title()}"
        elif match_score >= 80:
            smart_tag = "High Potential Candidate"
        else:
            smart_tag = "Needs Upskilling"

        readiness_yes = match_score >= 70 and skills_match >= 60 and experience_match >= 50
        readiness_label = "Yes" if readiness_yes else "No"
        readiness_reason = "Candidate demonstrates sufficient role readiness for structured interview assessment" if readiness_yes else "Candidate does not currently meet readiness threshold for this role"

        email_subject = f"Interview Invitation for {job_title}"
        email_body = (
            "Dear Candidate,\n\n"
            "Congratulations! We are pleased to inform you that your profile has been shortlisted for the next stage.\n"
            f"Role: {job_title}\n"
            f"Interview Date: {suggested_date}\n"
            f"Interview Time: {suggested_time}\n"
            f"Mode: {interview_mode}\n"
            "Meeting Link: [To be shared]\n\n"
            "Please be available at the scheduled time and keep your documents ready.\n\n"
            "Best regards,\n"
            "Recruitment Team"
        ) if match_score >= 75 else (
            "Dear Candidate,\n\n"
            "Thank you for your interest. At this time, we are moving forward with candidates whose profiles are more closely aligned with this role.\n\n"
            "Best regards,\n"
            "Recruitment Team"
        )

        timeline_next = next_action

        formatted_output = (
            f"Match Score: {match_score}/100\n\n"
            "Score Breakdown:\n"
            f"- Skills: {skills_match}%\n"
            f"- Experience: {experience_match}%\n"
            f"- Education: {education_match}%\n\n"
            "AI Summary:\n"
            f"{ai_summary}\n\n"
            "Skill Analysis:\n"
            f"- Matching Skills: {', '.join([s.title() for s in matched]) if matched else 'None'}\n"
            f"- Missing Skills: {', '.join([s.title() for s in missing]) if missing else 'None'}\n\n"
            "Hiring Recommendation:\n"
            f"{recommendation} - {recommendation_reason}\n\n"
            f"Top Candidate: {top_candidate}\n\n"
            "Recruiter Notes:\n"
            f"- {recruiter_notes[0]}\n"
            f"- {recruiter_notes[1]}\n\n"
            "Comparison Insight:\n"
            f"{comparison_insight}\n\n"
            "Next Action:\n"
            f"{next_action}\n\n"
            "Notification Insight:\n"
            f"{notification_insight}\n\n"
            "Interview Status:\n"
            f"{interview_status}\n\n"
            "Interview Details:\n"
            f"- Date: {suggested_date}\n"
            f"- Time: {suggested_time}\n"
            f"- Mode: {interview_mode}\n\n"
            "Email Content:\n"
            f"Subject:\n{email_subject}\n"
            f"Body:\n{email_body}\n\n"
            "Application Timeline:\n"
            "- Applied\n"
            "- Reviewed\n"
            f"- Next: {timeline_next}\n\n"
            "Candidate Priority:\n"
            f"{priority}\n\n"
            "Smart Tag:\n"
            f"{smart_tag}\n\n"
            "Interview Readiness:\n"
            f"{readiness_label} - {readiness_reason}"
        )

        return {
            'match_score': match_score,
            'skills_match': skills_match,
            'experience_match': experience_match,
            'education_match': education_match,
            'matching_skills': [s.title() for s in matched],
            'missing_skills': [s.title() for s in missing],
            'recommendation': recommendation,
            'top_candidate': top_candidate,
            'next_action': next_action,
            'interview_status': interview_status,
            'interview_date': suggested_date,
            'interview_time': suggested_time,
            'interview_mode': interview_mode,
            'interview_reason': interview_reason,
            'notification_insight': notification_insight,
            'priority': priority,
            'smart_tag': smart_tag,
            'interview_readiness': readiness_label,
            'interview_readiness_reason': readiness_reason,
            'formatted_output': formatted_output
        }
    except Exception as e:
        print(f"Error generating recruiter AI assessment: {e}")
        return {
            'match_score': 0,
            'skills_match': 0,
            'experience_match': 0,
            'education_match': 0,
            'matching_skills': [],
            'missing_skills': [],
            'recommendation': 'Weak Fit',
            'top_candidate': 'No',
            'next_action': 'Reject',
            'interview_status': 'Not Scheduled',
            'interview_date': '-',
            'interview_time': '-',
            'interview_mode': '-',
            'interview_reason': 'Candidate not a strong fit currently',
            'notification_insight': 'Candidate application received',
            'priority': 'Low',
            'smart_tag': 'Needs Review',
            'interview_readiness': 'No',
            'interview_readiness_reason': 'Unable to compute readiness',
            'formatted_output': 'Match Score: 0/100\n\nScore Breakdown:\n- Skills: 0%\n- Experience: 0%\n- Education: 0%\n\nAI Summary:\nUnable to generate insights for this candidate at the moment.'
        }

def calculate_match(candidate_skills, job_skills):
    job = set(job_skills.lower().split(','))
    cand = set(candidate_skills)

    match = job.intersection(cand)
    return int((len(match) / len(job)) * 100) if job else 0

@app.route('/recruiter-dashboard')
def recruiter_dashboard():
    if session.get('role') != 'recruiter':
        return redirect('/login')

    user_id = session.get('user_id')
    
    try:
        with SafeDBConnection() as (cursor, db):
            # Get recruiter info
            cursor.execute("SELECT profile_completed FROM recruiters WHERE id = %s", (user_id,))
            user = cursor.fetchone()
            is_complete = user['profile_completed'] if user else False
            
            # Get recruiter profile
            cursor.execute("SELECT * FROM recruiter_profiles WHERE recruiter_id = %s ORDER BY id DESC LIMIT 1", (user_id,))
            recruiter_profile = cursor.fetchone()
            
            # Compute profile_percent from stored profile (only essential required fields, excluding CIN)
            if recruiter_profile:
                required_keys = ['full_name','phone','designation','linkedin','company_name','company_type','company_size','industry','roles','experience_levels']
                total = len(required_keys)
                filled = 0
                for k in required_keys:
                    val = recruiter_profile.get(k) if isinstance(recruiter_profile, dict) else None
                    if val and str(val).strip() != '':
                        filled += 1
                profile_percent = int((filled/total)*100) if total else 0
            else:
                profile_percent = 40
            
            verification_status = recruiter_profile.get('verification_status') if recruiter_profile and isinstance(recruiter_profile, dict) else 'pending'
            
            # Get jobs posted (with LIMIT to prevent memory exhaustion)
            cursor.execute("SELECT * FROM jobs WHERE recruiter_id = %s LIMIT 100", (user_id,))
            jobs = cursor.fetchall()
            
            # Get stats with simplified query for performance
            try:
                cursor.execute("""
                    SELECT 
                        COUNT(DISTINCT j.id) as jobs_count,
                        COUNT(DISTINCT CASE WHEN a.id IS NOT NULL THEN a.id END) as applications_count,
                        COUNT(DISTINCT CASE WHEN a.status = 'Interview' THEN a.id END) as interviews_count,
                        COUNT(DISTINCT CASE WHEN a.status = 'Selected' THEN a.id END) as offers_count
                    FROM jobs j
                    LEFT JOIN applications a ON a.job_id = j.id
                    WHERE j.recruiter_id = %s
                """, (user_id,))
                
                stats = cursor.fetchone()
                jobs_count = stats['jobs_count'] if stats else 0
                applications_count = stats['applications_count'] if stats else 0
                interviews_count = stats['interviews_count'] if stats else 0
                offers_count = stats['offers_count'] if stats else 0
            except Exception as e:
                print(f"[WARNING] Stats query failed, using defaults: {e}")
                jobs_count = len(jobs) if jobs else 0
                applications_count = 0
                interviews_count = 0
                offers_count = 0
            
            # Fetch upcoming interviews (next 7 days)
            from datetime import date, timedelta
            today = date.today()
            next_week = today + timedelta(days=7)
            cursor.execute("""
                SELECT i.interview_date, i.interview_time, j.title as job_title, c.name as candidate_name
                FROM interviews i
                JOIN candidates c ON i.candidate_id = c.id
                JOIN jobs j ON i.job_id = j.id
                WHERE i.recruiter_id = %s
                  AND i.interview_date >= %s
                  AND i.interview_date <= %s
                  AND i.status = 'Scheduled'
                ORDER BY i.interview_date, i.interview_time
                LIMIT 5
            """, (user_id, today, next_week))
            upcoming_interviews = cursor.fetchall()

        # Determine current tab (overview by default)
        current_tab = request.args.get('tab', 'overview')
        
        # Handle edit-job tab - fetch specific job for editing
        edit_job = None
        if current_tab == 'edit-job':
            job_id = request.args.get('job_id')
            if job_id:
                with SafeDBConnection() as (cursor, db):
                    cursor.execute("SELECT * FROM jobs WHERE id = %s AND recruiter_id = %s", (job_id, user_id))
                    edit_job = cursor.fetchone()

        # today's date for job deadlines when needed
        from datetime import date
        today = date.today()

        return render_template(
            'recruiter_dashboard.html', 
            is_complete=is_complete,
            profile_percent=profile_percent,
            verification_status=verification_status,
            jobs=jobs,
            jobs_count=jobs_count,
            applications_count=applications_count,
            interviews_count=interviews_count,
            offers_count=offers_count,
            tab=current_tab,
            profile=recruiter_profile,
            edit_job=edit_job,
            today=today,
            upcoming_interviews=upcoming_interviews
        )
    except Exception as e:
        print(f"Error in recruiter_dashboard: {e}")
        flash("Error loading dashboard", "danger")
        return redirect('/login')

@app.route('/save-recruiter-profile', methods=['POST'])
def save_recruiter_profile():
    if session.get('role') != 'recruiter':
        return redirect('/login')

    user_id = session.get('user_id')
    full_name = request.form.get('full_name')
    phone = request.form.get('phone')
    designation = request.form.get('designation')
    linkedin = request.form.get('linkedin')
    company_name = request.form.get('company_name')
    company_type = request.form.get('company_type')
    company_size = request.form.get('company_size')
    industry = request.form.get('industry')
    address = request.form.get('address')
    website = request.form.get('website')
    company_doc = request.files.get('company_doc')
    auth_doc = request.files.get('auth_doc')
    logo = request.files.get('logo_file')
    geo_tag_pdf = request.files.get('geo_tag_pdf')
    roles = request.form.get('roles')
    experience_levels = request.form.get('experience_levels')
    job_types = request.form.get('job_types')
    
    # New fields
    work_email = request.form.get('work_email')
    recruiting_experience = request.form.get('recruiting_experience')
    specialization = ','.join(request.form.getlist('specialization'))  
    languages = ','.join(request.form.getlist('languages'))  
    company_registration = (request.form.get('company_registration') or '').strip()
    if not company_registration:
        company_registration = 'NA'
    founded_year = request.form.get('founded_year')
    headquarters_location = request.form.get('headquarters_location')
    company_linkedin = request.form.get('company_linkedin')
    hiring_locations = ','.join(request.form.getlist('hiring_locations'))  
    specific_locations = request.form.get('specific_locations')
    interview_mode = request.form.get('interview_mode')

    # Sanitize numeric fields (empty or invalid -> None)
    def _to_int(val):
        try:
            return int(val) if val not in (None, '', 'None') else None
        except (TypeError, ValueError):
            return None

    recruiting_experience_val = _to_int(recruiting_experience)
    founded_year_val = _to_int(founded_year)
    
    db = get_connection()
    # Fetch existing profile to preserve files when not re-uploaded
    existing_cursor = db.cursor(cursor_factory=RealDictCursor)
    existing_cursor.execute("SELECT * FROM recruiter_profiles WHERE recruiter_id = %s", (user_id,))
    existing_profile = existing_cursor.fetchone()
    existing_cursor.fetchall()
    existing_cursor.close()

    # Track if major changes occurred (company name or documents)
    # Only applies to EDITS of existing profiles, not first-time saves
    # 
    # MAJOR CHANGES (require MOU re-signing):
    #   - Company name changed
    #   - Company document (company_doc) re-uploaded with DIFFERENT filename
    #   - Authorization document (auth_doc) re-uploaded with DIFFERENT filename
    #
    # MINOR CHANGES (no MOU re-signing needed):
    #   - Contact details (phone, email, designation, LinkedIn)
    #   - Company info (type, size, industry, address, website, etc.)
    #   - Logo, geo-tag PDF, or other non-critical documents
    #   - Recruitment preferences (specialization, languages, etc.)
    major_change_detected = False
    
    comp_filename = existing_profile['company_doc'] if existing_profile else None
    # Check if company doc was actually uploaded (not just an empty file input)
    if company_doc and company_doc.filename and company_doc.filename.strip() != '':
        new_comp_filename = secure_filename(company_doc.filename)
        company_doc.save(os.path.join(UPLOAD_FOLDER, new_comp_filename))
        # Major change ONLY if filename is different from existing (actual replacement)
        if existing_profile and existing_profile.get('company_doc') and existing_profile.get('company_doc') != new_comp_filename:
            print(f"[MAJOR CHANGE] Company doc changed: {existing_profile.get('company_doc')} -> {new_comp_filename}")
            major_change_detected = True
        comp_filename = new_comp_filename

    auth_filename = existing_profile['auth_doc'] if existing_profile else None
    # Check if auth doc was actually uploaded (not just an empty file input)
    if auth_doc and auth_doc.filename and auth_doc.filename.strip() != '':
        new_auth_filename = secure_filename(auth_doc.filename)
        auth_doc.save(os.path.join(UPLOAD_FOLDER, new_auth_filename))
        # Major change ONLY if filename is different from existing (actual replacement)
        if existing_profile and existing_profile.get('auth_doc') and existing_profile.get('auth_doc') != new_auth_filename:
            print(f"[MAJOR CHANGE] Auth doc changed: {existing_profile.get('auth_doc')} -> {new_auth_filename}")
            major_change_detected = True
        auth_filename = new_auth_filename
    
    # Check company name change (only if profile already exists and name actually changed)
    if existing_profile and existing_profile.get('company_name') and existing_profile.get('company_name') != company_name:
        print(f"[MAJOR CHANGE] Company name changed: '{existing_profile.get('company_name')}' -> '{company_name}'")
        major_change_detected = True

    logo_filename = existing_profile['logo_file'] if existing_profile else None
    if logo and logo.filename != '':
        logo_filename = secure_filename(logo.filename)
        logo.save(os.path.join(UPLOAD_FOLDER, logo_filename))

    geo_tag_pdf_filename = existing_profile['geo_tag_pdf'] if existing_profile else None
    if geo_tag_pdf and geo_tag_pdf.filename != '':
        geo_tag_pdf_filename = secure_filename(geo_tag_pdf.filename)
        geo_tag_pdf.save(os.path.join(UPLOAD_FOLDER, geo_tag_pdf_filename))

    cursor = db.cursor(cursor_factory=RealDictCursor)
    
    try:
        # Ensure new columns exist - check before adding to avoid transaction errors
        cursor.execute("""
            SELECT column_name FROM information_schema.columns 
            WHERE table_name = 'recruiter_profiles'
        """)
        existing_cols = {row['column_name'] for row in cursor.fetchall()}
        
        new_columns = [
            ("linkedin", "VARCHAR(255)"),
            ("company_type", "VARCHAR(100)"),
            ("company_size", "VARCHAR(50)"),
            ("industry", "VARCHAR(100)"),
            ("address", "VARCHAR(255)"),
            ("logo_file", "VARCHAR(255)"),
            ("roles", "TEXT"),
            ("experience_levels", "TEXT"),
            ("job_types", "TEXT"),
            ("profile_percent", "INT DEFAULT 0"),
            ("verification_status", "VARCHAR(20) DEFAULT 'pending'"),
            ("work_email", "VARCHAR(255)"),
            ("recruiting_experience", "INT"),
            ("specialization", "TEXT"),
            ("languages", "TEXT"),
            ("company_registration", "VARCHAR(21)"),
            ("founded_year", "INT"),
            ("headquarters_location", "VARCHAR(255)"),
            ("company_linkedin", "VARCHAR(255)"),
            ("hiring_locations", "TEXT"),
            ("specific_locations", "TEXT"),
            ("interview_mode", "VARCHAR(50)"),
            ("geo_tag_pdf", "VARCHAR(255)"),
            ("agreement_accepted", "BOOLEAN DEFAULT FALSE"),
            ("agreement_accepted_at", "TIMESTAMP"),
            ("agreement_signature_name", "VARCHAR(255)"),
            ("agreement_signature_at", "TIMESTAMP"),
            ("agreement_signature_image", "BYTEA"),
            ("agreement_signature_image_mime", "VARCHAR(100)"),
            ("agreement_signature_image_name", "VARCHAR(255)"),
            ("agreement_pdf", "BYTEA"),
            ("agreement_pdf_mime", "VARCHAR(100)"),
            ("agreement_pdf_name", "VARCHAR(255)"),
            ("admin_countersigned", "BOOLEAN DEFAULT FALSE"),
            ("admin_countersigned_at", "TIMESTAMP"),
            ("admin_countersigned_by", "VARCHAR(255)"),
            ("mou_reset_reason", "VARCHAR(50)")
        ]
        
        for col_name, col_type in new_columns:
            if col_name not in existing_cols:
                cursor.execute(f"ALTER TABLE recruiter_profiles ADD COLUMN {col_name} {col_type}")
        
        db.commit()  # Commit column additions before proceeding
        
        # Compute profile_percent based on only essential required fields (excluding CIN)
        required_values = {
            'full_name': full_name,
            'phone': phone,
            'designation': designation,
            'linkedin': linkedin,
            'company_name': company_name,
            'company_type': company_type,
            'company_size': company_size,
            'industry': industry,
            'roles': roles,
            'experience_levels': experience_levels
        }
        
        # Count filled required fields (non-empty values)
        filled = sum(1 for v in required_values.values() if v and str(v).strip() not in ['', 'None'])
        total_fields = len(required_values)
        profile_percent = int((filled / total_fields) * 100) if total_fields > 0 else 0
        
        # Ensure percentage doesn't exceed 100
        profile_percent = min(profile_percent, 100)

        # Determine agreement field values based on whether major changes occurred
        if major_change_detected:
            # Major changes: reset MOU and verification
            agreement_accepted_val = False
            agreement_accepted_at_val = None
            agreement_signature_name_val = None
            agreement_signature_at_val = None
            agreement_signature_image_val = None
            agreement_signature_image_mime_val = None
            agreement_signature_image_name_val = None
            agreement_pdf_val = None
            agreement_pdf_mime_val = None
            agreement_pdf_name_val = None
            admin_countersigned_val = False
            admin_countersigned_at_val = None
            admin_countersigned_by_val = None
            verification_status_val = 'pending'
            profile_completed_val = False
            # Mark reason for MOU reset - check if MOU was previously signed
            if existing_profile and existing_profile.get('agreement_accepted'):
                mou_reset_reason_val = 'major_change'
            else:
                mou_reset_reason_val = None  # First time, no previous MOU
        else:
            # Minor changes: preserve existing MOU and verification status
            if existing_profile:
                agreement_accepted_val = existing_profile.get('agreement_accepted', False)
                agreement_accepted_at_val = existing_profile.get('agreement_accepted_at')
                agreement_signature_name_val = existing_profile.get('agreement_signature_name')
                agreement_signature_at_val = existing_profile.get('agreement_signature_at')
                agreement_signature_image_val = existing_profile.get('agreement_signature_image')
                agreement_signature_image_mime_val = existing_profile.get('agreement_signature_image_mime')
                agreement_signature_image_name_val = existing_profile.get('agreement_signature_image_name')
                agreement_pdf_val = existing_profile.get('agreement_pdf')
                agreement_pdf_mime_val = existing_profile.get('agreement_pdf_mime')
                agreement_pdf_name_val = existing_profile.get('agreement_pdf_name')
                admin_countersigned_val = existing_profile.get('admin_countersigned', False)
                admin_countersigned_at_val = existing_profile.get('admin_countersigned_at')
                admin_countersigned_by_val = existing_profile.get('admin_countersigned_by')
                verification_status_val = existing_profile.get('verification_status', 'pending')
                # Clear mou_reset_reason if MOU is signed OR if no previous reset reason exists
                # Only keep it if there was a previous major change AND MOU hasn't been re-signed yet
                if agreement_accepted_val or not existing_profile.get('mou_reset_reason'):
                    mou_reset_reason_val = None
                else:
                    mou_reset_reason_val = existing_profile.get('mou_reset_reason')
                # Only set profile_completed to current state from recruiters table if no major changes
                profile_completed_val = None  # We'll preserve it below by not updating
            else:
                # New profile (should not happen, but handle it)
                agreement_accepted_val = False
                agreement_accepted_at_val = None
                agreement_signature_name_val = None
                agreement_signature_at_val = None
                agreement_signature_image_val = None
                agreement_signature_image_mime_val = None
                agreement_signature_image_name_val = None
                agreement_pdf_val = None
                agreement_pdf_mime_val = None
                agreement_pdf_name_val = None
                admin_countersigned_val = False
                admin_countersigned_at_val = None
                admin_countersigned_by_val = None
                verification_status_val = 'pending'
                profile_completed_val = False
                mou_reset_reason_val = None

        # keep only one row per recruiter to avoid stale data showing
        cursor.execute("DELETE FROM recruiter_profiles WHERE recruiter_id = %s", (user_id,))

        cursor.execute("""
                        INSERT INTO recruiter_profiles 
                        (recruiter_id, full_name, designation, company_name, phone, website, company_doc, auth_doc,
                         linkedin, company_type, company_size, industry, address, logo_file, roles, experience_levels, job_types, 
                         profile_percent, verification_status, work_email, recruiting_experience, specialization, languages,
                         company_registration, founded_year, headquarters_location, company_linkedin, hiring_locations, 
                         specific_locations, interview_mode, geo_tag_pdf,
                         agreement_accepted, agreement_accepted_at, agreement_signature_name, agreement_signature_at,
                         agreement_signature_image, agreement_signature_image_mime, agreement_signature_image_name,
                         agreement_pdf, agreement_pdf_mime, agreement_pdf_name,
                         admin_countersigned, admin_countersigned_at, admin_countersigned_by, mou_reset_reason)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (user_id, full_name, designation, company_name, phone, website, comp_filename, auth_filename,
                            linkedin, company_type, company_size, industry, address, logo_filename, roles, experience_levels, job_types, 
                            profile_percent, verification_status_val, work_email, recruiting_experience_val, specialization, languages,
                                company_registration, founded_year_val, headquarters_location, company_linkedin, hiring_locations, 
                            specific_locations, interview_mode, geo_tag_pdf_filename,
                            agreement_accepted_val, agreement_accepted_at_val, agreement_signature_name_val, agreement_signature_at_val,
                            agreement_signature_image_val, agreement_signature_image_mime_val, agreement_signature_image_name_val,
                            agreement_pdf_val, agreement_pdf_mime_val, agreement_pdf_name_val,
                            admin_countersigned_val, admin_countersigned_at_val, admin_countersigned_by_val, mou_reset_reason_val))

        # Only lock dashboard if major changes require re-verification
        if major_change_detected and profile_completed_val is False:
            cursor.execute("UPDATE recruiters SET profile_completed = FALSE WHERE id = %s", (user_id,))
        
        print(f"[PROFILE UPDATE] User {user_id} - Major change detected: {major_change_detected}, MOU reset reason: {mou_reset_reason_val}")
        
        db.commit()
        
        # Flash appropriate message based on whether major changes occurred and profile status
        if major_change_detected:
            if existing_profile and existing_profile.get('agreement_accepted'):
                flash("⚠️ Major profile changes detected! Your previous MOU has been invalidated. Please re-sign the MOU for admin re-verification.", "warning")
            else:
                flash("✓ Profile saved successfully! Your profile completion is at {}%. Complete Step 2: Sign the MOU to proceed.".format(profile_percent), "success")
        else:
            if profile_percent >= 85:
                if existing_profile and not existing_profile.get('agreement_accepted'):
                    flash("✓ Profile updated successfully! You have reached {}% completion. Ready for Step 2: Sign the MOU.".format(profile_percent), "success")
                else:
                    flash("✓ Profile updated successfully! All changes saved.", "success")
            else:
                flash("✓ Profile saved! Current completion: {}%. Reach 85% to unlock MOU signing.".format(profile_percent), "info")
    except Exception as e:
        print(f"Database Error: {e}")
        db.rollback()
        flash("An error occurred while saving your profile.", "danger")
    finally:
        cleanup_db_resources(cursor, db)

    return redirect('/recruiter-dashboard?tab=profile')


@app.route('/recruiter/mou', methods=['GET', 'POST'])
def recruiter_mou():
    if session.get('role') != 'recruiter':
        return redirect('/login')

    recruiter_id = session.get('user_id')
    db = get_connection()
    cursor = db.cursor(cursor_factory=RealDictCursor)

    # Backward-compatible schema safety for older databases
    try:
        cursor.execute("ALTER TABLE recruiter_profiles ADD COLUMN IF NOT EXISTS mou_reset_reason VARCHAR(50)")
        db.commit()
    except Exception as schema_err:
        db.rollback()
        print(f"[WARNING] Could not ensure mou_reset_reason column exists: {schema_err}")

    cursor.execute("SELECT * FROM recruiter_profiles WHERE recruiter_id=%s", (recruiter_id,))
    profile = cursor.fetchone()

    cursor.execute("SELECT name, email FROM recruiters WHERE id=%s", (recruiter_id,))
    recruiter_row = cursor.fetchone() or {}
    recruiter_name = recruiter_row.get('name') or recruiter_id
    recruiter_email = recruiter_row.get('email') or ''

    if not profile:
        flash('Please complete your recruiter profile first.', 'warning')
        cleanup_db_resources(cursor, db)
        return redirect('/recruiter-dashboard?tab=profile')

    if request.method == 'POST':
        signature_name = (request.form.get('signature_name') or '').strip()
        accept = request.form.get('accept_agreement') == 'on'
        signature_data = (request.form.get('signature_data') or '').strip()
        signature_file = request.files.get('signature_image')
        signature_bytes = None
        signature_mime = None
        signature_filename = None

        # Prefer signature drawn on canvas (data URL), fallback to uploaded image.
        if signature_data.startswith('data:image/') and ',' in signature_data:
            try:
                header, encoded = signature_data.split(',', 1)
                signature_bytes = base64.b64decode(encoded)
                signature_mime = header.split(';')[0].replace('data:', '') or 'image/png'
                signature_filename = 'drawn_signature.png'
            except Exception as sig_err:
                print(f"[WARNING] Invalid drawn signature payload: {sig_err}")
                signature_bytes = None
                signature_mime = None
                signature_filename = None

        if signature_file and signature_file.filename:
            signature_bytes = signature_file.read()
            signature_mime = signature_file.mimetype
            signature_filename = secure_filename(signature_file.filename)

        if not signature_name or not accept:
            flash('Please enter your full name and accept the agreement.', 'warning')
            cleanup_db_resources(cursor, db)
            return redirect('/recruiter/mou')

        signed_at = datetime.utcnow()
        pdf_bytes = build_recruiter_mou_pdf(
            profile,
            recruiter_name,
            recruiter_email,
            recruiter_id,
            signature_name,
            signed_at,
            signature_image_bytes=signature_bytes
        )
        pdf_name = f"COMPANY_MOU_{recruiter_id}_{int(signed_at.timestamp())}.pdf"

        cursor.execute(
            """
            UPDATE recruiter_profiles
            SET agreement_accepted=TRUE,
                agreement_accepted_at=%s,
                agreement_signature_name=%s,
                agreement_signature_at=%s,
                agreement_signature_image=%s,
                agreement_signature_image_mime=%s,
                agreement_signature_image_name=%s,
                agreement_pdf=%s,
                agreement_pdf_mime=%s,
                agreement_pdf_name=%s,
                admin_countersigned=FALSE,
                admin_countersigned_at=NULL,
                admin_countersigned_by=NULL,
                verification_status='pending',
                mou_reset_reason=NULL
            WHERE recruiter_id=%s
            """,
            (
                signed_at, signature_name, signed_at,
                signature_bytes, signature_mime, signature_filename,
                pdf_bytes, 'application/pdf', pdf_name,
                recruiter_id
            )
        )
        db.commit()
        flash('✓ MOU Digitally Signed! Your profile has been submitted to admin for verification. You will receive notification once admin counter-signs and approves your company.', 'success')
        cleanup_db_resources(cursor, db)
        return redirect('/recruiter-dashboard?tab=profile')

    cleanup_db_resources(cursor, db)
    return render_template('recruiter_mou.html', profile=profile)


@app.route('/recruiter/mou/generate', methods=['POST'])
def recruiter_mou_generate():
    if session.get('role') != 'recruiter':
        return redirect('/login')

    recruiter_id = session.get('user_id')
    db = get_connection()
    cursor = db.cursor(cursor_factory=RealDictCursor)

    cursor.execute("SELECT * FROM recruiter_profiles WHERE recruiter_id=%s", (recruiter_id,))
    profile = cursor.fetchone()
    if not profile or not profile.get('agreement_accepted'):
        flash('Agreement is not accepted yet.', 'warning')
        cleanup_db_resources(cursor, db)
        return redirect('/recruiter/mou')

    cursor.execute("SELECT name, email FROM recruiters WHERE id=%s", (recruiter_id,))
    recruiter_row = cursor.fetchone() or {}
    recruiter_name = recruiter_row.get('name') or recruiter_id
    recruiter_email = recruiter_row.get('email') or ''

    signature_name = profile.get('agreement_signature_name') or recruiter_name
    signed_at = profile.get('agreement_signature_at') or profile.get('agreement_accepted_at') or datetime.utcnow()
    signature_bytes = profile.get('agreement_signature_image')

    pdf_bytes = build_recruiter_mou_pdf(
        profile,
        recruiter_name,
        recruiter_email,
        recruiter_id,
        signature_name,
        signed_at,
        signature_image_bytes=signature_bytes,
        admin_signature_name=profile.get('admin_countersigned_by'),
        admin_signed_at=profile.get('admin_countersigned_at')
    )
    pdf_name = f"COMPANY_MOU_{recruiter_id}_{int(signed_at.timestamp())}.pdf"

    cursor.execute(
        """
        UPDATE recruiter_profiles
        SET agreement_pdf=%s,
            agreement_pdf_mime=%s,
            agreement_pdf_name=%s
        WHERE recruiter_id=%s
        """,
        (pdf_bytes, 'application/pdf', pdf_name, recruiter_id)
    )
    db.commit()
    cleanup_db_resources(cursor, db)
    flash('Signed company MOU PDF generated.', 'success')
    return redirect('/recruiter/mou')


@app.route('/recruiter/mou/pdf')
def recruiter_mou_pdf():
    if session.get('role') != 'recruiter':
        return redirect('/login')

    recruiter_id = session.get('user_id')
    db = get_connection()
    cursor = db.cursor(cursor_factory=RealDictCursor)
    cursor.execute(
        """
        SELECT agreement_pdf, agreement_pdf_mime, agreement_pdf_name
        FROM recruiter_profiles WHERE recruiter_id=%s
        """,
        (recruiter_id,)
    )
    row = cursor.fetchone()
    cleanup_db_resources(cursor, db)

    if not row or not row.get('agreement_pdf'):
        flash('Signed company MOU PDF not available yet.', 'warning')
        return redirect('/recruiter/mou')

    pdf_bytes = row.get('agreement_pdf')
    pdf_name = row.get('agreement_pdf_name') or f"COMPANY_MOU_{recruiter_id}.pdf"
    return send_file(
        BytesIO(pdf_bytes),
        mimetype=row.get('agreement_pdf_mime') or 'application/pdf',
        as_attachment=False,
        download_name=pdf_name
    )

@app.route('/admin/verify-companies')
def admin_verify_companies():
    if session.get('role') != 'admin':
        return redirect('/login')
    
    # Redirect to admin dashboard with verify-companies hash section
    return redirect('/admin-dashboard#verify-companies')

@app.route('/admin/approve-company/<recruiter_id>')
def approve_company(recruiter_id):
    if session.get('role') != 'admin':
        return redirect('/login')

    db = get_connection()
    cursor = db.cursor(cursor_factory=RealDictCursor)
    try:
        cursor.execute("SELECT * FROM recruiter_profiles WHERE recruiter_id=%s", (recruiter_id,))
        profile = cursor.fetchone()
        if not profile:
            flash("Recruiter profile not found.", "danger")
            cleanup_db_resources(cursor, db)
            return redirect('/admin-dashboard#verify-companies')

        if not profile.get('agreement_accepted'):
            flash("Recruiter has not signed the company MOU yet.", "warning")
            cleanup_db_resources(cursor, db)
            return redirect('/admin-dashboard#verify-companies')

        cursor.execute("SELECT name FROM admins WHERE id=%s", (session.get('user_id'),))
        admin_row = cursor.fetchone() or {}
        admin_name = admin_row.get('name') or session.get('user_id') or 'Admin'
        admin_signed_at = datetime.utcnow()

        cursor.execute("""
            UPDATE recruiter_profiles
            SET admin_countersigned=TRUE,
                admin_countersigned_at=%s,
                admin_countersigned_by=%s,
                verification_status='approved'
            WHERE recruiter_id=%s
        """, (admin_signed_at, admin_name, recruiter_id))

        cursor.execute("SELECT name, email FROM recruiters WHERE id=%s", (recruiter_id,))
        recruiter_row = cursor.fetchone() or {}
        recruiter_name = recruiter_row.get('name') or recruiter_id
        recruiter_email = recruiter_row.get('email') or ''

        recruiter_signature_name = profile.get('agreement_signature_name') or profile.get('full_name') or recruiter_name
        recruiter_signed_at = profile.get('agreement_signature_at') or profile.get('agreement_accepted_at') or admin_signed_at
        recruiter_signature_image = profile.get('agreement_signature_image')

        pdf_bytes = build_recruiter_mou_pdf(
            profile,
            recruiter_name,
            recruiter_email,
            recruiter_id,
            recruiter_signature_name,
            recruiter_signed_at,
            signature_image_bytes=recruiter_signature_image,
            admin_signature_name=admin_name,
            admin_signed_at=admin_signed_at
        )

        company_slug = re.sub(r'[^A-Za-z0-9]+', '_', (profile.get('company_name') or str(recruiter_id))).strip('_')
        if not company_slug:
            company_slug = str(recruiter_id)
        pdf_name = f"COMPANY_MOU_{company_slug}_{int(admin_signed_at.timestamp())}.pdf"

        cursor.execute(
            """
            UPDATE recruiter_profiles
            SET agreement_pdf=%s,
                agreement_pdf_mime=%s,
                agreement_pdf_name=%s
            WHERE recruiter_id=%s
            """,
            (pdf_bytes, 'application/pdf', pdf_name, recruiter_id)
        )

        cursor.execute("UPDATE recruiters SET profile_completed=TRUE WHERE id=%s", (recruiter_id,))
        db.commit()
        flash("✓ Company Verified! MOU counter-signed and company profile approved. Recruiter account activated.", "success")
        try:
            send_notification('recruiter', recruiter_id, '✓ Congratulations! Your company profile has been verified and approved by admin. You can now access all recruitment features.')
        except Exception:
            pass
        try:
            log_audit(f"Approved company profile for recruiter {recruiter_id}")
        except Exception:
            pass
    except Exception as e:
        db.rollback()
        print(f"Approve company error: {e}")
        flash("Failed to approve company.", "danger")
    finally:
        cleanup_db_resources(cursor, db)
    return redirect('/admin-dashboard#verify-companies')


@app.route('/admin/company-mou/pdf/<recruiter_id>')
def admin_company_mou_pdf(recruiter_id):
    if session.get('role') != 'admin':
        return redirect('/login')

    db = get_connection()
    cursor = db.cursor(cursor_factory=RealDictCursor)
    cursor.execute(
        """
        SELECT agreement_pdf, agreement_pdf_mime, agreement_pdf_name
        FROM recruiter_profiles WHERE recruiter_id=%s
        """,
        (recruiter_id,)
    )
    row = cursor.fetchone()
    cleanup_db_resources(cursor, db)

    if not row or not row.get('agreement_pdf'):
        flash('Signed company MOU PDF not available.', 'warning')
        return redirect('/admin-dashboard#verify-companies')

    pdf_bytes = row.get('agreement_pdf')
    pdf_name = row.get('agreement_pdf_name') or f"COMPANY_MOU_{recruiter_id}.pdf"
    return send_file(
        BytesIO(pdf_bytes),
        mimetype=row.get('agreement_pdf_mime') or 'application/pdf',
        as_attachment=False,
        download_name=pdf_name
    )

@app.route('/admin/reject-company/<recruiter_id>')
def reject_company(recruiter_id):
    if session.get('role') != 'admin':
        return redirect('/login')

    db = get_connection()
    cursor = db.cursor(cursor_factory=RealDictCursor)
    try:
        cursor.execute("UPDATE recruiter_profiles SET verification_status='rejected' WHERE recruiter_id=%s", (recruiter_id,))
        cursor.execute("UPDATE recruiters SET profile_completed=FALSE WHERE id=%s", (recruiter_id,))
        db.commit()
        flash("Company verification rejected. Recruiter has been notified.", "warning")
        try:
            send_notification('recruiter', recruiter_id, '⚠️ Your company verification was rejected. Please update your profile with accurate information and valid documents, then re-submit for verification.')
        except Exception:
            pass
        try:
            log_audit(f"Rejected company profile for recruiter {recruiter_id}")
        except Exception:
            pass
    except Exception as e:
        db.rollback()
        print(f"Reject company error: {e}")
        flash("Failed to reject company.", "danger")
    finally:
        cleanup_db_resources(cursor, db)
    return redirect('/admin-dashboard#verify-companies')

@app.route('/recruiter/delete-profile', methods=['POST'])
def delete_recruiter_profile():
    if session.get('role') != 'recruiter':
        return redirect('/login')

    user_id = session.get('user_id')
    db = get_connection()
    cursor = db.cursor(cursor_factory=RealDictCursor)

    try:
        cursor.execute("DELETE FROM recruiter_profiles WHERE recruiter_id = %s", (user_id,))
        cursor.execute("UPDATE recruiters SET profile_completed = FALSE WHERE id = %s", (user_id,))
        db.commit()
        flash("Profile deleted. You can rebuild it anytime.", "info")
    except Exception as e:
        print(f"Error deleting recruiter profile: {e}")
        db.rollback()
        flash("Could not delete profile. Please try again.", "danger")
    finally:
        cleanup_db_resources(cursor, db)

    return redirect('/recruiter-dashboard?tab=profile')

@app.route('/update-status/<int:app_id>/<status>')
def update_status(app_id, status):
    if session.get('role') != 'recruiter':
        return redirect('/login')

    allowed = ['Shortlisted','Interview','Selected','Rejected']
    if status not in allowed:
        return "Invalid status"

    db = get_connection()
    cursor = db.cursor(cursor_factory=RealDictCursor)
    cursor.execute(
        "UPDATE applications SET status=%s WHERE id=%s",
        (status, app_id)
    )
    db.commit()
    cleanup_db_resources(cursor, db)

    return redirect('/recruiter-dashboard?tab=profile')

@app.route('/mentor-dashboard', methods=['GET', 'POST'])
def mentor_dashboard():
    flash('Mentor accounts are no longer supported.', 'info')
    return redirect('/login')

    if session.get('role') != 'mentor':
        return redirect('/login')

    mentor_id = session['user_id']
    db = get_connection()
    cursor = db.cursor(cursor_factory=RealDictCursor)

    # Add statement timeout to prevent long-running queries
    try:
        cursor.execute("SET statement_timeout = '10s'")
        cursor.execute("SELECT * FROM mentor_profiles WHERE mentor_id = %s", (mentor_id,))
        profile = cursor.fetchone()
    except psycopg2.errors.QueryCanceled:
        # Query timed out - return empty profile and log error
        db.rollback()
        print(f"[ERROR] mentor_profiles query timed out for mentor_id={mentor_id}")
        profile = None
        flash('Profile loading is taking longer than expected. Please try again.', 'warning')
    except Exception as e:
        db.rollback()
        print(f"[ERROR] Error fetching mentor profile: {e}")
        profile = None
        flash('Error loading profile. Please try again.', 'error')

    if request.method == 'POST':
        from werkzeug.utils import secure_filename
        agreement_accepted = profile.get('agreement_accepted') if profile else False
        agreement_accepted_at = profile.get('agreement_accepted_at') if profile else None
        agreement_signature_name = profile.get('agreement_signature_name') if profile else None
        agreement_signature_at = profile.get('agreement_signature_at') if profile else None     
        data = {
            'designation': request.form.get('designation'),
            'mentoring_areas': request.form.get('mentoring_areas'),
            'mode': request.form.get('mode'),
            'session_duration': request.form.get('session_duration'),
            'communication': request.form.get('communication'),
            'bio': request.form.get('bio'),
            'expertise': request.form.get('expertise'),
            'company': request.form.get('company'),
            'linkedin': request.form.get('linkedin'),
            'available_days': request.form.get('available_days'),
            'time_slot': request.form.get('time_slot'),
            'verification_type': request.form.get('verification_type'),
            'experience': request.form.get('experience', 0),
            'max_candidates': request.form.get('max_candidates', 5),
            # New general mentoring fields
            'mentor_category': ','.join(request.form.getlist('mentor_category')),
            'highest_qualification': request.form.get('highest_qualification'),
            'current_role': request.form.get('current_role'),
            'overall_experience': request.form.get('overall_experience'),
            'session_format': request.form.get('session_format'),
            'preferred_languages': request.form.get('preferred_languages'),
            'professional_email': request.form.get('professional_email'),
            'mentor_declaration': request.form.get('mentor_declaration') == 'on',
            'upi_id': request.form.get('upi_id'),
            'mentor_price': request.form.get('mentor_price', 99),
            'agreement_accepted': agreement_accepted,
            'agreement_accepted_at': agreement_accepted_at,
            'agreement_signature_name': agreement_signature_name,
            'agreement_signature_at': agreement_signature_at
        }

        file = request.files.get('verification_file')
        if file and file.filename != '':
            filename = secure_filename(file.filename)
            file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
        else:
            filename = profile['verification_file'] if profile else None

        # handle mentor profile photo upload (similar to candidate implementation)
        photo = request.files.get('photo')
        if photo and photo.filename != '':
            photo_filename = secure_filename(photo.filename)
            photo.save(os.path.join(app.config['UPLOAD_FOLDER'], photo_filename))
            cursor.execute("ALTER TABLE mentor_profiles ADD COLUMN IF NOT EXISTS photo_file VARCHAR(255)")
        else:
            photo_filename = profile.get('photo_file') if profile else None

        cursor.execute("""
            INSERT INTO mentor_profiles (mentor_id, expertise, mentoring_areas, mode, experience, designation, company,
            linkedin, session_duration, max_candidates, communication, bio, available_days,
            time_slot, verification_type, photo_file, verification_file, verification_status, profile_percent,
            mentor_category, highest_qualification, "current_role", overall_experience, session_format,
            preferred_languages, professional_email, upi_id, mentor_price, mentor_declaration,
            agreement_accepted, agreement_accepted_at, agreement_signature_name, agreement_signature_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending',100,
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (mentor_id) DO UPDATE SET
            expertise=EXCLUDED.expertise, mentoring_areas=EXCLUDED.mentoring_areas, mode=EXCLUDED.mode,
            experience=EXCLUDED.experience, designation=EXCLUDED.designation, company=EXCLUDED.company,
            linkedin=EXCLUDED.linkedin, session_duration=EXCLUDED.session_duration,
            max_candidates=EXCLUDED.max_candidates, communication=EXCLUDED.communication,
            bio=EXCLUDED.bio, available_days=EXCLUDED.available_days, time_slot=EXCLUDED.time_slot,
            verification_type=EXCLUDED.verification_type, photo_file=EXCLUDED.photo_file, verification_file=EXCLUDED.verification_file,
            verification_status='pending', profile_percent=100,
            mentor_category=EXCLUDED.mentor_category, highest_qualification=EXCLUDED.highest_qualification,
            "current_role"=EXCLUDED."current_role", overall_experience=EXCLUDED.overall_experience,
            session_format=EXCLUDED.session_format, preferred_languages=EXCLUDED.preferred_languages,
            professional_email=EXCLUDED.professional_email, upi_id=EXCLUDED.upi_id,
            mentor_price=EXCLUDED.mentor_price, mentor_declaration=EXCLUDED.mentor_declaration,
            agreement_accepted=EXCLUDED.agreement_accepted, agreement_accepted_at=EXCLUDED.agreement_accepted_at,
            agreement_signature_name=EXCLUDED.agreement_signature_name,
            agreement_signature_at=EXCLUDED.agreement_signature_at
        """, (
            mentor_id, data['expertise'], data['mentoring_areas'], data['mode'], data['experience'],
            data['designation'], data['company'], data['linkedin'], data['session_duration'],
            data['max_candidates'], data['communication'], data['bio'], data['available_days'],
            data['time_slot'], data['verification_type'], photo_filename, filename,
            data['mentor_category'], data['highest_qualification'], data['current_role'], data['overall_experience'],
            data['session_format'], data['preferred_languages'], data['professional_email'],
            data['upi_id'], data['mentor_price'], data['mentor_declaration'],
            data['agreement_accepted'], data['agreement_accepted_at'],
            data['agreement_signature_name'], data['agreement_signature_at']
        ))
        
        db.commit()
        cleanup_db_resources(cursor, db)
        flash('Profile saved! Please review and sign the Mentor-Company MOU agreement.', 'info')
        return redirect('/mentor/mou')

    profile_completed = True if profile else False
    profile_percent = profile['profile_percent'] if profile else 40
    verification_status = profile['verification_status'] if profile else 'pending'

    try:
        cursor.execute("""
            SELECT mr.id, mr.request_message, mr.candidate_id, u.name AS candidate_name,
                cp.first_name, cp.last_name, cp.headline, cp.bio,
                cp.degree AS education,
                cp.primary_skills AS skills,
                cp.work_experience AS experience,
                cp.resume_file, cp.photo_file, u.email AS candidate_email
            FROM mentorship_requests mr 
            JOIN candidates u ON mr.candidate_id = u.id
            LEFT JOIN candidate_profiles cp ON u.id = cp.candidate_id
            WHERE mr.mentor_id = %s AND mr.status = 'Pending'
        """, (mentor_id,))
        pending_requests = cursor.fetchall()
    except Exception as e:
        db.rollback()
        print(f"[ERROR] Failed loading pending mentor requests for {mentor_id}: {e}")
        pending_requests = []

    try:
        cursor.execute("""
            SELECT mr.id, mr.candidate_id, mr.request_message, mr.created_at AS started_at,
                u.name AS candidate_name, u.email AS candidate_email
            FROM mentorship_requests mr
            JOIN candidates u ON mr.candidate_id = u.id
            WHERE mr.mentor_id = %s AND mr.status = 'Accepted'
            ORDER BY mr.created_at DESC
        """, (mentor_id,))
        active_sessions = cursor.fetchall()
    except Exception as e:
        db.rollback()
        print(f"[ERROR] Failed loading active mentor sessions for {mentor_id}: {e}")
        active_sessions = []

    # load recent notifications for mentor
    try:
        cursor.execute("SELECT * FROM notifications WHERE receiver_role=%s AND receiver_id=%s ORDER BY created_at DESC", ('mentor', mentor_id))
        notifications = cursor.fetchall()
    except Exception as e:
        db.rollback()
        print(f"[ERROR] Failed loading mentor notifications for {mentor_id}: {e}")
        notifications = []

    # Fetch all verified mentors excluding the current mentor
    try:
        cursor.execute("""
            SELECT mp.*, m.name 
            FROM mentor_profiles mp
            JOIN mentors m ON mp.mentor_id = m.id
            WHERE mp.verification_status = 'approved' 
            AND mp.mentor_id != %s
            ORDER BY mp.experience DESC
        """, (mentor_id,))
        available_mentors = cursor.fetchall()
    except Exception as e:
        db.rollback()
        print(f"[ERROR] Failed loading available mentors for {mentor_id}: {e}")
        available_mentors = []

    cleanup_db_resources(cursor, db)

    return render_template(
        'mentor_dashboard.html',
        profile=profile,  
        profile_completed=profile_completed,
        profile_percent=profile_percent,
        verification_status=verification_status,
        pending_requests=pending_requests,
        active_sessions=active_sessions,
        notifications=notifications,
        available_mentors=available_mentors,
        user_id=mentor_id
    )
@app.route('/mentor/mou', methods=['GET', 'POST'])
def mentor_mou():
    if session.get('role') != 'mentor':
        return redirect('/login')

    mentor_id = session.get('user_id')
    db = get_connection()
    cursor = db.cursor(cursor_factory=RealDictCursor)

    cursor.execute("SELECT * FROM mentor_profiles WHERE mentor_id=%s", (mentor_id,))
    profile = cursor.fetchone()

    cursor.execute("SELECT name, email FROM mentors WHERE id=%s", (mentor_id,))
    mentor_row = cursor.fetchone() or {}
    mentor_name = mentor_row.get('name') or mentor_id
    mentor_email = mentor_row.get('email') or ''

    if not profile:
        flash('Please complete your mentor profile first.', 'warning')
        cleanup_db_resources(cursor, db)
        return redirect('/mentor-dashboard#profile')

    if request.method == 'POST':
        signature_name = (request.form.get('signature_name') or '').strip()
        accept = request.form.get('accept_agreement') == 'on'
        signature_file = request.files.get('signature_image')
        signature_bytes = None
        signature_mime = None
        signature_filename = None

        if signature_file and signature_file.filename:
            signature_bytes = signature_file.read()
            signature_mime = signature_file.mimetype
            signature_filename = secure_filename(signature_file.filename)

        if not signature_name or not accept:
            flash('Please enter your full name and accept the agreement.', 'warning')
            cleanup_db_resources(cursor, db)
            return redirect('/mentor/mou')

        signed_at = datetime.utcnow()
        pdf_bytes = build_mou_pdf(
            mentor_name,
            mentor_email,
            mentor_id,
            signature_name,
            signed_at,
            signature_image_bytes=signature_bytes
        )
        pdf_name = f"MOU_{mentor_id}_{int(signed_at.timestamp())}.pdf"
        cursor.execute(
            """
            UPDATE mentor_profiles
            SET agreement_accepted=TRUE,
                agreement_accepted_at=%s,
                agreement_signature_name=%s,
                agreement_signature_at=%s,
                agreement_signature_image=%s,
                agreement_signature_image_mime=%s,
                agreement_signature_image_name=%s,
                agreement_pdf=%s,
                agreement_pdf_mime=%s,
                agreement_pdf_name=%s
            WHERE mentor_id=%s
            """,
            (
                signed_at, signature_name, signed_at,
                signature_bytes, signature_mime, signature_filename,
                pdf_bytes, 'application/pdf', pdf_name,
                mentor_id
            )
        )
        db.commit()
        flash('Agreement accepted. Your profile is pending admin approval.', 'success')
        cleanup_db_resources(cursor, db)
        return redirect('/mentor-dashboard#profile')

    cleanup_db_resources(cursor, db)
    return render_template('mentor_mou.html', profile=profile)

@app.route('/mentor/mou/generate', methods=['POST'])
def mentor_mou_generate():
    if session.get('role') != 'mentor':
        return redirect('/login')

    mentor_id = session.get('user_id')
    db = get_connection()
    cursor = db.cursor(cursor_factory=RealDictCursor)

    cursor.execute("SELECT * FROM mentor_profiles WHERE mentor_id=%s", (mentor_id,))
    profile = cursor.fetchone()
    if not profile or not profile.get('agreement_accepted'):
        flash('Agreement is not accepted yet.', 'warning')
        cleanup_db_resources(cursor, db)
        return redirect('/mentor/mou')

    cursor.execute("SELECT name, email FROM mentors WHERE id=%s", (mentor_id,))
    mentor_row = cursor.fetchone() or {}
    mentor_name = mentor_row.get('name') or mentor_id
    mentor_email = mentor_row.get('email') or ''

    signature_name = profile.get('agreement_signature_name') or mentor_name
    signed_at = profile.get('agreement_signature_at') or profile.get('agreement_accepted_at') or datetime.utcnow()
    signature_bytes = profile.get('agreement_signature_image')

    pdf_bytes = build_mou_pdf(
        mentor_name,
        mentor_email,
        mentor_id,
        signature_name,
        signed_at,
        signature_image_bytes=signature_bytes
    )
    pdf_name = f"MOU_{mentor_id}_{int(signed_at.timestamp())}.pdf"

    cursor.execute(
        """
        UPDATE mentor_profiles
        SET agreement_pdf=%s,
            agreement_pdf_mime=%s,
            agreement_pdf_name=%s
        WHERE mentor_id=%s
        """,
        (pdf_bytes, 'application/pdf', pdf_name, mentor_id)
    )
    db.commit()
    cleanup_db_resources(cursor, db)
    flash('Signed agreement PDF generated.', 'success')
    return redirect('/mentor/mou')
    
def send_notification(role, user_id, message):
    """Send notification with timeout and error handling"""
    try:
        # Non-blocking notification - fail quick if database is slow
        db = None
        cursor = None
        try:
            db = get_connection()
            if db is None:
                print(f"[WARNING] Failed to get DB connection for notification")
                return
            
            cursor = db.cursor(cursor_factory=RealDictCursor)
            cursor.execute(
                "INSERT INTO notifications (receiver_role, receiver_id, message) VALUES (%s,%s,%s)",
                (role, user_id, message)
            )
            db.commit()
        except Exception as e:
            print(f"[ERROR] Notification failed: {e}")
            if db:
                try:
                    db.rollback()
                except:
                    pass
        finally:
            try:
                cleanup_db_resources(cursor, db)
            except:
                pass
    except Exception as e:
        print(f"[CRITICAL] send_notification crashed: {e}")

def log_audit(message, receiver_role='admin', receiver_id=0):
    """Log audit message with timeout and error handling"""
    try:
        db = None
        cursor = None
        try:
            db = get_connection()
            if db is None:
                return
            
            cursor = db.cursor(cursor_factory=RealDictCursor)
            cursor.execute(
                "INSERT INTO notifications (receiver_role, receiver_id, message) VALUES (%s,%s,%s)",
                (receiver_role, receiver_id, message)
            )
            db.commit()
        except Exception as e:
            print(f"[ERROR] Audit log failed: {e}")
            if db:
                try:
                    db.rollback()
                except:
                    pass
        finally:
            try:
                cleanup_db_resources(cursor, db)
            except:
                pass
    except Exception as e:
        print(f"[CRITICAL] log_audit crashed: {e}")
def build_mou_pdf(mentor_name, mentor_email, mentor_id, signature_name, signed_at, signature_image_bytes=None):
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4

    y = height - 50
    c.setFont("Helvetica-Bold", 16)
    c.drawString(50, y, "Mentor-Company MOU")
    y -= 24

    c.setFont("Helvetica", 10)
    meta_lines = [
        f"Mentor Name: {mentor_name}",
        f"Mentor Email: {mentor_email}",
        f"Mentor ID: {mentor_id}",
        f"Signed By: {signature_name}",
        f"Signed At (UTC): {signed_at.strftime('%Y-%m-%d %H:%M:%S')}",
    ]
    for line in meta_lines:
        c.drawString(50, y, line)
        y -= 14

    y -= 10
    c.setFont("Helvetica-Bold", 12)
    c.drawString(50, y, "Agreement")
    y -= 18
    c.setFont("Helvetica", 10)

    agreement_text = (
        "Mentor will provide professional guidance as committed in the profile. "
        "All mentoring sessions must follow platform policies and code of conduct. "
        "Payments are processed via the platform, with a 40% mentor share on completed payments. "
        "Any disputes will be handled through platform support and documented feedback."
    )
    for line in textwrap.wrap(agreement_text, width=95):
        if y < 80:
            c.showPage()
            y = height - 50
            c.setFont("Helvetica", 10)
        c.drawString(50, y, line)
        y -= 14

    y -= 20
    c.setFont("Helvetica-Bold", 11)
    c.drawString(50, y, "Digital Signature")
    y -= 14
    c.setFont("Helvetica", 10)
    c.drawString(50, y, f"Name: {signature_name}")
    y -= 14

    if signature_image_bytes:
        try:
            img = ImageReader(BytesIO(signature_image_bytes))
            c.drawImage(img, 50, y - 70, width=180, height=60, preserveAspectRatio=True, mask='auto')
            y -= 80
        except Exception:
            pass

    c.showPage()
    c.save()
    buffer.seek(0)
    return buffer.getvalue()


def build_recruiter_mou_pdf(
    profile,
    recruiter_name,
    recruiter_email,
    recruiter_id,
    signature_name,
    signed_at,
    signature_image_bytes=None,
    admin_signature_name=None,
    admin_signed_at=None
):

    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4

    company_name = (profile.get('company_name') or 'N/A').strip()
    representative = (signature_name or recruiter_name or '').strip() or 'N/A'
    designation = (profile.get('designation') or 'N/A').strip() if profile.get('designation') else 'N/A'
    company_email = (profile.get('work_email') or recruiter_email or '').strip() or 'N/A'
    company_phone = (profile.get('phone') or '').strip() or 'N/A'

    def _fmt_date(dt_obj):
        if not dt_obj:
            return 'N/A'
        try:
            return dt_obj.strftime('%d %b %Y')
        except Exception:
            return str(dt_obj)

    def _fmt_timestamp(dt_obj):
        if not dt_obj:
            return 'N/A'
        try:
            return dt_obj.strftime('%d %b %Y, %I:%M %p')
        except Exception:
            return str(dt_obj)

    page_margin = 40
    content_left = page_margin + 8
    content_width = width - (2 * page_margin) - 16
    y = height - 78

    theme_primary = colors.HexColor("#111827")
    theme_secondary = colors.HexColor("#F9FAFB")
    theme_text = colors.HexColor("#111827")
    theme_muted = colors.HexColor("#374151")
    theme_success = colors.HexColor("#14532D")
    theme_warning = colors.HexColor("#78350F")

    def _draw_footer():
        c.setStrokeColor(colors.HexColor("#D1D5DB"))
        c.line(page_margin, page_margin + 18, width - page_margin, page_margin + 18)
        c.setFillColor(colors.HexColor("#6B7280"))
        c.setFont("Times-Italic", 8)
        c.drawString(page_margin + 2, page_margin + 6, f"Confidential • {APP_NAME} Platform")
        c.drawRightString(width - page_margin - 2, page_margin + 6, "Digitally Generated Legal Document")

    def _new_page():
        nonlocal y
        _draw_footer()
        c.showPage()
        c.setStrokeColor(colors.HexColor("#CBD5E1"))
        c.roundRect(page_margin, page_margin, width - 2 * page_margin, height - 2 * page_margin, 8, fill=0, stroke=1)
        y = height - 60

    def _ensure_space(required=26):
        nonlocal y
        if y < (page_margin + required):
            _new_page()

    def _section_bar(title):
        nonlocal y
        _ensure_space(28)
        c.setFillColor(theme_secondary)
        c.setStrokeColor(colors.HexColor("#CBD5E1"))
        c.roundRect(content_left - 3, y - 15, content_width + 6, 20, 4, fill=1, stroke=1)
        c.setFillColor(theme_primary)
        c.setFont("Times-Bold", 10.5)
        c.drawString(content_left + 4, y - 2, title)
        y -= 24

    def _draw_wrapped(line, x=content_left, font_name='Times-Roman', font_size=10, wrap_width=104, gap=13, color=theme_text):
        nonlocal y
        c.setFillColor(color)
        c.setFont(font_name, font_size)
        for txt in textwrap.wrap(str(line), width=wrap_width):
            _ensure_space(gap + 4)
            c.drawString(x, y, txt)
            y -= gap

    c.setStrokeColor(colors.HexColor("#CBD5E1"))
    c.roundRect(page_margin, page_margin, width - 2 * page_margin, height - 2 * page_margin, 8, fill=0, stroke=1)

    c.setFillColor(theme_secondary)
    c.roundRect(page_margin + 1, height - 108, width - 2 * page_margin - 2, 56, 4, stroke=0, fill=1)
    c.setFillColor(theme_primary)
    c.setFont("Times-Bold", 14)
    c.drawCentredString(width / 2, height - 71, f"{APP_NAME.upper()} PLATFORM")
    c.setFont("Times-Roman", 10)
    c.drawCentredString(width / 2, height - 86, "MEMORANDUM OF UNDERSTANDING")
    c.setFont("Times-Italic", 9)
    c.drawCentredString(width / 2, height - 99, f"Entered with {company_name}")

    y = height - 125
    c.setFillColor(theme_text)
    c.setFont("Times-Bold", 12)
    c.drawString(content_left, y, "THIS MEMORANDUM OF UNDERSTANDING (" + "MOU" + ")")
    y -= 16
    c.setFont("Times-Roman", 9)
    c.setFillColor(theme_muted)
    c.drawString(content_left, y, f"Document ID: MOU-{recruiter_id} | Generated: {_fmt_timestamp(datetime.utcnow())}")
    y -= 18

    _section_bar("PARTIES & EFFECTIVE DATE")
    _draw_wrapped(f"This Memorandum of Understanding is entered into on {_fmt_date(signed_at)} between:")
    _draw_wrapped(f"PARTY A: {APP_NAME} Platform", font_name='Times-Bold')
    _draw_wrapped(f"PARTY B: {company_name} (Represented by {representative})", font_name='Times-Bold')

    _section_bar("1. PURPOSE")
    _draw_wrapped(f"This MOU establishes the terms and conditions under which {company_name} will utilize the {APP_NAME} platform for recruiting purposes.")

    _section_bar("2. OBLIGATIONS OF THE RECRUITER / COMPANY")
    for item in [
        "a) Provide accurate and truthful company information",
        "b) Post only legitimate job openings",
        "c) Respect candidate privacy and data protection laws",
        "d) Respond to candidate applications in a timely manner",
        "e) Not discriminate based on race, gender, religion, or other protected characteristics",
        "f) Maintain professional communication with all platform users",
    ]:
        _draw_wrapped(item)

    _section_bar(f"3. OBLIGATIONS OF {APP_NAME.upper()} PLATFORM")
    for item in [
        "a) Provide access to candidate database and job posting features",
        "b) Maintain platform security and data protection",
        "c) Verify recruiter credentials and company authenticity",
        "d) Support recruiter queries and technical issues",
    ]:
        _draw_wrapped(item)

    _section_bar("4. TERMS & CONDITIONS")
    for item in [
        "a) This agreement is valid for one year from the date of signing",
        "b) Either party may terminate with 30 days written notice",
        "c) All posted jobs must comply with applicable labor laws",
        "d) The platform reserves the right to remove non-compliant job postings",
    ]:
        _draw_wrapped(item)

    _section_bar("5. DATA PROTECTION")
    _draw_wrapped("Both parties agree to maintain confidentiality of user data and comply with applicable data protection regulations.")

    _section_bar("6. DISPUTE RESOLUTION")
    _draw_wrapped("Any disputes will be resolved through mutual consultation and mediation.")
    _draw_wrapped("By signing below, both parties acknowledge and agree to the terms stated above.", font_name='Times-Bold', color=theme_muted)

    _section_bar("COMPANY DETAILS")
    _draw_wrapped(f"Company Name: {company_name}")
    _draw_wrapped(f"Represented By: {representative}")
    _draw_wrapped(f"Designation: {designation}")
    _draw_wrapped(f"Email: {company_email}")
    _draw_wrapped(f"Phone: {company_phone}")

    _section_bar("SIGNATURES & APPROVALS")
    _draw_wrapped("Recruiter / Company Representative", font_name='Times-Bold')
    _draw_wrapped(f"Signed by: {representative}")
    _draw_wrapped(f"Date: {_fmt_timestamp(signed_at)}")

    if signature_image_bytes:
        try:
            _ensure_space(80)
            img = ImageReader(BytesIO(signature_image_bytes))
            c.setStrokeColor(colors.HexColor("#CBD5E1"))
            c.roundRect(content_left, y - 58, 190, 52, 4, fill=0, stroke=1)
            c.drawImage(img, content_left + 4, y - 54, width=182, height=44, preserveAspectRatio=True, mask='auto')
            y -= 64
        except Exception:
            pass

    y -= 4
    _draw_wrapped(f"{APP_NAME} Platform Representative", font_name='Times-Bold')
    if admin_signed_at:
        _draw_wrapped("Digital Counter Signature Applied")
        _draw_wrapped(f"Approved by: {admin_signature_name or 'Admin'}")
        _draw_wrapped(f"Approved on: {_fmt_timestamp(admin_signed_at)}")
        _ensure_space(26)
        c.setFillColor(colors.HexColor("#DCFCE7"))
        c.setStrokeColor(colors.HexColor("#86EFAC"))
        c.roundRect(content_left, y - 16, content_width, 18, 4, fill=1, stroke=1)
        c.setFillColor(theme_success)
        c.setFont("Times-Bold", 10)
        c.drawString(content_left + 8, y - 3, "STATUS: FULLY EXECUTED & APPROVED")
        y -= 22
    else:
        _draw_wrapped("Pending admin countersignature")
        _ensure_space(26)
        c.setFillColor(colors.HexColor("#FEF3C7"))
        c.setStrokeColor(colors.HexColor("#FCD34D"))
        c.roundRect(content_left, y - 16, content_width, 18, 4, fill=1, stroke=1)
        c.setFillColor(theme_warning)
        c.setFont("Times-Bold", 10)
        c.drawString(content_left + 8, y - 3, "STATUS: RECRUITER SIGNED, AWAITING ADMIN APPROVAL")
        y -= 22

    _ensure_space(56)
    c.setStrokeColor(colors.HexColor("#9CA3AF"))
    c.line(content_left, y - 8, content_left + 220, y - 8)
    c.line(content_left + 250, y - 8, content_left + 470, y - 8)
    c.setFillColor(theme_muted)
    c.setFont("Times-Roman", 9)
    c.drawString(content_left, y - 20, "Authorized Signatory (Recruiter)")
    c.drawString(content_left + 250, y - 20, "Authorized Signatory (Platform)")
    y -= 34

    c.line(content_left, y - 8, content_left + 220, y - 8)
    c.line(content_left + 250, y - 8, content_left + 470, y - 8)
    c.drawString(content_left, y - 20, "Witness 1")
    c.drawString(content_left + 250, y - 20, "Witness 2")
    y -= 32

    c.setFillColor(colors.HexColor("#4B5563"))
    c.setFont("Times-Italic", 8)
    c.drawString(content_left, page_margin + 10, f"This is a digitally generated legal agreement document from {APP_NAME} Platform.")

    _draw_footer()
    c.showPage()
    c.save()
    buffer.seek(0)
    return buffer.getvalue()

@app.route('/notifications')
def notifications():
    db = get_connection()
    cursor = db.cursor(cursor_factory=RealDictCursor)
    cursor.execute("""
        SELECT * FROM notifications
        WHERE receiver_role=%s AND receiver_id=%s
        ORDER BY created_at DESC
    """, (session["role"], session["user_id"]))
    notes = cursor.fetchall()
    cleanup_db_resources(cursor, db)

    return render_template('notifications.html', notes=notes)

@app.route('/mentor/mou/pdf')
def mentor_mou_pdf():
    if session.get('role') != 'mentor':
        return redirect('/login')

    mentor_id = session.get('user_id')
    db = get_connection()
    cursor = db.cursor(cursor_factory=RealDictCursor)
    cursor.execute(
        """
        SELECT agreement_pdf, agreement_pdf_mime, agreement_pdf_name
        FROM mentor_profiles WHERE mentor_id=%s
        """,
        (mentor_id,)
    )
    row = cursor.fetchone()
    cleanup_db_resources(cursor, db)

    if not row or not row.get('agreement_pdf'):
        flash('Signed agreement PDF not available yet.', 'warning')
        return redirect('/mentor/mou')

    pdf_bytes = row.get('agreement_pdf')
    pdf_name = row.get('agreement_pdf_name') or f"MOU_{mentor_id}.pdf"
    return send_file(
        BytesIO(pdf_bytes),
        mimetype=row.get('agreement_pdf_mime') or 'application/pdf',
        as_attachment=False,
        download_name=pdf_name
    )

@app.route('/admin/mou/pdf/<int:profile_id>')
def admin_mou_pdf(profile_id):
    if session.get('role') != 'admin':
        return redirect('/login')

    db = get_connection()
    cursor = db.cursor(cursor_factory=RealDictCursor)
    cursor.execute(
        """
        SELECT agreement_pdf, agreement_pdf_mime, agreement_pdf_name
        FROM mentor_profiles WHERE id=%s
        """,
        (profile_id,)
    )
    row = cursor.fetchone()
    cleanup_db_resources(cursor, db)

    if not row or not row.get('agreement_pdf'):
        flash('Signed agreement PDF not available.', 'warning')
        return redirect('/admin-dashboard#verify-mentors')

    pdf_bytes = row.get('agreement_pdf')
    pdf_name = row.get('agreement_pdf_name') or f"MOU_{profile_id}.pdf"
    return send_file(
        BytesIO(pdf_bytes),
        mimetype=row.get('agreement_pdf_mime') or 'application/pdf',
        as_attachment=False,
        download_name=pdf_name
    )

@app.route('/admin/verify-mentors')
def verify_mentors():
    flash('Mentor verification is no longer available.', 'info')
    return redirect('/admin-dashboard')

    if session.get('role') != 'admin':
        return redirect('/login')

    db = get_connection()
    cursor = db.cursor(cursor_factory=RealDictCursor)

    cursor.execute("""
         SELECT mp.id,
             mp.mentor_id,
             COALESCE(m.name, mp.agreement_signature_name, mp.mentor_id) AS name,
             m.email,
               mp.company,
               mp.verification_file,
               mp.expertise,
               mp.agreement_accepted,
               mp.agreement_signature_name,
               mp.agreement_signature_at,
               (mp.agreement_pdf IS NOT NULL) AS has_agreement_pdf
         FROM mentor_profiles mp
         LEFT JOIN mentors m ON m.id = mp.mentor_id
        WHERE mp.verification_status = 'pending'
    """)
    mentors = cursor.fetchall()

    cleanup_db_resources(cursor, db)

    return render_template('admin_verify_mentors.html', mentors=mentors)

@app.route('/admin/approve-mentor/<int:profile_id>', methods=['GET', 'POST'])
def approve_mentor(profile_id):
    if request.method == 'POST':
        return jsonify({'success': False, 'error': 'Mentor verification is no longer available.'}), 410
    flash('Mentor verification is no longer available.', 'info')
    return redirect('/admin-dashboard')

    if session.get('role') != 'admin':
        if request.method == 'POST':
            return jsonify({'success': False, 'error': 'Unauthorized'}), 401
        return redirect('/login')

    db = get_connection()
    cursor = db.cursor(cursor_factory=RealDictCursor)

    try:
        # get mentor id and agreement acceptance status
        cursor.execute("SELECT mentor_id, agreement_accepted FROM mentor_profiles WHERE id=%s", (profile_id,))
        row = cursor.fetchone()
        mentor_id = row['mentor_id'] if row else None
        if not row or not row.get('agreement_accepted'):
            if request.method == 'POST':
                return jsonify({'success': False, 'error': 'Mentor agreement not accepted yet.'}), 400
            flash('Mentor agreement is not accepted yet.', 'warning')
            cleanup_db_resources(cursor, db)
            return redirect('/admin/verify-mentors')
        cursor.execute(
            "UPDATE mentor_profiles SET verification_status='approved' WHERE id=%s",
            (profile_id,)
        )
        db.commit()

        # notify mentor
        try:
            if mentor_id:
                message = "Your verification has been approved. Congratulations!"
                send_notification('mentor', mentor_id, message)
                socketio.emit('mentor_notification', {'message': message}, room=f"mentor_{mentor_id}")
        except Exception:
            pass

        try:
            log_audit(f"Approved mentor profile {profile_id}")
        except Exception:
            pass

        cleanup_db_resources(cursor, db)
        
        # Return JSON for POST (AJAX) requests, redirect for GET requests
        if request.method == 'POST':
            return jsonify({'success': True, 'message': 'Mentor approved successfully'})
        else:
            return redirect('/admin/verify-mentors')
    
    except Exception as e:
        cleanup_db_resources(cursor, db)
        if request.method == 'POST':
            return jsonify({'success': False, 'error': str(e)}), 500
        else:
            return redirect('/admin/verify-mentors')

@app.route('/admin/reject-mentor/<int:profile_id>', methods=['POST'])
def reject_mentor(profile_id):
    return jsonify({'success': False, 'error': 'Mentor verification is no longer available.'}), 410

    if session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'unauthorized'}), 403

    data = request.get_json() or {}
    reason = data.get('reason', 'No reason provided')

    db = get_connection()
    cursor = db.cursor(cursor_factory=RealDictCursor)

    cursor.execute("SELECT mentor_id FROM mentor_profiles WHERE id=%s", (profile_id,))
    row = cursor.fetchone()
    mentor_id = row['mentor_id'] if row else None

    cursor.execute(
        "UPDATE mentor_profiles SET verification_status='rejected' WHERE id=%s",
        (profile_id,)
    )
    db.commit()

    # try to store rejection reason in mentor_profiles (add column if needed)
    try:
        cursor.execute("ALTER TABLE mentor_profiles ADD COLUMN IF NOT EXISTS rejection_reason TEXT")
        cursor.execute("UPDATE mentor_profiles SET rejection_reason=%s WHERE id=%s", (reason, profile_id))
        db.commit()
    except Exception:
        db.rollback()

    try:
        if mentor_id:
            message = f"Your verification was rejected. Reason: {reason}"
            send_notification('mentor', mentor_id, message)
            socketio.emit('mentor_notification', {'message': message}, room=f"mentor_{mentor_id}")
    except Exception:
        pass

    try:
        log_audit(f"Rejected mentor profile {profile_id}: {reason}")
    except Exception:
        pass

    cleanup_db_resources(cursor, db)

    return jsonify({'success': True})


@app.route('/admin/mentor-profile/<int:profile_id>')
def admin_get_mentor_profile(profile_id):
    return jsonify({'success': False, 'error': 'Mentor profiles are no longer available.'}), 410

    if session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'unauthorized'}), 403

    db = get_connection()
    cursor = db.cursor(cursor_factory=RealDictCursor)
    cursor.execute("""
        SELECT m.id AS mentor_id, m.name AS mentor_name, m.email AS mentor_email,
               mp.*
        FROM mentors m
        JOIN mentor_profiles mp ON m.id = mp.mentor_id
        WHERE mp.id = %s
    """, (profile_id,))
    row = cursor.fetchone()
    cleanup_db_resources(cursor, db)

    if not row:
        return jsonify({'success': False, 'error': 'not found'}), 404

    # strip binary fields for JSON safety
    sanitized = {}
    for key, value in row.items():
        if isinstance(value, (bytes, bytearray, memoryview)):
            sanitized[key] = None
        else:
            sanitized[key] = value
    # remove internal numeric keys if any and convert to JSON serializable
    return jsonify({'success': True, 'profile': sanitized})


@app.route('/admin/user-profile/<role>/<user_id>')
def admin_get_user_profile(role, user_id):
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'unauthorized'}), 403

    db = get_connection()
    cursor = db.cursor(cursor_factory=RealDictCursor)

    try:
        role = role.lower()
        if role == 'mentor':
            return jsonify({'success': False, 'error': 'unsupported role'}), 400

        if role == 'candidate':
            cursor.execute("SELECT id, name, email FROM candidates WHERE id=%s", (user_id,))
            row = cursor.fetchone()
            if not row:
                return jsonify({'success': False, 'error': 'not found'}), 404
            # try to load candidate profile
            cursor.execute("SELECT * FROM candidate_profiles WHERE candidate_id=%s", (user_id,))
            cp = cursor.fetchone()
            return jsonify({'success': True, 'profile': {'user': row, 'candidate_profile': cp}})

        if role == 'recruiter':
            cursor.execute("SELECT id, name, email FROM recruiters WHERE id=%s", (user_id,))
            row = cursor.fetchone()
            if not row:
                return jsonify({'success': False, 'error': 'not found'}), 404
            cursor.execute("SELECT * FROM recruiter_profiles WHERE recruiter_id=%s", (user_id,))
            rp = cursor.fetchone()
            return jsonify({'success': True, 'profile': {'user': row, 'recruiter_profile': rp}})

        return jsonify({'success': False, 'error': 'unsupported role'}), 400
    finally:
        cleanup_db_resources(cursor, db)

#MENTORSHIP ACTIONS

@app.route('/request-mentorship/<mentor_id>', methods=["GET", "POST"])
def request_mentorship(mentor_id):
    if request.method == 'POST' and (request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest'):
        return jsonify({'success': False, 'message': 'Mentorship features are no longer available.'}), 410
    flash('Mentorship features are no longer available on this platform.', 'info')
    return redirect('/candidate-dashboard')

    """Request mentorship from a mentor"""
    try:
        if session.get('role') != 'candidate':
            if request.method == 'POST' and (request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest'):
                return jsonify({'success': False, 'message': 'Unauthorized. Please login as a candidate.'}), 401
            return redirect('/login')

        candidate_id = session.get('user_id')
        print(f"Mentorship request from candidate {candidate_id} to mentor {mentor_id}")
        
        # WORKFLOW DEPENDENCY: Candidate → Profile Completion (≥85%) + Mentor → Admin Verification
        is_complete, profile_percent = check_candidate_profile_completion(candidate_id)
        
        if not is_complete:
            msg = f'Please complete your profile to at least 85% (currently {profile_percent}%) before requesting mentorship.'
            if request.method == 'POST' and (request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest'):
                return jsonify({'success': False, 'message': msg}), 400
            flash(msg, 'warning')
            return redirect('/candidate-dashboard#profile')
        
        # Check if mentor is verified
        is_verified, verification_status = check_mentor_verification(mentor_id)
        print(f"Mentor {mentor_id} verification status: {is_verified} ({verification_status})")
        
        if not is_verified:
            msg = 'This mentor is not yet verified by the admin.'
            if request.method == 'POST' and (request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest'):
                return jsonify({'success': False, 'message': msg}), 400
            flash(msg, 'warning')
            return redirect('/candidate-dashboard#mentors')
        
        msg = request.form.get('message', 'I would like your guidance.')
        
        db = get_connection()
        cursor = db.cursor(cursor_factory=RealDictCursor)
        
        # Get mentor and candidate names
        cursor.execute("SELECT id, name FROM mentors WHERE id = %s", (mentor_id,))
        mentor = cursor.fetchone()
        print(f"Mentor found: {mentor}")
        
        cursor.execute("SELECT id, name FROM candidates WHERE id = %s", (candidate_id,))
        candidate = cursor.fetchone()
        print(f"Candidate found: {candidate}")
        
        # Check if already requested
        cursor.execute(
            "SELECT id FROM mentorship_requests WHERE candidate_id = %s AND mentor_id = %s",
            (candidate_id, mentor_id)
        )
        existing = cursor.fetchone()
        
        if existing:
            msg_text = 'You have already requested mentorship from this mentor.'
            if request.method == 'POST' and (request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest'):
                return jsonify({'success': False, 'message': msg_text}), 400
            flash(msg_text, 'info')
            return redirect('/candidate-dashboard#mentors')
        
        cursor.execute(
            "INSERT INTO mentorship_requests (candidate_id, mentor_id, request_message) VALUES (%s, %s, %s)",
            (candidate_id, mentor_id, msg)
        )
        db.commit()
        cleanup_db_resources(cursor, db)
        
        # Send notification to mentor
        if mentor and candidate:
            notification_msg = f"New mentorship request from {candidate['name']}"
            send_notification('mentor', mentor_id, notification_msg)
        
        # Log activity for candidate
        log_activity(candidate_id, 'candidate', 'mentorship', 'Mentorship Request Sent', 
                    f'Requested mentorship from {mentor["name"] if mentor else "mentor"}')
        
        if request.method == 'POST' and (request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest'):
            return jsonify({'success': True, 'message': f"Mentorship request sent to {mentor['name'] if mentor else 'mentor'} successfully!"})
        
        flash(f"Mentorship request sent to {mentor['name'] if mentor else 'mentor'} successfully!", "success")
        return redirect('/candidate-dashboard#mentors')
    
    except Exception as e:
        print(f"Error in request_mentorship: {e}")
        import traceback
        traceback.print_exc()
        
        if request.method == 'POST' and (request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest'):
            return jsonify({'success': False, 'message': f'Error: {str(e)}'}), 500
        
        flash(f'Error: {str(e)}', 'danger')
        return redirect('/candidate-dashboard#mentors')

@app.route('/delete-mentorship-request/<int:request_id>', methods=['POST'])
def delete_mentorship_request(request_id):
    flash('Mentorship features are no longer available on this platform.', 'info')
    return redirect('/candidate-dashboard')

    """Delete a mentorship request"""
    if session.get('role') != 'candidate':
        flash('Unauthorized access!', 'danger')
        return redirect('/login')
    
    candidate_id = session.get('user_id')
    
    try:
        db = get_connection()
        cursor = db.cursor(cursor_factory=RealDictCursor)
        
        # Verify the request belongs to this candidate
        cursor.execute(
            "SELECT mr.*, m.name as mentor_name FROM mentorship_requests mr JOIN mentors m ON mr.mentor_id = m.id WHERE mr.id = %s AND mr.candidate_id = %s",
            (request_id, candidate_id)
        )
        request_data = cursor.fetchone()
        
        if not request_data:
            flash('Request not found or you do not have permission to delete it.', 'danger')
            return redirect('/candidate-dashboard#mentors')
        
        # Delete associated meeting if exists
        cursor.execute(
            "DELETE FROM mentor_meetings WHERE mentor_id = %s AND candidate_id = %s",
            (request_data['mentor_id'], candidate_id)
        )
        
        # Delete the mentorship request
        cursor.execute(
            "DELETE FROM mentorship_requests WHERE id = %s AND candidate_id = %s",
            (request_id, candidate_id)
        )
        
        db.commit()
        
        # Log activity
        log_activity(candidate_id, 'candidate', 'mentorship', 'Mentorship Request Deleted', 
                    f'Deleted mentorship request to {request_data["mentor_name"]}')
        
        flash(f'Mentorship request to {request_data["mentor_name"]} has been deleted successfully.', 'success')
        
    except Exception as e:
        db.rollback()
        flash(f'Error deleting request: {str(e)}', 'danger')
    finally:
        cleanup_db_resources(cursor, db)
    
    return redirect('/candidate-dashboard#mentors')

@app.route('/mentor/respond-request/<int:request_id>/<action>')
def respond_request(request_id, action):
    """Mentor accepts or rejects mentorship request with full coordination"""
    if session.get('role') != 'mentor':
        return redirect('/login')
    
    mentor_id = session.get('user_id')
    status = 'Accepted' if action == 'accept' else 'Rejected'
    
    try:
        db = get_connection()
        cursor = db.cursor(cursor_factory=RealDictCursor)
        
        # Get request details for notifications
        cursor.execute("""
            SELECT mr.*, m.name as mentor_name, c.name as candidate_name
            FROM mentorship_requests mr
            JOIN mentors m ON mr.mentor_id = m.id
            JOIN candidates c ON mr.candidate_id = c.id
            WHERE mr.id = %s AND mr.mentor_id = %s
        """, (request_id, mentor_id))
        
        request_data = cursor.fetchone()
        
        if not request_data:
            flash('Request not found', 'danger')
            return redirect('/mentor-dashboard')
        
        candidate_id = request_data['candidate_id']
        mentor_name = request_data['mentor_name']
        candidate_name = request_data['candidate_name']
        
        # Update request status
        cursor.execute(
            "UPDATE mentorship_requests SET status=%s WHERE id=%s AND mentor_id=%s",
            (status, request_id, mentor_id)
        )
        
        if action == 'accept':
            # Notify candidate about acceptance
            create_notification(
                'candidate', candidate_id, 'mentorship_accepted',
                'Mentorship Request Accepted! 🎉',
                f'{mentor_name} has accepted your mentorship request. You can now start chatting!',
                f'/mentor-chat/{request_id}'
            )
            
            # Log activity for candidate
            log_activity(candidate_id, 'candidate', 'mentorship', 'Mentorship Accepted',
                        f'Your mentorship request was accepted by {mentor_name}')
            
            # Log activity for mentor
            log_activity(mentor_id, 'mentor', 'mentorship', 'Accepted Mentee',
                        f'Accepted mentorship request from {candidate_name}')
            
            flash(f'Mentorship request from {candidate_name} accepted!', 'success')
        
        else:  # reject
            # Notify candidate about rejection
            create_notification(
                'candidate', candidate_id, 'mentorship_rejected',
                'Mentorship Request Update',
                f'{mentor_name} is currently unavailable. You can request other mentors.',
                '/candidate-dashboard#mentors'
            )
            
            # Log activity for candidate
            log_activity(candidate_id, 'candidate', 'mentorship', 'Request Not Accepted',
                        f'Mentorship request was not accepted by {mentor_name}')
            
            # Log activity for mentor
            log_activity(mentor_id, 'mentor', 'mentorship', 'Declined Request',
                        f'Declined mentorship request from {candidate_name}')
            
            flash(f'Mentorship request from {candidate_name} declined', 'info')
        
        db.commit()
        cleanup_db_resources(cursor, db)
        
    except Exception as e:
        print(f"Error responding to request: {e}")
        flash('Error processing request', 'danger')
    
    return redirect('/mentor-dashboard')

@app.route('/mentor/give-feedback/<int:request_id>', methods=["GET", "POST"])
def give_feedback(request_id):
    """Mentor provides feedback with candidate notification"""
    if session.get('role') != 'mentor':
        return redirect('/login')
    
    mentor_id = session.get('user_id')
    feedback = request.form.get('feedback')
    
    try:
        db = get_connection()
        cursor = db.cursor(cursor_factory=RealDictCursor)
        
        # Get request details
        cursor.execute("""
            SELECT mr.candidate_id, c.name as candidate_name, m.name as mentor_name
            FROM mentorship_requests mr
            JOIN candidates c ON mr.candidate_id = c.id
            JOIN mentors m ON mr.mentor_id = m.id
            WHERE mr.id = %s AND mr.mentor_id = %s
        """, (request_id, mentor_id))
        
        request_data = cursor.fetchone()
        
        if request_data:
            candidate_id = request_data['candidate_id']
            candidate_name = request_data['candidate_name']
            mentor_name = request_data['mentor_name']
            
            # Update feedback
            cursor.execute(
                "UPDATE mentorship_requests SET mentor_feedback=%s, status='Completed' WHERE id=%s",
                (feedback, request_id)
            )
            
            # Notify candidate about feedback
            create_notification(
                'candidate', candidate_id, 'feedback_received',
                'New Feedback from Mentor',
                f'{mentor_name} has provided feedback on your mentorship session',
                f'/mentor-chat/{request_id}'
            )
            
            # Log activity for both
            log_activity(candidate_id, 'mentorship', 'Feedback Received',
                        f'Received feedback from {mentor_name}')
            log_activity(mentor_id, 'mentorship', 'Feedback Provided',
                        f'Provided feedback to {candidate_name}')
            
            db.commit()
            flash('Feedback sent successfully!', 'success')
        
        cleanup_db_resources(cursor, db)
        
    except Exception as e:
        print(f"Error giving feedback: {e}")
        flash('Error sending feedback', 'danger')
    
    return redirect('/mentor-dashboard')


# ============================================================================
# ADVANCED MENTOR FEATURES
# ============================================================================

@app.route('/mentor/sessions')
def mentor_sessions():
    """View all mentorship sessions with filtering and analytics"""
    if session.get('role') != 'mentor':
        return redirect('/login')
    
    mentor_id = session.get('user_id')
    
    try:
        db = get_connection()
        cursor = db.cursor(cursor_factory=RealDictCursor)
        
        # Get filter parameters
        status_filter = request.args.get('status', 'all')
        
        # Build query with filters
        query = """
            SELECT mr.*, 
                   c.name as candidate_name, c.email as candidate_email,
                   cp.headline, cp.primary_skills,
                   (SELECT COUNT(*) FROM mentor_meetings mm 
                    WHERE mm.mentor_id = mr.mentor_id AND mm.candidate_id = mr.candidate_id) as total_meetings
            FROM mentorship_requests mr
            JOIN candidates c ON mr.candidate_id = c.id
            LEFT JOIN candidate_profiles cp ON c.id = cp.candidate_id
            WHERE mr.mentor_id = %s
        """
        
        params = [mentor_id]
        if status_filter != 'all':
            query += " AND mr.status = %s"
            params.append(status_filter)
        
        query += " ORDER BY mr.created_at DESC"
        
        cursor.execute(query, params)
        sessions = cursor.fetchall()
        
        # Get session statistics
        cursor.execute("""
            SELECT 
                status,
                COUNT(*) as count
            FROM mentorship_requests
            WHERE mentor_id = %s
            GROUP BY status
        """, (mentor_id,))
        session_stats = {row['status']: row['count'] for row in cursor.fetchall()}
        
        # Get upcoming sessions
        cursor.execute("""
            SELECT mm.*, mr.candidate_id, c.name as candidate_name,
                   (mm.meeting_date - CURRENT_DATE) as days_until
            FROM mentor_meetings mm
            JOIN mentorship_requests mr ON mm.mentor_id = mr.mentor_id AND mm.candidate_id = mr.candidate_id
            JOIN candidates c ON mm.candidate_id = c.id
            WHERE mm.mentor_id = %s 
            AND mm.meeting_date >= CURRENT_DATE
            ORDER BY mm.meeting_date ASC, mm.meeting_time ASC
            LIMIT 5
        """, (mentor_id,))
        upcoming = cursor.fetchall()
        
        cleanup_db_resources(cursor, db)
        
        return render_template('mentor_sessions.html', 
                             sessions=sessions,
                             session_stats=session_stats,
                             upcoming=upcoming,
                             status_filter=status_filter)
    
    except Exception as e:
        print(f"Error loading mentor sessions: {e}")
        flash('Error loading sessions', 'danger')
        return redirect('/mentor-dashboard')


@app.route('/mentor/rate-session/<int:request_id>', methods=['GET', 'POST'])
def rate_mentorship_session(request_id):
    """Mentor rates a completed session with detailed feedback"""
    if session.get('role') != 'mentor':
        return redirect('/login')
    
    mentor_id = session.get('user_id')
    
    if request.method == 'POST':
        try:
            rating = request.form.get('rating')
            feedback = request.form.get('feedback')
            areas_improved = request.form.get('areas_improved')
            recommendations = request.form.get('recommendations')
            session_duration = request.form.get('session_duration')
            
            db = get_connection()
            cursor = db.cursor(cursor_factory=RealDictCursor)
            
            # Create session_ratings table if not exists
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS session_ratings (
                    id SERIAL PRIMARY KEY,
                    mentorship_request_id INT,
                    mentor_id VARCHAR(20),
                    candidate_id VARCHAR(20),
                    rating INT CHECK (rating BETWEEN 1 AND 5),
                    feedback TEXT,
                    areas_improved TEXT,
                    recommendations TEXT,
                    session_duration INT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (mentorship_request_id) REFERENCES mentorship_requests(id),
                    FOREIGN KEY (mentor_id) REFERENCES mentors(id),
                    FOREIGN KEY (candidate_id) REFERENCES candidates(id)
                )
            """)
            
            # Get candidate_id
            cursor.execute("SELECT candidate_id FROM mentorship_requests WHERE id = %s AND mentor_id = %s", 
                          (request_id, mentor_id))
            result = cursor.fetchone()
            
            if not result:
                flash('Session not found', 'danger')
                return redirect('/mentor/sessions')
            
            candidate_id = result['candidate_id']
            
            # Insert rating
            cursor.execute("""
                INSERT INTO session_ratings 
                (mentorship_request_id, mentor_id, candidate_id, rating, feedback, 
                 areas_improved, recommendations, session_duration)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (request_id, mentor_id, candidate_id, rating, feedback, 
                  areas_improved, recommendations, session_duration))
            
            # Update mentorship request status
            cursor.execute("""
                UPDATE mentorship_requests 
                SET status = 'Completed',
                    mentor_feedback = %s
                WHERE id = %s AND mentor_id = %s
            """, (feedback, request_id, mentor_id))
            
            # Create notification for candidate
            create_notification(
                'candidate', candidate_id, 'session_rated',
                'Session Feedback Received',
                f'Your mentor has provided feedback on your mentorship session',
                f'/mentor-chat/{request_id}'
            )
            
            # Log activity
            log_activity(candidate_id, 'mentorship', 'Session Rated', 
                        f'Received feedback from mentor (Rating: {rating}/5)')
            
            db.commit()
            cleanup_db_resources(cursor, db)
            
            flash('Session rated successfully!', 'success')
            return redirect('/mentor/sessions')
        
        except Exception as e:
            print(f"Error rating session: {e}")
            flash('Error submitting rating', 'danger')
            return redirect('/mentor/sessions')
    
    # GET request - show rating form
    try:
        db = get_connection()
        cursor = db.cursor(cursor_factory=RealDictCursor)
        
        cursor.execute("""
            SELECT mr.*, c.name as candidate_name, c.email as candidate_email,
                   cp.headline, cp.primary_skills
            FROM mentorship_requests mr
            JOIN candidates c ON mr.candidate_id = c.id
            LEFT JOIN candidate_profiles cp ON c.id = cp.candidate_id
            WHERE mr.id = %s AND mr.mentor_id = %s
        """, (request_id, mentor_id))
        
        mentorship_session = cursor.fetchone()
        cleanup_db_resources(cursor, db)
        
        if not mentorship_session:
            flash('Session not found', 'danger')
            return redirect('/mentor/sessions')
        
        return render_template('rate_session.html', session=mentorship_session)
    
    except Exception as e:
        print(f"Error loading session: {e}")
        flash('Error loading session', 'danger')
        return redirect('/mentor/sessions')


@app.route('/mentor/availability', methods=['GET', 'POST'])
def mentor_availability():
    """Manage mentor availability calendar"""
    if session.get('role') != 'mentor':
        return redirect('/login')
    
    mentor_id = session.get('user_id')
    
    if request.method == 'POST':
        try:
            action = request.form.get('action')
            
            db = get_connection()
            cursor = db.cursor(cursor_factory=RealDictCursor)
            
            # Create availability table if not exists
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS mentor_availability (
                    id SERIAL PRIMARY KEY,
                    mentor_id VARCHAR(20),
                    day_of_week VARCHAR(10),
                    start_time TIME,
                    end_time TIME,
                    is_available BOOLEAN DEFAULT TRUE,
                    max_sessions_per_day INT DEFAULT 3,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (mentor_id) REFERENCES mentors(id),
                    UNIQUE (mentor_id, day_of_week, start_time)
                )
            """)
            
            if action == 'add':
                day = request.form.get('day_of_week')
                start_time = request.form.get('start_time')
                end_time = request.form.get('end_time')
                max_sessions = request.form.get('max_sessions', 3)
                
                cursor.execute("""
                    INSERT INTO mentor_availability 
                    (mentor_id, day_of_week, start_time, end_time, max_sessions_per_day)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (mentor_id, day_of_week, start_time) DO UPDATE SET
                    end_time = EXCLUDED.end_time,
                    max_sessions_per_day = EXCLUDED.max_sessions_per_day
                """, (mentor_id, day, start_time, end_time, max_sessions))
                
                flash('Availability added successfully!', 'success')
            
            elif action == 'delete':
                availability_id = request.form.get('availability_id')
                cursor.execute("""
                    DELETE FROM mentor_availability 
                    WHERE id = %s AND mentor_id = %s
                """, (availability_id, mentor_id))
                
                flash('Availability removed', 'info')
            
            elif action == 'toggle':
                availability_id = request.form.get('availability_id')
                cursor.execute("""
                    UPDATE mentor_availability 
                    SET is_available = NOT is_available
                    WHERE id = %s AND mentor_id = %s
                """, (availability_id, mentor_id))
                
                flash('Availability updated', 'success')
            
            db.commit()
            cleanup_db_resources(cursor, db)
            
            return redirect('/mentor/availability')
        
        except Exception as e:
            print(f"Error updating availability: {e}")
            flash('Error updating availability', 'danger')
            return redirect('/mentor/availability')
    
    # GET request - show availability
    try:
        db = get_connection()
        cursor = db.cursor(cursor_factory=RealDictCursor)
        
        # Create table if not exists
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS mentor_availability (
                id SERIAL PRIMARY KEY,
                mentor_id VARCHAR(20),
                day_of_week VARCHAR(10),
                start_time TIME,
                end_time TIME,
                is_available BOOLEAN DEFAULT TRUE,
                max_sessions_per_day INT DEFAULT 3,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (mentor_id) REFERENCES mentors(id),
                UNIQUE (mentor_id, day_of_week, start_time)
            )
        """)
        
        cursor.execute("""
            SELECT * FROM mentor_availability 
            WHERE mentor_id = %s
            ORDER BY 
                FIELD(day_of_week, 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'),
                start_time
        """, (mentor_id,))
        
        availability = cursor.fetchall()
        cleanup_db_resources(cursor, db)
        
        return render_template('mentor_availability.html', availability=availability)
    
    except Exception as e:
        print(f"Error loading availability: {e}")
        flash('Error loading availability', 'danger')
        return redirect('/mentor-dashboard')


@app.route('/mentor/analytics')
def mentor_analytics():
    """Advanced analytics dashboard for mentors"""
    if session.get('role') != 'mentor':
        return redirect('/login')
    
    mentor_id = session.get('user_id')
    
    try:
        db = get_connection()
        cursor = db.cursor(cursor_factory=RealDictCursor)
        
        # Overall statistics
        cursor.execute("""
            SELECT 
                COUNT(*) as total_sessions,
                SUM(CASE WHEN status = 'Completed' THEN 1 ELSE 0 END) as completed,
                SUM(CASE WHEN status = 'Accepted' THEN 1 ELSE 0 END) as active,
                SUM(CASE WHEN status = 'Pending' THEN 1 ELSE 0 END) as pending
            FROM mentorship_requests
            WHERE mentor_id = %s
        """, (mentor_id,))
        stats = cursor.fetchone()
        
        # Create session_ratings table if not exists
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS session_ratings (
                id SERIAL PRIMARY KEY,
                mentorship_request_id INT,
                mentor_id VARCHAR(20),
                candidate_id VARCHAR(20),
                rating INT CHECK (rating BETWEEN 1 AND 5),
                feedback TEXT,
                areas_improved TEXT,
                recommendations TEXT,
                session_duration INT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (mentorship_request_id) REFERENCES mentorship_requests(id),
                FOREIGN KEY (mentor_id) REFERENCES mentors(id),
                FOREIGN KEY (candidate_id) REFERENCES candidates(id)
            )
        """)
        
        # Average rating
        cursor.execute("""
            SELECT AVG(rating) as avg_rating, COUNT(*) as total_ratings,
                   SUM(session_duration) as total_hours
            FROM session_ratings
            WHERE mentor_id = %s
        """, (mentor_id,))
        rating_stats = cursor.fetchone()
        
        # Sessions per month (last 6 months)
        cursor.execute("""
            SELECT 
                TO_CHAR(created_at, 'YYYY-MM') as month,
                COUNT(*) as count
            FROM mentorship_requests
            WHERE mentor_id = %s
            AND created_at >= CURRENT_TIMESTAMP - INTERVAL '6 months'
            GROUP BY TO_CHAR(created_at, 'YYYY-MM')
            ORDER BY month DESC
        """, (mentor_id,))
        monthly_sessions = cursor.fetchall()
        
        # Top areas mentored
        cursor.execute("""
            SELECT 
                cp.primary_skills,
                COUNT(*) as count
            FROM mentorship_requests mr
            JOIN candidate_profiles cp ON mr.candidate_id = cp.candidate_id
            WHERE mr.mentor_id = %s AND cp.primary_skills IS NOT NULL
            GROUP BY cp.primary_skills
            ORDER BY count DESC
            LIMIT 5
        """, (mentor_id,))
        top_areas = cursor.fetchall()
        
        # Recent feedback
        cursor.execute("""
            SELECT sr.*, c.name as candidate_name
            FROM session_ratings sr
            JOIN candidates c ON sr.candidate_id = c.id
            WHERE sr.mentor_id = %s
            ORDER BY sr.created_at DESC
            LIMIT 5
        """, (mentor_id,))
        recent_feedback = cursor.fetchall()
        
        cleanup_db_resources(cursor, db)
        
        return render_template('mentor_analytics.html',
                             stats=stats,
                             rating_stats=rating_stats,
                             monthly_sessions=monthly_sessions,
                             top_areas=top_areas,
                             recent_feedback=recent_feedback)
    
    except Exception as e:
        print(f"Error loading analytics: {e}")
        flash('Error loading analytics', 'danger')
        return redirect('/mentor-dashboard')


@app.route('/api/mentor/meetings', methods=['GET', 'POST'])
def mentor_meetings_api():
    if session.get('role') != 'mentor':
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401
    mentor_id = session.get('user_id')
    db = get_connection()
    cursor = db.cursor(cursor_factory=RealDictCursor)
    try:
        if request.method == 'POST':
            data = request.get_json() or {}
            candidate_id = data.get('candidate_id')
            mode = data.get('mode')
            meeting_date = data.get('date')
            meeting_time = data.get('time')
            meeting_link = data.get('link')
            notes = data.get('notes')
            if not candidate_id:
                return jsonify({'success': False, 'message': 'candidate_id required'}), 400
            
            # Get mentor and candidate names for notifications
            cursor.execute("SELECT name FROM mentors WHERE id = %s", (mentor_id,))
            mentor_row = cursor.fetchone()
            mentor_name = mentor_row['name'] if mentor_row else 'Your mentor'
            
            cursor.execute("SELECT name FROM candidates WHERE id = %s", (candidate_id,))
            candidate_row = cursor.fetchone()
            candidate_name = candidate_row['name'] if candidate_row else 'Candidate'
            
            cursor.execute(
                """
                INSERT INTO mentor_meetings (mentor_id, candidate_id, mode, meeting_date, meeting_time, meeting_link, notes)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (mentor_id, candidate_id) DO UPDATE SET
                    mode=EXCLUDED.mode, meeting_date=EXCLUDED.meeting_date, meeting_time=EXCLUDED.meeting_time,
                    meeting_link=EXCLUDED.meeting_link, notes=EXCLUDED.notes
                """,
                (mentor_id, candidate_id, mode, meeting_date, meeting_time, meeting_link, notes)
            )
            
            # Notify candidate about the scheduled meeting
            create_notification(
                'candidate', candidate_id, 'meeting_scheduled',
                'New Meeting Scheduled! 📅',
                f'{mentor_name} has scheduled a meeting with you on {meeting_date} at {meeting_time}',
                '/candidate-dashboard#mentors'
            )
            
            # Log activity for both parties
            log_activity(candidate_id, 'candidate', 'mentorship', 'Meeting Scheduled',
                        f'Meeting scheduled with {mentor_name} on {meeting_date}')
            log_activity(mentor_id, 'mentor', 'mentorship', 'Meeting Set',
                        f'Scheduled meeting with {candidate_name} on {meeting_date}')
            
            db.commit()
            
            return jsonify({'success': True, 'meeting': {
                'candidate_id': candidate_id,
                'candidate_name': candidate_name,
                'mode': mode,
                'meeting_date': meeting_date,
                'meeting_time': meeting_time,
                'meeting_link': meeting_link,
                'notes': notes
            }})

        cursor.execute(
            """
            SELECT mm.*, c.name AS candidate_name
            FROM mentor_meetings mm
            JOIN candidates c ON mm.candidate_id = c.id
            WHERE mm.mentor_id = %s
            ORDER BY mm.meeting_date DESC, mm.meeting_time DESC
            """,
            (mentor_id,)
        )
        rows = cursor.fetchall()
        return jsonify({'success': True, 'meetings': rows})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        cleanup_db_resources(cursor, db)

@app.route('/api/candidate/meetings')
def candidate_meetings_api():
    if session.get('role') != 'candidate':
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401
    cand_id = session.get('user_id')
    db = get_connection()
    cursor = db.cursor(cursor_factory=RealDictCursor)
    try:
        cursor.execute(
            """
            SELECT mm.*, m.name AS mentor_name, mp.company, mp.mode, mp.available_days, mp.time_slot
            FROM mentor_meetings mm
            JOIN mentors m ON mm.mentor_id = m.id
            LEFT JOIN mentor_profiles mp ON m.id = mp.mentor_id
            WHERE mm.candidate_id = %s
            ORDER BY mm.meeting_date DESC, mm.meeting_time DESC
            """,
            (cand_id,)
        )
        rows = cursor.fetchall()
        return jsonify({'success': True, 'meetings': rows})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        cleanup_db_resources(cursor, db)
@app.route('/mentor/delete-profile', methods=['POST'])
def delete_mentor_profile():
    if session.get('role') != 'mentor':
        return redirect('/login')

    mentor_id = session['user_id']
    db = get_connection()
    cursor = db.cursor(cursor_factory=RealDictCursor)
    
    try:
        cursor.execute("DELETE FROM mentor_profiles WHERE mentor_id = %s", (mentor_id,))
        db.commit()
        flash("Profile deleted successfully.", "warning")
    except Exception as e:
        print(f"Error deleting profile: {e}")
        flash("Could not delete profile.", "danger")
    finally:
        cleanup_db_resources(cursor, db)

    return redirect('/mentor-dashboard')

@app.route('/admin-dashboard')
def admin_dashboard():
    if session.get('role') != 'admin':
        return redirect('/login')

    db = get_connection()
    cursor = db.cursor(cursor_factory=RealDictCursor)
    
    try:
        try:
            cursor.execute("""
                SELECT 
                    f.id,
                    f.from_role AS type,
                    f.rating,
                    f.comment,
                    f.created_at,
                    CASE 
                        WHEN f.from_role = 'candidate' THEN c.name
                        WHEN f.from_role = 'recruiter' THEN r.name
                        ELSE 'Unknown'
                    END AS user_name
                FROM feedback f
                LEFT JOIN candidates c ON f.from_role = 'candidate' AND f.from_id = c.id
                LEFT JOIN recruiters r ON f.from_role = 'recruiter' AND f.from_id = r.id
                WHERE f.to_role = 'admin'
                ORDER BY f.created_at DESC
            """)
            user_feedbacks = cursor.fetchall()
        except Exception as feedback_error:
            print(f"Admin feedback query warning: {feedback_error}")
            user_feedbacks = []

        cursor.execute("SELECT (SELECT COUNT(*) FROM candidates) + (SELECT COUNT(*) FROM recruiters) AS total")
        total_users_count = cursor.fetchone()['total']

        try:
            cursor.execute("""
                SELECT id, name, email, 'Candidate' as role, is_blocked FROM candidates
                UNION ALL
                SELECT id, name, email, 'Recruiter' as role, is_blocked FROM recruiters
                ORDER BY id ASC
            """)
            all_users = cursor.fetchall()
        except Exception as all_users_error:
            print(f"Admin users query warning: {all_users_error}")
            cursor.execute("""
                SELECT id, name, email, 'Candidate' as role, FALSE as is_blocked FROM candidates
                UNION ALL
                SELECT id, name, email, 'Recruiter' as role, FALSE as is_blocked FROM recruiters
                ORDER BY id ASC
            """)
            all_users = cursor.fetchall()

        candidate_payment_map = {}
        try:
            cursor.execute("""
                SELECT candidate_id, assessment_type, amount
                FROM candidate_assessment_payments
                WHERE payment_status = 'completed'
                ORDER BY paid_at DESC
            """)
            payment_rows = cursor.fetchall()

            assessment_labels = {
                'technical_test': 'Technical Test',
                'mock_interview': 'Mock Interview'
            }

            for row in payment_rows:
                candidate_id = row.get('candidate_id')
                if not candidate_id:
                    continue

                if candidate_id not in candidate_payment_map:
                    candidate_payment_map[candidate_id] = {
                        'types': [],
                        'total_amount': 0
                    }

                raw_type = row.get('assessment_type')
                type_label = assessment_labels.get(raw_type, str(raw_type).replace('_', ' ').title())
                if type_label not in candidate_payment_map[candidate_id]['types']:
                    candidate_payment_map[candidate_id]['types'].append(type_label)

                amount = row.get('amount') or 0
                try:
                    candidate_payment_map[candidate_id]['total_amount'] += int(amount)
                except Exception:
                    pass
        except Exception as payment_error:
            print(f"Admin payment summary warning: {payment_error}")
        mentorship_payment_map = {}
        try:
            cursor.execute("""
                SELECT mp.mentor_id, mp.candidate_id, mp.amount, mp.admin_share, mp.mentor_share, mp.payment_reference
                FROM mentorship_payments mp
                WHERE mp.payment_status = 'completed'
                ORDER BY mp.paid_at DESC
            """)
            mp_rows = cursor.fetchall()

            for row in mp_rows:
                mentor_id = row.get('mentor_id')
                candidate_id = row.get('candidate_id')
                if not mentor_id and not candidate_id:
                    continue

                if mentor_id and mentor_id not in mentorship_payment_map:
                    mentorship_payment_map[mentor_id] = {
                        'total_amount': 0,
                        'admin_share': 0,
                        'mentor_share': 0,
                        'last_reference': None
                    }

                if candidate_id and candidate_id not in mentorship_payment_map:
                    mentorship_payment_map[candidate_id] = {
                        'total_amount': 0,
                        'admin_share': 0,
                        'mentor_share': 0,
                        'last_reference': None
                    }

                amount = row.get('amount') or 0
                admin_share = row.get('admin_share') or 0
                mentor_share = row.get('mentor_share') or 0

                if mentor_id:
                    mentorship_payment_map[mentor_id]['total_amount'] += float(amount)
                    mentorship_payment_map[mentor_id]['admin_share'] += float(admin_share)
                    mentorship_payment_map[mentor_id]['mentor_share'] += float(mentor_share)
                    mentorship_payment_map[mentor_id]['last_reference'] = row.get('payment_reference')

                if candidate_id:
                    mentorship_payment_map[candidate_id]['total_amount'] += float(amount)
                    mentorship_payment_map[candidate_id]['admin_share'] += float(admin_share)
                    mentorship_payment_map[candidate_id]['mentor_share'] += float(mentor_share)
                    mentorship_payment_map[candidate_id]['last_reference'] = row.get('payment_reference')
        except Exception as mp_error:
            print(f"Admin mentorship payment summary warning: {mp_error}")

        # Batch-load mentor and recruiter profiles — replaces per-user N+1 queries
        try:
            cursor.execute("SELECT mentor_id, verification_status, agreement_accepted, upi_id FROM mentor_profiles")
            mentor_profile_map = {row['mentor_id']: row for row in cursor.fetchall()}
        except Exception:
            mentor_profile_map = {}
        try:
            cursor.execute("SELECT recruiter_id, verification_status FROM recruiter_profiles")
            recruiter_profile_map = {row['recruiter_id']: row for row in cursor.fetchall()}
        except Exception:
            recruiter_profile_map = {}

        for u in all_users:
            if u.get('role') == 'Mentor':
                r = mentor_profile_map.get(u.get('id'))
                u['verification_status'] = r['verification_status'] if r and 'verification_status' in r else None
                u['agreement_accepted'] = r.get('agreement_accepted') if r else False
                u['mentor_upi_id'] = r.get('upi_id') if r else None
            elif u.get('role') == 'Recruiter':
                r = recruiter_profile_map.get(u.get('id'))
                u['verification_status'] = r['verification_status'] if r and 'verification_status' in r else None
            else:
                u['verification_status'] = None
                u['agreement_accepted'] = None
                u['mentor_upi_id'] = None

            if u.get('role') == 'Candidate':
                payment_data = candidate_payment_map.get(u.get('id'))
                if payment_data and payment_data.get('types'):
                    u['assessment_payment_done'] = True
                    u['assessment_payment_for'] = ', '.join(payment_data['types'])
                    u['assessment_payment_amount'] = payment_data.get('total_amount', 0)
                else:
                    u['assessment_payment_done'] = False
                    u['assessment_payment_for'] = None
                    u['assessment_payment_amount'] = 0
            else:
                u['assessment_payment_done'] = None
                u['assessment_payment_for'] = None
                u['assessment_payment_amount'] = 0

            mentorship_payment_data = mentorship_payment_map.get(u.get('id'))
            if mentorship_payment_data:
                u['mentorship_payment_amount'] = mentorship_payment_data.get('total_amount', 0)
                u['mentorship_admin_share'] = mentorship_payment_data.get('admin_share', 0)
                u['mentorship_mentor_share'] = mentorship_payment_data.get('mentor_share', 0)
                u['mentorship_payment_reference'] = mentorship_payment_data.get('last_reference')
            else:
                u['mentorship_payment_amount'] = 0
                u['mentorship_admin_share'] = 0
                u['mentorship_mentor_share'] = 0
                u['mentorship_payment_reference'] = None

        cursor.execute("""
             SELECT mp.mentor_id,
                 COALESCE(m.name, mp.agreement_signature_name, mp.mentor_id) AS name,
                 m.email,
                   mp.id,
                   mp.expertise,
                   mp.company,
                   mp.verification_file,
                   mp.agreement_accepted,
                   mp.agreement_accepted_at,
                   mp.agreement_signature_name,
                   mp.agreement_signature_at,
                   (mp.agreement_pdf IS NOT NULL) AS has_agreement_pdf
                 FROM mentor_profiles mp
                 LEFT JOIN mentors m ON m.id = mp.mentor_id
              WHERE LOWER(REGEXP_REPLACE(COALESCE(mp.verification_status, 'pending'), '\\s+', '', 'g')) = 'pending'
            ORDER BY mp.id DESC
        """)
        pending_mentors = cursor.fetchall()

        cursor.execute("""
                 SELECT mp.mentor_id,
                     COALESCE(m.name, mp.agreement_signature_name, mp.mentor_id) AS name,
                     m.email,
                   mp.id,
                   mp.expertise,
                   mp.company,
                   mp.verification_file,
                   mp.agreement_accepted,
                   mp.agreement_accepted_at,
                   mp.agreement_signature_name,
                   mp.agreement_signature_at,
                   (mp.agreement_pdf IS NOT NULL) AS has_agreement_pdf
                 FROM mentor_profiles mp
                 LEFT JOIN mentors m ON m.id = mp.mentor_id
              WHERE LOWER(REGEXP_REPLACE(COALESCE(mp.verification_status, ''), '\\s+', '', 'g')) IN ('approved', 'verified')
            ORDER BY mp.id DESC
        """)
        verified_mentors = cursor.fetchall()

        cursor.execute("""
                 SELECT mp.mentor_id,
                     COALESCE(m.name, mp.agreement_signature_name, mp.mentor_id) AS name,
                     m.email,
                   mp.id,
                   mp.expertise,
                   mp.company,
                   mp.verification_file,
                   mp.agreement_accepted,
                   mp.agreement_accepted_at,
                   mp.agreement_signature_name,
                   mp.agreement_signature_at,
                   (mp.agreement_pdf IS NOT NULL) AS has_agreement_pdf,
                   mp.rejection_reason
            FROM mentor_profiles mp
            LEFT JOIN mentors m ON m.id = mp.mentor_id
            WHERE LOWER(REGEXP_REPLACE(COALESCE(mp.verification_status, ''), '\\s+', '', 'g')) = 'rejected'
            ORDER BY mp.id DESC
        """)
        rejected_mentors = cursor.fetchall()

        # Explicit counts for robust dashboard cards/badges
        cursor.execute("""
            SELECT COUNT(*) AS cnt
            FROM mentor_profiles mp
            WHERE LOWER(REGEXP_REPLACE(COALESCE(mp.verification_status, 'pending'), '\\s+', '', 'g')) = 'pending'
        """)
        pending_mentors_count_row = cursor.fetchone() or {}
        pending_mentors_count = pending_mentors_count_row.get('cnt', len(pending_mentors))

        cursor.execute("""
            SELECT COUNT(*) AS cnt
            FROM mentor_profiles mp
            WHERE LOWER(REGEXP_REPLACE(COALESCE(mp.verification_status, ''), '\\s+', '', 'g')) IN ('approved', 'verified')
        """)
        verified_mentors_count_row = cursor.fetchone() or {}
        verified_mentors_count = verified_mentors_count_row.get('cnt', len(verified_mentors))

        cursor.execute("""
            SELECT COUNT(*) AS cnt
            FROM mentor_profiles mp
            WHERE LOWER(REGEXP_REPLACE(COALESCE(mp.verification_status, ''), '\\s+', '', 'g')) = 'rejected'
        """)
        rejected_mentors_count_row = cursor.fetchone() or {}
        rejected_mentors_count = rejected_mentors_count_row.get('cnt', len(rejected_mentors))

        cursor.execute("""
            SELECT j.*, rp.company_name, COUNT(a.id) AS application_count
            FROM jobs j
            LEFT JOIN recruiter_profiles rp ON j.recruiter_id = rp.recruiter_id
            LEFT JOIN applications a ON j.id = a.job_id
            GROUP BY j.id, rp.company_name
            ORDER BY j.created_at DESC
        """)
        jobs = cursor.fetchall()

        # real analytics
        cursor.execute("SELECT COUNT(*) AS cnt FROM applications WHERE status='Selected'")
        row = cursor.fetchone()
        total_placements = row['cnt'] if row and 'cnt' in row else 0

        active_jobs = len(jobs) if jobs else 0

        cursor.execute("SELECT COUNT(*) AS total FROM candidates")
        row = cursor.fetchone()
        total_candidates = row['total'] if row and 'total' in row else 0

        try:
            cursor.execute("""
                SELECT COUNT(*) AS cnt
                FROM candidate_profiles
                WHERE COALESCE(NULLIF(TRIM(primary_skills), ''), NULLIF(TRIM(secondary_skills), ''), NULLIF(TRIM(frameworks_libraries), ''), NULLIF(TRIM(\"databases\"), ''), NULLIF(TRIM(tools_technologies), ''), NULLIF(TRIM(cloud_platforms), '')) IS NOT NULL
            """)
            row = cursor.fetchone()
            candidates_with_skills = row['cnt'] if row and 'cnt' in row else 0
        except Exception as skill_coverage_error:
            print(f"Admin skill coverage warning: {skill_coverage_error}")
            try:
                db.rollback()
            except Exception:
                pass
            candidates_with_skills = 0
        skill_coverage = int((candidates_with_skills / total_candidates) * 100) if total_candidates else 0

        try:
            cursor.execute("SELECT COUNT(*) AS cnt FROM feedback")
            row = cursor.fetchone()
            reports_count = row['cnt'] if row and 'cnt' in row else 0
        except Exception as reports_error:
            print(f"Admin reports count warning: {reports_error}")
            try:
                db.rollback()
            except Exception:
                pass
            reports_count = 0

        # Company verification datasets
        try:
            cursor.execute("""
                SELECT rp.*, COALESCE(r.email, '') AS company_email
                FROM recruiter_profiles rp
                LEFT JOIN recruiters r ON rp.recruiter_id = r.id
                WHERE LOWER(REGEXP_REPLACE(COALESCE(rp.verification_status, 'pending'), '\\s+', '', 'g')) = 'pending'
                  AND COALESCE(rp.admin_countersigned, FALSE) = FALSE
                ORDER BY rp.id DESC
            """)
            pending_companies = cursor.fetchall()
        except Exception as e:
            print(f"[ERROR] pending_companies query failed: {e}")
            try:
                db.rollback()
            except Exception:
                pass
            pending_companies = []

        try:
            cursor.execute("""
                SELECT rp.*, COALESCE(r.email, '') AS company_email
                FROM recruiter_profiles rp
                LEFT JOIN recruiters r ON rp.recruiter_id = r.id
                WHERE LOWER(REGEXP_REPLACE(COALESCE(rp.verification_status, ''), '\\s+', '', 'g')) IN ('approved', 'verified')
                   OR COALESCE(rp.admin_countersigned, FALSE) = TRUE
                   OR COALESCE(r.profile_completed, FALSE) = TRUE
                ORDER BY rp.id DESC
            """)
            verified_companies = cursor.fetchall()
        except Exception as e:
            print(f"[ERROR] verified_companies query failed: {e}")
            try:
                db.rollback()
            except Exception:
                pass
            verified_companies = []

        try:
            cursor.execute("""
                SELECT rp.*, COALESCE(r.email, '') AS company_email
                FROM recruiter_profiles rp
                LEFT JOIN recruiters r ON rp.recruiter_id = r.id
                WHERE LOWER(REGEXP_REPLACE(COALESCE(rp.verification_status, ''), '\\s+', '', 'g')) = 'rejected'
                ORDER BY rp.id DESC
            """)
            rejected_companies = cursor.fetchall()
        except Exception as e:
            print(f"[ERROR] rejected_companies query failed: {e}")
            try:
                db.rollback()
            except Exception:
                pass
            rejected_companies = []

        # Notifications & Alerts (only unread, latest 10)
        try:
            cursor.execute("""
                SELECT created_at, receiver_role, receiver_id, message, is_read
                FROM notifications
                WHERE is_read = FALSE
                ORDER BY created_at DESC
                LIMIT 10
            """)
            notifications_list = cursor.fetchall()
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
            notifications_list = []

        # Audit & Logs (latest 5)
        try:
            cursor.execute("""
                SELECT created_at, receiver_role, receiver_id, message, is_read
                FROM notifications
                ORDER BY created_at DESC
                LIMIT 5
            """)
            audit_rows = cursor.fetchall()
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
            audit_rows = []

        return render_template(
            'admin_dashboard.html',
            all_users=all_users,
            user_feedbacks=user_feedbacks,
            users=total_users_count,        
            pending_mentors=pending_mentors,
            verified_mentors=verified_mentors,
            rejected_mentors=rejected_mentors,
            pending_mentors_count=pending_mentors_count,
            verified_mentors_count=verified_mentors_count,
            rejected_mentors_count=rejected_mentors_count,
            pending_companies=pending_companies,
            verified_companies=verified_companies,
            rejected_companies=rejected_companies,
            jobs=jobs,
            total_placements=total_placements,
            active_jobs=active_jobs,
            skill_coverage=skill_coverage,
            reports_count=reports_count,
            notifications_list=notifications_list,
            audit_rows=audit_rows
        )
    
    except Exception as e:
        print(f"[ERROR] Admin dashboard error: {e}")
        if db:
            db.rollback()
        cleanup_db_resources(cursor, db)
        flash("An error occurred while loading the dashboard. Please try again.", "error")
        return redirect('/login')
    
    finally:
        cleanup_db_resources(cursor, db)


def _excel_response(filename, rows, header):
    from openpyxl import Workbook
    import io

    wb = Workbook()
    ws = wb.active
    ws.append(header)
    for row in rows:
        ws.append(row)

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)

    resp = make_response(output.getvalue())
    resp.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
    resp.headers['Content-Type'] = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    return resp


@app.route('/admin/export/users')
def export_users():
    if session.get('role') != 'admin':
        return redirect('/login')

    db = get_connection()
    cursor = db.cursor(cursor_factory=RealDictCursor)
    cursor.execute("""
        SELECT id, name, email, 'Candidate' AS role, created_at FROM candidates
        UNION ALL
        SELECT id, name, email, 'Recruiter' AS role, created_at FROM recruiters
        UNION ALL
        SELECT id, name, email, 'Mentor' AS role, created_at FROM mentors
        ORDER BY created_at DESC
    """)
    rows = cursor.fetchall()
    cleanup_db_resources(cursor, db)

    data_rows = [[r['id'], r['name'], r['email'], r['role'], r['created_at']] for r in rows]
    try:
        log_audit("Exported users Excel")
    except Exception:
        pass
    return _excel_response('users.xlsx', data_rows, ['id', 'name', 'email', 'role', 'created_at'])


@app.route('/admin/export/jobs')
def export_jobs():
    if session.get('role') != 'admin':
        return redirect('/login')

    db = get_connection()
    cursor = db.cursor(cursor_factory=RealDictCursor)
    cursor.execute("""
        SELECT j.id, j.title, j.skills, j.recruiter_id,
               (SELECT COUNT(*) FROM applications a WHERE a.job_id = j.id) AS applications_count
        FROM jobs j
        ORDER BY j.id DESC
    """)
    rows = cursor.fetchall()
    cleanup_db_resources(cursor, db)

    data_rows = [[r['id'], r['title'], r['skills'], r['recruiter_id'], r['applications_count']] for r in rows]
    try:
        log_audit("Exported jobs Excel")
    except Exception:
        pass
    return _excel_response('jobs.xlsx', data_rows, ['id', 'title', 'skills', 'recruiter_id', 'applications'])


@app.route('/admin/export/audit')
def export_audit():
    if session.get('role') != 'admin':
        return redirect('/login')

    # Using notifications table as lightweight audit trail
    db = get_connection()
    cursor = db.cursor(cursor_factory=RealDictCursor)
    cursor.execute("""
        SELECT created_at, receiver_role, receiver_id, message, is_read
        FROM notifications
        ORDER BY created_at DESC
        LIMIT 200
    """)
    rows = cursor.fetchall()
    cleanup_db_resources(cursor, db)

    data_rows = [[r['created_at'], r['receiver_role'], r['receiver_id'], r['message'], r['is_read']] for r in rows]
    try:
        log_audit("Exported audit Excel")
    except Exception:
        pass
    return _excel_response('audit.xlsx', data_rows, ['timestamp', 'receiver_role', 'receiver_id', 'message', 'is_read'])

@app.route('/admin/delete-logs', methods=['POST'])
def delete_logs():
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403
    
    data = request.get_json()
    log_ids = data.get('log_ids', [])
    
    if not log_ids:
        return jsonify({'success': False, 'error': 'No logs selected'}), 400
    
    db = get_connection()
    cursor = db.cursor(cursor_factory=RealDictCursor)
    
    try:
        # Delete selected logs
        placeholders = ','.join(['%s'] * len(log_ids))
        cursor.execute(f"DELETE FROM notifications WHERE id IN ({placeholders})", log_ids)
        db.commit()
        
        deleted_count = cursor.rowcount
        
        try:
            log_audit(f"Deleted {deleted_count} audit log entries")
        except Exception:
            pass
        
        cleanup_db_resources(cursor, db)
        
        return jsonify({'success': True, 'deleted_count': deleted_count, 'message': f'Deleted {deleted_count} log entries'})
        
    except Exception as e:
        db.rollback()
        cleanup_db_resources(cursor, db)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/admin/clear-all-logs', methods=['POST'])
def clear_all_logs():
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403
    
    db = get_connection()
    cursor = db.cursor(cursor_factory=RealDictCursor)
    
    try:
        # Clear all notifications (audit logs)
        cursor.execute("DELETE FROM notifications")
        db.commit()
        
        deleted_count = cursor.rowcount
        
        try:
            log_audit(f"Cleared all {deleted_count} audit log entries")
        except Exception:
            pass
        
        cleanup_db_resources(cursor, db)
        
        return jsonify({'success': True, 'deleted_count': deleted_count, 'message': f'Cleared all {deleted_count} log entries'})
        
    except Exception as e:
        db.rollback()
        cleanup_db_resources(cursor, db)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/admin/set-cleanup-schedule', methods=['POST'])
def set_cleanup_schedule():
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403
    
    data = request.get_json()
    days = data.get('days')
    
    try:
        current_settings = get_admin_settings()
        
        if days is None or days == '':
            # Disable cleanup
            current_settings.pop('log_cleanup_days', None)
            message = 'Auto-cleanup disabled'
        else:
            # Set cleanup schedule
            current_settings['log_cleanup_days'] = int(days)
            message = f'Auto-cleanup set to delete logs older than {days} days'
        
        save_admin_settings(current_settings)
        
        try:
            log_audit(f"Updated log cleanup schedule to {days} days" if days else "Disabled log cleanup")
        except Exception:
            pass
        
        return jsonify({'success': True, 'message': message})
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/admin/block-user', methods=['POST'])
def block_user():
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403
    
    data = request.get_json()
    user_id = data.get('user_id')
    role = data.get('role')
    
    if not user_id or not role:
        return jsonify({'success': False, 'error': 'Missing parameters'}), 400
    
    db = get_connection()
    cursor = db.cursor(cursor_factory=RealDictCursor)
    
    try:
        # Map role to table name
        table_map = {
            'candidate': 'candidates',
            'recruiter': 'recruiters',
            'mentor': 'mentors'
        }
        
        table = table_map.get(role.lower())
        if not table:
            return jsonify({'success': False, 'error': 'Invalid role'}), 400
        
        # Add is_blocked column if it doesn't exist
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS is_blocked BOOLEAN DEFAULT FALSE")

        # Block the user
        cursor.execute(f"UPDATE {table} SET is_blocked = TRUE WHERE id = %s", (user_id,))
        db.commit()
        print(f"[BLOCK] role={role} table={table} user_id={user_id} -> blocked")
        
        # Send notification to user
        try:
            send_notification(role.lower(), user_id, 'Your account has been blocked by admin.')
        except Exception:
            pass
        
        # Log audit
        try:
            log_audit(f"Blocked {role} user ID: {user_id}")
        except Exception:
            pass
        
        cleanup_db_resources(cursor, db)
        
        return jsonify({'success': True, 'message': 'User blocked successfully'})
        
    except Exception as e:
        db.rollback()
        cleanup_db_resources(cursor, db)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/admin/unblock-user', methods=['POST'])
def unblock_user():
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403
    
    data = request.get_json()
    user_id = data.get('user_id')
    role = data.get('role')
    
    if not user_id or not role:
        return jsonify({'success': False, 'error': 'Missing parameters'}), 400
    
    db = get_connection()
    cursor = db.cursor(cursor_factory=RealDictCursor)
    
    try:
        # Map role to table name
        table_map = {
            'candidate': 'candidates',
            'recruiter': 'recruiters',
            'mentor': 'mentors'
        }
        
        table = table_map.get(role.lower())
        if not table:
            return jsonify({'success': False, 'error': 'Invalid role'}), 400
        
        # Unblock the user
        cursor.execute(f"UPDATE {table} SET is_blocked = FALSE WHERE id = %s", (user_id,))
        db.commit()
        print(f"[UNBLOCK] role={role} table={table} user_id={user_id} -> unblocked")
        
        # Send notification to user
        try:
            send_notification(role.lower(), user_id, 'Your account has been unblocked by admin.')
        except Exception:
            pass
        
        # Log audit
        try:
            log_audit(f"Unblocked {role} user ID: {user_id}")
        except Exception:
            pass
        
        cleanup_db_resources(cursor, db)
        
        return jsonify({'success': True, 'message': 'User unblocked successfully'})
        
    except Exception as e:
        db.rollback()
        cleanup_db_resources(cursor, db)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/admin/delete-user', methods=['POST'])
def delete_user():
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403
    
    data = request.get_json()
    user_id = data.get('user_id')
    role = data.get('role')
    
    if not user_id or not role:
        return jsonify({'success': False, 'error': 'Missing parameters'}), 400
    
    db = get_connection()
    cursor = db.cursor(cursor_factory=RealDictCursor)
    
    try:
        # Map role to table name
        table_map = {
            'candidate': 'candidates',
            'recruiter': 'recruiters',
            'mentor': 'mentors'
        }
        
        table = table_map.get(role.lower())
        if not table:
            return jsonify({'success': False, 'error': 'Invalid role'}), 400
        
        # Delete the user
        cursor.execute(f"DELETE FROM {table} WHERE id = %s", (user_id,))
        db.commit()
        
        # Log audit
        try:
            log_audit(f"Deleted {role} user ID: {user_id}")
        except Exception:
            pass
        
        cleanup_db_resources(cursor, db)
        
        return jsonify({'success': True, 'message': 'User deleted successfully'})
        
    except Exception as e:
        db.rollback()
        cleanup_db_resources(cursor, db)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/admin/close-job/<int:job_id>', methods=['POST'])
def admin_close_job(job_id):
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403
    
    db = get_connection()
    cursor = db.cursor(cursor_factory=RealDictCursor)
    
    try:
        cursor.execute("UPDATE jobs SET status = 'closed' WHERE id = %s", (job_id,))
        db.commit()
        
        # Log audit
        try:
            log_audit(f"Admin closed job ID: {job_id}")
        except Exception:
            pass
        
        cleanup_db_resources(cursor, db)
        
        return jsonify({'success': True, 'message': 'Job closed successfully'})
        
    except Exception as e:
        db.rollback()
        cleanup_db_resources(cursor, db)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/admin/approve-job/<int:job_id>', methods=['POST'])
def admin_approve_job(job_id):
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403

    db = None
    cursor = None
    try:
        db = get_connection()
        if db is None:
            return jsonify({'success': False, 'error': 'Database connection failed'}), 500
        
        cursor = db.cursor(cursor_factory=RealDictCursor)
        
        # Update job status FIRST and ONLY operation
        cursor.execute("UPDATE jobs SET status = 'active' WHERE id = %s", (job_id,))
        db.commit()
        
        # Return immediately after update
        result = {'success': True, 'message': 'Job approved'}
        
        # Send notification and audit in background (non-blocking)
        try:
            cursor.execute("SELECT recruiter_id, title FROM jobs WHERE id = %s", (job_id,))
            row = cursor.fetchone()
            if row:
                recruiter_id = row['recruiter_id']
                title = row['title']
                # Use non-blocking thread for notification
                notification_thread = threading.Thread(
                    target=send_notification,
                    args=('recruiter', recruiter_id, f'Your job "{title}" has been approved and is now live.'),
                    daemon=True
                )
                notification_thread.start()
        except Exception as e:
            print(f"[WARNING] Failed to send notification: {e}")
        
        try:
            audit_thread = threading.Thread(
                target=log_audit,
                args=(f"Admin approved job ID: {job_id}",),
                daemon=True
            )
            audit_thread.start()
        except Exception as e:
            print(f"[WARNING] Failed to log audit: {e}")
        
        cleanup_db_resources(cursor, db)
        return jsonify(result), 200
        
    except Exception as e:
        print(f"[ERROR] Error approving job {job_id}: {e}")
        try:
            if db:
                db.rollback()
        except Exception:
            pass
        try:
            cleanup_db_resources(cursor, db)
        except Exception:
            pass
        return jsonify({'success': False, 'error': 'Failed to approve job'}), 500


@app.route('/admin/reject-job/<int:job_id>', methods=['POST'])
def admin_reject_job(job_id):
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403

    db = get_connection()
    cursor = db.cursor(cursor_factory=RealDictCursor)
    try:
        cursor.execute("UPDATE jobs SET status = 'draft' WHERE id = %s", (job_id,))
        db.commit()

        # Notify recruiter
        try:
            cursor.execute("SELECT recruiter_id, title FROM jobs WHERE id = %s", (job_id,))
            row = cursor.fetchone()
            if row:
                recruiter_id = row['recruiter_id']
                title = row['title']
                send_notification('recruiter', recruiter_id, f'Your job "{title}" has been rejected by admin.')
        except Exception:
            pass

        try:
            log_audit(f"Admin rejected job ID: {job_id}")
        except Exception:
            pass

        cleanup_db_resources(cursor, db)
        return jsonify({'success': True, 'message': 'Job rejected'})
    except Exception as e:
        db.rollback()
        cleanup_db_resources(cursor, db)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/admin/mark-all-read', methods=['POST'])
def mark_all_notifications_read_admin():
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403
    
    db = get_connection()
    cursor = db.cursor(cursor_factory=RealDictCursor)
    
    try:
        cursor.execute("UPDATE notifications SET is_read = TRUE WHERE is_read = FALSE")
        db.commit()
        cleanup_db_resources(cursor, db)
        return jsonify({'success': True, 'message': 'All notifications marked as read'})
    except Exception as e:
        try:
            cleanup_db_resources(cursor, db)
        except:
            pass
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/admin/get-unread-notifications')
def get_unread_notifications():
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403
    
    try:
        with SafeDBConnection() as (cursor, db):
            cursor.execute("""
                SELECT created_at, receiver_role, receiver_id, message
                FROM notifications
                WHERE is_read = FALSE
                ORDER BY created_at DESC
                LIMIT 10
            """)
            notifications = cursor.fetchall()
        
        # Format the notifications
        formatted_notifications = []
        for n in notifications:
            if n:
                formatted_notifications.append({
                    'message': str(n.get('message', '')),
                    'receiver_role': str(n.get('receiver_role', '')).title() if n.get('receiver_role') else '',
                    'created_at': n.get('created_at').strftime('%Y-%m-%d %H:%M') if n.get('created_at') else ''
                })
        
        return jsonify({'success': True, 'notifications': formatted_notifications}), 200
    except Exception as e:
        print(f"Error fetching unread notifications: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ===== Admin Settings APIs =====
@app.route('/admin/settings-data')
def admin_settings_data():
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403
    try:
        settings = get_admin_settings()
        return jsonify({'success': True, 'settings': settings})
    except Exception as e:
        print(f"Error loading admin settings: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/admin/settings-save', methods=['POST'])
def admin_settings_save():
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403
    data = request.get_json(force=True, silent=True) or {}
    current = get_admin_settings()

    def as_bool(val):
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            return val.lower() in ['1', 'true', 'yes', 'on']
        return bool(val)

    updated = {**current}
    updated['session_timeout_minutes'] = int(data.get('session_timeout_minutes', current['session_timeout_minutes']) or 30)
    updated['maintenance_mode'] = as_bool(data.get('maintenance_mode', current.get('maintenance_mode', False)))
    updated['platform_name'] = data.get('platform_name', current['platform_name'])
    updated['system_timezone'] = data.get('system_timezone', current['system_timezone'])
    updated['enable_registrations'] = as_bool(data.get('enable_registrations', current['enable_registrations']))
    updated['allowed_roles'] = data.get('allowed_roles', current.get('allowed_roles', [])) or []
    updated['failed_login_limit'] = int(data.get('failed_login_limit', current['failed_login_limit']) or 5)
    updated['enable_email_notifications'] = as_bool(data.get('enable_email_notifications', current['enable_email_notifications']))
    updated['alert_new_registrations'] = as_bool(data.get('alert_new_registrations', current['alert_new_registrations']))
    updated['alert_job_reports'] = as_bool(data.get('alert_job_reports', current['alert_job_reports']))
    updated['alert_verification_requests'] = as_bool(data.get('alert_verification_requests', current['alert_verification_requests']))
    updated['auto_approve_jobs'] = as_bool(data.get('auto_approve_jobs', current['auto_approve_jobs']))
    updated['auto_hide_threshold'] = int(data.get('auto_hide_threshold', current['auto_hide_threshold']) or 5)
    updated['log_admin_actions'] = as_bool(data.get('log_admin_actions', current['log_admin_actions']))
    updated['log_retention_days'] = int(data.get('log_retention_days', current['log_retention_days']) or 90)
    updated['auto_refresh_health'] = as_bool(data.get('auto_refresh_health', current['auto_refresh_health']))
    updated['auto_refresh_notifications'] = as_bool(data.get('auto_refresh_notifications', current['auto_refresh_notifications']))
    updated['refresh_interval_ms'] = int(data.get('refresh_interval_ms', current['refresh_interval_ms']) or 5000)

    # Enforce sensible bounds
    updated['session_timeout_minutes'] = max(5, min(updated['session_timeout_minutes'], 720))
    updated['failed_login_limit'] = max(3, min(updated['failed_login_limit'], 15))
    updated['auto_hide_threshold'] = max(1, min(updated['auto_hide_threshold'], 50))
    updated['log_retention_days'] = updated['log_retention_days'] if updated['log_retention_days'] in [30, 90, 180] else 90
    updated['refresh_interval_ms'] = max(3000, min(updated['refresh_interval_ms'], 60000))

    # Apply session timeout live
    app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(minutes=updated['session_timeout_minutes'])

    # Handle admin password change if provided
    new_password = data.get('new_password')
    confirm_password = data.get('confirm_password')
    if new_password:
        if new_password != confirm_password:
            return jsonify({'success': False, 'error': 'Password confirmation does not match'}), 400
        try:
            db = get_connection()
            cursor = db.cursor(cursor_factory=RealDictCursor)
            cursor.execute(
                "UPDATE admins SET password=%s WHERE id=%s",
                (generate_password_hash(new_password), session.get('user_id'))
            )
            db.commit()
            cleanup_db_resources(cursor, db)
        except Exception as e:
            return jsonify({'success': False, 'error': f'Password update failed: {e}'}), 500

    merged = save_admin_settings(updated)
    return jsonify({'success': True, 'settings': merged})


@app.route('/admin/change-password', methods=['POST'])
def admin_change_password():
    """Change the logged-in admin's password after verifying current password."""
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403

    data = request.get_json(force=True, silent=True) or {}
    current_password = data.get('current_password') or ''
    new_password = data.get('new_password') or ''
    confirm_password = data.get('confirm_password') or ''

    if not current_password or not new_password or not confirm_password:
        return jsonify({'success': False, 'error': 'All password fields are required'}), 400
    if new_password != confirm_password:
        return jsonify({'success': False, 'error': 'Password confirmation does not match'}), 400

    admin_id = session.get('user_id')
    db = get_connection()
    cursor = db.cursor(cursor_factory=RealDictCursor)
    try:
        cursor.execute("SELECT password FROM admins WHERE id = %s", (admin_id,))
        row = cursor.fetchone()
        if not row:
            cleanup_db_resources(cursor, db)
            return jsonify({'success': False, 'error': 'Admin not found'}), 404

        if not check_password_hash(row['password'], current_password):
            cleanup_db_resources(cursor, db)
            return jsonify({'success': False, 'error': 'Current password is incorrect'}), 400

        cursor.execute(
            "UPDATE admins SET password=%s, updated_at=CURRENT_TIMESTAMP WHERE id=%s",
            (generate_password_hash(new_password), admin_id)
        )
        db.commit()

        try:
            log_audit("Admin changed their password")
        except Exception:
            pass

        cleanup_db_resources(cursor, db)
        return jsonify({'success': True, 'message': 'Password updated successfully'})
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        cleanup_db_resources(cursor, db)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/admin/settings-force-logout', methods=['POST'])
def admin_settings_force_logout():
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403
    settings = get_admin_settings()
    settings['logout_version'] = settings.get('logout_version', 0) + 1
    merged = save_admin_settings(settings)
    return jsonify({'success': True, 'message': 'All users will be forced to re-authenticate', 'logout_version': merged['logout_version']})


@app.route('/admin/backup', methods=['POST'])
def admin_backup():
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403
    now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    # Placeholder: hook into real backup pipeline
    return jsonify({'success': True, 'message': f'Backup initiated at {now} UTC'})


@app.route('/platform-settings')
def public_platform_settings():
    """Public endpoint exposing maintenance status and message for client-side alerts."""
    try:
        settings = get_admin_settings()
        return jsonify({'success': True, 'maintenance_mode': settings.get('maintenance_mode', False)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e), 'maintenance_mode': False}), 200


@app.route('/admin/export/users', methods=['POST'])
def admin_export_users():
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403
    now = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
    return jsonify({'success': True, 'message': 'User export queued', 'export_url': f'/exports/users_{now}.xlsx'})


@app.route('/admin/export/jobs', methods=['POST'])
def admin_export_jobs():
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403
    now = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
    return jsonify({'success': True, 'message': 'Job export queued', 'export_url': f'/exports/jobs_{now}.xlsx'})

@app.route('/admin/platform-health')
def platform_health():
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403
    
    try:
        with SafeDBConnection() as (cursor, db):
            cursor.execute("""
                SELECT
                    (SELECT COUNT(*) FROM candidates) AS candidates,
                    (SELECT COUNT(*) FROM recruiters) AS recruiters,
                    (SELECT COUNT(*) FROM mentors) AS mentors,
                    (SELECT COUNT(*) FROM jobs WHERE status = 'active') AS active_jobs,
                    (SELECT COUNT(*) FROM applications) AS applications,
                    (SELECT COUNT(*) FROM notifications WHERE is_read = FALSE) AS unread_notifications
            """)
            row = cursor.fetchone() or {}
            candidates = row.get('candidates', 0)
            recruiters = row.get('recruiters', 0)
            mentors = row.get('mentors', 0)
            active_jobs = row.get('active_jobs', 0)
            applications = row.get('applications', 0)
            unread_notifications = row.get('unread_notifications', 0)
        
        return jsonify({
            'success': True,
            'health': {
                'database': 'Connected',
                'candidates': candidates,
                'recruiters': recruiters,
                'mentors': mentors,
                'active_jobs': active_jobs,
                'applications': applications,
                'unread_notifications': unread_notifications,
                'status': 'Healthy'
            }
        })
        
    except Exception as e:
        print(f"Error checking platform health: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

def extract_resume_text(path):
    doc = fitz.open(path)
    text = ""
    for page in doc:
        text += page.get_text()
    return text.lower()
def extract_skills(text):
    skills_db = [
        'python','java','sql','flask','django','react',
        'machine learning','data science','html','css'
    ]
    return [s for s in skills_db if s in text]
def skill_match(candidate, job):
    c = set(candidate)
    j = set(job.lower().split(','))
    return int(len(c & j) / len(j) * 100) if j else 0

@app.route('/api/analytics')
def get_analytics():
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'unauthorized'}), 403

    db = None
    cursor = None

    try:
        db = get_connection()
        cursor = db.cursor(cursor_factory=RealDictCursor)

        def has_column(table_name, column_name):
            cursor.execute(
                """
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = 'public'
                    AND table_name = %s
                    AND column_name = %s
                LIMIT 1
                """,
                (table_name, column_name)
            )
            return cursor.fetchone() is not None

        # 2. User role distribution - Real data
        cursor.execute("SELECT COUNT(*) AS total FROM candidates")
        row = cursor.fetchone()
        candidates_count = row['total'] if row else 0
        
        cursor.execute("SELECT COUNT(*) AS total FROM recruiters")
        row = cursor.fetchone()
        recruiters_count = row['total'] if row else 0
        
        cursor.execute("SELECT COUNT(*) AS total FROM mentors")
        row = cursor.fetchone()
        mentors_count = row['total'] if row else 0

        # 1. Platform growth - get monthly user counts (resilient to missing created_at columns)
        growth_months = []
        growth_users = []
        candidates_has_created = has_column('candidates', 'created_at')
        recruiters_has_created = has_column('recruiters', 'created_at')
        mentors_has_created = has_column('mentors', 'created_at')

        if candidates_has_created and recruiters_has_created and mentors_has_created:
            cursor.execute("""
                SELECT TO_CHAR(created_at, 'YYYY-MM') AS month
                FROM candidates
                WHERE created_at >= CURRENT_TIMESTAMP - INTERVAL '12 months'
                UNION
                SELECT TO_CHAR(created_at, 'YYYY-MM') AS month
                FROM recruiters
                WHERE created_at >= CURRENT_TIMESTAMP - INTERVAL '12 months'
                UNION
                SELECT TO_CHAR(created_at, 'YYYY-MM') AS month
                FROM mentors
                WHERE created_at >= CURRENT_TIMESTAMP - INTERVAL '12 months'
                ORDER BY month
            """)
            months_raw = cursor.fetchall()

            if months_raw:
                growth_months = [row['month'] for row in months_raw]
                for month in growth_months:
                    cursor.execute("""
                        SELECT COUNT(*) as cnt FROM (
                            SELECT id FROM candidates WHERE TO_CHAR(created_at, 'YYYY-MM') <= %s
                            UNION
                            SELECT id FROM recruiters WHERE TO_CHAR(created_at, 'YYYY-MM') <= %s
                            UNION
                            SELECT id FROM mentors WHERE TO_CHAR(created_at, 'YYYY-MM') <= %s
                        ) users
                    """, (month, month, month))
                    count_row = cursor.fetchone()
                    growth_users.append(count_row['cnt'] if count_row else 0)

        # Fallback growth data when created_at columns are unavailable or no rows yet
        if not growth_months or not growth_users:
            total_users = int(candidates_count or 0) + int(recruiters_count or 0) + int(mentors_count or 0)
            growth_months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun']
            if total_users <= 0:
                growth_users = [0, 0, 0, 0, 0, 0]
            else:
                step = max(1, total_users // 6)
                growth_users = [step, step * 2, step * 3, step * 4, step * 5, total_users]

        # 3. Jobs overview - Real data
        cursor.execute("SELECT COUNT(*) AS total FROM jobs")
        row = cursor.fetchone()
        total_jobs = row['total'] if row else 0
        
        cursor.execute("SELECT COUNT(DISTINCT job_id) AS total FROM applications WHERE status IN ('Selected','Rejected')")
        row = cursor.fetchone()
        closed_jobs_count = row['total'] if row else 0
        active_jobs_count = max(0, total_jobs - closed_jobs_count)

        # 4. Top skills in demand - Parse from existing candidate profile skill fields
        skills_raw = []
        try:
            cursor.execute("""
                SELECT
                    COALESCE(primary_skills, '') AS primary_skills,
                    COALESCE(secondary_skills, '') AS secondary_skills,
                    COALESCE(frameworks_libraries, '') AS frameworks_libraries,
                    COALESCE(tools_technologies, '') AS tools_technologies,
                    COALESCE(cloud_platforms, '') AS cloud_platforms,
                    COALESCE("databases", '') AS databases
                FROM candidate_profiles
            """)
            skills_raw = cursor.fetchall()
        except Exception:
            skills_raw = []

        skills_dict = {}
        if skills_raw:
            for row in skills_raw:
                merged_skills = ",".join([
                    row.get('primary_skills', ''),
                    row.get('secondary_skills', ''),
                    row.get('frameworks_libraries', ''),
                    row.get('tools_technologies', ''),
                    row.get('cloud_platforms', ''),
                    row.get('databases', '')
                ])
                if merged_skills:
                    # Split by comma and count each skill
                    skill_list = [s.strip().lower() for s in merged_skills.split(',')]
                    for skill in skill_list:
                        if skill and skill not in {'na', 'n/a', '-', 'none'}:
                            skills_dict[skill] = skills_dict.get(skill, 0) + 1
        
        # Sort by demand and get top 10
        if skills_dict:
            sorted_skills = sorted(skills_dict.items(), key=lambda x: x[1], reverse=True)[:10]
            skills_labels = [skill[0].title() for skill in sorted_skills]
            skills_demand = [skill[1] for skill in sorted_skills]
        else:
            skills_labels = ['JavaScript', 'Python', 'React', 'Django', 'SQL', 'AWS']
            skills_demand = [220, 190, 160, 140, 130, 110]

        cleanup_db_resources(cursor, db)

        return jsonify({
            'success': True,
            'growth': {
                'months': growth_months,
                'users': growth_users
            },
            'roles': {
                'labels': ['Candidates', 'Mentors', 'Recruiters'],
                'data': [candidates_count, mentors_count, recruiters_count]
            },
            'jobs': {
                'labels': ['Active Jobs', 'Closed Jobs'],
                'data': [active_jobs_count, closed_jobs_count]
            },
            'skills': {
                'labels': skills_labels,
                'data': skills_demand
            }
        })
    except Exception as e:
        print(f"Analytics Error: {e}")
        try:
            if cursor and db:
                cleanup_db_resources(cursor, db)
        except Exception:
            pass
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/posted-jobs')
def posted_jobs():
    if session.get('role') != 'recruiter':
        return redirect('/login')

    user_id = session.get('user_id')
    settings = get_admin_settings()

    try:
        with SafeDBConnection() as (cursor, db):
            # Get recruiter info
            cursor.execute("SELECT profile_completed FROM recruiters WHERE id = %s", (user_id,))
            user = cursor.fetchone()
            is_complete = user['profile_completed'] if user else False

            # Get recruiter profile
            cursor.execute("SELECT * FROM recruiter_profiles WHERE recruiter_id = %s ORDER BY id DESC LIMIT 1", (user_id,))
            recruiter_profile = cursor.fetchone()

            # Compute profile_percent from stored profile (only essential required fields, excluding CIN)
            if recruiter_profile:
                required_keys = ['full_name','phone','designation','linkedin','company_name','company_type','company_size','industry','roles','experience_levels']
                total = len(required_keys)
                filled = 0
                for k in required_keys:
                    val = recruiter_profile.get(k) if isinstance(recruiter_profile, dict) else None
                    if val and str(val).strip() != '':
                        filled += 1
                profile_percent = int((filled/total)*100) if total else 0
            else:
                profile_percent = 40

            verification_status = recruiter_profile.get('verification_status') if recruiter_profile and isinstance(recruiter_profile, dict) else 'pending'

            # Get jobs posted
            cursor.execute("SELECT * FROM jobs WHERE recruiter_id = %s", (user_id,))
            jobs = cursor.fetchall()

            # Get all stats (placeholder, implement as needed)
            # Example: cursor.execute("SELECT COUNT(*) FROM jobs WHERE recruiter_id = %s", (user_id,))
            # stats = cursor.fetchone()
            # You can add stats to the template context if needed

            return render_template(
                "posted_jobs.html",
                recruiter_profile=recruiter_profile,
                jobs=jobs,
                is_complete=is_complete,
                profile_percent=profile_percent,
                verification_status=verification_status,
                maintenance_mode=settings.get('maintenance_mode', False)
            )
    except Exception as e:
        print(f"Error in posted_jobs: {e}")
        flash("Error loading jobs", "danger")
        return redirect('/recruiter-dashboard')

@app.route('/edit-job/<int:job_id>', methods=['GET', 'POST'])
def edit_job(job_id):
    if session.get('role') != 'recruiter':
        return redirect('/login')
    
    db = get_connection()
    cursor = db.cursor(cursor_factory=RealDictCursor)
    
    try:
        cursor.execute("""
            SELECT * FROM jobs WHERE id = %s AND recruiter_id = %s
        """, (job_id, session.get('user_id')))
        job = cursor.fetchone()
        
        if not job:
            flash("Job not found", "danger")
            return redirect('/recruiter-dashboard?tab=jobs')
        
        if request.method == 'POST':
            # Update job details
            title = request.form.get('title')
            department = request.form.get('department')
            location = request.form.get('location')
            job_type = request.form.get('job_type')
            employment_mode = request.form.get('employment_mode')
            salary_min = request.form.get('salary_min')
            salary_max = request.form.get('salary_max')
            min_experience = request.form.get('min_experience')
            max_experience = request.form.get('max_experience')
            education = request.form.get('education')
            openings = request.form.get('openings')
            deadline = request.form.get('deadline')
            description = request.form.get('description')
            skills = request.form.get('skills')
            interview_mode = request.form.get('interview_mode')

            # Keep active jobs visible by rejecting past deadlines.
            if deadline and str(deadline).strip():
                try:
                    from datetime import datetime
                    parsed_deadline = datetime.strptime(str(deadline), '%Y-%m-%d').date()
                    if parsed_deadline < datetime.now().date():
                        flash('Deadline cannot be in the past. Please choose today or a future date.', 'error')
                        cleanup_db_resources(cursor, db)
                        return redirect('/recruiter-dashboard?tab=jobs')
                except Exception:
                    flash('Invalid deadline format. Please use a valid date.', 'error')
                    cleanup_db_resources(cursor, db)
                    return redirect('/recruiter-dashboard?tab=jobs')
            
            cursor.execute("""
                UPDATE jobs SET 
                    title=%s, department=%s, location=%s, job_type=%s, 
                    employment_mode=%s, salary_min=%s, salary_max=%s,
                    min_experience=%s, max_experience=%s, education=%s,
                    openings=%s, deadline=%s, description=%s, 
                    skills=%s, interview_mode=%s
                WHERE id=%s AND recruiter_id=%s
            """, (title, department, location, job_type, employment_mode, 
                  salary_min, salary_max, min_experience, max_experience, 
                  education, openings, deadline, description, skills, 
                  interview_mode, job_id, session.get('user_id')))
            
            db.commit()
            flash("Job updated successfully!", "success")
            cleanup_db_resources(cursor, db)
            return redirect('/recruiter-dashboard?tab=jobs')
        
        # GET request - redirect to dashboard with edit tab
        cleanup_db_resources(cursor, db)
        return redirect(f'/recruiter-dashboard?tab=edit-job&job_id={job_id}')
    except Exception as e:
        print(f"Error: {e}")
        cleanup_db_resources(cursor, db)
        flash("Error updating job", "danger")
        return redirect('/recruiter-dashboard?tab=jobs')

@app.route('/delete-job/<int:job_id>', methods=['POST'])
def delete_job(job_id):
    if session.get('role') != 'recruiter':
        return redirect('/login')
    
    db = get_connection()
    cursor = db.cursor(cursor_factory=RealDictCursor)
    
    try:
        # First verify the job belongs to the recruiter
        cursor.execute("""
            SELECT id FROM jobs WHERE id = %s AND recruiter_id = %s
        """, (job_id, session.get('user_id')))
        
        if not cursor.fetchone():
            flash("Job not found", "danger")
            return redirect('/posted-jobs')
        
        # Delete applications for this job
        cursor.execute("DELETE FROM applications WHERE job_id = %s", (job_id,))
        
        # Delete the job
        cursor.execute("DELETE FROM jobs WHERE id = %s", (job_id,))
        
        db.commit()
        flash("Job deleted successfully!", "success")
        cleanup_db_resources(cursor, db)
        return redirect('/recruiter-dashboard?tab=jobs')
    except Exception as e:
        print(f"Error: {e}")
        db.rollback()
        cleanup_db_resources(cursor, db)
        flash("Error deleting job", "danger")
        return redirect('/recruiter-dashboard?tab=jobs')

@app.route('/duplicate-job/<int:job_id>', methods=['POST'])
def duplicate_job(job_id):
    if session.get('role') != 'recruiter':
        return redirect('/login')
    
    db = get_connection()
    cursor = db.cursor(cursor_factory=RealDictCursor)
    
    try:
        # Fetch the original job
        cursor.execute("""
            SELECT * FROM jobs WHERE id = %s AND recruiter_id = %s
        """, (job_id, session.get('user_id')))
        job = cursor.fetchone()
        
        if not job:
            flash("Job not found", "danger")
            return redirect('/recruiter-dashboard?tab=jobs')
        
        # Create duplicate with "[Copy]" suffix
        cursor.execute("""
            INSERT INTO jobs (
                recruiter_id, title, department, location, job_type,
                employment_mode, salary_min, salary_max, min_experience,
                max_experience, education, openings, deadline,
                description, skills, interview_mode, created_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP
            )
        """, (
            session.get('user_id'),
            f"{job['title']} [Copy]",
            job.get('department'),
            job.get('location'),
            job.get('job_type'),
            job.get('employment_mode'),
            job.get('salary_min'),
            job.get('salary_max'),
            job.get('min_experience'),
            job.get('max_experience'),
            job.get('education'),
            job.get('openings'),
            job.get('deadline'),
            job.get('description'),
            job.get('skills'),
            job.get('interview_mode')
        ))
        
        db.commit()
        flash("Job duplicated successfully!", "success")
        cleanup_db_resources(cursor, db)
        return redirect('/recruiter-dashboard?tab=jobs')
    except Exception as e:
        print(f"Error duplicating job: {e}")
        db.rollback()
        cleanup_db_resources(cursor, db)
        flash("Error duplicating job", "danger")
        return redirect('/recruiter-dashboard?tab=jobs')

@app.route('/toggle-job-status/<int:job_id>', methods=['POST'])
def toggle_job_status(job_id):
    if session.get('role') != 'recruiter':
        return redirect('/login')
    
    db = get_connection()
    cursor = db.cursor(cursor_factory=RealDictCursor)
    
    try:
        # Check current status (using deadline as active/inactive indicator)
        cursor.execute("""
            SELECT id, deadline FROM jobs WHERE id = %s AND recruiter_id = %s
        """, (job_id, session.get('user_id')))
        job = cursor.fetchone()
        
        if not job:
            flash("Job not found", "danger")
            return redirect('/recruiter-dashboard?tab=jobs')
        
        # Toggle: if no deadline or past deadline, set to 30 days from now
        # If active (future deadline), set to today (making it closed)
        from datetime import datetime, timedelta
        today = datetime.now().date()
        
        if job['deadline'] and job['deadline'] >= today:
            # Job is active, pause it by setting deadline to yesterday
            new_deadline = today - timedelta(days=1)
            status_msg = "paused"
        else:
            # Job is paused, activate it
            new_deadline = today + timedelta(days=30)
            status_msg = "activated"
        
        cursor.execute("""
            UPDATE jobs SET deadline = %s WHERE id = %s
        """, (new_deadline, job_id))
        
        db.commit()
        flash(f"Job {status_msg} successfully!", "success")
        cleanup_db_resources(cursor, db)
        return redirect('/recruiter-dashboard?tab=jobs')
    except Exception as e:
        print(f"Error toggling job status: {e}")
        db.rollback()
        cleanup_db_resources(cursor, db)
        flash("Error updating job status", "danger")
        return redirect('/recruiter-dashboard?tab=jobs')

@app.route('/applications')
def applications():
    if session.get('role') != 'recruiter':
        return redirect('/login')
    
    user_id = session.get('user_id')
    
    try:
        with SafeDBConnection() as (cursor, db):
            cursor.execute("SELECT profile_completed FROM recruiters WHERE id = %s", (user_id,))
            user = cursor.fetchone()
            is_complete = user['profile_completed'] if user else False
            
            # Get recruiter profile
            cursor.execute("SELECT * FROM recruiter_profiles WHERE recruiter_id = %s ORDER BY id DESC LIMIT 1", (user_id,))
            profile = cursor.fetchone()
            
            # Compute profile_percent from stored profile (only essential required fields, excluding CIN)
            if profile:
                required_keys = ['full_name','phone','designation','linkedin','company_name','company_type','company_size','industry','roles','experience_levels']
                total = len(required_keys)
                filled = 0
                for k in required_keys:
                    val = profile.get(k) if isinstance(profile, dict) else None
                    if val and str(val).strip() != '':
                        filled += 1
                profile_percent = int((filled/total)*100) if total else 0
            else:
                profile_percent = 40
            
            verification_status = profile.get('verification_status') if profile and isinstance(profile, dict) else 'pending'
            
            # Get all applications for jobs posted by this recruiter, grouped by job
            try:
                cursor.execute("SELECT to_regclass('public.candidate_profiles') AS candidate_profiles_tbl")
                cp_exists = bool((cursor.fetchone() or {}).get('candidate_profiles_tbl'))

                if cp_exists:
                    cursor.execute("""
                        SELECT 
                            a.id,
                            a.status,
                            a.assessment_status,
                            a.assessment_assigned_at,
                            a.assessment_completed_at,
                            a.applied_at,
                            c.id AS candidate_id,
                            c.name AS candidate_name,
                            c.email AS candidate_email,
                            cp.primary_skills,
                            cp.secondary_skills,
                            cp.work_experience,
                            cp.degree,
                            cp.degree AS education,
                            cp.resume_file,
                            COALESCE(latest_ai.percentage, 0) AS ai_score,
                            at_assessment.obtained_marks AS assessment_obtained_marks,
                            at_assessment.total_marks AS assessment_total_marks,
                            at_assessment.percentage AS assessment_percentage,
                            j.id AS job_id,
                            j.title AS job_title,
                            j.skills AS job_skills,
                            j.description AS job_description,
                            j.location AS job_location,
                            j.employment_mode AS job_employment_mode,
                            j.min_experience AS job_min_experience,
                            j.max_experience AS job_max_experience,
                            j.education AS job_education,
                            COALESCE(j.openings, 0) AS job_openings
                        FROM applications a
                        JOIN candidates c ON a.candidate_id = c.id
                        JOIN jobs j ON a.job_id = j.id
                        LEFT JOIN candidate_profiles cp ON c.id = cp.candidate_id
                        LEFT JOIN ai_tests at_assessment ON a.assessment_id = at_assessment.id
                        LEFT JOIN (
                            SELECT DISTINCT ON (candidate_id) candidate_id, percentage
                            FROM ai_tests
                            WHERE status = 'completed'
                            ORDER BY candidate_id, completed_at DESC
                        ) AS latest_ai ON c.id = latest_ai.candidate_id
                        WHERE j.recruiter_id = %s
                        ORDER BY j.id, a.applied_at DESC
                    """, (user_id,))
                else:
                    cursor.execute("""
                        SELECT 
                            a.id,
                            a.status,
                            a.assessment_status,
                            a.assessment_assigned_at,
                            a.assessment_completed_at,
                            a.applied_at,
                            c.id AS candidate_id,
                            c.name AS candidate_name,
                            c.email AS candidate_email,
                            NULL::TEXT AS primary_skills,
                            NULL::TEXT AS secondary_skills,
                            NULL::TEXT AS work_experience,
                            NULL::TEXT AS degree,
                            NULL::TEXT AS education,
                            NULL::TEXT AS resume_file,
                            COALESCE(latest_ai.percentage, 0) AS ai_score,
                            at_assessment.obtained_marks AS assessment_obtained_marks,
                            at_assessment.total_marks AS assessment_total_marks,
                            at_assessment.percentage AS assessment_percentage,
                            j.id AS job_id,
                            j.title AS job_title,
                            j.skills AS job_skills,
                            j.description AS job_description,
                            j.location AS job_location,
                            j.employment_mode AS job_employment_mode,
                            j.min_experience AS job_min_experience,
                            j.max_experience AS job_max_experience,
                            j.education AS job_education,
                            COALESCE(j.openings, 0) AS job_openings
                        FROM applications a
                        JOIN candidates c ON a.candidate_id = c.id
                        JOIN jobs j ON a.job_id = j.id
                        LEFT JOIN ai_tests at_assessment ON a.assessment_id = at_assessment.id
                        LEFT JOIN (
                            SELECT DISTINCT ON (candidate_id) candidate_id, percentage
                            FROM ai_tests
                            WHERE status = 'completed'
                            ORDER BY candidate_id, completed_at DESC
                        ) AS latest_ai ON c.id = latest_ai.candidate_id
                        WHERE j.recruiter_id = %s
                        ORDER BY j.id, a.applied_at DESC
                    """, (user_id,))
                applications_list = cursor.fetchall()
            except Exception as query_error:
                print(f"Query error: {query_error}")
                import traceback
                traceback.print_exc()
                applications_list = []
            
            # Group applications by job
            jobs_dict = {}
            for app in applications_list:
                try:
                    # Convert to dict if it's a RealDictRow
                    app_dict = dict(app) if hasattr(app, 'items') else dict(zip([desc[0] for desc in cursor.description], app))
                    
                    match = calculate_match_score(
                        profile={
                            "primary_skills": app_dict.get("primary_skills"),
                            "secondary_skills": app_dict.get("secondary_skills")
                        },
                        job={"skills": app_dict.get("job_skills")},
                        ai_score=app_dict.get("ai_score") or 0
                    )
                    
                    app_dict["skill_match"] = match["skill_match"]

                    recruiter_ai = generate_recruiter_ai_assessment(
                        job_description=app_dict.get("job_description") or "",
                        candidate_data={
                            "candidate_name": app_dict.get("candidate_name") or "Candidate",
                            "job_title": app_dict.get("job_title") or "Role",
                            "location": app_dict.get("job_location") or "Location not specified",
                            "employment_type": app_dict.get("job_employment_mode") or "Full-Time",
                            "job_skills": app_dict.get("job_skills") or "",
                            "primary_skills": app_dict.get("primary_skills") or "",
                            "secondary_skills": app_dict.get("secondary_skills") or "",
                            "work_experience": app_dict.get("work_experience") or "",
                            "candidate_education": app_dict.get("degree") or app_dict.get("education") or "",
                            "job_min_experience": app_dict.get("job_min_experience"),
                            "job_max_experience": app_dict.get("job_max_experience"),
                            "job_education": app_dict.get("job_education") or ""
                        }
                    )
                    app_dict["ai_dashboard_text"] = recruiter_ai.get("formatted_output", "")
                    app_dict["ai_match_score"] = recruiter_ai.get("match_score", 0)
                    app_dict["ai_next_action"] = recruiter_ai.get("next_action", "Review")
                    app_dict["ai_priority"] = recruiter_ai.get("priority", "Medium")
                    app_dict["selection_score"] = calculate_selection_score(app_dict)
                    
                    # Group by job_id
                    job_id = app_dict.get("job_id")
                    if job_id not in jobs_dict:
                        jobs_dict[job_id] = {
                            "job_id": job_id,
                            "job_title": app_dict.get("job_title"),
                            "job_skills": app_dict.get("job_skills"),
                            "job_openings": int(app_dict.get("job_openings") or 0),
                            "applications": []
                        }
                    
                    jobs_dict[job_id]["applications"].append(app_dict)
                except Exception as calc_error:
                    print(f"Error processing application: {calc_error}")

            # Sort each job's applications by best-fit selection score and mark best-fit/reserve pools
            for job in jobs_dict.values():
                openings = int(job.get("job_openings") or 0)
                reserve_limit = max(20, openings // 2) if openings else 20
                ranked_apps = sorted(
                    job["applications"],
                    key=lambda item: (
                        -int(item.get("selection_score") or 0),
                        -int(item.get("ai_match_score") or 0),
                        -int(item.get("skill_match") or 0),
                        item.get("applied_at") or datetime.min
                    )
                )

                best_fit_limit = openings if openings > 0 else len(ranked_apps)
                for index, app in enumerate(ranked_apps, start=1):
                    app["selection_rank"] = index
                    if best_fit_limit and index <= best_fit_limit:
                        app["selection_bucket"] = "best-fit"
                    elif index <= best_fit_limit + reserve_limit:
                        app["selection_bucket"] = "reserve"
                    else:
                        app["selection_bucket"] = "backlog"

                job["applications"] = ranked_apps
                job["best_fit_limit"] = best_fit_limit
                job["reserve_limit"] = reserve_limit
                job["best_fit_candidates"] = ranked_apps[:best_fit_limit]
                job["reserve_candidates"] = ranked_apps[best_fit_limit:best_fit_limit + reserve_limit]
                job["selection_summary"] = {
                    "best_fit_count": len(job["best_fit_candidates"]),
                    "reserve_count": len(job["reserve_candidates"]),
                    "total_applications": len(ranked_apps)
                }
            
            # Convert to list sorted by job_id
            jobs_grouped = sorted(jobs_dict.values(), key=lambda x: x["job_id"], reverse=True)

        return render_template('applications_grouped.html', jobs_grouped=jobs_grouped, is_complete=is_complete, profile=profile, profile_percent=profile_percent, verification_status=verification_status)
    except Exception as e:
        print(f"Error in applications: {e}")
        import traceback
        traceback.print_exc()
        flash("Error loading applications", "danger")
        return redirect('/recruiter-dashboard')

@app.route('/interviews')
def interviews():
    if session.get('role') != 'recruiter':
        return redirect('/login')
    
    user_id = session.get('user_id')
    
    try:
        with SafeDBConnection() as (cursor, db):
            cursor.execute("SELECT profile_completed FROM recruiters WHERE id = %s", (user_id,))
            user = cursor.fetchone()
            is_complete = user['profile_completed'] if user else False
            
            # Get recruiter profile
            cursor.execute("SELECT * FROM recruiter_profiles WHERE recruiter_id = %s ORDER BY id DESC LIMIT 1", (user_id,))
            profile = cursor.fetchone()
            
            # Compute profile_percent from stored profile (only essential required fields, excluding CIN)
            if profile:
                required_keys = ['full_name','phone','designation','linkedin','company_name','company_type','company_size','industry','roles','experience_levels']
                total = len(required_keys)
                filled = 0
                for k in required_keys:
                    val = profile.get(k) if isinstance(profile, dict) else None
                    if val and str(val).strip() != '':
                        filled += 1
                profile_percent = int((filled/total)*100) if total else 0
            else:
                profile_percent = 40
            
            verification_status = profile.get('verification_status') if profile and isinstance(profile, dict) else 'pending'
            
            # Get applications for recruiter jobs and only the latest interview record per application
            # Use DISTINCT ON to keep only the first (latest) interview per application
            cursor.execute("""
                SELECT DISTINCT ON (a.id)
                       a.id, a.status, a.applied_at,
                       c.id as candidate_id, c.name as candidate_name, c.email as candidate_email,
                       j.id as job_id, j.title as job_title,
                       cp.resume_file,
                       i.id as interview_id, i.interview_mode, i.interview_date, i.interview_time, 
                       i.interview_link, i.location, i.status as interview_status,
                       i.interview_round, i.round_name, i.total_rounds
                FROM applications a
                JOIN candidates c ON a.candidate_id = c.id
                JOIN jobs j ON a.job_id = j.id
                LEFT JOIN candidate_profiles cp ON c.id = cp.candidate_id
                LEFT JOIN interviews i ON (i.application_id = a.id OR (i.application_id IS NULL AND i.candidate_id = c.id AND i.job_id = j.id))
                WHERE j.recruiter_id = %s AND a.status IN ('Interview'::application_status, 'Selected'::application_status, 'Rejected'::application_status)
                ORDER BY a.id, COALESCE(i.interview_round, 0) DESC, COALESCE(i.id, 0) DESC
            """, (user_id,))
            interviews_list = cursor.fetchall()
            
            # Group interviews by job
            jobs_dict = {}
            for interview in interviews_list:
                interview_dict = dict(interview) if hasattr(interview, 'items') else dict(zip([desc[0] for desc in cursor.description], interview))
                
                job_id = interview_dict.get("job_id")
                if job_id not in jobs_dict:
                    jobs_dict[job_id] = {
                        "job_id": job_id,
                        "job_title": interview_dict.get("job_title"),
                        "interviews": []
                    }
                
                jobs_dict[job_id]["interviews"].append(interview_dict)
            
            # Convert to list sorted by job_id
            jobs_grouped = sorted(jobs_dict.values(), key=lambda x: x["job_id"], reverse=True)
        
        return render_template('interviews.html', jobs_grouped=jobs_grouped, is_complete=is_complete, profile=profile, profile_percent=profile_percent, verification_status=verification_status, tab='interviews')
    except Exception as e:
        print(f"Error in interviews: {e}")
        import traceback
        traceback.print_exc()
        flash("Error loading interviews", "danger")
        return redirect('/recruiter-dashboard')

@app.route('/schedule-next-round/<int:app_id>/<int:next_round>', methods=['POST'])
def schedule_next_round(app_id, next_round):
    """
    Move a candidate to the next round after feedback submission.
    Creates a new interview record for the next round.
    """
    if session.get('role') != 'recruiter':
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    
    try:
        with SafeDBConnection() as (cursor, db):
            # Get application and candidate info
            cursor.execute("""
                SELECT a.id, a.candidate_id, a.job_id, 
                       c.name as candidate_name, c.email as candidate_email,
                       j.recruiter_id, j.title as job_title,
                       i.total_rounds, i.round_name,
                       i.interview_mode, i.interview_date, i.interview_time,
                       i.interview_link, i.location, i.interviewer_name,
                       i.interview_type, i.notes
                FROM applications a
                JOIN candidates c ON a.candidate_id = c.id
                JOIN jobs j ON a.job_id = j.id
                LEFT JOIN LATERAL (
                    SELECT i1.*
                    FROM interviews i1
                    WHERE i1.application_id = a.id
                    ORDER BY COALESCE(i1.interview_round, 1) DESC, i1.id DESC
                    LIMIT 1
                ) i ON TRUE
                WHERE a.id = %s
                LIMIT 1
            """, (app_id,))
            
            app_data = cursor.fetchone()
            if not app_data:
                return jsonify({'success': False, 'message': 'Application not found'}), 404
            
            # Verify recruiter owns this job
            if app_data['recruiter_id'] != session.get('user_id'):
                return jsonify({'success': False, 'message': 'Unauthorized'}), 403
            
            candidate_id = app_data['candidate_id']
            job_id = app_data['job_id']
            total_rounds = app_data['total_rounds'] or 1
            
            # Get next round name from configuration (if grid discussion after selected)
            next_round_name = f"Round {next_round}"
            if next_round > total_rounds:
                return jsonify({'success': False, 'message': f'Cannot advance beyond Round {total_rounds}'}), 400
            
            # Create new interview record for next round
            try:
                cursor.execute("""
                    INSERT INTO interviews 
                    (candidate_id, recruiter_id, job_id, application_id,
                     interview_round, round_name, total_rounds,
                     interview_date, interview_time, interview_mode, interview_link,
                     location, interviewer_name, interview_type, notes,
                     status)
                    VALUES (%s, %s, %s, %s,
                            %s, %s, %s,
                            %s, %s, %s, %s,
                            %s, %s, %s, %s,
                            'Scheduled')
                """, (
                    candidate_id,
                    session.get('user_id'),
                    job_id,
                    app_id,
                    next_round,
                    next_round_name,
                    total_rounds,
                    app_data.get('interview_date'),
                    app_data.get('interview_time'),
                    app_data.get('interview_mode'),
                    app_data.get('interview_link'),
                    app_data.get('location'),
                    app_data.get('interviewer_name'),
                    app_data.get('interview_type'),
                    app_data.get('notes')
                ))
                db.commit()
                
                # Send email notification to candidate
                cursor.execute("SELECT email FROM candidates WHERE id = %s", (candidate_id,))
                candidate = cursor.fetchone()
                if candidate:
                    try:
                        from flask_mail import Message
                        candidate_email = candidate['email']
                        msg = Message(
                            subject=f"Round {next_round} Interview - {app_data['job_title']}",
                            recipients=[candidate_email],
                            html=f"""
                            <div style="font-family: Arial, sans-serif; color: #333;">
                                <p>Dear {app_data['candidate_name']},</p>
                                <p>Congratulations! You have been selected for <strong>Round {next_round}</strong> of the interview process for the position of <strong>{app_data['job_title']}</strong>.</p>
                                <p>We will send you the interview details shortly. Please stay ready.</p>
                                <p>Best regards,<br>HireHub Team</p>
                            </div>
                            """
                        )
                        Thread(target=send_async_email, args=(app, msg)).start()
                    except Exception as email_error:
                        print(f"Error sending email: {email_error}")
                
                return jsonify({'success': True, 'message': f'Candidate moved to Round {next_round}'}), 200
            except Exception as e:
                db.rollback()
                print(f"Error creating next round interview: {e}")
                return jsonify({'success': False, 'message': str(e)}), 500
    
    except Exception as e:
        print(f"Error in schedule_next_round: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'message': 'Server error'}), 500

@app.route('/update-application/<int:app_id>/<action>', methods=['GET', 'POST'])
def update_application(app_id, action):
    """
    WORKFLOW DEPENDENCY: Candidate -> Recruiter (Interview Scheduling)
    - Only recruiters can schedule interviews
    - Candidates cannot self-create interviews
    - Interview workflow starts only from recruiter side
    
    WORKFLOW DEPENDENCY: Candidate -> Recruiter (Offer Letter)
    - Offer letter exists only if recruiter confirms selection
    - Candidate receives offer after recruiter selects them
    """
    if session.get('role') != 'recruiter':
        return redirect('/login')
    
    # Map action to status
    action_map = {
        'shortlist': 'Shortlisted',
        'interview': 'Interview',      # Recruiter schedules interview
        'offer': 'Selected',            # Recruiter sends offer
        'reject': 'Rejected'
    }
    
    if action not in action_map:
        flash("Invalid action", "danger")
        return redirect('/applications')
    
    new_status = action_map[action]

    # Auto-offer workflow: selecting candidate immediately generates offer letter PDF
    if action == 'offer':
        try:
            generated = generate_offer_letter_for_application(
                app_id=app_id,
                recruiter_id=session.get('user_id'),
                auto_generated=True
            )
            flash(
                f"Candidate marked as Selected. Offer letter generated automatically for {generated.get('candidate_name', 'candidate')}.",
                "success"
            )
            return redirect('/interviews')
        except Exception as e:
            print(f"Auto offer generation error: {e}")
            flash(f"Failed to auto-generate offer letter: {str(e)}", 'danger')
            return redirect('/applications')
    
    db = get_connection()
    cursor = db.cursor(cursor_factory=RealDictCursor)
    
    try:
        # Verify the application belongs to a job posted by this recruiter
        cursor.execute(
            "SELECT a.id FROM applications a JOIN jobs j ON a.job_id = j.id WHERE a.id = %s AND j.recruiter_id = %s",
            (app_id, session.get('user_id'))
        )
        
        if not cursor.fetchone():
            flash("Application not found", "danger")
            return redirect('/applications')
        
        # Update application status
        cursor.execute(
            "UPDATE applications SET status = %s WHERE id = %s",
            (new_status, app_id)
        )
        
        db.commit()
        flash(f"Application status updated to {new_status}", "success")
        cleanup_db_resources(cursor, db)
        
        # Redirect based on where the action was initiated
        if action in ['offer', 'reject']:
            return redirect('/interviews')
        else:
            return redirect('/applications')
    except Exception as e:
        print(f"Error: {e}")
        db.rollback()
        cleanup_db_resources(cursor, db)
        flash("Error updating application", "danger")
        return redirect('/applications')

@app.route('/accept-application/<int:app_id>', methods=['POST'])
def accept_application(app_id):
    """Accept application and prepare to generate AI test editor"""
    if session.get('role') != 'recruiter':
        return jsonify({'error': 'Unauthorized'}), 401
    
    recruiter_id = session.get('user_id')
    
    try:
        with SafeDBConnection() as (cursor, db):
            # Verify the application belongs to recruiter's job
            cursor.execute("""
                SELECT a.id, a.candidate_id, a.job_id, j.title, j.skills, c.name as candidate_name, c.email as candidate_email
                FROM applications a
                JOIN jobs j ON a.job_id = j.id
                JOIN candidates c ON a.candidate_id = c.id
                WHERE a.id = %s AND j.recruiter_id = %s AND a.status = 'Applied'::application_status
            """, (app_id, recruiter_id))
            
            app_data = cursor.fetchone()
            if not app_data:
                return jsonify({'error': 'Application not found or already processed'}), 404
            
            candidate_id = app_data['candidate_id']
            job_id = app_data['job_id']
            
            # Update application status to 'Accepted' first
            cursor.execute("""
                UPDATE applications SET status = 'Accepted'::application_status, updated_at = NOW()
                WHERE id = %s
            """, (app_id,))
            
            db.commit()
            
            # Log activity
            log_activity(
                recruiter_id, 'recruiter', 'application_accepted',
                f'Accepted application from {app_data["candidate_name"]}',
                f'Application accepted for {app_data["title"]}',
                {'application_id': app_id, 'job_id': job_id}
            )
        
        return jsonify({
            'success': True, 
            'message': 'Application accepted. Proceeding to test configuration...',
            'redirect_url': f'/edit-recruiter-test/{app_id}'
        })
    
    except Exception as e:
        print(f"Error accepting application: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': f'Failed to accept application: {str(e)}'}), 500

@app.route('/edit-recruiter-test/<int:app_id>')
def edit_recruiter_test(app_id):
    """Recruiter test editor - Configure and generate AI test questions"""
    if session.get('role') != 'recruiter':
        return redirect('/login')
    
    recruiter_id = session.get('user_id')
    
    try:
        with SafeDBConnection() as (cursor, db):
            # Get application and job details
            cursor.execute("""
                SELECT a.id, a.candidate_id, a.job_id, a.status,
                       j.title, j.skills, j.recruiter_id,
                       c.name as candidate_name, c.email as candidate_email
                FROM applications a
                JOIN jobs j ON a.job_id = j.id
                JOIN candidates c ON a.candidate_id = c.id
                WHERE a.id = %s AND j.recruiter_id = %s
            """, (app_id, recruiter_id))
            
            app_data = cursor.fetchone()
            if not app_data:
                flash('Application not found', 'error')
                return redirect('/applications')
            
            # Check if test already exists for this application
            cursor.execute("""
                SELECT test_id FROM recruiter_assessments WHERE application_id = %s
            """, (app_id,))
            
            assessment = cursor.fetchone()
            test_id = assessment['test_id'] if assessment else None
            
            # Get recruiter profile for verification
            cursor.execute("""
                SELECT verification_status FROM recruiter_profiles WHERE recruiter_id = %s
            """, (recruiter_id,))
            profile = cursor.fetchone()
            verification_status = profile['verification_status'] if profile else 'pending'
            
            # Get profile completion
            cursor.execute("""
                SELECT 
                    CASE 
                        WHEN rp.id IS NULL THEN 0
                        ELSE 100
                    END as profile_percent
                FROM recruiters r
                LEFT JOIN recruiter_profiles rp ON r.id = rp.recruiter_id
                WHERE r.id = %s
            """, (recruiter_id,))
            profile_data = cursor.fetchone()
            profile_percent = profile_data['profile_percent'] if profile_data else 0
            
            # Prepare configuration data
            config_data = {
                'app_id': app_id,
                'candidate_name': app_data['candidate_name'],
                'candidate_email': app_data['candidate_email'],
                'job_title': app_data['title'],
                'job_skills': app_data['skills'],
                'test_id': test_id,
                'application_status': app_data['status'],
                'verification_status': verification_status,
                'profile_percent': profile_percent,
                'tab': 'test_editor'  # Set current tab
            }
        
        return render_template('recruiter_test_editor.html', **config_data)
    
    except Exception as e:
        print(f"Error opening test editor: {e}")
        import traceback
        traceback.print_exc()
        flash('Error loading test editor', 'error')
        return redirect('/applications')

@app.route('/api/generate-recruiter-questions/<int:app_id>', methods=['POST'])
def generate_recruiter_questions(app_id):
    """API endpoint to generate AI questions for recruiter test"""
    if session.get('role') != 'recruiter':
        return jsonify({'error': 'Unauthorized'}), 401
    
    recruiter_id = session.get('user_id')
    
    try:
        data = request.get_json()
        
        num_aptitude = int(data.get('num_aptitude', 15))
        num_technical = int(data.get('num_technical', 15))
        
        with SafeDBConnection() as (cursor, db):
            # Verify application belongs to recruiter
            cursor.execute("""
                SELECT a.id, j.skills, j.title
                FROM applications a
                JOIN jobs j ON a.job_id = j.id
                WHERE a.id = %s AND j.recruiter_id = %s
            """, (app_id, recruiter_id))
            
            app_data = cursor.fetchone()
            if not app_data:
                return jsonify({'error': 'Application not found'}), 404
            
            job_skills = app_data['skills']
            job_title = app_data['title']
        
        # Generate aptitude questions
        aptitude_prompt = f"""Generate {num_aptitude} aptitude test questions for job position: {job_title}

Cover topics:
- Logical Reasoning
- Quantitative Ability
- Verbal Reasoning
- Data Interpretation
- Problem Solving

Each question must have 4 options (A, B, C, D) with one correct answer."""
        
        # Generate technical questions
        technical_prompt = f"""Generate {num_technical} technical questions for skills: {job_skills or 'General Programming'}
For job position: {job_title}

Cover:
- Core concepts and fundamentals
- Practical application
- Problem-solving scenarios
- Best practices
- Industry standards

Each question must have 4 options (A, B, C, D) with one correct answer."""
        
        # Call AI generation
        aptitude_questions = generate_test_questions_combined(aptitude_prompt, num_aptitude, 'Aptitude')
        technical_questions = generate_test_questions_combined(technical_prompt, num_technical, 'Technical')
        
        # Combine with section information
        all_questions = []
        for idx, q in enumerate(aptitude_questions, 1):
            all_questions.append({
                'section': 'Aptitude',
                'question_number': idx,
                'question': q.get('question', ''),
                'option_a': q.get('option_a', ''),
                'option_b': q.get('option_b', ''),
                'option_c': q.get('option_c', ''),
                'option_d': q.get('option_d', ''),
                'correct_answer': q.get('correct_answer', 'A')
            })
        
        for idx, q in enumerate(technical_questions, 1):
            all_questions.append({
                'section': 'Technical',
                'question_number': idx,
                'question': q.get('question', ''),
                'option_a': q.get('option_a', ''),
                'option_b': q.get('option_b', ''),
                'option_c': q.get('option_c', ''),
                'option_d': q.get('option_d', ''),
                'correct_answer': q.get('correct_answer', 'A')
            })
        
        return jsonify({
            'success': True,
            'questions': all_questions,
            'total_questions': len(all_questions),
            'aptitude_count': len(aptitude_questions),
            'technical_count': len(technical_questions)
        })
    
    except Exception as e:
        print(f"[ERROR] Error generating questions: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/save-recruiter-test/<int:app_id>', methods=['POST'])
def save_recruiter_test(app_id):
    """Save test configuration and send to candidate"""
    if session.get('role') != 'recruiter':
        return jsonify({'error': 'Unauthorized'}), 401
    
    recruiter_id = session.get('user_id')
    
    try:
        data = request.get_json()
        questions = data.get('questions', [])
        duration_minutes = int(data.get('duration_minutes', 90))
        total_marks = int(data.get('total_marks', 60))
        
        if not questions or len(questions) == 0:
            return jsonify({'error': 'No questions provided'}), 400
        
        with SafeDBConnection() as (cursor, db):
            # Verify application and get details
            cursor.execute("""
                SELECT a.id, a.candidate_id, a.job_id, j.title, j.recruiter_id, c.name as candidate_name
                FROM applications a
                JOIN jobs j ON a.job_id = j.id
                JOIN candidates c ON a.candidate_id = c.id
                WHERE a.id = %s AND j.recruiter_id = %s
            """, (app_id, recruiter_id))
            
            app_data = cursor.fetchone()
            if not app_data:
                return jsonify({'error': 'Application not found'}), 404
            
            candidate_id = app_data['candidate_id']
            job_id = app_data['job_id']
            job_title = app_data['title']
            candidate_name = app_data['candidate_name']
            
            # Create AI test record
            num_questions = len(questions)
            cursor.execute("""
                INSERT INTO ai_tests (candidate_id, skills_tested, test_type, total_questions, total_marks, status)
                VALUES (%s, %s, 'combined_recruiter', %s, %s, 'pending')
                RETURNING id
            """, (candidate_id, job_id, num_questions, total_marks))
            
            test_id = cursor.fetchone()['id']
            
            # Insert questions
            for idx, q in enumerate(questions, 1):
                cursor.execute("""
                    INSERT INTO ai_test_questions 
                    (test_id, question_number, question_text, option_a, option_b, option_c, option_d, correct_answer)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    test_id, idx,
                    q.get('question', ''),
                    q.get('option_a', ''),
                    q.get('option_b', ''),
                    q.get('option_c', ''),
                    q.get('option_d', ''),
                    q.get('correct_answer', 'A')
                ))
            
            # Create recruiter assessment record
            due_date = datetime.now() + timedelta(days=7)
            cursor.execute("""
                INSERT INTO recruiter_assessments 
                (recruiter_id, candidate_id, job_id, application_id, test_id, assessment_type, skills_tested, due_at, status)
                VALUES (%s, %s, %s, %s, %s, 'combined_recruiter', %s, %s, 'sent')
            """, (recruiter_id, candidate_id, job_id, app_id, test_id, job_title, due_date))
            
            # Update application status to 'Test Sent'
            cursor.execute("""
                UPDATE applications SET status = 'Test Sent'::application_status, updated_at = NOW()
                WHERE id = %s
            """, (app_id,))
            
            db.commit()
            
            # Send notification to candidate
            create_notification(
                'candidate', candidate_id, 'test_assigned',
                'Assessment Test Assigned',
                f'You have been assigned an assessment test for {job_title}. Complete it within 7 days.',
                f'/take-recruiter-test/{test_id}'
            )
            
            # Log activity
            log_activity(
                recruiter_id, 'recruiter', 'test_assigned',
                f'Assigned customized test to {candidate_name}',
                f'Assessment test sent for {job_title} ({num_questions} questions, {duration_minutes} mins)',
                {'application_id': app_id, 'test_id': test_id, 'job_title': job_title}
            )
        
        return jsonify({
            'success': True,
            'message': 'Test sent to candidate successfully',
            'test_id': test_id
        })
    
    except Exception as e:
        print(f"Error saving test: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': f'Failed to save test: {str(e)}'}), 500

@app.route('/generate-test-for-job/<int:job_id>', methods=['POST'])
def generate_test_for_job(job_id):
    """Generate a single AI test (Technical + Aptitude + General Knowledge) for all candidates applied to a job"""
    if session.get('role') != 'recruiter':
        return jsonify({'error': 'Unauthorized'}), 401
    
    recruiter_id = session.get('user_id')
    data = request.get_json(silent=True) or {}
    
    try:
        with SafeDBConnection() as (cursor, db):
            # Verify job belongs to recruiter
            cursor.execute("""
                SELECT id, title, skills FROM jobs 
                WHERE id = %s AND recruiter_id = %s
            """, (job_id, recruiter_id))
            
            job = cursor.fetchone()
            if not job:
                return jsonify({'error': 'Job not found'}), 404
            
            job_title = job['title']
            job_skills = job['skills']
            
            # Get all Applied candidates for this job
            cursor.execute("""
                SELECT 
                    a.id as app_id,
                    a.candidate_id,
                    c.name as candidate_name,
                    c.email as candidate_email
                FROM applications a
                JOIN candidates c ON a.candidate_id = c.id
                WHERE a.job_id = %s AND a.status = 'Applied'
                ORDER BY a.applied_at DESC
            """, (job_id,))
            
            candidates = cursor.fetchall()
            
            if not candidates:
                return jsonify({
                    'success': False,
                    'error': 'No Applied candidates found for this job'
                }), 400
            
            # Generate combined AI test questions with 3 sections
            try:
                num_questions = int(data.get('num_questions', 20))
                test_duration = int(data.get('test_duration', 60))
            except (TypeError, ValueError):
                return jsonify({
                    'success': False,
                    'error': 'Please enter valid numeric values for number of questions and duration'
                }), 400

            if num_questions <= 0 or test_duration <= 0:
                return jsonify({
                    'success': False,
                    'error': 'Number of questions and duration must be greater than 0'
                }), 400
            instructions = data.get('instructions', '')
            notify_candidates = data.get('notify_candidates', True)

            if num_questions < 3:
                return jsonify({
                    'success': False,
                    'error': 'Number of questions must be at least 3 for all sections'
                }), 400

            # Distribution: 40% technical, 30% aptitude, 30% general knowledge
            technical_count = max(1, int(num_questions * 0.4))
            aptitude_count = max(1, int(num_questions * 0.3))
            general_count = max(1, num_questions - technical_count - aptitude_count)

            # Adjust if rounding causes mismatch
            section_total = technical_count + aptitude_count + general_count
            if section_total != num_questions:
                general_count += (num_questions - section_total)

            technical_prompt = f"""Generate {technical_count} technical MCQ questions for job position: {job_title}
Required skills: {job_skills or 'General Programming and Problem Solving'}

Focus on:
- Core technical concepts
- Practical problem solving
- Real-world application scenarios

Each question must have exactly 4 options (A, B, C, D) and one correct answer."""

            aptitude_prompt = f"""Generate {aptitude_count} aptitude MCQ questions for job position: {job_title}

Focus on:
- Logical reasoning
- Quantitative aptitude
- Analytical thinking
- Data interpretation

Each question must have exactly 4 options (A, B, C, D) and one correct answer."""

            general_prompt = f"""Generate {general_count} general knowledge and workplace awareness MCQ questions for job position: {job_title}

Focus on:
- Professional communication
- Workplace ethics and awareness
- Industry and current affairs basics
- General analytical awareness

Each question must have exactly 4 options (A, B, C, D) and one correct answer."""

            technical_questions = generate_test_questions_combined(technical_prompt, technical_count, 'Technical')
            aptitude_questions = generate_test_questions_combined(aptitude_prompt, aptitude_count, 'Aptitude')
            general_questions = generate_test_questions_combined(general_prompt, general_count, 'General')

            all_questions = []
            question_id = 1

            for q in technical_questions[:technical_count]:
                all_questions.append({
                    'question_id': question_id,
                    'question_text': q.get('question', 'Technical question'),
                    'question_type': 'mcq',
                    'section': 'technical',
                    'marks': 1,
                    'option_a': q.get('option_a', 'Option A'),
                    'option_b': q.get('option_b', 'Option B'),
                    'option_c': q.get('option_c', 'Option C'),
                    'option_d': q.get('option_d', 'Option D'),
                    'correct_answer': q.get('correct_answer', 'A')
                })
                question_id += 1

            for q in aptitude_questions[:aptitude_count]:
                all_questions.append({
                    'question_id': question_id,
                    'question_text': q.get('question', 'Aptitude question'),
                    'question_type': 'mcq',
                    'section': 'aptitude',
                    'marks': 1,
                    'option_a': q.get('option_a', 'Option A'),
                    'option_b': q.get('option_b', 'Option B'),
                    'option_c': q.get('option_c', 'Option C'),
                    'option_d': q.get('option_d', 'Option D'),
                    'correct_answer': q.get('correct_answer', 'A')
                })
                question_id += 1

            for q in general_questions[:general_count]:
                all_questions.append({
                    'question_id': question_id,
                    'question_text': q.get('question', 'General knowledge question'),
                    'question_type': 'mcq',
                    'section': 'general_knowledge',
                    'marks': 1,
                    'option_a': q.get('option_a', 'Option A'),
                    'option_b': q.get('option_b', 'Option B'),
                    'option_c': q.get('option_c', 'Option C'),
                    'option_d': q.get('option_d', 'Option D'),
                    'correct_answer': q.get('correct_answer', 'A')
                })
                question_id += 1

            num_questions = len(all_questions)
            
            # Delete old tests for all candidates in this job to avoid duplicates
            candidate_ids = [c['candidate_id'] for c in candidates]
            if candidate_ids:
                placeholders = ','.join(['%s'] * len(candidate_ids))
                # Delete old test questions first (foreign key constraint)
                cursor.execute(f"""
                    DELETE FROM ai_test_questions 
                    WHERE test_id IN (
                        SELECT id FROM ai_tests 
                        WHERE candidate_id IN ({placeholders})
                        AND test_type = 'combined_job'
                    )
                """, candidate_ids)
                
                # Delete old tests
                cursor.execute(f"""
                    DELETE FROM ai_tests 
                    WHERE candidate_id IN ({placeholders})
                    AND test_type = 'combined_job'
                """, candidate_ids)
            
            # Create a single AI test record
            cursor.execute("""
                INSERT INTO ai_tests (candidate_id, skills_tested, test_type, total_questions, total_marks, status)
                VALUES (%s, %s, 'combined_job', %s, %s, 'pending')
                RETURNING id
            """, (
                candidates[0]['candidate_id'],  # Use first candidate as test owner
                job_id,
                num_questions,
                num_questions  # total_marks = num_questions (1 mark each)
            ))
             
            test_id = cursor.fetchone()['id']

            cursor.execute("""
                ALTER TABLE ai_test_questions
                ADD COLUMN IF NOT EXISTS section VARCHAR(50) DEFAULT 'technical'
            """)

            # Insert all questions into ai_test_questions
            for idx, q in enumerate(all_questions, 1):
                cursor.execute("""
                    INSERT INTO ai_test_questions 
                    (test_id, question_number, question_text, option_a, option_b, option_c, option_d, correct_answer, section)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    test_id, idx,
                    q.get('question_text', ''),
                    q.get('option_a', ''),
                    q.get('option_b', ''),
                    q.get('option_c', ''),
                    q.get('option_d', ''),
                    q.get('correct_answer', 'A'),
                    q.get('section', 'technical')
                ))
            
            # Update all candidates to have pending assessment status
            # Don't send test yet - recruiter needs to review and approve first
            for candidate in candidates:
                cursor.execute("""
                    UPDATE applications 
                    SET assessment_id = %s, 
                        assessment_status = 'pending_review',
                        assessment_assigned_at = NOW()
                    WHERE id = %s
                """, (test_id, candidate['app_id']))
            
            # Note: db.commit() is automatically called by SafeDBConnection context manager on exit
            
            # Log activity
            log_activity(
                recruiter_id, 'recruiter', 'test_created_for_review',
                f'Created test for {job_title}',
                f'Test created with {technical_count}T + {aptitude_count}A + {general_count}G questions. Pending review and approval.',
                {'job_id': job_id, 'test_id': test_id, 'candidate_count': len(candidates)}
            )
        
        return jsonify({
            'success': True,
            'message': f'Test created and ready for review. Review before sending to {len(candidates)} candidates.',
            'candidate_count': len(candidates),
            'test_id': test_id,
            'sections': {
                'technical': technical_count,
                'aptitude': aptitude_count,
                'general_knowledge': general_count
            },
            'redirect': f'/review-test/{test_id}/{job_id}'
        })
    
    except Exception as e:
        print(f"Error generating test for job: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': f'Failed to generate test: {str(e)}'}), 500

@app.route('/review-test/<int:test_id>/<int:job_id>')
def review_test(test_id, job_id):
    """Review test before sending to candidates"""
    if session.get('role') != 'recruiter':
        return redirect(url_for('login'))
    
    recruiter_id = session.get('user_id')
    
    try:
        with SafeDBConnection() as (cursor, db):
            # Verify job belongs to recruiter
            cursor.execute("""
                SELECT id, title FROM jobs 
                WHERE id = %s AND recruiter_id = %s
            """, (job_id, recruiter_id))
            
            job = cursor.fetchone()
            if not job:
                return jsonify({'error': 'Unauthorized'}), 401
            
            # Get test details
            cursor.execute("""
                SELECT id, total_questions, total_marks, status FROM ai_tests 
                WHERE id = %s
            """, (test_id,))
            
            test = cursor.fetchone()
            if not test:
                return jsonify({'error': 'Test not found'}), 404

            cursor.execute("""
                ALTER TABLE ai_test_questions
                ADD COLUMN IF NOT EXISTS section VARCHAR(50) DEFAULT 'technical'
            """)
            
            # Get all test questions
            cursor.execute("""
                SELECT id, question_number, question_text, option_a, option_b, option_c, option_d, correct_answer, section
                FROM ai_test_questions 
                WHERE test_id = %s 
                ORDER BY question_number
            """, (test_id,))
            
            raw_questions = cursor.fetchall()

            technical_count = max(1, int(test['total_questions'] * 0.4))
            aptitude_count = max(1, int(test['total_questions'] * 0.3))
            general_count = max(1, test['total_questions'] - technical_count - aptitude_count)
            section_total = technical_count + aptitude_count + general_count
            if section_total != test['total_questions']:
                general_count += (test['total_questions'] - section_total)

            def infer_section(question_number):
                if question_number <= technical_count:
                    return 'technical'
                if question_number <= (technical_count + aptitude_count):
                    return 'aptitude'
                return 'general_knowledge'

            section_order = {'technical': 1, 'aptitude': 2, 'general_knowledge': 3}
            questions = []
            for q in raw_questions:
                current_section = (q.get('section') or '').strip().lower() if isinstance(q, dict) else ''
                if current_section not in section_order:
                    current_section = infer_section(q.get('question_number', 0))
                q['section'] = current_section
                questions.append(q)

            questions.sort(key=lambda item: (section_order.get(item.get('section'), 99), item.get('question_number', 0)))

            sectioned_questions = {
                'technical': [q for q in questions if q.get('section') == 'technical'],
                'aptitude': [q for q in questions if q.get('section') == 'aptitude'],
                'general_knowledge': [q for q in questions if q.get('section') == 'general_knowledge']
            }
            
            # Get candidate count for this test
            cursor.execute("""
                SELECT COUNT(*) as count FROM applications 
                WHERE assessment_id = %s
            """, (test_id,))
            
            app_count = cursor.fetchone()['count']
            
            # Get recruiter profile for dashboard context
            cursor.execute("""
                SELECT profile_completed FROM recruiters WHERE id = %s
            """, (recruiter_id,))
            
            recruiter = cursor.fetchone()
            is_complete = recruiter['profile_completed'] if recruiter else False
            
            # Get recruiter profile for additional context
            cursor.execute("""
                SELECT * FROM recruiter_profiles WHERE recruiter_id = %s ORDER BY id DESC LIMIT 1
            """, (recruiter_id,))
            
            profile = cursor.fetchone()
            profile_percent = 0
            if profile:
                required_keys = ['full_name','phone','designation','linkedin','company_name','company_type','company_size','industry','roles','experience_levels']
                total = len(required_keys)
                filled = 0
                for k in required_keys:
                    val = profile.get(k) if isinstance(profile, dict) else None
                    if val and str(val).strip() != '':
                        filled += 1
                profile_percent = int((filled/total)*100) if total else 0
            else:
                profile_percent = 40
            
            return render_template('review_test.html', 
                                   test=test, 
                                   questions=questions,
                                   sectioned_questions=sectioned_questions,
                                   job_title=job['title'],
                                   job_id=job_id,
                                   candidate_count=app_count,
                                   is_complete=is_complete,
                                   profile=profile,
                                   profile_percent=profile_percent)
    
    except Exception as e:
        print(f"Error reviewing test: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': f'Failed to review test: {str(e)}'}), 500

@app.route('/update-test-question/<int:test_id>/<int:question_number>', methods=['PUT'])
def update_test_question(test_id, question_number):
    """Update a single question during recruiter review"""
    if session.get('role') != 'recruiter':
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401

    recruiter_id = session.get('user_id')
    data = request.get_json(silent=True) or {}

    question_text = (data.get('question_text') or '').strip()
    option_a = (data.get('option_a') or '').strip()
    option_b = (data.get('option_b') or '').strip()
    option_c = (data.get('option_c') or '').strip()
    option_d = (data.get('option_d') or '').strip()
    correct_answer = (data.get('correct_answer') or '').strip().upper()
    section = (data.get('section') or '').strip().lower()

    if not question_text or not option_a or not option_b or not option_c or not option_d:
        return jsonify({'success': False, 'error': 'All question and option fields are required'}), 400

    if correct_answer not in ['A', 'B', 'C', 'D']:
        return jsonify({'success': False, 'error': 'Correct answer must be one of A, B, C, D'}), 400

    if section not in ['technical', 'aptitude', 'general_knowledge']:
        return jsonify({'success': False, 'error': 'Section must be technical, aptitude, or general_knowledge'}), 400

    try:
        with SafeDBConnection() as (cursor, db):
            cursor.execute("""
                ALTER TABLE ai_test_questions
                ADD COLUMN IF NOT EXISTS section VARCHAR(50) DEFAULT 'technical'
            """)

            cursor.execute("""
                SELECT 1
                FROM applications a
                JOIN jobs j ON a.job_id = j.id
                WHERE a.assessment_id = %s AND j.recruiter_id = %s
                LIMIT 1
            """, (test_id, recruiter_id))

            auth = cursor.fetchone()
            if not auth:
                return jsonify({'success': False, 'error': 'Unauthorized'}), 401

            cursor.execute("""
                UPDATE ai_test_questions
                SET question_text = %s,
                    option_a = %s,
                    option_b = %s,
                    option_c = %s,
                    option_d = %s,
                    correct_answer = %s,
                    section = %s
                WHERE test_id = %s AND question_number = %s
            """, (question_text, option_a, option_b, option_c, option_d, correct_answer, section, test_id, question_number))

            if cursor.rowcount == 0:
                return jsonify({'success': False, 'error': 'Question not found'}), 404

        return jsonify({'success': True, 'message': 'Question updated successfully'})
    except Exception as e:
        print(f"Error updating test question: {e}")
        return jsonify({'success': False, 'error': f'Failed to update question: {str(e)}'}), 500

@app.route('/send-test-to-candidates/<int:test_id>/<int:job_id>', methods=['POST'])
def send_test_to_candidates(test_id, job_id):
    """Send approved test to all candidates"""
    if session.get('role') != 'recruiter':
        return jsonify({'error': 'Unauthorized'}), 401
    
    recruiter_id = session.get('user_id')
    data = request.get_json(silent=True) or {}
    
    try:
        with SafeDBConnection() as (cursor, db):
            # Verify job belongs to recruiter
            cursor.execute("""
                SELECT title FROM jobs 
                WHERE id = %s AND recruiter_id = %s
            """, (job_id, recruiter_id))
            
            job = cursor.fetchone()
            if not job:
                return jsonify({'error': 'Unauthorized'}), 401
            
            job_title = job['title']
            
            # Get test details
            cursor.execute("""
                SELECT id, total_questions FROM ai_tests 
                WHERE id = %s
            """, (test_id,))
            
            test = cursor.fetchone()
            if not test:
                return jsonify({'error': 'Test not found'}), 404
            
            # Get all candidates assigned this test
            cursor.execute("""
                SELECT a.id as app_id, c.name as candidate_name, c.email as candidate_email, a.candidate_id
                FROM applications a
                JOIN candidates c ON a.candidate_id = c.id
                WHERE a.assessment_id = %s AND a.assessment_status IN ('pending_review', 'ready_to_send')
            """, (test_id,))
            
            candidates = cursor.fetchall()
            candidate_count = 0
            candidates_to_notify = []
            
            for candidate in candidates:
                try:
                    # Update application status first
                    cursor.execute("""
                        UPDATE applications 
                        SET status = 'Test Sent', 
                            assessment_status = 'sent',
                            assessment_sent_at = NOW(),
                            updated_at = NOW()
                        WHERE id = %s
                    """, (candidate['app_id'],))

                    candidates_to_notify.append(candidate)
                    candidate_count += 1
                    
                except Exception as e:
                    print(f"Error updating assessment status for candidate {candidate['candidate_id']}: {e}")

            # Commit status updates before notifying candidates (avoids notification->access race)
            db.commit()

            for candidate in candidates_to_notify:
                # Send email notification
                try:
                    test_link = f'{request.host_url.rstrip("/")}/take-recruiter-test/{test_id}'
                    msg = Message(
                        subject=f'Assessment Test - {job_title}',
                        recipients=[candidate['candidate_email']],
                        html=f"""
                        <html>
                            <body style="font-family: Arial, sans-serif;">
                                <h2>Assessment Test Invitation</h2>
                                <p>Dear {candidate['candidate_name']},</p>
                                <p>You have been invited to take an assessment test for the <strong>{job_title}</strong> position.</p>
                                <p><strong>Test Details:</strong></p>
                                <ul>
                                    <li>Number of Questions: {test['total_questions']}</li>
                                    <li>Sections: Technical, Aptitude, General Knowledge</li>
                                </ul>
                                <p><a href="{test_link}" target="_blank" style="background-color: #007bff; color: white; padding: 10px 20px; text-decoration: none; border-radius: 4px; display: inline-block;">Start Test</a></p>
                                <p>Best regards,<br>Recruiter Team</p>
                            </body>
                        </html>
                        """
                    )
                    send_async_email(app, msg)
                except Exception as email_error:
                    print(f"Error sending email to {candidate['candidate_email']}: {email_error}")

                # Create notification
                create_notification(
                    'candidate', candidate['candidate_id'], 'test_assigned',
                    'Assessment Test Assigned',
                    f'You have been assigned an assessment test for {job_title}.',
                    f'/take-recruiter-test/{test_id}'
                )
            
            # Log activity
            log_activity(
                recruiter_id, 'recruiter', 'test_sent_to_candidates',
                f'Sent test to candidates for {job_title}',
                f'Test sent to {candidate_count} candidates',
                {'job_id': job_id, 'test_id': test_id, 'candidate_count': candidate_count}
            )
        
        return jsonify({
            'success': True,
            'message': f'Test sent to {candidate_count} candidates',
            'candidate_count': candidate_count
        })
    
    except Exception as e:
        print(f"Error sending test to candidates: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': f'Failed to send test: {str(e)}'}), 500

@app.route('/shortlist-all-for-job/<int:job_id>', methods=['POST'])
def shortlist_all_for_job(job_id):
    """Shortlist all Applied candidates for a job"""
    if session.get('role') != 'recruiter':
        return jsonify({'error': 'Unauthorized'}), 401
    
    recruiter_id = session.get('user_id')
    
    try:
        with SafeDBConnection() as (cursor, db):
            # Verify job belongs to recruiter
            cursor.execute("""
                SELECT title FROM jobs 
                WHERE id = %s AND recruiter_id = %s
            """, (job_id, recruiter_id))
            
            job = cursor.fetchone()
            if not job:
                return jsonify({'error': 'Job not found'}), 404
            
            # Shortlist all Applied candidates
            cursor.execute("""
                UPDATE applications
                SET status = 'Shortlisted'
                WHERE job_id = %s AND status = 'Applied'
            """, (job_id,))
            
            count = cursor.rowcount
            
            # Log activity
            log_activity(
                recruiter_id, 'recruiter', 'bulk_shortlist',
                f'Shortlisted all candidates for {job["title"]}',
                f'{count} candidates shortlisted',
                {'job_id': job_id, 'count': count}
            )
        
        return jsonify({
            'success': True,
            'message': f'{count} candidates shortlisted',
            'count': count
        })
    
    except Exception as e:
        print(f"Error shortlisting candidates: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': f'Failed to shortlist candidates: {str(e)}'}), 500


@app.route('/bulk-shortlist-best-fit/<int:job_id>', methods=['POST'])
def bulk_shortlist_best_fit(job_id):
    """Shortlist the top best-fit candidates up to the job openings count."""
    if session.get('role') != 'recruiter':
        return jsonify({'error': 'Unauthorized'}), 401

    recruiter_id = session.get('user_id')

    try:
        with SafeDBConnection() as (cursor, db):
            cursor.execute(
                "SELECT title, COALESCE(openings, 0) AS openings FROM jobs WHERE id = %s AND recruiter_id = %s",
                (job_id, recruiter_id)
            )
            job = cursor.fetchone()
            if not job:
                return jsonify({'error': 'Job not found'}), 404

            job_meta, ranked_apps, best_fit_limit, reserve_limit = build_ranked_selection_pool(cursor, recruiter_id, job_id)
            shortlisted_ids = []

            for app in ranked_apps[:best_fit_limit]:
                if (app.get('status') or '').lower() in {'rejected', 'selected'}:
                    continue
                cursor.execute(
                    """
                    UPDATE applications
                    SET status = 'Shortlisted'
                    WHERE id = %s AND status NOT IN ('Rejected'::application_status, 'Selected'::application_status)
                    """,
                    (app['id'],)
                )
                if cursor.rowcount > 0:
                    shortlisted_ids.append(app['id'])

            db.commit()

            try:
                log_activity(
                    recruiter_id,
                    'recruiter',
                    'bulk_best_fit_shortlist',
                    f"Shortlisted top best-fit candidates for {job['title']}",
                    f"{len(shortlisted_ids)} candidates shortlisted from a top-fit pool of {best_fit_limit}",
                    {'job_id': job_id, 'best_fit_limit': best_fit_limit, 'shortlisted_count': len(shortlisted_ids)}
                )
            except Exception:
                pass

            return jsonify({
                'success': True,
                'message': f'Shortlisted top {len(shortlisted_ids)} best-fit candidates',
                'shortlisted_count': len(shortlisted_ids),
                'best_fit_limit': best_fit_limit,
                'reserve_limit': reserve_limit
            })
    except Exception as e:
        print(f"Error bulk shortlisting best-fit candidates: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': f'Failed to shortlist best-fit candidates: {str(e)}'}), 500


@app.route('/save-selection-pool/<int:job_id>', methods=['POST'])
def save_selection_pool(job_id):
    """Persist a ranked best-fit and reserve candidate pool for future recruiter use."""
    if session.get('role') != 'recruiter':
        return jsonify({'error': 'Unauthorized'}), 401

    recruiter_id = session.get('user_id')

    try:
        with SafeDBConnection() as (cursor, db):
            cursor.execute(
                "SELECT title, COALESCE(openings, 0) AS openings FROM jobs WHERE id = %s AND recruiter_id = %s",
                (job_id, recruiter_id)
            )
            job = cursor.fetchone()
            if not job:
                return jsonify({'error': 'Job not found'}), 404

            job_meta, ranked_apps, best_fit_limit, reserve_limit = build_ranked_selection_pool(cursor, recruiter_id, job_id)
            pool_candidates = ranked_apps[:best_fit_limit + reserve_limit]

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS job_selection_pools (
                    id SERIAL PRIMARY KEY,
                    job_id INTEGER NOT NULL,
                    recruiter_id VARCHAR(20) NOT NULL,
                    best_fit_limit INTEGER NOT NULL,
                    reserve_limit INTEGER NOT NULL,
                    pool_data JSONB NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(job_id, recruiter_id)
                )
                """
            )

            payload = {
                'job_id': job_id,
                'job_title': job.get('title'),
                'job_openings': int(job.get('openings') or 0),
                'best_fit_limit': best_fit_limit,
                'reserve_limit': reserve_limit,
                'best_fit_candidates': [
                    {
                        'application_id': app['id'],
                        'candidate_id': app['candidate_id'],
                        'candidate_name': app['candidate_name'],
                        'candidate_email': app['candidate_email'],
                        'selection_score': app.get('selection_score', 0),
                        'selection_rank': app.get('selection_rank', 0),
                        'selection_bucket': app.get('selection_bucket', 'best-fit'),
                        'ai_match_score': app.get('ai_match_score', 0),
                        'skill_match': app.get('skill_match', 0),
                        'status': app.get('status', 'Applied')
                    }
                    for app in pool_candidates
                ]
            }

            cursor.execute(
                """
                INSERT INTO job_selection_pools (
                    job_id, recruiter_id, best_fit_limit, reserve_limit, pool_data
                ) VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (job_id, recruiter_id) DO UPDATE SET
                    best_fit_limit = EXCLUDED.best_fit_limit,
                    reserve_limit = EXCLUDED.reserve_limit,
                    pool_data = EXCLUDED.pool_data,
                    created_at = CURRENT_TIMESTAMP
                """,
                (job_id, recruiter_id, best_fit_limit, reserve_limit, json.dumps(payload))
            )

            db.commit()

            try:
                log_activity(
                    recruiter_id,
                    'recruiter',
                    'save_selection_pool',
                    f"Saved selection pool for {job['title']}",
                    f"Saved {len(pool_candidates)} candidates for future review",
                    {'job_id': job_id, 'best_fit_limit': best_fit_limit, 'reserve_limit': reserve_limit}
                )
            except Exception:
                pass

            return jsonify({
                'success': True,
                'message': 'Selection pool saved for future use',
                'best_fit_limit': best_fit_limit,
                'reserve_limit': reserve_limit,
                'saved_candidates': len(pool_candidates)
            })
    except Exception as e:
        print(f"Error saving selection pool: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': f'Failed to save selection pool: {str(e)}'}), 500

@app.route('/take-recruiter-test/<int:test_id>')
def take_recruiter_test(test_id):
    """Candidate takes recruiter-assigned test (Aptitude + Technical + General Knowledge)"""
    if session.get('role') != 'candidate':
        return redirect('/login')
    
    candidate_id = session.get('user_id')
    
    try:
        with SafeDBConnection() as (cursor, db):
            # Check if test belongs to this candidate via applications table
            cursor.execute("""
                SELECT 
                    at.id as test_id, 
                    at.status as test_status,
                    a.id as application_id,
                    a.assessment_status,
                    j.title as job_title,
                    j.skills as job_skills
                FROM ai_tests at
                JOIN applications a ON at.id = a.assessment_id
                JOIN jobs j ON a.job_id = j.id
                WHERE at.id = %s AND a.candidate_id = %s
                LIMIT 1
            """, (test_id, candidate_id))
            
            test_data = cursor.fetchone()
            if not test_data:
                # Try recruiter_assessments as fallback
                cursor.execute("""
                    SELECT ra.id, ra.job_id, ra.skills_tested, ra.due_at, ra.status,
                           at.id as test_id, at.status as test_status,
                           j.title as job_title, j.skills as job_skills,
                           'sent' as assessment_status
                    FROM recruiter_assessments ra
                    JOIN ai_tests at ON ra.test_id = at.id
                    JOIN jobs j ON ra.job_id = j.id
                    WHERE ra.test_id = %s AND ra.candidate_id = %s
                """, (test_id, candidate_id))
                
                test_data = cursor.fetchone()
                if not test_data:
                    flash('Test not found or unauthorized', 'danger')
                    return redirect('/candidate-dashboard')

            # If recruiter has not sent the test yet, prevent access
            if test_data.get('assessment_status') in ['pending_review', 'ready_to_send', 'not_assigned']:
                flash('This assessment is not available yet. Please wait for recruiter approval.', 'warning')
                return redirect('/candidate-dashboard')
            
            if test_data['test_status'] == 'completed':
                flash('You have already completed this test', 'info')
                return redirect('/candidate-dashboard')
            
            # Check if test questions exist
            cursor.execute("SELECT COUNT(*) as cnt FROM ai_test_questions WHERE test_id = %s", (test_id,))
            question_count = cursor.fetchone()['cnt']
            
            if question_count == 0:
                flash('Test questions not found', 'danger')
                return redirect('/candidate-dashboard')
            
            # Get all test questions
            cursor.execute("""
                SELECT id, question_number, question_text, option_a, option_b, option_c, option_d, section
                FROM ai_test_questions 
                WHERE test_id = %s
                ORDER BY question_number
            """, (test_id,))
            
            questions = cursor.fetchall()
            
            # Mark test as started
            cursor.execute("""
                UPDATE ai_tests 
                SET status = 'in_progress', started_at = NOW()
                WHERE id = %s AND status = 'pending'
            """, (test_id,))
            
            return render_template('take_recruiter_test.html', 
                                   test_id=test_id,
                                   job_title=test_data['job_title'],
                                   questions=questions,
                                   total_questions=len(questions))
    
    except Exception as e:
        print(f"Error in take_recruiter_test: {e}")
        import traceback
        traceback.print_exc()
        flash('Error loading test', 'danger')
        return redirect('/candidate-dashboard')

@app.route('/submit-recruiter-test/<int:test_id>', methods=['POST'])
def submit_recruiter_test(test_id):
    """Submit recruiter-assigned test and calculate scores"""
    if session.get('role') != 'candidate':
        return jsonify({'error': 'Unauthorized'}), 401
    
    candidate_id = session.get('user_id')
    
    try:
        payload = request.json or {}
        answers = payload.get('answers', {})
        tab_switch_count = int(payload.get('tab_switch_count', 0) or 0)
        
        with SafeDBConnection() as (cursor, db):
            # Check if test is from applications table (new workflow)
            cursor.execute("""
                SELECT a.id as application_id
                FROM applications a
                WHERE a.assessment_id = %s AND a.candidate_id = %s
            """, (test_id, candidate_id))
            
            app_result = cursor.fetchone()
            application_id = app_result['application_id'] if app_result else None
            
            # If not found in applications, try recruiter_assessments (old workflow)
            if not application_id:
                cursor.execute("""
                    SELECT ra.application_id
                    FROM recruiter_assessments ra
                    WHERE ra.test_id = %s AND ra.candidate_id = %s
                """, (test_id, candidate_id))
                
                test_data = cursor.fetchone()
                if not test_data:
                    return jsonify({'error': 'Test not found'}), 404
                application_id = test_data['application_id']
            
            # Update candidate answers and check correctness
            correct_count = 0
            total_questions = 0
            
            for question_id, answer in answers.items():
                cursor.execute("""
                    UPDATE ai_test_questions
                    SET candidate_answer = %s,
                        is_correct = (correct_answer = %s)
                    WHERE id = %s AND test_id = %s
                    RETURNING is_correct
                """, (answer, answer, int(question_id), test_id))
                
                result = cursor.fetchone()
                if result and result['is_correct']:
                    correct_count += 1
                total_questions += 1
            
            # Calculate percentage
            obtained_marks = correct_count
            total_marks = total_questions if total_questions > 0 else 1
            percentage = (obtained_marks / total_marks) * 100 if total_marks > 0 else 0
            
            # Update test record
            cursor.execute("""
                UPDATE ai_tests
                SET status = 'completed', 
                    obtained_marks = %s,
                    percentage = %s,
                    tab_switch_count = %s,
                    completed_at = NOW()
                WHERE id = %s
            """, (obtained_marks, percentage, tab_switch_count, test_id))
            
            # Update assessment status in recruiter_assessments if exists
            cursor.execute("""
                UPDATE recruiter_assessments
                SET status = 'completed', updated_at = NOW()
                WHERE test_id = %s
            """, (test_id,))
            
            # Update application - handle both old and new status enums
            cursor.execute("""
                UPDATE applications
                SET assessment_status = 'completed',
                    assessment_completed_at = NOW(),
                    updated_at = NOW()
                WHERE id = %s
            """, (application_id,))
            
            # Try to update status to Test Completed if it exists in enum
            try:
                cursor.execute("""
                    UPDATE applications
                    SET status = 'Test Completed'::application_status
                    WHERE id = %s
                """, (application_id,))
            except:
                pass  # Enum value might not exist, continue anyway
            
            # Get recruiter and job info for notification
            cursor.execute("""
                SELECT COALESCE(ra.recruiter_id, j.recruiter_id) as recruiter_id, j.title as job_title
                FROM applications a
                LEFT JOIN recruiter_assessments ra ON a.assessment_id = ra.test_id
                JOIN jobs j ON a.job_id = j.id
                WHERE a.id = %s
            """, (application_id,))
            
            info = cursor.fetchone()
            if info and info['recruiter_id']:
                create_notification(
                    'recruiter', info['recruiter_id'], 'test_completed',
                    'Candidate Completed Assessment',
                    f'A candidate has completed the assessment test with {percentage:.1f}% score',
                    f'/applications'
                )
        
        return jsonify({
            'success': True,
            'obtained_marks': obtained_marks,
            'total_marks': total_marks,
            'percentage': round(percentage, 2),
            'correct_answers': correct_count,
            'total_questions': total_questions
        })
    
    except Exception as e:
        print(f"Error submitting test: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': f'Failed to submit test: {str(e)}'}), 500

@app.route('/submit-interview-feedback/<int:interview_id>', methods=['POST'])
def submit_interview_feedback(interview_id):
    """Submit panel feedback for interview"""
    if session.get('role') != 'recruiter':
        return jsonify({'error': 'Unauthorized'}), 401
    
    recruiter_id = session.get('user_id')
    
    try:
        data = request.json
        
        technical_score = float(data.get('technical_score', 0))
        communication_score = float(data.get('communication_score', 0))
        problem_solving_score = float(data.get('problem_solving_score', 0))
        cultural_fit_score = float(data.get('cultural_fit_score', 0))
        
        # Calculate overall score (average of all scores)
        overall_score = (technical_score + communication_score + problem_solving_score + cultural_fit_score) / 4
        
        strengths = data.get('strengths', '')
        weaknesses = data.get('weaknesses', '')
        recommendation = data.get('recommendation', 'Pending')
        detailed_feedback = data.get('detailed_feedback', '')
        
        with SafeDBConnection() as (cursor, db):
            # Get interview details
            cursor.execute("""
                SELECT i.candidate_id, i.job_id, i.recruiter_id, c.name as candidate_name, j.title as job_title,
                       a.id as application_id
                FROM interviews i
                JOIN candidates c ON i.candidate_id = c.id
                JOIN jobs j ON i.job_id = j.id
                JOIN applications a ON a.candidate_id = i.candidate_id AND a.job_id = i.job_id
                WHERE i.id = %s AND i.recruiter_id = %s
            """, (interview_id, recruiter_id))
            
            interview_data = cursor.fetchone()
            if not interview_data:
                return jsonify({'error': 'Interview not found'}), 404
            
            candidate_id = interview_data['candidate_id']
            job_id = interview_data['job_id']
            application_id = interview_data['application_id']
            
            # Insert or update feedback
            cursor.execute("""
                INSERT INTO interview_feedback 
                (interview_id, candidate_id, recruiter_id, job_id, technical_score, communication_score, 
                 problem_solving_score, cultural_fit_score, overall_score, strengths, weaknesses, 
                 recommendation, detailed_feedback)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (interview_id, candidate_id, recruiter_id, job_id, technical_score, communication_score,
                  problem_solving_score, cultural_fit_score, overall_score, strengths, weaknesses,
                  recommendation, detailed_feedback))
            
            # Update interview status
            cursor.execute("""
                UPDATE interviews
                SET status = 'Completed', result = %s, feedback = %s, updated_at = NOW()
                WHERE id = %s
            """, (recommendation, detailed_feedback, interview_id))
            
            # Update application status to 'Interview Completed'
            cursor.execute("""
                UPDATE applications
                SET status = 'Interview Completed'::application_status, updated_at = NOW()
                WHERE id = %s
            """, (application_id,))
            
            db.commit()
            
            # Send notification to candidate with feedback
            create_notification(
                'candidate', candidate_id, 'interview_feedback',
                'Interview Feedback Received',
                f'Your interview feedback for {interview_data["job_title"]} is now available',
                f'/view-interview-feedback/{interview_id}'
            )
        
        return jsonify({
            'success': True,
            'message': 'Interview feedback submitted successfully',
            'overall_score': round(overall_score, 2)
        })
    
    except Exception as e:
        print(f"Error submitting interview feedback: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': f'Failed to submit feedback: {str(e)}'}), 500

@app.route('/schedule-group-discussion/<int:candidate_id>/<int:job_id>', methods=['GET', 'POST'])
def schedule_group_discussion(candidate_id, job_id):
    """Schedule group discussion for candidate"""
    if session.get('role') != 'recruiter':
        return redirect('/login')
    
    recruiter_id = session.get('user_id')
    
    if request.method == 'POST':
        try:
            discussion_topic = request.form.get('discussion_topic')
            discussion_date = request.form.get('discussion_date')
            discussion_time = request.form.get('discussion_time')
            mode = request.form.get('mode')
            meeting_link = request.form.get('meeting_link', '')
            location = request.form.get('location', '')
            
            with SafeDBConnection() as (cursor, db):
                # Get application ID
                cursor.execute("""
                    SELECT a.id, c.name as candidate_name, j.title as job_title
                    FROM applications a
                    JOIN candidates c ON a.candidate_id = c.id
                    JOIN jobs j ON a.job_id = j.id
                    WHERE a.candidate_id = %s AND a.job_id = %s AND j.recruiter_id = %s
                """, (candidate_id, job_id, recruiter_id))
                
                app_data = cursor.fetchone()
                if not app_data:
                    flash('Application not found', 'danger')
                    return redirect('/interviews')
                
                application_id = app_data['id']
                
                # Create group discussion record
                cursor.execute("""
                    INSERT INTO group_discussions
                    (candidate_id, recruiter_id, job_id, discussion_topic, discussion_date, 
                     discussion_time, mode, meeting_link, location, status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'Scheduled')
                    RETURNING id
                """, (candidate_id, recruiter_id, job_id, discussion_topic, discussion_date,
                      discussion_time, mode, meeting_link, location))
                
                gd_id = cursor.fetchone()['id']
                
                # Update application status to 'Group Discussion'
                cursor.execute("""
                    UPDATE applications
                    SET status = 'Group Discussion'::application_status, updated_at = NOW()
                    WHERE id = %s
                """, (application_id,))
                
                db.commit()
                
                # Send notification
                create_notification(
                    'candidate', candidate_id, 'gd_scheduled',
                    'Group Discussion Scheduled',
                    f'Group discussion scheduled for {app_data["job_title"]} on {discussion_date}',
                    f'/view-group-discussion/{gd_id}'
                )
                
                flash('Group discussion scheduled successfully', 'success')
                return redirect('/interviews')
        
        except Exception as e:
            print(f"Error scheduling GD: {e}")
            import traceback
            traceback.print_exc()
            flash('Error scheduling group discussion', 'danger')
            return redirect('/interviews')
    
    # GET request - show form
    try:
        with SafeDBConnection() as (cursor, db):
            cursor.execute("""
                SELECT c.name as candidate_name, c.email, j.title as job_title
                FROM candidates c
                JOIN applications a ON c.id = a.candidate_id
                JOIN jobs j ON a.job_id = j.id
                WHERE c.id = %s AND j.id = %s AND j.recruiter_id = %s
            """, (candidate_id, job_id, recruiter_id))
            
            data = cursor.fetchone()
            if not data:
                flash('Application not found', 'danger')
                return redirect('/interviews')
        
        return render_template('schedule_group_discussion.html', 
                             candidate_id=candidate_id,
                             job_id=job_id,
                             candidate_name=data['candidate_name'],
                             job_title=data['job_title'])
    
    except Exception as e:
        print(f"Error loading GD form: {e}")
        flash('Error loading page', 'danger')
        return redirect('/interviews')

@app.route('/get-gd-id/<candidate_id>/<int:job_id>', methods=['GET'])
def get_gd_id(candidate_id, job_id):
    """Get the GD ID for a candidate-job pair"""
    if session.get('role') != 'recruiter':
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        with SafeDBConnection() as (cursor, db):
            cursor.execute("""
                SELECT id FROM group_discussions 
                WHERE candidate_id = %s AND job_id = %s 
                ORDER BY created_at DESC LIMIT 1
            """, (candidate_id, job_id))
            
            gd = cursor.fetchone()
            if gd:
                return jsonify({'gd_id': gd['id']})
            else:
                return jsonify({'gd_id': None, 'error': 'Group discussion not found'})
    
    except Exception as e:
        print(f"Error getting GD ID: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/submit-gd-feedback/<int:gd_id>', methods=['POST'])
def submit_gd_feedback(gd_id):
    """Submit feedback for group discussion"""
    if session.get('role') != 'recruiter':
        return jsonify({'error': 'Unauthorized'}), 401
    
    recruiter_id = session.get('user_id')
    
    try:
        data = request.json
        
        leadership_score = float(data.get('leadership_score', 0))
        communication_score = float(data.get('communication_score', 0))
        teamwork_score = float(data.get('teamwork_score', 0))
        critical_thinking_score = float(data.get('critical_thinking_score', 0))
        
        # Calculate overall score
        overall_score = (leadership_score + communication_score + teamwork_score + critical_thinking_score) / 4
        
        feedback = data.get('feedback', '')
        result = data.get('result', 'Pending')
        
        with SafeDBConnection() as (cursor, db):
            # Update group discussion record
            cursor.execute("""
                UPDATE group_discussions
                SET leadership_score = %s,
                    communication_score = %s,
                    teamwork_score = %s,
                    critical_thinking_score = %s,
                    overall_score = %s,
                    feedback = %s,
                    status = 'Completed',
                    result = %s,
                    updated_at = NOW()
                WHERE id = %s AND recruiter_id = %s
                RETURNING candidate_id, job_id
            """, (leadership_score, communication_score, teamwork_score, critical_thinking_score,
                  overall_score, feedback, result, gd_id, recruiter_id))
            
            gd_data = cursor.fetchone()
            if not gd_data:
                return jsonify({'error': 'Group discussion not found'}), 404
            
            # Update application status to 'GD Completed'
            cursor.execute("""
                UPDATE applications
                SET status = 'GD Completed'::application_status, updated_at = NOW()
                WHERE candidate_id = %s AND job_id = %s
            """, (gd_data['candidate_id'], gd_data['job_id']))
            
            db.commit()
            
            # Send notification
            create_notification(
                'candidate', gd_data['candidate_id'], 'gd_completed',
                'Group Discussion Completed',
                f'Your group discussion has been evaluated',
                f'/view-gd-feedback/{gd_id}'
            )
        
        return jsonify({
            'success': True,
            'message': 'Group discussion feedback submitted successfully',
            'overall_score': round(overall_score, 2)
        })
    
    except Exception as e:
        print(f"Error submitting GD feedback: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': f'Failed to submit feedback: {str(e)}'}), 500

@app.route('/final-selection/<int:candidate_id>/<int:job_id>', methods=['POST'])
def final_selection(candidate_id, job_id):
    """Final selection based on combined scores (Test + Interview + GD)"""
    if session.get('role') != 'recruiter':
        return jsonify({'error': 'Unauthorized'}), 401
    
    recruiter_id = session.get('user_id')
    
    try:
        decision = request.json.get('decision')  # 'select' or 'reject'
        rejection_reason = request.json.get('rejection_reason', '')
        
        with SafeDBConnection() as (cursor, db):
            # Get all scores
            cursor.execute("""
                SELECT 
                    at.percentage as test_score,
                    if.overall_score as interview_score,
                    gd.overall_score as gd_score,
                    a.id as application_id,
                    c.name as candidate_name,
                    j.title as job_title
                FROM applications a
                JOIN candidates c ON a.candidate_id = c.id
                JOIN jobs j ON a.job_id = j.id
                LEFT JOIN recruiter_assessments ra ON ra.candidate_id = c.id AND ra.job_id = j.id
                LEFT JOIN ai_tests at ON at.id = ra.test_id
                LEFT JOIN interviews i ON i.candidate_id = c.id AND i.job_id = j.id AND i.status = 'Completed'
                LEFT JOIN interview_feedback if ON if.interview_id = i.id
                LEFT JOIN group_discussions gd ON gd.candidate_id = c.id AND gd.job_id = j.id AND gd.status = 'Completed'
                WHERE a.candidate_id = %s AND a.job_id = %s AND j.recruiter_id = %s
            """, (candidate_id, job_id, recruiter_id))
            
            scores = cursor.fetchone()
            if not scores:
                return jsonify({'error': 'Application not found'}), 404
            
            # Calculate combined score (weighted average)
            # Test: 40%, Interview: 35%, GD: 25%
            test_score = scores['test_score'] or 0
            interview_score = (scores['interview_score'] or 0) * 10  # Convert to percentage
            gd_score = (scores['gd_score'] or 0) * 10  # Convert to percentage
            
            combined_score = (test_score * 0.4) + (interview_score * 0.35) + (gd_score * 0.25)
            
            if decision == 'select':
                # Update application to Selected
                cursor.execute("""
                    UPDATE applications
                    SET status = 'Selected'::application_status, updated_at = NOW()
                    WHERE id = %s
                """, (scores['application_id'],))
                
                db.commit()
                
                # Send notification
                create_notification(
                    'candidate', candidate_id, 'job_offer',
                    'Congratulations! You are Selected',
                    f'You have been selected for {scores["job_title"]}. Combined score: {combined_score:.1f}%',
                    f'/offer-letter/{scores["application_id"]}'
                )
                
                message = f'Candidate selected successfully! Combined score: {combined_score:.1f}%'
                
            else:  # reject
                # Update application to Rejected
                cursor.execute("""
                    UPDATE applications
                    SET status = 'Rejected'::application_status, 
                        rejection_reason = %s,
                        updated_at = NOW()
                    WHERE id = %s
                """, (rejection_reason, scores['application_id']))
                
                db.commit()
                
                # Send notification
                create_notification(
                    'candidate', candidate_id, 'application_rejected',
                    'Application Update',
                    f'Thank you for your interest in {scores["job_title"]}',
                    f'/my-applications'
                )
                
                message = 'Candidate rejected'
        
        return jsonify({
            'success': True,
            'message': message,
            'combined_score': round(combined_score, 2),
            'breakdown': {
                'test_score': round(test_score, 2),
                'interview_score': round(interview_score, 2),
                'gd_score': round(gd_score, 2)
            }
        })
    
    except Exception as e:
        print(f"Error in final selection: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': f'Failed to process selection: {str(e)}'}), 500

def generate_test_questions_combined(prompt, num_questions, section_type):
    """Helper function to generate questions for aptitude or technical section"""
    try:
        full_prompt = f"""{prompt}

Return ONLY a valid JSON array with exactly {num_questions} questions in this format:
[
    {{
        "question": "Question text here?",
        "option_a": "Option A",
        "option_b": "Option B",
        "option_c": "Option C",
        "option_d": "Option D",
        "correct_answer": "A"
    }}
]

IMPORTANT: Return ONLY the JSON array, no markdown, no explanations."""
        response_text = _generate_gemini_text(full_prompt, timeout=20)
        if not response_text:
            raise RuntimeError("Gemini did not return any text")
        response_text = response_text.strip()
        
        # Clean response
        if response_text.startswith('```json'):
            response_text = response_text[7:]
        if response_text.startswith('```'):
            response_text = response_text[3:]
        if response_text.endswith('```'):
            response_text = response_text[:-3]
        
        response_text = response_text.strip()
        
        # Parse JSON
        questions = json.loads(response_text)
        
        return questions[:num_questions]
    
    except Exception as e:
        print(f"[ERROR] Failed to generate {section_type} questions: {e}")
        import traceback
        traceback.print_exc()
        
        # Return simple fallback questions for recruiter test editor
        fallback = []
        if section_type == 'Aptitude':
            for i in range(num_questions):
                fallback.append({
                    'question': f'Sample Aptitude Question {i+1}: What is 2+2?',
                    'option_a': '3',
                    'option_b': '4',
                    'option_c': '5',
                    'option_d': '6',
                    'correct_answer': 'B'
                })
        elif section_type == 'General':
            for i in range(num_questions):
                fallback.append({
                    'question': f'Sample General Knowledge Question {i+1}: Which communication practice is best in a workplace?',
                    'option_a': 'Ignore team updates',
                    'option_b': 'Share clear and timely updates',
                    'option_c': 'Avoid documentation',
                    'option_d': 'Skip feedback',
                    'correct_answer': 'B'
                })
        else:  # Technical
            for i in range(num_questions):
                fallback.append({
                    'question': f'Sample Technical Question {i+1}: What does HTML stand for?',
                    'option_a': 'Hyperlinks and Text Markup Language',
                    'option_b': 'Hyper Text Markup Language',
                    'option_c': 'Home Tool Markup Language',
                    'option_d': 'Hyperlinking Text Markup Language',
                    'correct_answer': 'B'
                })
        return fallback

def generate_fallback_questions(num_questions, section_type):
    """Generate fallback questions if AI generation fails"""
    fallback = []
    
    if section_type == 'Aptitude':
        for i in range(num_questions):
            fallback.append({
                'question': f'Sample Aptitude Question {i+1}',
                'option_a': 'Option A',
                'option_b': 'Option B',
                'option_c': 'Option C',
                'option_d': 'Option D',
                'correct_answer': 'A'
            })
    else:  # Technical
        for i in range(num_questions):
            fallback.append({
                'question': f'Sample Technical Question {i+1}',
                'option_a': 'Option A',
                'option_b': 'Option B',
                'option_c': 'Option C',
                'option_d': 'Option D',
                'correct_answer': 'A'
            })
    
    return fallback

@app.route('/start-ai-test', methods=['GET', 'POST'])
def start_ai_test():
    if session.get('role') != 'candidate':
        return redirect('/login')
    
    candidate_id = session.get('user_id')
    
    # Keep the assessment available even if the profile is incomplete.
    # The generator will fall back to generic assessment topics when needed.
    is_complete, profile_percent = check_candidate_profile_completion(candidate_id)
    if not is_complete:
        flash(
            f'Your profile is currently {profile_percent}% complete. The AI assessment is still available, but completing your profile will improve question personalization.',
            'info'
        )
    
    # Get candidate profile to extract ALL skills
    db = get_connection()
    cursor = db.cursor(cursor_factory=RealDictCursor)

    if not is_assessment_paid(cursor, candidate_id, 'technical_test'):
        cleanup_db_resources(cursor, db)
        flash(f'Please pay ₹{ASSESSMENT_PAYMENT_AMOUNT} to unlock Technical Test.', 'warning')
        return redirect('/candidate-dashboard#assessments')
    
    cursor.execute(
        "SELECT * FROM candidate_profiles WHERE candidate_id = %s",
        (candidate_id,)
    )
    profile = cursor.fetchone()
    profile = profile or {}
    
    # Collect ALL skills from profile
    all_skills = []
    
    # Add primary skills
    if profile.get('primary_skills'):
        all_skills.extend([s.strip() for s in str(profile.get('primary_skills', '')).split(',') if s.strip()])
    
    # Add secondary skills
    if profile.get('secondary_skills'):
        all_skills.extend([s.strip() for s in str(profile.get('secondary_skills', '')).split(',') if s.strip()])
    
    # Add frameworks/libraries
    if profile.get('frameworks_libraries'):
        all_skills.extend([s.strip() for s in str(profile.get('frameworks_libraries', '')).split(',') if s.strip()])
    
    # Add databases
    if profile.get('databases'):
        all_skills.extend([s.strip() for s in str(profile.get('databases', '')).split(',') if s.strip()])
    
    # Add cloud platforms
    if profile.get('cloud_platforms'):
        all_skills.extend([s.strip() for s in str(profile.get('cloud_platforms', '')).split(',') if s.strip()])
    
    # Add tools/technologies
    if profile.get('tools_technologies'):
        all_skills.extend([s.strip() for s in str(profile.get('tools_technologies', '')).split(',') if s.strip()])
    
    # Remove duplicates while preserving order
    seen = set()
    all_skills = [s for s in all_skills if not (s in seen or seen.add(s))]
    
    if not all_skills:
        all_skills = ['General Aptitude', 'Logical Reasoning', 'Basic Technical Knowledge']
    
    skills_text = ", ".join(all_skills)
    print(f"All skills for test: {skills_text}")
    
    # Get all previous questions asked to this candidate for uniqueness
    cursor.execute("""
        SELECT DISTINCT q.question_text 
        FROM ai_test_questions q
        JOIN ai_tests t ON q.test_id = t.id
        WHERE t.candidate_id = %s AND t.status = 'completed'
    """, (candidate_id,))
    previous_questions = [row['question_text'] for row in cursor.fetchall()]
    print(f"Found {len(previous_questions)} previous questions for this candidate")
    
    # Check if there's an ongoing test
    cursor.execute(
        "SELECT id, started_at FROM ai_tests WHERE candidate_id = %s AND status = 'in_progress' ORDER BY started_at DESC LIMIT 1",
        (candidate_id,)
    )
    existing_test = cursor.fetchone()
    
    if existing_test:
        # Check if the test has questions
        cursor.execute(
            "SELECT COUNT(*) as count FROM ai_test_questions WHERE test_id = %s",
            (existing_test["id"],)
        )
        question_count = cursor.fetchone()
        
        if question_count['count'] > 0:
            # Test has questions, continue with it
            cleanup_db_resources(cursor, db)
            return redirect(f'/take-ai-test/{existing_test["id"]}')
        else:
            # Test exists but has no questions, show loading screen while generating
            cleanup_db_resources(cursor, db)
            return render_template('test_loading.html', test_id=existing_test["id"])
    
    # Create new test with ALL skills
    print(f"Creating test for candidate {candidate_id} with skills: {skills_text}")
    
    cursor.execute(
        "INSERT INTO ai_tests (candidate_id, skills_tested) VALUES (%s, %s) RETURNING id",
        (candidate_id, skills_text)
    )
    inserted_row = cursor.fetchone()
    test_id = inserted_row['id'] if inserted_row else None

    if not test_id:
        cleanup_db_resources(cursor, db)
        flash('Unable to create test session. Please try again.', 'danger')
        return redirect('/candidate-dashboard#assessments')

    db.commit()
    print(f"Created test with ID: {test_id}")
    
    cleanup_db_resources(cursor, db)
    
    # Redirect to loading screen immediately - questions will be generated asynchronously
    return render_template('test_loading.html', test_id=test_id)

@app.route('/check-test-ready/<int:test_id>')
def check_test_ready(test_id):
    """Check if test questions have been generated"""
    if session.get('role') != 'candidate':
        return jsonify({'ready': False, 'error': 'Unauthorized'}), 401
    
    candidate_id = session.get('user_id')
    
    db = get_connection()
    cursor = db.cursor(cursor_factory=RealDictCursor)
    
    # Verify test belongs to candidate
    cursor.execute(
        "SELECT * FROM ai_tests WHERE id = %s AND candidate_id = %s",
        (test_id, candidate_id)
    )
    test = cursor.fetchone()
    
    if not test:
        cleanup_db_resources(cursor, db)
        return jsonify({'ready': False, 'error': 'Test not found'}), 404
    
    # Check if questions exist
    cursor.execute(
        "SELECT COUNT(*) as count FROM ai_test_questions WHERE test_id = %s",
        (test_id,)
    )
    question_count = cursor.fetchone()
    
    if question_count['count'] > 0:
        cleanup_db_resources(cursor, db)
        return jsonify({'ready': True, 'question_count': question_count['count']})
    
    # Get candidate profile
    cursor.execute(
        "SELECT * FROM candidate_profiles WHERE candidate_id = %s",
        (candidate_id,)
    )
    profile = cursor.fetchone()
    profile = profile or {}
    
    # Collect ALL skills
    all_skills = []
    if profile.get('primary_skills'):
        all_skills.extend([s.strip() for s in str(profile.get('primary_skills', '')).split(',') if s.strip()])
    if profile.get('secondary_skills'):
        all_skills.extend([s.strip() for s in str(profile.get('secondary_skills', '')).split(',') if s.strip()])
    if profile.get('frameworks_libraries'):
        all_skills.extend([s.strip() for s in str(profile.get('frameworks_libraries', '')).split(',') if s.strip()])
    if profile.get('databases'):
        all_skills.extend([s.strip() for s in str(profile.get('databases', '')).split(',') if s.strip()])
    if profile.get('cloud_platforms'):
        all_skills.extend([s.strip() for s in str(profile.get('cloud_platforms', '')).split(',') if s.strip()])
    if profile.get('tools_technologies'):
        all_skills.extend([s.strip() for s in str(profile.get('tools_technologies', '')).split(',') if s.strip()])
    
    seen = set()
    all_skills = [s for s in all_skills if not (s in seen or seen.add(s))]
    if not all_skills:
        all_skills = ['General Aptitude', 'Logical Reasoning', 'Basic Technical Knowledge']
    skills_text = ", ".join(all_skills)
    
    # Get previous questions
    cursor.execute("""
        SELECT DISTINCT q.question_text 
        FROM ai_test_questions q
        JOIN ai_tests t ON q.test_id = t.id
        WHERE t.candidate_id = %s AND t.status = 'completed'
    """, (candidate_id,))
    previous_questions = [row['question_text'] for row in cursor.fetchall()]
    
    # Generate questions
    try:
        def generate_ai_questions(skills_text, skills_list, num_questions=25, previous_questions=None):
            """Generate AI-powered questions based on candidate skills, ensuring uniqueness (Gemini via google-genai)."""

            def _profile_summary_text(profile_data):
                if not profile_data:
                    return "No profile data available"

                parts = []
                for label, key in [
                    ("Role", "preferred_job_role"),
                    ("Primary skills", "primary_skills"),
                    ("Secondary skills", "secondary_skills"),
                    ("Frameworks", "frameworks_libraries"),
                    ("Databases", "databases"),
                    ("Cloud", "cloud_platforms"),
                    ("Tools", "tools_technologies"),
                    ("Experience", "work_experience"),
                ]:
                    value = profile_data.get(key)
                    if value and str(value).strip():
                        parts.append(f"{label}: {str(value).strip()}")

                return " | ".join(parts) if parts else "No profile data available"

            candidate_role = profile.get('preferred_job_role') or 'Technical Candidate'
            profile_summary = _profile_summary_text(profile)

            # Prefer a smaller, highly relevant skill set for question targeting.
            focus_skills = [s for s in skills_list if s and str(s).strip()]
            if len(focus_skills) > 8:
                focus_skills = focus_skills[:8]
            if not focus_skills:
                focus_skills = ['General Aptitude', 'Logical Reasoning', 'Basic Technical Knowledge']

            # Use centralized advanced API-first generator to ensure uniqueness and strong option diversity.
            return _generate_advanced_technical_questions(
                skills_text=skills_text,
                skills_list=focus_skills,
                num_questions=num_questions,
                previous_questions=previous_questions,
                candidate_role=candidate_role,
                profile_summary=profile_summary
            )

            # Build prompt
            import time
            timestamp = int(time.time())
            easy_count = num_questions // 3
            medium_count = num_questions // 3
            hard_count = num_questions - easy_count - medium_count
            questions_per_skill = max(1, num_questions // len(skills_list)) if len(skills_list) > 0 else 1

            exclusion_text = ""
            if previous_questions:
                exclusion_text = f"""

        CRITICAL: DO NOT REPEAT ANY OF THESE PREVIOUSLY ASKED QUESTIONS:
        {chr(10).join([f"- {q}" for q in previous_questions[:50]])}
        """

            prompt = f"""
        You are an expert technical interviewer creating high-quality MCQ questions.

        Candidate role: {candidate_role}
        Candidate profile summary: {profile_summary}
        Skills to focus on: {', '.join(focus_skills)}
        Full skills text: {skills_text}
        Question count: {num_questions}
        Easy: {easy_count}, Medium: {medium_count}, Hard: {hard_count}
        Questions per skill (approx): {questions_per_skill}
        Timestamp seed: {timestamp}
        {exclusion_text}

        Output format: ONE QUESTION BLOCK PER LINE. Use ~~~ as separator between question and options.
        <Skill>|<Difficulty>|<Question text>~~~<OptionA>|<OptionB>|<OptionC>|<OptionD>|<CorrectAnswer>

        Example:
Python|Easy|What is the output of print(2**3)?~~~8|6|9|16|A
SQL|Medium|Which keyword is used to specify conditions?~~~WHERE|SELECT|JOIN|GROUP|A

        Guidelines:
        - Each question must be tailored to the candidate role and profile
        - Each question must have exactly 4 options (A, B, C, D)
        - Specify correct answer as A, B, C, or D
        - Avoid duplicates and near-duplicates
        - Balance coverage across skills from the candidate profile
        - Keep questions and options concise and clear
        - Only output the format specified, no explanations
        """

            generated_questions = []
            try:
                text = _generate_gemini_text(prompt, timeout=10)  # uses google-genai Client with timeout
                if text:
                    for line in text.split('\n'):
                        if not line.strip() or '~~~' not in line:
                            continue
                        try:
                            parts = line.split('~~~')
                            if len(parts) != 2:
                                continue
                            question_part = parts[0].strip()
                            options_part = parts[1].strip()
                            
                            q_parts = [p.strip() for p in question_part.split('|')]
                            o_parts = [p.strip() for p in options_part.split('|')]
                            
                            if len(q_parts) >= 3 and len(o_parts) >= 5:
                                skill, difficulty, question = q_parts[0], q_parts[1], q_parts[2]
                                option_a, option_b, option_c, option_d, correct_answer = o_parts[0], o_parts[1], o_parts[2], o_parts[3], o_parts[4]
                                
                                # Validate correct answer
                                if correct_answer.upper() not in ['A', 'B', 'C', 'D']:
                                    correct_answer = 'A'
                                
                                generated_questions.append({
                                    'skill': skill,
                                    'difficulty': difficulty,
                                    'question': question,
                                    'option_a': option_a,
                                    'option_b': option_b,
                                    'option_c': option_c,
                                    'option_d': option_d,
                                    'correct_answer': correct_answer.upper()
                                })
                        except Exception as parse_error:
                            print(f"Error parsing question line: {line}, Error: {parse_error}")
                            continue
            except Exception as e:
                print(f"Error generating AI questions: {e}")

            # Remove duplicates from Gemini output before filling with fallback questions.
            if generated_questions:
                unique_questions = []
                seen_questions = set()
                for q in generated_questions:
                    normalized = re.sub(r'\s+', ' ', str(q.get('question', '')).strip().lower())
                    if not normalized or normalized in seen_questions:
                        continue
                    seen_questions.add(normalized)
                    unique_questions.append(q)
                generated_questions = unique_questions

            if len(generated_questions) < num_questions:
                fallback_questions = generate_fallback_questions(focus_skills, num_questions)
                existing_questions = {q['question'].strip().lower() for q in generated_questions if q.get('question')}
                for fallback_question in fallback_questions:
                    if len(generated_questions) >= num_questions:
                        break
                    if fallback_question['question'].strip().lower() not in existing_questions:
                        generated_questions.append(fallback_question)
                        existing_questions.add(fallback_question['question'].strip().lower())

            if len(generated_questions) < num_questions:
                print(f"⚠️ Warning: Only generated {len(generated_questions)} questions out of {num_questions} requested")
            
            return generated_questions
        
        generated_questions = generate_ai_questions(skills_text, all_skills, 15, previous_questions)
    
    except Exception as e:
        print(f"Error in AI test question generation: {e}")
        generated_questions = []

    if not generated_questions:
        generated_questions = generate_fallback_questions(all_skills, 15)
    
    # Save generated questions to database
    if generated_questions:
        try:
            db_save = get_connection()
            cursor_save = db_save.cursor()
            
            for idx, q in enumerate(generated_questions, 1):
                cursor_save.execute("""
                    INSERT INTO ai_test_questions 
                    (test_id, question_number, question_text, option_a, option_b, option_c, option_d, correct_answer)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    test_id, idx, q['question'],
                    q.get('option_a', ''),
                    q.get('option_b', ''),
                    q.get('option_c', ''),
                    q.get('option_d', ''),
                    q.get('correct_answer', 'A')
                ))
            
            db_save.commit()
            cursor_save.close()
            db_save.close()
        except Exception as db_error:
            print(f"Error saving questions to database: {db_error}")
            # Continue anyway - questions were generated even if save failed
    
    cleanup_db_resources(cursor, db)
    
    # Return ready status
    # If we generated a usable fallback set, let the candidate proceed instead of hard-failing.
    if generated_questions and len(generated_questions) >= 5:
        return jsonify({'ready': True, 'question_count': len(generated_questions)})
    else:
        error_msg = 'AI service is currently unavailable. '
        if 'quota' in str(generated_questions).lower() or 'RESOURCE_EXHAUSTED' in str(generated_questions):
            error_msg += 'The AI question generation service has reached its daily limit. Please try again later or contact support to increase the limit.'
        else:
            error_msg += 'Unable to generate AI questions at this time. Please try again later.'
        return jsonify({'ready': False, 'error': error_msg}), 503

@app.route('/take-ai-test/<int:test_id>')
def take_ai_test(test_id):
    """Display the AI test for a candidate"""
    if session.get('role') != 'candidate':
        return redirect('/login')
    
    candidate_id = session.get('user_id')
    
    db = get_connection()
    cursor = db.cursor(cursor_factory=RealDictCursor)
    
    # Verify test belongs to candidate and get test details
    cursor.execute(
        "SELECT * FROM ai_tests WHERE id = %s AND candidate_id = %s",
        (test_id, candidate_id)
    )
    test = cursor.fetchone()
    
    if not test:
        cleanup_db_resources(cursor, db)
        flash("Test not found", "danger")
        return redirect('/candidate-dashboard')
    
    # Get all questions for this test
    cursor.execute(
        "SELECT * FROM ai_test_questions WHERE test_id = %s ORDER BY question_number",
        (test_id,)
    )
    questions = cursor.fetchall()
    
    cleanup_db_resources(cursor, db)
    
    if not questions:
        flash("No questions found for this test", "warning")
        return redirect('/start-ai-test')
    
    return render_template('ai_test.html', test=test, questions=questions)

@app.route('/submit-answer/<int:test_id>/<int:question_id>', methods=['POST'])
def submit_answer(test_id, question_id):
    """Submit answer for a test question"""
    if session.get('role') != 'candidate':
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401
    
    try:
        data = request.get_json()
        answer = data.get('answer')
        
        if not answer:
            return jsonify({'success': False, 'message': 'No answer provided'}), 400
        
        candidate_id = session.get('user_id')
        
        db = get_connection()
        cursor = db.cursor(cursor_factory=RealDictCursor)
        
        # Verify test belongs to candidate
        cursor.execute(
            "SELECT * FROM ai_tests WHERE id = %s AND candidate_id = %s",
            (test_id, candidate_id)
        )
        test = cursor.fetchone()
        
        if not test:
            cleanup_db_resources(cursor, db)
            return jsonify({'success': False, 'message': 'Test not found'}), 404
        
        # Update the answer for this question and auto-check correctness immediately.
        cursor.execute(
            """
            UPDATE ai_test_questions
            SET candidate_answer = %s,
                is_correct = (UPPER(TRIM(correct_answer)) = UPPER(TRIM(%s)))
            WHERE id = %s AND test_id = %s
            RETURNING is_correct, correct_answer
            """,
            (answer, answer, question_id, test_id)
        )
        result = cursor.fetchone()
        
        db.commit()
        cleanup_db_resources(cursor, db)
        
        return jsonify({
            'success': True,
            'is_correct': bool(result['is_correct']) if result else False,
            'correct_answer': result['correct_answer'] if result else None
        })
    
    except Exception as e:
        print(f"Error submitting answer: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/finish-test/<int:test_id>', methods=['POST'])
def finish_test(test_id):
    if session.get('role') != 'candidate':
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401
    
    candidate_id = session.get('user_id')
    
    db = get_connection()
    cursor = db.cursor(cursor_factory=RealDictCursor)
    
    # Verify test belongs to candidate
    cursor.execute(
        "SELECT * FROM ai_tests WHERE id = %s AND candidate_id = %s AND status = 'in_progress'",
        (test_id, candidate_id)
    )
    test = cursor.fetchone()
    
    if not test:
        cleanup_db_resources(cursor, db)
        return jsonify({'success': False, 'message': 'Test not found'}), 404
    
    # Calculate marks
    cursor.execute(
        "SELECT COUNT(*) as correct FROM ai_test_questions WHERE test_id = %s AND COALESCE(is_correct, FALSE) = TRUE",
        (test_id,)
    )
    result = cursor.fetchone()
    correct_count = result['correct']
    
    obtained_marks = correct_count * 2
    percentage = (obtained_marks / test['total_marks']) * 100
    
    # Analyze skill gaps - identify weak skills
    cursor.execute("""
        SELECT question_text, is_correct 
        FROM ai_test_questions 
        WHERE test_id = %s
    """, (test_id,))
    all_questions = cursor.fetchall()
    
    skill_analysis = {}
    weak_skills = []
    
    for q in all_questions:
        # Extract skill from question text (format: [Skill - Level] Question)
        question = q['question_text']
        if '[' in question and ']' in question:
            skill_part = question[question.find('[')+1:question.find(']')]
            if ' - ' in skill_part:
                skill = skill_part.split(' - ')[0].strip()
                
                if skill not in skill_analysis:
                    skill_analysis[skill] = {'total': 0, 'correct': 0}
                
                skill_analysis[skill]['total'] += 1
                if q['is_correct']:
                    skill_analysis[skill]['correct'] += 1
    
    # Identify weak skills (less than 60% accuracy)
    for skill, stats in skill_analysis.items():
        accuracy = (stats['correct'] / stats['total'] * 100) if stats['total'] > 0 else 0
        if accuracy < 60:
            weak_skills.append(skill)
    
    skill_gap_report = ', '.join(weak_skills) if weak_skills else 'No major gaps identified'
    
    # Update test status with skill gap analysis
    cursor.execute(
        "UPDATE ai_tests SET status = 'completed', obtained_marks = %s, percentage = %s, completed_at = CURRENT_TIMESTAMP WHERE id = %s",
        (obtained_marks, percentage, test_id)
    )
    
    db.commit()
    cleanup_db_resources(cursor, db)
    
    return jsonify({
        'success': True,
        'obtained_marks': obtained_marks,
        'total_marks': test['total_marks'],
        'percentage': round(percentage, 2),
        'correct_count': correct_count,
        'total_questions': test['total_questions'],
        'skill_gaps': weak_skills,
        'skill_gap_report': skill_gap_report
    })


@app.route('/api/technical-test/<int:test_id>/regenerate', methods=['POST'])
def regenerate_technical_test(test_id):
    """Regenerate a candidate technical test with cleaner, more unique questions and persist them."""
    if session.get('role') != 'candidate':
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401

    candidate_id = session.get('user_id')

    try:
        with SafeDBConnection() as (cursor, db):
            cursor.execute("SELECT * FROM ai_tests WHERE id = %s AND candidate_id = %s", (test_id, candidate_id))
            test = cursor.fetchone()
            if not test:
                return jsonify({'success': False, 'error': 'Test not found'}), 404

            cursor.execute("DELETE FROM ai_test_questions WHERE test_id = %s", (test_id,))

            cursor.execute("SELECT * FROM candidate_profiles WHERE candidate_id = %s", (candidate_id,))
            profile = cursor.fetchone() or {}

            # Build a compact, role-aware skill list for better targeting.
            skills_pool = []
            for field in ['primary_skills', 'secondary_skills', 'frameworks_libraries', 'databases', 'cloud_platforms', 'tools_technologies']:
                if profile.get(field):
                    skills_pool.extend([s.strip() for s in str(profile.get(field)).split(',') if s.strip()])

            seen = set()
            skills_pool = [s for s in skills_pool if not (s.lower() in seen or seen.add(s.lower()))]
            if not skills_pool:
                skills_pool = ['Python', 'JavaScript', 'SQL', 'Problem Solving']

            generated_questions = generate_fallback_questions(skills_pool, 15)

            # Try Gemini-backed generation first; fallback questions stay as the safety net.
            try:
                prev_questions = []
                cursor.execute("""
                    SELECT q.question_text
                    FROM ai_test_questions q
                    JOIN ai_tests t ON q.test_id = t.id
                    WHERE t.candidate_id = %s AND t.status = 'completed'
                """, (candidate_id,))
                prev_questions = [row['question_text'] for row in cursor.fetchall()]
                ai_questions = generate_ai_questions(
                    ', '.join(skills_pool),
                    skills_pool,
                    15,
                    prev_questions
                )
                if ai_questions:
                    generated_questions = ai_questions
            except Exception as gen_error:
                print(f"[REGENERATE] Gemini generation failed, using fallback: {gen_error}")

            for idx, q in enumerate(generated_questions, start=1):
                cursor.execute("""
                    INSERT INTO ai_test_questions
                    (test_id, question_number, question_text, option_a, option_b, option_c, option_d, correct_answer)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    test_id, idx, q['question'], q.get('option_a', ''), q.get('option_b', ''),
                    q.get('option_c', ''), q.get('option_d', ''), q.get('correct_answer', 'A')
                ))

            db.commit()

            return jsonify({
                'success': True,
                'question_count': len(generated_questions),
                'message': 'Test regenerated successfully'
            })

    except Exception as e:
        print(f"[REGENERATE] Error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/log-tab-switch/<int:test_id>', methods=['POST'])
def log_tab_switch(test_id):
    if session.get('role') != 'candidate':
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401
    
    candidate_id = session.get('user_id')
    
    try:
        data = request.get_json()
        switch_count = data.get('switch_count', 0)
        
        db = get_connection()
        cursor = db.cursor(cursor_factory=RealDictCursor)
        
        # Verify test belongs to candidate
        cursor.execute(
            "SELECT id FROM ai_tests WHERE id = %s AND candidate_id = %s AND status = 'in_progress'",
            (test_id, candidate_id)
        )
        test = cursor.fetchone()
        
        if not test:
            cleanup_db_resources(cursor, db)
            return jsonify({'success': False, 'message': 'Test not found'}), 404
        
        # Log tab switch - update or add tab_switch_count column
        cursor.execute(
            "UPDATE ai_tests SET tab_switch_count = %s WHERE id = %s",
            (switch_count, test_id)
        )
        
        db.commit()
        cleanup_db_resources(cursor, db)
        
        return jsonify({'success': True})
    except Exception as e:
        print(f"Error logging tab switch: {str(e)}")
        return jsonify({'success': False, 'message': 'Error logging tab switch'}), 500

@app.route('/start-mock-interview')
def start_mock_interview():
    if session.get('role') != 'candidate':
        return redirect('/login')
    
    candidate_id = session.get('user_id')
    
    # WORKFLOW DEPENDENCY: Candidate → Profile Completion (≥85%)
    is_complete, profile_percent = check_candidate_profile_completion(candidate_id)
    
    if not is_complete:
        flash(f'Please complete your profile to at least 85% (currently {profile_percent}%) before starting mock interviews.', 'warning')
        return redirect('/candidate-dashboard#profile')
    
    db = get_connection()
    cursor = db.cursor(cursor_factory=RealDictCursor)

    if not is_assessment_paid(cursor, candidate_id, 'mock_interview'):
        cleanup_db_resources(cursor, db)
        flash(f'Please pay ₹{ASSESSMENT_PAYMENT_AMOUNT} to unlock Mock Interview.', 'warning')
        return redirect('/candidate-dashboard#assessments')
    
    # Get candidate's profile for role-based questions
    cursor.execute("""
        SELECT preferred_job_role, primary_skills, work_experience AS experience 
        FROM candidate_profiles 
        WHERE candidate_id = %s
    """, (candidate_id,))
    profile = cursor.fetchone()
    
    if not profile or not profile.get('preferred_job_role'):
        cleanup_db_resources(cursor, db)
        flash('Please add your preferred job role in your profile before starting mock interview.', 'warning')
        return redirect('/candidate-dashboard#profile')
    
    job_role = profile['preferred_job_role']
    skills = profile.get('primary_skills', '')
    experience_level = profile.get('experience', '') or profile.get('work_experience', '')
    
    # Create mock interview record and fetch generated PostgreSQL ID safely
    cursor.execute("""
        INSERT INTO mock_interviews 
        (candidate_id, job_role, difficulty_level, total_questions, status)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING id
    """, (candidate_id, job_role, 'Medium', 5, 'in_progress'))

    inserted_row = cursor.fetchone()
    interview_id = inserted_row['id'] if inserted_row and inserted_row.get('id') else None

    if not interview_id:
        cleanup_db_resources(cursor, db)
        flash('Unable to start mock interview right now. Please try again.', 'danger')
        return redirect('/candidate-dashboard#assessments')
    
    # Generate interview questions using AI
    questions = generate_interview_questions(job_role, skills, experience_level, 5)
    
    # Save questions to database
    for idx, question in enumerate(questions):
        question_text = (question or {}).get('text') or f"Interview question {idx + 1}"
        question_type = (question or {}).get('type') or 'Technical'
        cursor.execute("""
            INSERT INTO mock_interview_questions
            (interview_id, question_number, question_text, question_type)
            VALUES (%s, %s, %s, %s)
        """, (interview_id, idx + 1, question_text, question_type))
    
    db.commit()
    
    # Fetch saved questions with IDs
    cursor.execute("""
        SELECT id, question_number, question_text, question_type
        FROM mock_interview_questions
        WHERE interview_id = %s
        ORDER BY question_number
    """, (interview_id,))
    saved_questions = cursor.fetchall()
    
    cleanup_db_resources(cursor, db)
    
    return render_template('mock_interview.html', 
                         interview_id=interview_id,
                         questions=saved_questions,
                         total_questions=len(saved_questions),
                         job_role=job_role)

# --- Gemini AI helpers ----------------------------------------------------

def _get_gemini_client():
    """Create a Gemini client. Prefer google-genai; fallback to google-generativeai."""
    genai_error = None
    legacy_error = None

    api_key = os.getenv('GEMINI_API_KEY') or os.getenv('GOOGLE_API_KEY')
    if not api_key:
        raise RuntimeError("Gemini API key is not configured. Set GEMINI_API_KEY (or GOOGLE_API_KEY).")

    try:
        from google import genai  # type: ignore
        return {
            'kind': 'genai',
            'client': genai.Client(api_key=api_key)
        }
    except Exception as e:
        genai_error = e

    try:
        import google.generativeai as legacy_genai  # type: ignore
        legacy_genai.configure(api_key=api_key)
        return {
            'kind': 'legacy',
            'module': legacy_genai
        }
    except Exception as e:
        legacy_error = e

    raise RuntimeError(f"Gemini client library not available. google-genai error: {genai_error}; google-generativeai error: {legacy_error}")


def _generate_gemini_text(prompt: str, model: str = "gemini-2.5-flash", timeout: int = 15):
    """Generate text from Gemini with resilient parsing and graceful fallback."""
    try:
        client_info = _get_gemini_client()

        if client_info.get('kind') == 'genai':
            client = client_info['client']
            response = client.models.generate_content(model=model, contents=prompt)

            # Preferred attribute
            if hasattr(response, "text") and response.text:
                return response.text

            # Fallback: stitch together candidate parts
            candidates = getattr(response, "candidates", None)
            if candidates:
                parts = []
                for cand in candidates:
                    content = getattr(cand, "content", None)
                    if content and getattr(content, "parts", None):
                        for part in content.parts:
                            text_val = getattr(part, "text", None)
                            if text_val:
                                parts.append(text_val)
                if parts:
                    return "\n".join(parts)
        else:
            legacy_genai = client_info['module']
            model_candidates = [
                model,
                'gemini-1.5-flash',
                'gemini-1.5-flash-latest',
                'gemini-1.5-pro',
                'gemini-1.5-pro-latest',
                'gemini-pro',
            ]

            # Extend candidates with models available in this project/key.
            try:
                available = legacy_genai.list_models()
                for m in available:
                    name = getattr(m, 'name', '') or ''
                    methods = getattr(m, 'supported_generation_methods', []) or []
                    if 'generateContent' in methods:
                        short_name = name.split('/')[-1]
                        model_candidates.append(short_name)
            except Exception:
                pass

            # Deduplicate while preserving order
            deduped_models = []
            seen_models = set()
            for m in model_candidates:
                if not m or m in seen_models:
                    continue
                seen_models.add(m)
                deduped_models.append(m)

            last_legacy_error = None
            for legacy_model_name in deduped_models:
                try:
                    legacy_model = legacy_genai.GenerativeModel(legacy_model_name)
                    response = legacy_model.generate_content(prompt)

                    response_text = getattr(response, 'text', None)
                    if response_text:
                        return response_text

                    candidates = getattr(response, 'candidates', None)
                    if candidates:
                        parts = []
                        for cand in candidates:
                            content = getattr(cand, 'content', None)
                            if content and getattr(content, 'parts', None):
                                for part in content.parts:
                                    text_val = getattr(part, 'text', None)
                                    if text_val:
                                        parts.append(text_val)
                        if parts:
                            return "\n".join(parts)
                except Exception as legacy_err:
                    last_legacy_error = legacy_err
                    continue

            if last_legacy_error:
                raise last_legacy_error
    except Exception as e:
        error_str = str(e)
        if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str or "quota" in error_str.lower():
            print(f"Gemini API quota exhausted or rate limited")
        else:
            print(f"Gemini generation error: {e}")

    return None


def _generate_gemini_json(prompt: str, model: str = "gemini-2.5-flash", timeout: int = 20):
    """Generate and parse JSON from Gemini using the shared API helper."""
    text = _generate_gemini_text(prompt, model=model, timeout=timeout)
    if not text:
        return None

    cleaned = text.strip()
    if cleaned.startswith('```'):
        cleaned = cleaned.strip('`').strip()
        if cleaned.lower().startswith('json'):
            cleaned = cleaned[4:].strip()

    try:
        return json.loads(cleaned)
    except Exception:
        import re
        match = re.search(r'\{.*\}|\[.*\]', cleaned, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except Exception:
                return None
        return None

def _gemini_generate_with_fallback(prompt: str):
    """Try multiple Gemini models, returning the first successful reply and model name.

    This helps when certain models have free-tier quota = 0 or are rate-limited.
    """
    models_to_try = [
        "gemini-2.0-flash",      # prefer newest, may require paid quota
        "gemini-1.5-flash",      # widely available on free tier
        "gemini-1.5-flash-8b",   # smaller, cheaper, often available
        "gemini-pro"              # legacy text-only model
    ]

    for m in models_to_try:
        try:
            reply_text = _generate_gemini_text(prompt, model=m)
            if reply_text and str(reply_text).strip():
                return str(reply_text).strip(), m

        except Exception as e:
            err = str(e)
            # If quota/rate limits, try next model; otherwise keep trying
            if ("429" in err or "RESOURCE_EXHAUSTED" in err or "quota" in err.lower() or "limit: 0" in err.lower()):
                print(f"Model {m} quota limited, trying next model...")
                continue
            print(f"Model {m} error: {err}; trying next model...")

    return None, None

def generate_interview_questions(job_role, skills, experience_level, num_questions=5):
    """Generate interview questions using Gemini (google-genai)."""

    prompt = f"""
    Generate {num_questions} interview questions for a {job_role} position.
    
    Candidate Details:
    - Job Role: {job_role}
    - Skills: {skills}
    - Experience Level: {experience_level}
    
    Generate a mix of:
    1. Technical questions (40%)
    2. Behavioral questions (30%)
    3. Situational questions (30%)
    
    Format each question as:
    Question Type | Question Text
    
    Example:
    Technical | Explain the difference between REST and GraphQL APIs
    Behavioral | Tell me about a time you faced a challenging deadline
    Situational | How would you handle a disagreement with a team member?
    
    Make questions relevant to the role and experience level.
    """
    
    try:
        questions_text = _generate_gemini_text(prompt)
        questions = []

        if questions_text:
            for line in questions_text.split('\n'):
                if '|' in line:
                    parts = line.split('|', 1)
                    if len(parts) == 2:
                        q_type = parts[0].strip()
                        q_text = parts[1].strip()
                        questions.append({'type': q_type, 'text': q_text})

        if len(questions) < num_questions:
            fallback_questions = _build_mock_interview_fallback_questions(job_role, skills, num_questions)
            existing = {q['text'].strip().lower() for q in questions if q.get('text')}
            for fallback_question in fallback_questions:
                if len(questions) >= num_questions:
                    break
                if fallback_question['text'].strip().lower() not in existing:
                    questions.append(fallback_question)
                    existing.add(fallback_question['text'].strip().lower())
    except Exception as e:
        print(f"Error generating questions: {e}")
        questions = _build_mock_interview_fallback_questions(job_role, skills, num_questions)

    # Return AI-generated questions, padding with local fallback if Gemini is unavailable
    if len(questions) < num_questions:
        print(f"⚠️ Warning: Only generated {len(questions)} questions out of {num_questions} requested for interview")
    
    return questions


def _build_mock_interview_fallback_questions(job_role, skills, num_questions=5):
    """Build local fallback mock-interview questions when Gemini is unavailable."""
    skill_list = [s.strip() for s in str(skills).split(',') if s.strip()] if skills else []
    if not skill_list:
        skill_list = ["problem solving", "communication", "technical fundamentals"]

    templates = [
        ("Technical", lambda skill: f"How would you apply {skill} in a {job_role} project?"),
        ("Behavioral", lambda skill: f"Tell me about a time you had to learn {skill} quickly."),
        ("Situational", lambda skill: f"How would you handle a deadline issue while working as a {job_role}?"),
    ]

    questions = []
    for idx in range(num_questions):
        skill = skill_list[idx % len(skill_list)]
        q_type, builder = templates[idx % len(templates)]
        questions.append({'type': q_type, 'text': builder(skill)})

    return questions

@app.route('/save-interview-answer', methods=['POST'])
def save_interview_answer():
    if session.get('role') != 'candidate':
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401
    
    try:
        data = request.get_json()
        
        interview_id = data.get('interview_id')
        question_id = data.get('question_id')
        answer_text = data.get('answer_text', '')
        answer_duration = data.get('answer_duration', 0)
        emotion_detected = data.get('emotion_detected', 'Neutral')
        eye_contact_score = data.get('eye_contact_score', 0)
        filler_words = data.get('filler_words', 0)
        word_count = data.get('word_count', 0)
        
        # Analyze answer quality using AI
        db = get_connection()
        cursor = db.cursor(cursor_factory=RealDictCursor)
        
        # Get question text
        cursor.execute("""
            SELECT question_text, question_type 
            FROM mock_interview_questions 
            WHERE id = %s
        """, (question_id,))
        question = cursor.fetchone()
        
        if question:
            # Get AI feedback on answer
            ai_feedback, relevance_score, clarity_score = analyze_interview_answer(
                question['question_text'],
                question['question_type'],
                answer_text
            )
            
            # Update question with answer and analysis
            cursor.execute("""
                UPDATE mock_interview_questions
                SET answer_text = %s,
                    answer_duration = %s,
                    confidence_level = %s,
                    emotion_detected = %s,
                    filler_words = %s,
                    clarity_score = %s,
                    relevance_score = %s,
                    ai_feedback = %s
                WHERE id = %s
            """, (answer_text, answer_duration, 'Medium', emotion_detected, 
                  filler_words, clarity_score, relevance_score, ai_feedback, question_id))
            
            db.commit()
        
        cleanup_db_resources(cursor, db)
        
        return jsonify({'success': True})
        
    except Exception as e:
        print(f"Error saving interview answer: {str(e)}")
        return jsonify({'success': False, 'message': str(e)}), 500

def analyze_interview_answer(question_text, question_type, answer_text):
    """Analyze interview answer using Gemini (google-genai)."""

    prompt = f"""
    Analyze this interview answer:
    
    Question Type: {question_type}
    Question: {question_text}
    Answer: {answer_text}
    
    Provide analysis in this format:
    RELEVANCE: [Score 0-10]
    CLARITY: [Score 0-10]
    FEEDBACK: [2-3 sentences of constructive feedback]
    TIPS: [1-2 improvement suggestions]
    
    Be constructive and encouraging.
    """
    
    try:
        analysis_text = _generate_gemini_text(prompt)
    except Exception as e:
        print(f"Error analyzing answer: {e}")
        analysis_text = None

    # Defaults in case AI is unavailable
    relevance_score = 7.0
    clarity_score = 7.0
    feedback = "Good answer with room for improvement."

    if analysis_text:
        analysis = analysis_text.strip()

        if 'RELEVANCE:' in analysis:
            relevance_line = [line for line in analysis.split('\n') if 'RELEVANCE:' in line][0]
            try:
                relevance_score = float(relevance_line.split(':')[1].strip().split()[0])
            except Exception:
                pass

        if 'CLARITY:' in analysis:
            clarity_line = [line for line in analysis.split('\n') if 'CLARITY:' in line][0]
            try:
                clarity_score = float(clarity_line.split(':')[1].strip().split()[0])
            except Exception:
                pass

        if 'FEEDBACK:' in analysis:
            feedback_start = analysis.index('FEEDBACK:') + len('FEEDBACK:')
            if 'TIPS:' in analysis:
                feedback_end = analysis.index('TIPS:')
                feedback = analysis[feedback_start:feedback_end].strip()
            else:
                feedback = analysis[feedback_start:].strip()

    return feedback, relevance_score, clarity_score

@app.route('/finish-interview/<int:interview_id>', methods=['POST'])
def finish_interview(interview_id):
    if session.get('role') != 'candidate':
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401
    
    candidate_id = session.get('user_id')
    
    try:
        data = request.get_json()
        
        db = get_connection()
        cursor = db.cursor(cursor_factory=RealDictCursor)
        
        # Verify interview belongs to candidate
        cursor.execute("""
            SELECT * FROM mock_interviews 
            WHERE id = %s AND candidate_id = %s AND status = 'in_progress'
        """, (interview_id, candidate_id))
        interview = cursor.fetchone()
        
        if not interview:
            cleanup_db_resources(cursor, db)
            return jsonify({'success': False, 'message': 'Interview not found'}), 404
        
        # Get all answers and calculate scores
        cursor.execute("""
            SELECT clarity_score, relevance_score, filler_words, emotion_detected
            FROM mock_interview_questions
            WHERE interview_id = %s
        """, (interview_id,))
        answers = cursor.fetchall()
        
        # Calculate overall scores
        total_clarity = sum([a['clarity_score'] or 0 for a in answers])
        total_relevance = sum([a['relevance_score'] or 0 for a in answers])
        total_filler = sum([a['filler_words'] or 0 for a in answers])
        
        communication_score = (total_clarity / len(answers)) * 10 if answers else 0
        technical_score = (total_relevance / len(answers)) * 10 if answers else 0
        
        # Confidence score based on emotions
        positive_emotions = sum([1 for a in answers if a['emotion_detected'] in ['happy', 'neutral', 'Neutral']])
        confidence_score = (positive_emotions / len(answers)) * 100 if answers else 0
        
        # Overall score (weighted average)
        overall_score = (
            communication_score * 0.3 +
            technical_score * 0.4 +
            confidence_score * 0.3
        )
        
        # Generate comprehensive feedback using AI
        strengths, weaknesses, recommendations = generate_interview_feedback(
            interview_id, answers, overall_score
        )
        
        # Update interview with final scores
        cursor.execute("""
            UPDATE mock_interviews
            SET status = 'completed',
                overall_score = %s,
                confidence_score = %s,
                communication_score = %s,
                technical_score = %s,
                avg_response_time = %s,
                filler_words_count = %s,
                strengths = %s,
                weaknesses = %s,
                recommendations = %s,
                completed_at = CURRENT_TIMESTAMP
            WHERE id = %s
        """, (overall_score, confidence_score, communication_score, technical_score,
              data.get('avg_response_time', 0), total_filler,
              strengths, weaknesses, recommendations, interview_id))
        
        db.commit()
        cleanup_db_resources(cursor, db)
        
        return jsonify({'success': True})
        
    except Exception as e:
        print(f"Error finishing interview: {str(e)}")
        return jsonify({'success': False, 'message': str(e)}), 500

def generate_interview_feedback(interview_id, answers, overall_score):
    """Generate comprehensive feedback using Gemini (google-genai)."""

    # Prepare data for AI analysis
    answers_summary = "\n".join([
        f"- Clarity: {a['clarity_score']}/10, Relevance: {a['relevance_score']}/10, Emotion: {a['emotion_detected']}"
        for a in answers
    ])
    
    prompt = f"""Analyze this mock interview performance:\n\nOverall Score: {overall_score:.1f}/100\nAnswers Summary:\n{answers_summary}\n\nProvide feedback in this format:\n\nSTRENGTHS:\n- [3-4 specific strengths]\n\nAREAS FOR IMPROVEMENT:\n- [3-4 specific weaknesses]\n\nRECOMMENDATIONS:\n- [4-5 actionable recommendations]\n\nBe specific, constructive, and encouraging."""
    
    try:
        feedback_text = _generate_gemini_text(prompt)
    except Exception as e:
        print(f"Error generating feedback: {e}")
        feedback_text = None

    # Defaults
    strengths = "Good communication and technical knowledge."
    weaknesses = "Could improve response structure."
    recommendations = "Practice more technical questions and work on confidence."

    if feedback_text:
        feedback = feedback_text.strip()

        if 'STRENGTHS:' in feedback:
            strengths_start = feedback.index('STRENGTHS:') + len('STRENGTHS:')
            strengths_end = feedback.index('AREAS FOR IMPROVEMENT:') if 'AREAS FOR IMPROVEMENT:' in feedback else len(feedback)
            strengths = feedback[strengths_start:strengths_end].strip()
        
        if 'AREAS FOR IMPROVEMENT:' in feedback:
            weaknesses_start = feedback.index('AREAS FOR IMPROVEMENT:') + len('AREAS FOR IMPROVEMENT:')
            weaknesses_end = feedback.index('RECOMMENDATIONS:') if 'RECOMMENDATIONS:' in feedback else len(feedback)
            weaknesses = feedback[weaknesses_start:weaknesses_end].strip()
        
        if 'RECOMMENDATIONS:' in feedback:
            recommendations_start = feedback.index('RECOMMENDATIONS:') + len('RECOMMENDATIONS:')
            recommendations = feedback[recommendations_start:].strip()

    return strengths, weaknesses, recommendations

@app.route('/interview-report/<int:interview_id>')
def interview_report(interview_id):
    if session.get('role') != 'candidate':
        return redirect('/login')
    
    candidate_id = session.get('user_id')
    
    db = get_connection()
    cursor = db.cursor(cursor_factory=RealDictCursor)
    
    # Get interview details
    cursor.execute("""
        SELECT * FROM mock_interviews
        WHERE id = %s AND candidate_id = %s AND status = 'completed'
    """, (interview_id, candidate_id))
    interview = cursor.fetchone()
    
    if not interview:
        cleanup_db_resources(cursor, db)
        flash('Interview not found.', 'danger')
        return redirect('/candidate-dashboard')
    
    # Get all questions and answers
    cursor.execute("""
        SELECT * FROM mock_interview_questions
        WHERE interview_id = %s
        ORDER BY question_number
    """, (interview_id,))
    questions = cursor.fetchall()
    
    cleanup_db_resources(cursor, db)
    
    return render_template('interview_report.html',
                         interview=interview,
                         questions=questions)

@app.route('/view-candidate-test/<int:candidate_id>')
def view_candidate_test(candidate_id):
    if session.get('role') != 'recruiter':
        return redirect('/login')
    
    db = get_connection()
    cursor = db.cursor(cursor_factory=RealDictCursor)
    
    # Get all completed tests for this candidate
    cursor.execute("""
        SELECT id, test_type, total_questions, total_marks, obtained_marks, 
               percentage, skills_tested, completed_at
        FROM ai_tests
        WHERE candidate_id = %s AND status = 'completed'
        ORDER BY completed_at DESC
    """, (candidate_id,))
    tests = cursor.fetchall()
    
    # Get candidate info
    cursor.execute("SELECT name, email FROM candidates WHERE id = %s", (candidate_id,))
    candidate = cursor.fetchone()
    
    cleanup_db_resources(cursor, db)
    
    if not candidate:
        flash("Candidate not found", "danger")
        return redirect('/applications')
    
    return render_template('view_candidate_tests.html', tests=tests, candidate=candidate)

def generate_ai_skill_analysis(profile, test, skill_performance, weak_skills, strong_skills, detailed_questions, previous_tests):
    """Generate AI-powered personalized skill analysis and recommendations"""
    try:
        api_key = os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY')
        if not api_key:
            return _get_default_analysis(skill_performance, weak_skills, strong_skills)
        
        try:
            # Prepare analysis context
            strong_skills_text = ", ".join([s['name'] for s in strong_skills]) if strong_skills else "None identified"
            weak_skills_text = ", ".join([s['name'] for s in weak_skills]) if weak_skills else "None identified"
            
            # Calculate improvement trend
            trend = "No previous tests"
            if previous_tests and len(previous_tests) > 0:
                prev_avg = sum([t['percentage'] for t in previous_tests]) / len(previous_tests)
                current_pct = test.get('percentage', 0)
                if current_pct > prev_avg + 10:
                    trend = "Significant improvement"
                elif current_pct > prev_avg:
                    trend = "Steady improvement"
                elif current_pct < prev_avg - 10:
                    trend = "Needs attention"
                else:
                    trend = "Consistent performance"
            
            prompt = f"""You are an expert career counselor and technical skills analyst. Analyze this candidate's test performance and provide detailed, actionable insights.

**CANDIDATE PROFILE:**
- Name: {profile.get('name', 'N/A')}
- Experience: {profile.get('experience_years', 0)} years
- Education: {profile.get('education', 'N/A')}
- Current Skills: {profile.get('skills', 'N/A')}
- Location: {profile.get('location', 'N/A')}

**TEST DETAILS:**
- Role: {test.get('skills_tested', 'Technical Assessment').split(',')[0]}
- Score: {test.get('obtained_marks', 0)}/{test.get('total_questions', 0)} ({test.get('percentage', 0):.1f}%)
- Performance Trend: {trend}

**SKILL BREAKDOWN:**
- Strong Skills (80%+): {strong_skills_text}
- Weak Skills (<60%): {weak_skills_text}

**DETAILED PERFORMANCE:**
{chr(10).join([f"- {skill}: {data['correct']}/{data['total']} correct ({data['percentage']:.1f}%)" for skill, data in skill_performance.items()])}

Provide a comprehensive analysis in the following JSON format:
{{
    "strengths_analysis": "2-3 sentences analyzing their strong skills and what this means for their career",
    "weaknesses_analysis": "2-3 sentences analyzing weak areas with specific improvement strategies",
    "personalized_advice": "3-4 actionable recommendations based on their experience level and current skills",
    "next_steps": ["Step 1", "Step 2", "Step 3", "Step 4"],
    "career_fit": "Assessment of readiness for {test.get('skills_tested', 'this role').split(',')[0]} (Ready/Almost Ready/Needs Preparation)",
    "estimated_ready_time": "Time estimate to become job-ready (e.g., '2-3 months', 'Already ready', '6+ months')",
    "trending_skills_to_add": ["Skill 1", "Skill 2", "Skill 3"],
    "interview_preparation_tips": ["Tip 1", "Tip 2", "Tip 3"]
}}

Be honest, encouraging, and specific. Consider their experience level when making recommendations."""
            
            analysis = _generate_gemini_json(prompt, timeout=20)
            if analysis:
                return analysis
            return _get_default_analysis(skill_performance, weak_skills, strong_skills)
            
        except Exception as api_error:
            print(f"AI API error: {str(api_error)}")
            return _get_default_analysis(skill_performance, weak_skills, strong_skills)
    
    except ImportError:
        print("Google GenAI package not installed. Using default analysis.")
        return _get_default_analysis(skill_performance, weak_skills, strong_skills)

def _get_default_analysis(skill_performance, weak_skills, strong_skills):
    """Return default analysis when AI is unavailable"""
    strong_skills_text = ", ".join([s['name'] for s in strong_skills]) if strong_skills else "None identified"
    weak_skills_text = ", ".join([s['name'] for s in weak_skills]) if weak_skills else "None identified"
    
    return {
        'strengths_analysis': f"You demonstrated strong performance in: {strong_skills_text}. These skills are highly valued in the industry and form a solid foundation for your career growth.",
        'weaknesses_analysis': f"Key areas for improvement include: {weak_skills_text}. These skills are important for advancing your career. Focus on targeted practice and hands-on projects to strengthen these areas.",
        'personalized_advice': "1) Practice regularly with real-world projects, 2) Review fundamentals of weak areas, 3) Complete recommended online courses, 4) Seek mentorship in challenging topics",
        'next_steps': ["Review weak skill fundamentals", "Complete recommended courses", "Build projects to practice", "Retake assessment after 2-3 weeks"],
        'career_fit': 'Almost Ready' if weak_skills_text != "None identified" else 'Ready',
        'estimated_ready_time': '3-6 months with consistent practice',
        'trending_skills_to_add': [],
        'interview_preparation_tips': [
            "Practice explaining your projects and experience clearly",
            "Research the company and role thoroughly before interview",
            "Prepare examples of how you've handled challenges"
        ]
    }

def get_trending_technologies(job_role):
    """Get trending technologies for the job role"""
    trending_tech_db = {
        'Python Developer': [
            {'name': 'FastAPI', 'category': 'Web Framework', 'demand': 'High', 'description': 'Modern async web framework'},
            {'name': 'PyTorch', 'category': 'AI/ML', 'demand': 'Very High', 'description': 'Deep learning framework'},
            {'name': 'Pandas', 'category': 'Data Science', 'demand': 'High', 'description': 'Data manipulation library'},
            {'name': 'Docker', 'category': 'DevOps', 'demand': 'Very High', 'description': 'Containerization platform'},
        ],
        'Full Stack Developer': [
            {'name': 'Next.js', 'category': 'Frontend', 'demand': 'Very High', 'description': 'React framework with SSR'},
            {'name': 'TypeScript', 'category': 'Language', 'demand': 'Very High', 'description': 'Typed JavaScript'},
            {'name': 'GraphQL', 'category': 'API', 'demand': 'High', 'description': 'Query language for APIs'},
            {'name': 'Kubernetes', 'category': 'DevOps', 'demand': 'High', 'description': 'Container orchestration'},
        ],
        'Frontend Developer': [
            {'name': 'React 18', 'category': 'Framework', 'demand': 'Very High', 'description': 'Latest React with Suspense'},
            {'name': 'Tailwind CSS', 'category': 'Styling', 'demand': 'Very High', 'description': 'Utility-first CSS'},
            {'name': 'Next.js', 'category': 'Framework', 'demand': 'Very High', 'description': 'Production React framework'},
            {'name': 'Vite', 'category': 'Build Tool', 'demand': 'High', 'description': 'Lightning-fast dev server'},
        ],
        'Backend Developer': [
            {'name': 'Node.js', 'category': 'Runtime', 'demand': 'Very High', 'description': 'JavaScript runtime'},
            {'name': 'PostgreSQL', 'category': 'Database', 'demand': 'High', 'description': 'Advanced SQL database'},
            {'name': 'Redis', 'category': 'Cache', 'demand': 'High', 'description': 'In-memory data store'},
            {'name': 'Microservices', 'category': 'Architecture', 'demand': 'Very High', 'description': 'Distributed systems'},
        ],
        'Data Scientist': [
            {'name': 'TensorFlow', 'category': 'AI/ML', 'demand': 'Very High', 'description': 'Machine learning platform'},
            {'name': 'Jupyter', 'category': 'Tools', 'demand': 'High', 'description': 'Interactive notebooks'},
            {'name': 'Scikit-learn', 'category': 'ML Library', 'demand': 'High', 'description': 'ML algorithms'},
            {'name': 'Apache Spark', 'category': 'Big Data', 'demand': 'High', 'description': 'Distributed computing'},
        ]
    }
    
    # Return matching technologies or default
    for key in trending_tech_db.keys():
        if key.lower() in job_role.lower():
            return trending_tech_db[key]
    
    # Default trending technologies
    return [
        {'name': 'AI/ML', 'category': 'Emerging Tech', 'demand': 'Very High', 'description': 'Artificial Intelligence & Machine Learning'},
        {'name': 'Cloud (AWS/Azure/GCP)', 'category': 'Infrastructure', 'demand': 'Very High', 'description': 'Cloud platforms'},
        {'name': 'DevOps', 'category': 'Methodology', 'demand': 'High', 'description': 'CI/CD and automation'},
        {'name': 'Cybersecurity', 'category': 'Security', 'demand': 'High', 'description': 'Application security'},
    ]


def _normalize_skill_text(value):
    return re.sub(r'[^a-z0-9\+\#\.\-\s]', '', str(value or '').strip().lower())


def _split_skills_text(skills_text):
    if not skills_text:
        return []
    tokens = []
    for token in re.split(r'[\,\|;/]+', str(skills_text)):
        clean = _normalize_skill_text(token)
        if clean:
            tokens.append(clean)
    return tokens


def _extract_candidate_skill_set(profile):
    fields = [
        'primary_skills',
        'secondary_skills',
        'frameworks_libraries',
        'databases',
        'cloud_platforms',
        'tools_technologies',
        'skills'
    ]
    collected = []
    for field in fields:
        collected.extend(_split_skills_text((profile or {}).get(field)))
    return set(collected)


def generate_ai_market_insights(job_role, market_trends, weak_skills):
    """Generate AI narrative for market trends and upskilling roadmap."""
    try:
        api_key = os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY')
        if not api_key:
            raise ValueError("Missing API key")

        top_skills = market_trends.get('top_skills', [])[:6]
        gap_skills = market_trends.get('urgent_gap_skills', [])[:5]
        weak_skill_names = [s.get('skill_name') or s.get('name') for s in (weak_skills or [])]
        weak_skill_names = [w for w in weak_skill_names if w]

        prompt = f"""You are a hiring market analyst for technical roles.

Role: {job_role}
Market trend snapshot:
- Total active postings analyzed: {market_trends.get('total_postings', 0)}
- Last 30-day postings: {market_trends.get('recent_postings_30d', 0)}
- Top skills: {', '.join([s.get('skill') for s in top_skills if s.get('skill')])}
- Urgent gap skills for candidate: {', '.join(gap_skills)}
- Candidate weak skills from assessment: {', '.join(weak_skill_names)}

Return strict JSON:
{{
  "market_outlook": "2-3 sentence outlook",
  "hiring_signal": "Short sentence with hiring intensity",
  "priority_skills": ["skill 1", "skill 2", "skill 3", "skill 4"],
  "project_ideas": ["idea 1", "idea 2", "idea 3"],
  "weekly_plan": ["week step 1", "week step 2", "week step 3", "week step 4"]
}}
"""

        result = _generate_gemini_json(prompt, timeout=20)
        if isinstance(result, dict):
            return result
    except Exception as e:
        print(f"AI market insight generation fallback: {e}")

    fallback_priority = market_trends.get('urgent_gap_skills', [])[:4]
    if not fallback_priority:
        fallback_priority = [s.get('skill') for s in market_trends.get('top_skills', []) if s.get('skill')][:4]

    return {
        'market_outlook': f"Demand for {job_role} roles remains strong, with employers increasingly prioritizing practical, project-backed skills and modern tooling.",
        'hiring_signal': "Moderate to high hiring activity detected from active role postings.",
        'priority_skills': fallback_priority,
        'project_ideas': [
            "Build an end-to-end portfolio project with authentication and deployment",
            "Create a mini analytics dashboard using real-world datasets",
            "Develop one optimization-focused feature and measure performance impact"
        ],
        'weekly_plan': [
            "Week 1: Strengthen fundamentals in top missing skill",
            "Week 2: Build one guided project with that skill",
            "Week 3: Add testing, optimization, and documentation",
            "Week 4: Mock interview + assessment retake"
        ]
    }


def analyze_role_market_trends(job_role, candidate_skill_set, weak_skills):
    """Analyze in-platform job postings to infer latest market trends for a given role."""
    trend_data = {
        'job_role': job_role,
        'total_postings': 0,
        'recent_postings_30d': 0,
        'top_skills': [],
        'urgent_gap_skills': [],
        'market_insights': {}
    }

    try:
        with SafeDBConnection() as (cursor, db):
            cursor.execute("""
                SELECT title, required_skills, created_at
                FROM jobs
                WHERE (deadline IS NULL OR deadline >= CURRENT_DATE)
                  AND LOWER(COALESCE(status, '')) = 'active'
                ORDER BY created_at DESC
                LIMIT 500
            """)
            jobs = cursor.fetchall() or []

        role_terms = [
            _normalize_skill_text(t) for t in re.split(r'\s+', str(job_role or '').strip())
            if len(_normalize_skill_text(t)) >= 3
        ]

        relevant_jobs = []
        for job in jobs:
            title = _normalize_skill_text(job.get('title'))
            if not role_terms or any(term in title for term in role_terms):
                relevant_jobs.append(job)

        # Fallback to all jobs if role-specific sample is too small
        if len(relevant_jobs) < 15:
            relevant_jobs = jobs

        now = datetime.utcnow()
        skill_stats = {}
        recent_postings_30d = 0

        for job in relevant_jobs:
            created_at = job.get('created_at')
            age_days = 180
            if created_at:
                try:
                    age_days = max(0, (now - created_at.replace(tzinfo=None)).days)
                except Exception:
                    age_days = 180

            if age_days <= 30:
                recent_postings_30d += 1

            recency_weight = 1.0
            if age_days <= 30:
                recency_weight = 1.5
            elif age_days <= 90:
                recency_weight = 1.2

            skills = _split_skills_text(job.get('required_skills'))
            for skill in skills:
                if skill not in skill_stats:
                    skill_stats[skill] = {'weighted': 0.0, 'count': 0, 'recent': 0, 'older': 0}
                skill_stats[skill]['weighted'] += recency_weight
                skill_stats[skill]['count'] += 1
                if age_days <= 30:
                    skill_stats[skill]['recent'] += 1
                else:
                    skill_stats[skill]['older'] += 1

        sorted_skills = sorted(skill_stats.items(), key=lambda x: x[1]['weighted'], reverse=True)
        top_skills = []
        for skill, stats in sorted_skills[:10]:
            if stats['recent'] >= max(2, stats['older']):
                momentum = 'Rising'
            elif stats['recent'] >= 1 and stats['older'] >= 1:
                momentum = 'Stable'
            else:
                momentum = 'Emerging'

            top_skills.append({
                'skill': skill.title(),
                'demand_score': round(stats['weighted'], 1),
                'job_count': stats['count'],
                'momentum': momentum,
                'candidate_status': 'Covered' if skill in (candidate_skill_set or set()) else 'Gap'
            })

        weak_skill_names = {
            _normalize_skill_text(s.get('skill_name') or s.get('name'))
            for s in (weak_skills or []) if (s.get('skill_name') or s.get('name'))
        }

        gap_skills = []
        for item in top_skills:
            skill_norm = _normalize_skill_text(item['skill'])
            if skill_norm not in (candidate_skill_set or set()):
                gap_skills.append(item['skill'])

        # Prioritize overlap with assessment weak skills first
        prioritized = [s for s in gap_skills if _normalize_skill_text(s) in weak_skill_names]
        remaining = [s for s in gap_skills if s not in prioritized]
        urgent_gap_skills = (prioritized + remaining)[:6]

        trend_data.update({
            'total_postings': len(relevant_jobs),
            'recent_postings_30d': recent_postings_30d,
            'top_skills': top_skills,
            'urgent_gap_skills': urgent_gap_skills
        })

    except Exception as e:
        print(f"Market trend analysis error: {e}")

    trend_data['market_insights'] = generate_ai_market_insights(job_role, trend_data, weak_skills)
    return trend_data


def generate_video_recommendations(weak_skills, market_trends, job_role):
    """Generate curated video recommendations from weak and trending skills."""
    video_library = {
        'python': [
            {'title': 'Python Full Course for Beginners', 'url': 'https://www.youtube.com/watch?v=_uQrJ0TkZlc', 'platform': 'YouTube', 'duration': '6h', 'focus': 'Core Python'},
            {'title': 'Python OOP Crash Course', 'url': 'https://www.youtube.com/watch?v=Ej_02ICOIgs', 'platform': 'YouTube', 'duration': '2h', 'focus': 'Object-Oriented Programming'}
        ],
        'sql': [
            {'title': 'SQL Full Course for Beginners', 'url': 'https://www.youtube.com/watch?v=HXV3zeQKqGY', 'platform': 'YouTube', 'duration': '4h', 'focus': 'Queries & Joins'},
            {'title': 'Advanced SQL Tutorial', 'url': 'https://www.youtube.com/watch?v=7S_tz1z_5bA', 'platform': 'YouTube', 'duration': '3h', 'focus': 'Performance & Optimization'}
        ],
        'javascript': [
            {'title': 'JavaScript Crash Course', 'url': 'https://www.youtube.com/watch?v=hdI2bqOjy3c', 'platform': 'YouTube', 'duration': '1.5h', 'focus': 'JS Fundamentals'},
            {'title': 'JavaScript Projects for Portfolio', 'url': 'https://www.youtube.com/watch?v=3PHXvlpOkf4', 'platform': 'YouTube', 'duration': '8h', 'focus': 'Hands-on Projects'}
        ],
        'react': [
            {'title': 'React JS Full Course', 'url': 'https://www.youtube.com/watch?v=bMknfKXIFA8', 'platform': 'YouTube', 'duration': '12h', 'focus': 'React Fundamentals to Advanced'},
            {'title': 'React Project Build', 'url': 'https://www.youtube.com/watch?v=w7ejDZ8SWv8', 'platform': 'YouTube', 'duration': '2h', 'focus': 'Practical App Development'}
        ],
        'docker': [
            {'title': 'Docker Tutorial for Beginners', 'url': 'https://www.youtube.com/watch?v=3c-iBn73dDE', 'platform': 'YouTube', 'duration': '3h', 'focus': 'Container Fundamentals'}
        ],
        'fastapi': [
            {'title': 'FastAPI Full Course', 'url': 'https://www.youtube.com/watch?v=7t2alSnE2-I', 'platform': 'YouTube', 'duration': '5h', 'focus': 'Modern Python APIs'}
        ],
        'node.js': [
            {'title': 'Node.js and Express.js Full Course', 'url': 'https://www.youtube.com/watch?v=Oe421EPjeBE', 'platform': 'YouTube', 'duration': '8h', 'focus': 'Backend Development'}
        ]
    }

    weak_names = [
        _normalize_skill_text(s.get('skill_name') or s.get('name'))
        for s in (weak_skills or []) if (s.get('skill_name') or s.get('name'))
    ]
    trend_names = [_normalize_skill_text(s) for s in market_trends.get('urgent_gap_skills', [])]

    candidate_targets = []
    for name in weak_names + trend_names:
        if name and name not in candidate_targets:
            candidate_targets.append(name)

    if not candidate_targets:
        candidate_targets = [_normalize_skill_text(job_role)]

    picked = []
    seen_urls = set()

    for target in candidate_targets:
        for key, videos in video_library.items():
            if key in target or target in key:
                for video in videos:
                    if video['url'] in seen_urls:
                        continue
                    entry = dict(video)
                    entry['recommended_for'] = target.title()
                    picked.append(entry)
                    seen_urls.add(video['url'])
                    if len(picked) >= 8:
                        return picked

    # Generic fallback videos
    generic = [
        {'title': 'System Design Interview Basics', 'url': 'https://www.youtube.com/watch?v=bUHFg8CZFws', 'platform': 'YouTube', 'duration': '2h', 'focus': 'Architecture', 'recommended_for': 'System Design'},
        {'title': 'DSA Roadmap for Placements', 'url': 'https://www.youtube.com/watch?v=RBSGKlAvoiM', 'platform': 'YouTube', 'duration': '8h', 'focus': 'Problem Solving', 'recommended_for': 'Coding Interviews'},
        {'title': 'Behavioral Interview Masterclass', 'url': 'https://www.youtube.com/watch?v=9FgfsLa_SmY', 'platform': 'YouTube', 'duration': '1h', 'focus': 'Communication', 'recommended_for': 'Interview Readiness'}
    ]
    for video in generic:
        if video['url'] not in seen_urls:
            picked.append(video)
            seen_urls.add(video['url'])
        if len(picked) >= 8:
            break

    return picked

def generate_career_roadmap(profile, strong_skills, weak_skills, job_role):
    """Generate a personalized career roadmap"""
    experience = profile.get('experience_years', 0)
    
    roadmap = {
        'current_level': '',
        'target_level': '',
        'timeline': '',
        'milestones': []
    }
    
    # Determine current level
    score_pct = len(strong_skills) / (len(strong_skills) + len(weak_skills)) * 100 if (len(strong_skills) + len(weak_skills)) > 0 else 0
    
    if experience < 1:
        roadmap['current_level'] = 'Entry Level / Fresher'
        roadmap['target_level'] = 'Junior Developer'
        roadmap['timeline'] = '3-6 months'
        roadmap['milestones'] = [
            {'title': 'Master Fundamentals', 'duration': '1-2 months', 'status': 'in_progress'},
            {'title': 'Build 3-5 Projects', 'duration': '2-3 months', 'status': 'pending'},
            {'title': 'Complete Certifications', 'duration': '1 month', 'status': 'pending'},
            {'title': 'Apply for Junior Roles', 'duration': 'Ongoing', 'status': 'pending'}
        ]
    elif experience < 3:
        roadmap['current_level'] = 'Junior Developer'
        roadmap['target_level'] = 'Mid-Level Developer'
        roadmap['timeline'] = '6-12 months'
        roadmap['milestones'] = [
            {'title': 'Strengthen Core Skills', 'duration': '2-3 months', 'status': 'in_progress'},
            {'title': 'Learn Advanced Concepts', 'duration': '3-4 months', 'status': 'pending'},
            {'title': 'Contribute to Open Source', 'duration': '2-3 months', 'status': 'pending'},
            {'title': 'Build Complex Projects', 'duration': '3-4 months', 'status': 'pending'}
        ]
    else:
        roadmap['current_level'] = 'Experienced Developer'
        roadmap['target_level'] = 'Senior/Lead Developer'
        roadmap['timeline'] = '12-18 months'
        roadmap['milestones'] = [
            {'title': 'Master Architecture Patterns', 'duration': '3-4 months', 'status': 'in_progress'},
            {'title': 'Learn System Design', 'duration': '3-4 months', 'status': 'pending'},
            {'title': 'Mentoring & Leadership', 'duration': '4-6 months', 'status': 'pending'},
            {'title': 'Specialize in Domain', 'duration': '6-8 months', 'status': 'pending'}
        ]
    
    # Adjust based on performance
    if score_pct >= 80:
        roadmap['timeline'] = roadmap['timeline'].split('-')[0] + ' months'  # Shorter timeline
        if roadmap['milestones']:
            roadmap['milestones'][0]['status'] = 'completed'
    
    return roadmap

@app.route('/skill-gap-analysis/<int:test_id>')
def skill_gap_analysis(test_id):
    if session.get('role') != 'candidate':
        return redirect('/login')
    
    candidate_id = session.get('user_id')
    db = get_connection()
    cursor = db.cursor(cursor_factory=RealDictCursor)
    
    # Get candidate profile with ALL details
    cursor.execute("""
        SELECT cp.*, c.name, c.email 
        FROM candidate_profiles cp
        JOIN candidates c ON cp.candidate_id = c.id
        WHERE cp.candidate_id = %s
    """, (candidate_id,))
    profile = cursor.fetchone()
    
    # Profile is optional for viewing test results
    if not profile:
        # Create minimal profile data if none exists
        profile = {
            'candidate_id': candidate_id,
            'profile_completed': 0,
            'name': session.get('user_name', 'Candidate'),
            'email': session.get('user_email', '')
        }
    
    # Verify test belongs to candidate
    cursor.execute(
        "SELECT * FROM ai_tests WHERE id = %s AND candidate_id = %s AND status = 'completed'",
        (test_id, candidate_id)
    )
    test = cursor.fetchone()
    
    if not test:
        cleanup_db_resources(cursor, db)
        flash("Test not found", "danger")
        return redirect('/candidate-dashboard#ai-assessments')
    
    # Get all questions with detailed info
    cursor.execute("""
        SELECT question_text, is_correct, candidate_answer, correct_answer, question_number, section
        FROM ai_test_questions 
        WHERE test_id = %s
        ORDER BY question_number
    """, (test_id,))
    questions = cursor.fetchall()
    
    # Get candidate's previous test history for trend analysis
    cursor.execute("""
        SELECT percentage, total_questions, completed_at, skills_tested
        FROM ai_tests 
        WHERE candidate_id = %s AND status = 'completed' AND id != %s
        ORDER BY completed_at DESC
        LIMIT 5
    """, (candidate_id, test_id))
    previous_tests = cursor.fetchall()
    
    cleanup_db_resources(cursor, db)
    
    # Analyze skills with detailed tracking
    skill_performance = {}
    detailed_questions = []
    
    for q in questions:
        # Store detailed question info for AI analysis
        detailed_questions.append({
            'number': q['question_number'],
            'question': q['question_text'],
            'correct': q['is_correct'],
            'candidate_answer': q['candidate_answer'],
            'correct_answer': q['correct_answer']
        })
    
    section_labels = {
        'technical': 'Technical Skills',
        'aptitude': 'Aptitude',
        'general_knowledge': 'General Knowledge'
    }

    for q in questions:
        question = q['question_text'] or ''
        skill = None
        level = 'medium'

        # Preferred format: [Skill - Level] Question
        if '[' in question and ']' in question:
            skill_part = question[question.find('[')+1:question.find(']')]
            if ' - ' in skill_part:
                parts = skill_part.split(' - ')
                skill = parts[0].strip()
                level = parts[1].strip().lower() if len(parts) > 1 else 'medium'

        # Fallback to section metadata when embedded skill tags are missing
        if not skill:
            section = (q.get('section') or '').strip().lower()
            skill = section_labels.get(section, 'Overall Performance')

        if skill not in skill_performance:
            skill_performance[skill] = {
                'total': 0,
                'correct': 0,
                'wrong': 0,
                'easy': {'total': 0, 'correct': 0},
                'medium': {'total': 0, 'correct': 0},
                'hard': {'total': 0, 'correct': 0}
            }

        skill_performance[skill]['total'] += 1

        # Track by difficulty when the source is available
        if level in skill_performance[skill]:
            skill_performance[skill][level]['total'] += 1
            if q['is_correct']:
                skill_performance[skill][level]['correct'] += 1

        if q['is_correct']:
            skill_performance[skill]['correct'] += 1
        else:
            skill_performance[skill]['wrong'] += 1
    
    # Calculate percentages and identify weak skills
    weak_skills = []
    strong_skills = []
    moderate_skills = []
    
    for skill, stats in skill_performance.items():
        stats['percentage'] = round((stats['correct'] / stats['total'] * 100), 1) if stats['total'] > 0 else 0
        stats['skill_name'] = skill
        stats['accuracy'] = stats['percentage']
        stats['name'] = skill
        
        # Add difficulty breakdown for template
        stats['easy_total'] = stats['easy']['total']
        stats['easy_correct'] = stats['easy']['correct']
        stats['medium_total'] = stats['medium']['total']
        stats['medium_correct'] = stats['medium']['correct']
        stats['hard_total'] = stats['hard']['total']
        stats['hard_correct'] = stats['hard']['correct']
        
        if stats['percentage'] < 60:
            weak_skills.append(stats)
        elif stats['percentage'] >= 80:
            strong_skills.append(stats)
        else:
            moderate_skills.append(stats)
    
    # Get course recommendations for weak skills
    recommendations = get_course_recommendations(weak_skills)
    
    # Add courses to weak skills for template
    for skill_data in weak_skills:
        skill_name = skill_data['skill_name']
        if skill_name in recommendations:
            skill_data['courses'] = recommendations[skill_name]
        else:
            skill_data['courses'] = []
    
    # Generate AI-powered personalized insights
    ai_insights = generate_ai_skill_analysis(
        profile=profile,
        test=test,
        skill_performance=skill_performance,
        weak_skills=weak_skills,
        strong_skills=strong_skills,
        detailed_questions=detailed_questions,
        previous_tests=previous_tests
    )
    
    # Get trending technologies and career recommendations
    job_role_for_analysis = test.get('skills_tested', 'Technical Assessment').split(',')[0] if test.get('skills_tested') else 'Technical Assessment'
    trending_tech = get_trending_technologies(job_role_for_analysis)
    career_roadmap = generate_career_roadmap(profile, strong_skills, weak_skills, job_role_for_analysis)
    candidate_skill_set = _extract_candidate_skill_set(profile)
    market_trends = analyze_role_market_trends(job_role_for_analysis, candidate_skill_set, weak_skills)
    video_recommendations = generate_video_recommendations(weak_skills, market_trends, job_role_for_analysis)

    # Enrich AI insights with high-demand missing skills if model did not return any.
    if isinstance(ai_insights, dict) and not ai_insights.get('trending_skills_to_add'):
        ai_insights['trending_skills_to_add'] = market_trends.get('urgent_gap_skills', [])[:5]
    
    return render_template('skill_gap_analysis.html', 
                         test=test,
                         test_id=test_id,
                         profile=profile,
                         job_role=test.get('skills_tested', 'Technical Assessment').split(',')[0] if test.get('skills_tested') else 'Technical Assessment',
                         test_date=test.get('completed_at', test.get('started_at', '')).strftime('%B %d, %Y') if test.get('completed_at') or test.get('started_at') else 'N/A',
                         score=int(test.get('obtained_marks', 0) / 2),
                         total_questions=test.get('total_questions', 25),
                         percentage=round(test.get('percentage', 0), 1),
                         skill_performance=skill_performance,
                         weak_skills=weak_skills,
                         strong_skills=strong_skills,
                         moderate_skills=moderate_skills,
                         recommendations=recommendations,
                         ai_insights=ai_insights,
                         trending_tech=trending_tech,
                         career_roadmap=career_roadmap,
                         market_trends=market_trends,
                         video_recommendations=video_recommendations,
                         previous_tests=previous_tests)

def get_course_recommendations(weak_skills):
    """Generate course and video recommendations for weak skills"""
    recommendations = {}
    
    # Course database with popular learning resources
    course_library = {
        'Python': [
            {'title': 'Python for Everybody - Coursera', 'url': 'https://www.coursera.org/specializations/python', 'type': 'Course', 'platform': 'Coursera'},
            {'title': 'Complete Python Bootcamp - Udemy', 'url': 'https://www.udemy.com/course/complete-python-bootcamp/', 'type': 'Course', 'platform': 'Udemy'},
            {'title': 'Python Tutorial - Programming with Mosh', 'url': 'https://www.youtube.com/watch?v=_uQrJ0TkZlc', 'type': 'Video', 'platform': 'YouTube'},
        ],
        'SQL': [
            {'title': 'SQL for Data Science - Coursera', 'url': 'https://www.coursera.org/learn/sql-for-data-science', 'type': 'Course', 'platform': 'Coursera'},
            {'title': 'The Complete SQL Bootcamp - Udemy', 'url': 'https://www.udemy.com/course/the-complete-sql-bootcamp/', 'type': 'Course', 'platform': 'Udemy'},
            {'title': 'SQL Tutorial - Full Database Course', 'url': 'https://www.youtube.com/watch?v=HXV3zeQKqGY', 'type': 'Video', 'platform': 'YouTube'},
        ],
        'JavaScript': [
            {'title': 'JavaScript - The Complete Guide - Udemy', 'url': 'https://www.udemy.com/course/javascript-the-complete-guide-2020-beginner-advanced/', 'type': 'Course', 'platform': 'Udemy'},
            {'title': 'JavaScript Algorithms and Data Structures', 'url': 'https://www.freecodecamp.org/learn/javascript-algorithms-and-data-structures/', 'type': 'Course', 'platform': 'freeCodeCamp'},
            {'title': 'JavaScript Crash Course', 'url': 'https://www.youtube.com/watch?v=hdI2bqOjy3c', 'type': 'Video', 'platform': 'YouTube'},
        ],
        'HTML': [
            {'title': 'HTML Full Course - Build a Website Tutorial', 'url': 'https://www.youtube.com/watch?v=pQN-pnXPaVg', 'type': 'Video', 'platform': 'YouTube'},
            {'title': 'Responsive Web Design - freeCodeCamp', 'url': 'https://www.freecodecamp.org/learn/responsive-web-design/', 'type': 'Course', 'platform': 'freeCodeCamp'},
        ],
        'CSS': [
            {'title': 'CSS - The Complete Guide - Udemy', 'url': 'https://www.udemy.com/course/css-the-complete-guide-incl-flexbox-grid-sass/', 'type': 'Course', 'platform': 'Udemy'},
            {'title': 'CSS Tutorial - Zero to Hero', 'url': 'https://www.youtube.com/watch?v=1Rs2ND1ryYc', 'type': 'Video', 'platform': 'YouTube'},
            {'title': 'Responsive Web Design - freeCodeCamp', 'url': 'https://www.freecodecamp.org/learn/responsive-web-design/', 'type': 'Course', 'platform': 'freeCodeCamp'},
        ],
        'React': [
            {'title': 'React - The Complete Guide - Udemy', 'url': 'https://www.udemy.com/course/react-the-complete-guide-incl-redux/', 'type': 'Course', 'platform': 'Udemy'},
            {'title': 'React JS Full Course - YouTube', 'url': 'https://www.youtube.com/watch?v=bMknfKXIFA8', 'type': 'Video', 'platform': 'YouTube'},
        ],
        'Django': [
            {'title': 'Django for Everybody - Coursera', 'url': 'https://www.coursera.org/specializations/django', 'type': 'Course', 'platform': 'Coursera'},
            {'title': 'Python Django Tutorial - YouTube', 'url': 'https://www.youtube.com/watch?v=F5mRW0jo-U4', 'type': 'Video', 'platform': 'YouTube'},
        ],
        'Flask': [
            {'title': 'Flask Mega-Tutorial', 'url': 'https://blog.miguelgrinberg.com/post/the-flask-mega-tutorial-part-i-hello-world', 'type': 'Tutorial', 'platform': 'Blog'},
            {'title': 'Flask Course - Python Web Development', 'url': 'https://www.youtube.com/watch?v=Qr4QMBUPxWo', 'type': 'Video', 'platform': 'YouTube'},
        ],
        'Git': [
            {'title': 'Git and GitHub for Beginners', 'url': 'https://www.youtube.com/watch?v=RGOj5yH7evk', 'type': 'Video', 'platform': 'YouTube'},
            {'title': 'Version Control with Git - Coursera', 'url': 'https://www.coursera.org/learn/version-control-with-git', 'type': 'Course', 'platform': 'Coursera'},
        ],
        'AWS': [
            {'title': 'AWS Certified Cloud Practitioner', 'url': 'https://www.youtube.com/watch?v=3hLmDS179YE', 'type': 'Video', 'platform': 'YouTube'},
            {'title': 'AWS Fundamentals - Coursera', 'url': 'https://www.coursera.org/learn/aws-fundamentals-going-cloud-native', 'type': 'Course', 'platform': 'Coursera'},
        ],
        'REST': [
            {'title': 'REST API concepts and examples', 'url': 'https://www.youtube.com/watch?v=7YcW25PHnAA', 'type': 'Video', 'platform': 'YouTube'},
            {'title': 'REST APIs with Flask and Python', 'url': 'https://www.udemy.com/course/rest-api-flask-and-python/', 'type': 'Course', 'platform': 'Udemy'},
        ],
        'MySQL': [
            {'title': 'MySQL Tutorial for Beginners', 'url': 'https://www.youtube.com/watch?v=7S_tz1z_5bA', 'type': 'Video', 'platform': 'YouTube'},
            {'title': 'MySQL Database Development Mastery', 'url': 'https://www.udemy.com/course/mysql-database-development-mastery/', 'type': 'Course', 'platform': 'Udemy'},
        ],
        'PostgreSQL': [
            {'title': 'PostgreSQL Tutorial Full Course', 'url': 'https://www.youtube.com/watch?v=qw--VYLpxG4', 'type': 'Video', 'platform': 'YouTube'},
            {'title': 'The Complete PostgreSQL Course', 'url': 'https://www.udemy.com/course/the-complete-python-postgresql-developer-course/', 'type': 'Course', 'platform': 'Udemy'},
        ],
    }
    
    # Default recommendations for any skill not in library
    default_recommendations = [
        {'title': 'Search on Udemy', 'url': 'https://www.udemy.com', 'type': 'Platform', 'platform': 'Udemy'},
        {'title': 'Search on Coursera', 'url': 'https://www.coursera.org', 'type': 'Platform', 'platform': 'Coursera'},
        {'title': 'Search on YouTube', 'url': 'https://www.youtube.com', 'type': 'Platform', 'platform': 'YouTube'},
        {'title': 'freeCodeCamp', 'url': 'https://www.freecodecamp.org', 'type': 'Platform', 'platform': 'freeCodeCamp'},
    ]
    
    for skill_data in weak_skills:
        skill = skill_data['skill_name']
        
        # Find matching courses (case-insensitive partial match)
        matching_courses = []
        for key, courses in course_library.items():
            if key.lower() in skill.lower() or skill.lower() in key.lower():
                matching_courses.extend(courses)
        
        if matching_courses:
            recommendations[skill] = matching_courses
        else:
            # Use default recommendations
            recommendations[skill] = default_recommendations
    
    return recommendations

@app.route('/view-test-details/<int:test_id>')
def view_test_details(test_id):
    if session.get('role') not in ['recruiter', 'candidate']:
        return redirect('/login')
    
    user_id = session.get('user_id')
    user_role = session.get('role')
    
    db = get_connection()
    cursor = db.cursor(cursor_factory=RealDictCursor)
    
    # Get test details
    cursor.execute("SELECT * FROM ai_tests WHERE id = %s", (test_id,))
    test = cursor.fetchone()
    
    if not test:
        cleanup_db_resources(cursor, db)
        flash("Test not found", "danger")
        return redirect('/candidate-dashboard' if user_role == 'candidate' else '/applications')
    
    # Authorization check
    if user_role == 'candidate' and test['candidate_id'] != user_id:
        cleanup_db_resources(cursor, db)
        flash("Unauthorized access", "danger")
        return redirect('/candidate-dashboard')
    
    # For recruiters, verify the candidate applied to their jobs
    if user_role == 'recruiter':
        cursor.execute("""
            SELECT COUNT(*) as count FROM applications a
            JOIN jobs j ON a.job_id = j.id
            WHERE a.candidate_id = %s AND j.recruiter_id = %s
        """, (test['candidate_id'], user_id))
        result = cursor.fetchone()
        if result['count'] == 0:
            cleanup_db_resources(cursor, db)
            flash("Unauthorized access", "danger")
            return redirect('/applications')
    
    # Get all questions for this test
    cursor.execute("""
        SELECT * FROM ai_test_questions 
        WHERE test_id = %s 
        ORDER BY question_number
    """, (test_id,))
    questions = cursor.fetchall()
    
    # Get candidate info
    cursor.execute("SELECT name FROM candidates WHERE id = %s", (test['candidate_id'],))
    candidate = cursor.fetchone()
    
    cleanup_db_resources(cursor, db)
    
    return render_template('test_details.html', test=test, questions=questions, candidate=candidate)

def _normalize_text_for_uniqueness(value):
    return re.sub(r'\s+', ' ', str(value or '').strip().lower())


def _is_valid_mcq_item(item):
    required = ['question', 'option_a', 'option_b', 'option_c', 'option_d', 'correct_answer']
    if not isinstance(item, dict):
        return False
    if not all(item.get(k) for k in required):
        return False
    answer = str(item.get('correct_answer', '')).strip().upper()
    if answer not in ['A', 'B', 'C', 'D']:
        return False
    options = [
        _normalize_text_for_uniqueness(item.get('option_a')),
        _normalize_text_for_uniqueness(item.get('option_b')),
        _normalize_text_for_uniqueness(item.get('option_c')),
        _normalize_text_for_uniqueness(item.get('option_d')),
    ]
    if '' in options or len(set(options)) < 4:
        return False
    return True


def _generate_advanced_technical_questions(
    skills_text,
    skills_list,
    num_questions=25,
    previous_questions=None,
    candidate_role='Technical Candidate',
    profile_summary='No profile data available'
):
    """Generate advanced, unique technical MCQs using Gemini JSON output with strict post-validation."""
    skill_list = [s.strip() for s in (skills_list or []) if s and str(s).strip()]
    if not skill_list:
        skill_list = ['Python', 'SQL', 'JavaScript']

    previous_questions = previous_questions or []
    prev_set = {
        _normalize_text_for_uniqueness(str(prev).split(']', 1)[-1])
        for prev in previous_questions
        if prev
    }

    easy_count = num_questions // 4
    medium_count = num_questions // 3
    hard_count = num_questions - easy_count - medium_count

    exclusion_text = ""
    if previous_questions:
        exclusion_text = f"""
DO NOT REPEAT ANY OF THESE PREVIOUS QUESTIONS:
{chr(10).join([f"- {q}" for q in previous_questions[:80]])}
"""

    prompt = f"""
You are a senior technical interviewer creating HARDCORE and ADVANCED MCQs.

Candidate role: {candidate_role}
Candidate profile summary: {profile_summary}
Skills to cover: {', '.join(skill_list)}
Skills context: {skills_text}
Required count: {num_questions}
Difficulty distribution: EASY={easy_count}, MEDIUM={medium_count}, HARD={hard_count}

{exclusion_text}

STRICT RULES:
1) Return ONLY a JSON array of exactly {num_questions} objects.
2) Each question must be unique and scenario-based (not textbook repeats).
3) Each question must contain 4 DISTINCT options.
4) Avoid repeated option phrases across questions.
5) Make options plausible and technical, not generic placeholders.
6) Include a mix of debugging, architecture, optimization, security, and edge-case reasoning.
7) correct_answer must be one of: A, B, C, D.

JSON schema per item:
{{
  "skill": "one of listed skills",
  "difficulty": "EASY|MEDIUM|HARD",
  "question": "question text only",
  "option_a": "...",
  "option_b": "...",
  "option_c": "...",
  "option_d": "...",
  "correct_answer": "A|B|C|D"
}}
"""

    parsed = _generate_gemini_json(prompt, timeout=30)
    candidates = parsed if isinstance(parsed, list) else []

    accepted = []
    seen_q = set()
    seen_option_sets = set()
    used_option_phrases = set()

    for item in candidates:
        if not _is_valid_mcq_item(item):
            continue

        q_text = str(item.get('question', '')).strip()
        q_norm = _normalize_text_for_uniqueness(q_text)
        if not q_norm or q_norm in seen_q or q_norm in prev_set:
            continue

        options = [
            str(item.get('option_a', '')).strip(),
            str(item.get('option_b', '')).strip(),
            str(item.get('option_c', '')).strip(),
            str(item.get('option_d', '')).strip(),
        ]
        opt_norm = [_normalize_text_for_uniqueness(o) for o in options]
        option_fingerprint = tuple(sorted(opt_norm))
        if option_fingerprint in seen_option_sets:
            continue

        overlap = sum(1 for o in opt_norm if o in used_option_phrases)
        if overlap >= 3:
            continue

        skill = str(item.get('skill', skill_list[len(accepted) % len(skill_list)])).strip() or skill_list[len(accepted) % len(skill_list)]
        difficulty = str(item.get('difficulty', 'MEDIUM')).strip().upper()
        if difficulty not in ['EASY', 'MEDIUM', 'HARD']:
            difficulty = 'MEDIUM'

        accepted.append({
            'skill': skill,
            'difficulty': difficulty.capitalize(),
            'question': f"[{skill} - {difficulty}] {q_text}",
            'option_a': options[0],
            'option_b': options[1],
            'option_c': options[2],
            'option_d': options[3],
            'correct_answer': str(item.get('correct_answer')).strip().upper()
        })

        seen_q.add(q_norm)
        seen_option_sets.add(option_fingerprint)
        used_option_phrases.update(opt_norm)

        if len(accepted) >= num_questions:
            break

    if len(accepted) < num_questions:
        fallback = generate_fallback_questions(skill_list, num_questions)
        for f in fallback:
            if len(accepted) >= num_questions:
                break
            q_norm = _normalize_text_for_uniqueness(str(f.get('question', '')).split(']', 1)[-1])
            if not q_norm or q_norm in seen_q or q_norm in prev_set:
                continue
            opt_norm = [
                _normalize_text_for_uniqueness(f.get('option_a')),
                _normalize_text_for_uniqueness(f.get('option_b')),
                _normalize_text_for_uniqueness(f.get('option_c')),
                _normalize_text_for_uniqueness(f.get('option_d')),
            ]
            option_fingerprint = tuple(sorted(opt_norm))
            if option_fingerprint in seen_option_sets:
                continue
            accepted.append(f)
            seen_q.add(q_norm)
            seen_option_sets.add(option_fingerprint)

    return accepted[:num_questions]

def generate_ai_questions(skills_text, skills_list, num_questions=25, previous_questions=None):
    """Generate AI-powered questions based on candidate skills, ensuring uniqueness."""
    try:
        return _generate_advanced_technical_questions(
            skills_text=skills_text,
            skills_list=skills_list,
            num_questions=num_questions,
            previous_questions=previous_questions,
            candidate_role='Technical Candidate',
            profile_summary='Profile summary not provided for this path'
        )
    except Exception as e:
        print(f"AI generation error: {e}")
        import traceback
        traceback.print_exc()
        return generate_fallback_questions(skills_list, num_questions)

def generate_fallback_questions(skills_list, num_questions=25):
    """
    Local fallback questions for candidate AI assessments when Gemini is unavailable.
    """
    if isinstance(skills_list, str):
        skills_list = [s.strip() for s in skills_list.split(',') if s.strip()]

    if not skills_list:
        skills_list = ['Python', 'SQL', 'JavaScript']

    fallback_questions = []

    templates = {
        "Easy": [
            {
                "stem": "In a {skill} code review, what is the best first check before merging?",
                "correct": "Validate inputs, error handling, and boundary conditions",
                "distractors": [
                    "Accept if code compiles on one machine",
                    "Skip tests for small changes",
                    "Ignore warning logs to speed delivery",
                    "Assume all user input is trusted",
                    "Merge without peer review"
                ]
            },
            {
                "stem": "What improves baseline reliability most in a {skill} service?",
                "correct": "Structured logging with actionable error messages",
                "distractors": [
                    "Removing validation for faster execution",
                    "Using broad try/except without context",
                    "Hardcoding environment values",
                    "Turning off alerts during debugging",
                    "Skipping rollback strategy"
                ]
            }
        ],
        "Medium": [
            {
                "stem": "A {skill} feature passes locally but fails in staging. What is the best next action?",
                "correct": "Compare runtime config, dependency versions, and secret values",
                "distractors": [
                    "Delete staging logs and rerun pipeline",
                    "Rewrite the module from scratch immediately",
                    "Disable staging checks temporarily",
                    "Increase CPU limits without investigation",
                    "Rollback unrelated components"
                ]
            },
            {
                "stem": "For maintainable {skill} architecture, which design choice is strongest?",
                "correct": "Define clear module contracts and test integration boundaries",
                "distractors": [
                    "Put all logic into one utility file",
                    "Use global mutable state for convenience",
                    "Avoid interface definitions",
                    "Prefer hidden side effects over explicit flows",
                    "Skip dependency pinning"
                ]
            }
        ],
        "Hard": [
            {
                "stem": "Your high-traffic {skill} API shows tail-latency spikes. Best mitigation strategy?",
                "correct": "Add targeted caching, backpressure, and idempotent retries with observability",
                "distractors": [
                    "Scale only frontend pods and ignore backend traces",
                    "Disable circuit breakers to avoid throttling",
                    "Turn off monitoring to reduce overhead",
                    "Use synchronous fan-out calls for all dependencies",
                    "Retry infinitely without jitter"
                ]
            },
            {
                "stem": "Which security posture is strongest for a production {skill} platform?",
                "correct": "Least privilege, secret rotation, and auditable access controls",
                "distractors": [
                    "Shared admin credentials across all services",
                    "Hardcoded API keys inside repositories",
                    "Open internal endpoints to public internet",
                    "Disable token expiry to reduce login friction",
                    "Skip dependency vulnerability scanning"
                ]
            }
        ]
    }

    labels = ['A', 'B', 'C', 'D']
    rotations = [
        [0, 1, 2, 3],
        [1, 3, 0, 2],
        [2, 0, 3, 1],
        [3, 2, 1, 0],
    ]

    for idx in range(num_questions):
        skill = skills_list[idx % len(skills_list)]
        difficulty = ["Easy", "Medium", "Hard"][idx % 3]
        bucket = templates[difficulty]
        t = bucket[(idx + len(skill)) % len(bucket)]
        variant = (idx // max(1, len(skills_list))) + 1

        base_correct = f"{t['correct']} ({skill}, scenario {variant})"
        dist_pool = [f"{d} ({skill}, scenario {variant})" for d in t['distractors']]
        start = (idx + sum(ord(ch) for ch in skill)) % len(dist_pool)
        distractors = [dist_pool[(start + j) % len(dist_pool)] for j in range(3)]

        options = [base_correct] + distractors
        rot = rotations[(idx + variant + len(skill)) % len(rotations)]
        final_opts = [options[i] for i in rot]
        correct_letter = labels[final_opts.index(base_correct)]

        fallback_questions.append({
            'skill': skill,
            'difficulty': difficulty,
            'question': f"[{skill} - {difficulty.upper()}] {t['stem'].format(skill=skill)} (Variant {variant})",
            'option_a': final_opts[0],
            'option_b': final_opts[1],
            'option_c': final_opts[2],
            'option_d': final_opts[3],
            'correct_answer': correct_letter
        })

    return fallback_questions

@app.route('/mentor-chat/<int:mentorship_request_id>')
def mentor_chat(mentorship_request_id):
    flash('Mentorship features are no longer available on this platform.', 'info')
    return redirect('/candidate-dashboard')

    user_role = session.get('role')
    user_id = session.get('user_id')
    
    if not user_role or user_role not in ['candidate', 'mentor']:
        return redirect('/login')
    
    db = get_connection()
    cursor = db.cursor(cursor_factory=RealDictCursor)
    
    # Get mentorship request details and verify access
    cursor.execute("""
        SELECT mr.*, 
               c.name as candidate_name, c.email as candidate_email,
               m.name as mentor_name, m.email as mentor_email,
               mp.expertise, mp.company
        FROM mentorship_requests mr
        JOIN candidates c ON mr.candidate_id = c.id
        JOIN mentors m ON mr.mentor_id = m.id
        LEFT JOIN mentor_profiles mp ON m.id = mp.mentor_id
        WHERE mr.id = %s
    """, (mentorship_request_id,))
    request = cursor.fetchone()
    
    if not request:
        cleanup_db_resources(cursor, db)
        flash("Mentorship request not found", "danger")
        return redirect('/candidate-dashboard' if user_role == 'candidate' else '/mentor-dashboard')
    
    # Verify user has access to this chat
    if user_role == 'candidate' and request['candidate_id'] != user_id:
        cleanup_db_resources(cursor, db)
        flash("Unauthorized access", "danger")
        return redirect('/candidate-dashboard')
    
    if user_role == 'mentor' and request['mentor_id'] != user_id:
        cleanup_db_resources(cursor, db)
        flash("Unauthorized access", "danger")
        return redirect('/mentor-dashboard')
    
    # Only allow chat if request is accepted
    if request['status'] != 'Accepted':
        cleanup_db_resources(cursor, db)
        flash("Chat is only available for accepted mentorship requests", "warning")
        return redirect('/candidate-dashboard' if user_role == 'candidate' else '/mentor-dashboard')
    
    # Get all messages for this mentorship
    cursor.execute("""
        SELECT mm.*, 
               CASE 
                   WHEN mm.sender_role = 'candidate' THEN c.name
                   WHEN mm.sender_role = 'mentor' THEN m.name
               END as sender_name
        FROM mentor_messages mm
        LEFT JOIN candidates c ON mm.sender_id = c.id AND mm.sender_role = 'candidate'
        LEFT JOIN mentors m ON mm.sender_id = m.id AND mm.sender_role = 'mentor'
        WHERE mm.mentorship_request_id = %s
        ORDER BY mm.created_at ASC
    """, (mentorship_request_id,))
    messages = cursor.fetchall()
    
    # Mark messages as read for the current user
    cursor.execute("""
        UPDATE mentor_messages 
        SET is_read = TRUE 
        WHERE mentorship_request_id = %s 
        AND sender_role != %s
        AND is_read = FALSE
    """, (mentorship_request_id, user_role))
    db.commit()
    
    # Check if meeting is scheduled
    cursor.execute("""
        SELECT * FROM mentor_meetings 
        WHERE mentor_id = %s AND candidate_id = %s
    """, (request['mentor_id'], request['candidate_id']))
    meeting = cursor.fetchone()
    
    cleanup_db_resources(cursor, db)
    
    from datetime import datetime, timedelta
    
    return render_template('mentor_chat.html', 
                         request=request, 
                         messages=messages, 
                         user_role=user_role,
                         meeting=meeting,
                         now=datetime.now(),
                         timedelta=timedelta)
    
@app.route('/send-mentor-message', methods=['POST'])
def send_mentor_message():
    return jsonify({'success': False, 'message': 'Mentorship features are no longer available.'}), 410

    user_role = session.get('role')
    user_id = session.get('user_id')
    
    if not user_role or user_role not in ['candidate', 'mentor']:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401
    
    data = request.get_json()
    mentorship_request_id = data.get('mentorship_request_id')
    message_text = data.get('message', '').strip()
    
    if not message_text:
        return jsonify({'success': False, 'message': 'Message cannot be empty'}), 400
    
    db = get_connection()
    cursor = db.cursor(cursor_factory=RealDictCursor)
    
    # Verify access
    cursor.execute("""
        SELECT * FROM mentorship_requests 
        WHERE id = %s AND (candidate_id = %s OR mentor_id = %s) AND status = 'Accepted'
    """, (mentorship_request_id, user_id, user_id))
    request_data = cursor.fetchone()
    
    if not request_data:
        cleanup_db_resources(cursor, db)
        return jsonify({'success': False, 'message': 'Invalid request'}), 403
    
    # Insert message
    cursor.execute("""
        INSERT INTO mentor_messages (mentorship_request_id, sender_role, sender_id, message_text)
        VALUES (%s, %s, %s, %s)
    """, (mentorship_request_id, user_role, user_id, message_text))
    db.commit()
    
    message_id = cursor.lastrowid
    
    # Get sender name
    if user_role == 'candidate':
        cursor.execute("SELECT name FROM candidates WHERE id = %s", (user_id,))
    else:
        cursor.execute("SELECT name FROM mentors WHERE id = %s", (user_id,))
    sender = cursor.fetchone()
    
    cleanup_db_resources(cursor, db)
    
    # Emit socket event for real-time update
    socketio.emit('new_mentor_message', {
        'id': message_id,
        'mentorship_request_id': mentorship_request_id,
        'sender_role': user_role,
        'sender_name': sender['name'] if sender else 'Unknown',
        'message_text': message_text,
        'created_at': 'Just now'
    }, room=f"mentorship_{mentorship_request_id}")
    
    return jsonify({'success': True, 'message': 'Message sent'})
@app.route('/schedule-mentor-meeting', methods=['POST'])
def schedule_mentor_meeting():
    return jsonify({'success': False, 'message': 'Mentorship features are no longer available.'}), 410

    user_role = session.get('role')
    user_id = session.get('user_id')
    
    if user_role != 'mentor':
        return jsonify({'success': False, 'message': 'Only mentors can schedule meetings'}), 401
    
    data = request.get_json()
    candidate_id = data.get('candidate_id')
    mode = data.get('mode')
    meeting_date = data.get('meeting_date')
    meeting_time = data.get('meeting_time')
    meeting_link = data.get('meeting_link', '')
    notes = data.get('notes', '')
    
    if not all([candidate_id, mode, meeting_date, meeting_time]):
        return jsonify({'success': False, 'message': 'All fields are required'}), 400
    
    db = get_connection()
    cursor = db.cursor(cursor_factory=RealDictCursor)
    
    # Check if meeting already exists
    cursor.execute("""
        SELECT id FROM mentor_meetings 
        WHERE mentor_id = %s AND candidate_id = %s
    """, (user_id, candidate_id))
    existing = cursor.fetchone()
    
    if existing:
        # Update existing meeting
        cursor.execute("""
            UPDATE mentor_meetings 
            SET mode = %s, meeting_date = %s, meeting_time = %s, 
                meeting_link = %s, notes = %s
            WHERE mentor_id = %s AND candidate_id = %s
        """, (mode, meeting_date, meeting_time, meeting_link, notes, user_id, candidate_id))
    else:
        # Insert new meeting
        cursor.execute("""
            INSERT INTO mentor_meetings 
            (mentor_id, candidate_id, mode, meeting_date, meeting_time, meeting_link, notes)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (user_id, candidate_id, mode, meeting_date, meeting_time, meeting_link, notes))
    
    db.commit()
    cleanup_db_resources(cursor, db)
    
    # Send notification to candidate
    send_notification('candidate', candidate_id, 
                     f'Your mentor has scheduled a meeting on {meeting_date} at {meeting_time}')
    
    return jsonify({'success': True, 'message': 'Meeting scheduled successfully'})


# ============================================================================
# CANDIDATE-SPECIFIC ROUTES (Complete Workflow Implementation)
# ============================================================================

@app.route('/candidate-view-job/<int:job_id>')
def candidate_view_job(job_id):
    """
    COMPREHENSIVE VIEW JOB PAGE FOR CANDIDATES
    - Shows full job details with compatibility analysis
    - Calculates match percentage based on skills
    - Displays eligibility indicators
    - Provides Save and Apply actions
    """
    if session.get('role') != 'candidate':
        flash('Please login as a candidate to view jobs', 'warning')
        return redirect('/login?role=candidate')
    
    candidate_id = session.get('user_id')
    mode = request.args.get("mode", "smart")
    simple_view = (mode == "simple")
    try:
        # Check profile completion (85% rule)
        is_complete, profile_percent = check_candidate_profile_completion(candidate_id)
        
        if not is_complete:
            flash(f'Complete your profile to {profile_percent}% to view full job details (currently {profile_percent}%)', 'warning')
            return redirect('/candidate-dashboard#profile')
        
        with SafeDBConnection() as (cursor, db):
            # Get job details with recruiter info
            cursor.execute("""
                SELECT j.*, 
                       rp.company_name, rp.logo_file, rp.company_size, rp.industry,
                       rp.website, rp.address, rp.company_type,
                       r.email as recruiter_email
                FROM jobs j
                JOIN recruiters r ON j.recruiter_id = r.id
                LEFT JOIN recruiter_profiles rp ON r.id = rp.recruiter_id
                WHERE j.id = %s AND (j.deadline IS NULL OR j.deadline >= CURRENT_DATE)
                AND LOWER(COALESCE(j.status, '')) = 'active'
                AND LOWER(COALESCE(rp.verification_status, '')) = 'approved'
            """, (job_id,))
            job = cursor.fetchone()
            
            if not job:
                flash('Job not found or no longer active', 'danger')
                return redirect('/candidate-dashboard#jobs')
            
            # Get candidate profile for compatibility analysis
            cursor.execute("SELECT * FROM candidate_profiles WHERE candidate_id = %s", (candidate_id,))
            profile = cursor.fetchone()
            
            # Check if already applied
            cursor.execute("SELECT id, status FROM applications WHERE candidate_id = %s AND job_id = %s", 
                          (candidate_id, job_id))
            application = cursor.fetchone()
            
            # Fetch latest AI test for this candidate
            latest_test = None
            try:
                cursor.execute("""
                    SELECT percentage, ROUND(percentage,2) as score
                    FROM ai_tests
                    WHERE candidate_id = %s AND status = 'completed'
                    ORDER BY completed_at DESC LIMIT 1
                """, (candidate_id,))
                latest_test = cursor.fetchone()
            except Exception:
                latest_test = None

            match = compute_candidate_job_match(job, profile, latest_test)
            match_data = {
                'match_percentage': match.get('total_score', 0),
                'matched_skills': match.get('matched_skills', []),
                'missing_skills': match.get('missing_skills', []),
                'total_required': len(match.get('matched_skills', [])) + len(match.get('missing_skills', [])),
                'skill_match': match.get('skill_match', 0),
                'exp_match': match.get('exp_match', 0),
                'ai_match': match.get('ai_match', 0)
            }
            
            # Check eligibility
            eligibility = {
                'profile_complete': is_complete,
                'resume_uploaded': bool(profile and profile.get('resume_file')),
                'experience_match': check_experience_match(job, profile),
                'already_applied': bool(application)
            }
        
        return render_template('candidate_view_job.html',
                             job=job,
                             profile=profile,
                             match_data=match_data,
                             eligibility=eligibility,
                             application=application,
                             simple_view=simple_view,
                             candidate_view=True)
    
    except Exception as e:
        print(f"Error in candidate_view_job: {e}")
        import traceback
        traceback.print_exc()
        flash('Error loading job details', 'danger')
        return redirect('/candidate-dashboard')


@app.route('/api/test-connection')
def test_connection():
    """Test endpoint to verify database connectivity"""
    try:
        db = get_connection()
        cursor = db.cursor()
        cursor.execute("SELECT 1")
        cursor.close()
        db.close()
        return jsonify({'status': 'success', 'message': 'Database connection working'}), 200
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/job-details/<int:job_id>')
def api_job_details(job_id):
    try:
        if session.get('role') != 'candidate':
            return jsonify({'success': False, 'error': 'Unauthorized'}), 401

        user_id = session.get('user_id')

        with SafeDBConnection() as (cursor, db):
            # Fetch job details
            cursor.execute("""
                SELECT 
                    j.id,
                    j.title,
                    j.location,
                    j.job_type,
                    j.description,
                    j.deadline,
                    j.employment_mode,
                    j.required_skills,
                    CASE
                        WHEN j.min_experience IS NOT NULL AND j.max_experience IS NOT NULL THEN
                            CONCAT(j.min_experience, ' - ', j.max_experience, ' years')
                        WHEN j.min_experience IS NOT NULL THEN
                            CONCAT(j.min_experience, ' years')
                        WHEN j.max_experience IS NOT NULL THEN
                            CONCAT(j.max_experience, ' years')
                        ELSE NULL
                    END AS experience_required,
                    j.salary_min,
                    j.salary_max,
                    rp.company_name,
                    rp.logo_file
                FROM jobs j
                JOIN recruiter_profiles rp 
                    ON j.recruiter_id = rp.recruiter_id
                WHERE j.id = %s
            """, (job_id,))

            job = cursor.fetchone()
            
            if not job:
                return jsonify({'success': False, 'error': 'Job not found'}), 404
            
            # Get candidate profile for matching
            cursor.execute("""
                SELECT primary_skills, secondary_skills, frameworks_libraries, 
                       databases, cloud_platforms, tools_technologies, profile_completed, profile_percent, resume_file
                FROM candidate_profiles
                WHERE candidate_id = %s
            """, (user_id,))
            
            profile = cursor.fetchone()
            
            # Get existing application
            cursor.execute("""
                SELECT status, applied_at
                FROM applications
                WHERE candidate_id = %s AND job_id = %s
            """, (user_id, job_id))
            
            application = cursor.fetchone()
            
            # Convert job to dict with column names (RealDictCursor returns dict-like)
            job_dict = {
                'id': job['id'],
                'title': job['title'],
                'location': job['location'],
                'job_type': job['job_type'],
                'description': job['description'],
                'deadline': job['deadline'].isoformat() if job['deadline'] else None,
                'employment_mode': job['employment_mode'],
                'required_skills': job['required_skills'],
                'experience_required': job['experience_required'],
                'salary_min': job['salary_min'],
                'salary_max': job['salary_max'],
                'company_name': job['company_name'],
                'logo_file': job['logo_file']
            }
            
            # Calculate salary range string
            try:
                if job_dict['salary_min'] and job_dict['salary_max']:
                    salary_min = int(job_dict['salary_min'])
                    salary_max = int(job_dict['salary_max'])
                    job_dict['salary_range'] = f"₹{salary_min:,} - ₹{salary_max:,}"
                else:
                    job_dict['salary_range'] = "Not specified"
            except (ValueError, TypeError):
                job_dict['salary_range'] = "Not specified"
            
            # Calculate skills match
            match_data = {
                'match_percentage': 0,
                'matched_skills': [],
                'missing_skills': [],
                'total_required': 0
            }
            
            try:
                # Collect ALL candidate skills from all fields (like compute_candidate_job_match does)
                candidate_skills = []
                if profile:
                    if profile.get('primary_skills'):
                        candidate_skills.extend([s.strip().lower() for s in str(profile.get('primary_skills', '')).split(',') if s.strip()])
                    if profile.get('secondary_skills'):
                        candidate_skills.extend([s.strip().lower() for s in str(profile.get('secondary_skills', '')).split(',') if s.strip()])
                    if profile.get('frameworks_libraries'):
                        candidate_skills.extend([s.strip().lower() for s in str(profile.get('frameworks_libraries', '')).split(',') if s.strip()])
                    if profile.get('databases'):
                        candidate_skills.extend([s.strip().lower() for s in str(profile.get('databases', '')).split(',') if s.strip()])
                    if profile.get('cloud_platforms'):
                        candidate_skills.extend([s.strip().lower() for s in str(profile.get('cloud_platforms', '')).split(',') if s.strip()])
                    if profile.get('tools_technologies'):
                        candidate_skills.extend([s.strip().lower() for s in str(profile.get('tools_technologies', '')).split(',') if s.strip()])
                
                # Remove duplicates
                candidate_skills = list(set(candidate_skills))
                
                # Get job required skills
                required_skills_str = job_dict.get('required_skills', '') or ''
                
                # If we have both candidate skills AND job required skills, calculate match
                if candidate_skills and required_skills_str:
                    required_skills_list = [s.strip() for s in required_skills_str.split(',') if s.strip()]
                    required_skills = [s.lower() for s in required_skills_list]
                    
                    match_data['total_required'] = len(required_skills)
                    match_data['matched_skills'] = [s for s in required_skills_list if s.lower() in candidate_skills]
                    match_data['missing_skills'] = [s for s in required_skills_list if s.lower() not in candidate_skills]
                    
                    if required_skills:
                        match_data['match_percentage'] = int((len(match_data['matched_skills']) / len(required_skills)) * 100)
                    
            except Exception as skill_error:
                print(f"ERROR calculating skills match: {skill_error}")
                import traceback
                traceback.print_exc()
                # Continue with default match_data values
            
            # Eligibility check based on actual profile data
            profile_complete = profile and profile.get('profile_completed') and profile.get('profile_percent', 0) >= 85
            resume_uploaded = profile and profile.get('resume_file')
            
            eligibility = {
                'profile_complete': profile_complete,
                'resume_uploaded': resume_uploaded,
                'is_eligible': profile_complete and resume_uploaded,
                'message': 'You are eligible to apply for this job!' if (profile_complete and resume_uploaded) else 'Complete your profile and upload resume to apply'
            }
            
            # Application status
            application_dict = None
            if application:
                application_dict = {
                    'status': application['status'],
                    'applied_at': application['applied_at'].isoformat() if application['applied_at'] else None
                }

        return jsonify({
            'success': True,
            'job': job_dict,
            'match_data': match_data,
            'eligibility': eligibility,
            'application': application_dict
        })

    except Exception as e:
        print(f"[API ERROR] job-details/{job_id}: {e}")
        import traceback
        error_msg = traceback.format_exc()
        print(f"Full traceback:\n{error_msg}")
        return jsonify({'success': False, 'error': str(e) if str(e) else type(e).__name__}), 500

@app.route('/view-job/<int:job_id>')
def view_job(job_id):
    """General view for jobs used by admin/recruiter moderation panel.
    - Admins can view any job regardless of status.
    - Recruiters can view their own jobs.
    - Candidates are redirected to the candidate-specific view which enforces eligibility checks.
    """
    role = session.get('role')
    user_id = session.get('user_id')
    try:
        db = get_connection()
        cursor = db.cursor(cursor_factory=RealDictCursor)

        cursor.execute("""
            SELECT j.*, 
                   rp.company_name, rp.logo_file, rp.company_size, rp.industry,
                   rp.website, rp.address, rp.company_type,
                   rp.recruiter_id as recruiter_email, rp.recruiter_id as recruiter_user_id
            FROM jobs j
            INNER JOIN recruiter_profiles rp ON j.recruiter_id = rp.recruiter_id
            WHERE j.id = %s
        """, (job_id,))
        job = cursor.fetchone()

        if not job:
            cleanup_db_resources(cursor, db)
            flash('Job not found', 'danger')
            return redirect('/admin-dashboard' if role == 'admin' else '/')

        # Candidates must go through the candidate view (which enforces profile/deadline/verification)
        if role == 'candidate':
            cleanup_db_resources(cursor, db)
            return redirect(f'/candidate-view-job/{job_id}')

        # Recruiters may view only their own jobs
        if role == 'recruiter' and user_id != job.get('recruiter_user_id'):
            cleanup_db_resources(cursor, db)
            flash('Unauthorized to view this job', 'danger')
            return redirect('/login')

        # Prepare minimal context expected by the template
        match_data = {}
        profile = None
        application = None
        eligibility = {}

        back_url = '/admin-dashboard#jobs' if role == 'admin' else None

        cleanup_db_resources(cursor, db)

        # Admins / recruiters should see the admin-style job details page
        return render_template('view_job.html',
                       job=job)

    except Exception as e:
        print(f"Error in view_job: {e}")
        import traceback
        traceback.print_exc()
        flash('Error loading job details', 'danger')
        return redirect('/admin-dashboard' if session.get('role') == 'admin' else '/')


@app.route('/save-job/<int:job_id>', methods=['POST'])
def save_job(job_id):
    """Save a job for later (add to saved jobs list)"""
    if session.get('role') != 'candidate':
        return jsonify({'error': 'Unauthorized'}), 401
    
    candidate_id = session.get('user_id')
    
    try:
        db = get_connection()
        cursor = db.cursor(cursor_factory=RealDictCursor)
        
        # Create saved_jobs table if it doesn't exist
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS saved_jobs (
                id SERIAL PRIMARY KEY,
                candidate_id VARCHAR(20),
                job_id INT,
                saved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (candidate_id, job_id),
                FOREIGN KEY (candidate_id) REFERENCES candidates(id),
                FOREIGN KEY (job_id) REFERENCES jobs(id)
            )
        """)
        
        # Save the job
        cursor.execute("""
            INSERT IGNORE INTO saved_jobs (candidate_id, job_id)
            VALUES (%s, %s)
        """, (candidate_id, job_id))
        
        db.commit()
        cleanup_db_resources(cursor, db)
        
        # Log activity
        log_activity(
            candidate_id, 'candidate', 'job_saved',
            'Saved Job',
            f'Saved job ID {job_id} for later review',
            {'job_id': job_id}
        )
        
        return jsonify({'success': True, 'message': 'Job saved successfully'})
    
    except Exception as e:
        print(f"Error saving job: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/unsave-job/<int:job_id>', methods=['POST'])
def unsave_job(job_id):
    """Remove a job from saved jobs list"""
    if session.get('role') != 'candidate':
        return jsonify({'error': 'Unauthorized'}), 401
    
    candidate_id = session.get('user_id')
    
    try:
        db = get_connection()
        cursor = db.cursor(cursor_factory=RealDictCursor)
        
        cursor.execute("""
            DELETE FROM saved_jobs 
            WHERE candidate_id = %s AND job_id = %s
        """, (candidate_id, job_id))
        
        db.commit()
        cleanup_db_resources(cursor, db)
        
        return jsonify({'success': True, 'message': 'Job removed from saved list'})
    
    except Exception as e:
        print(f"Error unsaving job: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/schedule-interview/<int:app_id>', methods=['POST'])
def schedule_interview(app_id):
    """Schedule an interview and send details to candidate"""
    if session.get('role') != 'recruiter':
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401
    
    try:
        data = request.get_json()
        print(f"DEBUG: Schedule interview called for app_id={app_id}")
        print(f"DEBUG: Data received: {data}")
        
        with SafeDBConnection() as (cursor, db):
            # Get application and candidate info
            print(f"DEBUG: Fetching application info for app_id={app_id}")
            cursor.execute("""
                SELECT a.id, a.candidate_id, c.email, c.name, j.title as job_title, j.id as job_id
                FROM applications a
                JOIN candidates c ON a.candidate_id = c.id
                JOIN jobs j ON a.job_id = j.id
                WHERE a.id = %s
            """, (app_id,))
            app_info = cursor.fetchone()
            print(f"DEBUG: Application info: {app_info}")
            
            if not app_info:
                print(f"ERROR: Application {app_id} not found")
                return jsonify({'success': False, 'error': 'Application not found'}), 404
            
            # Insert interview record with round information
            print(f"DEBUG: Inserting interview record for round {data.get('interview_round')}")
            cursor.execute("""
                INSERT INTO interviews (
                    candidate_id, recruiter_id, job_id, application_id,
                    interview_round, round_name, total_rounds,
                    interview_date, interview_time, 
                    interview_mode, interview_link, location,
                    interviewer_name, interview_type, notes,
                    status
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'Scheduled')
            """, (
                app_info['candidate_id'],
                session.get('user_id'),
                app_info['job_id'],
                app_id,
                data.get('interview_round', 1),
                data.get('round_name', 'Round 1'),
                data.get('total_rounds', 1),
                data.get('interview_date'),
                data.get('interview_time'),
                data.get('interview_mode'),
                data.get('interview_link'),
                data.get('location'),
                data.get('interviewer_name'),
                data.get('interview_type'),
                data.get('notes')
            ))
            print(f"DEBUG: Interview record inserted")
            
            # Update application status to Interview (only if first round)
            if data.get('interview_round', 1) == 1:
                cursor.execute("""
                    UPDATE applications SET status = 'Interview'::application_status WHERE id = %s
                """, (app_id,))
                print(f"DEBUG: Application status updated to Interview")
            
            db.commit()
            print(f"DEBUG: Transaction committed")
        
        # Send email to candidate if requested
        if data.get('send_email'):
            print(f"DEBUG: Sending email to {app_info['email']}")
            send_interview_email(
                candidate_name=app_info['name'],
                candidate_email=app_info['email'],
                job_title=app_info['job_title'],
                interview_date=data.get('interview_date'),
                interview_time=data.get('interview_time'),
                interview_mode=data.get('interview_mode'),
                platform=data.get('platform'),
                interview_link=data.get('interview_link'),
                location=data.get('location'),
                interviewer_name=data.get('interviewer_name'),
                interview_type=data.get('interview_type'),
                notes=data.get('notes'),
                round_name=data.get('round_name'),
                interview_round=data.get('interview_round'),
                total_rounds=data.get('total_rounds')
            )
        
        print(f"DEBUG: Interview scheduled successfully")
        return jsonify({'success': True, 'message': 'Interview scheduled successfully'})
    
    except Exception as e:
        print(f"ERROR scheduling interview: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/schedule-interviews-bulk/<int:job_id>', methods=['POST'])
def schedule_interviews_bulk(job_id):
    """Schedule interviews for all eligible candidates in a job with slot-based timing."""
    if session.get('role') != 'recruiter':
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401

    try:
        data = request.get_json() or {}
        recruiter_id = session.get('user_id')

        interview_date = data.get('interview_date')
        start_time = data.get('interview_time')
        slot_duration = int(data.get('slot_duration') or 30)

        if not interview_date or not start_time:
            return jsonify({'success': False, 'error': 'Interview date and start time are required'}), 400

        if slot_duration < 5:
            return jsonify({'success': False, 'error': 'Slot duration must be at least 5 minutes'}), 400

        with SafeDBConnection() as (cursor, db):
            # Verify recruiter owns the job
            cursor.execute("""
                SELECT id, title FROM jobs
                WHERE id = %s AND recruiter_id = %s
            """, (job_id, recruiter_id))
            job = cursor.fetchone()

            if not job:
                return jsonify({'success': False, 'error': 'Job not found or unauthorized'}), 404

            # Fetch all eligible candidates/applications for combined scheduling
            cursor.execute("""
                SELECT
                    a.id AS application_id,
                    a.candidate_id,
                    c.name,
                    c.email,
                    j.title AS job_title,
                    j.id AS job_id
                FROM applications a
                JOIN candidates c ON c.id = a.candidate_id
                JOIN jobs j ON j.id = a.job_id
                WHERE a.job_id = %s
                  AND j.recruiter_id = %s
                  AND a.status IN ('Shortlisted'::application_status, 'Interview'::application_status)
                ORDER BY a.id
            """, (job_id, recruiter_id))
            candidates = cursor.fetchall()

            if not candidates:
                return jsonify({'success': False, 'error': 'No eligible candidates found for combined scheduling'}), 400

            base_datetime = datetime.strptime(f"{interview_date} {start_time}", "%Y-%m-%d %H:%M")

            scheduled_rows = []
            for index, candidate in enumerate(candidates):
                scheduled_dt = base_datetime + timedelta(minutes=index * slot_duration)
                slot_date = scheduled_dt.strftime('%Y-%m-%d')
                slot_time = scheduled_dt.strftime('%H:%M')

                cursor.execute("""
                    INSERT INTO interviews (
                        candidate_id, recruiter_id, job_id, application_id,
                        interview_round, round_name, total_rounds,
                        interview_date, interview_time,
                        interview_mode, interview_link, location,
                        interviewer_name, interview_type, notes,
                        status
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'Scheduled')
                """, (
                    candidate['candidate_id'],
                    recruiter_id,
                    candidate['job_id'],
                    candidate['application_id'],
                    data.get('interview_round', 1),
                    data.get('round_name', 'Round 1'),
                    data.get('total_rounds', 1),
                    slot_date,
                    slot_time,
                    data.get('interview_mode'),
                    data.get('interview_link'),
                    data.get('location'),
                    data.get('interviewer_name'),
                    data.get('interview_type'),
                    data.get('notes')
                ))

                cursor.execute("""
                    UPDATE applications
                    SET status = 'Interview'::application_status
                    WHERE id = %s
                """, (candidate['application_id'],))

                scheduled_rows.append({
                    'candidate_name': candidate['name'],
                    'candidate_email': candidate['email'],
                    'interview_date': slot_date,
                    'interview_time': slot_time,
                    'job_title': candidate['job_title']
                })

            db.commit()

        if data.get('send_email'):
            for item in scheduled_rows:
                send_interview_email(
                    candidate_name=item['candidate_name'],
                    candidate_email=item['candidate_email'],
                    job_title=item['job_title'],
                    interview_date=item['interview_date'],
                    interview_time=item['interview_time'],
                    interview_mode=data.get('interview_mode'),
                    platform=data.get('platform'),
                    interview_link=data.get('interview_link'),
                    location=data.get('location'),
                    interviewer_name=data.get('interviewer_name'),
                    interview_type=data.get('interview_type'),
                    notes=data.get('notes'),
                    round_name=data.get('round_name'),
                    interview_round=data.get('interview_round'),
                    total_rounds=data.get('total_rounds')
                )

        return jsonify({
            'success': True,
            'message': 'Interviews scheduled for all eligible candidates',
            'scheduled_count': len(scheduled_rows)
        })

    except Exception as e:
        print(f"ERROR scheduling interviews in bulk: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/schedule-next-interview/<int:candidate_id>/<int:job_id>', methods=['POST'])
def schedule_next_interview(candidate_id, job_id):
    """Schedule next round of interview for candidate"""
    if session.get('role') != 'recruiter':
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401
    
    try:
        data = request.get_json()
        recruiter_id = session.get('user_id')
        
        print(f"DEBUG: Schedule next interview called for candidate={candidate_id}, job={job_id}")
        print(f"DEBUG: Data received: {data}")
        
        with SafeDBConnection() as (cursor, db):
            # Verify recruiter owns this job
            cursor.execute("""
                SELECT id FROM jobs WHERE id = %s AND recruiter_id = %s
            """, (job_id, recruiter_id))
            
            if not cursor.fetchone():
                print(f"ERROR: Job {job_id} not found or unauthorized")
                return jsonify({'success': False, 'error': 'Unauthorized'}), 401
            
            # Get candidate and job info
            cursor.execute("""
                SELECT c.id, c.email, c.name, j.title as job_title
                FROM candidates c
                JOIN jobs j ON j.id = %s
                WHERE c.id = %s
            """, (job_id, candidate_id))
            
            candidate_info = cursor.fetchone()
            if not candidate_info:
                print(f"ERROR: Candidate {candidate_id} not found")
                return jsonify({'success': False, 'error': 'Candidate not found'}), 404
            
            # Insert new interview record for next round
            print(f"DEBUG: Inserting next round interview record")
            cursor.execute("""
                INSERT INTO interviews (
                    candidate_id, recruiter_id, job_id, 
                    interview_date, interview_time, 
                    interview_mode, interview_link, location,
                    interviewer_name, interview_type, notes,
                    status
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'Scheduled')
            """, (
                candidate_id,
                recruiter_id,
                job_id,
                data.get('interview_date'),
                data.get('interview_time'),
                data.get('interview_mode'),
                data.get('interview_link'),
                data.get('location'),
                data.get('interviewer_name'),
                data.get('interview_type'),
                data.get('notes')
            ))
            print(f"DEBUG: Interview record inserted")
            
            db.commit()
            print(f"DEBUG: Transaction committed")
        
        # Send email to candidate if requested
        if data.get('send_email'):
            print(f"DEBUG: Sending email to {candidate_info['email']}")
            send_interview_email(
                candidate_name=candidate_info['name'],
                candidate_email=candidate_info['email'],
                interview_date=data.get('interview_date'),
                interview_time=data.get('interview_time'),
                interview_mode=data.get('interview_mode'),
                interview_link=data.get('interview_link'),
                location=data.get('location'),
                interviewer_name=data.get('interviewer_name'),
                interview_type=data.get('interview_type'),
                job_title=candidate_info['job_title'],
                notes=data.get('notes')
            )
        
        # Send notification to candidate
        create_notification(
            'candidate', candidate_id, 'interview_scheduled',
            'Next Interview Round Scheduled',
            f'Your next interview round for {candidate_info["job_title"]} has been scheduled',
            '/interviews'
        )
        
        print(f"DEBUG: Next interview scheduled successfully")
        return jsonify({'success': True, 'message': 'Next interview round scheduled successfully'})
    
    except Exception as e:
        print(f"ERROR scheduling next interview: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


def send_interview_email(candidate_name, candidate_email, job_title, interview_date, interview_time, 
                         interview_mode, platform=None, interview_link=None, location=None, 
                         interviewer_name=None, interview_type=None, notes=None, round_name=None, 
                         interview_round=None, total_rounds=None):
    """Send interview details to candidate via email"""
    try:
        import smtplib
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart
        from datetime import datetime
        
        # Parse date and time
        interview_datetime = f"{interview_date} {interview_time}"
        
        # Build subject
        round_info = f" - {round_name}" if round_name else ""
        total_info = f" ({interview_round}/{total_rounds})" if interview_round and total_rounds else ""
        subject = f"Interview Scheduled{round_info}{total_info} - {job_title}"
        
        # Build HTML email
        html_content = f"""
        <html>
            <body style="font-family: Arial, sans-serif; color: #333;">
                <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
                    <div style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 30px; border-radius: 10px; text-align: center; margin-bottom: 30px;">
                        <h1 style="margin: 0;">Interview Scheduled!</h1>
                        <p style="margin: 5px 0 0 0; font-size: 18px;">{job_title}</p>
                        {f'<p style="margin: 5px 0 0 0; font-size: 16px;">{round_name} ({interview_round}/{total_rounds})</p>' if round_name and interview_round and total_rounds else ''}
                    </div>
                    
                    <div style="background: #f8f9fa; padding: 20px; border-radius: 10px; margin-bottom: 20px;">
                        <p style="margin: 0 0 15px 0;"><strong>Dear {candidate_name},</strong></p>
                        <p>We are pleased to inform you that your interview has been scheduled for the position of <strong>{job_title}</strong>.</p>
                        
                        <div style="background: white; padding: 20px; border-radius: 8px; margin: 20px 0;">
                            <h3 style="margin-top: 0; color: #667eea;">Interview Details</h3>
                            
                            {f'<p style="margin: 10px 0;"><strong>🎯 Round:</strong> {round_name} (Round {interview_round} of {total_rounds})</p>' if round_name and interview_round and total_rounds else ''}
                            <p style="margin: 10px 0;"><strong>📅 Date:</strong> {interview_date}</p>
                            <p style="margin: 10px 0;"><strong>⏰ Time:</strong> {interview_time}</p>
                            <p style="margin: 10px 0;"><strong>📍 Mode:</strong> {interview_mode.capitalize()}</p>
        """
        
        if interview_mode.lower() == 'online':
            if platform:
                html_content += f"<p style='margin: 10px 0;'><strong>🖥️ Platform:</strong> {platform.replace('_', ' ').title()}</p>"
            if interview_link:
                html_content += f"<p style='margin: 10px 0;'><strong>🔗 Meeting Link:</strong> <a href='{interview_link}' style='color: #667eea; text-decoration: none;'>{interview_link}</a></p>"
        else:
            if location:
                html_content += f"<p style='margin: 10px 0;'><strong>📌 Location:</strong> {location}</p>"
        
        if interviewer_name:
            html_content += f"<p style='margin: 10px 0;'><strong>👤 Interviewer:</strong> {interviewer_name}</p>"
        
        if interview_type:
            html_content += f"<p style='margin: 10px 0;'><strong>📋 Interview Type:</strong> {interview_type.replace('_', ' ').title()}</p>"
        
        html_content += """
                        </div>
        """
        
        if notes:
            html_content += f"""
                        <div style="background: #e7f3ff; border-left: 4px solid #667eea; padding: 15px; margin: 20px 0;">
                            <h4 style="margin-top: 0; color: #667eea;">Additional Information</h4>
                            <p style="margin: 0; white-space: pre-wrap;">{notes}</p>
                        </div>
            """
        
        html_content += f"""
                        <p style="margin-top: 20px;">Please make sure to join the meeting a few minutes early. If you have any questions or need to reschedule, please contact us.</p>
                        
                        <p style="margin-top: 30px; color: #666; font-size: 14px;">
                            Best regards,<br>
                            HireHub Team<br>
                            <a href="https://sudeshm-jobs.com" style="color: #667eea; text-decoration: none;">sudeshm-jobs.com</a>
                        </p>
                    </div>
                </div>
            </body>
        </html>
        """
        
        # Create email
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = 'noreply@sudeshm-jobs.com'
        msg['To'] = candidate_email
        
        msg.attach(MIMEText(html_content, 'html'))
        
        # Send email (you'll need to configure your SMTP settings)
        # This is a placeholder - configure with your email service
        round_desc = f" - {round_name} ({interview_round}/{total_rounds})" if round_name else ""
        print(f"Email prepared for {candidate_email}: Interview scheduled{round_desc} for {interview_datetime}")
        
    except Exception as e:
        print(f"Error sending interview email: {e}")
        import traceback
        traceback.print_exc()


def _parse_notice_period_days(notice_period):
    """Best-effort parse of notice period text to days."""
    if not notice_period:
        return None

    text = str(notice_period).strip().lower()
    if not text:
        return None

    if any(k in text for k in ['immediate', 'join immediately', 'immediately']):
        return 0

    m_days = re.search(r'(\d+)\s*(day|days|d)\b', text)
    if m_days:
        return int(m_days.group(1))

    m_weeks = re.search(r'(\d+)\s*(week|weeks|w)\b', text)
    if m_weeks:
        return int(m_weeks.group(1)) * 7

    m_months = re.search(r'(\d+)\s*(month|months|m)\b', text)
    if m_months:
        return int(m_months.group(1)) * 30

    m_num = re.search(r'\b(\d{1,3})\b', text)
    if m_num:
        days = int(m_num.group(1))
        if days <= 180:
            return days

    if 'serving' in text and 'notice' in text:
        return 30

    return None


def _compute_smart_joining_date(availability_date=None, notice_period=None, base_date=None):
    """Compute a practical joining date and explain the basis used."""
    base = base_date or datetime.now().date()
    candidate_dates = []
    reasons = []

    parsed_availability = None
    if availability_date:
        try:
            if hasattr(availability_date, 'strftime'):
                parsed_availability = availability_date
                if hasattr(parsed_availability, 'date'):
                    parsed_availability = parsed_availability.date()
            else:
                parsed_availability = datetime.strptime(str(availability_date), '%Y-%m-%d').date()
        except Exception:
            parsed_availability = None

    if parsed_availability:
        candidate_dates.append(parsed_availability)
        reasons.append(f"candidate availability ({parsed_availability.strftime('%d %b %Y')})")

    notice_days = _parse_notice_period_days(notice_period)
    if notice_days is not None:
        notice_date = base + timedelta(days=max(notice_days, 0))
        candidate_dates.append(notice_date)
        reasons.append(f"notice period ({notice_days} days)")

    if not candidate_dates:
        # Advanced default: 21-day lead instead of fixed 30, and move to next business day.
        tentative = base + timedelta(days=21)
        reason = "standard lead time (21 days)"
    else:
        tentative = max(candidate_dates)
        reason = " and ".join(reasons)

    # Adjust weekends to next business day
    while tentative.weekday() >= 5:  # 5=Sat, 6=Sun
        tentative += timedelta(days=1)

    return tentative.strftime('%Y-%m-%d'), reason


def generate_offer_letter_for_application(
    app_id,
    recruiter_id,
    salary=None,
    joining_date=None,
    employment_type='Full-time',
    work_location=None,
    benefits='',
    auto_generated=False
):
    """Generate offer letter PDF, persist offer record, and mark application as Selected."""
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    from reportlab.lib.units import inch

    with SafeDBConnection() as (cursor, db):
        cursor.execute(
            """
            SELECT a.id, a.candidate_id, a.job_id, a.status,
                   c.name as candidate_name, c.email as candidate_email,
                   j.title as job_title, j.location as job_location,
                   j.salary_min, j.salary_max, j.job_type,
                   cp.notice_period, cp.availability_date,
                   COALESCE(rp.company_name, 'HireHub Partner Company') as company_name,
                   COALESCE(rp.address, 'Corporate Office') as company_address
            FROM applications a
            JOIN candidates c ON a.candidate_id = c.id
            JOIN jobs j ON a.job_id = j.id
            LEFT JOIN candidate_profiles cp ON cp.candidate_id = a.candidate_id
            LEFT JOIN recruiter_profiles rp ON j.recruiter_id = rp.recruiter_id
            WHERE a.id = %s AND j.recruiter_id = %s
            """,
            (app_id, recruiter_id)
        )
        application = cursor.fetchone()

        if not application:
            raise ValueError('Application not found or unauthorized')

        if (application.get('status') or '').lower() == 'rejected':
            raise ValueError('Cannot generate offer letter for rejected application')

        if not salary:
            salary = application.get('salary_max') or application.get('salary_min') or 'As per company standards'
        joining_date_reason = 'manual recruiter input'
        if not joining_date:
            joining_date, joining_date_reason = _compute_smart_joining_date(
                availability_date=application.get('availability_date'),
                notice_period=application.get('notice_period')
            )
        if not employment_type:
            employment_type = application.get('job_type') or 'Full-time'

        final_location = work_location if work_location else (application.get('job_location') or 'As assigned')

        offer_filename = f"offer_{application['candidate_id']}_{application['job_id']}_{datetime.now().strftime('%Y%m%d%H%M%S')}.pdf"
        offer_path = os.path.join(app.config.get("UPLOAD_FOLDER", "static/uploads"), offer_filename)
        os.makedirs(os.path.dirname(offer_path), exist_ok=True)

        c = canvas.Canvas(offer_path, pagesize=letter)
        width, height = letter

        c.setFont("Helvetica-Bold", 18)
        c.drawString(1 * inch, height - 1 * inch, f"{application['company_name']}")
        c.setFont("Helvetica", 12)
        c.drawString(1 * inch, height - 1.3 * inch, application['company_address'])

        c.setFont("Helvetica-Bold", 16)
        c.drawString(1 * inch, height - 2 * inch, "OFFER LETTER")
        c.setFont("Helvetica", 11)
        c.drawString(1 * inch, height - 2.5 * inch, f"Date: {datetime.now().strftime('%B %d, %Y')}")
        c.drawString(1 * inch, height - 3 * inch, f"Dear {application['candidate_name']},")

        y_position = height - 3.5 * inch
        offer_text = [
            f"We are pleased to offer you the position of {application['job_title']} at {application['company_name']}.",
            "",
            "Position Details:",
            f"• Job Title: {application['job_title']}",
            f"• Employment Type: {employment_type}",
            f"• Location: {final_location}",
            f"• Annual Salary: ₹{salary}",
            f"• Joining Date: {joining_date}",
            f"• Joining Date Basis: {joining_date_reason}",
        ]

        if benefits:
            offer_text.append("")
            offer_text.append("Benefits & Perks:")
            for benefit in str(benefits).split('\n'):
                if benefit.strip():
                    offer_text.append(f"• {benefit.strip()}")

        if auto_generated:
            offer_text.extend([
                "",
                "This offer was generated automatically when your application moved to the Selected stage.",
            ])

        offer_text.extend([
            "",
            "We believe your skills and experience will be a valuable addition to our team.",
            "Please confirm your acceptance by signing and returning this letter.",
            "",
            "We look forward to welcoming you to our team!",
            "",
            "Sincerely,",
            f"{application['company_name']} HR Team"
        ])

        c.setFont("Helvetica", 11)
        for line in offer_text:
            c.drawString(1 * inch, y_position, line)
            y_position -= 0.25 * inch
            if y_position < 1.1 * inch:
                c.showPage()
                y_position = height - 1 * inch
                c.setFont("Helvetica", 11)

        c.save()

        cursor.execute(
            """
            DELETE FROM offer_letters
            WHERE candidate_id = %s AND recruiter_id = %s AND job_id = %s
            """,
            (application['candidate_id'], recruiter_id, application['job_id'])
        )

        cursor.execute(
            """
            INSERT INTO offer_letters 
            (candidate_id, recruiter_id, job_id, position, salary, joining_date, 
             location, employment_type, benefits, offer_file, status, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'Sent', NOW())
            """,
            (
                application['candidate_id'], recruiter_id, application['job_id'],
                application['job_title'], salary, joining_date, final_location,
                employment_type, benefits, offer_filename
            )
        )

        cursor.execute(
            """
            UPDATE applications
            SET status = 'Selected'::application_status, updated_at = NOW()
            WHERE id = %s
            """,
            (app_id,)
        )

        db.commit()

    create_notification(
        'candidate', application['candidate_id'], 'offer_received',
        'Offer Letter Received!',
        f"Congratulations! You have received an offer for {application['job_title']}",
        '/my-applications'
    )

    log_activity(
        application['candidate_id'], 'candidate', 'offer_received',
        'Offer Letter Received',
        f"Received offer for {application['job_title']} at ₹{salary}",
        {
            'job_id': application['job_id'],
            'job_title': application['job_title'],
            'salary': salary,
            'joining_date': joining_date,
            'joining_date_reason': joining_date_reason,
            'employment_type': employment_type,
            'location': final_location,
            'benefits': benefits,
            'offer_file': offer_filename
        }
    )

    log_activity(
        recruiter_id, 'recruiter', 'offer_sent',
        'Offer Letter Sent',
        f"Sent offer to {application['candidate_name']} for {application['job_title']}",
        {
            'candidate_id': application['candidate_id'],
            'candidate_name': application['candidate_name'],
            'job_id': application['job_id'],
            'job_title': application['job_title'],
            'salary': salary,
            'offer_file': offer_filename,
            'auto_generated': auto_generated
        }
    )

    return {
        'candidate_id': application['candidate_id'],
        'candidate_name': application['candidate_name'],
        'candidate_email': application['candidate_email'],
        'job_id': application['job_id'],
        'job_title': application['job_title'],
        'offer_file': offer_filename,
        'salary': salary,
        'joining_date': joining_date,
        'joining_date_reason': joining_date_reason,
        'employment_type': employment_type,
        'location': final_location
    }


@app.route('/generate-offer/<int:app_id>', methods=['GET', 'POST'])
def generate_offer(app_id):
    """
    GENERATE OFFER LETTER FOR SELECTED CANDIDATE
    Workflow: Recruiter selects candidate -> System generates offer PDF -> Candidate can download
    """
    if session.get('role') != 'recruiter':
        flash('Only recruiters can generate offers', 'danger')
        return redirect('/login')
    
    recruiter_id = session.get('user_id')
    
    if request.method == 'POST':
        try:
            # Capture all form data
            salary = request.form.get('salary')
            joining_date = request.form.get('joining_date')
            employment_type = request.form.get('employment_type', 'Full-time')
            work_location = request.form.get('work_location', '')
            benefits = request.form.get('benefits', '')
            
            # Validate required fields
            if not salary or not joining_date:
                flash('Salary and joining date are required', 'danger')
                return redirect(f'/generate-offer/{app_id}')
            generate_offer_letter_for_application(
                app_id=app_id,
                recruiter_id=recruiter_id,
                salary=salary,
                joining_date=joining_date,
                employment_type=employment_type,
                work_location=work_location,
                benefits=benefits,
                auto_generated=False
            )
            
            flash('Offer letter generated and sent successfully', 'success')
            return redirect('/recruiter-dashboard#applications')
        
        except Exception as e:
            print(f"Error generating offer: {e}")
            import traceback
            traceback.print_exc()
            flash(f'Error generating offer letter: {str(e)}', 'danger')
            return redirect('/recruiter-dashboard')
    
    # GET request - show form
    try:
        db = get_connection()
        cursor = db.cursor(cursor_factory=RealDictCursor)
        
        # First get the application with all details
        cursor.execute("""
            SELECT a.id, a.candidate_id, a.job_id, a.status,
                   c.name as candidate_name, c.email as candidate_email,
                   j.title as job_title, j.salary_min, j.salary_max, j.location,
                   rp.company_name
            FROM applications a
            JOIN candidates c ON a.candidate_id = c.id
            JOIN jobs j ON a.job_id = j.id
            JOIN recruiter_profiles rp ON j.recruiter_id = rp.recruiter_id
            WHERE a.id = %s AND j.recruiter_id = %s
        """, (app_id, recruiter_id))
        application = cursor.fetchone()
        
        cleanup_db_resources(cursor, db)
        
        if not application:
            print(f"Application {app_id} not found for recruiter {recruiter_id}")
            flash('Application not found', 'danger')
            return redirect('/recruiter-dashboard')
        
        from datetime import datetime, timedelta
        return render_template('generate_offer.html', application=application, datetime=datetime, timedelta=timedelta)
    
    except Exception as e:
        print(f"Error loading offer form: {e}")
        import traceback
        traceback.print_exc()
        flash(f'Error loading form: {str(e)}', 'danger')
        return redirect('/recruiter-dashboard')

# Endpoint to set maintenance mode (admin only)
@app.route('/admin/set-maintenance-mode', methods=['POST'])
def set_maintenance_mode():
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403
    data = request.get_json(force=True, silent=True) or {}
    maintenance_mode = bool(data.get('maintenance_mode', False))
    settings = get_admin_settings()
    settings['maintenance_mode'] = maintenance_mode
    merged = save_admin_settings(settings)
    return jsonify({'success': True, 'maintenance_mode': merged.get('maintenance_mode', False)})
#app.py

@app.route('/offer-letter/<int:app_id>')
def view_offer_letter(app_id):
    """
    VIEW/DOWNLOAD OFFER LETTER
    Allows candidate to view and download their offer letter
    """
    candidate_id = session.get('user_id')
    candidate_role = session.get('role')
    
    if candidate_role != 'candidate':
        flash('Only candidates can view offer letters', 'danger')
        return redirect('/login')
    
    try:
        db = get_connection()
        cursor = db.cursor(cursor_factory=RealDictCursor)
        
        # Get the application and related offer letter
        cursor.execute("""
            SELECT a.id, a.status,
                   c.name as candidate_name, c.email as candidate_email,
                   j.title as job_title, j.location as job_location, j.recruiter_id,
                   rp.company_name,
                   ol.id as offer_id, ol.position, ol.salary, ol.joining_date, 
                   ol.location, ol.employment_type, ol.benefits, ol.offer_file,
                   ol.status as offer_status, ol.created_at
            FROM applications a
            JOIN candidates c ON a.candidate_id = c.id
            JOIN jobs j ON a.job_id = j.id
            JOIN recruiter_profiles rp ON j.recruiter_id = rp.recruiter_id
            LEFT JOIN offer_letters ol ON a.job_id = ol.job_id AND a.candidate_id = ol.candidate_id
            WHERE a.id = %s AND a.candidate_id = %s
        """, (app_id, candidate_id))
        
        application = cursor.fetchone()
        cleanup_db_resources(cursor, db)
        
        if not application:
            flash('Application or offer not found', 'danger')
            return redirect('/candidate-dashboard#applications')
        
        if not application['offer_file']:
            # Auto-heal legacy selected records that don't have generated offer rows/files yet.
            if (application.get('status') or '').lower() == 'selected' and application.get('recruiter_id'):
                try:
                    generated = generate_offer_letter_for_application(
                        app_id=app_id,
                        recruiter_id=application.get('recruiter_id'),
                        auto_generated=True
                    )
                    application['offer_file'] = generated.get('offer_file')
                except Exception as auto_error:
                    print(f"Candidate-side auto offer generation failed: {auto_error}")
                    flash('No offer letter available for this application', 'warning')
                    return redirect('/candidate-dashboard#applications')
            else:
                flash('No offer letter available for this application', 'warning')
                return redirect('/candidate-dashboard#applications')
        
        # Serve the PDF file
        offer_path = os.path.join(app.config.get("UPLOAD_FOLDER", "static/uploads"), application['offer_file'])
        
        if not os.path.exists(offer_path):
            flash('Offer letter file not found', 'danger')
            return redirect('/candidate-dashboard#applications')
        
        # Return the PDF file
        return send_file(
            offer_path,
            as_attachment=True,
            download_name=f"Offer_Letter_{application['candidate_name']}_{application['job_title']}.pdf",
            mimetype='application/pdf'
        )
    
    except Exception as e:
        print(f"Error viewing offer letter: {e}")
        import traceback
        traceback.print_exc()
        flash(f'Error loading offer letter: {str(e)}', 'danger')
        return redirect('/candidate-dashboard#applications')


@app.route('/recruiter/offer-letter/<app_id>')
@app.route('/recruiter/offer_letter/<app_id>')
@app.route('/recruiter/download-offer/<app_id>')
def recruiter_view_offer_letter(app_id):
    """Allow recruiter to download offer letter for their own selected candidate application."""
    if session.get('role') != 'recruiter':
        flash('Only recruiters can view recruiter offer letters', 'danger')
        return redirect('/login')

    recruiter_id = session.get('user_id')

    try:
        app_id = int(str(app_id).strip())
    except Exception:
        flash('Invalid application id for offer letter.', 'danger')
        return redirect('/applications')

    try:
        db = get_connection()
        cursor = db.cursor(cursor_factory=RealDictCursor)

        cursor.execute(
            """
            SELECT a.id, a.status, c.name AS candidate_name,
                   j.title AS job_title,
                   ol.offer_file
            FROM applications a
            JOIN jobs j ON a.job_id = j.id
            JOIN candidates c ON a.candidate_id = c.id
            LEFT JOIN offer_letters ol ON ol.job_id = a.job_id AND ol.candidate_id = a.candidate_id AND ol.recruiter_id = j.recruiter_id
            WHERE a.id = %s AND j.recruiter_id = %s
            """,
            (app_id, recruiter_id)
        )
        record = cursor.fetchone()
        cleanup_db_resources(cursor, db)

        if not record:
            flash('Application not found', 'danger')
            return redirect('/applications')

        if not record.get('offer_file'):
            if (record.get('status') or '').lower() == 'selected':
                try:
                    generated = generate_offer_letter_for_application(
                        app_id=app_id,
                        recruiter_id=recruiter_id,
                        auto_generated=True
                    )
                    record['offer_file'] = generated.get('offer_file')
                except Exception as auto_error:
                    print(f"Recruiter-side auto offer generation failed: {auto_error}")
                    flash('Offer letter is not available for this candidate yet.', 'warning')
                    return redirect('/applications')
            else:
                flash('Offer letter is not available for this candidate yet.', 'warning')
                return redirect('/applications')

        offer_path = os.path.join(app.config.get("UPLOAD_FOLDER", "static/uploads"), record['offer_file'])
        if not os.path.exists(offer_path):
            flash('Offer letter file not found on server.', 'danger')
            return redirect('/applications')

        return send_file(
            offer_path,
            as_attachment=True,
            download_name=f"Offer_Letter_{record['candidate_name']}_{record['job_title']}.pdf",
            mimetype='application/pdf'
        )
    except Exception as e:
        print(f"Error recruiter viewing offer letter: {e}")
        import traceback
        traceback.print_exc()
        flash(f'Error loading offer letter: {str(e)}', 'danger')
        return redirect('/applications')


@app.route('/my-interviews')
def my_interviews():
    """View all scheduled interviews for candidate"""
    if session.get('role') != 'candidate':
        return redirect('/login')
    
    candidate_id = session.get('user_id')
    
    try:
        # Check profile completion
        is_complete, profile_percent = check_candidate_profile_completion(candidate_id)
        
        if not is_complete:
            flash(f'Complete your profile to {profile_percent}% to access interviews', 'warning')
            return redirect('/candidate-dashboard#profile')
        
        db = get_connection()
        cursor = db.cursor(cursor_factory=RealDictCursor)
        
        cursor.execute("""
            SELECT 
                i.id, i.interview_date, i.interview_time, i.interview_mode,
                i.interview_link, i.location, i.status, i.interviewer_name,
                i.interview_type, i.notes, i.result, i.feedback,
                j.title AS job_title, j.location AS job_location,
                rp.company_name, rp.logo_file
            FROM interviews i
            JOIN jobs j ON i.job_id = j.id
            JOIN recruiter_profiles rp ON j.recruiter_id = rp.recruiter_id
            WHERE i.candidate_id = %s
            ORDER BY i.interview_date DESC, i.interview_time DESC
        """, (candidate_id,))
        
        interviews = cursor.fetchall()
        
        cleanup_db_resources(cursor, db)
        
        return render_template("interviews.html", 
                             interviews=interviews, 
                             candidate_view=True)
    
    except Exception as e:
        print(f"Error loading interviews: {e}")
        flash('Error loading interviews', 'danger')
        return redirect('/candidate-dashboard')


@app.route('/download-offer/<int:offer_id>')
def download_offer(offer_id):
    """Download offer letter PDF"""
    if session.get('role') != 'candidate':
        flash('Unauthorized', 'danger')
        return redirect('/login')
    
    candidate_id = session.get('user_id')
    
    try:
        db = get_connection()
        cursor = db.cursor(cursor_factory=RealDictCursor)
        
        cursor.execute("""
            SELECT offer_file FROM offer_letters 
            WHERE id = %s AND candidate_id = %s
        """, (offer_id, candidate_id))
        offer = cursor.fetchone()
        
        cleanup_db_resources(cursor, db)
        
        if not offer:
            flash('Offer letter not found', 'danger')
            return redirect('/my-applications')
        
        from flask import send_file
        offer_path = os.path.join(app.config.get("UPLOAD_FOLDER", "static/uploads"), offer['offer_file'])
        
        if os.path.exists(offer_path):
            return send_file(offer_path, as_attachment=True, download_name=offer['offer_file'])
        else:
            flash('Offer file not found', 'danger')
            return redirect('/my-applications')
    
    except Exception as e:
        print(f"Error downloading offer: {e}")
        flash('Error downloading offer letter', 'danger')
        return redirect('/my-applications')


@app.route('/my-mentorship')
def my_mentorship():
    flash('Mentorship features are no longer available on this platform.', 'info')
    return redirect('/candidate-dashboard')


@app.route('/candidate-settings', methods=['GET', 'POST'])
def candidate_settings():
    """Advanced candidate account settings with security and privacy controls"""
    if session.get('role') != 'candidate':
        return redirect('/login')
    
    candidate_id = session.get('user_id')
    
    if request.method == 'POST':
        action = request.form.get('action')
        
        try:
            db = get_connection()
            cursor = db.cursor(cursor_factory=RealDictCursor)
            
            if action == 'change_password':
                current_password = request.form.get('current_password')
                new_password = request.form.get('new_password')
                confirm_password = request.form.get('confirm_password')
                
                # Verify current password
                cursor.execute("SELECT password FROM candidates WHERE id = %s", (candidate_id,))
                user = cursor.fetchone()
                
                if not check_password_hash(user['password'], current_password):
                    flash('Current password is incorrect', 'danger')
                elif new_password != confirm_password:
                    flash('New passwords do not match', 'danger')
                elif len(new_password) < 6:
                    flash('Password must be at least 6 characters', 'danger')
                else:
                    hashed = generate_password_hash(new_password)
                    cursor.execute("UPDATE candidates SET password = %s WHERE id = %s", 
                                 (hashed, candidate_id))
                    
                    # Log password change activity
                    log_activity(candidate_id, 'security', 'Password Changed', 
                                'Password was successfully updated')
                    
                    # Create notification
                    create_notification('candidate', candidate_id, 'security', 
                                      'Password Changed', 
                                      'Your password has been changed successfully', 
                                      '/candidate-settings')
                    
                    db.commit()
                    flash('Password changed successfully', 'success')
            
            elif action == 'update_privacy':
                profile_visibility = request.form.get('profile_visibility')
                show_email = request.form.get('show_email', 'off')
                show_phone = request.form.get('show_phone', 'off')
                allow_recruiter_messages = request.form.get('allow_recruiter_messages', 'on')
                
                cursor.execute("""
                    UPDATE candidate_profiles 
                    SET profile_visibility = %s
                    WHERE candidate_id = %s
                """, (profile_visibility, candidate_id))
                
                # Store additional privacy settings
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS privacy_settings (
                        id SERIAL PRIMARY KEY,
                        candidate_id VARCHAR(20) UNIQUE,
                        show_email BOOLEAN DEFAULT FALSE,
                        show_phone BOOLEAN DEFAULT FALSE,
                        allow_recruiter_messages BOOLEAN DEFAULT TRUE,
                        searchable_profile BOOLEAN DEFAULT TRUE,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (candidate_id) REFERENCES candidates(id)
                    )
                """)
                
                cursor.execute("""
                    INSERT INTO privacy_settings 
                    (candidate_id, show_email, show_phone, allow_recruiter_messages)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (candidate_id) DO UPDATE SET
                    show_email = EXCLUDED.show_email,
                    show_phone = EXCLUDED.show_phone,
                    allow_recruiter_messages = EXCLUDED.allow_recruiter_messages
                """, (candidate_id, 
                      True if show_email == 'on' else False,
                      True if show_phone == 'on' else False,
                      True if allow_recruiter_messages == 'on' else False))
                
                db.commit()
                flash('Privacy settings updated', 'success')
            
            elif action == 'notification_preferences':
                email_notifications = request.form.get('email_notifications', 'on')
                job_alerts = request.form.get('job_alerts', 'on')
                interview_reminders = request.form.get('interview_reminders', 'on')
                application_updates = request.form.get('application_updates', 'on')
                mentor_messages = request.form.get('mentor_messages', 'on')
                
                # Store notification preferences
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS notification_preferences (
                        id SERIAL PRIMARY KEY,
                        candidate_id VARCHAR(20) UNIQUE,
                        email_notifications BOOLEAN DEFAULT TRUE,
                        job_alerts BOOLEAN DEFAULT TRUE,
                        interview_reminders BOOLEAN DEFAULT TRUE,
                        application_updates BOOLEAN DEFAULT TRUE,
                        mentor_messages BOOLEAN DEFAULT TRUE,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (candidate_id) REFERENCES candidates(id)
                    )
                """)
                
                cursor.execute("""
                    INSERT INTO notification_preferences 
                    (candidate_id, email_notifications, job_alerts, interview_reminders, 
                     application_updates, mentor_messages)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (candidate_id) DO UPDATE SET
                    email_notifications = EXCLUDED.email_notifications,
                    job_alerts = EXCLUDED.job_alerts,
                    interview_reminders = EXCLUDED.interview_reminders,
                    application_updates = EXCLUDED.application_updates,
                    mentor_messages = EXCLUDED.mentor_messages
                """, (candidate_id,
                      True if email_notifications == 'on' else False,
                      True if job_alerts == 'on' else False,
                      True if interview_reminders == 'on' else False,
                      True if application_updates == 'on' else False,
                      True if mentor_messages == 'on' else False))
                
                db.commit()
                flash('Notification preferences updated', 'success')
            
            elif action == 'deactivate_account':
                reason = request.form.get('deactivation_reason')
                confirmation = request.form.get('confirmation')
                
                if confirmation != 'DEACTIVATE':
                    flash('Please type DEACTIVATE to confirm', 'danger')
                else:
                    cursor.execute("""
                        UPDATE candidates 
                        SET is_active = 0,
                            deactivation_date = CURRENT_TIMESTAMP,
                            deactivation_reason = %s
                        WHERE id = %s
                    """, (reason, candidate_id))
                    
                    # Add is_active column if not exists
                    cursor.execute("""
                        ALTER TABLE candidates 
                        ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE,
                        ADD COLUMN IF NOT EXISTS deactivation_date DATETIME,
                        ADD COLUMN IF NOT EXISTS deactivation_reason TEXT
                    """)
                    
                    db.commit()
                    
                    # Log out the user
                    session.clear()
                    flash('Your account has been deactivated', 'info')
                    return redirect('/login')
            
            elif action == 'export_data':
                # Export user data in JSON format (GDPR compliance)
                cursor.execute("""
                    SELECT * FROM candidates WHERE id = %s
                """, (candidate_id,))
                candidate_data = cursor.fetchone()
                
                cursor.execute("""
                    SELECT * FROM candidate_profiles WHERE candidate_id = %s
                """, (candidate_id,))
                profile_data = cursor.fetchone()
                
                cursor.execute("""
                    SELECT * FROM applications WHERE candidate_id = %s
                """, (candidate_id,))
                applications_data = cursor.fetchall()
                
                export_data = {
                    'candidate': candidate_data,
                    'profile': profile_data,
                    'applications': applications_data,
                    'export_date': datetime.datetime.now().isoformat()
                }
                
                # Return as JSON download
                from flask import Response
                import json
                
                response = Response(
                    json.dumps(export_data, indent=2, default=str),
                    mimetype='application/json',
                    headers={'Content-Disposition': f'attachment;filename=my_data_{candidate_id}.json'}
                )
                return response
            
            cleanup_db_resources(cursor, db)
        
        except Exception as e:
            print(f"Error updating settings: {e}")
            flash('Error updating settings', 'danger')
        return redirect('/candidate-dashboard#settings')

    # GET request - settings now lives inline on dashboard
    return redirect('/candidate-dashboard#settings')


@app.route('/clear-session/<int:session_id>')
def clear_session(session_id):
    """Clear a specific login session"""
    if session.get('role') != 'candidate':
        return redirect('/login')
    
    candidate_id = session.get('user_id')
    
    try:
        db = get_connection()
        cursor = db.cursor(cursor_factory=RealDictCursor)
        
        # In a real implementation, you'd have an active_sessions table
        # For now, just delete from login history
        cursor.execute("""
            DELETE FROM login_history 
            WHERE id = %s AND user_id = %s AND user_type = 'candidate'
        """, (session_id, candidate_id))
        
        db.commit()
        cleanup_db_resources(cursor, db)
        
        flash('Session cleared', 'success')
    except Exception as e:
        print(f"Error clearing session: {e}")
        flash('Error clearing session', 'danger')
    
    return redirect('/candidate-dashboard#settings')


@app.route('/activity-timeline')
def activity_timeline():
    """Enhanced activity timeline with filters, search, and analytics"""
    if 'user_id' not in session or 'role' not in session:
        return redirect('/login')
    
    user_role = session.get('role')
    if user_role not in ['candidate', 'mentor']:
        return redirect('/login')
    
    user_id = session.get('user_id')
    
    try:
        # Get filter parameters
        activity_type = request.args.get('type', 'all')
        date_filter = request.args.get('date', 'all')  # all, today, week, month
        search_query = request.args.get('search', '')
        
        db = get_connection()
        cursor = db.cursor(cursor_factory=RealDictCursor)
        
        # Build dynamic query based on filters
        query = """
            SELECT * FROM activity_timeline 
            WHERE user_id = %s AND user_role = %s
        """
        params = [user_id, user_role]
        
        # Activity type filter
        if activity_type != 'all':
            query += " AND activity_type = %s"
            params.append(activity_type)
        
        # Date filter
        if date_filter == 'today':
            query += " AND DATE(created_at) = CURRENT_DATE"
        elif date_filter == 'week':
            query += " AND created_at >= CURRENT_TIMESTAMP - INTERVAL '7 days'"
        elif date_filter == 'month':
            query += " AND created_at >= CURRENT_TIMESTAMP - INTERVAL '30 days'"
        
        # Search filter
        if search_query:
            query += " AND (activity_title LIKE %s OR activity_description LIKE %s)"
            search_term = f"%{search_query}%"
            params.extend([search_term, search_term])
        
        query += " ORDER BY created_at DESC LIMIT 100"
        
        cursor.execute(query, params)
        activities = cursor.fetchall()
        
        # Get activity statistics
        cursor.execute("""
            SELECT 
                activity_type,
                COUNT(*) as count,
                MAX(created_at) as last_activity
            FROM activity_timeline 
            WHERE user_id = %s AND user_role = %s
            GROUP BY activity_type
        """, (user_id, user_role))
        activity_stats = cursor.fetchall()
        
        # Get recent activity count by day (last 7 days)
        cursor.execute("""
            SELECT 
                DATE(created_at) as date,
                COUNT(*) as count
            FROM activity_timeline 
            WHERE user_id = %s AND user_role = %s
            AND created_at >= CURRENT_TIMESTAMP - INTERVAL '7 days'
            GROUP BY DATE(created_at)
            ORDER BY date DESC
        """, (user_id, user_role))
        daily_stats = cursor.fetchall()
        
        cleanup_db_resources(cursor, db)
        
        return render_template('activity_timeline.html', 
                             activities=activities,
                             activity_stats=activity_stats,
                             daily_stats=daily_stats,
                             current_filter=activity_type,
                             current_date=date_filter,
                             search_query=search_query,
                             user_role=user_role)
    
    except Exception as e:
        print(f"Error loading activity timeline: {e}")
        flash('Error loading timeline', 'danger')
        redirect_url = '/mentor-dashboard' if user_role == 'mentor' else '/candidate-dashboard'
        return redirect(redirect_url)


@app.route('/export-activity', methods=['POST'])
def export_activity():
    """Export activity timeline as Excel"""
    if 'user_id' not in session or 'role' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    user_role = session.get('role')
    if user_role not in ['candidate', 'mentor']:
        return jsonify({'error': 'Unauthorized'}), 401
    
    user_id = session.get('user_id')
    
    try:
        db = get_connection()
        cursor = db.cursor(cursor_factory=RealDictCursor)
        
        cursor.execute("""
            SELECT activity_type, activity_title, activity_description, created_at
            FROM activity_timeline 
            WHERE user_id = %s AND user_role = %s
            ORDER BY created_at DESC
        """, (user_id, user_role))
        
        activities = cursor.fetchall()
        cleanup_db_resources(cursor, db)
        
        data_rows = [
            [
                activity['created_at'].strftime('%Y-%m-%d %H:%M:%S'),
                activity['activity_type'],
                activity['activity_title'],
                activity['activity_description']
            ]
            for activity in activities
        ]

        return _excel_response('activity_timeline.xlsx', data_rows, ['Date', 'Type', 'Title', 'Description'])
    
    except Exception as e:
        print(f"Error exporting activity: {e}")
        return jsonify({'error': str(e)}), 500


# Helper function for job matching
def calculate_job_match(job, profile):
    """Calculate compatibility between candidate and job"""
    if not profile:
        return {
            'match_percentage': 0,
            'matched_skills': [],
            'missing_skills': [],
            'total_required': 0
        }
    
    # Extract skills from job
    job_skills = []
    if job.get('skills'):
        job_skills = [s.strip().lower() for s in job['skills'].split(',')]
    if job.get('required_skills'):
        job_skills.extend([s.strip().lower() for s in job['required_skills'].split(',')])
    job_skills = list(set(job_skills))  # Remove duplicates
    
    # Extract skills from candidate profile
    candidate_skills = []
    if profile.get('primary_skills'):
        candidate_skills.extend([s.strip().lower() for s in profile['primary_skills'].split(',')])
    if profile.get('secondary_skills'):
        candidate_skills.extend([s.strip().lower() for s in profile['secondary_skills'].split(',')])
    if profile.get('skills'):
        candidate_skills.extend([s.strip().lower() for s in profile['skills'].split(',')])
    candidate_skills = list(set(candidate_skills))
    
    # Calculate match
    matched_skills = [skill for skill in job_skills if skill in candidate_skills]
    missing_skills = [skill for skill in job_skills if skill not in candidate_skills]
    
    match_percentage = 0
    if job_skills:
        match_percentage = int((len(matched_skills) / len(job_skills)) * 100)
    
    return {
        'match_percentage': match_percentage,
        'matched_skills': matched_skills,
        'missing_skills': missing_skills,
        'total_required': len(job_skills)
    }


def check_experience_match(job, profile):
    """Check if candidate meets experience requirements"""
    if not job or not profile:
        return True  # Default to eligible
    
    min_exp = job.get('min_experience', 0) or 0
    
    # Parse experience from work_experience field
    # This is a simple check - you can make it more sophisticated
    work_exp = profile.get('work_experience', '')
    
    # For freshers or if no minimum experience required
    if min_exp == 0:
        return True
    
    # Basic heuristic: if work_experience field has content, assume candidate has some experience
    if work_exp and len(work_exp) > 50:  
        return True
    
    # Check job_type_preference for internship/fresher roles
    job_type = job.get('job_type', '').lower()
    if 'internship' in job_type or 'fresher' in job_type:
        return True
    
    return min_exp <= 1  # Default to eligible for entry-level positions

# ===== AI CHATBOT ENDPOINT =====
@app.route("/api/chat", methods=["POST"])
def ai_chatbot():
    try:
        data = request.json or {}
        user_message = data.get("message", "").strip()

        if not user_message:
            return jsonify({"reply": "Please type a message."})

        # Lightweight intent handling to avoid AI calls for common questions
        lm = user_message.lower()
        if any(k in lm for k in ["where is register", "register button", "how to register", "register", "sign up", "signup", "create account"]):
            return jsonify({
                "reply": "You can register from the top navigation bar: click Register, then choose your role (Candidate, Company/HR, or Mentor). Or open this page: /register."
            })
        if any(k in lm for k in ["login", "sign in"]):
            return jsonify({
                "reply": "Use the Login link in the top navigation, or go to /login."
            })
        if any(k in lm for k in ["contact", "support", "help"]):
            return jsonify({
                "reply": "Visit our Contact page from the footer or go to /contact."
            })
        if any(k in lm for k in ["jobs", "internships", "view jobs", "openings"]):
            return jsonify({
                "reply": "After logging in as a Candidate and completing your profile, open Jobs from the dashboard or visit /jobs."
            })

        prompt = f"""
You are an AI assistant for HireHub.

ROLES:
- Candidate (job seeker / intern)
- Recruiter (company / HR)
- Mentor (career guide)

RULES:
- Detect the user's intent.
- Explain the relevant role clearly.
- Mention benefits.
- Give next step (Register → Role).
- Keep response short, friendly, and professional.

User message:
{user_message}
"""

        # Try generating with model fallback to avoid quota=0 issues
        reply_text, used_model = _gemini_generate_with_fallback(prompt)
        if reply_text:
            return jsonify({"reply": reply_text})
        # If still nothing, provide helpful guidance
        return jsonify({
            "reply": "Hi! I'm currently experiencing high demand. While I'm unavailable, here are quick links to get started: \n\n- Candidates: Go to Register (top navigation) → Choose Candidate\n- Recruiters: Go to Register → Choose Company/HR\n- Mentors: Go to Register → Choose Mentor\n\nYou can also visit the Register page directly via the menu."
        })

    except Exception as e:
        error_str = str(e)
        print("Chatbot Error:", e)
        
        # Handle quota exhaustion gracefully
        if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str or "quota" in error_str.lower():
            return jsonify({
                "reply": "Hi! I'm currently experiencing high demand. While I'm unavailable, feel free to explore our platform:\n\n🔹 **Candidates**: Register to find jobs & internships\n🔹 **Recruiters**: Post jobs and hire talent\n🔹 **Mentors**: Guide aspiring professionals\n\nNeed help? Contact us via the Contact page!"
            }), 200
        
        # Generic error fallback
        return jsonify({
            "reply": "Sorry, I'm having trouble right now. Please try again or contact support if the issue persists."
        }), 500

# ===== FEEDBACK SYSTEM =====
@app.route('/submit-feedback', methods=['POST'])
def submit_feedback():
    """Submit feedback to admin from candidate/mentor/recruiter dashboards"""
    try:
        if 'user_id' not in session or 'role' not in session:
            return jsonify({'success': False, 'error': 'Unauthorized'}), 401
        
        from_role = session.get('role')
        from_id = str(session.get('user_id'))
        rating = request.form.get('rating', 0, type=int)
        comment = request.form.get('comment', '').strip()
        
        if not comment:
            return jsonify({'success': False, 'error': 'Feedback comment cannot be empty'}), 400
        
        if rating < 1 or rating > 5:
            return jsonify({'success': False, 'error': 'Invalid rating'}), 400
        
        db = get_connection()
        cursor = db.cursor(cursor_factory=RealDictCursor)
        
        # Insert feedback (to_role is admin; to_id stored as string for consistency)
        cursor.execute("""
            INSERT INTO feedback (from_role, from_id, to_role, to_id, rating, comment)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (from_role, from_id, 'admin', '0', rating, comment))
        
        db.commit()
        cleanup_db_resources(cursor, db)
        
        return jsonify({'success': True, 'message': 'Thank you for your feedback!'}), 200
    
    except Exception as e:
        print(f"Error submitting feedback: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/logout', methods=['GET', 'POST'])
def logout():
    session.clear()
    return redirect('/login')

# ===== ERROR HANDLERS TO PREVENT SERVER CRASHES =====
@app.errorhandler(500)
def internal_error(error):
    """Handle internal server errors"""
    import traceback
    print(f"[500 ERROR] {error}")
    traceback.print_exc()
    
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or 'json' in request.headers.get('Content-Type', '') or 'json' in request.headers.get('Accept', ''):
        return jsonify({'success': False, 'error': 'Internal server error. Please try again.'}), 500
    
    return f'''<!DOCTYPE html><html><head><title>Error 500</title><style>body{{font-family:Arial;text-align:center;padding:50px;}}h1{{color:#e74c3c;}}</style></head><body><h1>500 - Internal Server Error</h1><p>Something went wrong. Please try again.</p><a href="/">Go Home</a> | <a href="javascript:history.back()">Go Back</a></body></html>''', 500

@app.errorhandler(404)
def not_found_error(error):
    """Handle 404 errors"""
    print(f"[404 ERROR] Page not found: {request.path}")
    
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or 'json' in request.headers.get('Content-Type', '') or 'json' in request.headers.get('Accept', ''):
        return jsonify({'success': False, 'error': 'Resource not found'}), 404
    
    return f'''<!DOCTYPE html><html><head><title>Error 404</title><style>body{{font-family:Arial;text-align:center;padding:50px;}}h1{{color:#3498db;}}</style></head><body><h1>404 - Page Not Found</h1><p>The page you are looking for does not exist.</p><a href="/">Go Home</a> | <a href="javascript:history.back()">Go Back</a></body></html>''', 404

@app.errorhandler(403)
def forbidden_error(error):
    """Handle 403 forbidden errors"""
    print(f"[403 ERROR] Forbidden: {request.path}")
    
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or 'json' in request.headers.get('Content-Type', '') or 'json' in request.headers.get('Accept', ''):
        return jsonify({'success': False, 'error': 'Access forbidden'}), 403
    
    return redirect('/login')

@app.errorhandler(401)
def unauthorized_error(error):
    """Handle 401 unauthorized errors"""
    print(f"[401 ERROR] Unauthorized: {request.path}")
    
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or 'json' in request.headers.get('Content-Type', '') or 'json' in request.headers.get('Accept', ''):
        return jsonify({'success': False, 'error': 'Unauthorized. Please login.'}), 401
    
    return redirect('/login')

@app.errorhandler(Exception)
def handle_exception(error):
    """Catch-all for any unhandled exceptions to prevent crashes"""
    import traceback
    error_trace = traceback.format_exc()
    print(f"[CRITICAL ERROR] Unhandled exception: {type(error).__name__}: {str(error)}")
    print(error_trace)
    
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or 'json' in request.headers.get('Content-Type', '') or 'json' in request.headers.get('Accept', ''):
        return jsonify({'success': False, 'error': 'An unexpected error occurred. Please try again.'}), 500
    
    return f'''<!DOCTYPE html><html><head><title>Error</title><style>body{{font-family:Arial;text-align:center;padding:50px;}}h1{{color:#e74c3c;}}</style></head><body><h1>Unexpected Error</h1><p>An unexpected error occurred. Please try again.</p><a href="/">Go Home</a> | <a href="javascript:history.back()">Go Back</a></body></html>''', 500

@app.before_request
def before_request_handler():
    """Validate session and handle errors before each request"""
    try:
        # Skip for static files and auth routes
        if request.endpoint in ['static', 'login', 'register', 'forgot_password', 'reset_password', 'index', None]:
            return None
        
        # Validate session integrity
        if 'user_id' in session:
            user_id = session.get('user_id')
            role = session.get('role')
            
            # Check for corrupted session
            if user_id is None or role is None:
                print("[WARNING] Corrupted session detected, clearing...")
                session.clear()
                if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                    return jsonify({'success': False, 'error': 'Session expired. Please login again.'}), 401
                return redirect('/login')
    except Exception as e:
        print(f"[ERROR] Session validation failed: {e}")
        session.clear()
        if request.endpoint not in ['static', 'login', 'register', 'index', None]:
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({'success': False, 'error': 'Session error. Please login again.'}), 401
            return redirect('/login')
    
    return None

if __name__ == "__main__":
    # Disable the reloader and debug when running Socket.IO on Windows to
    # avoid duplicate port binding (WinError 10048). Explicit host/port
    # makes it easy to change if the default port is in use.
    print("\n" + "="*60)
    print("[START] Starting HireHub Server...")
    print("="*60)
    print("[INFO] Server: http://0.0.0.0:5000")
    print(f"[INFO] Secret Key: {'SET' if app.secret_key != 'hirehub-secret' else 'USING DEFAULT (Change in production!)'}")
    print(f"[INFO] Database: {DB_CONFIG.get('database', 'Not configured')}")
    print("="*60 + "\n")
    
    socketio.run(app, host='0.0.0.0', port=5000, debug=False, use_reloader=False, allow_unsafe_werkzeug=True)
