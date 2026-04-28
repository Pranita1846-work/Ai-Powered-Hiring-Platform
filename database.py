# database.py
import psycopg2
from psycopg2 import pool
from psycopg2 import extensions
from psycopg2.extras import RealDictCursor
from werkzeug.security import generate_password_hash
import os
import sys
from dotenv import load_dotenv

# Load environment variables
load_dotenv()


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _resolve_database_url():
    """Resolve DATABASE_URL and ensure sslmode is present for cloud databases."""
    database_url = (
        os.environ.get("DATABASE_URL")
        or os.environ.get("POSTGRES_URL")
        or os.environ.get("SUPABASE_DATABASE_URL")
        or os.environ.get("SUPABASE_DB_URL")
        or os.environ.get("DATABASE_URI")
        or os.environ.get("SQLALCHEMY_DATABASE_URI")
    )
    if not database_url:
        return None

    db_sslmode = os.environ.get("DB_SSLMODE")
    if "sslmode=" not in database_url.lower():
        sslmode_value = db_sslmode if db_sslmode else "require"
        separator = "&" if "?" in database_url else "?"
        database_url = f"{database_url}{separator}sslmode={sslmode_value}"

    return database_url


DATABASE_URL = _resolve_database_url()

IS_PRODUCTION = _env_flag("IS_PRODUCTION", False)

def _resolve_sslmode(default_for_cloud: str = "require"):
    return os.environ.get("DB_SSLMODE", default_for_cloud)


# Load database configuration from environment variable
if DATABASE_URL:
    DB_CONFIG = {
        "dsn": DATABASE_URL,
        "driver": "PostgreSQL",
        "connection_timeout": 30,
        "uses_dsn": True
    }
else:
    db_host = os.environ.get("DB_HOST") or os.environ.get("PGHOST")
    db_user = os.environ.get("DB_USER") or os.environ.get("PGUSER")
    db_password = os.environ.get("DB_PASSWORD") or os.environ.get("PGPASSWORD")
    db_name = os.environ.get("DB_NAME") or os.environ.get("PGDATABASE")
    db_port = os.environ.get("DB_PORT") or os.environ.get("PGPORT")

    # In production, avoid silently pointing at localhost unless the deployer
    # explicitly configured local database values.
    if IS_PRODUCTION and not db_host:
        db_host = ""

    DB_CONFIG = {
        "host": db_host or ("localhost" if not IS_PRODUCTION else ""),
        "user": db_user or ("postgres" if not IS_PRODUCTION else ""),
        "password": db_password or ("root" if not IS_PRODUCTION else ""),
        "database": db_name or ("hiring_platform" if not IS_PRODUCTION else ""),
        "port": db_port or ("5432" if not IS_PRODUCTION else ""),
        "driver": "PostgreSQL",
        "connection_timeout": 30,
        "sslmode": _resolve_sslmode("require" if IS_PRODUCTION else "disable"),
        "uses_dsn": False
    }


def _database_config_is_valid() -> bool:
    if DB_CONFIG.get("uses_dsn"):
        return bool(DB_CONFIG.get("dsn"))
    required = ["host", "user", "password", "database", "port"]
    return all(str(DB_CONFIG.get(key) or "").strip() for key in required)

# Create connection pool for better performance
connection_pool = None
_DIRECT_CONNECTION_IDS = set()


def _safe_log(message: str):
    """Best-effort logging that won't crash during interpreter shutdown."""
    try:
        if sys.is_finalizing():
            return
        print(message, flush=True)
    except Exception:
        # Ignore logging failures during shutdown/finalization.
        pass


def _is_supabase_dsn() -> bool:
    dsn = (DB_CONFIG.get("dsn") or "").lower()
    host = str(DB_CONFIG.get("host") or "").lower()
    return (
        "supabase.co" in dsn
        or "pooler.supabase" in dsn
        or "supabase.co" in host
        or "pooler.supabase" in host
    )

def init_connection_pool():
    """Initialize PostgreSQL connection pool"""
    global connection_pool
    try:
        if not _database_config_is_valid():
            _safe_log(
                "[WARNING] Database configuration is incomplete. "
                "Set DATABASE_URL (recommended) or DB_HOST/DB_USER/DB_PASSWORD/DB_NAME/DB_PORT."
            )
            connection_pool = None
            return

        if _is_supabase_dsn():
            default_min = 1
            default_max = 3
        elif DB_CONFIG.get("uses_dsn"):
            default_min = 1
            default_max = 5
        else:
            default_min = 1
            default_max = 10

        min_conn = int(os.environ.get("DB_POOL_MIN", default_min))
        max_conn = int(os.environ.get("DB_POOL_MAX", default_max))
        if max_conn < min_conn:
            max_conn = min_conn

        if DB_CONFIG.get("uses_dsn"):
            connection_pool = pool.SimpleConnectionPool(
                min_conn,
                max_conn,
                dsn=DB_CONFIG["dsn"]
            )
        else:
            connection_pool = pool.SimpleConnectionPool(
                min_conn,  # minconn - minimum number of connections to keep open
                max_conn,  # maxconn - maximum number of connections allowed
                host=DB_CONFIG["host"],
                user=DB_CONFIG["user"],
                password=DB_CONFIG["password"],
                database=DB_CONFIG["database"],
                port=DB_CONFIG["port"],
                sslmode=DB_CONFIG["sslmode"],
                connect_timeout=10
            )
        print(f"PostgreSQL connection pool created successfully! (Min: {min_conn}, Max: {max_conn})")
    except Exception as e:
        print(f"Error creating connection pool: {e}")
        connection_pool = None

def get_connection():
    """Get a connection from the pool"""
    global connection_pool
    if connection_pool is None:
        init_connection_pool()

    if not _database_config_is_valid():
        raise Exception(
            "Database configuration is incomplete. Set DATABASE_URL (recommended) or "
            "DB_HOST/DB_USER/DB_PASSWORD/DB_NAME/DB_PORT in the deployment environment."
        )
    
    max_retries = int(os.environ.get("DB_CONNECT_RETRIES", "2"))
    retry_count = 0
    
    while retry_count < max_retries:
        try:
            if connection_pool:
                try:
                    conn = connection_pool.getconn()
                    # Test if connection is alive
                    if conn and not conn.closed:
                        # Quick connection test
                        try:
                            cur = conn.cursor()
                            cur.execute('SELECT 1')
                            cur.close()
                            return conn
                        except:
                            # Connection is dead, try to get a new one
                            try:
                                connection_pool.putconn(conn, close=True)
                            except Exception:
                                if not conn.closed:
                                    conn.close()
                            continue
                except Exception as pool_error:
                    print(f"[WARNING] Pool error (attempt {retry_count + 1}/{max_retries}): {pool_error}")
                    retry_count += 1
                    if retry_count >= max_retries:
                        print(f"[INFO] Attempting direct connection fallback...")
                        # Fallback to direct connection if pool is exhausted
                        if DB_CONFIG.get("uses_dsn"):
                            conn = psycopg2.connect(DB_CONFIG["dsn"])
                            _DIRECT_CONNECTION_IDS.add(id(conn))
                            return conn
                        conn = psycopg2.connect(
                            host=DB_CONFIG["host"],
                            user=DB_CONFIG["user"],
                            password=DB_CONFIG["password"],
                            database=DB_CONFIG["database"],
                            port=DB_CONFIG["port"],
                            sslmode=DB_CONFIG["sslmode"],
                            connect_timeout=10
                        )
                        _DIRECT_CONNECTION_IDS.add(id(conn))
                        return conn
                    import time
                    time.sleep(min(2.0, 0.5 * retry_count))  # small progressive backoff
                    continue
            else:
                if DB_CONFIG.get("uses_dsn"):
                    conn = psycopg2.connect(DB_CONFIG["dsn"])
                    _DIRECT_CONNECTION_IDS.add(id(conn))
                    return conn
                conn = psycopg2.connect(
                    host=DB_CONFIG["host"],
                    user=DB_CONFIG["user"],
                    password=DB_CONFIG["password"],
                    database=DB_CONFIG["database"],
                    port=DB_CONFIG["port"],
                    sslmode=DB_CONFIG["sslmode"],
                    connect_timeout=10
                )
                _DIRECT_CONNECTION_IDS.add(id(conn))
                return conn
        except Exception as e:
            print(f"[ERROR] Error getting connection (attempt {retry_count + 1}/{max_retries}): {e}")
            retry_count += 1
            if retry_count >= max_retries:
                raise Exception(f"Failed to establish database connection after {max_retries} attempts: {str(e)}")
            import time
            time.sleep(min(2.0, 0.5 * retry_count))
    
    raise Exception("Failed to get database connection")

