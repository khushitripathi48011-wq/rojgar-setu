# CareerTwin X / Rojgar Setu — updated prototype

This version strengthens the existing Flask + HTML/JS application rather than replacing its frontend.

## Run on Windows / VS Code

Open the `rozgarsetu-engine` folder in VS Code, then use Terminal → New Terminal:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python app.py
```

Open `http://127.0.0.1:5000`. Keep the terminal running while you use the app. To deploy publicly, configure environment secrets and use a production WSGI server; do not expose Flask debug mode.

## What was fixed / added

- `/evaluate` and `/analyze` return the same response schema: `score`, `gaps`, `role_name`, matched/missing skills, explanation and a skill-specific roadmap.
- Explainable skill coverage is calculated as matched required skills divided by listed required skills. It is **not** a hiring probability.
- SQLite stores student accounts, student profile/skills, company accounts, jobs, applications, match evidence and application status.
- Student email/password accounts use Werkzeug password hashes; passwords are not stored in browser localStorage. Login attempts are throttled. Sessions use HttpOnly and SameSite=Lax cookies.
- MSME registration creates a company login. Company password must be at least 10 characters. Companies can post jobs only for their own logged-in account.
- Student applications are saved with matched/missing skill lists. The company that owns a job can view its applications and update status to Shortlisted or Interview.
- The recruiter panel includes a searchable directory of 22 clearly labelled sample student profiles, with viewable and downloadable generated TXT resumes. These are demo-only records, not verified real candidates; replace them with consented real profiles before production.\n- Students can save up to 20 portfolio projects with a GitHub repository URL, tech stack, description, and optional live demo. Recruiters reviewing applicants for their own jobs see these links as proof-of-work evidence. GitHub links are not automatically verified and do not guarantee that a candidate authored every line of code.
- The `static/hero-avatar.png` asset is included so the hero and dashboard AI avatar render again.\n- The company directory contains clearly labelled sample MSME companies; they are not real or verified employers.
- Opportunity matching uses jobs posted by companies registered in this database. No fabricated employer names or sample match percentages are presented as real opportunities.
- The mentor endpoint uses the user's supplied skills, goal and gaps for contextual rule-based advice. It is not connected to an LLM provider.
- Resume uploads are limited to 5 MB and PDF/TXT only. Extraction is keyword-based and should be reviewed by the student.
- Basic security headers, input checks, ownership checks and API smoke tests were added.

## Tests

Run from this folder after installing requirements:

```powershell
python -m unittest tests.py -v
```

Tests cover API alias compatibility, invalid roles, company/job persistence, skill matching, account password hashing/login, duplicate applications, company-only application review/shortlisting, and contextual mentor responses.

## Important limitations before production / SIH final submission

- A company remains `Pending verification` until a real human/admin verifies its documents. Entering an Udyam number is not verification.
- SMS OTP is available only when a real identity provider (the existing optional Clerk integration) is correctly configured. Do not claim OTP is working without testing the provider and production keys.
- Email/password authentication is a prototype baseline, not a full security audit. Use HTTPS, a stable secret key from `CAREERTWIN_SECRET_KEY`, backups, CSRF protection appropriate to deployment, and professional security review before public production use.
- Company accounts are password-protected, but email verification, company-document/admin verification, password reset and recruiter notifications are not implemented.
- The mentor is contextual and rule-based; no LLM API is configured. The matching engine is explainable rule-based skill coverage, not trained ML and not a hiring probability.
- Job records are only as trustworthy as the company that submits them. Do not call opportunities verified unless the separate verification process has been completed.
- Automated tests are not a substitute for testing the full browser workflow on the target laptop and hosting environment.

## MSME company directory (sample)

The recruiter panel lists 12 clearly labelled sample MSME companies (Uttar Pradesh) with one sample opening each, replacing the earlier MNC list. They are demo data only and appear in Job Matches as non-applicable sample openings.

## GitHub links and recruiter selection

- Job Matches: each real job card has a GitHub link field (suggestions come from your saved projects), an "Open link" button and the link is saved with the application. Links typed as `github.com/user/repo` are normalised to `https://...`.
- Recruiters can see the applicant's GitHub link in the applications table.
- Student Resume Directory: recruiters can Select students (also from the resume popup), "Select top 5 shown", clear the list and download selected students as CSV. Selection is stored in the browser only.
