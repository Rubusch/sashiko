#!/usr/bin/env python3
"""
vector RAG

Searches commits, saves results in a chromadb db (vector db)

TODO we need graph RAG based on AST, does this solution satisfy?
"""
import os
import re
from git import Repo
import chromadb
from chromadb.config import Settings

# --- CONFIGURATION ---
## TODO do args
KERNEL_PATH = "/path/to/your/linux-kernel-repo"  # Change to your local kernel path
DB_DIR = "./sashiko_crypto_rag_db"
COLLECTION_NAME = "kernel_crypto_bugfixes"

# Targets paths matching your subsystem requests
## TODO do configurable
TARGET_PATHS = [
    "drivers/crypto/",
    "drivers/char/hw_random/",
    "include/crypto/"
]

# High-signal semantic search terms for git log matching your bug requirements
## TODO do configurable
BUG_KEYWORDS = [
    "leak", "uaf", "use-after-free", "free", "race", 
    "lock", "mutex", "spin_lock", "deadlock", "concurrency", 
    "synchronization", "refcount", "double free"
]

def setup_vector_db():
    """Initializes local file-backed ChromaDB store."""
    client = chromadb.PersistentClient(path=DB_DIR)
    # Uses default lightweight embedding model (all-MiniLM-L6-v2) natively
    collection = client.get_or_create_collection(name=COLLECTION_NAME)
    return collection

def extract_filtered_commits(repo_path, paths, keywords):
    """Gathers commits matching specific sub-paths and bug tracking rules."""
    repo = Repo(repo_path)
    print(f"[*] Analyzing repository at: {repo_path}")
    
    # Compile a combined regex search for speed
    keyword_regex = re.compile("|".join(keywords), re.IGNORECASE)
    seen_commits = set()
    filtered_data = []

    for path in paths:
        print(f"[*] Extracting high-signal logs from: {path}")
        # Fetch up to 1500 historical commits per path for deep history density
## TODO do upper limit configurable, e.g. per key word? use dict approach?
        commits = list(repo.iter_commits(paths=path, max_count=1500))
        
        for commit in commits:
            if commit.hexsha in seen_commits:
                continue
                
            # Verify if commit message targets memory/locking structures
            if keyword_regex.search(commit.message):
                seen_commits.add(commit.hexsha)
                
                # Extract the commit summary, message, and unified diff summary text
                try:
                    # Limit diff scope sizes to prevent context blowout
                    diff_text = repo.git.show(commit.hexsha, "--", path, max_lines=300)
                    filtered_data.append({
                        "sha": commit.hexsha,
                        "summary": commit.summary,
                        "message": commit.message,
                        "diff": diff_text,
                        "path": path
                    })
                except Exception as e:
                    # Safely pass binary blobs/merge conflict edge errors
                    continue
                    
    print(f"[+] Found {len(filtered_data)} bug-centric reference commits.")
    return filtered_data

def chunk_and_index(collection, commit_records):
    """Slices commit logs and diff modifications into searchable payloads."""
    print("[*] Chunking documentation data and generating vectors...")
    
    id_counter = 0
    for record in commit_records:
        # Construct the context payload block
        payload_text = (
            f"COMMIT SHA: {record['sha']}\n"
            f"SUBSYSTEM PATH: {record['path']}\n"
            f"SUMMARY: {record['summary']}\n"
            f"COMMIT DESCRIPTION:\n{record['message']}\n"
            f"CODE CHANGES / DIFF:\n{record['diff']}"
        )
        
        # Break large diff changes into 1500 character semantic chunks
        # with 200 character overlaps to preserve context continuity
## TODO make this configurable
        chunk_size = 1500
        overlap = 200
        
        for i in range(0, len(payload_text), chunk_size - overlap):
            chunk = payload_text[i:i + chunk_size]
            
            metadata = {
                "sha": record["sha"],
                "subsystem": record["path"],
                "summary": record["summary"]
            }
            
            collection.add(
                documents=[chunk],
                metadatas=[metadata],
                ids=[f"commit_{record['sha']}_{id_counter}"]
            )
            id_counter += 1

    print(f"[+] Successfully loaded context structures into {DB_DIR}")

if __name__ == "__main__":
    if not os.path.exists(KERNEL_PATH):
        print(f"[!] Target path error: Update KERNEL_PATH with your local tree layout.")
    else:
        db_collection = setup_vector_db()
        matched_commits = extract_filtered_commits(KERNEL_PATH, TARGET_PATHS, BUG_KEYWORDS)
        if matched_commits:
            chunk_and_index(db_collection, matched_commits)

print("READY.\n")