def return_connection(conn):
    """Return connection to pool"""
    global connection_pool
    if conn:
        try:
            direct_connection = id(conn) in _DIRECT_CONNECTION_IDS
            if direct_connection:
                _DIRECT_CONNECTION_IDS.discard(id(conn))

            # Rollback any failed transactions before returning to pool
            if not conn.closed:
                try:
                    # Check transaction status
                    if conn.get_transaction_status() != psycopg2.extensions.TRANSACTION_STATUS_IDLE:
                        conn.rollback()
                        _safe_log("[INFO] Auto-rollback performed on connection return")
                except Exception as rollback_err:
                    _safe_log(f"[WARNING] Error during auto-rollback: {rollback_err}")
                    try:
                        conn.close()
                        return
                    except:
                        pass
            
            if direct_connection:
                if not conn.closed:
                    conn.close()
            elif connection_pool and not conn.closed:
                try:
                    connection_pool.putconn(conn)
                except Exception as e:
                    _safe_log(f"Error putting connection back to pool: {e}")
                    # If can't return to pool, close it
                    try:
                        conn.close()
                    except:
                        pass
            elif not conn.closed:
                # If no pool, just close the connection
                conn.close()
        except Exception as e:
            _safe_log(f"Error returning connection to pool: {e}")
            # Try to close the connection if returning to pool failed
            try:
                if conn and not conn.closed:
                    conn.close()
            except Exception as close_err:
                _safe_log(f"Error closing connection: {close_err}")

def cleanup_db_resources(cursor, connection):
    """Properly clean up database resources - cursor FIRST, then connection"""
    if cursor:
        try:
            cursor.close()
        except Exception as e:
            _safe_log(f"Error closing cursor: {e}")
    
    if connection:
        try:
            # Rollback any uncommitted/failed transaction before cleanup
            if not connection.closed:
                try:
                    if connection.get_transaction_status() == psycopg2.extensions.TRANSACTION_STATUS_INERROR:
                        connection.rollback()
                        _safe_log("[INFO] Rolled back failed transaction during cleanup")
                except Exception as rollback_err:
                    _safe_log(f"[WARNING] Error during cleanup rollback: {rollback_err}")
            
            return_connection(connection)
        except Exception as e:
            _safe_log(f"Error returning connection: {e}")

def close_connection_pool():
    """Close and cleanup the entire connection pool"""
    global connection_pool
    if connection_pool:
        try:
            connection_pool.closeall()
            _safe_log("[OK] Connection pool closed successfully!")
        except Exception as e:
            _safe_log(f"[ERROR] Error closing connection pool: {e}")
        finally:
            connection_pool = None

def create_database():
    """Create the database if it doesn't exist"""
    if DB_CONFIG.get("uses_dsn"):
        # Managed DBs (Supabase/Render/Neon) already provide the database.
        return

    try:
        print(f"[INFO] Connecting to database server at {DB_CONFIG['host']}...")
        conn = psycopg2.connect(
            host=DB_CONFIG["host"],
            user=DB_CONFIG["user"],
            password=DB_CONFIG["password"],
            port=DB_CONFIG["port"],
            database="postgres",
            sslmode=DB_CONFIG["sslmode"],
            connect_timeout=10
        )
        conn.autocommit = True
        cursor = conn.cursor()
        
        # Check if database exists
        cursor.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s",
            (DB_CONFIG["database"],)
        )
        if not cursor.fetchone():
            cursor.execute(f"CREATE DATABASE {DB_CONFIG['database']}")
            print("[OK] Database '" + DB_CONFIG['database'] + "' created!")
        else:
            print("[OK] Database '" + DB_CONFIG['database'] + "' already exists!")
        
        cursor.close()
        conn.close()
    except Exception as e:
        print("[ERROR] Error creating database: " + str(e))

