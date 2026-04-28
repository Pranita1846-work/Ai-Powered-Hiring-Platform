"""
Database Initialization Script
Run this after creating the database to set up all tables
"""
import os
import sys

# Load environment variables
try:
    from dotenv import load_dotenv
    load_dotenv()
    print("✓ Environment variables loaded")
except ImportError:
    print("⚠ python-dotenv not installed, using system environment")

# Import database functions
try:
    from database import create_tables, create_database, migrate_database_schema
    print("✓ Database module imported")
except ImportError as e:
    print(f"✗ Error importing database module: {e}")
    sys.exit(1)

def main():
    print("\n" + "="*50)
    print("  HIRING PLATFORM - Database Initialization")
    print("="*50 + "\n")
    
    # Step 1: Verify connection
    print("Step 1: Verifying database connection...")
    try:
        from database import get_connection
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT version();")
        version = cursor.fetchone()
        print(f"✓ Connected to PostgreSQL")
        print(f"  Version: {version[0][:50]}...")
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"✗ Connection failed: {e}")
        print("\nPlease check:")
        print("  1. PostgreSQL service is running")
        print("  2. Database 'hiring_platform' exists")
        print("  3. .env file has correct credentials")
        sys.exit(1)
    
    # Step 2: Create tables
    print("\nStep 2: Creating database tables...")
    try:
        create_tables()
        print("✓ All tables created successfully!")
    except Exception as e:
        print(f"✗ Error creating tables: {e}")
        sys.exit(1)
    
    # Step 2.5: Run migrations
    print("\nStep 2.5: Running database migrations...")
    try:
        migrate_database_schema()
        print("✓ Database migrations completed!")
    except Exception as e:
        print(f"⚠ Migration warning: {e}")
    
    # Step 3: Verify tables
    print("\nStep 3: Verifying tables...")
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT table_name 
            FROM information_schema.tables 
            WHERE table_schema = 'public'
            ORDER BY table_name;
        """)
        tables = cursor.fetchall()
        print(f"✓ Found {len(tables)} tables:")
        for table in tables:
            print(f"  - {table[0]}")
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"⚠ Could not verify tables: {e}")
    
    print("\n" + "="*50)
    print("  Database initialization complete!")
    print("="*50 + "\n")
    print("You can now run: python app.py")

if __name__ == "__main__":
    main()
