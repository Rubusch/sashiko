#!/usr/bin/env python3
"""
knowledge RAG via sqlite db matching Sashiko's internal layout

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

def setup_database(db_path):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.executescript('''
        CREATE TABLE IF NOT EXISTS rag_entities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            type TEXT NOT NULL,          -- 'function', 'lock_primitive', 'struct'
            file_path TEXT,
            source_code TEXT             -- Stores patch snippets/context
        );
        CREATE TABLE IF NOT EXISTS rag_bug_patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subsystem TEXT NOT NULL,
            bug_type TEXT NOT NULL,      -- 'UAF', 'race', 'locking', 'logic_error'
            description TEXT NOT NULL,
            remedy_template TEXT
        );
        CREATE TABLE IF NOT EXISTS rag_bug_edges (
            entity_id INTEGER,
            pattern_id INTEGER,
            relation_type TEXT DEFAULT 'associated_with',
            PRIMARY KEY(entity_id, pattern_id),
            FOREIGN KEY(entity_id) REFERENCES rag_entities(id) ON DELETE CASCADE,
            FOREIGN KEY(pattern_id) REFERENCES rag_bug_patterns(id) ON DELETE CASCADE
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

    tree = parser.parse(bytes(file_content, "utf8"))
    root_node = tree.root_node

    entities = set()

    function_query = C_LANGUAGE.query("""
        (function_definition
            declarator: (function_declarator
                declarator: (identifier) @func_name))
    """)

    lock_query = C_LANGUAGE.query("""
        (call_expression
            function: (identifier) @lock_fn
            (#match? @lock_fn "^(spin_lock|mutex_lock|spin_unlock|mutex_unlock|spin_lock_irqsave|spin_unlock_irqrestore)$"))
    """)

    struct_query = C_LANGUAGE.query("""
        (struct_specifier
            name: (identifier) @struct_name)
    """)

    for node, tag in function_query.captures(root_node):
        func_name = node.text.decode('utf-8', errors='ignore')
        entities.add((func_name, "function"))

    for node, tag in lock_query.captures(root_node):
        lock_fn = node.text.decode('utf-8', errors='ignore')
        entities.add((lock_fn, "lock_primitive"))

    for node, tag in struct_query.captures(root_node):
        struct_name = node.text.decode('utf-8', errors='ignore')
        if "crypto" in struct_name or "alg" in struct_name:
            entities.add((f"struct {struct_name}", "struct"))

    return entities

def extract_entities_from_patch(diff_text: str):
    """
    Reconstructs the pseudo-code context from git diff additions/deletions
    and pipes it directly into the tree-sitter extractor.
    """
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

    print(f"Analyzing commits for paths: {paths}... This may take a couple of minutes.")
    parsed_commits = 0
    linked_edges = 0

    for commit in repo.iter_commits(paths=paths, max_count=1000):
## TODO make count configurable
        msg = commit.message
        if not any(k in msg.lower() for k in keywords):
            continue

        bug_type = classify_bug(msg)
        description = f"Commit: {commit.hexsha[:8]}\nSummary: {msg.splitlines()[0]}\n\nDetails:\n{msg}"
## TODO this goes mainly by commit description - is it possible additionally to take code changes into account?

        cursor.execute('''
            INSERT INTO rag_bug_patterns (subsystem, bug_type, description, remedy_template)
            VALUES (?, ?, ?, ?)
        ''', ('crypto', bug_type, description, ""))
        pattern_id = cursor.lastrowid
        parsed_commits += 1

        if commit.parents:
            diffs = commit.parents[0].diff(commit, create_patch=True)

            for diff in diffs:
                file_path = diff.b_path or diff.a_path
                if not file_path:
                    continue

                if not any(p in file_path for p in paths):
                    continue

                diff_text = diff.diff.decode('utf-8', errors='ignore') if diff.diff else ""
                if not diff_text:
                    continue

                found_entities = extract_entities_from_patch(diff_text)

                for entity_name, entity_type in found_entities:
                    cursor.execute('''
                        INSERT INTO rag_entities (name, type, file_path, source_code)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(name) DO UPDATE SET
                            source_code = excluded.source_code,
                            file_path = excluded.file_path
                    ''', (entity_name, entity_type, file_path, diff_text[:2000]))

                    cursor.execute('SELECT id FROM rag_entities WHERE name = ?', (entity_name,))
                    row = cursor.fetchone()
                    if row:
                        entity_id = row[0]
                        cursor.execute('''
                            INSERT OR IGNORE INTO rag_bug_edges (entity_id, pattern_id)
                            VALUES (?, ?)
                        ''', (entity_id, pattern_id))
                        linked_edges += 1

    conn.commit()
    conn.close()
    print(f"Successfully processed {parsed_commits} bug commits and linked {linked_edges} AST entity relationships.")

if __name__ == '__main__':
## TODO do args
    KERNEL_REPO = "./linux"
    OUTPUT_DB = "./crypto.db"
    build_knowledge_graph(KERNEL_REPO, OUTPUT_DB)
