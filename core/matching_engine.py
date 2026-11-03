"""Explainable, deterministic skill matching for CareerTwin X.

Scores are coverage percentages over the explicit required-skill checklist, not
probabilities of getting hired. This keeps the score easy to explain and test.
"""
import json
import os
import re

ALIASES = {
    "dsa": "data structures",
    "data structure": "data structures",
    "algorithms": "algorithms",
    "oops": "oops",
    "object oriented programming": "oops",
    "rest api": "rest apis",
    "api": "rest apis",
    "scikit learn": "scikit-learn",
    "sklearn": "scikit-learn",
    "numpy": "numpy",
    "js": "javascript",
    "postgres": "postgresql",
    "linux": "linux basics",
}

def normalize_skill(value):
    value = re.sub(r"\s+", " ", str(value or "").strip().lower())
    value = value.strip(" .,:;|-/")
    return ALIASES.get(value, value)

def parse_skills(value):
    if isinstance(value, str):
        parts = re.split(r"[,;\n|]+", value)
    elif isinstance(value, (list, tuple, set)):
        parts = list(value)
    else:
        parts = []
    out, seen = [], set()
    for part in parts:
        skill = normalize_skill(part)
        if skill and skill not in seen:
            out.append(skill)
            seen.add(skill)
    return out

def load_benchmarks():
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(base_dir, "data", "benchmarks.json"), encoding="utf-8") as file:
        return json.load(file)

def match_skills(user_skills, required_skills):
    user = set(parse_skills(user_skills))
    required = list(required_skills or [])
    normalized_required = [(skill, normalize_skill(skill)) for skill in required]
    acquired = [display for display, norm in normalized_required if norm in user]
    missing = [display for display, norm in normalized_required if norm not in user]
    score = round((len(acquired) / len(required)) * 100) if required else 0
    return {"match_percentage": score, "acquired_skills": acquired, "missing_skills": missing,
            "required_skill_count": len(required), "matched_skill_count": len(acquired),
            "score_explanation": f"{len(acquired)} of {len(required)} required skills matched; each required skill contributes equally."}

def analyze_career_readiness(user_skills, target_role_key):
    benchmarks = load_benchmarks()
    if target_role_key not in benchmarks:
        return {"error": "Invalid target role specified."}
    role_data = benchmarks[target_role_key]
    parsed_user = parse_skills(user_skills)
    result = match_skills(parsed_user, role_data.get("required_skills", []))
    user_set = set(parsed_user)
    roadmap = []
    for skill in result["missing_skills"]:
        prerequisite = role_data.get("prerequisites", {}).get(skill)
        prerequisite_missing = prerequisite and normalize_skill(prerequisite) not in user_set
        roadmap.append({
            "skill": skill,
            "status": f"Learn {prerequisite} first" if prerequisite_missing else "Ready to learn",
            "prerequisite": prerequisite if prerequisite_missing else None,
            "description": role_data.get("descriptions", {}).get(skill, "Build this competency through a small practical project."),
            "suggested_project": f"Build a small portfolio project that demonstrates {skill}."
        })
    return {
        "role_title": role_data["title"], "role_name": role_data["title"], "role": target_role_key,
        **result, "score": result["match_percentage"], "gaps": result["missing_skills"],
        "roadmap": roadmap, "user_skills": parsed_user,
        "disclaimer": "Skill coverage indicator only; it is not a hiring probability or employer endorsement."
    }
