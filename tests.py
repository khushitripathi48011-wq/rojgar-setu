"""Smoke/integration tests: run with `python -m unittest tests.py`."""
import json, os, tempfile, unittest
from app import app
from core.matching_engine import analyze_career_readiness, match_skills

class CareerTwinTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        app.config.update(TESTING=True, SECRET_KEY='test-secret-key')
        import app as app_module
        self.old_db = app_module.DB_PATH
        app_module.DB_PATH = os.path.join(self.tmp.name, 'test.db')
        app_module.init_db()
        self.client = app.test_client()

    def tearDown(self):
        import app as app_module
        app_module.DB_PATH = self.old_db
        self.tmp.cleanup()

    def test_home_page_renders(self):
        response=self.client.get('/')
        self.assertEqual(response.status_code,200)
        self.assertIn(b'CareerTwin',response.data)

    def test_analyze_and_evaluate_are_compatible(self):
        payload = {'role':'sde','skills':'Python, SQL, Git, OOPs'}
        a = self.client.post('/analyze', json=payload)
        b = self.client.post('/evaluate', json=payload)
        self.assertEqual(a.status_code, 200); self.assertEqual(b.status_code, 200)
        self.assertEqual(a.json['score'], b.json['score'])
        self.assertEqual(a.json['gaps'], b.json['gaps'])
        self.assertIn('score_explanation', a.json)
        self.assertEqual(a.json['score'], round(4/9*100))

    def test_msme_directory_and_demo_matches(self):
        d=self.client.get('/api/company-directory').json
        self.assertEqual(len(d),12); self.assertTrue(all(c['sample'] for c in d))
        self.assertFalse(any('careers_url' in c for c in d))
        m=self.client.post('/api/matches',json={'skills':'Python, SQL','include_demo':True}).json
        self.assertEqual(m['count'],12); self.assertTrue(all(x['demo'] for x in m['matches']))

    def test_github_link_saved_with_application(self):
        from app import normalize_github_url
        self.assertEqual(normalize_github_url('github.com/abc/repo.git'),'https://github.com/abc/repo')
        self.assertEqual(normalize_github_url('www.github.com/abc'),'https://github.com/abc')
        self.assertIsNone(normalize_github_url('https://evil.com/abc/repo'))
        self.assertIsNone(normalize_github_url('github.com/abc',require_repo=True))
        company=self.client.__class__(app) if False else app.test_client()
        c=company.post('/api/companies',json={'company_name':'GH MSME','industry':'IT','email':'gh@test.example','location':'Lucknow','password':'company-pass-123'}).json
        job=company.post('/api/jobs',json={'company_id':c['company_id'],'title':'Dev','job_type':'Internship','skills':'Python','location':'Lucknow'}).json
        student=app.test_client()
        student.post('/api/auth/register',json={'name':'Student One','email':'s@test.example','password':'student-pass-123'})
        r=student.post('/api/applications',json={'job_id':job['job_id'],'skills':'Python','github_url':'github.com/s/proj'})
        self.assertEqual(r.status_code,201); self.assertEqual(r.json['github_url'],'https://github.com/s/proj')
        rows=company.get(f"/api/recruiter/jobs/{job['job_id']}/applications").json
        self.assertEqual(rows[0]['github_url'],'https://github.com/s/proj')

    def test_invalid_role_is_400(self):
        self.assertEqual(self.client.post('/evaluate', json={'role':'bad','skills':'python'}).status_code, 400)

    def test_company_job_match_and_persistence(self):
        c = self.client.post('/api/companies', json={'company_name':'Test MSME','industry':'IT','email':'jobs@test.example','location':'Varanasi','password':'company-pass-123'})
        self.assertEqual(c.status_code,201)
        cid = c.json['company_id']
        j = self.client.post('/api/jobs', json={'company_id':cid,'title':'Python Intern','job_type':'Internship','skills':'Python, SQL, Git','location':'Remote'})
        self.assertEqual(j.status_code,201)
        matches = self.client.post('/api/matches', json={'skills':'Python, Git'})
        self.assertEqual(matches.status_code,200)
        self.assertEqual(matches.json['count'],1)
        self.assertEqual(matches.json['matches'][0]['match_percentage'],67)
        self.assertEqual(matches.json['matches'][0]['missing_skills'],['sql'])
        self.assertEqual(len(self.client.get('/api/jobs').json),1)

    def test_duplicate_company_email_rejected(self):
        data={'company_name':'Test','industry':'IT','email':'jobs@test.example','location':'Varanasi','password':'company-pass-123'}
        self.assertEqual(self.client.post('/api/companies',json=data).status_code,201)
        self.assertEqual(self.client.post('/api/companies',json=data).status_code,409)

    def test_company_login_and_company_job_ownership(self):
        company_client=app.test_client()
        reg=company_client.post('/api/companies',json={'company_name':'Owner Co','industry':'IT','email':'owner@test.example','location':'Remote','password':'owner-pass-123'})
        self.assertEqual(reg.status_code,201)
        company_id=reg.json['company_id']
        company_client.post('/api/company/logout')
        login=company_client.post('/api/company/login',json={'email':'owner@test.example','password':'owner-pass-123'})
        self.assertEqual(login.status_code,200)
        other=app.test_client().post('/api/companies',json={'company_name':'Other Co','industry':'IT','email':'other@test.example','location':'Remote','password':'other-pass-123'}).json
        forbidden=company_client.post('/api/jobs',json={'company_id':other['company_id'],'title':'Not yours','job_type':'Internship','skills':'Python','location':'Remote'})
        self.assertEqual(forbidden.status_code,403)
        self.assertEqual(company_client.post('/api/jobs',json={'company_id':company_id,'title':'Owned role','job_type':'Internship','skills':'Python','location':'Remote'}).status_code,201)

    def test_password_hash_login_and_application_flow(self):
        reg=self.client.post('/api/auth/register',json={'name':'Student Test','email':'student@test.example','password':'correct-horse-123'})
        self.assertEqual(reg.status_code,201)
        with self.client.session_transaction() as sess: self.assertIn('user_id',sess)
        with self.client.application.app_context():
            import app as app_module
            with app_module.get_db() as db:
                row=db.execute('SELECT password_hash FROM users WHERE email=?',('student@test.example',)).fetchone()
                self.assertNotEqual(row['password_hash'],'correct-horse-123')
                self.assertTrue(row['password_hash'].startswith('scrypt:'))
        self.client.post('/api/auth/logout')
        self.assertEqual(self.client.post('/api/auth/login',json={'email':'student@test.example','password':'wrong'}).status_code,401)
        self.assertEqual(self.client.post('/api/auth/login',json={'email':'student@test.example','password':'correct-horse-123'}).status_code,200)
        c=self.client.post('/api/companies',json={'company_name':'Test MSME','industry':'IT','email':'jobs@test.example','location':'Varanasi','password':'company-pass-123'}).json
        j=self.client.post('/api/jobs',json={'company_id':c['company_id'],'title':'Python Intern','job_type':'Internship','skills':'Python, SQL','location':'Remote'}).json
        app_resp=self.client.post('/api/applications',json={'job_id':j['job_id'],'skills':'Python'})
        self.assertEqual(app_resp.status_code,201)
        self.assertEqual(app_resp.json['match_percentage'],50)
        self.assertEqual(self.client.post('/api/applications',json={'job_id':j['job_id'],'skills':'Python'}).status_code,409)
        self.assertEqual(len(self.client.get('/api/applications').json),1)

    def test_company_can_review_and_shortlist_own_applications(self):
        company_client = app.test_client()
        company = company_client.post('/api/companies', json={'company_name':'Hiring MSME','industry':'IT','email':'hire@test.example','location':'Varanasi','password':'company-pass-123'}).json
        job = company_client.post('/api/jobs', json={'company_id':company['company_id'],'title':'Python Intern','job_type':'Internship','skills':'Python, SQL','location':'Remote'}).json
        student_client = app.test_client()
        student_client.post('/api/auth/register', json={'name':'Applicant One','email':'applicant@test.example','password':'student-pass-123'})
        applied = student_client.post('/api/applications', json={'job_id':job['job_id'],'skills':'Python'})
        self.assertEqual(applied.status_code,201)
        rows = company_client.get(f"/api/recruiter/jobs/{job['job_id']}/applications")
        self.assertEqual(rows.status_code,200)
        self.assertEqual(len(rows.json),1)
        updated = company_client.patch(f"/api/applications/{rows.json[0]['id']}/status", json={'status':'Shortlisted'})
        self.assertEqual(updated.status_code,200)
        self.assertEqual(student_client.get('/api/applications').json[0]['status'],'Shortlisted')

    def test_mentor_uses_provided_context(self):
        response=self.client.post('/api/mentor',json={'question':'How do I close gaps?','career_goal':'Data Science','skills':['Python'],'missing_skills':['Pandas','Statistics']})
        self.assertEqual(response.status_code,200)
        self.assertIn('pandas',response.json['answer'].lower())
        self.assertEqual(response.json['personalized_to']['career_goal'],'Data Science')

    def test_candidate_access_requires_company_login(self):
        self.assertEqual(self.client.get('/api/recruiter/jobs/1/applications').status_code,401)

if __name__ == '__main__': unittest.main(verbosity=2)
