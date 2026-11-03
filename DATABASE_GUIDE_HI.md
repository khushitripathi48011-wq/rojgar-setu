# CareerTwin X — Database Guide (Hindi)

## Database pehle se integrated hai
Project SQLite database use karta hai. App start hone par `data/career_twin.db` file aur tables automatically ban jaate hain. Alag se database server install karne ki zaroorat nahi.

## Kaunsa data save hota hai?
- `users`: student account aur password ka secure hash
- `students`: education, career goal aur skills
- `companies`: MSME/company profile aur verification status
- `jobs`: companies ki job postings
- `applications`: student applications, matching score aur status
- `login_attempts`: login attempts ka record

## Run kaise karein (Windows / VS Code)
1. VS Code mein project folder kholein.
2. Terminal mein chalayein:
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install -r requirements.txt
   python app.py
   ```
3. Browser mein `http://127.0.0.1:5000` kholein.
4. Database file `data/career_twin.db` mein hogi. App band karke dobara chalane par bhi data rahega.

## Database check
Browser mein `http://127.0.0.1:5000/api/health` kholein. Healthy app par JSON response mein `"database": "connected"` aana chahiye.

## Important
- `data/career_twin.db` ko delete na karein, warna saved data chala jayega.
- Backup ke liye app band karke `data/career_twin.db` ki copy bana lein.
- `CAREERTWIN_SECRET_KEY` ko deployment par environment variable se set karein; local development key restart par badal sakti hai.
- Company verification status default mein `Pending verification` hota hai. App khud se kisi company ko verified nahi maanta.
