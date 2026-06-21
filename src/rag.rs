//use rusqlite::{Connection, Result};
//use std::path::Path;
//
//#[derive(Debug)]
//pub struct HistoricalLesson {
//    pub bug_type: String,
//    pub description: String,
//}
//
//pub fn query_knowledge_base(db_path: &Path, entity_names: &[String]) -> Result<Vec<HistoricalLesson>> {
//    let conn = Connection::open(db_path)?;
//    
//    // Dynamically build variables for the SQL IN clause
//    if entity_names.is_empty() {
//        return Ok(vec![]);
//    }
//    
//    let placeholders: Vec<String> = entity_names.iter().map(|_| "?".to_string()).collect();
//    let query_str = format!(
//        "SELECT bp.bug_type, bp.description \
//         FROM bug_patterns bp \
//         JOIN bug_edges be ON bp.id = be.pattern_id \
//         JOIN entities e ON be.entity_id = e.id \
//         WHERE e.name IN ({}) LIMIT 5",
//        placeholders.join(", ")
//    );
//
//    let mut stmt = conn.prepare(&query_str)?;
//    
//    // Bind parameters safely
//    let params = rusqlite::params_from_iter(entity_names.iter());
//    
//    let rows = stmt.query_map(params, |row| {
//        Ok(HistoricalLesson {
//            bug_type: row.get(0)?,
//            description: row.get(1)?,
//        })
//    })?;
//
//    let mut lessons = Vec::new();
//    for row in rows {
//        if let Ok(lesson) = row {
//            lessons.push(lesson);
//        }
//    }
//    
//    Ok(lessons)
//}

// src/rag.rs
use libsql::Connection;

#[derive(Debug)]
pub struct HistoricalLesson {
    pub bug_type: String,
    pub description: String,
}

pub async fn query_knowledge_base(
    conn: &Connection, 
    entity_names: &[String]
) -> Result<Vec<HistoricalLesson>, Box<dyn std::error::Error>> {
    if entity_names.is_empty() {
        return Ok(vec![]);
    }

    let placeholders: Vec<String> = (1..=entity_names.len())
        .map(|i| format!("?{}", i))
        .collect();

    let query_str = format!(
        "SELECT bp.bug_type, bp.description \
         FROM rag_bug_patterns bp \
         JOIN rag_bug_edges be ON bp.id = be.pattern_id \
         JOIN rag_entities e ON be.entity_id = e.id \
         WHERE e.name IN ({}) LIMIT 5",
        placeholders.join(", ")
    );

    let params: Vec<libsql::Value> = entity_names
        .iter()
        .map(|name| libsql::Value::from(name.clone()))
        .collect();

    let mut rows = conn.query(&query_str, libsql::params_from_iter(params)).await?;
    let mut lessons = Vec::new();

    while let Some(row) = rows.next().await? {
        lessons.push(HistoricalLesson {
            bug_type: row.get(0)?,
            description: row.get(1)?,
        });
    }

    Ok(lessons)
}
