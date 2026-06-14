#!/usr/bin/env python3
"""
knowledge RAG

Execute this script inside your source repository

Searches commits, saves result in a .json file
$ ./01_*.py

TODO apply this time delta approach in the selected script!
"""
import json
import subprocess
## TODO use: from git import Repo
from datetime import datetime, timedelta
import os

## TODO do args
REPO_PATH = "." # TODO arg?
## TODO make this configurable
SUBSYSTEM_PATHS = ["drivers/crypto", "drivers/char/hw_random", "include/crypto"]
OUTPUT_FILE = "crypto_historical_bugs.json"

## target terms
KEYWORDS = ["mutex", "use-after-free", "uaf", "race condition", "lock", "deadlock", "sync"]

def get_git_commits():
    ## last 5 years from today
## TODO make temporal scope configurable
    five_years_ago = (datetime.now() - timedelta(days=5*365)).strftime('%Y-%m-%d')

    ## extract hash, subject, and full body separated by a delimiter
    ## %H = Commit Hash, %s = Subject, %b = Body
    delimiter = "===COMMIT_DELIMITER==="
    cmd = [
        "git", "log",
        f"--since={five_years_ago}",
        f"--format=%H%n%s%n%b{delimiter}"
    ] + SUBSYSTEM_PATHS

    print(f"Scanning git history since {five_years_ago} for paths: {', '.join(SUBSYSTEM_PATHS)}...")
    try:
        result = subprocess.run(cmd, cwd=REPO_PATH, capture_output=True, text=True, check=True)
        return result.stdout.split(delimiter)
    except subprocess.CalledProcessError as e:
        print(f"Error running git command: {e.stderr}")
        return []

def parse_commits(raw_commits):
    vulnerabilities = []

    for raw_block in raw_commits:
        lines = [line.strip() for line in raw_block.strip().split('\n') if line.strip()]
        if len(lines) < 2:
            continue

        commit_hash = lines[0]
        subject = lines[1]
        body = " ".join(lines[2:])
        full_text = f"{subject} {body}".lower()

# Condition 1: Must contain 'Fixes:' tag (standard practice for stable backports)
## TODO really?  why just 'fixes:'?
        if "fixes:" not in full_text:
            continue

# Condition 2: Must match at least one of our core architectural vulnerability keywords
        matched_keywords = [kw for kw in KEYWORDS if kw in full_text]

        if matched_keywords:
            vulnerabilities.append({
                "hash": commit_hash,
                "subject": subject,
                "matched_keywords": matched_keywords,
                "body": "\n".join(lines[2:])  # Preserve layout for LLM to extract context later
            })
    return vulnerabilities

def main():
    if not os.path.exists(os.path.join(REPO_PATH, ".git")):
        print("Error: Please run this script inside the root of a valid git repository.")
        return

    raw_commits = get_git_commits()
    parsed_bugs = parse_commits(raw_commits)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(parsed_bugs, f, indent=4, ensure_ascii=False)

    print(f"Extraction complete! Found {len(parsed_bugs)} relevant bug-fixing commits.")
    print(f"Results saved to: {OUTPUT_FILE}")

if __name__ == "__main__":
    main()