def create_tables():
    """Create all required tables in PostgreSQL"""
    create_database()
    conn = get_connection()
    cursor = conn.cursor()

    try:
        # Keep startup fast to avoid deployment port timeouts.
        cursor.execute("SET statement_timeout = '30s'")
        print("[INFO] Fast startup schema init (30s timeout)")

        essential_tables = [
            """CREATE TABLE IF NOT EXISTS admins (
                id VARCHAR(20) PRIMARY KEY,
                name VARCHAR(100),
                email VARCHAR(120) UNIQUE NOT NULL,
                password VARCHAR(255) NOT NULL,
                profile_completed BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_admins_email ON admins(email);""",
            """CREATE TABLE IF NOT EXISTS candidates (
                id VARCHAR(20) PRIMARY KEY,
                name VARCHAR(100),
                email VARCHAR(120) UNIQUE NOT NULL,
                password VARCHAR(255) NOT NULL,
                profile_completed BOOLEAN DEFAULT FALSE,
                is_active BOOLEAN DEFAULT TRUE,
                is_blocked BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_candidates_email ON candidates(email);""",
            """CREATE TABLE IF NOT EXISTS recruiters (
                id VARCHAR(20) PRIMARY KEY,
                name VARCHAR(100),
                email VARCHAR(120) UNIQUE NOT NULL,
                password VARCHAR(255) NOT NULL,
                profile_completed BOOLEAN DEFAULT FALSE,
                is_blocked BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_recruiters_email ON recruiters(email);""",
            """CREATE TABLE IF NOT EXISTS mentors (
                id VARCHAR(20) PRIMARY KEY,
                name VARCHAR(100),
                email VARCHAR(120) UNIQUE NOT NULL,
                password VARCHAR(255) NOT NULL,
                profile_completed BOOLEAN DEFAULT FALSE,
                is_blocked BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_mentors_email ON mentors(email);""",
            """CREATE TABLE IF NOT EXISTS jobs (
                id SERIAL PRIMARY KEY,
                recruiter_id VARCHAR(20) NOT NULL,
                title VARCHAR(150),
                location VARCHAR(150),
                status VARCHAR(50) DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (recruiter_id) REFERENCES recruiters(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_jobs_recruiter ON jobs(recruiter_id);"""
        ]

        for table_sql in essential_tables:
            try:
                cursor.execute(table_sql)
                conn.commit()
            except Exception as table_error:
                conn.rollback()
                print(f"[INFO] Essential table skipped: {str(table_error)[:100]}")

        print("[OK] Essential tables created")

        # Optional fast-only mode for constrained startup environments.
        # Default is full schema creation so all features/tables are available.
        fast_only = os.environ.get("DB_FAST_SCHEMA_ONLY", "false").lower() == "true"
        if fast_only:
            print("[INFO] DB_FAST_SCHEMA_ONLY=true, skipping full schema creation")
            return

        # Recruiters table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS recruiters (
            id VARCHAR(20) PRIMARY KEY,
            name VARCHAR(100),
            email VARCHAR(120) UNIQUE NOT NULL,
            password VARCHAR(255) NOT NULL,
            profile_completed BOOLEAN DEFAULT FALSE,
            is_blocked BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_recruiters_email ON recruiters(email);
        """)

        # Recruiter profiles table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS recruiter_profiles (
            id SERIAL PRIMARY KEY,
            recruiter_id VARCHAR(20) UNIQUE NOT NULL,
            full_name VARCHAR(100),
            designation VARCHAR(100),
            company_name VARCHAR(150),
            phone VARCHAR(20),
            website VARCHAR(255),
            company_doc VARCHAR(255),
            auth_doc VARCHAR(255),
            linkedin VARCHAR(255),
            company_type VARCHAR(100),
            company_size VARCHAR(50),
            industry VARCHAR(100),
            address VARCHAR(255),
            logo_file VARCHAR(255),
            roles TEXT,
            experience_levels TEXT,
            job_types TEXT,
            profile_percent INTEGER DEFAULT 0,
            verification_status VARCHAR(50) DEFAULT 'pending',
            work_email VARCHAR(255),
            recruiting_experience INTEGER,
            specialization TEXT,
            languages TEXT,
            company_registration VARCHAR(21),
            founded_year INTEGER,
            headquarters_location VARCHAR(255),
            company_linkedin VARCHAR(255),
            hiring_locations TEXT,
            specific_locations TEXT,
            interview_mode VARCHAR(50),
            geo_tag_pdf VARCHAR(255),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (recruiter_id) REFERENCES recruiters(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_recruiter_profiles_recruiter ON recruiter_profiles(recruiter_id);
        CREATE INDEX IF NOT EXISTS idx_recruiter_profiles_verification ON recruiter_profiles(verification_status);
        """)

        # Mentors table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS mentors (
            id VARCHAR(20) PRIMARY KEY,
            name VARCHAR(100),
            email VARCHAR(120) UNIQUE NOT NULL,
            password VARCHAR(255) NOT NULL,
            profile_completed BOOLEAN DEFAULT FALSE,
            is_blocked BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_mentors_email ON mentors(email);
        """)
        conn.commit()
        print("[OK] Recruiter and mentor base tables created")

        # Mentor profiles table - split into smaller statements to avoid timeout
        try:
            # Part 1: Create base table with essential columns
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS mentor_profiles (
                id SERIAL PRIMARY KEY,
                mentor_id VARCHAR(20) UNIQUE NOT NULL,
                expertise VARCHAR(255),
                mentoring_areas TEXT,
                mode VARCHAR(50),
                experience INTEGER,
                designation VARCHAR(100),
                company VARCHAR(150),
                linkedin VARCHAR(255),
                session_duration VARCHAR(50),
                max_candidates INTEGER,
                communication VARCHAR(50),
                bio TEXT,
                available_days VARCHAR(50),
                time_slot VARCHAR(50),
                verification_type VARCHAR(50),
                verification_file VARCHAR(255),
                verification_status VARCHAR(50) DEFAULT 'pending',
                profile_percent INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (mentor_id) REFERENCES mentors(id) ON DELETE CASCADE
            );
            """)
            conn.commit()
            print("[OK] Mentor profiles base table created")
            
            # Part 2: Add optional columns via ALTER TABLE
            optional_columns = [
                "photo_file VARCHAR(255)",
                "mentor_category VARCHAR(100)",
                "highest_qualification VARCHAR(255)",
                "\"current_role\" VARCHAR(255)",
                "overall_experience INTEGER",
                "session_format VARCHAR(50)",
                "preferred_languages VARCHAR(255)",
                "professional_email VARCHAR(255)",
                "upi_id VARCHAR(100)",
                "mentor_price INTEGER DEFAULT 99",
                "mentor_declaration BOOLEAN DEFAULT FALSE",
                "agreement_accepted BOOLEAN DEFAULT FALSE",
                "agreement_accepted_at TIMESTAMP",
                "agreement_signature_name VARCHAR(255)",
                "agreement_signature_at TIMESTAMP",
                "agreement_signature_image BYTEA",
                "agreement_signature_image_mime VARCHAR(100)",
                "agreement_signature_image_name VARCHAR(255)",
                "agreement_pdf BYTEA",
                "agreement_pdf_mime VARCHAR(100)",
                "agreement_pdf_name VARCHAR(255)",
                "rejection_reason TEXT"
            ]
            
            for col_def in optional_columns:
                try:
                    cursor.execute(f"ALTER TABLE mentor_profiles ADD COLUMN IF NOT EXISTS {col_def};")
                    conn.commit()
                except Exception as col_err:
                    conn.rollback()
                    # Continue if column already exists
                    pass
            
            # Part 3: Create indexes
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_mentor_profiles_mentor ON mentor_profiles(mentor_id);")
            conn.commit()
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_mentor_profiles_verification ON mentor_profiles(verification_status);")
            conn.commit()
            print("[OK] Mentor profiles table, columns, and indexes created")
            
        except Exception as e:
            conn.rollback()
            print(f"[WARNING] Mentor profiles table creation: {str(e)[:100]}")

        # Jobs table
        try:
            cursor.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id SERIAL PRIMARY KEY,
            recruiter_id VARCHAR(20) NOT NULL,
            title VARCHAR(150),
            department VARCHAR(100),
            location VARCHAR(150),
            job_type VARCHAR(50),
            employment_mode VARCHAR(50),
            salary_min VARCHAR(100),
            salary_max VARCHAR(100),
            min_experience INTEGER,
            max_experience INTEGER,
            experience_required VARCHAR(100),
            required_skills TEXT,
            education VARCHAR(150),
            openings INTEGER,
            deadline DATE,
            description TEXT,
            skills TEXT,
            interview_mode VARCHAR(50),
            status VARCHAR(50) DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (recruiter_id) REFERENCES recruiters(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_jobs_recruiter ON jobs(recruiter_id);
        CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
        CREATE INDEX IF NOT EXISTS idx_jobs_deadline ON jobs(deadline);
        """)
            conn.commit()
            print("[OK] Jobs table created")
        except Exception as e:
            conn.rollback()
            print(f"[WARNING] Jobs table creation: {str(e)[:100]}")

        # Create ENUM type for application status
        try:
            cursor.execute("""
        DO $$ BEGIN
            CREATE TYPE application_status AS ENUM ('Applied','Accepted','Test Sent','Test Completed','Shortlisted','Interview','Interview Completed','Group Discussion','GD Completed','Selected','Rejected');
        EXCEPTION WHEN duplicate_object THEN null;
        END $$;
        """)
            conn.commit()
            print("[OK] Application status enum created")
        except Exception as e:
            conn.rollback()
            print(f"[INFO] Enum creation: {str(e)[:80]}")
        
        # Applications table
        try:
            cursor.execute("""
        CREATE TABLE IF NOT EXISTS applications (
            id SERIAL PRIMARY KEY,
            candidate_id VARCHAR(20) NOT NULL,
            job_id INTEGER NOT NULL,
            status application_status DEFAULT 'Applied',
            applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            rejection_reason TEXT,
            FOREIGN KEY (candidate_id) REFERENCES candidates(id) ON DELETE CASCADE,
            FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_applications_candidate ON applications(candidate_id);
        CREATE INDEX IF NOT EXISTS idx_applications_job ON applications(job_id);
        CREATE INDEX IF NOT EXISTS idx_applications_status ON applications(status);
        """)
            conn.commit()
            print("[OK] Applications table created")
        except Exception as e:
            conn.rollback()
            print(f"[WARNING] Applications table creation: {str(e)[:100]}")

        # Interviews table
        try:
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS interviews (
                id SERIAL PRIMARY KEY,
                candidate_id VARCHAR(20),
                recruiter_id VARCHAR(20),
                job_id INTEGER,
                application_id INTEGER,
                interview_round INTEGER DEFAULT 1,
                round_name VARCHAR(100),
                total_rounds INTEGER DEFAULT 1,
                interview_date DATE,
                interview_time TIME,
                interview_mode VARCHAR(50),
                interview_link VARCHAR(255),
                location TEXT,
                status VARCHAR(50) DEFAULT 'Scheduled',
                interviewer_name VARCHAR(150),
                interview_type VARCHAR(100),
                notes TEXT,
                result VARCHAR(50) DEFAULT 'Pending',
                feedback TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (candidate_id) REFERENCES candidates(id) ON DELETE CASCADE,
                FOREIGN KEY (recruiter_id) REFERENCES recruiters(id) ON DELETE CASCADE,
                FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
            );
            """)
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"[INFO] Interviews table creation: {str(e)[:100]}")
        
        # Create indexes for interviews table separately
        try:
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_interviews_candidate ON interviews(candidate_id);")
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"[INFO] Index creation skipped: {str(e)[:80]}")
        
        try:
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_interviews_recruiter ON interviews(recruiter_id);")
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"[INFO] Index creation skipped: {str(e)[:80]}")
        
        try:
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_interviews_application ON interviews(application_id);")
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"[INFO] Index creation skipped: {str(e)[:80]}")
        
        # Try to add application_id foreign key constraint if it doesn't exist
        try:
            cursor.execute("""
            ALTER TABLE interviews
            ADD CONSTRAINT fk_interviews_application
            FOREIGN KEY (application_id) REFERENCES applications(id) ON DELETE CASCADE
            """)
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"[INFO] Foreign key constraint: {str(e)[:80]}")

        # Mentorship requests table
        try:
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS mentorship_requests (
                id SERIAL PRIMARY KEY,
                candidate_id VARCHAR(20) NOT NULL,
                mentor_id VARCHAR(20) NOT NULL,
                request_message TEXT,
                mentor_feedback TEXT,
                status VARCHAR(50) DEFAULT 'Pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (candidate_id) REFERENCES candidates(id) ON DELETE CASCADE,
                FOREIGN KEY (mentor_id) REFERENCES mentors(id) ON DELETE CASCADE
            );
            """)
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"[INFO] Mentorship requests table: {str(e)[:100]}")
        
        # Create indexes for mentorship_requests
        try:
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_mentorship_requests_candidate ON mentorship_requests(candidate_id);")
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"[INFO] Index creation skipped: {str(e)[:80]}")
        
        try:
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_mentorship_requests_mentor ON mentorship_requests(mentor_id);")
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"[INFO] Index creation skipped: {str(e)[:80]}")

        # Mentorship payments table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS mentorship_payments (
            id SERIAL PRIMARY KEY,
            mentorship_request_id INTEGER UNIQUE NOT NULL,
            candidate_id VARCHAR(20) NOT NULL,
            mentor_id VARCHAR(20) NOT NULL,
            amount NUMERIC(10,2) NOT NULL,
            currency VARCHAR(10) DEFAULT 'INR',
            admin_share NUMERIC(10,2) NOT NULL,
            mentor_share NUMERIC(10,2) NOT NULL,
            mentor_payout_amount NUMERIC(10,2) NOT NULL,
            payout_status VARCHAR(20) DEFAULT 'pending',
            payment_status VARCHAR(20) DEFAULT 'completed',
            payment_reference VARCHAR(100),
            payout_reference VARCHAR(100),
            paid_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (mentorship_request_id) REFERENCES mentorship_requests(id) ON DELETE CASCADE,
            FOREIGN KEY (candidate_id) REFERENCES candidates(id) ON DELETE CASCADE,
            FOREIGN KEY (mentor_id) REFERENCES mentors(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_mentorship_payments_candidate ON mentorship_payments(candidate_id);
        CREATE INDEX IF NOT EXISTS idx_mentorship_payments_mentor ON mentorship_payments(mentor_id);
        CREATE INDEX IF NOT EXISTS idx_mentorship_payments_status ON mentorship_payments(payment_status);
        """)

        # Mentor meetings table
        try:
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS mentor_meetings (
                id SERIAL PRIMARY KEY,
                mentor_id VARCHAR(20) NOT NULL,
                candidate_id VARCHAR(20) NOT NULL,
                mode VARCHAR(50),
                meeting_date DATE,
                meeting_time TIME,
                meeting_link VARCHAR(255),
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(mentor_id, candidate_id),
                FOREIGN KEY (mentor_id) REFERENCES mentors(id) ON DELETE CASCADE,
                FOREIGN KEY (candidate_id) REFERENCES candidates(id) ON DELETE CASCADE
            );
            """)
            conn.commit()
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_mentor_meetings_mentor ON mentor_meetings(mentor_id);")
            conn.commit()
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_mentor_meetings_candidate ON mentor_meetings(candidate_id);")
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"[INFO] Mentor meetings table: {str(e)[:100]}")

        # Mentor availability table
        try:
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS mentor_availability (
                id SERIAL PRIMARY KEY,
                mentor_id VARCHAR(20) NOT NULL,
                day_of_week VARCHAR(10),
                start_time TIME,
                end_time TIME,
                is_available BOOLEAN DEFAULT TRUE,
                max_sessions_per_day INTEGER DEFAULT 3,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(mentor_id, day_of_week, start_time),
                FOREIGN KEY (mentor_id) REFERENCES mentors(id) ON DELETE CASCADE
            );
            """)
            conn.commit()
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_mentor_availability_mentor ON mentor_availability(mentor_id);")
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"[INFO] Mentor availability table: {str(e)[:100]}")

        # Notifications table
        try:
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS notifications (
                id SERIAL PRIMARY KEY,
                receiver_role VARCHAR(20),
                receiver_id VARCHAR(20),
                notification_type VARCHAR(100),
                title VARCHAR(255),
                message TEXT,
                action_url VARCHAR(255),
                is_read BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)
            conn.commit()
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_notifications_receiver ON notifications(receiver_id, receiver_role);")
            conn.commit()
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_notifications_read ON notifications(is_read);")
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"[INFO] Notifications table: {str(e)[:100]}")

        # AI Tests table
        try:
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS ai_tests (
                id SERIAL PRIMARY KEY,
                candidate_id VARCHAR(20) NOT NULL,
                skills_tested TEXT,
                test_type VARCHAR(50) DEFAULT 'Technical',
                total_questions INTEGER DEFAULT 25,
                total_marks INTEGER DEFAULT 50,
                obtained_marks INTEGER DEFAULT 0,
                percentage FLOAT DEFAULT 0,
                status VARCHAR(50) DEFAULT 'in_progress',
                tab_switch_count INTEGER DEFAULT 0,
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP NULL,
                FOREIGN KEY (candidate_id) REFERENCES candidates(id) ON DELETE CASCADE
            );
            """)
            conn.commit()
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_ai_tests_candidate ON ai_tests(candidate_id);")
            conn.commit()
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_ai_tests_status ON ai_tests(status);")
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"[INFO] AI tests table: {str(e)[:100]}")

        # AI Test questions table
        try:
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS ai_test_questions (
                id SERIAL PRIMARY KEY,
                test_id INTEGER NOT NULL,
                question_number INTEGER,
                question_text TEXT,
                section VARCHAR(50) DEFAULT 'technical',
                option_a TEXT,
                option_b TEXT,
                option_c TEXT,
                option_d TEXT,
                correct_answer VARCHAR(1),
                candidate_answer VARCHAR(1),
                is_correct BOOLEAN DEFAULT FALSE,
                FOREIGN KEY (test_id) REFERENCES ai_tests(id) ON DELETE CASCADE
            );
            """)
            conn.commit()
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_ai_test_questions_test ON ai_test_questions(test_id);")
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"[INFO] AI test questions table: {str(e)[:100]}")

        # Recruiter assessments table - Links recruiter-assigned tests to applications
        try:
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS recruiter_assessments (
                id SERIAL PRIMARY KEY,
                recruiter_id VARCHAR(20) NOT NULL,
                candidate_id VARCHAR(20) NOT NULL,
                job_id INTEGER NOT NULL,
                application_id INTEGER NOT NULL,
                test_id INTEGER,
                assessment_type VARCHAR(50) DEFAULT 'combined',
                skills_tested TEXT,
                scheduled_for TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                due_at TIMESTAMP,
                status VARCHAR(50) DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (recruiter_id) REFERENCES recruiters(id) ON DELETE CASCADE,
                FOREIGN KEY (candidate_id) REFERENCES candidates(id) ON DELETE CASCADE,
                FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE,
                FOREIGN KEY (application_id) REFERENCES applications(id) ON DELETE CASCADE,
                FOREIGN KEY (test_id) REFERENCES ai_tests(id) ON DELETE SET NULL
            );
            """)
            conn.commit()
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_recruiter_assessments_recruiter ON recruiter_assessments(recruiter_id);")
            conn.commit()
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_recruiter_assessments_candidate ON recruiter_assessments(candidate_id);")
            conn.commit()
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_recruiter_assessments_job ON recruiter_assessments(job_id);")
            conn.commit()
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_recruiter_assessments_application ON recruiter_assessments(application_id);")
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"[INFO] Recruiter assessments table: {str(e)[:100]}")

        # Interview feedback table - Stores panel feedback for interviews
        try:
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS interview_feedback (
                id SERIAL PRIMARY KEY,
                interview_id INTEGER NOT NULL,
                candidate_id VARCHAR(20) NOT NULL,
                recruiter_id VARCHAR(20) NOT NULL,
                job_id INTEGER NOT NULL,
                technical_score FLOAT DEFAULT 0,
                communication_score FLOAT DEFAULT 0,
                problem_solving_score FLOAT DEFAULT 0,
                cultural_fit_score FLOAT DEFAULT 0,
                overall_score FLOAT DEFAULT 0,
                strengths TEXT,
                weaknesses TEXT,
                recommendation VARCHAR(50),
                detailed_feedback TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (interview_id) REFERENCES interviews(id) ON DELETE CASCADE,
                FOREIGN KEY (candidate_id) REFERENCES candidates(id) ON DELETE CASCADE,
                FOREIGN KEY (recruiter_id) REFERENCES recruiters(id) ON DELETE CASCADE,
                FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
            );
            """)
            conn.commit()
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_interview_feedback_interview ON interview_feedback(interview_id);")
            conn.commit()
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_interview_feedback_candidate ON interview_feedback(candidate_id);")
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"[INFO] Interview feedback table: {str(e)[:100]}")

        # Group discussions table - Stores group discussion records and scores
        try:
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS group_discussions (
                id SERIAL PRIMARY KEY,
                candidate_id VARCHAR(20) NOT NULL,
                recruiter_id VARCHAR(20) NOT NULL,
                job_id INTEGER NOT NULL,
                discussion_topic TEXT,
                discussion_date DATE,
                discussion_time TIME,
                mode VARCHAR(50),
                meeting_link VARCHAR(255),
                location TEXT,
                leadership_score FLOAT DEFAULT 0,
                communication_score FLOAT DEFAULT 0,
                teamwork_score FLOAT DEFAULT 0,
                critical_thinking_score FLOAT DEFAULT 0,
                overall_score FLOAT DEFAULT 0,
                feedback TEXT,
                status VARCHAR(50) DEFAULT 'Scheduled',
                result VARCHAR(50) DEFAULT 'Pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (candidate_id) REFERENCES candidates(id) ON DELETE CASCADE,
                FOREIGN KEY (recruiter_id) REFERENCES recruiters(id) ON DELETE CASCADE,
                FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
            );
            """)
            conn.commit()
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_group_discussions_candidate ON group_discussions(candidate_id);")
            conn.commit()
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_group_discussions_recruiter ON group_discussions(recruiter_id);")
            conn.commit()
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_group_discussions_job ON group_discussions(job_id);")
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"[INFO] Group discussions table: {str(e)[:100]}")

        # Mock interviews table
        try:
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS mock_interviews (
                id SERIAL PRIMARY KEY,
                candidate_id VARCHAR(20) NOT NULL,
                job_role VARCHAR(255),
                difficulty_level VARCHAR(50),
                total_questions INTEGER DEFAULT 5,
                status VARCHAR(50) DEFAULT 'in_progress',
                overall_score FLOAT DEFAULT 0,
                confidence_score FLOAT DEFAULT 0,
                communication_score FLOAT DEFAULT 0,
                technical_score FLOAT DEFAULT 0,
                body_language_score FLOAT DEFAULT 0,
                avg_response_time FLOAT DEFAULT 0,
                filler_words_count INTEGER DEFAULT 0,
                eye_contact_percentage FLOAT DEFAULT 0,
                positive_emotions_percentage FLOAT DEFAULT 0,
                strengths TEXT,
                weaknesses TEXT,
                recommendations TEXT,
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP NULL,
                FOREIGN KEY (candidate_id) REFERENCES candidates(id) ON DELETE CASCADE
            );
            """)
            conn.commit()
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_mock_interviews_candidate ON mock_interviews(candidate_id);")
            conn.commit()
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_mock_interviews_status ON mock_interviews(status);")
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"[INFO] Mock interviews table: {str(e)[:100]}")

        # Mock interview questions table
        try:
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS mock_interview_questions (
                id SERIAL PRIMARY KEY,
                interview_id INTEGER NOT NULL,
                question_number INTEGER,
                question_text TEXT,
                question_type VARCHAR(50),
                answer_text TEXT,
                answer_duration FLOAT DEFAULT 0,
                confidence_level VARCHAR(50),
                emotion_detected VARCHAR(50),
                speaking_pace VARCHAR(50),
                filler_words INTEGER DEFAULT 0,
                clarity_score FLOAT DEFAULT 0,
                relevance_score FLOAT DEFAULT 0,
                ai_feedback TEXT,
                improvement_tips TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (interview_id) REFERENCES mock_interviews(id) ON DELETE CASCADE
            );
            """)
            conn.commit()
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_mock_interview_questions_interview ON mock_interview_questions(interview_id);")
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"[INFO] Mock interview questions table: {str(e)[:100]}")

        # Candidate assessment payments table
        try:
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS candidate_assessment_payments (
                id SERIAL PRIMARY KEY,
                candidate_id VARCHAR(20) NOT NULL,
                assessment_type VARCHAR(50) NOT NULL,
                amount INTEGER NOT NULL,
                currency VARCHAR(10) DEFAULT 'INR',
                payment_status VARCHAR(20) DEFAULT 'completed',
                payment_reference VARCHAR(100),
                paid_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (candidate_id) REFERENCES candidates(id) ON DELETE CASCADE
            );
            """)
            conn.commit()
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_assessment_payments_candidate ON candidate_assessment_payments(candidate_id);")
            conn.commit()
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_assessment_payments_type_status ON candidate_assessment_payments(assessment_type, payment_status);")
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"[INFO] Candidate assessment payments table: {str(e)[:100]}")

        # Saved jobs table
        try:
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS saved_jobs (
                id SERIAL PRIMARY KEY,
                candidate_id VARCHAR(20) NOT NULL,
                job_id INTEGER NOT NULL,
                saved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(candidate_id, job_id),
                FOREIGN KEY (candidate_id) REFERENCES candidates(id) ON DELETE CASCADE,
                FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
            );
            """)
            conn.commit()
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_saved_jobs_candidate ON saved_jobs(candidate_id);")
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"[INFO] Saved jobs table: {str(e)[:100]}")

        # Activity timeline table
        try:
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS activity_timeline (
                id SERIAL PRIMARY KEY,
                user_id VARCHAR(20),
                user_role VARCHAR(50),
                activity_type VARCHAR(100),
                activity_title VARCHAR(255),
                activity_description TEXT,
                metadata JSONB,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)
            conn.commit()
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_activity_timeline_user ON activity_timeline(user_id, user_role);")
            conn.commit()
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_activity_timeline_created ON activity_timeline(created_at);")
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"[INFO] Activity timeline table: {str(e)[:100]}")

        # Resume analyses table
        try:
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS resume_analyses (
                id SERIAL PRIMARY KEY,
                candidate_id VARCHAR(20) NOT NULL,
                filename VARCHAR(255),
                score INTEGER,
                strengths JSONB,
                weaknesses JSONB,
                feedback TEXT,
                tips JSONB,
                analyzed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (candidate_id) REFERENCES candidates(id) ON DELETE CASCADE
            );
            """)
            conn.commit()
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_resume_analyses_candidate ON resume_analyses(candidate_id);")
            conn.commit()
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_resume_analyses_analyzed ON resume_analyses(analyzed_at);")
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"[INFO] Resume analyses table: {str(e)[:100]}")

        # Offer letters table
        try:
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS offer_letters (
                id SERIAL PRIMARY KEY,
                candidate_id VARCHAR(20),
                recruiter_id VARCHAR(20),
                job_id INTEGER,
                position VARCHAR(150),
                salary INTEGER,
                joining_date DATE,
                location VARCHAR(150),
                employment_type VARCHAR(50),
                benefits TEXT,
                offer_file VARCHAR(255),
                status VARCHAR(50) DEFAULT 'Generated',
                generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (candidate_id) REFERENCES candidates(id) ON DELETE CASCADE,
                FOREIGN KEY (recruiter_id) REFERENCES recruiters(id) ON DELETE CASCADE,
                FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
            );
            """)
            conn.commit()
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_offer_letters_candidate ON offer_letters(candidate_id);")
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"[INFO] Offer letters table: {str(e)[:100]}")

        # Feedback table
        try:
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS feedback (
                id SERIAL PRIMARY KEY,
                from_role VARCHAR(50),
                from_id VARCHAR(20),
                to_role VARCHAR(50),
                to_id VARCHAR(20),
                rating INTEGER CHECK (rating BETWEEN 1 AND 5),
                comment TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)
            conn.commit()
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_feedback_to ON feedback(to_role, to_id);")
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"[INFO] Feedback table: {str(e)[:100]}")

        # Migrate feedback id columns from INTEGER to VARCHAR if needed
        try:
            cursor.execute("""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_schema = 'public'
                    AND table_name = 'feedback'
                    AND column_name = 'from_id'
                    AND data_type = 'integer'
                ) THEN
                    ALTER TABLE feedback
                    ALTER COLUMN from_id TYPE VARCHAR(20)
                    USING from_id::VARCHAR;
                END IF;

                IF EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_schema = 'public'
                    AND table_name = 'feedback'
                    AND column_name = 'to_id'
                    AND data_type = 'integer'
                ) THEN
                    ALTER TABLE feedback
                    ALTER COLUMN to_id TYPE VARCHAR(20)
                    USING to_id::VARCHAR;
                END IF;
            END $$;
            """)
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"[INFO] Feedback column migration: {str(e)[:100]}")

        # Mentor messages table
        try:
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS mentor_messages (
                id SERIAL PRIMARY KEY,
                mentorship_request_id INTEGER,
                sender_role VARCHAR(20),
                sender_id VARCHAR(20),
                message_text TEXT,
                is_read BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (mentorship_request_id) REFERENCES mentorship_requests(id) ON DELETE CASCADE
            );
            """)
            conn.commit()
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_mentor_messages_request ON mentor_messages(mentorship_request_id);")
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"[INFO] Mentor messages table: {str(e)[:100]}")

        # Session ratings table
        try:
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS session_ratings (
                id SERIAL PRIMARY KEY,
                mentorship_request_id INTEGER,
                mentor_id VARCHAR(20),
                candidate_id VARCHAR(20),
                rating INTEGER CHECK (rating BETWEEN 1 AND 5),
                feedback TEXT,
                areas_improved TEXT,
                recommendations TEXT,
                session_duration INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (mentorship_request_id) REFERENCES mentorship_requests(id) ON DELETE CASCADE,
                FOREIGN KEY (mentor_id) REFERENCES mentors(id) ON DELETE CASCADE,
                FOREIGN KEY (candidate_id) REFERENCES candidates(id) ON DELETE CASCADE
            );
            """)
            conn.commit()
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_session_ratings_mentor ON session_ratings(mentor_id);")
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"[INFO] Session ratings table: {str(e)[:100]}")

        # Privacy settings table
        try:
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS privacy_settings (
                id SERIAL PRIMARY KEY,
                candidate_id VARCHAR(20) UNIQUE NOT NULL,
                show_email BOOLEAN DEFAULT FALSE,
                show_phone BOOLEAN DEFAULT FALSE,
                allow_recruiter_messages BOOLEAN DEFAULT TRUE,
                searchable_profile BOOLEAN DEFAULT TRUE,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (candidate_id) REFERENCES candidates(id) ON DELETE CASCADE
            );
            """)
            conn.commit()
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_privacy_settings_candidate ON privacy_settings(candidate_id);")
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"[INFO] Privacy settings table: {str(e)[:100]}")

        # Notification preferences table
        try:
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS notification_preferences (
                id SERIAL PRIMARY KEY,
                candidate_id VARCHAR(20) UNIQUE NOT NULL,
                email_notifications BOOLEAN DEFAULT TRUE,
                job_alerts BOOLEAN DEFAULT TRUE,
                interview_reminders BOOLEAN DEFAULT TRUE,
                application_updates BOOLEAN DEFAULT TRUE,
                mentor_messages BOOLEAN DEFAULT TRUE,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (candidate_id) REFERENCES candidates(id) ON DELETE CASCADE
            );
            """)
            conn.commit()
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_notification_preferences_candidate ON notification_preferences(candidate_id);")
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"[INFO] Notification preferences table: {str(e)[:100]}")

        # Login history table
        try:
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS login_history (
                id SERIAL PRIMARY KEY,
                user_id VARCHAR(20),
                user_type VARCHAR(20),
                ip_address VARCHAR(45),
                user_agent TEXT,
                login_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)
            conn.commit()
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_login_history_user ON login_history(user_id, user_type);")
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"[INFO] Login history table: {str(e)[:100]}")

        # Admin settings table
        try:
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS admin_settings (
                id INTEGER PRIMARY KEY DEFAULT 1,
                settings JSONB NOT NULL
            );
            """)
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"[INFO] Admin settings table: {str(e)[:100]}")

        conn.commit()
        
        # Add migration: Add rejection_reason column to mentor_profiles if it doesn't exist
        try:
            cursor.execute("""
            ALTER TABLE mentor_profiles
            ADD COLUMN IF NOT EXISTS rejection_reason TEXT;
            """)
            conn.commit()
            print("[OK] Mentor profiles table migration completed!")
        except Exception as e:
            print(f"[WARNING] Mentor profile migration warning (column may already exist): {e}")
            conn.rollback()
        
        # Add migration: Add assessment columns to applications table if they don't exist
        try:
            cursor.execute("""
            ALTER TABLE applications
            ADD COLUMN IF NOT EXISTS assessment_id INTEGER,
            ADD COLUMN IF NOT EXISTS assessment_status VARCHAR(50) DEFAULT 'not_assigned',
            ADD COLUMN IF NOT EXISTS assessment_assigned_at TIMESTAMP,
            ADD COLUMN IF NOT EXISTS assessment_sent_at TIMESTAMP,
            ADD COLUMN IF NOT EXISTS assessment_completed_at TIMESTAMP;
            """)
            conn.commit()
            print("[OK] Applications table assessment columns migration completed!")
        except Exception as e:
            print(f"[WARNING] Applications migration warning (columns may already exist): {e}")
            conn.rollback()

        # Add migration: Add section column to ai_test_questions if it doesn't exist
        try:
            cursor.execute("""
            ALTER TABLE ai_test_questions
            ADD COLUMN IF NOT EXISTS section VARCHAR(50) DEFAULT 'technical';
            """)
            conn.commit()
            print("[OK] AI test questions section column migration completed!")
        except Exception as e:
            print(f"[WARNING] AI test questions migration warning (column may already exist): {e}")
            conn.rollback()
        
        print("[OK] PostgreSQL tables created successfully!")

    except Exception as e:
        conn.rollback()
        print("[ERROR] Error during table creation (continuing anyway): " + str(e)[:150])
        # Don't raise - allow app to continue starting even if schema creation fails
        # Tables might already exist or connection issues may be temporary
    finally:
        cleanup_db_resources(cursor, conn)

