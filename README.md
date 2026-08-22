# 🤖 AI-Powered Hiring Platform

### 🚀 Intelligent Recruitment • AI-Powered Matching • Career Growth

An end-to-end **AI-assisted hiring and career development platform** designed to connect **candidates, recruiters, mentors, and administrators** in one intelligent ecosystem.

The platform combines **AI-powered resume analysis, candidate-job matching, skill-gap detection, assessments, interview management, recommendations, and recruitment analytics** to make hiring faster, smarter, and more personalized.

<p align="center">
  <strong>🎯 Find the right candidate. Discover the right opportunity. Build the right career.</strong>
</p>

---

## 🌐 Live Demo

### 🚀 Try the platform

**Live Application:**
[AI-Powered Hiring Platform — Live Demo](https://hiringplatform-git-main-thosarpranita1846-7356s-projects.vercel.app/)

> Replace the `#` above with your deployed Vercel URL if you want the link embedded directly in GitHub.

**Deployment:** Vercel
**Backend:** Flask + Python
**Database:** PostgreSQL
**AI:** Google Generative AI

---

## ✨ Why This Project?

Traditional recruitment platforms mainly focus on **job listings and applications**.

This platform goes a step further by introducing an intelligent layer that helps answer:

* 👤 Is this candidate suitable for the job?
* 📄 What skills does the candidate actually have?
* 🎯 Which jobs best match their profile?
* 📊 What skills are missing?
* 🧠 What should the candidate learn next?
* 🏢 Which applicants are the strongest matches?
* 📅 How can recruiters manage interviews and assessments efficiently?

The goal is to transform recruitment from a simple **"search and apply" workflow** into an **AI-assisted career and hiring ecosystem**.

---

# 🌟 Key Features

## 👩‍💻 Candidate Portal

Candidates get a personalized workspace to manage their entire career journey.

### Profile Management

* Create and manage professional profiles
* Add education, experience, certifications, and skills
* Maintain a centralized career profile

### 📄 AI Resume Analysis

* Upload resumes
* Extract important candidate information
* Identify technical and soft skills
* Analyze experience and qualifications
* Generate structured candidate profiles

### 🎯 Intelligent Job Matching

* Match candidates with relevant job openings
* Rank opportunities based on profile compatibility
* Consider skills, experience, education, and job requirements

### 📊 Application Tracking

Track applications through different stages:

`Applied → Screening → Assessment → Interview → Selected / Rejected`

### 🧠 Skill Gap Analysis

The platform compares the candidate's existing skills with job requirements and identifies:

* Missing skills
* Recommended skills
* Areas for improvement
* Career development opportunities

### 📚 Career Recommendations

Candidates receive personalized recommendations for:

* Skills to learn
* Relevant opportunities
* Career improvement
* Mentorship
* Interview preparation

---

# 🏢 Recruiter Portal

Recruiters can manage the complete hiring workflow from a centralized dashboard.

### 🏷️ Job Management

Recruiters can:

* Create job openings
* Define job requirements
* Specify required skills
* Set experience requirements
* Edit and manage job postings
* Track job activity

### 👥 Applicant Management

Recruiters can:

* View applicants
* Filter candidates
* Review candidate profiles
* Analyze resumes
* Compare candidate skills
* Track applicant status

### 🤖 AI Candidate Ranking

AI-assisted matching helps recruiters identify candidates based on:

* Skills
* Experience
* Education
* Job requirements
* Resume information
* Overall compatibility

This helps reduce manual screening effort.

### 📝 Assessment Management

Recruiters can:

* Create assessments
* Generate candidate tests
* Manage questions
* Evaluate candidate performance
* Track assessment results

### 📅 Interview Management

Recruiters can:

* Schedule interviews
* Manage interview details
* Track interview status
* Coordinate candidate interactions
* Manage recruitment activities

---

# 🧠 AI-Powered Intelligence

The core of the platform is its AI-assisted recruitment engine.

## 📄 Resume Parsing

The platform processes uploaded resumes and extracts useful information such as:

```text
Candidate
   │
   ├── Education
   ├── Experience
   ├── Skills
   ├── Certifications
   ├── Projects
   └── Professional Information
```

This converts an unstructured resume into structured candidate information.

---

## 🎯 Candidate–Job Matching

The platform analyzes both candidate profiles and job requirements.

```text
Candidate Profile
       │
       ▼
   AI Analysis
       │
       ├── Skills
       ├── Experience
       ├── Education
       └── Qualifications
       │
       ▼
Job Requirements
       │
       ▼
Compatibility Analysis
       │
       ▼
Match / Ranking
```

Recruiters can therefore focus their attention on candidates who are more relevant to the position.

---

## 📊 Skill Gap Analysis

The platform identifies the difference between:

**Current Candidate Skills**

and

**Required Job Skills**

Example:

```text
Required Skills
├── Python        ✓
├── Flask         ✓
├── PostgreSQL    ✓
├── Docker        ✗
└── AWS           ✗

Skill Gap
├── Docker
└── AWS
```

The system can then provide recommendations for improving those skills.

---

# 📈 Analytics & Dashboards

Different users receive role-specific dashboards.

### 👩‍💻 Candidate Dashboard

Provides insights such as:

* Application statistics
* Interview status
* Recommended jobs
* Skill gaps
* Career recommendations
* Assessment performance

### 🏢 Recruiter Dashboard

Provides:

* Total jobs
* Total applicants
* Candidate pipeline
* Interview statistics
* Assessment activity
* Hiring progress

### 🧑‍🏫 Mentor Dashboard

Supports:

* Candidate guidance
* Career mentorship
* Skill development
* Progress monitoring

### 🛡️ Admin Dashboard

Provides platform-level management including:

* User management
* Recruiter management
* Company verification
* Platform monitoring
* Recruitment analytics

---

# 👥 Multi-Role Architecture

The platform supports multiple user roles:

| Role            | Main Responsibilities                             |
| --------------- | ------------------------------------------------- |
| 👩‍💻 Candidate | Jobs, applications, resume, skills, career growth |
| 🏢 Recruiter    | Jobs, applicants, assessments, interviews         |
| 🧑‍🏫 Mentor    | Guidance and career development                   |
| 🛡️ Admin       | Platform management and verification              |

Each role receives a dedicated workflow and dashboard.

---

# 🔐 Authentication & Security

The platform includes role-based access and authentication features.

### Authentication

* Session-based authentication
* OAuth support
* Google authentication support
* Role-based authorization
* Protected dashboards
* Password reset workflow

### Data Protection

* Environment variables for secrets
* Secure authentication sessions
* Protected user workflows
* Controlled file uploads
* Database-backed application data

> Never commit `.env` files or production credentials to the repository.

---

# 🏗️ System Architecture

```text
                    ┌─────────────────────┐
                    │       Users         │
                    │                     │
                    │ Candidate Recruiter │
                    │ Mentor    Admin     │
                    └──────────┬──────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │   Flask Web App     │
                    │                     │
                    │ Routes / Sessions   │
                    │ Business Logic      │
                    └──────────┬──────────┘
                               │
             ┌─────────────────┼─────────────────┐
             │                 │                 │
             ▼                 ▼                 ▼
      ┌─────────────┐   ┌─────────────┐   ┌─────────────┐
      │ PostgreSQL  │   │ AI Engine   │   │ File System │
      │  Database   │   │             │   │             │
      │             │   │ Gemini / AI │   │ Resumes     │
      │ Users       │   │ Analysis    │   │ Documents   │
      │ Jobs        │   │ Matching    │   │ Uploads     │
      │ Applications│   │ Recommend.  │   │             │
      └─────────────┘   └─────────────┘   └─────────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │  AI Recommendations │
                    │                     │
                    │ Job Matching        │
                    │ Skill Gap Analysis   │
                    │ Resume Insights     │
                    └─────────────────────┘
```

---

# 🛠️ Technology Stack

## Backend

* 🐍 **Python**
* 🌶️ **Flask**
* 🔌 Flask-SocketIO
* ⚡ Eventlet

## Database

* 🐘 **PostgreSQL**
* `psycopg2-binary`

## Frontend

* HTML5
* CSS3
* JavaScript
* Bootstrap-style responsive components
* Flask/Jinja templates

## Artificial Intelligence

* 🤖 Google Generative AI
* AI-assisted resume analysis
* Skill extraction
* Candidate-job matching
* Recommendations
* Skill-gap analysis

## Authentication

* Flask Sessions
* Authlib
* Flask-Dance
* OAuth / Google Authentication

## Document Processing

* PyMuPDF
* PyPDF2
* pdfplumber
* ReportLab
* python-docx
* openpyxl

## Deployment

* 🚀 Vercel
* 🐍 Python/Flask WSGI
* `wsgi.py`
* Passenger deployment support

---

# 📂 Project Structure

```text
AI-Powered-Hiring-Platform/
│
├── app.py
├── database.py
├── wsgi.py
├── passenger_wsgi.py
│
├── init_database.py
├── setup_db.bat
├── create_database.ps1
│
├── requirements.txt
├── .env.example
│
├── templates/
│   ├── auth/
│   ├── candidate/
│   ├── recruiter/
│   ├── mentor/
│   ├── admin/
│   └── ...
│
├── static/
│   ├── css/
│   ├── js/
│   ├── images/
│   └── uploads/
│
└── README.md
```

---

# 🚀 Getting Started

## Prerequisites

Before running the project, make sure you have:

* Python **3.10+**
* PostgreSQL
* Git
* Gmail account or SMTP provider
* Google API key for AI functionality

---

## 1️⃣ Clone the Repository

```bash
git clone <your-repository-url>
cd "AI powered hiring platform"
```

---

## 2️⃣ Create a Virtual Environment

### Windows PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### macOS / Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
```

---

## 3️⃣ Install Dependencies

```bash
pip install -r requirements.txt
```

---

# 🔑 Environment Configuration

Create a `.env` file based on `.env.example`.

Example:

```env
SECRET_KEY=your-secret-key

DATABASE_URL=your-postgresql-database-url

MAIL_USERNAME=your-email
MAIL_PASSWORD=your-email-password

GOOGLE_CLIENT_ID=your-google-client-id
GOOGLE_CLIENT_SECRET=your-google-client-secret

GOOGLE_API_KEY=your-google-api-key
```

> ⚠️ **Important:** Never commit `.env` files containing real credentials.

---

# 🗄️ Database Setup

Initialize the PostgreSQL database using the appropriate script:

```bash
python init_database.py
```

Or, depending on your environment:

```text
setup_db.bat
```

or

```text
create_database.ps1
```

Make sure the database schema has been initialized before starting the application.

---

# ▶️ Run the Application

Start the Flask development server:

```bash
python app.py
```

The application will typically be available at:

```text
http://127.0.0.1:5000/
```

---

# 🚀 Production Deployment

The project includes a WSGI entry point:

```text
wsgi.py
```

For Gunicorn-based deployment:

```bash
gunicorn wsgi:app
```

The application can also be deployed using compatible cloud hosting platforms.

---

# ☁️ Deployment

The project is deployed on **Vercel** for live demonstration.

### Production Environment

```text
Frontend / Web Application
        │
        ▼
     Vercel
        │
        ▼
 Flask Application
        │
        ├── PostgreSQL
        ├── AI Services
        └── Email Services
```

> For production deployment, configure all required environment variables in the hosting provider's dashboard.

---

# 🔄 Complete Hiring Workflow

The platform brings the complete recruitment lifecycle into one workflow.

```text
Candidate
    │
    ▼
Create Profile
    │
    ▼
Upload Resume
    │
    ▼
AI Resume Analysis
    │
    ▼
Discover Jobs
    │
    ▼
AI Job Matching
    │
    ▼
Apply
    │
    ▼
Recruiter Screening
    │
    ▼
Assessment
    │
    ▼
Interview
    │
    ▼
Selection
    │
    ▼
Career Growth
    │
    ▼
Skill Recommendations
```

---

# 💡 What Makes This Project Different?

### Traditional Hiring

```text
Resume → Manual Screening → Interview → Hiring
```

### AI-Assisted Hiring

```text
Resume
   ↓
AI Analysis
   ↓
Skill Extraction
   ↓
Job Matching
   ↓
Candidate Ranking
   ↓
Skill Gap Analysis
   ↓
Assessment
   ↓
Interview
   ↓
Hiring
   ↓
Career Development
```

The platform doesn't stop after a candidate applies.

It continues supporting the candidate through **skill development and career growth**.

---

# 🎯 Project Goals

The primary goals of this project are to:

* ⚡ Reduce manual recruitment effort
* 🎯 Improve candidate-job matching
* 🤖 Introduce AI into recruitment workflows
* 📄 Automate resume analysis
* 📊 Provide meaningful recruitment analytics
* 🧠 Identify candidate skill gaps
* 📚 Recommend career development opportunities
* 🤝 Connect candidates with mentors
* 🏢 Give recruiters better visibility into applicants
* 🚀 Create an end-to-end recruitment ecosystem

---

# 🧪 Future Enhancements

Potential future improvements include:

* [ ] Advanced semantic candidate-job matching
* [ ] AI-powered interview question generation
* [ ] Automated interview evaluation
* [ ] AI interview assistant
* [ ] Candidate behavioral analysis
* [ ] Advanced recruitment analytics
* [ ] Skill-learning platform integration
* [ ] Job market trend analysis
* [ ] Resume improvement suggestions
* [ ] Automated recruiter notifications
* [ ] Calendar integration
* [ ] Mobile application
* [ ] Microservices-based architecture
* [ ] Advanced recommendation engine

---

# 📊 Example Use Cases

### 👩‍💻 Candidate

> Uploads a resume → AI extracts skills → discovers matching jobs → identifies missing skills → receives personalized recommendations.

### 🏢 Recruiter

> Creates a job → receives applications → AI analyzes candidates → candidates are ranked → recruiter schedules assessments and interviews.

### 🧑‍🏫 Mentor

> Reviews candidate progress → identifies development areas → provides career guidance.

### 🛡️ Administrator

> Verifies recruiters and companies → manages users → monitors platform activity.

---

# 📌 Important Notes

* AI functionality requires a valid Google API configuration.
* Email and password-reset functionality requires valid SMTP credentials.
* Uploaded files are currently stored under:

```text
static/uploads/
```

* Production environments should use secure external storage for user-uploaded documents.
* Sensitive environment variables should never be committed to Git.

---

# 🤝 Contributing

Contributions, suggestions, and improvements are welcome.

### Contribution Workflow

```bash
git checkout -b feature/your-feature
```

Make your changes, test them, and submit a pull request.

---

# 👩‍💻 Author

### Pranita Thosar

**AI-Powered Hiring Platform**

Built with:

```text
Python • Flask • PostgreSQL • AI • HTML • CSS • JavaScript
```

---

# ⭐ Support

If you find this project interesting or useful, consider giving the repository a ⭐.

It helps support the project and encourages further development.

---

<p align="center">

### 🚀 AI-Powered Recruitment for the Future

**Smarter Hiring • Better Matching • Stronger Careers**

</p>
