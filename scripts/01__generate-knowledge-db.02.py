#!/usr/bin/env python3
"""
knowledge RAG via sqlite/rusqlite db

Searches commits, saves results as TODO

uses tree_sitter for AST parsing (correct?)

TODO optimize the tree-sitter queries to explicitly track asynchronous crypto callback functions
TODO args: path to repo
TODO configuration: error pattern
TODO configuration: time delta
TODO configuration: subsystem(s) to search through
TODO configuration: max number of items
TODO use a time delta concept, configurable for inputs
"""

import os
import sqlite3
import re
from git import Repo
from tree_sitter import Language, Parser
import tree_sitter_c as tsc

C_LANGUAGE = Language(tsc.language())
parser = Parser(C_LANGUAGE)

#def setup_database(db_path):
### TODO assure a generic layout, to leave bug types, subsystem, temporal scope as open as possible
#    conn = sqlite3.connect(db_path)
#    cursor = conn.cursor()
#    cursor.executescript('''
#        CREATE TABLE IF NOT EXISTS entities (
#            id INTEGER PRIMARY KEY,
#            name TEXT NOT NULL UNIQUE,
#            type TEXT NOT NULL
#        );
#        CREATE TABLE IF NOT EXISTS bug_patterns (
#            id INTEGER PRIMARY KEY,
#            subsystem TEXT NOT NULL,
#            bug_type TEXT NOT NULL,
#            description TEXT NOT NULL,
#            remedy_template TEXT
#        );
#        CREATE TABLE IF NOT EXISTS bug_edges (
#            entity_id INTEGER,
#            pattern_id INTEGER,
#            FOREIGN KEY(entity_id) REFERENCES entities(id),
#            FOREIGN KEY(pattern_id) REFERENCES bug_patterns(id),
#            UNIQUE(entity_id, pattern_id)
#        );
#    ''')
#    conn.commit()
#    return conn
# Inside your python pipeline: generate-db.py

def setup_database(db_path):
    # Point directly to your active local sashiko.db file path
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.executescript('''
        CREATE TABLE IF NOT EXISTS rag_entities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            type TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS rag_bug_patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subsystem TEXT NOT NULL,
            bug_type TEXT NOT NULL,
            description TEXT NOT NULL,
            remedy_template TEXT
        );
        CREATE TABLE IF NOT EXISTS rag_bug_edges (
            entity_id INTEGER,
            pattern_id INTEGER,
            FOREIGN KEY(entity_id) REFERENCES rag_entities(id),
            FOREIGN KEY(pattern_id) REFERENCES rag_bug_patterns(id),
            PRIMARY KEY(entity_id, pattern_id)
        );
    ''')
    conn.commit()
    return conn

def extract_entities_from_ast(file_content: str):
    """
    Parses a C source file into an AST and pulls out exact function definitions,
    structure initializations, and locking operations.
    """
    if not file_content:
        return set()

    # Parse source string into bytes
    tree = parser.parse(bytes(file_content, "utf8"))
    root_node = tree.root_node
    
    entities = set()

    # Pre-compiled tree-sitter queries for high performance mapping
    # 1. Finds function definitions: e.g., static int crypto_hash(...) { ... }
    function_query = C_LANGUAGE.query("""
        (function_definition
            declarator: (function_declarator
                declarator: (identifier) @func_name))
    """)

    # 2. Finds targeted lock macros and calls: e.g., spin_lock(&lock), mutex_lock(...)
    lock_query = C_LANGUAGE.query("""
        (call_expression
            function: (identifier) @lock_fn
            (#match? @lock_fn "^(spin_lock|mutex_lock|spin_unlock|mutex_unlock|spin_lock_irqsave|spin_unlock_irqrestore)$"))
    """)

    # 3. Finds explicit type references to crypto structs
    struct_query = C_LANGUAGE.query("""
        (struct_specifier
            name: (identifier) @struct_name)
    """)

    # Execute Function Definition Query
    for node, tag in function_query.captures(root_node):
        func_name = node.text.decode('utf-8', errors='ignore')
        entities.add((func_name, "function"))

    # Execute Lock Primitives Query
    for node, tag in lock_query.captures(root_node):
        lock_fn = node.text.decode('utf-8', errors='ignore')
        entities.add((lock_fn, "lock_primitive"))

    # Execute Struct Layout Query
    for node, tag in struct_query.captures(root_node):
        struct_name = node.text.decode('utf-8', errors='ignore')
        # Filter for specific structures to avoid bloat
        if "crypto" in struct_name or "alg" in struct_name:
            entities.add((f"struct {struct_name}", "struct"))

    return entities

def extract_entities_from_patch(diff_text: str):
    """
    Reconstructs the pseudo-code context from git diff additions/deletions 
    and pipes it directly into the tree-sitter extractor.
    """
    # Isolate modified chunks to prevent parsing out unchanged boilerplate
    reconstructed_lines = []
    for line in diff_text.splitlines():
        if line.startswith('+') and not line.startswith('+++'):
            reconstructed_lines.append(line[1:])
        elif line.startswith('-') and not line.startswith('---'):
            reconstructed_lines.append(line[1:])
        elif line.startswith(' '):
            reconstructed_lines.append(line[1:])

    pseudo_c_code = "\n".join(reconstructed_lines)
    return extract_entities_from_ast(pseudo_c_code)

