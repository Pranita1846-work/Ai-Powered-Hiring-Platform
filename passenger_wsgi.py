import sys
import os

# Add your project directory to the sys.path
# IMPORTANT: Replace 'yourusername' with your actual Hostinger username
project_home = '/home/sudeshm/public_html'
if project_home not in sys.path:
    sys.path.insert(0, project_home)

# Load environment variables from .env file
from dotenv import load_dotenv
load_dotenv(os.path.join(project_home, '.env'))

# Import Flask app
from app import app as application

# WSGI application entry point
if __name__ == "__main__":
    application.run()