def migrate_database_schema():
    """Add any missing columns to existing tables and fix enum types"""
    conn = get_connection()
    cursor = conn.cursor()
    
    try:
        print("[INFO] Running database schema migrations...")
        
        # Fix application_status enum - add missing values
        print("[INFO] Checking application_status enum...")
        try:
            # Get existing enum values
            cursor.execute("""
                SELECT e.enumlabel
                FROM pg_type t 
                JOIN pg_enum e ON t.oid = e.enumtypid  
                WHERE t.typname = 'application_status'
                ORDER BY e.enumsortorder;
            """)
            existing_values = [row[0] for row in cursor.fetchall()]
            print(f"[INFO] Current enum values: {existing_values}")
            
            # Required enum values in correct order
            required_values = [
                'Applied', 'Accepted', 'Test Sent', 'Test Completed', 
                'Shortlisted', 'Interview', 'Interview Completed', 
                'Group Discussion', 'GD Completed', 'Selected', 'Rejected'
            ]
            
            # Add missing values
            for value in required_values:
                if value not in existing_values:
                    print(f"[INFO] Adding missing enum value: '{value}'")
                    cursor.execute(f"ALTER TYPE application_status ADD VALUE IF NOT EXISTS '{value}';")
                    conn.commit()
            
            print("[OK] application_status enum updated!")
            
        except Exception as e:
            print(f"[WARNING] Enum migration issue: {e}")
            conn.rollback()
        
        # Candidate profiles - ensure all fields exist
        candidate_profile_migrations = [
            "ALTER TABLE candidate_profiles ADD COLUMN IF NOT EXISTS phone VARCHAR(20)",
            "ALTER TABLE candidate_profiles ADD COLUMN IF NOT EXISTS whatsapp VARCHAR(20)",
            "ALTER TABLE candidate_profiles ADD COLUMN IF NOT EXISTS contact_email VARCHAR(255)",
        ]
        
        # Mentor profiles - ensure rejection_reason exists
        mentor_profile_migrations = [
            "ALTER TABLE mentor_profiles ADD COLUMN IF NOT EXISTS rejection_reason TEXT",
            "ALTER TABLE mentor_profiles ADD COLUMN IF NOT EXISTS mentor_price INTEGER DEFAULT 99",
            "ALTER TABLE mentor_profiles ADD COLUMN IF NOT EXISTS upi_id VARCHAR(100)",
            "ALTER TABLE mentor_profiles ADD COLUMN IF NOT EXISTS agreement_accepted BOOLEAN DEFAULT FALSE",
            "ALTER TABLE mentor_profiles ADD COLUMN IF NOT EXISTS agreement_accepted_at TIMESTAMP",
            "ALTER TABLE mentor_profiles ADD COLUMN IF NOT EXISTS agreement_signature_name VARCHAR(255)",
            "ALTER TABLE mentor_profiles ADD COLUMN IF NOT EXISTS agreement_signature_at TIMESTAMP",
            "ALTER TABLE mentor_profiles ADD COLUMN IF NOT EXISTS agreement_signature_image BYTEA",
            "ALTER TABLE mentor_profiles ADD COLUMN IF NOT EXISTS agreement_signature_image_mime VARCHAR(100)",
            "ALTER TABLE mentor_profiles ADD COLUMN IF NOT EXISTS agreement_signature_image_name VARCHAR(255)",
            "ALTER TABLE mentor_profiles ADD COLUMN IF NOT EXISTS agreement_pdf BYTEA",
            "ALTER TABLE mentor_profiles ADD COLUMN IF NOT EXISTS agreement_pdf_mime VARCHAR(100)",
            "ALTER TABLE mentor_profiles ADD COLUMN IF NOT EXISTS agreement_pdf_name VARCHAR(255)",
            "ALTER TABLE mentor_profiles ADD COLUMN IF NOT EXISTS photo_file VARCHAR(255)",
            "ALTER TABLE mentor_profiles ADD COLUMN IF NOT EXISTS mentor_category VARCHAR(100)",
            "ALTER TABLE mentor_profiles ADD COLUMN IF NOT EXISTS highest_qualification VARCHAR(255)",
            "ALTER TABLE mentor_profiles ADD COLUMN IF NOT EXISTS \"current_role\" VARCHAR(255)",
            "ALTER TABLE mentor_profiles ADD COLUMN IF NOT EXISTS overall_experience INTEGER",
            "ALTER TABLE mentor_profiles ADD COLUMN IF NOT EXISTS session_format VARCHAR(50)",
            "ALTER TABLE mentor_profiles ADD COLUMN IF NOT EXISTS preferred_languages VARCHAR(255)",
            "ALTER TABLE mentor_profiles ADD COLUMN IF NOT EXISTS professional_email VARCHAR(255)",
            "ALTER TABLE mentor_profiles ADD COLUMN IF NOT EXISTS mentor_declaration BOOLEAN DEFAULT FALSE",
        ]

        recruiter_profile_migrations = [
            "ALTER TABLE recruiter_profiles ADD COLUMN IF NOT EXISTS agreement_accepted BOOLEAN DEFAULT FALSE",
            "ALTER TABLE recruiter_profiles ADD COLUMN IF NOT EXISTS agreement_accepted_at TIMESTAMP",
            "ALTER TABLE recruiter_profiles ADD COLUMN IF NOT EXISTS agreement_signature_name VARCHAR(255)",
            "ALTER TABLE recruiter_profiles ADD COLUMN IF NOT EXISTS agreement_signature_at TIMESTAMP",
            "ALTER TABLE recruiter_profiles ADD COLUMN IF NOT EXISTS agreement_signature_image BYTEA",
            "ALTER TABLE recruiter_profiles ADD COLUMN IF NOT EXISTS agreement_signature_image_mime VARCHAR(100)",
            "ALTER TABLE recruiter_profiles ADD COLUMN IF NOT EXISTS agreement_signature_image_name VARCHAR(255)",
            "ALTER TABLE recruiter_profiles ADD COLUMN IF NOT EXISTS agreement_pdf BYTEA",
            "ALTER TABLE recruiter_profiles ADD COLUMN IF NOT EXISTS agreement_pdf_mime VARCHAR(100)",
            "ALTER TABLE recruiter_profiles ADD COLUMN IF NOT EXISTS agreement_pdf_name VARCHAR(255)",
            "ALTER TABLE recruiter_profiles ADD COLUMN IF NOT EXISTS admin_countersigned BOOLEAN DEFAULT FALSE",
            "ALTER TABLE recruiter_profiles ADD COLUMN IF NOT EXISTS admin_countersigned_at TIMESTAMP",
            "ALTER TABLE recruiter_profiles ADD COLUMN IF NOT EXISTS admin_countersigned_by VARCHAR(255)",
            "ALTER TABLE recruiter_profiles ADD COLUMN IF NOT EXISTS mou_reset_reason VARCHAR(50)"
        ]

        mentorship_payment_migrations = [
            """
            CREATE TABLE IF NOT EXISTS mentorship_payments (
                id SERIAL PRIMARY KEY,
                mentorship_request_id INTEGER UNIQUE NOT NULL,
                candidate_id VARCHAR(20) NOT NULL,
                mentor_id VARCHAR(20) NOT NULL,
                amount NUMERIC(10,2) NOT NULL,
                currency VARCHAR(10) DEFAULT 'INR',
                admin_share NUMERIC(10,2) NOT NULL,
                mentor_share NUMERIC(10,2) NOT NULL,
                mentor_payout_amount NUMERIC(10,2) NOT NULL,
                payout_status VARCHAR(20) DEFAULT 'pending',
                payment_status VARCHAR(20) DEFAULT 'completed',
                payment_reference VARCHAR(100),
                payout_reference VARCHAR(100),
                paid_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (mentorship_request_id) REFERENCES mentorship_requests(id) ON DELETE CASCADE,
                FOREIGN KEY (candidate_id) REFERENCES candidates(id) ON DELETE CASCADE,
                FOREIGN KEY (mentor_id) REFERENCES mentors(id) ON DELETE CASCADE
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_mentorship_payments_candidate ON mentorship_payments(candidate_id)",
            "CREATE INDEX IF NOT EXISTS idx_mentorship_payments_mentor ON mentorship_payments(mentor_id)",
            "CREATE INDEX IF NOT EXISTS idx_mentorship_payments_status ON mentorship_payments(payment_status)",
            "ALTER TABLE mentorship_payments ADD COLUMN IF NOT EXISTS mentor_payout_amount NUMERIC(10,2)",
            "ALTER TABLE mentorship_payments ADD COLUMN IF NOT EXISTS payout_status VARCHAR(20) DEFAULT 'pending'",
            "ALTER TABLE mentorship_payments ADD COLUMN IF NOT EXISTS payout_reference VARCHAR(100)",
        ]
        
        # Execute all migrations
        all_migrations = (
            candidate_profile_migrations +
            mentor_profile_migrations +
            recruiter_profile_migrations
        )

        all_migrations.extend([
            "ALTER TABLE ai_test_questions ADD COLUMN IF NOT EXISTS section VARCHAR(50) DEFAULT 'technical'",
            "ALTER TABLE interviews ADD COLUMN IF NOT EXISTS interview_round INTEGER DEFAULT 1",
            "ALTER TABLE interviews ADD COLUMN IF NOT EXISTS round_name VARCHAR(100)",
            "ALTER TABLE interviews ADD COLUMN IF NOT EXISTS total_rounds INTEGER DEFAULT 1",
            "ALTER TABLE interviews ADD COLUMN IF NOT EXISTS application_id INTEGER"
        ])
        
        for migration in all_migrations:
            try:
                cursor.execute(migration)
                conn.commit()
                print(f"[OK] Migration applied: {migration[:80]}...")
            except Exception as e:
                conn.rollback()
                print(f"[INFO] Migration skipped (may already exist): {str(e)[:100]}")
                continue
        
        # Add foreign key constraint for application_id if it doesn't exist
        try:
            cursor.execute("""
            ALTER TABLE interviews
            ADD CONSTRAINT fk_interviews_application
            FOREIGN KEY (application_id) REFERENCES applications(id) ON DELETE CASCADE
            """)
            conn.commit()
            print("[OK] Foreign key constraint added for interviews.application_id")
        except Exception as e:
            conn.rollback()
            print(f"[INFO] Foreign key constraint migration skipped: {str(e)[:100]}")
        
        # Create indexes for interviews table if they don't exist
        index_migrations = [
            "CREATE INDEX IF NOT EXISTS idx_interviews_candidate ON interviews(candidate_id)",
            "CREATE INDEX IF NOT EXISTS idx_interviews_recruiter ON interviews(recruiter_id)",
            "CREATE INDEX IF NOT EXISTS idx_interviews_application ON interviews(application_id)"
        ]
        
        for index_sql in index_migrations:
            try:
                cursor.execute(index_sql)
                conn.commit()
                print(f"[OK] Index created: {index_sql[:60]}...")
            except Exception as e:
                conn.rollback()
                print(f"[INFO] Index migration skipped: {str(e)[:100]}")
        
        print("[OK] Database schema migrations completed!")
        
    except Exception as e:
        conn.rollback()
        print(f"[WARNING] Migration error: {e}")
    finally:
        cleanup_db_resources(cursor, conn)

if __name__ == "__main__":
    create_tables()
    migrate_database_schema()
    
