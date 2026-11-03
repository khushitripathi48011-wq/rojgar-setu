from flask import Flask, render_template, request, jsonify, session
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from datetime import datetime, timedelta, timezone
import os, sqlite3, io, re, secrets
from contextlib import contextmanager
from core.matching_engine import analyze_career_readiness, load_benchmarks, parse_skills, match_skills

try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None

app = Flask(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("CAREERTWIN_DB_PATH", os.path.join(BASE_DIR, "data", "career_twin.db"))
app.config.update(SECRET_KEY=os.environ.get("CAREERTWIN_SECRET_KEY") or secrets.token_hex(32),
                  SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=os.environ.get("CAREERTWIN_HTTPS", "0") == "1",
                  MAX_CONTENT_LENGTH=5 * 1024 * 1024)


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

@contextmanager
def get_db():
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db():
    with get_db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
          id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, email TEXT NOT NULL UNIQUE,
          password_hash TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS students (
          user_id INTEGER PRIMARY KEY, education TEXT DEFAULT '', career_goal TEXT DEFAULT '',
          skills TEXT NOT NULL DEFAULT '[]', updated_at TEXT NOT NULL,
          FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS companies (
          id INTEGER PRIMARY KEY AUTOINCREMENT, company_name TEXT NOT NULL, industry TEXT NOT NULL,
          email TEXT NOT NULL UNIQUE, phone TEXT, location TEXT NOT NULL, website TEXT,
          udyam_number TEXT, password_hash TEXT NOT NULL DEFAULT '',
          verification_status TEXT NOT NULL DEFAULT 'Pending verification', created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS jobs (
          id INTEGER PRIMARY KEY AUTOINCREMENT, company_id INTEGER NOT NULL, title TEXT NOT NULL,
          job_type TEXT NOT NULL, skills TEXT NOT NULL, location TEXT NOT NULL, description TEXT,
          stipend_salary TEXT, deadline TEXT, created_at TEXT NOT NULL,
          FOREIGN KEY(company_id) REFERENCES companies(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS applications (
          id INTEGER PRIMARY KEY AUTOINCREMENT, student_id INTEGER NOT NULL, job_id INTEGER NOT NULL,
          match_score INTEGER NOT NULL, matched_skills TEXT NOT NULL, missing_skills TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'Applied', created_at TEXT NOT NULL,
          UNIQUE(student_id, job_id), FOREIGN KEY(student_id) REFERENCES users(id) ON DELETE CASCADE,
          FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS login_attempts (
          id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL, attempted_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS student_projects (
          id INTEGER PRIMARY KEY AUTOINCREMENT, student_id INTEGER NOT NULL,
          title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', tech_stack TEXT NOT NULL DEFAULT '',
          github_url TEXT NOT NULL, demo_url TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
          FOREIGN KEY(student_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_student_projects_student ON student_projects(student_id);
        CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company_id);
        CREATE INDEX IF NOT EXISTS idx_applications_job ON applications(job_id);
        CREATE INDEX IF NOT EXISTS idx_login_attempts_email_date ON login_attempts(email, attempted_at);
        """)
        # Additive migration for databases created by the earlier MSME prototype.
        existing = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        company_columns = {row[1] for row in conn.execute("PRAGMA table_info(companies)").fetchall()}
        if "password_hash" not in company_columns: conn.execute("ALTER TABLE companies ADD COLUMN password_hash TEXT NOT NULL DEFAULT ''")
        app_columns = {row[1] for row in conn.execute("PRAGMA table_info(applications)").fetchall()}
        if "github_url" not in app_columns: conn.execute("ALTER TABLE applications ADD COLUMN github_url TEXT NOT NULL DEFAULT ''")
        if "description" not in existing: conn.execute("ALTER TABLE jobs ADD COLUMN description TEXT")
        if "stipend_salary" not in existing: conn.execute("ALTER TABLE jobs ADD COLUMN stipend_salary TEXT")
        if "deadline" not in existing: conn.execute("ALTER TABLE jobs ADD COLUMN deadline TEXT")

        # No fictional employers or invented vacancies are seeded. Remove only the four
        # known placeholder records from earlier versions, if this is an existing local DB.
        # Real company career links are a separate external directory, not recruiter accounts.
        placeholder_emails = (
            "careers@northstardigital.test", "hr@blueorbit.test",
            "careers@greenbyte.test", "jobs@cloudspring.test"
        )
        placeholders = conn.execute(
            "SELECT id FROM companies WHERE email IN (?,?,?,?)", placeholder_emails
        ).fetchall()
        for placeholder in placeholders:
            conn.execute("DELETE FROM companies WHERE id=?", (placeholder["id"],))
        conn.commit()

init_db()


GITHUB_RE = re.compile(r"https://github\.com/[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})(?:/[A-Za-z0-9._-]+)?", re.I)
def normalize_github_url(value, require_repo=False):
    """Accept github.com/user/repo typed with or without https://, www., .git or trailing slash."""
    v = str(value or "").strip()
    if not v: return ""
    v = re.sub(r"^(?:https?://)?(?:www\.)?", "", v, flags=re.I)
    if not v.lower().startswith("github.com/"): return None
    v = v.split("?")[0].split("#")[0].rstrip("/")
    v = re.sub(r"\.git$", "", v, flags=re.I)
    v = "https://" + v
    if not GITHUB_RE.fullmatch(v): return None
    if require_repo and v.count("/") < 4: return None
    return v

def json_error(message, status=400):
    return jsonify({"success": False, "error": message}), status

def current_user_id():
    return session.get("user_id")
def login_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not current_user_id(): return json_error("Please log in to perform this action.", 401)
        return fn(*args, **kwargs)
    return wrapped


def company_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not session.get("company_id"):
            return json_error("Please register or log in as the company before managing jobs/applications.", 401)
        return fn(*args, **kwargs)
    return wrapped

def safe_json_list(value):
    import json
    try:
        result = json.loads(value or "[]")
        return result if isinstance(result, list) else []
    except (ValueError, TypeError): return []

@app.after_request
def security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Cache-Control", "no-store")
    return response

@app.route("/")
def index():
    return render_template("index.html", roles={k: v["title"] for k, v in load_benchmarks().items()})

# Server-side email/password account APIs. Phone/SMS OTP requires a configured identity provider.
@app.route("/api/auth/register", methods=["POST"])
def auth_register():
    data = request.get_json(silent=True) or {}
    name, email, password = str(data.get("name", "")).strip(), str(data.get("email", "")).strip().lower(), str(data.get("password", ""))
    if len(name) < 2: return json_error("Enter your full name.")
    if len(email) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email): return json_error("Enter a valid email address.")
    if len(password) < 10: return json_error("Password must contain at least 10 characters.")
    if len(password) > 256: return json_error("Password is too long.")
    try:
        with get_db() as conn:
            cur = conn.execute("INSERT INTO users(name,email,password_hash,created_at) VALUES(?,?,?,?)", (name,email,generate_password_hash(password),utcnow()))
            user_id = cur.lastrowid
            conn.execute("INSERT INTO students(user_id,updated_at) VALUES(?,?)", (user_id,utcnow()))
        session.clear(); session["user_id"] = user_id
        return jsonify({"success": True, "user": {"id": user_id, "name": name, "email": email}}), 201
    except sqlite3.IntegrityError: return json_error("This email is already registered. Please log in.", 409)

@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    data = request.get_json(silent=True) or {}
    email, password = str(data.get("email", "")).strip().lower(), str(data.get("password", ""))
    now = datetime.now(timezone.utc)
    with get_db() as conn:
        conn.execute("DELETE FROM login_attempts WHERE attempted_at < ?", ((now-timedelta(hours=24)).isoformat(),))
        attempts = conn.execute("SELECT COUNT(*) n FROM login_attempts WHERE email=? AND attempted_at>=?", (email,(now-timedelta(minutes=15)).isoformat())).fetchone()["n"]
        if attempts >= 8: return json_error("Too many login attempts. Wait 15 minutes and try again.", 429)
        user = conn.execute("SELECT id,name,email,password_hash FROM users WHERE email=?", (email,)).fetchone()
        if not user or not check_password_hash(user["password_hash"], password):
            conn.execute("INSERT INTO login_attempts(email,attempted_at) VALUES(?,?)", (email,now.isoformat()))
            return json_error("Email or password is incorrect.", 401)
        conn.execute("DELETE FROM login_attempts WHERE email=?", (email,))
    session.clear(); session["user_id"] = user["id"]
    return jsonify({"success": True, "user": {"id": user["id"], "name": user["name"], "email": user["email"]}})

@app.route("/api/auth/me")
def auth_me():
    if not current_user_id(): return jsonify({"authenticated": False})
    with get_db() as conn: user = conn.execute("SELECT id,name,email FROM users WHERE id=?", (current_user_id(),)).fetchone()
    if not user: session.clear(); return jsonify({"authenticated": False})
    return jsonify({"authenticated": True, "user": dict(user)})

@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    session.clear(); return jsonify({"success": True})

MSME_COMPANY_DIRECTORY = [
    {"id": 1, "name": "Gomti Software Works", "initials": "GSW", "industry": "Software & Web Development", "location": "Lucknow, Uttar Pradesh", "focus": "Web apps and billing software for local retailers", "role": "Python Developer Intern", "job_type": "Internship", "skills": ["Python", "SQL", "Git", "HTML"]},
    {"id": 2, "name": "Kashi Digital Solutions", "initials": "KDS", "industry": "Digital Marketing & Web", "location": "Varanasi, Uttar Pradesh", "focus": "Websites and online stores for handloom and tourism businesses", "role": "Frontend Developer", "job_type": "Full-time job", "skills": ["HTML", "CSS", "JavaScript", "Git"]},
    {"id": 3, "name": "Awadh DataTech", "initials": "ADT", "industry": "Data Analytics", "location": "Lucknow, Uttar Pradesh", "focus": "Sales dashboards and reporting for small manufacturers", "role": "Data Analyst Trainee", "job_type": "Apprenticeship", "skills": ["Python", "SQL", "Pandas", "Data Analysis"]},
    {"id": 4, "name": "Ganga CloudOps", "initials": "GCO", "industry": "Cloud & DevOps Services", "location": "Prayagraj, Uttar Pradesh", "focus": "Cloud setup and deployment support for MSME clients", "role": "DevOps Intern", "job_type": "Internship", "skills": ["Linux Basics", "Docker", "Git", "Python"]},
    {"id": 5, "name": "Taj IoT Systems", "initials": "TIS", "industry": "IoT & Embedded", "location": "Agra, Uttar Pradesh", "focus": "Sensor-based monitoring products for small factories", "role": "Embedded / IoT Trainee", "job_type": "Apprenticeship", "skills": ["C++", "Python", "Linux Basics", "Git"]},
    {"id": 6, "name": "Naimisha Cyber Labs", "initials": "NCL", "industry": "Cybersecurity Services", "location": "Noida, Uttar Pradesh", "focus": "Basic security audits for small businesses", "role": "Security Analyst Intern", "job_type": "Internship", "skills": ["Network Security", "Linux Basics", "Cryptography"]},
    {"id": 7, "name": "Prayag EdTech Services", "initials": "PES", "industry": "Education Technology", "location": "Prayagraj, Uttar Pradesh", "focus": "Learning portals for coaching institutes", "role": "Backend Developer", "job_type": "Full-time job", "skills": ["Python", "Flask", "SQL", "REST APIs"]},
    {"id": 8, "name": "Kanpur Fintech Solutions", "initials": "KFS", "industry": "Fintech & Accounting Software", "location": "Kanpur, Uttar Pradesh", "focus": "Invoicing and GST tools for traders", "role": "Software Developer", "job_type": "Full-time job", "skills": ["Java", "SQL", "OOPs", "REST APIs"]},
    {"id": 9, "name": "Bundelkhand AgriTech", "initials": "BAT", "industry": "Agri Technology", "location": "Jhansi, Uttar Pradesh", "focus": "Crop advisory and farm-data tools", "role": "ML Project Intern", "job_type": "Project-based work", "skills": ["Python", "Machine Learning", "Pandas", "NumPy"]},
    {"id": 10, "name": "Meerut QA Labs", "initials": "MQL", "industry": "Software Testing", "location": "Meerut, Uttar Pradesh", "focus": "Manual and automated testing for small software firms", "role": "QA Trainee", "job_type": "Internship", "skills": ["SQL", "Python", "REST APIs", "Git"]},
    {"id": 11, "name": "Chikan Craft eCommerce", "initials": "CCE", "industry": "E-commerce", "location": "Lucknow, Uttar Pradesh", "focus": "Online marketplace for chikankari artisans", "role": "Full Stack Developer", "job_type": "Full-time job", "skills": ["JavaScript", "MongoDB", "HTML", "CSS", "Git"]},
    {"id": 12, "name": "Ghaziabad Mobile Apps", "initials": "GMA", "industry": "Mobile App Development", "location": "Ghaziabad, Uttar Pradesh", "focus": "Android apps for local service businesses", "role": "Android Developer Intern", "job_type": "Internship", "skills": ["Java", "Git", "REST APIs", "SQL"]},
]
for _c in MSME_COMPANY_DIRECTORY:
    _c["sample"] = True
    _c["note"] = "SAMPLE MSME — demo data, not a registered or verified company"

@app.get("/api/company-directory")
def official_company_directory_api():
    # Sample MSME profiles for the demo. They are NOT real registered employers and not verified.
    return jsonify(MSME_COMPANY_DIRECTORY)

def demo_msme_jobs():
    return [{"id": f"demo-{c['id']}", "demo": True, "company_name": c["name"] + " (Sample MSME)", "title": c["role"],
             "job_type": c["job_type"], "location": c["location"], "skills": c["skills"], "verification_status": "Sample"}
            for c in MSME_COMPANY_DIRECTORY]

@app.route("/api/profile", methods=["GET", "POST"])
@login_required
def profile_api():
    import json
    with get_db() as conn:
        if request.method == "GET":
            row = conn.execute("SELECT users.id,users.name,users.email,students.education,students.career_goal,students.skills FROM users JOIN students ON students.user_id=users.id WHERE users.id=?", (current_user_id(),)).fetchone()
            result = dict(row); result["skills"] = safe_json_list(result["skills"]); return jsonify(result)
        data = request.get_json(silent=True) or {}
        name = str(data.get("name", "")).strip()[:100]
        education = str(data.get("education", "")).strip()[:200]
        goal = str(data.get("career_goal", "")).strip()[:200]
        skills = parse_skills(data.get("skills", []))[:100]
        if name: conn.execute("UPDATE users SET name=? WHERE id=?", (name,current_user_id()))
        conn.execute("UPDATE students SET education=?,career_goal=?,skills=?,updated_at=? WHERE user_id=?", (education,goal,json.dumps(skills),utcnow(),current_user_id()))
    return jsonify({"success": True, "skills": skills})

@app.route("/api/companies", methods=["GET", "POST"])
def companies_api():
    if request.method == "GET":
        with get_db() as conn:
            if request.args.get("mine") == "1":
                if session.get("company_id"):
                    rows = conn.execute("SELECT id,company_name,industry,location,website,verification_status,created_at FROM companies WHERE id=? ORDER BY id DESC", (session["company_id"],)).fetchall()
                else:
                    rows = []
            else:
                rows = conn.execute("SELECT id,company_name,industry,location,website,verification_status,created_at FROM companies ORDER BY id DESC").fetchall()
        return jsonify([dict(row) for row in rows])
    data = request.get_json(silent=True) or {}
    name, industry, email, location = [str(data.get(k, "")).strip() for k in ("company_name","industry","email","location")]
    email = email.lower(); password = str(data.get("password", ""))
    if not all([name,industry,email,location]): return json_error("Company name, industry, email and location are required.")
    if len(name)>160 or len(industry)>100 or len(location)>160 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+",email): return json_error("Please check company details and email format.")
    if len(password) < 10: return json_error("Create a company password with at least 10 characters.")
    try:
        with get_db() as conn:
            cur=conn.execute("INSERT INTO companies(company_name,industry,email,phone,location,website,udyam_number,password_hash,verification_status,created_at) VALUES(?,?,?,?,?,?,?,?,'Pending verification',?)", (name,industry,email,str(data.get("phone",""))[:40].strip(),location,str(data.get("website",""))[:255].strip(),str(data.get("udyam_number",""))[:80].strip(),generate_password_hash(password),utcnow()))
            company_id=cur.lastrowid
        session["company_id"] = company_id
        return jsonify({"success":True,"company_id":company_id,"verification_status":"Pending verification","message":"Company account created. Verification must be completed separately."}),201
    except sqlite3.IntegrityError: return json_error("This company email is already registered. Use Company Login.",409)

@app.route("/api/company/login", methods=["POST"])
def company_login():
    data=request.get_json(silent=True) or {}; email=str(data.get("email", "")).strip().lower(); password=str(data.get("password", ""))
    now=datetime.now(timezone.utc)
    with get_db() as conn:
        conn.execute("DELETE FROM login_attempts WHERE attempted_at < ?", ((now-timedelta(hours=24)).isoformat(),))
        attempts=conn.execute("SELECT COUNT(*) n FROM login_attempts WHERE email=? AND attempted_at>=?", ("company:"+email,(now-timedelta(minutes=15)).isoformat())).fetchone()["n"]
        if attempts>=8: return json_error("Too many company login attempts. Wait 15 minutes and try again.",429)
        company=conn.execute("SELECT id,company_name,email,password_hash FROM companies WHERE email=?",(email,)).fetchone()
        if not company or not company["password_hash"] or not check_password_hash(company["password_hash"],password):
            conn.execute("INSERT INTO login_attempts(email,attempted_at) VALUES(?,?)", ("company:"+email,now.isoformat()))
            return json_error("Company email or password is incorrect.",401)
        conn.execute("DELETE FROM login_attempts WHERE email=?", ("company:"+email,))
    session["company_id"]=company["id"]
    return jsonify({"success":True,"company":{"id":company["id"],"company_name":company["company_name"],"email":company["email"]}})

@app.route("/api/company/logout", methods=["POST"])
def company_logout():
    session.pop("company_id",None)
    return jsonify({"success":True})

@app.route("/api/jobs", methods=["GET","POST"])
def jobs_api():
    if request.method == "GET":
        with get_db() as conn: rows=conn.execute("SELECT jobs.*,companies.company_name,companies.verification_status FROM jobs JOIN companies ON companies.id=jobs.company_id ORDER BY jobs.id DESC").fetchall()
        return jsonify([dict(r) for r in rows])
    if not session.get("company_id"):
        return json_error("Company login required before posting a job.",401)
    data=request.get_json(silent=True) or {}
    try: company_id=int(data.get("company_id",0))
    except (TypeError,ValueError): company_id=0
    title,job_type,skills,location=[str(data.get(k," ")).strip() for k in ("title","job_type","skills","location")]
    if not company_id or not title.strip() or not skills.strip() or not location.strip(): return json_error("Company, title, required skills and location are required.")
    if len(title)>180 or len(location)>180 or len(skills)>1000: return json_error("One or more job fields are too long.")
    if job_type not in {"Internship","Full-time job","Part-time job","Project-based work","Apprenticeship"}: return json_error("Select a valid job type.")
    with get_db() as conn:
        if company_id != session.get("company_id"):
            return json_error("You can only post requirements for the company account you are logged in to.",403)
        if not conn.execute("SELECT id FROM companies WHERE id=?",(company_id,)).fetchone(): return json_error("Please register/select a company first.")
        cur=conn.execute("INSERT INTO jobs(company_id,title,job_type,skills,location,description,stipend_salary,deadline,created_at) VALUES(?,?,?,?,?,?,?,?,?)",(company_id,title.strip(),job_type,", ".join(parse_skills(skills)),location.strip(),str(data.get("description",""))[:4000].strip(),str(data.get("stipend_salary",""))[:100].strip(),str(data.get("deadline",""))[:20].strip(),utcnow()))
        job_id=cur.lastrowid
    return jsonify({"success":True,"job_id":job_id,"message":"Requirement saved. This does not imply company verification."}),201

@app.route("/api/matches", methods=["GET","POST"])
def matches_api():
    data=(request.get_json(silent=True) or {}) if request.method=="POST" else request.args
    skills=parse_skills(data.get("skills", ""))
    if not skills and current_user_id():
        with get_db() as conn: row=conn.execute("SELECT skills FROM students WHERE user_id=?",(current_user_id(),)).fetchone()
        if row: skills=safe_json_list(row["skills"])
    with get_db() as conn: rows=conn.execute("SELECT jobs.*,companies.company_name,companies.verification_status FROM jobs JOIN companies ON companies.id=jobs.company_id ORDER BY jobs.id DESC").fetchall()
    results=[]
    for row in rows:
        job=dict(row); required=parse_skills(job["skills"]); match=match_skills(skills,required)
        results.append({**job,"skills":required,"match_percentage":match["match_percentage"],"matched_skills":match["acquired_skills"],"missing_skills":match["missing_skills"],"score_explanation":match["score_explanation"],"verified_company":job["verification_status"]=="Verified"})
    if str(data.get("include_demo","")).lower() in {"1","true","yes"}:
        for dj in demo_msme_jobs():
            m=match_skills(skills,parse_skills(dj["skills"]))
            results.append({**dj,"skills":parse_skills(dj["skills"]),"match_percentage":m["match_percentage"],"matched_skills":m["acquired_skills"],"missing_skills":m["missing_skills"],"score_explanation":m["score_explanation"],"verified_company":False})
    real=[r for r in results if not r.get("demo")]; demo=[r for r in results if r.get("demo")]
    ordered=sorted(real,key=lambda x:(x["match_percentage"],x["id"]),reverse=True)+sorted(demo,key=lambda x:x["match_percentage"],reverse=True)
    return jsonify({"skills_used":skills,"count":len(results),"matches":ordered,"note":"Scores measure listed-skill coverage only. They are not hiring probabilities. Only manually verified companies are labelled verified."})

@app.route("/api/applications", methods=["GET","POST"])
@login_required
def applications_api():
    if request.method=="GET":
        with get_db() as conn:
            rows=conn.execute("SELECT a.*,j.title,j.job_type,j.location,c.company_name FROM applications a JOIN jobs j ON j.id=a.job_id JOIN companies c ON c.id=j.company_id WHERE a.student_id=? ORDER BY a.id DESC",(current_user_id(),)).fetchall()
        return jsonify([dict(r, matched_skills=safe_json_list(r["matched_skills"]), missing_skills=safe_json_list(r["missing_skills"])) for r in rows])
    data=request.get_json(silent=True) or {}
    try: job_id=int(data.get("job_id",0))
    except (TypeError,ValueError): job_id=0
    github_link=normalize_github_url(data.get("github_url",""))
    if github_link is None: return json_error("GitHub link must look like https://github.com/username or https://github.com/username/repository.")
    with get_db() as conn:
        job=conn.execute("SELECT * FROM jobs WHERE id=?",(job_id,)).fetchone()
        if not job: return json_error("Job not found.",404)
        profile=conn.execute("SELECT skills FROM students WHERE user_id=?",(current_user_id(),)).fetchone()
        skills=parse_skills(data.get("skills", safe_json_list(profile["skills"]) if profile else []))
        match=match_skills(skills,parse_skills(job["skills"]))
        try:
            cur=conn.execute("INSERT INTO applications(student_id,job_id,match_score,matched_skills,missing_skills,status,created_at,github_url) VALUES(?,?,?,?,?,'Applied',?,?)",(current_user_id(),job_id,match["match_percentage"],__import__('json').dumps(match["acquired_skills"]),__import__('json').dumps(match["missing_skills"]),utcnow(),github_link))
        except sqlite3.IntegrityError: return json_error("You have already applied for this job.",409)
    return jsonify({"success":True,"application_id":cur.lastrowid,"status":"Applied","github_url":github_link,"match_percentage":match["match_percentage"],"matched_skills":match["acquired_skills"],"missing_skills":match["missing_skills"]}),201

@app.route("/api/projects", methods=["GET", "POST"])
@login_required
def student_projects_api():
    """Student portfolio: GitHub repository links are evidence links, not verified claims."""
    if request.method == "GET":
        with get_db() as conn:
            rows = conn.execute("SELECT id,title,description,tech_stack,github_url,demo_url,created_at FROM student_projects WHERE student_id=? ORDER BY id DESC", (current_user_id(),)).fetchall()
        return jsonify([dict(row) for row in rows])
    data = request.get_json(silent=True) or {}
    title = str(data.get("title", "")).strip()[:120]
    description = str(data.get("description", "")).strip()[:1200]
    tech_stack = str(data.get("tech_stack", "")).strip()[:300]
    github_url = str(data.get("github_url", "")).strip()
    demo_url = str(data.get("demo_url", "")).strip()
    def valid_http_url(value):
        return not value or (len(value) <= 500 and re.fullmatch(r"https?://[^\s]+", value, re.I) is not None)
    if len(title) < 2: return json_error("Project title must contain at least 2 characters.")
    github_url = normalize_github_url(github_url, require_repo=True)
    if not github_url:
        return json_error("Enter a valid GitHub repository URL, e.g. https://github.com/username/repository.")
    if not valid_http_url(demo_url): return json_error("Demo URL must start with http:// or https://.")
    with get_db() as conn:
        count = conn.execute("SELECT COUNT(*) n FROM student_projects WHERE student_id=?", (current_user_id(),)).fetchone()["n"]
        if count >= 20: return json_error("You can add up to 20 projects.", 409)
        cur = conn.execute("INSERT INTO student_projects(student_id,title,description,tech_stack,github_url,demo_url,created_at) VALUES(?,?,?,?,?,?,?)", (current_user_id(),title,description,tech_stack,github_url,demo_url,utcnow()))
        row = conn.execute("SELECT id,title,description,tech_stack,github_url,demo_url,created_at FROM student_projects WHERE id=?", (cur.lastrowid,)).fetchone()
    return jsonify({"success": True, "project": dict(row)}), 201

@app.route("/api/projects/<int:project_id>", methods=["DELETE"])
@login_required
def delete_student_project(project_id):
    with get_db() as conn:
        cur = conn.execute("DELETE FROM student_projects WHERE id=? AND student_id=?", (project_id,current_user_id()))
        if not cur.rowcount: return json_error("Project not found in your portfolio.",404)
    return jsonify({"success": True, "project_id": project_id})

@app.route("/api/recruiter/jobs/<int:job_id>/applications")
@company_required
def recruiter_applications(job_id):
    with get_db() as conn:
        owned=conn.execute("SELECT id FROM jobs WHERE id=? AND company_id=?",(job_id,session["company_id"])).fetchone()
        if not owned: return json_error("Job not found for this company account.",404)
        rows=conn.execute("SELECT a.id,a.student_id,a.match_score,a.matched_skills,a.missing_skills,a.status,a.created_at,a.github_url,u.name,u.email FROM applications a JOIN users u ON u.id=a.student_id JOIN jobs j ON j.id=a.job_id WHERE j.id=? ORDER BY a.match_score DESC,a.id",(job_id,)).fetchall()
        result=[]
        for r in rows:
            item=dict(r,matched_skills=safe_json_list(r["matched_skills"]),missing_skills=safe_json_list(r["missing_skills"]))
            item["projects"]=[dict(p) for p in conn.execute("SELECT id,title,description,tech_stack,github_url,demo_url FROM student_projects WHERE student_id=? ORDER BY id DESC",(r["student_id"],)).fetchall()]
            result.append(item)
    return jsonify(result)

@app.route("/api/applications/<int:application_id>/status", methods=["PATCH"])
@company_required
def application_status(application_id):
    status=str((request.get_json(silent=True) or {}).get("status", "")).strip()
    if status not in {"Applied","Shortlisted","Rejected","Interview","Hired"}: return json_error("Invalid application status.")
    with get_db() as conn:
        cur=conn.execute("UPDATE applications SET status=? WHERE id=? AND job_id IN (SELECT id FROM jobs WHERE company_id=?)",(status,application_id,session["company_id"]))
        if cur.rowcount==0: return json_error("Application not found for this company account.",404)
    return jsonify({"success":True,"application_id":application_id,"status":status})

@app.route("/api/mentor", methods=["POST"])
def mentor_api():
    data=request.get_json(silent=True) or {}
    question=str(data.get("question", "")).strip()[:1000]
    if not question: return json_error("Enter a question.")
    skills=parse_skills(data.get("skills", [])); goal=str(data.get("career_goal", "software development")).strip()[:120]
    gaps=parse_skills(data.get("missing_skills", []))
    lower=question.lower()
    if not gaps and current_user_id():
        with get_db() as conn: row=conn.execute("SELECT skills,career_goal FROM students WHERE user_id=?",(current_user_id(),)).fetchone()
        if row:
            skills=skills or safe_json_list(row["skills"]); goal=goal or row["career_goal"]
    if any(k in lower for k in ["gap","roadmap","30 day","missing skill"]):
        focus=gaps[:5] or ["choose a target role and run Skill Analysis first"]
        answer=f"For your goal ({goal}), focus next on: {', '.join(focus)}. For each skill: (1) learn the core concept, (2) complete one small hands-on task, (3) publish evidence in a project/portfolio, and (4) reassess your match. Current skills provided: {', '.join(skills) if skills else 'not provided'} ."
    elif any(k in lower for k in ["resume","cv"]): answer=f"For a {goal} target, keep your resume evidence-based: list skills you can demonstrate, add 1–2 projects with your contribution and outcome, and align wording to role requirements. Current skills available to tailor advice: {', '.join(skills) if skills else 'none supplied'} ."
    elif any(k in lower for k in ["interview","question"]): answer=f"For {goal}, practice one role-specific concept, explain a project aloud, then solve a small practical problem. Review these skills from your profile: {', '.join(skills[:6]) if skills else 'add your skills first'}. Track weak answers and revisit them in 48 hours."
    else: answer=f"I can help plan your next step toward {goal}. Your supplied skills are {', '.join(skills) if skills else 'not yet added'} and current gaps are {', '.join(gaps) if gaps else 'not supplied'}. Tell me which skill or job requirement you want to work on; I will suggest a concrete learning task and project evidence. This mentor uses your provided context and curated rules, not a general-purpose LLM."
    return jsonify({"answer":answer,"personalized_to":{"career_goal":goal,"skills_used":skills,"gaps_used":gaps},"mode":"context-aware rule-based mentor"})

@app.route("/analyze", methods=["POST"])
@app.route("/evaluate", methods=["POST"])
def analyze():
    data=request.get_json(silent=True) or {}
    skills=data.get("skills", "")
    role=str(data.get("role", "sde"))
    result=analyze_career_readiness(skills,role)
    if "error" in result: return jsonify(result),400
    return jsonify(result)

@app.route("/upload_resume", methods=["POST"])
def upload_resume():
    file=request.files.get("resume")
    if not file or not file.filename: return json_error("Choose a PDF or TXT resume first.")
    if not (file.filename.lower().endswith(".pdf") or file.filename.lower().endswith(".txt")): return json_error("Only PDF and TXT files are supported.",415)
    try:
        content=file.read()
        if len(content)>5*1024*1024: return json_error("Resume must be 5 MB or smaller.",413)
        if file.filename.lower().endswith(".pdf"):
            if PdfReader is None: return json_error("PDF support is not installed. Install requirements.txt.",500)
            extracted=" ".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(content)).pages)
        else: extracted=content.decode("utf-8",errors="ignore")
        vocabulary={"python":"python","java":"java","c++":"c++","javascript":"javascript","html":"html","css":"css","sql":"sql","git":"git","github":"github","dsa":"data structures","data structures":"data structures","algorithms":"algorithms","oops":"oops","dbms":"dbms","operating systems":"operating systems","flask":"flask","django":"django","fastapi":"fastapi","docker":"docker","linux":"linux basics","pandas":"pandas","numpy":"numpy","machine learning":"machine learning","data analysis":"data analysis","statistics":"statistics","mongodb":"mongodb","postgresql":"postgresql","redis":"redis","rest api":"rest apis","system design":"system design","scikit-learn":"scikit-learn","network security":"network security","cryptography":"cryptography"}
        text=extracted.lower(); found=[]
        for phrase,canonical in vocabulary.items():
            if re.search(r"(?<!\w)"+re.escape(phrase)+r"(?!\w)",text) and canonical not in found: found.append(canonical)
        return jsonify({"success":True,"skills":found,"raw_count":len(found),"skills_string":", ".join(found),"note":"Resume extraction finds listed keywords only; please review the detected skills."})
    except Exception: return json_error("Could not parse this resume. Try a text-based PDF or TXT file.",400)

@app.get("/health")
def health():
    with get_db() as conn: conn.execute("SELECT 1").fetchone()
    return jsonify({"status":"ok","database":"connected"})




SEED_STUDENTS_LIST = [
  {
    "id": 101,
    "name": "Srishti Singh",
    "email": "srishti.singh@bit.ac.in",
    "education": "B.Tech CSE (Data Science) - Buddha Institute of Technology, Gorakhpur",
    "career_goal": "Python Developer / AI Engineer",
    "skills": [
      "Python",
      "SQL",
      "FastAPI",
      "React",
      "PostgreSQL",
      "Git",
      "REST APIs"
    ],
    "github_username": "srishtisingh-code",
    "github_url": "https://github.com/srishtisingh-code",
    "projects": [
      {
        "title": "CareerTwin X - Smart Placement Platform",
        "tech_stack": "Python, FastAPI, React, PostgreSQL",
        "github_url": "https://github.com/srishtisingh-code/careertwin-x",
        "description": "AI-powered career digital twin mapping candidate skills to MSME openings."
      },
      {
        "title": "AegisNova - Edge Vision Framework",
        "tech_stack": "Python, OpenCV, YOLO, Raspberry Pi 5",
        "github_url": "https://github.com/srishtisingh-code/aegisnova-edge",
        "description": "Vision-based edge computing surveillance system for crop-damaging animal detection."
      }
    ]
  },
  {
    "id": 102,
    "name": "Sneha Singh",
    "email": "sneha.singh@bit.ac.in",
    "education": "B.Tech CSE - Buddha Institute of Technology, Gorakhpur",
    "career_goal": "Full Stack Developer",
    "skills": [
      "JavaScript",
      "React",
      "Node.js",
      "Express",
      "MongoDB",
      "Tailwind CSS",
      "Git"
    ],
    "github_username": "snehasingh-dev",
    "github_url": "https://github.com/snehasingh-dev",
    "projects": [
      {
        "title": "Rojgar Setu Web Portal",
        "tech_stack": "React, Node.js, Express, MongoDB",
        "github_url": "https://github.com/snehasingh-dev/rojgar-setu-ui",
        "description": "Responsive dashboard for student-recruiter matching."
      }
    ]
  },
  {
    "id": 103,
    "name": "Sadhna Maurya",
    "email": "sadhna.maurya@bit.ac.in",
    "education": "B.Tech CSE - Buddha Institute of Technology, Gorakhpur",
    "career_goal": "Data Analyst / ML Developer",
    "skills": [
      "Python",
      "Pandas",
      "NumPy",
      "Scikit-Learn",
      "SQL",
      "PowerBI",
      "Matplotlib"
    ],
    "github_username": "sadhna-maurya-ds",
    "github_url": "https://github.com/sadhna-maurya-ds",
    "projects": [
      {
        "title": "MSME Market Analytics Pipeline",
        "tech_stack": "Python, Pandas, Scikit-Learn, PowerBI",
        "github_url": "https://github.com/sadhna-maurya-ds/msme-analytics",
        "description": "Predictive analytics dashboard tracking industrial requirement trends."
      }
    ]
  },
  {
    "id": 104,
    "name": "Sahaj Singh",
    "email": "sahaj.singh@gmail.com",
    "education": "B.Tech Computer Science Engineering",
    "career_goal": "Backend Engineer / Systems Architect",
    "skills": [
      "C++",
      "Java",
      "Data Structures",
      "Algorithms",
      "System Design",
      "Docker",
      "SQL"
    ],
    "github_username": "sahajsingh-algo",
    "github_url": "https://github.com/sahajsingh-algo",
    "projects": [
      {
        "title": "Distributed Task Scheduler",
        "tech_stack": "C++, Redis, Docker",
        "github_url": "https://github.com/sahajsingh-algo/distributed-scheduler",
        "description": "High-throughput asynchronous task execution node."
      }
    ]
  },
  {
    "id": 105,
    "name": "Anjali Verma",
    "email": "anjali.verma@example.in",
    "education": "B.Tech Information Technology",
    "career_goal": "Python Backend Developer",
    "skills": [
      "Python",
      "Django",
      "Flask",
      "SQL",
      "Docker",
      "REST APIs",
      "Git"
    ],
    "github_username": "anjaliverma-dev",
    "github_url": "https://github.com/anjaliverma-dev",
    "projects": [
      {
        "title": "Smart Attendance Management API",
        "tech_stack": "Python, Flask, SQLite",
        "github_url": "https://github.com/anjaliverma-dev/attendance-api",
        "description": "QR and biometric backend logging API for institutions."
      }
    ]
  },
  {
    "id": 106,
    "name": "Rahul Sharma",
    "email": "rahul.sharma@example.in",
    "education": "B.Tech CSE - MMMUT Gorakhpur",
    "career_goal": "Java Backend Developer",
    "skills": [
      "Java",
      "Spring Boot",
      "Hibernate",
      "Microservices",
      "PostgreSQL",
      "Kafka",
      "Git"
    ],
    "github_username": "rahulsharma-java",
    "github_url": "https://github.com/rahulsharma-java",
    "projects": [
      {
        "title": "Microservices Payment Gateway Integration",
        "tech_stack": "Java, Spring Boot, Apache Kafka, PostgreSQL",
        "github_url": "https://github.com/rahulsharma-java/pay-gateway-service",
        "description": "Fault-tolerant transaction processing pipeline."
      }
    ]
  },
  {
    "id": 107,
    "name": "Priya Yadav",
    "email": "priya.yadav@example.in",
    "education": "B.Tech Computer Science",
    "career_goal": "Frontend Developer / UI Engineer",
    "skills": [
      "HTML",
      "CSS",
      "JavaScript",
      "React",
      "Redux",
      "Tailwind CSS",
      "Git"
    ],
    "github_username": "priyayadav-frontend",
    "github_url": "https://github.com/priyayadav-frontend",
    "projects": [
      {
        "title": "Vidyut Digital Notebook UI",
        "tech_stack": "React, Tailwind CSS, LocalStorage",
        "github_url": "https://github.com/priyayadav-frontend/vidyut-notebook",
        "description": "Responsive client-side notebook application."
      }
    ]
  },
  {
    "id": 108,
    "name": "Aditya Kumar",
    "email": "aditya.kumar@example.in",
    "education": "B.Tech CSE - AKTU",
    "career_goal": "Data Engineer",
    "skills": [
      "Python",
      "PySpark",
      "SQL",
      "MongoDB",
      "Data Pipelines",
      "Airflow",
      "Linux Basics"
    ],
    "github_username": "adityakumar-data",
    "github_url": "https://github.com/adityakumar-data",
    "projects": [
      {
        "title": "Real-time E-commerce ETL Pipeline",
        "tech_stack": "Python, Apache Spark, Airflow, MongoDB",
        "github_url": "https://github.com/adityakumar-data/ecommerce-etl",
        "description": "Automated pipeline processing batch and streaming logs."
      }
    ]
  },
  {
    "id": 109,
    "name": "Kavya Pandey",
    "email": "kavya.pandey@example.in",
    "education": "B.Tech IT - KNIT Sultanpur",
    "career_goal": "Cybersecurity Analyst",
    "skills": [
      "Computer Networks",
      "Linux Basics",
      "Cryptography",
      "Network Security",
      "Wireshark",
      "OWASP Top 10",
      "Python"
    ],
    "github_username": "kavyapandey-sec",
    "github_url": "https://github.com/kavyapandey-sec",
    "projects": [
      {
        "title": "Automated Network Vulnerability Scanner",
        "tech_stack": "Python, Linux, Scapy, Wireshark",
        "github_url": "https://github.com/kavyapandey-sec/network-vulnerability-scanner",
        "description": "Script scanner identifying open ports and vulnerable service versions."
      }
    ]
  },
  {
    "id": 110,
    "name": "Vikas Tiwari",
    "email": "vikas.tiwari@example.in",
    "education": "B.Tech CSE - HBTU Kanpur",
    "career_goal": "DevOps & Cloud Engineer",
    "skills": [
      "Linux Basics",
      "Docker",
      "Kubernetes",
      "AWS",
      "CI/CD",
      "Terraform",
      "Git"
    ],
    "github_username": "vikastiwari-devops",
    "github_url": "https://github.com/vikastiwari-devops",
    "projects": [
      {
        "title": "Kubernetes Multi-Cluster Deployment Pipeline",
        "tech_stack": "Docker, Kubernetes, GitHub Actions, Terraform",
        "github_url": "https://github.com/vikastiwari-devops/k8s-ci-cd-pipeline",
        "description": "Infrastructure as code repository for web app deployments."
      }
    ]
  },
  {
    "id": 111,
    "name": "Shreya Srivastava",
    "email": "shreya.srivastava@example.in",
    "education": "B.Tech Computer Science & Engineering",
    "career_goal": "Machine Learning Engineer",
    "skills": [
      "Python",
      "PyTorch",
      "Scikit-Learn",
      "NumPy",
      "Pandas",
      "Computer Vision",
      "Git"
    ],
    "github_username": "shreya-srivastava-ml",
    "github_url": "https://github.com/shreya-srivastava-ml",
    "projects": [
      {
        "title": "Crop Disease Detection Engine",
        "tech_stack": "Python, PyTorch, OpenCV, Flask",
        "github_url": "https://github.com/shreya-srivastava-ml/crop-disease-detection",
        "description": "Deep learning model identifying leaf spot infections."
      }
    ]
  },
  {
    "id": 112,
    "name": "Aman Gupta",
    "email": "aman.gupta@example.in",
    "education": "B.Tech Computer Science",
    "career_goal": "Full Stack MERN Developer",
    "skills": [
      "JavaScript",
      "TypeScript",
      "React",
      "Node.js",
      "Express",
      "MongoDB",
      "Git"
    ],
    "github_username": "amangupta-mern",
    "github_url": "https://github.com/amangupta-mern",
    "projects": [
      {
        "title": "MSME Vendor Portal Dashboard",
        "tech_stack": "React, Node.js, Express, MongoDB",
        "github_url": "https://github.com/amangupta-mern/msme-vendor-portal",
        "description": "Supplier inventory management interface."
      }
    ]
  },
  {
    "id": 113,
    "name": "Pooja Mishra",
    "email": "pooja.mishra@example.in",
    "education": "B.Tech Electronics & Communication",
    "career_goal": "Embedded Systems & IoT Engineer",
    "skills": [
      "C",
      "C++",
      "ESP32",
      "Raspberry Pi",
      "Arduino",
      "Python",
      "MQTT"
    ],
    "github_username": "poojamishra-iot",
    "github_url": "https://github.com/poojamishra-iot",
    "projects": [
      {
        "title": "Smart Industrial Agriculture Sensor Node",
        "tech_stack": "C++, ESP32, MQTT, FreeRTOS",
        "github_url": "https://github.com/poojamishra-iot/smart-agri-sensor",
        "description": "Low-power environmental monitoring telemetry node."
      }
    ]
  },
  {
    "id": 114,
    "name": "Rohan Gupta",
    "email": "rohan.gupta@example.in",
    "education": "B.Tech CSE - BIET Jhansi",
    "career_goal": "Android Application Developer",
    "skills": [
      "Kotlin",
      "Java",
      "Android SDK",
      "Jetpack Compose",
      "REST APIs",
      "SQLite",
      "Git"
    ],
    "github_username": "rohangupta-android",
    "github_url": "https://github.com/rohangupta-android",
    "projects": [
      {
        "title": "Rojgar Setu Mobile App",
        "tech_stack": "Kotlin, Jetpack Compose, Retrofit",
        "github_url": "https://github.com/rohangupta-android/rojgar-setu-app",
        "description": "Native Android app for job updates and skill tracking."
      }
    ]
  },
  {
    "id": 115,
    "name": "Nisha Tripathi",
    "email": "nisha.tripathi@example.in",
    "education": "B.Tech Information Technology",
    "career_goal": "Database Administrator & SQL Developer",
    "skills": [
      "SQL",
      "PostgreSQL",
      "MySQL",
      "Oracle DBMS",
      "Database Tuning",
      "Python",
      "Git"
    ],
    "github_username": "nishatripathi-db",
    "github_url": "https://github.com/nishatripathi-db",
    "projects": [
      {
        "title": "High-Availability DB Schema Optimization",
        "tech_stack": "PostgreSQL, Python, SQL",
        "github_url": "https://github.com/nishatripathi-db/pg-schema-optimizer",
        "description": "Indexing and transaction lock optimization scripts."
      }
    ]
  },
  {
    "id": 116,
    "name": "Harsh Vardhan",
    "email": "harsh.vardhan@example.in",
    "education": "B.Tech CSE - IET Lucknow",
    "career_goal": "Software Engineer & DSA Expert",
    "skills": [
      "C++",
      "Data Structures",
      "Algorithms",
      "OOPs",
      "DBMS",
      "System Design",
      "Git"
    ],
    "github_username": "harshvardhan-dsa",
    "github_url": "https://github.com/harshvardhan-dsa",
    "projects": [
      {
        "title": "Competitive Problem Solutions Repository",
        "tech_stack": "C++, Data Structures, Algorithms",
        "github_url": "https://github.com/harshvardhan-dsa/leetcode-solutions",
        "description": "500+ verified algorithms with step-by-step walkthroughs."
      }
    ]
  },
  {
    "id": 117,
    "name": "Divya Sharma",
    "email": "divya.sharma@example.in",
    "education": "B.Tech Computer Science",
    "career_goal": "Cloud Solutions Architect",
    "skills": [
      "AWS",
      "Python",
      "Docker",
      "Linux Basics",
      "REST APIs",
      "Terraform",
      "Git"
    ],
    "github_username": "divyasharma-cloud",
    "github_url": "https://github.com/divyasharma-cloud",
    "projects": [
      {
        "title": "Serverless API Architecture on AWS",
        "tech_stack": "AWS Lambda, Python, API Gateway, DynamoDB",
        "github_url": "https://github.com/divyasharma-cloud/aws-serverless-api",
        "description": "Scalable REST microservice deployed via SAM."
      }
    ]
  },
  {
    "id": 118,
    "name": "Shivam Jaiswal",
    "email": "shivam.jaiswal@example.in",
    "education": "B.Tech IT - REC Banda",
    "career_goal": "Backend FastApi Developer",
    "skills": [
      "Python",
      "FastAPI",
      "SQLAlchemy",
      "PostgreSQL",
      "Docker",
      "Redis",
      "Git"
    ],
    "github_username": "shivamjaiswal-backend",
    "github_url": "https://github.com/shivamjaiswal-backend",
    "projects": [
      {
        "title": "Real-time Notification Microservice",
        "tech_stack": "FastAPI, WebSockets, Redis, Docker",
        "github_url": "https://github.com/shivamjaiswal-backend/fastapi-notifications",
        "description": "Asynchronous messaging hub for recruiter updates."
      }
    ]
  },
  {
    "id": 119,
    "name": "Prachi Saxena",
    "email": "prachi.saxena@example.in",
    "education": "B.Tech Computer Science",
    "career_goal": "UI/UX & Frontend Developer",
    "skills": [
      "HTML",
      "CSS",
      "JavaScript",
      "Figma",
      "React",
      "Bootstrap",
      "Git"
    ],
    "github_username": "prachisaxena-ui",
    "github_url": "https://github.com/prachisaxena-ui",
    "projects": [
      {
        "title": "Career Twin Minimalist Design System",
        "tech_stack": "Figma, React, CSS Modules",
        "github_url": "https://github.com/prachisaxena-ui/career-twin-design-system",
        "description": "Accessible dark/light design system for student portals."
      }
    ]
  },
  {
    "id": 120,
    "name": "Abhishek Dubey",
    "email": "abhishek.dubey@example.in",
    "education": "B.Tech CSE - REC Bijnor",
    "career_goal": "AI & Natural Language Processing Specialist",
    "skills": [
      "Python",
      "Transformers",
      "NLP",
      "PyTorch",
      "Flask",
      "Scikit-Learn",
      "Git"
    ],
    "github_username": "abhishekdubey-ai",
    "github_url": "https://github.com/abhishekdubey-ai",
    "projects": [
      {
        "title": "Resume Skill Extractor Model",
        "tech_stack": "Python, SpaCy, HuggingFace Transformers, Flask",
        "github_url": "https://github.com/abhishekdubey-ai/resume-skill-extractor",
        "description": "NER model parsing domain technical terms from PDF files."
      }
    ]
  },
  {
    "id": 121,
    "name": "Ritu Singh",
    "email": "ritu.singh@example.in",
    "education": "B.Tech CSE - Buddha Institute of Technology",
    "career_goal": "Software QA & Test Automation Engineer",
    "skills": [
      "Python",
      "Selenium",
      "PyTest",
      "Postman",
      "SQL",
      "Git",
      "Jira"
    ],
    "github_username": "ritusingh-qa",
    "github_url": "https://github.com/ritusingh-qa",
    "projects": [
      {
        "title": "Automated API Test Suite",
        "tech_stack": "Python, PyTest, Postman, Requests",
        "github_url": "https://github.com/ritusingh-qa/api-test-automation",
        "description": "End-to-end regression test suite for placement APIs."
      }
    ]
  },
  {
    "id": 122,
    "name": "Deepak Gautam",
    "email": "deepak.gautam@example.in",
    "education": "B.Tech Computer Science",
    "career_goal": "Blockchain & Smart Contract Developer",
    "skills": [
      "Solidity",
      "JavaScript",
      "Ethereum",
      "Web3.js",
      "Node.js",
      "Git",
      "Cryptography"
    ],
    "github_username": "deepakgautam-web3",
    "github_url": "https://github.com/deepakgautam-web3",
    "projects": [
      {
        "title": "Verified Degree Certificate Registry",
        "tech_stack": "Solidity, Web3.js, React, Ethereum",
        "github_url": "https://github.com/deepakgautam-web3/degree-verification-dapp",
        "description": "Tamper-proof academic credential verification smart contract."
      }
    ]
  }
]

@app.route("/api/students", methods=["GET"])
def get_all_students_directory():
    """Return clearly labelled demo student profiles for recruiter UI demonstrations."""
    students = []
    for source in SEED_STUDENTS_LIST:
        student = dict(source)
        student["demo_profile"] = True
        student["resume_label"] = "SAMPLE RESUME — DEMO DATA, NOT A VERIFIED REAL CANDIDATE"
        students.append(student)
    return jsonify({
        "success": True,
        "count": len(students),
        "demo_data": True,
        "note": "These are sample profiles for demonstration only. Replace them with consented student profiles before production.",
        "students": students
    })

@app.route("/api/students/<int:student_id>/resume.txt", methods=["GET"])
def download_demo_student_resume(student_id):
    """Build a plain-text sample resume from a demo profile."""
    import json
    student = next((s for s in SEED_STUDENTS_LIST if int(s.get("id", -1)) == student_id), None)
    if not student:
        return json_error("Student profile not found.", 404)
    projects = student.get("projects", [])
    lines = [
        "SAMPLE RESUME — DEMO DATA, NOT A VERIFIED REAL CANDIDATE",
        "",
        student.get("name", "Demo Student"),
        student.get("email", ""),
        student.get("education", ""),
        "",
        "CAREER OBJECTIVE",
        student.get("career_goal", "Entry-level technology role"),
        "",
        "TECHNICAL SKILLS",
        ", ".join(student.get("skills", [])),
        "",
        "PROJECTS"
    ]
    if projects:
        for project in projects:
            lines += [
                "- " + project.get("title", "Project"),
                "  Tech stack: " + project.get("tech_stack", ""),
                "  " + project.get("description", ""),
                "  Repository: " + project.get("github_url", ""),
                ""
            ]
    else:
        lines.append("Project details not provided.")
    lines += ["", "GitHub profile: " + student.get("github_url", ""),
              "", "NOTE: This generated resume is for UI/demo testing only. Details and links have not been independently verified."]
    response = app.response_class("\n".join(lines), mimetype="text/plain; charset=utf-8")
    response.headers["Content-Disposition"] = f'attachment; filename="sample_resume_{student_id}.txt"'
    return response

if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG", "0") == "1", port=int(os.environ.get("PORT", "5000")))
