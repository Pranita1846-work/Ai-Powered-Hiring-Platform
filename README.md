# AI Powered Hiring Platform

An AI-assisted recruitment and career development platform that connects candidates, recruiters, mentors, and admins in one workflow. The app streamlines job discovery, application tracking, resume analysis, interview management, and skill recommendations.

## Highlights

### Candidate features
- Create and manage professional profiles
- Upload and analyze resumes
- Track applications and interview status
- Receive AI-based job and skill recommendations
- Access mentorship and career guidance

### Recruiter features
- Create recruiter and company profiles
- Post and manage jobs
- Review and filter applicants
- Generate and manage assessment tests
- Schedule interviews and track hiring activity

### AI and analytics
- Resume parsing and skill extraction
- Candidate-job matching and ranking
- Skill gap analysis and performance breakdowns
- Personalized recommendations and insights
- Dashboard analytics for candidates, recruiters, mentors, and admins

### Platform capabilities
- Role-based authentication
- Company verification workflows
- Interview scheduling and notifications
- File uploads for resumes and verification documents
- Responsive UI built with Flask templates

## Tech stack

- **Backend:** Flask, Python
- **Database:** PostgreSQL (`psycopg2-binary`)
- **Frontend:** HTML templates, CSS, Bootstrap-style components
- **AI:** Google Generative AI
- **Auth:** Flask session authentication plus OAuth support via `authlib` and `flask-dance`
- **Documents:** PyMuPDF, PyPDF2, pdfplumber, reportlab, python-docx, openpyxl
- **Realtime/async:** Flask-SocketIO, eventlet

## Prerequisites

- Python 3.10+ recommended
- PostgreSQL database
- Gmail account or SMTP provider for email features
- Google API key if you want AI-assisted features enabled

## Setup

### 1) Clone or open the project

```bash
git clone <your-repo-url>
cd "AI powered hiring platform"
```

### 2) Create and activate a virtual environment

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### 3) Install dependencies

```bash
pip install -r requirements.txt
```

### 4) Configure environment variables

Copy `.env.example` to `.env` and fill in your values.

Common settings:

- `SECRET_KEY`
- `MAIL_USERNAME`
- `MAIL_PASSWORD`
- `GOOGLE_CLIENT_ID`
- `GOOGLE_CLIENT_SECRET`
- `DATABASE_URL` or your local PostgreSQL settings, if applicable

> Never commit `.env` with real secrets.

### 5) Initialize the database

Use whichever setup script fits your environment:

- `python init_database.py`
- `setup_db.bat`
- `create_database.ps1`

If you already have a configured database, make sure the schema is present before starting the app.

## Run locally

```bash
python app.py
```

The app is usually available at `http://127.0.0.1:5000/`.

## Production entry point

The project includes `wsgi.py` for production servers.

Example:

```bash
gunicorn wsgi:app
```

## Project structure

- `app.py` — main Flask application and routes
- `database.py` — database helpers and connection logic
- `wsgi.py` — production WSGI entry point
- `passenger_wsgi.py` — Passenger deployment entry point
- `templates/` — HTML templates
- `static/` — images, uploads, and static assets
- `requirements.txt` — Python dependencies
- `init_database.py` — database initialization helper

## Notes

- Some AI features depend on a valid Google API key.
- Email and password reset flows depend on the mail settings in `.env`.
- Uploaded files are stored under `static/uploads/` in the current project layout.

## Goal of the project

To make hiring faster and smarter by:

- reducing manual screening work
- improving candidate-job fit with AI
- helping candidates understand skill gaps
- giving recruiters better visibility into applicant quality
- supporting career growth beyond simple job applications