# ## TODO rm, REGEX parsing
# def extract_c_entities(diff_text):
#     """
#     Quickly isolates modified function names or structs using a basic regex 
#     or full tree-sitter parsing on the patch block.
#     """
#     entities = set()
#     # Find common patterns like function calls or definitions in git diffs
#     functions = re.findall(r'^[+-].*\b([a-zA-Z_][a-zA-Z0-9_]*)\s*\(', diff_text, re.MULTILINE)
#     structs = re.findall(r'struct\s+([a-zA-Z_][a-zA-Z0-9_]*)', diff_text)
#     
#     for f in functions:
#         if f not in ['if', 'while', 'for', 'switch', 'return', 'sizeof']:
#             entities.add((f, 'function'))
#     for s in structs:
#         entities.add((s, 'struct'))
#     return entities

def classify_bug(commit_msg):
## TODO make bug type configurable, this is just a first approach
    msg = commit_msg.lower()
    if 'use-after-free' in msg or 'uaf' in msg or 'freeing' in msg:
        return 'UAF'
    if 'race' in msg or 'data race' in msg or 'concurrency' in msg:
        return 'race'
    if 'lock' in msg or 'mutex' in msg or 'deadlock' in msg or 'spin_lock' in msg:
        return 'locking'
    return 'logic_error'

def build_knowledge_graph(repo_path, db_path):
    conn = setup_database(db_path)
    cursor = conn.cursor()
    repo = Repo(repo_path)
    
## TODO make this configurable
    paths = ['drivers/crypto', 'drivers/char/hw_random', 'include/crypto']
## TODO make this configurable 
    keywords = ['fix', 'bug', 'uaf', 'race', 'lock', 'leak', 'crash']
    
    print("Analyzing commits... This may take a couple of minutes.")
    for commit in repo.iter_commits(paths=paths, max_count=1000):
## TODO make count configurable
        msg = commit.message
        if not any(k in msg.lower() for k in keywords):
            continue
            
        bug_type = classify_bug(msg)
        description = f"Commit: {commit.hexsha[:8]}\nSummary: {msg.splitlines()[0]}\n\nDetails:\n{msg}"
## TODO this goes mainly by commit description - is it possible additionally to take code changes into account?
        
        # Insert bug pattern
        cursor.execute('''
            INSERT INTO bug_patterns (subsystem, bug_type, description, remedy_template)
            VALUES (?, ?, ?, ?)
        ''', ('crypto', bug_type, description, ""))
        pattern_id = cursor.lastrowid
        
        # Get diff to extract associated entities
#        if commit.parents:
#            diffs = commit.parents[0].diff(commit, create_patch=True)
#            for d in diffs:
#                if d.a_path and any(p in d.a_path for p in paths):
#                    diff_text = d.diff.decode('utf-8', errors='ignore')
#                    found_entities = extract_c_entities(diff_text)
#                    
#                    for name, e_type in found_entities:
#                        # Insert or ignore entity
#                        cursor.execute('INSERT OR IGNORE INTO entities (name, type) VALUES (?, ?)', (name, e_type))
#                        cursor.execute('SELECT id FROM entities WHERE name = ?', (name,))
#                        entity_id = cursor.fetchone()[0]
#                        
#                        # Link entity to bug pattern
#                        cursor.execute('INSERT OR IGNORE INTO bug_edges (entity_id, pattern_id) VALUES (?, ?)', (entity_id, pattern_id))

## TODO prefer the AST based tree sitter approach, than REGEX
        if commit.parents:
            diffs = commit.parents.diff(commit, create_patch=True) # TODO check if we need parents[0]
            for d in diffs:
                if d.a_path and any(p in d.a_path for p in paths):
                    diff_text = d.diff.decode('utf-8', errors='ignore')
                    found_entities = extract_entities_from_patch(diff_text)
                    
                    for name, e_type in found_entities:
                        # Insert or ignore entity
                        cursor.execute('INSERT OR IGNORE INTO entities (name, type) VALUES (?, ?)', (name, e_type))
                        cursor.execute('SELECT id FROM entities WHERE name = ?', (name,))
                        entity_id = cursor.fetchone()[0]
                        
                        # Link entity to the historical bug pattern
                        cursor.execute('INSERT OR IGNORE INTO bug_edges (entity_id, pattern_id) VALUES (?, ?)', (entity_id, pattern_id))
                        
    conn.commit()
    conn.close()
    print(f"Database generation complete! File saved to {db_path}")

if __name__ == '__main__':
## TODO do args
    KERNEL_REPO = "./linux" 
    OUTPUT_DB = "./crypto.db"
    build_knowledge_graph(KERNEL_REPO, OUTPUT_DB)

